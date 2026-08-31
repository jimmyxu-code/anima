"""豆包全双工实时语音（Seeduplex，/api/v3/duplex/）协议客户端。

与 doubao_rt.py（半双工，二进制帧）同构的同步 API，供 companion_rt 按
config.rt_duplex 开关灰度切换——半双工路径一行不动（聊天层神圣）。

协议差异（官方文档 6561/2549778 + 2549732 接入必读）：
- 纯 JSON 文本帧（OpenAI realtime 风格）：session.create / input_audio_buffer.append
  （base64 音频）/ conversation.item.create / speech_text_buffer.commit。
- 下行：conversation.item.input_audio_transcription.*（用户语音事件，started=打断点）、
  response.output_audio.delta（TTS 音频块）、response.function_call_arguments.done（FC）、
  response.done（用量）。

2026-08-22 probe 实锤（probe_duplex_fc.py / 压测台账）：
- conversation.item.create 注入/提问 **不触发作答**（无 ack 无回复无报错，
  response.create 报 45000000 不存在）——RAG 注回在此协议下是死路，
  companion_rt 的 _inject_rag 走"本地人格渲染 + say_hello 确定性播报"。
- FC 全回路验证 PASS：session.tools 声明 → 语音轮次触发
  function_call_arguments.done（call_id/name/arguments）→ 本地执行 →
  conversation.item.create(role=tool, call_id, content[input_text]) →
  模型据结果续答（回传延迟 6s 内仍正常）。
- 保活纪律：上行音频不能断（52000033 无输入过久杀会话）；停采必须
  mute_input(True)，恢复 mute_input(False)。
- 打招呼/强制播报：speech_text_buffer.commit（say_hello）实测可用。
"""

import asyncio
import base64
import json
import queue
import ssl
import threading
import uuid

import certifi


class _OggOpusDecoder:
    """流式 OGG-Opus → PCM s16le 24k 解码器（PyAV 在进程内解）。
    双工协议默认下行就是 OGG-Opus（官方文档 6561/2549732：默认 OGG-Opus，
    PCM 需另配但我们实测各字段形态全部仍发 OGG——解码是唯一可靠路）。
    delta 分片经队列喂入，解码线程边收边出 PCM（48k→24k 重采样）。"""

    def __init__(self, on_pcm, log=print):
        self._on_pcm = on_pcm
        self._log = log
        self._q = queue.Queue()
        self._buf = bytearray()
        self._eof = False
        self._thread = None

    # --- 给 av.open 的 file-like：只读流，故意不提供 seek/tell——
    # PyAV 发现对象有 seek 就会去调，空实现等于"假装成功"，
    # demuxer 中途 seek 错乱直接断粮（2026-08-21 0.02s 事故的根因）。
    def read(self, n):
        while len(self._buf) < n and not self._eof:
            item = self._q.get()
            if item is None:
                self._eof = True
            else:
                self._buf += item
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def feed(self, data):
        self._q.put(data)

    def finish(self):
        self._q.put(None)

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="opus-dec")
            self._thread.start()

    def _run(self):
        try:
            import av
            from fractions import Fraction  # noqa
            container = av.open(self, format="ogg", mode="r")
            resampler = av.AudioResampler(format="s16", layout="mono", rate=24000)
            for packet in container.demux(audio=0):
                for frame in packet.decode():
                    for r in resampler.resample(frame):
                        # planes[0] 的缓冲区含对齐填充（960 有效实得 1088），
                        # 填充字节被当音频播就是电音——必须按真实采样数截断
                        self._on_pcm(bytes(r.planes[0])[:r.samples * 2])
        except Exception as e:
            self._log(f"opus 解码异常: {e}")

WSS_URL = "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue"

# 与 doubao_rt 对齐的事件常量（companion_rt 分发用）
EV_SESSION_STARTED = 150
EV_SESSION_FAILED = 153
EV_USAGE = 154
EV_TTS_SENTENCE_START = 350
EV_TTS_ENDED = 359
EV_ASR_INFO = 450        # 用户开说（客户端停播报）
EV_ASR_RESPONSE = 451    # 识别文本增量
EV_ASR_ENDED = 459       # 用户说完
EV_FUNCTION_CALL = 460   # FC 参数生成完成（response.function_call_arguments.done）
EV_CHAT_RESPONSE = 550   # 回复文本增量（response.output_text.delta，对齐 doubao_rt）
EV_DIALOG_ERROR = 599

_EVENT_MAP = {
    "response.output_audio.started": EV_TTS_SENTENCE_START,
    "response.output_audio.done": EV_TTS_ENDED,
    "response.output_text.delta": EV_CHAT_RESPONSE,
    "response.function_call_arguments.done": EV_FUNCTION_CALL,
    "response.done": EV_USAGE,
    "session.created": EV_SESSION_STARTED,
    "error": EV_DIALOG_ERROR,
}


class DoubaoDuplex:
    """与 DoubaoRT 相同的公共同步 API（start/start_session/send_audio/
    chat_text/rag_text/say_hello/finish_session/stop）。"""

    def __init__(self, app_id, access_key, on_audio=None, on_event=None,
                 on_error=None, log=print):
        self.app_id = app_id            # 新版控制台只用 access_key；保留签名兼容
        self.access_key = access_key
        self.on_audio = on_audio or (lambda b: None)
        self.on_event = on_event or (lambda ev, obj: None)
        self.on_error = on_error or (lambda m: None)
        self.log = log
        self.session_id = None          # 本地 uuid（仅日志对齐用）
        self.dialog_id = None           # session.created 下发（原 dialog id）
        self._loop = None
        self._ws = None
        self._session = None
        self._session_on = threading.Event()
        self._loop_started = threading.Event()
        self._asr_acc = {}          # item_id → 累计 ASR 文本（delta 归一用）
        self._decoders = {}         # response_id → OGG 解码器
        self._cur = None            # 当前音频流的桶号（delta 不带 rid 时用）
        self._stream_bytes = {}     # rid → 已喂字节数（2026-08-26 断流探针）

    # ---------------- 公共同步 API ----------------
    def start(self, timeout=10):
        self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        return fut.result(timeout)

    def start_session(self, config, timeout=10):
        fut = asyncio.run_coroutine_threadsafe(self._start_session(config),
                                               self._loop)
        return fut.result(timeout)

    def send_audio(self, pcm):
        if self._ws is not None and self._session_on.is_set() and self._loop:
            self._send_json({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            })

    def chat_text(self, text):
        """文本提问（测试用）：作为用户文本入会话。
        注意：双工无 response.create（实测 45000000 unknown event）——
        模型是否据文本项作答取决于服务端轮次判定，不保证开口。"""
        self._send_json({
            "type": "conversation.item.create",
            "items": [{"role": "user",
                       "content": [{"type": "input_text", "text": text}]}],
        })

    def rag_text(self, external_rag_json):
        """RAG 注回：包成用户侧文本项注入（无双工 ChatRAGText 等价物）。
        实测悬案：注入后模型可能不开口；调用方需要确定性播报时请走
        speak_text（ChatTTSText 同构的 replacement 通道）。"""
        self._send_json({
            "type": "conversation.item.create",
            "items": [{"role": "user",
                       "content": [{"type": "input_text",
                                    "text": external_rag_json}]}],
        })

    def speak_text(self, text):
        """speech_text_buffer.replacement.commit（ChatTTSText 同构）：
        客户端文本交模型按人设口语化后播读——注入播报的确定性兜底。"""
        self._send_json({"type": "speech_text_buffer.replacement.commit",
                         "text": text})

    def mute_input(self, muted):
        """音频保活纪律（接入必读）：停采必须发 mute，否则服务端等不到
        音频超时、模型无响应（45000003 十分钟无交互释放连接）。"""
        self._send_json({"type": "input_audio_mute.commit" if muted
                         else "input_audio_unmute.commit"})

    def say_hello(self, content="你好"):
        """强制让指定文本出声（打招呼/保底播报）。"""
        self._send_json({"type": "speech_text_buffer.commit",
                         "text": content})

    def send_function_output(self, call_id, output):
        """FC 结果回传（文档 6561/2549778：conversation.item.create，role=tool，
        call_id 原样带回；服务端配对后继续生成回复，无需 response.create）。"""
        self._send_json({
            "type": "conversation.item.create",
            "items": [{"call_id": call_id, "role": "tool",
                       "content": [{"type": "input_text",
                                    "text": str(output)[:4000]}]}],
        })

    def finish_session(self):
        self._send_json({"type": "session.close"})
        self._session_on.clear()

    def stop(self):
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._close(), self._loop)

    # ---------------- 内部 ----------------
    def _ensure_loop(self):
        if self._loop is not None:
            return

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._loop_started.set()
            loop.run_forever()
        threading.Thread(target=_run, name="doubao-duplex", daemon=True).start()
        self._loop_started.wait(5)

    def _send_json(self, obj):
        if self._loop is None or self._ws is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._ws.send_str(json.dumps(obj, ensure_ascii=False)), self._loop)

    async def _connect(self):
        import aiohttp
        headers = {
            "X-Api-Key": self.access_key,
            "X-Api-Resource-Id": "volc.speech.dialog",
            "X-Api-Connect-Id": uuid.uuid4().hex,
        }
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        self._session = aiohttp.ClientSession(trust_env=True)
        self._ws = await self._session.ws_connect(WSS_URL, headers=headers,
                                                  ssl=ssl_ctx, max_msg_size=0)
        self._recv_task = asyncio.ensure_future(self._recv_loop())
        return True

    async def _start_session(self, config):
        """把半双工版 StartSession 配置映射到双工 session.create：
        人格/纪律进 instructions；asr/tts/dialog 各节经 extension 原样透传。
        跨会话接续：带上次的 session.id（原 dialog id），服务端记最近 20 轮。"""
        self.session_id = str(uuid.uuid4())
        dialog = config.get("dialog", {})
        tts = config.get("tts", {})
        session = {
            "model": "1.2.6.1",
            "instructions": dialog.get("system_role", ""),
            "audio": {
                "input": {"format": "pcm"},          # 采样率 16K（协议硬性）
                "output": {
                    "format": "pcm_s16le",           # 24K（协议硬性）
                    "voice": tts.get("speaker", "zh_female_vv_jupiter_bigtts"),
                },
            },
            # 原 S2S 各节（asr/tts/dialog 含联网搜索、位置、speaking_style 等）透传
            "extension": {
                "asr": config.get("asr", {}),
                "tts": tts,
                "dialog": dialog,
            },
        }
        last_dialog = str(dialog.get("dialog_id") or "")
        if last_dialog:
            session["id"] = last_dialog   # 接续历史（文档 6561/2549778）
        tools = config.get("tools")   # FC 工具定义（标准 JSON Schema，可选）
        if tools:
            session["tools"] = tools
        self._send_json_nowait = None
        await self._ws.send_str(json.dumps(
            {"type": "session.create", "session": session}, ensure_ascii=False))
        self._session_on.set()
        return True

    async def _recv_loop(self):
        import aiohttp
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        obj = json.loads(msg.data)
                    except ValueError:
                        continue
                    self._dispatch(obj)
                elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                  aiohttp.WSMsgType.CLOSING,
                                  aiohttp.WSMsgType.ERROR):
                    break
        except Exception as e:
            self.on_error(f"recv: {e}")
        self.on_error("连接已断开")

    def _dispatch(self, obj):
        etype = obj.get("type", "")
        if etype == "response.output_audio.started":
            # 解码器按流管理（2026-08-22 两轮实锤）：delta 不带 response_id，
            # 纯按 rid 分桶会让数据流进无 finish 的死桶=播一半就断。
            # 策略：rid 是账，_cur 是当前流；delta/done 缺 rid 就落当前流。
            rid = obj.get("response_id") or "?"
            old = self._decoders.pop(self._cur, None) if self._cur else None
            if old is not None:   # 新流顶旧流：先干净收尾（EOS），不悬挂
                old.finish()
            # [audio] 探针（2026-08-26 断流定位）：started 是否一次回答多次触发
            self.log(f"[audio] started rid={rid} 顶旧流={self._cur}"
                     f"(fed={self._stream_bytes.get(self._cur, 0)}B)")
            self._stream_bytes.pop(self._cur, None)
            dec = _OggOpusDecoder(self.on_audio, log=self.log)
            self._decoders[rid] = dec
            self._cur = rid
            dec.start()
        elif etype == "response.output_audio.delta":
            raw = base64.b64decode(obj.get("delta") or obj.get("audio") or "")
            rid = obj.get("response_id") or self._cur or "?"
            dec = self._decoders.get(rid)
            if dec is None:
                dec = self._decoders[rid] = _OggOpusDecoder(
                    self.on_audio, log=self.log)
                self._cur = rid
                self.log(f"[audio] 无 started 直接 delta 建流 rid={rid}")
                dec.start()
            self._stream_bytes[rid] = self._stream_bytes.get(rid, 0) + len(raw)
            dec.feed(raw)
        elif etype == "response.output_audio.done":
            rid = obj.get("response_id") or self._cur or "?"
            dec = self._decoders.pop(rid, None)
            if dec is not None:
                dec.finish()
            # [audio] 探针：正常收尾的字节数（对照文字完整而音频截短的现场）
            self.log(f"[audio] done rid={rid} fed={self._stream_bytes.pop(rid, 0)}B")
            if self._cur == rid:
                self._cur = None
        if etype == "session.created":
            self.dialog_id = (obj.get("session") or {}).get("id")
            # 归一成半双工形态：companion_rt 读 obj["dialog_id"] 持久化——
            # 不归一它永远拿 None，跨会话 20 轮记忆全断（2026-08-22 实锤）。
            self.on_event(EV_SESSION_STARTED,
                          {**obj, "dialog_id": self.dialog_id})
            return
        if etype == "error":
            self.on_error(f"duplex error: {obj}")
        # ASR 三事件归一到 doubao_rt 的 results 形态（适配层职责：
        # companion_rt 事件分发零改动——此前只映射事件 ID 不归一载荷，
        # 导致双工下用户语音终稿从未进入路由，2026-08-22 实锤修复）。
        if etype == "conversation.item.input_audio_transcription.started":
            self.on_event(EV_ASR_INFO, obj)
            return
        if etype == "conversation.item.input_audio_transcription.delta":
            text = obj.get("delta", "")
            iid = obj.get("item_id", "")
            prev = self._asr_acc.get(iid, "")
            # 实测 delta 是累计快照（前缀增长）；防御增量形态：非前缀则累加
            acc = text if text.startswith(prev) else prev + text
            self._asr_acc[iid] = acc
            self.on_event(EV_ASR_RESPONSE,
                          {"results": [{"text": acc, "is_interim": True}]})
            return
        if etype == "conversation.item.input_audio_transcription.completed":
            text = obj.get("text", "")
            self._asr_acc.pop(obj.get("item_id", ""), None)
            if text.strip():   # 先终稿进路由，再句尾（与半双工时序一致）
                self.on_event(EV_ASR_RESPONSE,
                              {"results": [{"text": text,
                                            "is_interim": False}]})
            self.on_event(EV_ASR_ENDED, obj)
            return
        if etype == "conversation.item.input_audio_transcription.failed":
            self.on_event(EV_ASR_ENDED, obj)   # 失败也按句尾收尾，别卡中间态
            return
        ev = _EVENT_MAP.get(etype)
        if ev is not None:
            if etype == "response.output_text.delta":
                # 字段对齐 doubao_rt：companion_rt 读的是 content
                obj = {"content": obj.get("delta", "")}
            self.on_event(ev, obj)

    async def _close(self):
        self._session_on.clear()
        try:
            if self._ws is not None and not self._ws.closed:
                await self._ws.close()
        except Exception:
            pass
        try:
            if self._session is not None:
                await self._session.close()
        except Exception:
            pass
