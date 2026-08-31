"""感知层：唯一的麦克风/VAD/打断/鼠标长按/光球实现。

交互层（companion_lite）与干活层（agent_flow，休眠中）共用本模块。
任何感知行为的修改只允许发生在这里——两份拷贝曾经漂移出"一个来回"级 bug。

接线方式（由主程序 init 一次）：
    init(log_fn, on_utterance, should_listen)
    - on_utterance(frames, session_open)：一段完整语音断句完毕
    - should_listen()：会话外是否保持开麦（唤醒词开关在主程序侧）
"""

import os
import re
import sys
import threading
import time
from collections import deque


import numpy as np
import sounddevice as sd

import orb_overlay  # noqa: E402  （2026-08-31 起收编进本仓库根目录）
import tts_speaker

# 光球视觉尺寸对齐 WhisperFlow（2026-08-30 实锤：本进程 per-monitor DPI 感知
# =不缩放，WhisperFlow 由 DWM 放大——同 150 基准视觉差 1.5 倍；按系统 DPI 放大）。
try:
    import ctypes as _ct
    orb_overlay.set_scale(_ct.windll.user32.GetDpiForSystem() / 96)
except Exception:
    pass

# --- 参数 ---
HOLD_THRESHOLD_SECONDS = 0.35
MOVE_CANCEL_PX = 10
SAMPLE_RATE = 16000
BLOCK = 1600                    # 0.1s 每块
VAD_START_RMS = 0.006           # 本机麦安静人声 ~0.01，阈值给足灵敏度
VAD_STOP_RMS = 0.0035
VAD_STOP_SECONDS = 0.7          # 句尾静默：防腰斩（中途停顿不抢答）
GRACE_SECONDS = 0.4             # 断句宽限：0.4s 内再开口就并成一句
VAD_MAX_UTTERANCE = 20.0
VAD_PREBUFFER = 3

# --- 接线 ---
log = print
_on_utterance = None            # fn(frames, session_open)
_on_busy_utterance = None       # fn(frames)：一轮进行中又收到语音（叫停/确认用）
_should_listen = lambda: True   # fn() -> bool

_session = {"open": False, "close_req": False}
_session_lock = threading.Lock()
processing = threading.Event()  # 主程序持有：一轮对话进行中置位


_on_session_close = None        # fn()：会话关闭时主程序挂的清理（中止生成等）


def init(log_fn, on_utterance, should_listen, on_busy_utterance=None,
         on_session_close=None):
    global log, _on_utterance, _should_listen, _on_busy_utterance, _on_session_close
    log = log_fn
    _on_utterance = on_utterance
    _should_listen = should_listen
    _on_busy_utterance = on_busy_utterance
    _on_session_close = on_session_close


def session_open():
    with _session_lock:
        return _session["open"]


# --- 光球三态 ---
orb_overlay._PALETTES["listening"] = (
    (10, 46, 52), [(64, 224, 208), (72, 168, 255), (150, 255, 230)],
)
orb_overlay._PALETTES["speaking"] = orb_overlay._PALETTES["listening"]
orb_overlay._PALETTES["thinking"] = (
    (26, 20, 58), [(150, 130, 255), (96, 190, 255), (220, 170, 255)],
)
orb_overlay._PALETTES["confirm"] = (
    (52, 36, 10), [(255, 180, 64), (255, 214, 96), (255, 150, 80)],
)
orb_overlay._PALETTES["error"] = (
    (38, 10, 12), [(200, 60, 50), (160, 40, 44), (235, 110, 80)],
)
orb_overlay._PALETTES["agent"] = orb_overlay._PALETTES["listening"]  # 兼容旧引用


def orb_mode(mode):
    try:
        with orb_overlay._lock:
            if orb_overlay._state["visible"]:
                orb_overlay._state["mode"] = mode
    except Exception:
        pass


def orb_hide():
    """只负责让光球 UI 消失；会话/任务/进程语义由上层调用方决定。"""
    try:
        with orb_overlay._lock:
            orb_overlay._state["visible"] = False
    except Exception:
        pass


def orb_show():
    try:
        import ctypes
        user32 = ctypes.windll.user32
        size = orb_overlay._SIZE
        with orb_overlay._lock:
            keep = orb_overlay._state.get("user_pos")
            x, y = orb_overlay._state["x"], orb_overlay._state["y"]
        if not keep:
            pt = ctypes.wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
            x = min(max(pt.x - size // 2, 0), sw - size)
            y = pt.y - size - 12
            if y < 0:
                y = pt.y + 24
            y = min(y, sh - size - orb_overlay._BTN_H)   # 给悬停按钮区留位
        with orb_overlay._lock:
            orb_overlay._state.update(visible=True, mode="listening", x=x, y=y)
        orb_overlay._ensure_thread()
    except Exception:
        pass


def set_orb_click(cb):
    orb_overlay.set_click_handler(cb)


def set_orb_buttons(cb, state_cb):
    """光球正下方唯一贴身按钮（text=文字框开关）：cb(name) 点击回调，
    state_cb() 供按钮着色。"""
    orb_overlay.set_button_handler(cb, state_cb)


def _on_orb_click_default():
    with _session_lock:
        if _session["open"]:
            _session["close_req"] = True
            log("光球被点击，请求关闭会话")


# --- 麦克风 ---
stream = None
stream_lock = threading.Lock()
_last_cb = [time.time()]


def start_capture():
    global stream
    with stream_lock:
        try:
            if stream is not None:
                stream.stop()
                stream.close()
        except Exception:
            pass
        stream = None
        blocksize = 320 if _stream_cb is not None else BLOCK   # 裸流 20ms 一包
        try:
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                blocksize=blocksize, callback=audio_callback,
            )
            stream.start()
            _last_cb[0] = time.time()
            log("MIC: 常开录音开始")
        except Exception as e:
            log(f"MIC: 打开失败: {e}")


def stop_capture():
    global stream
    with stream_lock:
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            stream = None
            log("MIC: 已关闭")


def mic_running():
    with stream_lock:
        return stream is not None


# --- 裸流模式（realtime 内核用）：不做 VAD，原始音频块直送回调 ---
_stream_cb = None


def start_stream_capture(on_block):
    """麦克风原始流：20ms 一块直接回调（VAD 由服务端/模型负责）。"""
    global _stream_cb
    _stream_cb = on_block
    start_capture()


def stop_stream_capture():
    global _stream_cb
    _stream_cb = None
    stop_capture()


def mic_watchdog():
    while True:
        time.sleep(2)
        with stream_lock:
            s = stream
        if s is None:
            continue
        if time.time() - _last_cb[0] > 5:
            log("MIC: 回调超时（设备可能断开），重新打开")
            start_capture()


# --- VAD 状态机 ---
_vad = {
    "speaking": False,
    "frames": [],
    "quiet": 0.0,
    "prebuffer": deque(maxlen=VAD_PREBUFFER),
    "during": False,            # 本句是否在播报期间开始（回声判定用）
}
_grace = {"pending": None, "timer": None}   # 断句宽限合并
_heartbeat = {"at": 0.0, "max": 0.0}   # 诊断探针：会话中每 2s 报峰值电平


def audio_callback(indata, frames, time_info, status):
    """收音永不关闭：播报中也照常 VAD（回声过滤在主程序文本层做）。
    裸流模式下 VAD 全部旁路，原始块直送 realtime 客户端。"""
    _last_cb[0] = time.time()
    if _stream_cb is not None:
        try:
            _stream_cb(indata.copy())
        except Exception:
            pass
        return
    open_ = session_open()
    if not open_ and not _should_listen():
        return
    block = indata.copy()
    rms = float(np.sqrt(np.mean(block ** 2)))

    if open_:
        _heartbeat["max"] = max(_heartbeat["max"], rms)
        if time.time() - _heartbeat["at"] > 2.0:
            log(f"心跳: 近2s峰值 rms={_heartbeat['max']:.4f} "
                f"(起始阈值 {VAD_START_RMS})")
            _heartbeat["at"] = time.time()
            _heartbeat["max"] = 0.0

    dt = len(block) / SAMPLE_RATE
    if tts_speaker.is_speaking.is_set():
        _echo_recent.append(rms)   # 播报期间的电平样本 = 回声底噪
    if not _vad["speaking"]:
        _vad["prebuffer"].append(block)
        if rms >= VAD_START_RMS:
            _vad["speaking"] = True
            _vad["frames"] = list(_vad["prebuffer"])
            _vad["prebuffer"].clear()
            _vad["quiet"] = 0.0
            _vad["during"] = tts_speaker.is_speaking.is_set()
            log(f"VAD: 语音开始 (rms={rms:.4f})")
    else:
        _vad["frames"].append(block)
        if rms < VAD_STOP_RMS:
            _vad["quiet"] += dt
        else:
            _vad["quiet"] = 0.0
        dur = sum(len(f) for f in _vad["frames"]) / SAMPLE_RATE
        if _vad["quiet"] >= VAD_STOP_SECONDS or dur >= VAD_MAX_UTTERANCE:
            frames = _vad["frames"]
            during = _vad["during"]
            _vad["speaking"] = False
            _vad["frames"] = []
            _vad["quiet"] = 0.0
            _vad["during"] = False
            log(f"VAD: 语音结束 ({dur:.1f}s){' [播报中]' if during else ''}")
            if during:
                _dispatch(frames, during=True)    # 打断判定要即时，不走宽限
            else:
                _grace_merge(frames)              # 宽限合并防腰斩


def _grace_merge(frames):
    """断句后留 GRACE_SECONDS 续话窗口：窗口内的新语音并入同一句再派发。"""
    if _grace["timer"] is not None:
        _grace["timer"].cancel()
    _grace["pending"] = (frames if _grace["pending"] is None
                         else _grace["pending"] + frames)

    def _fire():
        f = _grace["pending"]
        _grace["pending"] = None
        if f:
            _dispatch(f, during=False)

    t = threading.Timer(GRACE_SECONDS, _fire)
    t.daemon = True
    _grace["timer"] = t
    t.start()


# 回声能量门：播报期间记录回声底噪（25 分位），
# 插话的峰值能量必须显著超过底噪才送识别——绝大多数回声在这里就被拦下
_echo_recent = deque(maxlen=60)


def _utterance_peak(frames):
    peak = 0.0
    for f in frames:
        r = float(np.sqrt(np.mean(f ** 2)))
        if r > peak:
            peak = r
    return peak


def _dispatch(frames, during):
    if during:
        recent = list(_echo_recent)
        if recent:
            floor = float(np.percentile(recent, 25))
            peak = _utterance_peak(frames)
            if peak < floor * 1.8:
                log(f"回声能量忽略 (peak={peak:.4f} < floor={floor:.4f}*1.8)")
                return
    if processing.is_set():
        if _on_busy_utterance is not None:
            threading.Thread(target=_on_busy_utterance, args=(frames, during),
                             daemon=True).start()
        else:
            log("一轮进行中，丢弃本次语音")
        return
    if _on_utterance is not None:
        threading.Thread(
            target=_on_utterance, args=(frames, session_open(), during),
            daemon=True).start()


# --- 流式切句播报（两个主程序共用） ---
class StreamSpeaker:
    """把流式字增量按句切分喂给全局共享嗓；首段逗号级早切，尽快开口。"""

    def __init__(self):
        self.spk = tts_speaker.get_shared(log=log)
        self.buf = []
        self.parts = []
        self._emitted = False

    def feed(self, chunk):
        self.parts.append(chunk)
        self.buf.append(chunk)
        text_so_far = "".join(self.buf)
        while True:
            m = re.search(r"[。！？!?；;：:\n]", text_so_far)
            if m:
                end = m.end()
            elif not self._emitted and len(text_so_far) >= 4:
                m2 = re.search(r"[，,、]", text_so_far)
                if not m2:
                    break
                end = m2.end()
            elif self._emitted and len(text_so_far) >= 30:
                commas = list(re.finditer(r"[，,、]", text_so_far[:30]))
                if not commas:
                    break
                end = commas[-1].end()
            else:
                break
            sentence, text_so_far = text_so_far[:end], text_so_far[end:]
            self.spk.enqueue(sentence)
            self._emitted = True
        self.buf.clear()
        self.buf.append(text_so_far)

    def flush(self):
        rest = "".join(self.buf).strip()
        if rest:
            self.spk.enqueue(rest)
        self.buf.clear()
        self.spk.finish()

    def wait(self):
        self.spk.wait_done()

    def text(self):
        return "".join(self.parts)


# --- 会话开关 ---
def open_session(greet=True):
    with _session_lock:
        if _session["open"]:
            return
        _session["open"] = True
        _session["close_req"] = False
    orb_show()
    if not mic_running():
        start_capture()
    if greet:
        orb_overlay.pulse(0.7)   # 亮闪代替"在呢"
    log("=== 会话开启 ===")


def close_session():
    with _session_lock:
        _session["open"] = False
        _session["close_req"] = False
    if _on_session_close is not None:
        try:
            _on_session_close()      # 先中止生成/干活，再清播报
        except Exception:
            pass
    tts_speaker.stop_all(log=log)
    if not _should_listen():
        stop_capture()
    orb_overlay.notify_done()
    log("=== 会话关闭 ===")


def session_watcher():
    while True:
        with _session_lock:
            req = _session["close_req"]
        if req:
            close_session()
        time.sleep(0.1)


# --- 左键监听（原地长按唤醒/关闭；事件一律放行） ---
_press = {"at": None, "pos": None, "moved": False, "generation": 0}
_injected_click = {"t": 0.0, "pos": (0, 0)}   # agent 注入左键记账（LLMHF_INJECTED）

def recent_injected_click(within_s=2.0):
    """最近 within_s 秒内有没有注入左键按下（agent 自己的 SendInput）。
    光球全停/按钮只认真实物理点击（2026-08-31 自伤盾换维度实锤）。"""
    return bool(_injected_click["t"] and time.time() - _injected_click["t"] < within_s)


def _on_left_down(x, y):
    _press["at"] = time.perf_counter()
    _press["pos"] = (x, y)
    _press["moved"] = False
    _press["generation"] += 1
    gen = _press["generation"]

    def _watcher():
        time.sleep(HOLD_THRESHOLD_SECONDS)
        if _press["at"] is None or _press["moved"] or gen != _press["generation"]:
            return
        if session_open():
            log(">>> 会话中再次长按，关闭会话")
            close_session()
        else:
            log(">>> 左键长按，开启会话")
            open_session()

    threading.Thread(target=_watcher, daemon=True).start()


def _on_left_move(x, y):
    if _press["at"] is not None and _press["pos"] is not None and not _press["moved"]:
        dx = x - _press["pos"][0]
        dy = y - _press["pos"][1]
        if dx * dx + dy * dy > MOVE_CANCEL_PX * MOVE_CANCEL_PX:
            _press["moved"] = True


def _on_left_up():
    _press["at"] = None
    _press["pos"] = None


def start_mouse_hook():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    WH_MOUSE_LL = 14
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    WM_MOUSEMOVE = 0x0200

    WPARAM_T = ctypes.c_size_t
    LPARAM_T = ctypes.c_ssize_t
    LRESULT_T = ctypes.c_ssize_t

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt", wintypes.POINT),
            ("mouseData", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT_T, ctypes.c_int, WPARAM_T, LPARAM_T)
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HANDLE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HANDLE
    user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, WPARAM_T, LPARAM_T]
    user32.CallNextHookEx.restype = LRESULT_T
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HANDLE,
                                   wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int

    LLMHF_INJECTED = 0x0001

    def _hook_proc(nCode, wParam, lParam):
        try:
            if nCode == 0:
                info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                # 注入事件（agent 自己的 SendInput/mouse_event）不算真实用户输入：
                # 否则 GUI agent 自己点的鼠标会误触长按唤醒/接管检测（P3a 注入鉴别）
                if info.flags & LLMHF_INJECTED:
                    if wParam == WM_LBUTTONDOWN:
                        _injected_click["t"] = time.time()
                        _injected_click["pos"] = (info.pt.x, info.pt.y)
                    return user32.CallNextHookEx(None, nCode, wParam, lParam)
                if wParam == WM_LBUTTONDOWN:
                    _on_left_down(info.pt.x, info.pt.y)
                elif wParam == WM_MOUSEMOVE:
                    _on_left_move(info.pt.x, info.pt.y)
                elif wParam == WM_LBUTTONUP:
                    _on_left_up()
        except Exception as e:
            log(f"HOOK ERROR: {e}")
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    global _hook_ref
    _hook_ref = HOOKPROC(_hook_proc)

    def _thread():
        hook = user32.SetWindowsHookExW(WH_MOUSE_LL, _hook_ref, None, 0)
        if not hook:
            log("MOUSE HOOK ERROR")
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(msg)
            user32.DispatchMessageW(msg)

    threading.Thread(target=_thread, name="left-button-hook", daemon=True).start()
