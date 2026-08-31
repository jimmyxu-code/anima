# -*- coding: utf-8 -*-
"""任务浮层（P2-3 → 测试期常驻日志面板版）。

模式：
- 常驻（pinned，测试期默认开）：随光球会话同步唤醒/消失，实时滚动日志，
  可按住拖动，不点穿（方便观察）。
-  transient（任务进度条）：show_task/add_step/finish 那套，点击穿透。

API（线程安全、非阻塞）：
    set_pinned(bool) / session_open(bool) / log_line(text)
    show_task(title) / add_step(text) / complete_step() / finish(ok, note) / hide()
    work_begin(key, desc) / work_end(key)   # 沉默可见化：不可见工作登记

铁律演进（用户 2026-08-26 裁决，修订 08-22 版）：沉默时的"在干什么"
改由语音层自然说出（卡住必说话）；文字框平时隐藏、只有用户按文字按钮
才展开，但一切内容持续记录（要看得随时有）。work_begin/work_end 照常
登记——它们同时喂语音层的沉默解说与面板内容。
"""

import ctypes
import threading
import time
from ctypes import wintypes

import numpy as np
from PIL import Image, ImageDraw, ImageFont

_W, _H = 660, 320
_FPS = 20   # 2026-08-27 用户令：文字框更新做快一点（渲染 ~2ms/帧，无压力）

_state = {"visible": False, "title": "", "steps": [], "until": 0.0,
          "pinned": False, "session": False, "x": None, "y": None,
          "bind_offset": None,   # 面板相对光球的偏移（绑定模式）
          "expanded": False,     # 文字框展开态（任务自动展开，2026-08-29 用户令）
          "activities": {}}      # 在途不可见工作 {key: (desc, since, grace)}
_auto = {"suppress": False}   # 显式收起（藏起来/退下）→ 抑制任务自动展开
_lock = threading.Lock()
_wake = threading.Event()   # 零延迟渲染（2026-08-28 用户令：文字框主观
_thread = None              # 无延迟）——内容一变立即唤醒渲染线程出帧


def _touch():
    _wake.set()

_FONT_PATH = r"C:\Windows\Fonts\msyh.ttc"
_ACT_GRACE = 1.0   # 工作登记宽限期：短于 1s 的瞬态不上屏（防纯闲聊闪烁）


def _font(size):
    try:
        return ImageFont.truetype(_FONT_PATH, size)
    except Exception:
        return ImageFont.load_default()


def _render(title, steps, pinned, activities=()):
    """任务面板（2026-08-30 用户令：内容一列列清晰，不许粘在一起）。
    标题居中、内容行统一左对齐同一边距——每行一条，标记/颜色分工；
    行数按需取，高度随内容伸缩，永不画出圆角框外。"""
    img = Image.new("RGBA", (_W, _H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    lines = []
    if title:
        lines.append((f"● {title}", _font(15), (126, 232, 216, 255), "title"))
    f_step = _font(13)
    for s in steps[-8:]:
        mark = "√ " if s["done"] else "· "
        color = (150, 160, 170, 255) if s["done"] else (235, 240, 245, 255)
        lines.append((mark + s["text"], f_step, color, "row"))
    for desc in list(activities)[-2:]:
        lines.append(("→ " + desc, f_step, (232, 190, 90, 255), "row"))
    # 行高 24、上下留白 14/12；超出画布就截尾（内容永不画出圆角框外）
    max_lines = (_H - 26) // 24
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    box_h = 14 + len(lines) * 24 + 12
    d.rounded_rectangle([0, 0, _W - 1, box_h - 1], radius=14,
                        fill=(14, 18, 24, 215), outline=(64, 224, 208, 90),
                        width=1)
    cx = _W // 2
    y = 14
    for text, f, color, kind in lines:
        if kind == "title":
            d.text((cx, y), text, font=f, fill=color, anchor="ma")
        else:
            d.text((18, y), text, font=f, fill=color, anchor="la")   # 左对齐成列
        y += 24
    return img


def _bgra_bytes(img):
    a = np.asarray(img, dtype=np.float32)
    alpha = a[..., 3:4] / 255.0
    a[..., :3] *= alpha
    bgra = a[..., [2, 1, 0, 3]].astype(np.uint8)
    return bgra.tobytes()


def _window_thread():
    # 私有 DLL 实例（不用 ctypes.windll 共享对象）：windll 的函数对象全进程
    # 共享，在共享对象上设 argtypes 会泄漏污染其他模块——orb_overlay 的
    # CreateDIBSection 就是被本模块的类型化签名搞崩的（BITMAPINFO 类不匹配）。
    user32 = ctypes.WinDLL("user32")
    gdi32 = ctypes.WinDLL("gdi32")

    WS_POPUP = 0x80000000
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOPMOST = 0x00000008
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    SW_HIDE, SW_SHOWNA = 0, 8
    ULW_ALPHA = 2
    AC_SRC_ALPHA = 1
    GWL_EXSTYLE = -20
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    WM_MOUSEMOVE = 0x0200

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT), ("style", wintypes.UINT),
            ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HANDLE),
            ("hIcon", wintypes.HANDLE), ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HANDLE), ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HANDLE),
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

    kernel32 = ctypes.windll.kernel32
    hinst = kernel32.GetModuleHandleW(None)

    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      ctypes.c_size_t, ctypes.c_ssize_t]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    WNDPROC_T = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND,
                                   wintypes.UINT, ctypes.c_size_t,
                                   ctypes.c_ssize_t)

    drag = {"on": False, "dx": 0, "dy": 0}

    def _wndproc(hwnd, msg, wparam, lparam):
        try:
            if msg == WM_LBUTTONDOWN:
                drag["on"] = True
                drag["dx"] = lparam & 0xFFFF
                drag["dy"] = (lparam >> 16) & 0xFFFF
                user32.SetCapture(hwnd)
                return 0
            if msg == WM_MOUSEMOVE and drag["on"]:
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                with _lock:
                    nx = pt.x - drag["dx"]
                    ny = pt.y - drag["dy"]
                    orb = _get_orb_pos()
                    if orb:
                        # 绑定模式：拖面板=调相对光球的偏移，光球动面板跟
                        _state["bind_offset"] = (nx - orb[0], ny - orb[1])
                    else:
                        _state["x"] = nx
                        _state["y"] = ny
                return 0
            if msg == WM_LBUTTONUP:
                drag["on"] = False
                user32.ReleaseCapture()
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        except Exception:
            return 0

    cls = WNDCLASSEXW()
    cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
    cls.lpfnWndProc = ctypes.cast(WNDPROC_T(_wndproc), ctypes.c_void_p)
    cls.hInstance = hinst
    cls.lpszClassName = "TaskOverlayBar"
    if not user32.RegisterClassExW(ctypes.byref(cls)):
        # 1410 = 类已注册（同进程重复 ensure），直接复用；其余错误才抛
        if kernel32.GetLastError() != 1410:
            raise OSError("RegisterClassExW failed")

    hwnd = user32.CreateWindowExW(
        WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
        | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT,
        "TaskOverlayBar", "", WS_POPUP, 0, 0, _W, _H, None, None, hinst, None)
    if not hwnd:
        raise OSError("CreateWindowExW failed")

    # 64 位签名必须显式声明——默认 c_int 截断句柄导致 DIB 静默失败（实测：
    # GetDC 默认 restype 返回垃圾负数，HDC restype 返回真句柄）。
    # 且结构体/byref 参数必须用类型化 POINTER——c_void_p 会静默传错地址。
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO),
                                       wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p),
                                       wintypes.HANDLE, wintypes.DWORD]
    gdi32.CreateDIBSection.restype = wintypes.HANDLE
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
    gdi32.SelectObject.restype = wintypes.HANDLE
    user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.HDC, ctypes.c_void_p,
        wintypes.DWORD, ctypes.c_void_p, wintypes.UINT]
    user32.UpdateLayeredWindow.restype = wintypes.BOOL
    hdc_screen = user32.GetDC(None) & 0xFFFFFFFF
    # 句柄必须 & 0xFFFFFFFF：64 位 RAX 返回的高 32 位是未定义垃圾，
    # c_void_p/HDC 会原样带进下一个调用——CreateDIBSection 校验全 64 位直接拒
    # （这就是同一段代码有时成功有时 NULL 的原因；orb 一直正常恰恰因为
    # 它全程默认 c_int 截断，反而天然干净）
    hdc = gdi32.CreateCompatibleDC(hdc_screen) & 0xFFFFFFFF
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = _W
    bmi.bmiHeader.biHeight = -_H
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bits = ctypes.c_void_p()
    hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    hbm = (hbm or 0) & 0xFFFFFFFF
    if not hbm or not bits.value:
        raise OSError(f"CreateDIBSection failed hbm={hbm} bits={bits.value}")
    gdi32.SelectObject(hdc, hbm)

    blend = BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)
    pt_src = wintypes.POINT(0, 0)
    size = wintypes.POINT(_W, _H)
    msg = wintypes.MSG()
    shown = False
    was_interactive = None
    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)

    PM_REMOVE = 1
    while True:
        while user32.PeekMessageW(ctypes.byref(msg), hwnd, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        with _lock:
            pinned = _state["pinned"]
            session = _state["session"]
            now_ts = time.time()
            # 各工作按自己的宽限期过滤（瞬态闲聊/秒答不闪面板）
            acts = [desc for desc, since, grace in _state["activities"].values()
                    if now_ts - since >= grace]
            if pinned:
                # 2026-08-26 用户裁决：只有按文字按钮（expanded）才显示；
                # 内容照常记录（steps/activities 都留着），要看随时点开全在。
                visible = session and _state["expanded"]
            else:
                visible = _state["visible"] and now_ts < _state["until"]
            title = _state["title"]
            steps = list(_state["steps"])
            bind_off = _state["bind_offset"]
        # 绑定光球：光球可见时面板锚定相对位置（拖动光球面板跟随）
        orb = _get_orb_pos() if pinned else None
        if orb:
            ox, oy, osize = orb
            if bind_off is None:
                bind_off = (osize + 12, (osize - _H) // 2)   # 默认：光球右侧
            x = ox + bind_off[0]
            y = oy + bind_off[1]
            x = min(max(x, 0), sw - _W)
            y = min(max(y, 0), sh - _H)
            with _lock:
                _state["x"], _state["y"] = x, y
        else:
            with _lock:
                x = _state["x"] if _state["x"] is not None else (sw - _W) // 2
                y = _state["y"] if _state["y"] is not None else sh - _H - 48

        # 常驻=可拖动可点；transient=点击穿透
        interactive = pinned
        if interactive != was_interactive:
            style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            if interactive:
                style &= ~WS_EX_TRANSPARENT
            else:
                style |= WS_EX_TRANSPARENT
            user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
            was_interactive = interactive

        if not visible:
            if shown:
                user32.ShowWindow(hwnd, SW_HIDE)
                shown = False
            _wake.wait(0.1)
            _wake.clear()
            continue

        try:
            img = _render(title, steps, pinned, activities=acts)
            ctypes.memmove(bits, _bgra_bytes(img), _W * _H * 4)
            pt_dst = wintypes.POINT(x, y)
            user32.UpdateLayeredWindow(hwnd, hdc_screen, ctypes.byref(pt_dst),
                                       ctypes.byref(size), hdc, ctypes.byref(pt_src),
                                       0, ctypes.byref(blend), ULW_ALPHA)
            if not shown:
                user32.ShowWindow(hwnd, SW_SHOWNA)
                shown = True
        except Exception:
            pass   # 渲染出错绝不许线程死（面板消失=用户失明，这是教训）
        # 零延迟：等"内容变化"唤醒（有变化立即出下一帧），30fps 封顶防忙转
        _wake.wait(1.0 / _FPS)
        _wake.clear()


def _get_orb_pos():
    """读光球位置（orb_overlay 可见时）。基准面板用它锚定，失败返回 None。"""
    try:
        import orb_overlay
        with orb_overlay._lock:
            if orb_overlay._state["visible"]:
                return (orb_overlay._state["x"], orb_overlay._state["y"],
                        orb_overlay._SIZE)
    except Exception:
        pass
    return None


def _ensure_thread():
    global _thread
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_window_thread, name="task-overlay",
                                   daemon=True)
        _thread.start()


def set_pinned(v):
    """常驻开关（测试期开）：随会话同步显隐、可拖动、收日志。"""
    _ensure_thread()
    with _lock:
        _state["pinned"] = bool(v)
    _touch()


def session_open(v):
    """光球会话同步：开=面板醒，关=面板收。
    新逻辑（用户 2026-08-21 明令）：文字框的职责=显示屏幕上看不到的一切——
    会话开时先清空待命，有不可见内容（任务受理/步骤/结果/等待确认）才自动出现；
    屏幕上看得到的（前台 GUI 操作画面）不重复占屏。"""
    _ensure_thread()
    with _lock:
        _state["session"] = bool(v)
        if not v:
            _state["expanded"] = False   # 会话结束收起，下次默认不展开
            _state["activities"] = {}    # 会话外的登记全部作废
        if v:
            _auto["suppress"] = False    # 会话开=用户在场，解除自动展开抑制
            _state["steps"] = []         # 新会话清空：无内容时不出现
            _state["title"] = "小凯在干活（屏幕上看不到的都在这里）"
    _touch()


def work_begin(key, desc, grace=None):
    """登记一件"屏幕上看不到且正在发生"的工作（沉默可见化铁律）。
    同 key 重复登记=更新描述；超过宽限期自动上屏，work_end 销记。
    grace：秒，默认 _ACT_GRACE。聊天层快工具（语音 FC/路由）用大宽限——
    秒答场景不弹面板（用户 2026-08-22 明令：聊天层不用文字框展示）。"""
    _ensure_thread()
    with _lock:
        if not _state["session"]:
            return
        _state["activities"][key] = (str(desc)[:44], time.time(),
                                     grace if grace is not None else _ACT_GRACE)
    _touch()


def work_end(key):
    with _lock:
        _state["activities"].pop(key, None)
    _touch()


def log_line(text):
    """实时日志行（常驻模式下滚到面板上）。"""
    with _lock:
        _state["steps"].append({"text": text[:44], "done": False})
        if len(_state["steps"]) > 40:
            _state["steps"] = _state["steps"][-20:]
    _touch()


def show_task(title):
    """记录任务标题。新任务=面板翻新页（2026-08-27 用户令：到了下一个任务
    还显示上一个——steps/activities 全清，从零开始记）。
    任务即自动展开（2026-08-29 用户令：做任务时文字框自动展示，文字按钮退役；
    "藏起来/退下"显式收起后抑制，开会话或显式展开解除）。"""
    _ensure_thread()
    with _lock:
        _state.update(visible=True, title=title[:40],
                      until=time.time() + 3600,
                      steps=[], activities={})
        if not _auto["suppress"]:
            _state["expanded"] = True
    _touch()


def toggle_panel():
    """文字按钮：展开/收起日志面板。"""
    _ensure_thread()
    with _lock:
        _state["expanded"] = not _state["expanded"]
    _touch()


def set_panel(v):
    """显式设定展开态（语音命令"打开工作台/藏起来"用，幂等）。
    显式收起=抑制任务自动展开（退下同理）；显式展开=解除。"""
    _ensure_thread()
    with _lock:
        _state["expanded"] = bool(v)
        _auto["suppress"] = not bool(v)
    _touch()


def panel_expanded():
    with _lock:
        return bool(_state["expanded"])


def add_step(text):
    with _lock:
        _state["steps"].append({"text": text[:44], "done": False})
    _touch()


def complete_step():
    with _lock:
        for s in reversed(_state["steps"]):
            if not s["done"]:
                s["done"] = True
                break
    _touch()


def finish(ok=True, note=""):
    with _lock:
        if note:
            _state["steps"].append({"text": ("√ " if ok else "× ") + note[:44],
                                    "done": True})
        else:
            for s in _state["steps"]:
                s["done"] = True
        if not _state["pinned"]:
            _state["until"] = time.time() + 2.5
    _touch()


def hide():
    with _lock:
        _state["visible"] = False
    _touch()
