"""TTS 播报：流式合成 + 流式播放（商业化合规版）。

合成走引擎抽象层 tts_engines（默认豆包 TTS 2.0 HTTP Chunked 单向流式，
边收边喂 miniaudio 流式解码），引擎失败一律回退 Windows SAPI 离线合成，
绝不出声失败。edge-tts 已移除（GPL-3.0 + 微软未授权端点，商业化红线）。
Speaker: 句子队列，后台串行播报。speak(text): 一次性整段播报。
"""

import asyncio
import os
import queue
import tempfile
import threading
import time

import miniaudio

_tmp_dir = os.path.join(tempfile.gettempdir(), "desktop_agent_tts")
os.makedirs(_tmp_dir, exist_ok=True)


def _null_log(msg):
    """引擎层日志直通 stdout（agent_flow 的 log 在 Speaker 层另有一份）。"""
    print(msg, flush=True)

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
_full_cache = {"t": 0.0, "v": {}}


def _cfg_full():
    """整份 config.json（5 秒缓存），给引擎层用。"""
    if time.time() - _full_cache["t"] > 5:
        try:
            import json
            with open(_CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                _full_cache["v"] = json.load(f)
        except (OSError, ValueError):
            _full_cache["v"] = {}
        _full_cache["t"] = time.time()
    return _full_cache["v"]


_seq = 0
_seq_lock = threading.Lock()
is_speaking = threading.Event()   # 播放期间置位，调用方用它做回声防护
_stop_playback = threading.Event()  # 置位时流式播放立即中断（Speaker.stop 用）
_active_speakers = []               # 活着的 Speaker 实例（stop_all 用）
_now_saying = {"cur": "", "last": ""}   # 正在/刚刚播报的句子（回声过滤参照物）


def now_saying():
    """共享嗓当前播报文本 + 上一句（回声过滤用）。"""
    return _now_saying["cur"], _now_saying["last"]


class _ChunkSource(miniaudio.StreamableSource):
    """Queue-backed byte stream: the synth producer feeds mp3 chunks,
    miniaudio's decoder pulls read() on the playback thread."""

    def __init__(self):
        self._q = queue.Queue()
        self._buf = bytearray()
        self._eof = False
        self.error_in_readcallback = None
        self.failed = False          # 合成失败标记（_play_streaming 据它回退 SAPI）

    def feed(self, data):
        self._q.put(data)

    def feed_eof(self):
        self._q.put(None)

    def read(self, num_bytes):
        while len(self._buf) < num_bytes and not self._eof:
            item = self._q.get()
            if item is None:
                self._eof = True
            else:
                self._buf += item
        out = bytes(self._buf[:num_bytes])
        del self._buf[:num_bytes]
        return out

    def close(self):
        self._eof = True


async def _produce(text, source):
    # 引擎层（config.json 的 tts_engine，默认 doubao 流式）；
    # 失败不硬撑，标记后由调用方回退 SAPI 离线保底，绝不出声失败
    ok = False
    try:
        import tts_engines
        ok = await asyncio.to_thread(tts_engines.synth, text, _cfg_full(),
                                     source.feed, _null_log)
    except Exception:
        ok = False
    source.feed_eof()
    source.failed = not ok


def _play_streaming(text, log=print):
    """Synthesize and play concurrently; returns when playback finishes.
    引擎失败时抛异常（调用方回退 SAPI）。"""
    source = _ChunkSource()
    producer = threading.Thread(target=lambda: asyncio.run(_produce(text, source)), daemon=True)
    producer.start()

    gen = miniaudio.stream_any(
        source,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=24000,
    )
    next(gen)  # prime the generator before handing it to the device
    done = threading.Event()

    def _driven():
        try:
            yield from gen
        finally:
            done.set()

    wrapped = _driven()
    next(wrapped)  # the device send()s frame counts, so this generator must
                   # already be started too
    dev = miniaudio.PlaybackDevice(
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1, sample_rate=24000, buffersize_msec=100,
    )
    try:
        dev.start(wrapped)
        # 硬超时 + 可中断：输出设备死亡不挂线程，用户喊停立即退出
        deadline = time.time() + 8.0 + len(text) * 0.2
        while not done.wait(0.2):
            if _stop_playback.is_set():
                log("TTS: 被叫停")
                break
            if time.time() > deadline:
                log(f"TTS: 播放超时，丢弃这段")
                break
    finally:
        try:
            dev.close()
        except Exception:
            pass
    if source.failed:
        raise RuntimeError("TTS 引擎合成失败")


class Speaker:
    """逐句流式播报：enqueue() 喂句子，finish() 收尾，wait_done() 等播完。
    stop() 立刻清空队列并停掉当前播放（用户喊"停"时用）。
    全局单声道约定：所有播报走 get_shared() 那一条嗓子，永不叠音。"""

    def __init__(self, log=print):
        self._q = queue.Queue()
        self._log = log
        self._stopped = False
        self._pending = 0                  # 已入队未播完的句子数
        self._pending_lock = threading.Lock()
        self._drained = threading.Event()  # 队列排空+播放完毕时置位
        self._drained.set()
        _active_speakers.append(self)
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def stop(self):
        _stop_playback.set()
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        is_speaking.clear()
        with self._pending_lock:      # 清场：等待者必须放行，不得卡死
            self._pending = 0
            self._drained.set()
        threading.Timer(1.0, _stop_playback.clear).start()
        if getattr(self, "_shared_instance", False):
            self._stopped = False   # 共享嗓不死：清场后继续服役
        else:
            self._stopped = True

    def enqueue(self, sentence):
        if self._stopped:
            return
        sentence = (sentence or "").strip()
        if sentence:
            with self._pending_lock:
                self._pending += 1
                self._drained.clear()
            self._q.put(sentence)

    def finish(self):
        self._q.put(None)

    def wait_done(self, timeout=None):
        # 等"队列排空且最后一句播完"。共享嗓工作线程永不下班，
        # 绝不能 join 线程本身（join 会永久阻塞，锁死整轮对话）。
        self._drained.wait(timeout)

    def _run(self):
        while True:
            sentence = self._q.get()
            if sentence is None and getattr(self, "_shared_instance", False):
                # 冲刷标记 = 本轮句子全部播完，归还麦克风。
                # 不修这个，is_speaking 会永远卡住，麦克风被永久当"防自听"丢弃
                threading.Timer(0.4, is_speaking.clear).start()
                continue   # 共享嗓：哨兵只当冲刷标记，不下班
            if sentence is None or self._stopped:
                # 播报线程结束：无论队列状态，归还麦克风（0.4s 余量吸余音）
                threading.Timer(0.4, is_speaking.clear).start()
                try:
                    _active_speakers.remove(self)
                except ValueError:
                    pass
                return
            is_speaking.set()
            _now_saying["cur"] = sentence
            try:
                t0 = time.time()
                _play_streaming(sentence, self._log)
                self._log(f"TTS: 播完 ({time.time() - t0:.1f}s): {sentence[:30]}")
            except Exception as e:
                if not self._stopped:
                    self._log(f"TTS 引擎失败 ({e})，启用 SAPI 保底")
                    _speak_sapi(sentence, self._log)
            finally:
                # 队列里还有下一句就保持闭麦；真正播完才留 0.4s 余量放开
                if self._q.empty():
                    threading.Timer(0.4, is_speaking.clear).start()
                _now_saying["last"] = _now_saying["cur"]
                _now_saying["cur"] = ""
                with self._pending_lock:
                    self._pending -= 1
                    if self._pending <= 0:
                        self._pending = 0
                        self._drained.set()


def warmup(log=print):
    """预热合成路径（DNS/TLS），只合成不播放，第一口不冷。"""

    class _NullSource:
        failed = False

        def feed(self, data):
            pass

        def feed_eof(self):
            pass

    try:
        asyncio.run(_produce("嗯", _NullSource()))
    except Exception:
        pass


_shared = None
_shared_lock = threading.Lock()


def get_shared(log=print):
    """全局唯一嗓音：所有播报（主回复/插话/后台通知）共用这一条队列，
    永不叠音。长生命周期；stop() 只清场不下班。"""
    global _shared
    with _shared_lock:
        if _shared is None or (getattr(_shared, "_stopped", False)
                               and not getattr(_shared, "_shared_instance", False)):
            _shared = Speaker(log=log)
            _shared._shared_instance = True
        return _shared


def stop_all(log=print):
    """停掉所有正在播报的 Speaker（用户叫停时用）。"""
    for s in list(_active_speakers):
        try:
            s.stop()
        except Exception:
            pass
    _active_speakers.clear()


def _speak_sapi(text, log=print):
    """最后的保底：Windows SAPI 离线合成。引擎全挂时也必须能出声。"""
    try:
        import subprocess
        ps = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$s.Rate = 1; $s.Speak([Console]::In.ReadToEnd())"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       input=text, text=True, timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log(f"SAPI ERROR: {e}")


def speak(text, log=print):
    """一次性整段播报（阻塞到播完）。"""
    text = (text or "").strip()
    if text:
        is_speaking.set()
        try:
            _play_streaming(text, log)
        except Exception:
            _speak_sapi(text, log)
        finally:
            threading.Timer(0.4, is_speaking.clear).start()


if __name__ == "__main__":
    t0 = time.time()
    spk = Speaker()
    spk.enqueue("你好，我是你的电脑伙伴。")
    spk.enqueue("流式播报已经打通了，现在你听到的每一句话都是边说边生成的。")
    spk.finish()
    spk.wait_done()
    print(f"total {time.time() - t0:.1f}s")
