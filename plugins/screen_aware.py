# -*- coding: utf-8 -*-
"""screen-aware 插件：三层屏幕感知（2026-08-26 用户批准立项"把三层做好"）。

L0 眼角余光（零成本）：WinEventHook 订阅前台窗口切换/标题变化——小凯
永远知道用户在哪个应用、看什么标题，不调任何模型。轮询兜底（钩子静默
死亡时 2s 内仍能跟上）。
L1 画面差分（近零成本）：1fps 缩略图感知哈希（aHash），只判"画面大变
没有"，不产出描述。
L2 按需细看（贵，不在本插件）：gui_agent 步边界事件校验 /
voice-fc look_screen 截屏问视觉模型——装配层把 context_line() 拼进
问题，让 L2 的描述天然带着 L0 的底账。

为什么不做实时流式截屏：带宽/算力/隐私三重不划算；ChatGPT 桌面共享/
Copilot Vision/Claude 桌面全部走"事件+关键帧"，没有一家常开流式。

消费接口（线程安全、非阻塞）：
    context_line()  一句话现状（给 L2 问题拼上下文用）
    snapshot()      完整状态 dict（app/title/最近事件/画面变化计数）
    recent_line()   最近 N 秒事件流水（调试/日志面板用）
"""

import ctypes
import threading
import time
from ctypes import wintypes

_lock = threading.Lock()
_state = {
    "app": "",            # 前台进程名（如 chrome.exe）
    "title": "",          # 前台窗口标题
    "since": 0.0,         # 当前前台持续了多久（起点）
    "events": [],         # [(ts, "切换到 chrome.exe《标题》"), ...] 最近 12 条
    "scene_changes": 0,   # 近 60s 画面大变化次数（滚动计数窗）
    "_scene_ts": [],      # 变化时间戳（60s 滚动）
}
_started = {"hook": False, "scene": False}

_EVENT_SYSTEM_FOREGROUND = 0x0003
_EVENT_OBJECT_NAMECHANGE = 0x800C
_WINEVENT_OUTOFCONTEXT = 0x0000
_WINEVENT_SKIPOWNPROCESS = 0x0010


def _window_title(hwnd):
    try:
        user32 = ctypes.windll.user32
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value.strip()
    except Exception:
        return ""


def _proc_name(hwnd):
    try:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x0410, False, pid.value)   # QUERY|VM_READ
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value.split("\\")[-1]
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        pass
    return ""


def _record_foreground(hwnd, why):
    title = _window_title(hwnd)
    if not title:
        return   # 桌面/空窗口不入账
    app = _proc_name(hwnd) or "?"
    now = time.time()
    with _lock:
        if app == _state["app"] and title == _state["title"]:
            return   # 无变化（NAMECHANGE 抖动/同窗刷新）
        _state["app"], _state["title"], _state["since"] = app, title, now
        _state["events"].append((now, f"{why} {app}《{title[:36]}》"))
        del _state["events"][:-12]


def _hook_thread():
    """L0 事件钩子线程：SetWinEventHook 需要消息泵，本线程专职泵消息。"""
    try:
        user32 = ctypes.windll.user32
        user32.SetWinEventHook.restype = ctypes.c_void_p
        user32.SetWinEventHook.argtypes = [
            wintypes.DWORD, wintypes.DWORD, wintypes.HINSTANCE,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
        WinEventProc = ctypes.WINFUNCTYPE(
            None, ctypes.c_void_p, wintypes.DWORD, wintypes.HWND,
            wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)

        _rate = {"last": 0.0}

        def cb(hook, event, hwnd, id_object, id_child, thread, ts):
            try:
                if id_object != 0:      # 只看 OBJID_WINDOW，子对象抖动全滤
                    return
                now = time.time()
                if event == _EVENT_OBJECT_NAMECHANGE:
                    # 标题变化只跟踪当前前台（后台窗口标题噪声大）
                    with _lock:
                        if now - _rate["last"] < 0.5:
                            return
                    if hwnd != user32.GetForegroundWindow():
                        return
                    _rate["last"] = now
                    _record_foreground(hwnd, "标题变化")
                elif event == _EVENT_SYSTEM_FOREGROUND:
                    if now - _rate["last"] < 0.15:
                        return
                    _rate["last"] = now
                    _record_foreground(hwnd, "切到")
            except Exception:
                pass   # 钩子回调里绝不许抛

        proc = WinEventProc(cb)
        flags = _WINEVENT_OUTOFCONTEXT | _WINEVENT_SKIPOWNPROCESS
        hooks = [
            user32.SetWinEventHook(e, e, None, proc, 0, 0, flags)
            for e in (_EVENT_SYSTEM_FOREGROUND, _EVENT_OBJECT_NAMECHANGE)]
        if not any(hooks):
            return   # 钩子没装上：轮询兜底仍在
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except Exception:
        pass   # 兜底轮询仍在


def _poll_thread():
    """L0 轮询兜底：钩子静默死亡（提权切换/系统卡顿）时 2s 内跟上。"""
    while True:
        time.sleep(2.0)
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if hwnd:
                _record_foreground(hwnd, "切到")
        except Exception:
            pass


def _ahash(img):
    small = img.convert("L").resize((8, 8))
    px = list(small.getdata())
    avg = sum(px) / 64.0
    return sum(1 << i for i, p in enumerate(px) if p > avg)


def _scene_thread():
    """L1 画面差分：1fps aHash，海明距 >12 判"大变化"（60s 滚动计数）。"""
    prev = None
    while True:
        time.sleep(1.0)
        try:
            import privacy
            if privacy.is_on():
                prev = None
                continue
            import host_input
            img = host_input.screenshot()
        except Exception:
            continue   # 隐私/截屏失败：跳过本拍，不出声
        try:
            h = _ahash(img)
        except Exception:
            continue
        if prev is not None and bin(h ^ prev).count("1") > 12:
            now = time.time()
            with _lock:
                _state["_scene_ts"].append(now)
                cutoff = now - 60
                _state["_scene_ts"] = [t for t in _state["_scene_ts"] if t > cutoff]
                _state["scene_changes"] = len(_state["_scene_ts"])
        prev = h


def start():
    """幂等启动（装配时调一次）。"""
    if not _started["hook"]:
        _started["hook"] = True
        threading.Thread(target=_hook_thread, daemon=True,
                         name="screen-aware-hook").start()
        threading.Thread(target=_poll_thread, daemon=True,
                         name="screen-aware-poll").start()
    if not _started["scene"]:
        _started["scene"] = True
        threading.Thread(target=_scene_thread, daemon=True,
                         name="screen-aware-scene").start()


def snapshot():
    with _lock:
        import copy
        return copy.deepcopy(_state)


def context_line():
    """给 L2 视觉问题拼的底账一句话；无信息时返回空串（零噪声）。"""
    with _lock:
        app, title, since = _state["app"], _state["title"], _state["since"]
        changes = _state["scene_changes"]
    if not app:
        return ""
    age = time.time() - since
    when = "现在" if age < 2 else f"{int(age)} 秒前"
    line = f"[屏幕感知] 用户{when}的前台是 {app}"
    if title:
        line += f"，窗口标题《{title[:40]}》"
    if changes >= 3:
        line += f"，近一分钟画面有 {changes} 次大变化"
    return line


def recent_line():
    with _lock:
        now = time.time()
        return "；".join(f"{now - t:.0f}s前 {d}" for t, d in _state["events"][-4:])


def register(ctx):
    """kernel 装配：注册服务 + 自动起线程（插件即插即用，无需装配层拉起）。"""
    ctx.provide("screen-aware", {
        "start": start, "snapshot": snapshot,
        "context_line": context_line, "recent_line": recent_line})
    start()
