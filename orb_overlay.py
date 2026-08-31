"""Floating orb overlay shown while dictating.

A borderless, click-through, always-on-top layered window rendered with
numpy/PIL frames (flowing gradient, breathing glow, orbiting sparkles).
All Win32 calls live on one dedicated thread; the rest of the app only
touches thread-safe state, so the mouse hook is never blocked.

Public API (all cheap and non-blocking):
    notify_press(delay)      - schedule the orb to appear after `delay` seconds
                               (a short click cancels before it ever shows)
    notify_release(processing) - short click: hide; dictation: switch to the
                               warm "processing" palette
    notify_done()            - hide
"""

import os
import threading
import time

import numpy as np
from PIL import Image, ImageDraw

import app_config

_SIZE = 150           # window is _SIZE x _SIZE physical pixels（基准值）
_K = 1.0              # 等比缩放因子（set_scale 调整；WhisperFlow 不动=共享零影响）
_BTN_H = 40           # 光球下方按钮带（照搬 Codex：左右两圆钮）
_STRIP_Y = _SIZE - 16   # 条带向上叠进球晕区（贴近光球留一道缝）
_FPS = 30

# 按钮排几何（全部 1x 逻辑坐标，随 _K 等比）：Codex 三钮去×保留位，左右对称于球心。
_BTN = {
    "names": ("mic", "speaker"),
    "r": 13,
    "cy": _STRIP_Y + 15,                    # 按钮带垂直中心
    "cx": {"mic": _SIZE // 2 - 33, "speaker": _SIZE // 2 + 33},
}


_scale_locked = False   # 尺寸钉死（2026-08-30 用户令）：只允许启动时定一次


def _apply_scale(k):
    """按 k 重建全部尺寸全局量（启动定初始值与运行时 DPI 跟随共用）。"""
    global _SIZE, _K, _BTN_H, _STRIP_Y, _BTN
    _SIZE = round(150 * k)
    _K = _SIZE / 150
    _BTN_H = round(40 * _K)
    _STRIP_Y = _SIZE - round(16 * _K)
    _BTN = {
        "names": ("mic", "speaker"),
        "r": round(13 * _K),
        "cy": _STRIP_Y + round(15 * _K),
        "cx": {"mic": _SIZE // 2 - round(33 * _K),
               "speaker": _SIZE // 2 + round(33 * _K)},
    }


def set_scale(k):
    """启动时定初始尺寸（一次性锁，防外部二次改）。运行时尺寸由窗口线程跟随
    当前显示器 DPI（2026-08-30 插拔外接屏实锤：启动时算死=换屏即错——固定的
    是逻辑 150px 视觉大小，物理像素=150×当前屏 DPI/96，逐屏换算）。"""
    global _scale_locked
    if _scale_locked:
        return
    _apply_scale(k)
    _scale_locked = True

_state = {"visible": False, "x": 0, "y": 0, "mode": "recording"}
_lock = threading.Lock()
_timer = None
_thread = None
_hwnd = None            # 当前光球窗口句柄（重启先销旧再建新，防叠球）
_click_cb = None   # when set, the orb window is clickable; click fires this
_button_cb = None      # 悬停按钮回调：cb("mic"|"text")
_button_state_cb = None  # 返回 {"muted": bool, "panel": bool} 供按钮着色
_hover = {"on": False, "tracked": False, "token": 0}   # 悬停状态；token=驻留过期令牌


def set_click_handler(cb):
    """Make the orb clickable (removes click-through) and call cb() on click.
    Must be called before the window thread is first started. cb runs on the
    window thread — keep it fast (e.g. just set an event)."""
    global _click_cb
    _click_cb = cb


def set_button_handler(cb, state_cb=None):
    """悬停按钮排（参照图 2026-08-26）：mic/close/speaker 密排 + 间隔 + text。
    cb(name) 在点击时触发；state_cb() 返回 {"muted","speaker","panel"} 供着色。"""
    global _button_cb, _button_state_cb
    _button_cb = cb
    _button_state_cb = state_cb


def pulse(seconds=0.7):
    """唤醒亮闪：短暂整体提亮，作为"我在"的光影应答（不用开口）。"""
    with _lock:
        _state["pulse_until"] = time.time() + seconds


# ---------------------------------------------------------------- rendering
# Restrained, Siri-style palettes: a deep base with three slowly drifting
# light blobs on top, a soft rim light, and gentle breathing. No sparkles,
# no rainbow cycling - slow and soft reads "premium".
_PALETTES = {
    # (base color, [blob colors]) in RGB
    "recording":  ((24, 32, 84),  [(110, 190, 255), (170, 130, 255), (255, 120, 175)]),
    "processing": ((78, 32, 20),  [(255, 176, 122), (255, 122, 140), (255, 214, 160)]),
}
# Per-blob orbit: (angular speed, orbit radius, phase, gaussian sigma).
# Small sigmas keep the blobs distinct so the gradient stays visible.
_BLOBS = [(0.50, 0.24, 0.0, 0.28), (-0.35, 0.30, 2.1, 0.32), (0.70, 0.20, 4.2, 0.25)]

# 每模式动效参数：(光斑速倍, 呼吸基值, 呼吸幅度, 呼吸频率, 扫光角速度)
# 扫光角速度 > 0 时，环上一道亮弧持续旋转 = "正在思考"，与聆听/播报一眼可辨。
_ANIM = {
    "recording":  (1.0, 0.93, 0.07, 1.6, 0.0),
    "processing": (1.0, 0.93, 0.07, 1.6, 0.0),
    "agent":      (1.0, 0.93, 0.07, 1.6, 0.0),
    "listening":  (1.0, 0.93, 0.07, 1.6, 0.0),
    "speaking":   (1.6, 0.88, 0.12, 3.4, 0.0),
    "thinking":   (2.8, 0.85, 0.15, 4.8, 3.2),
    "confirm":    (2.2, 0.85, 0.15, 4.2, 2.4),   # 琥珀扫光（配色由主程序给）
    "error":      (0.8, 0.80, 0.20, 1.2, 0.0),   # 暗红缓脉冲（故障态）
}
# 默认配色（agent_flow 会覆写为自己的一套）
for _m, _p in (("listening", ((10, 46, 52), [(64, 224, 208), (72, 168, 255), (150, 255, 230)])),
               ("speaking", ((10, 46, 52), [(64, 224, 208), (72, 168, 255), (150, 255, 230)])),
               ("thinking", ((26, 20, 58), [(150, 130, 255), (96, 190, 255), (220, 170, 255)]))):
    _PALETTES.setdefault(_m, _p)


_SS = 2  # supersample factor (render big, downsample with LANCZOS)
_STATIC = {}  # grids and time-invariant fields, computed once


def _static(size):
    if _STATIC.get("size") != size:
        c = (size - 1) / 2.0
        yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
        dx = (xx - c) / c
        dy = (yy - c) / c
        r = np.sqrt(dx * dx + dy * dy)
        r0 = 0.60
        # Silhouette: smoothstep core edge; the halo gaussian runs continuously
        # across the edge so there is no brightness/alpha jump anywhere.
        s = np.clip((r0 - r) / 0.05, 0, 1)
        core = s * s * (3 - 2 * s)
        halo = np.exp(-((r - r0) ** 2) / (2 * 0.16 ** 2)) * 0.45
        _STATIC.clear()
        _STATIC.update(
            size=size, dx=dx, dy=dy, r=r,
            rim=np.exp(-((r - r0) ** 2) / (2 * 0.045 ** 2)),
            ring=np.exp(-((r - 0.42) ** 2) / (2 * 0.22 ** 2)),
            alpha=np.clip(np.maximum(core, halo), 0, 1),
            toplight=(1.0 - 0.18 * np.clip(dy, 0, 1))[..., None],
        )
    return _STATIC


def render_frame_pil(t, mode):
    """One RGBA frame as a PIL Image (also used by the offline frame test)."""
    st = _static(_SIZE * _SS)
    dx, dy = st["dx"], st["dy"]
    speed_mult, b_base, b_amp, b_freq, sweep_w = _ANIM.get(mode, _ANIM["recording"])

    base_rgb, blob_colors = _PALETTES.get(mode, _PALETTES["recording"])
    base = np.asarray(base_rgb, dtype=np.float32) / 255.0

    weights = []
    for speed, orbit, phase, sigma in _BLOBS:
        a = t * speed * speed_mult + phase
        bx, by = orbit * np.cos(a), orbit * np.sin(a)
        d2 = (dx - bx) ** 2 + (dy - by) ** 2
        weights.append(np.exp(-d2 / (2.0 * sigma * sigma)))
    total = weights[0] + weights[1] + weights[2]
    coverage = np.clip(total, 0, 1)[..., None]

    blob_mix = np.zeros((*dx.shape, 3), dtype=np.float32)
    for w, col in zip(weights, blob_colors):
        blob_mix += w[..., None] * (np.asarray(col, dtype=np.float32) / 255.0)
    blob_mix /= np.maximum(total, 1e-5)[..., None]

    rgb = base * (1 - coverage) + blob_mix * coverage
    rgb = np.clip(rgb * st["toplight"], 0, 1)
    rgb = np.clip(rgb + st["rim"][..., None] * np.array([0.42, 0.47, 0.58]), 0, 1)
    rgb = np.clip(rgb * (b_base + b_amp * np.sin(t * b_freq)), 0, 1)

    # 思考扫光：环上一道亮弧匀速旋转（"在想，不是卡住"的确定性信号）
    if sweep_w:
        theta = np.arctan2(dy, dx)
        d = (theta - t * sweep_w + np.pi) % (2 * np.pi) - np.pi
        band = np.exp(-(d ** 2) / (2 * 0.55 ** 2)) * st["ring"]
        rgb = np.clip(rgb + band[..., None] * np.array([0.50, 0.58, 0.72],
                                                       dtype=np.float32), 0, 1)

    # 唤醒亮闪：pulse() 后的短暂时刻整体提亮（代替"在呢"这类语音应答）
    if time.time() < _state.get("pulse_until", 0):
        rgb = np.clip(rgb * 1.35, 0, 1)

    rgba = np.dstack([rgb, st["alpha"]])
    img = Image.fromarray((rgba * 255).astype(np.uint8), "RGBA")
    return img.resize((_SIZE, _SIZE), Image.LANCZOS)


# ------------------------------------------------------------- Win32 window
def _bgra_bytes(img):
    """Premultiplied BGRA bytes as UpdateLayeredWindow expects."""
    a = np.asarray(img, dtype=np.float32)
    alpha = a[..., 3:4] / 255.0
    a[..., :3] *= alpha                      # premultiply
    bgra = a[..., [2, 1, 0, 3]].astype(np.uint8)
    return bgra.tobytes()


def _draw_button_strip(states):
    """两圆钮一行（2026-08-29 照搬 Codex 参照图）：[mic][speaker] 居中对称。
    3× 超采样绘制再 LANCZOS 缩回——圆与斜线全部平滑。
    states: {"muted","speaker"}——关闭/静音态红缘+斜杠。"""
    S = 3   # 超采样倍数
    strip = Image.new("RGBA", (_SIZE * S, _BTN_H * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(strip)
    # 注意：条带局部坐标——按钮排在 40px 高的条带内（贴到 (0,_STRIP_Y) 前），
    # 不是整窗坐标。08-26 第一次实现画到了条带外=按钮全部消失，实锤教训。
    cy = (_BTN["cy"] - _STRIP_Y) * S
    r = _BTN["r"] * S
    u = r / 39.0          # 图标/线宽等比因子（基准 r=13,S=3 → u=1）
    lw = max(2, int(round(6 * u)))
    fg = (240, 246, 250, 255)
    muted_red = (255, 108, 108, 255)
    # 配色照 Codex 参照图像素实测：环=近白浅灰（实测 (244,248,249)），
    # 填充=近黑微透明；环宽 ~1.6px（1x 逻辑），比旧蓝灰细环更挺更干净。
    ring_w = max(2, int(round(1.6 * S * u)))
    for name in _BTN["names"]:
        cx = _BTN["cx"][name] * S
        active = {"mic": states.get("muted"), "speaker": states.get("speaker"),
                  "text": states.get("panel"), "close": False}[name]
        if name in ("mic", "speaker"):   # 停/静音=红缘警示
            edge = (255, 108, 108, 225) if active else (225, 232, 238, 215)
            fill = (52, 24, 26, 242) if active else (14, 17, 20, 235)
        else:
            edge = (64, 224, 208, 230) if active else (225, 232, 238, 215)
            fill = (16, 52, 58, 244) if active else (14, 17, 20, 235)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill,
                  outline=edge, width=ring_w)

        if name == "mic":
            # 图标全部收在圆内（随 u 等比）
            d.rounded_rectangle([cx - 9 * u, cy - 21 * u, cx + 9 * u, cy + 5 * u],
                                radius=9 * u, outline=fg, width=lw)
            d.arc([cx - 15 * u, cy - 5 * u, cx + 15 * u, cy + 23 * u],
                  10, 170, fill=fg, width=lw)
            d.line([cx, cy + 23 * u, cx, cy + 29 * u], fill=fg, width=lw)
            d.line([cx - 10 * u, cy + 29 * u, cx + 10 * u, cy + 29 * u],
                   fill=fg, width=lw)
        elif name == "close":
            d.line([cx - 12, cy - 12, cx + 12, cy + 12], fill=fg, width=6)
            d.line([cx - 12, cy + 12, cx + 12, cy - 12], fill=fg, width=6)
        elif name == "speaker":
            d.polygon([(cx - 16 * u, cy - 6 * u), (cx - 6 * u, cy - 6 * u),
                       (cx + 4 * u, cy - 16 * u), (cx + 4 * u, cy + 16 * u),
                       (cx - 6 * u, cy + 6 * u), (cx - 16 * u, cy + 6 * u)],
                      fill=fg)
            d.arc([cx + 2 * u, cy - 9 * u, cx + 16 * u, cy + 9 * u],
                  -60, 60, fill=fg, width=lw)
            d.arc([cx + 0 * u, cy - 15 * u, cx + 24 * u, cy + 15 * u],
                  -55, 55, fill=fg, width=lw)
        elif name == "text":
            d.line([cx - 12, cy - 12, cx + 12, cy - 12], fill=fg, width=6)
            d.line([cx - 12, cy, cx + 12, cy], fill=fg, width=6)
            d.line([cx - 12, cy + 12, cx + 2, cy + 12], fill=fg, width=6)
        if name in ("mic", "speaker") and active:
            d.line([cx + 17, cy - 21, cx - 17, cy + 21],
                   fill=muted_red, width=7)
    return strip.resize((_SIZE, _BTN_H), Image.LANCZOS)


def _compose_frame(t, mode):
    """光球帧 + 下方按钮排（注册处理器+悬停才绘制，平时隐藏）。
    未注册（如语音输入产品）画布与历史完全一致——共享文件零影响。"""
    frame = render_frame_pil(t, mode)
    canvas = Image.new("RGBA", (_SIZE, _SIZE + _BTN_H), (0, 0, 0, 0))
    canvas.paste(frame, (0, 0))
    if _button_cb is not None and _hover["on"]:
        states = _button_state_cb() if _button_state_cb else {}
        canvas.alpha_composite(_draw_button_strip(states), (0, _STRIP_Y))
    return canvas


def _window_thread():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    WS_POPUP = 0x80000000
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOPMOST = 0x00000008
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    SW_HIDE, SW_SHOWNA = 0, 8
    ULW_ALPHA = 2
    AC_SRC_ALPHA = 1

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

    # 64 位签名：不设的话 lParam 被按 c_int 截断，回调里 OverflowError 刷屏
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      ctypes.c_size_t, ctypes.c_ssize_t]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t

    # Custom WndProc: forward everything to DefWindowProc, but report clicks
    # when a click handler is registered (agent mode: click the orb to close).
    WNDPROC_T = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
        ctypes.c_size_t, ctypes.c_ssize_t,
    )
    WM_LBUTTONDOWN = 0x0201
    WM_MOUSEMOVE = 0x0200
    WM_LBUTTONUP = 0x0202
    WM_MOUSELEAVE = 0x02A3
    TME_LEAVE = 0x00000002

    class TRACKMOUSEEVENT(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("hwndTrack", wintypes.HWND), ("dwHoverTime", wintypes.DWORD)]

    def _btn_at(cx, cy):
        """按钮排命中（悬停可见时才可点）：四圆钮圆形命中域。"""
        if _button_cb is None or not _hover["on"]:
            return None
        by = _BTN["cy"]
        rr = (_BTN["r"] + 1) ** 2
        for name in _BTN["names"]:
            bx = _BTN["cx"][name]
            if (cx - bx) ** 2 + (cy - by) ** 2 <= rr:
                return name
        return None

    # 拖动支持（agent 模式）：按住移动超 6px = 拖动搬位置（记住），
    # 否则 = 点击（关会话）。拖动过的位置存 user_pos，下次唤醒不跳回光标旁。
    _drag = {"sx": 0, "sy": 0, "ox": 0, "oy": 0, "down": False, "moved": False,
             "t0": 0.0}
    _btn_press = {"name": None, "t0": 0.0}
    # 短点=点击，按住 ≥0.35s=语音长按（sense_io HOLD_THRESHOLD_SECONDS 同值）——
    # 2026-08-30 实锤"语音不回了"：在球上长按说话，松开瞬间 click 触发全停，
    # 会话即开即杀。点击必须给长按链路让路。
    _CLICK_HOLD_MAX = 0.35

    def _wndproc(hwnd, msg, wparam, lparam):
        if _click_cb is not None:
            if msg == WM_MOUSEMOVE:
                _hover["token"] += 1   # 任何移动=作废未决驻留过期
                if not _hover["tracked"]:
                    _hover["tracked"] = True
                    tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT),
                                          TME_LEAVE, hwnd, 0)
                    user32.TrackMouseEvent(ctypes.byref(tme))
                _hover["on"] = True
                # fall through：拖动追踪仍要用 MOUSEMOVE
            elif msg == WM_MOUSELEAVE:
                _hover["tracked"] = False
                # 驻留 1.5s 再隐（2026-08-30 实锤"点不到"：按钮与球体之间的
                # 缝隙是全透明像素——不接收鼠标，穿越瞬间触发 LEAVE 按钮先没
                # 了；1.5s 够从球体挪到钮上，离开又确实会消失）
                _hover["token"] += 1
                tok = _hover["token"]

                def _expire(t=tok):
                    time.sleep(1.5)
                    if _hover["token"] == t:
                        _hover["on"] = False
                threading.Thread(target=_expire, daemon=True).start()
                return 0
            if msg == WM_MOUSEMOVE and _drag["down"]:
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                dx, dy = pt.x - _drag["sx"], pt.y - _drag["sy"]
                if abs(dx) > 6 or abs(dy) > 6:
                    _drag["moved"] = True
                if _drag["moved"]:
                    with _lock:
                        _state["x"] = _drag["ox"] + dx
                        _state["y"] = _drag["oy"] + dy
                        _state["user_pos"] = True
                return 0
            if msg == WM_LBUTTONDOWN:
                cx = ctypes.c_short(lparam & 0xFFFF).value
                cy = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                btn = _btn_at(cx, cy)   # 悬停可见才命中
                if btn:
                    _btn_press["name"] = btn
                    _btn_press["t0"] = time.perf_counter()
                    user32.SetCapture(hwnd)
                    return 0
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                with _lock:
                    _drag.update(sx=pt.x, sy=pt.y, ox=_state["x"], oy=_state["y"],
                                 down=True, moved=False,
                                 t0=time.perf_counter())
                user32.SetCapture(hwnd)
                return 0
            if msg == WM_LBUTTONUP:
                if _btn_press["name"]:
                    cx = ctypes.c_short(lparam & 0xFFFF).value
                    cy = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                    name = _btn_press["name"]
                    held = time.perf_counter() - _btn_press["t0"]
                    _btn_press["name"] = None
                    user32.ReleaseCapture()
                    if (_btn_at(cx, cy) == name and _button_cb is not None
                            and held < _CLICK_HOLD_MAX):
                        try:
                            _button_cb(name)
                        except Exception:
                            pass
                    return 0
                was_drag = _drag["moved"]
                held = time.perf_counter() - _drag["t0"]
                _drag["down"] = False
                user32.ReleaseCapture()
                if not was_drag and held < _CLICK_HOLD_MAX:
                    try:
                        _click_cb()
                    except Exception:
                        pass
                return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    _wndproc_ref = WNDPROC_T(_wndproc)  # keep a reference alive

    cls = WNDCLASSEXW()
    cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
    cls.lpfnWndProc = ctypes.cast(_wndproc_ref, ctypes.c_void_p)
    cls.hInstance = hinst
    cls.lpszClassName = "WhisperFlowOrb"
    if not user32.RegisterClassExW(ctypes.byref(cls)):
        # 1410 = 类已注册（上次线程崩溃残留），直接复用；其余错误才抛
        if kernel32.GetLastError() != 1410:
            raise OSError("RegisterClassExW failed")

    ex_style = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    if _click_cb is None:
        ex_style |= WS_EX_TRANSPARENT   # 默认保持点击穿透（听写模式）

    hdc_screen = user32.GetDC(None)

    def _create():
        """（重）建窗口+DIB——初次创建与 DPI 跟随重建共用。"""
        h = user32.CreateWindowExW(
            ex_style, "WhisperFlowOrb", "", WS_POPUP,
            0, 0, _SIZE, _SIZE + _BTN_H, None, None, hinst, None)
        if not h:
            raise OSError("CreateWindowExW failed")
        dc = gdi32.CreateCompatibleDC(hdc_screen)
        b = BITMAPINFO()
        b.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        b.bmiHeader.biWidth = _SIZE
        b.bmiHeader.biHeight = -(_SIZE + _BTN_H)  # top-down
        b.bmiHeader.biPlanes = 1
        b.bmiHeader.biBitCount = 32
        bt = ctypes.c_void_p()
        bm = gdi32.CreateDIBSection(dc, ctypes.byref(b), 0, ctypes.byref(bt),
                                    None, 0)
        gdi32.SelectObject(dc, bm)
        return h, dc, bt

    hwnd, hdc, bits = _create()
    global _hwnd
    if _hwnd:
        try:   # 旧窗不销毁就建新窗=两球叠显（线程异常重启场景）
            user32.DestroyWindow(_hwnd)
        except Exception:
            pass
    _hwnd = hwnd

    blend = BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)
    pt_src = wintypes.POINT(0, 0)
    msg = wintypes.MSG()
    shown = False
    t0 = time.perf_counter()
    frame_dump_dir = os.environ.get("ORB_DEBUG_DIR")
    dumped = 0

    PM_REMOVE = 1
    last_dpi_check = 0.0
    while True:
        # Keep the queue pumped so Windows never ghosts the window.
        while user32.PeekMessageW(ctypes.byref(msg), hwnd, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # 跟随当前显示器 DPI（2026-08-30 插拔外接屏实锤）：1s 轮询，
        # 变了就地销毁重建——视觉大小从此与所在屏无关。
        now_m = time.perf_counter()
        if now_m - last_dpi_check > 1.0:
            last_dpi_check = now_m
            try:
                dpi = user32.GetDpiForWindow(hwnd)
                if dpi and round(150 * dpi / 96) != _SIZE:
                    _apply_scale(dpi / 96)
                    user32.DestroyWindow(hwnd)
                    hwnd, hdc, bits = _create()
                    _hwnd = hwnd
                    shown = False
            except Exception:
                pass

        with _lock:
            visible = _state["visible"]
            x, y, mode = _state["x"], _state["y"], _state["mode"]

        if not visible:
            if shown:
                user32.ShowWindow(hwnd, SW_HIDE)
                shown = False
                with _lock:
                    # 隐藏即忘拖动位（2026-08-30 用户令"保持老样子"：每次召唤
                    # 都在按的地方出现；同一次出现期间拖动的位置仍记住）
                    _state["user_pos"] = None
            time.sleep(0.05)
            continue

        t = time.perf_counter() - t0
        # 渲染异常不炸线程（2026-08-30 用户令钉尺寸：线程死→_ensure_thread
        # 重建→同进程第二个光球窗口叠着旧窗="大小/样子莫名其妙变"的代码根因）
        try:
            img = _compose_frame(t, mode)
            ctypes.memmove(bits, _bgra_bytes(img), _SIZE * (_SIZE + _BTN_H) * 4)
            pt_dst = wintypes.POINT(x, y)
            sz = wintypes.POINT(_SIZE, _SIZE + _BTN_H)
            user32.UpdateLayeredWindow(hwnd, hdc_screen, ctypes.byref(pt_dst),
                                       ctypes.byref(sz), hdc, ctypes.byref(pt_src),
                                       0, ctypes.byref(blend), ULW_ALPHA)
            if not shown:
                user32.ShowWindow(hwnd, SW_SHOWNA)
                shown = True
        except Exception:
            time.sleep(0.2)

        if frame_dump_dir and dumped < 12:
            os.makedirs(frame_dump_dir, exist_ok=True)
            img.save(os.path.join(frame_dump_dir, f"frame_{dumped:02d}_{mode}.png"))
            dumped += 1

        time.sleep(1.0 / _FPS)


def _ensure_thread():
    global _thread
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_window_thread, name="orb-overlay", daemon=True)
        _thread.start()


def _enabled():
    return bool(app_config.load().get("orb", True))


# ---------------------------------------------------------------- public API
def notify_press(delay):
    """The button went down: show the orb at the cursor after `delay` seconds
    (a short click is over before that and never shows anything)."""
    global _timer
    if not _enabled():
        return
    if _timer is not None:
        _timer.cancel()

    def _show():
        try:
            import ctypes
            pt = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            # 完整上屏钳制（2026-08-28 用户实锤"光球时大时小"）：光球在屏幕
            # 边缘出生时被切掉一截=看着变小。x/y 都夹进可视区，任何位置
            # 都是完整一颗。
            u = ctypes.windll.user32
            sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
            x = min(max(pt.x - _SIZE // 2, 0), max(0, sw - _SIZE))
            y = pt.y - _SIZE - 12
            if y < 0:
                y = pt.y + 24
            y = min(max(y, 0), max(0, sh - _SIZE - _BTN_H))
            with _lock:
                _state.update(visible=True, mode="recording", x=x, y=y)
            _ensure_thread()
        except Exception:
            pass

    _timer = threading.Timer(delay, _show)
    _timer.daemon = True
    _timer.start()


def notify_release(processing):
    """Button released. Short click (orb never showed): cancel. Dictation:
    switch to the processing palette."""
    global _timer
    if _timer is not None:
        _timer.cancel()
        _timer = None
    with _lock:
        if not _state["visible"]:
            return
        if processing:
            _state["mode"] = "processing"
        else:
            _state["visible"] = False


def notify_done():
    """Text has been injected (or failed): hide the orb."""
    with _lock:
        _state["visible"] = False
