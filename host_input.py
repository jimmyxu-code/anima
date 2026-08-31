"""host_input：与人类同层级的输入自动化（截图 / 鼠标 / 键盘 / 窗口激活）。

设计原则（用户定的）：操作层级和真人一模一样——看的是真实屏幕，
动的是真实鼠标键盘。不通任何特权通道，所以对一切窗口有效
（包括 VirtualBox 这类 guestcontrol 半残的场景）。
"""

import ctypes
import time
from ctypes import wintypes

from PIL import ImageGrab

user32 = ctypes.windll.user32


def screenshot(path=None, region=None, all_screens=True):
    """真实屏幕截图 -> PIL Image。region=(left,top,right,bottom)。
    默认 all_screens=True 抓整个虚拟屏（多显示器），坐标系与 SetCursorPos 一致。
    P-1-2：隐私模式开启时禁止截屏（含休眠件复活后的按需截屏）。"""
    import privacy
    if privacy.is_on():
        raise privacy.PrivacyBlocked("隐私模式已开启，截屏被禁止")
    try:
        img = ImageGrab.grab(bbox=region, all_screens=all_screens)
    except TypeError:  # 老版本 PIL 没有 all_screens
        img = ImageGrab.grab(bbox=region)
    if path:
        img.save(path)
    return img


def move_to(x, y):
    user32.SetCursorPos(int(x), int(y))


def cursor_pos():
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def move_smooth(x, y, duration=None, fps=60, cancel_check=None):
    """贝塞尔平滑移动（P3b 拟人鼠标）。所有距离都走可见连续轨迹，
    不再对近距离调用一次性 SetCursorPos；duration 随距离自适应。"""
    import math
    import random
    x0, y0 = cursor_pos()
    dx, dy = x - x0, y - y0
    dist = math.hypot(dx, dy)
    if cancel_check and cancel_check():
        return False
    if dist < 0.5:
        return True
    if duration is None:
        duration = min(max(0.08 + dist / 2500.0, 0.10), 0.7)
    # 控制点：路径中点垂直方向随机偏移，形成自然弧线
    mx, my = (x0 + x) / 2, (y0 + y) / 2
    spread = dist * 0.15
    c1 = (x0 + dx * 0.25 + random.uniform(-spread, spread),
          y0 + dy * 0.25 + random.uniform(-spread, spread))
    c2 = (x0 + dx * 0.75 + random.uniform(-spread, spread),
          y0 + dy * 0.75 + random.uniform(-spread, spread))
    steps = max(int(duration * fps), 4)
    for i in range(1, steps + 1):
        if cancel_check and cancel_check():
            return False
        t = i / steps
        # 缓入缓出
        te = t * t * (3 - 2 * t)
        u = 1 - te
        px = u**3 * x0 + 3 * u * u * te * c1[0] + 3 * u * te * te * c2[0] + te**3 * x
        py = u**3 * y0 + 3 * u * u * te * c1[1] + 3 * u * te * te * c2[1] + te**3 * y
        move_to(px, py)
        time.sleep(1.0 / fps)
    return True


def _mouse_event(flags):
    user32.mouse_event(flags, 0, 0, 0, 0)


def click(x=None, y=None, button="left", double=False, cancel_check=None):
    if x is not None:
        if not move_smooth(x, y, cancel_check=cancel_check):
            return False
        time.sleep(0.05)
    if cancel_check and cancel_check():
        return False
    flags = {
        "left": (0x0002, 0x0004),
        "right": (0x0008, 0x0010),
        "middle": (0x0020, 0x0040),
    }[button]
    times = 2 if double else 1
    for _ in range(times):
        if cancel_check and cancel_check():
            return False
        _mouse_event(flags[0])
        _mouse_event(flags[1])
        time.sleep(0.06)
    return True


def double_click(x=None, y=None, cancel_check=None):
    return click(x, y, double=True, cancel_check=cancel_check)


def scroll(x, y, delta, cancel_check=None):
    """在 (x,y) 处滚轮。delta>0 向上，<0 向下（单位：格，1 格=120）。"""
    if not move_smooth(x, y, cancel_check=cancel_check):
        return False
    time.sleep(0.05)
    if cancel_check and cancel_check():
        return False
    user32.mouse_event(0x0800, 0, 0, int(delta * 120), 0)  # MOUSEEVENTF_WHEEL
    return True


def drag(x1, y1, x2, y2, duration=0.4, steps=20, cancel_check=None):
    """左键按住从 (x1,y1) 拖到 (x2,y2)。"""
    if not move_smooth(x1, y1, cancel_check=cancel_check):
        return False
    time.sleep(0.05)
    if cancel_check and cancel_check():
        return False
    _mouse_event(0x0002)  # LEFTDOWN
    try:
        for i in range(1, steps + 1):
            if cancel_check and cancel_check():
                return False
            t = i / steps
            move_to(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
            time.sleep(duration / steps)
    finally:
        _mouse_event(0x0004)  # LEFTUP，即使取消也不让按键卡住
    return True


def type_text(text, interval=0.01, cancel_check=None):
    """逐键输入（英文/数字/符号）。键盘布局级，所见即所得。"""
    vk_map = {}
    for ch in text:
        o = ord(ch)
        if o in vk_map:
            continue
        r = user32.VkKeyScanW(o)
        vk_map[o] = r
        if r == -1:
            raise ValueError(f"char not typeable: {ch!r}")
    for ch in text:
        if cancel_check and cancel_check():
            return False
        r = vk_map[ord(ch)]
        vk = r & 0xFF
        shift = (r >> 8) & 1
        if shift:
            user32.keybd_event(0x10, 0, 0, 0)
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)
        if shift:
            user32.keybd_event(0x10, 0, 2, 0)
        time.sleep(interval)
    return True


def type_unicode(text, interval=0.015, cancel_check=None):
    """Unicode 文本输入（中文/emoji/任意字符）：SendInput KEYEVENTF_UNICODE。
    P3a-0 关键件——VkKeyScanW 打不出汉字，中文任务全挂在这条上。"""
    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_KEYUP = 0x0002

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    class INPUT_UNION(ctypes.Union):
        # 真实 union 含 MOUSEINPUT（x64 下 32 字节），不能只按 KEYBDINPUT 的 24 字节填
        _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_ubyte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]

    assert ctypes.sizeof(INPUT) == 40, f"INPUT 尺寸错误: {ctypes.sizeof(INPUT)}"

    for ch in text:
        if cancel_check and cancel_check():
            return False
        code = ord(ch)
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = INPUT()
            inp.type = 1  # INPUT_KEYBOARD
            inp.union.ki.wVk = 0
            inp.union.ki.wScan = code
            inp.union.ki.dwFlags = flags
            inp.union.ki.time = 0
            inp.union.ki.dwExtraInfo = 0
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(interval)
    return True


def launch_app(name):
    """语义级启动应用（不走 Win 键开始菜单——高频开始菜单调用会把 shell
    输入链打楔死，2026-08-21 两次实测）：开始菜单快捷方式 → App Paths → PATH。
    返回 (是否已发出启动, 说明)。发出≠起来，窗口出现由调用方核验。"""
    import glob
    import os
    import subprocess
    name = (name or "").strip()
    if not name:
        return False, "缺少应用名"
    roots = [
        os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
        os.path.expandvars(r"%AppData%\Microsoft\Windows\Start Menu\Programs"),
        os.path.expandvars(r"%USERPROFILE%\Desktop"),
    ]
    needle = name.lower().replace(" ", "")
    hits = []
    for root in roots:
        for p in glob.glob(os.path.join(root, "**", "*.lnk"), recursive=True):
            stem = os.path.splitext(os.path.basename(p))[0].lower().replace(" ", "")
            if needle in stem:
                hits.append(p)
    if hits:
        hits.sort(key=len)   # 名字最短的最贴（"微信" > "微信开发者工具"）
        try:
            os.startfile(hits[0])
            return True, f"快捷方式启动: {os.path.basename(hits[0])}"
        except OSError as e:
            return False, f"快捷方式启动失败: {e}"
    # 开始菜单没有 → App Paths / PATH（os.startfile 对注册应用名有效）
    for cand in (name, name + ".exe"):
        try:
            os.startfile(cand)
            return True, f"直接启动: {cand}"
        except OSError:
            pass
    return False, f"找不到应用 {name}"


def hotkey(*keys, cancel_check=None):
    """如 hotkey('ctrl','v')。支持 shift/ctrl/alt/win + 普通键。
    别名：meta/cmd/super→win，return→enter，escape→esc（模型常用写法）。"""
    mods = {"shift": 0x10, "ctrl": 0x11, "alt": 0x12, "win": 0x5B}
    alias = {"meta": "win", "cmd": "win", "command": "win", "super": "win",
             "return": "enter", "escape": "esc", "del": "delete", " ": "space"}
    codes = []
    for k in keys:
        k = alias.get(k.lower(), k.lower())
        if k in mods:
            codes.append(mods[k])
        elif len(k) == 1:
            codes.append(ord(k.upper()))
        else:
            f = {"enter": 0x0D, "esc": 0x1B, "tab": 0x09, "space": 0x20,
                 "backspace": 0x08, "delete": 0x2E, "home": 0x24, "end": 0x23,
                 "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
                 "f4": 0x73, "f5": 0x74}
            codes.append(f[k])
    if cancel_check and cancel_check():
        return False
    for vk in codes:
        user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)   # 模拟真实按键的按下时长（Win 键等需要 down/up 间隔）
    for vk in reversed(codes):
        user32.keybd_event(vk, 0, 2, 0)
    return True


def activate_hwnd(hwnd):
    """按句柄把窗口提到前台（2026-08-31：后台服务拉起的窗口默认沉底，
    前台全屏窗口会把它整个盖住——验收实锤记事本开了 5 次模型都说没看见）。"""
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    # Windows 防焦点窃取：先附着到前台线程的输入队列再抢前台
    kernel32 = ctypes.windll.kernel32
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None)
    cur_tid = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(cur_tid, fg_tid, True)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    user32.AttachThreadInput(cur_tid, fg_tid, False)
    time.sleep(0.5)
    return True


def activate_window(title_part):
    """把标题包含 title_part 的窗口提到前台，返回是否成功。"""
    import ctypes.wintypes as wt

    found = {"hwnd": None}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if title_part.lower() in buf.value.lower():
            found["hwnd"] = hwnd
            return False
        return True

    user32.EnumWindows(enum_cb, 0)
    hwnd = found["hwnd"]
    if not hwnd:
        return False
    return activate_hwnd(hwnd)


def window_rect(title_part):
    """返回窗口 (left, top, right, bottom)，找不到返回 None。"""
    import ctypes.wintypes as wt

    found = {"hwnd": None}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if title_part.lower() in buf.value.lower():
            found["hwnd"] = hwnd
            return False
        return True

    user32.EnumWindows(enum_cb, 0)
    if not found["hwnd"]:
        return None
    rect = wt.RECT()
    user32.GetWindowRect(found["hwnd"], ctypes.byref(rect))
    return (rect.left, rect.top, rect.right, rect.bottom)


if __name__ == "__main__":
    img = screenshot()
    print("screenshot:", img.size)
