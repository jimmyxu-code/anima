"""豆包端到端实时语音（Realtime/S2S）协议客户端。

协议要点（官方文档 6561/1594356）：
- wss://openspeech.bytedance.com/api/v3/realtime/dialogue
- 鉴权头：X-Api-App-ID / X-Api-Access-Key / X-Api-Resource-Id=volc.speech.dialog
  / X-Api-App-Key=PlgvMymc7f3tQnJ6（文档给定固定值）
- 二进制帧：4字节头 [ver<<4|hdr_size] [msg_type<<4|flags] [serial<<4|compress] [0]
  + 可选 code(4B) / sequence(4B) / event(4B) / connect_id / session_id
  + payload_size(4B) + payload
- 轮流说话/打断由服务端模型负责：ASRInfo(450)=用户开说（客户端应停播报），
  ASREnded(459)=说完，TTSResponse(352)=音频分片，ASRResponse(451)=识别文本。

同步 API（内部 asyncio 循环跑在专属线程）：
    rt = DoubaoRT(app_id, access_key, on_audio=..., on_event=..., on_error=...)
    rt.start()                 # 建连
    rt.start_session(config)   # 开聊，返回 dialog_id
    rt.send_audio(pcm_bytes)   # 20ms 一包 PCM s16le 16kHz
    rt.chat_text("你好")        # 文本注入（测试/RAG 用）
    rt.finish_session()        # 结束会话（连接可复用）
"""

import asyncio
import gzip
import json
import queue
import ssl
import struct
import threading
import time
import uuid

import certifi

WSS_URL = "wss://openspeech.bytedance.com/api/v3/realtime/dialogue"
APP_KEY_FIXED = "PlgvMymc7f3tQnJ6"   # 文档给定的固定值

# 事件 ID
EV_START_CONNECTION = 1
EV_FINISH_CONNECTION = 2
EV_START_SESSION = 100
EV_FINISH_SESSION = 102
EV_TASK_REQUEST = 200
EV_SAY_HELLO = 300
EV_CHAT_TTS_TEXT = 500
EV_CHAT_TEXT_QUERY = 501
EV_CHAT_RAG_TEXT = 502
# 服务端
EV_CONNECTION_STARTED = 50
EV_CONNECTION_FAILED = 51
EV_SESSION_STARTED = 150
EV_SESSION_FINISHED = 152
EV_SESSION_FAILED = 153
EV_TTS_SENTENCE_START = 350
EV_TTS_SENTENCE_END = 351
EV_TTS_RESPONSE = 352
EV_TTS_ENDED = 359
EV_USAGE = 154   # UsageResponse：每轮交互的 token 用量
EV_ASR_INFO = 450
EV_ASR_RESPONSE = 451
EV_ASR_ENDED = 459
EV_CHAT_RESPONSE = 550
EV_CHAT_ENDED = 559
EV_DIALOG_ERROR = 599

_CONNECT_EVENTS = {EV_CONNECTION_STARTED, EV_CONNECTION_FAILED, 52}


def _frame(msg_type, flags, serialization, event=None, session_id=None,
           payload=b""):
    """组一帧。flags: 0b0100=带 event。session 级事件须带 session_id。"""
    out = bytearray()
    out.append(0x11)                                  # v1, header 4 字节
    out.append((msg_type << 4) | flags)
    out.append(serialization << 4)                    # 无压缩
    out.append(0x00)
    if event is not None:
        out += struct.pack(">i", event)
    if session_id is not None:
        sid = session_id.encode()
        out += struct.pack(">i", len(sid))
        out += sid
    out += struct.pack(">i", len(payload))
    out += payload
    return bytes(out)


def _parse_frames(data):
    """解一帧（服务端消息总是一帧一条）。返回 dict。"""
    if len(data) < 8:
        return None
    b1, b2 = data[1], data[2]
    msg_type = b1 >> 4
    flags = b1 & 0x0F
    compression = b2 & 0x0F
    pos = 4
    res = {"msg_type": msg_type, "event": None, "payload": b""}
    if msg_type == 0b1111:                            # error 帧带 code
        res["code"] = struct.unpack(">i", data[pos:pos + 4])[0]
        pos += 4
    if flags & 0b0011:                                # 0b0001/10/11 都带 sequence
        pos += 4
    if flags & 0b0100:
        res["event"] = struct.unpack(">i", data[pos:pos + 4])[0]
        pos += 4
    ev = res["event"]
    if ev is not None:
        if ev in _CONNECT_EVENTS:                     # connect 级带 connect id
            cid_size = struct.unpack(">i", data[pos:pos + 4])[0]
            pos += 4 + cid_size
        else:                                         # session 级带 session id
            sid_size = struct.unpack(">i", data[pos:pos + 4])[0]
            pos += 4
            res["session_id"] = data[pos:pos + sid_size].decode("utf-8", "replace")
            pos += sid_size
    if pos + 4 > len(data):
        return res
    psize = struct.unpack(">i", data[pos:pos + 4])[0]
    pos += 4
    payload = data[pos:pos + psize]
    if compression == 1 and payload:
        try:
            payload = gzip.decompress(payload)
        except OSError:
            pass
    res["payload"] = payload
    return res


class DoubaoRT:
    def __init__(self, app_id, access_key, on_audio=None, on_event=None,
                 on_error=None, log=print):
        self.app_id = app_id
        self.access_key = access_key
        self.on_audio = on_audio or (lambda b: None)
        self.on_event = on_event or (lambda ev, obj: None)
        self.on_error = on_error or (lambda m: None)
        self.log = log
        self.session_id = None
        self.dialog_id = None
        self._loop = None
        self._ws = None
        self._session = None
        self._send_q = None
        self._ready = threading.Event()
        self._session_on = threading.Event()
        self._loop_started = threading.Event()

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
            frame = _frame(0b0010, 0b0100, 0, event=EV_TASK_REQUEST,
                           session_id=self.session_id, payload=pcm)
            asyncio.run_coroutine_threadsafe(self._ws.send_bytes(frame),
                                             self._loop)

    def chat_text(self, text):
        self._send_event(EV_CHAT_TEXT_QUERY, {"content": text})

    def rag_text(self, external_rag_json):
        self._send_event(EV_CHAT_RAG_TEXT, {"external_rag": external_rag_json})

    def say_hello(self, content="你好"):
        self._send_event(EV_SAY_HELLO, {"content": content})

    def finish_session(self):
        self._send_event(EV_FINISH_SESSION, {})
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
        threading.Thread(target=_run, name="doubao-rt", daemon=True).start()
        self._loop_started.wait(5)

    def _send_event(self, event, obj, serialization=1):
        if self._loop is None or self._ws is None:
            return
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        frame = _frame(0b0001, 0b0100, serialization, event=event,
                       session_id=self.session_id, payload=payload)
        asyncio.run_coroutine_threadsafe(self._ws.send_bytes(frame), self._loop)

    async def _connect(self):
        import aiohttp
        # 新版控制台只需 X-Api-Key（旧版 appid/token/appkey 三元组不再使用）
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
        # StartConnection
        frame = _frame(0b0001, 0b0100, 1, event=EV_START_CONNECTION,
                       payload=b"{}")
        await self._ws.send_bytes(frame)
        self._ready.set()
        return True

    async def _start_session(self, config):
        self.session_id = str(uuid.uuid4())
        payload = json.dumps(config, ensure_ascii=False).encode("utf-8")
        frame = _frame(0b0001, 0b0100, 1, event=EV_START_SESSION,
                       session_id=self.session_id, payload=payload)
        await self._ws.send_bytes(frame)
        self._session_on.set()
        return True

    async def _recv_loop(self):
        import aiohttp
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    fr = _parse_frames(msg.data)
                    if fr is None:
                        continue
                    self._dispatch(fr)
                elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                  aiohttp.WSMsgType.CLOSING,
                                  aiohttp.WSMsgType.ERROR):
                    break
        except Exception as e:
            self.on_error(f"recv: {e}")
        self.on_error("连接已断开")

    def _dispatch(self, fr):
        ev = fr["event"]
        payload = fr["payload"]
        if fr["msg_type"] == 0b1011:                  # 音频
            self.on_audio(payload)
            return
        obj = {}
        if payload:
            try:
                obj = json.loads(payload.decode("utf-8", "replace"))
            except ValueError:
                obj = {}
        if ev == EV_SESSION_STARTED:
            self.dialog_id = obj.get("dialog_id")
        elif ev in (EV_SESSION_FAILED, EV_CONNECTION_FAILED, EV_DIALOG_ERROR):
            self.on_error(f"event {ev}: {obj}")
        self.on_event(ev, obj)

    async def _close(self):
        self._session_on.clear()
        try:
            if self._ws is not None and not self._ws.closed:
                frame = _frame(0b0001, 0b0100, 1, event=EV_FINISH_CONNECTION,
                               payload=b"{}")
                await self._ws.send_bytes(frame)
                await self._ws.close()
        except Exception:
            pass
        try:
            if self._session is not None:
                await self._session.close()
        except Exception:
            pass
        for t in (getattr(self, "_recv_task", None),
                  getattr(self, "_sender_task", None)):
            if t:
                t.cancel()
