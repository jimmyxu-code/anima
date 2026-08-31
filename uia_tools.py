# -*- coding: utf-8 -*-
"""uia_tools：UIA 语义定位（混合层的"找"，毫秒级）。

定位哲学（用户拍的板）：UIA 负责找元素坐标，真鼠标负责点——
找得快找得准，点还是看得见地点。UIA 覆盖不了的界面（Electron/游戏/自绘）
返回 None，调用方回退纯视觉。

用 comtypes 直调 UIAutomationCore，不引第三方重依赖。
"""

import ctypes
import time

import comtypes
import comtypes.client

_uia = None
_TRUE_COND = None

# TreeScope
_CHILDREN = 2
_DESCENDANTS = 4

_TYPE_IDS = {"button": 50000, "edit": 50004, "text": 50020, "menuitem": 50011,
             "listitem": 50007, "hyperlink": 50005, "tabitem": 50019,
             "checkbox": 50002, "combobox": 50003, "image": 50006}

# 可交互控件类型（dump 语义接地用）：ID → 中文标签。
# text/image 不收（静态内容截图已看得见；dump 只给"能点的"）。
_INTERACTIVE_TYPE_IDS = {
    50000: "按钮", 50004: "输入框", 50011: "菜单项", 50007: "列表项",
    50005: "链接", 50019: "标签页", 50002: "复选框", 50003: "下拉框",
}
_UA_CONTROL_TYPE_PID = 30003
_INTERACTIVE_COND = None


def _get_interactive_cond():
    """交互类型 OrCondition：FindAll 只回匹配节点——巨型树（浏览器）上千
    节点的 COM 往返从"全树遍历"降到"几十个控件"，每步注入才喂得起。"""
    global _INTERACTIVE_COND
    if _INTERACTIVE_COND is None:
        uia = _get_uia()
        conds = [uia.CreatePropertyCondition(_UA_CONTROL_TYPE_PID, t)
                 for t in _INTERACTIVE_TYPE_IDS]
        cond = conds[0]
        for c in conds[1:]:
            cond = uia.CreateOrCondition(cond, c)
        _INTERACTIVE_COND = cond
    return _INTERACTIVE_COND


def _render_dump(raw, max_items=40):
    """raw=[(label,name,left,top,w,h)] → 单行清单文本（纯函数，可单测）。
    去重（同类型同名）、阅读顺序（上→下左→右）、截 max_items 并注明余量。"""
    items = []
    seen = set()
    for label, name, left, top, w, h in raw:
        name = (name or "").strip()
        if not name or w <= 0 or h <= 0 or (label, name) in seen:
            continue
        seen.add((label, name))
        cx, cy = int(left + w / 2), int(top + h / 2)
        items.append((top, left, f"{label}「{name[:18]}」@({cx},{cy})"))
    if not items:
        return None
    items.sort()
    lines = [t for _, _, t in items[:max_items]]
    more = len(items) - len(lines)
    text = "当前窗口可交互控件（要点击优先 uia_click 填「」里的名字，精准落点）："
    text += "；".join(lines)
    if more > 0:
        text += f"；…另有 {more} 个未列出"
    return text


def dump_interactive(max_items=40, max_nodes=2000):
    """前台窗口可交互控件清单（2026-08-29 路线A 语义接地）——每步喂给模型，
    让它按名点（uia_click）而不是像素猜。UIA 覆盖不了（Electron/游戏/自绘/
    提权窗口 UIPI）返回 None，调用方回退纯视觉。"""
    try:
        uia = _get_uia()
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return None
        root_el = uia.ElementFromHandle(hwnd)
        els = root_el.FindAll(_DESCENDANTS, _get_interactive_cond())
        n = min(els.Length, max_nodes)
    except Exception:
        return None
    raw = []
    for i in range(n):
        try:
            el = els.GetElement(i)
            if el.CurrentIsOffscreen:
                continue
            label = _INTERACTIVE_TYPE_IDS.get(el.CurrentControlType)
            if label is None:
                continue
            r = el.CurrentBoundingRectangle
            raw.append((label, el.CurrentName or "",
                        r.left, r.top, r.right - r.left, r.bottom - r.top))
        except Exception:
            continue
    return _render_dump(raw, max_items=max_items)


def _get_uia():
    global _uia, _TRUE_COND
    if _uia is None:
        comtypes.CoInitialize()
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as U
        _uia = comtypes.client.CreateObject(
            U.CUIAutomation, interface=U.IUIAutomation)
        _TRUE_COND = _uia.CreateTrueCondition()
    return _uia


def find_element(name_part, control_type=None, timeout=3.0, root="foreground"):
    """按名称模糊查找 UIA 元素，返回物理像素中心 (x, y) 或 None。
    name_part: 名称包含此子串（不区分大小写）。
    control_type: "button"/"edit"/"menuitem"/"text"/"listitem"/"hyperlink" 等。
    root: "foreground"=前台窗口内找；"desktop"=全桌面（慢，慎用于开始菜单等 shell 表面）。
    同一名字多处出现时返回面积最小的那个（多半是按钮本体而非容器）。"""
    uia = _get_uia()
    deadline = time.time() + timeout
    needle = name_part.lower()
    want_type = _TYPE_IDS.get(control_type.lower()) if control_type else None

    while time.time() < deadline:
        try:
            if root == "foreground":
                hwnd = ctypes.windll.user32.GetForegroundWindow()
                if not hwnd:
                    time.sleep(0.2)
                    continue
                root_el = uia.ElementFromHandle(hwnd)
            else:
                root_el = uia.GetRootElement()
            hit = _search_once(root_el, needle, want_type)
            if hit is not None:
                return hit
        except Exception:
            pass
        time.sleep(0.25)
    return None


def _norm(s):
    """名称归一化：去掉快捷键括号（文件(F)≈文件）、统一小写、去空白全半角。"""
    import re
    s = (s or "").lower()
    s = re.sub(r"[（(][a-z0-9]+[)）]", "", s)
    return s.strip()


def _search_once(root_el, needle, want_type, max_nodes=3000):
    best = None
    best_area = None
    needle = _norm(needle)
    try:
        els = root_el.FindAll(_DESCENDANTS, _TRUE_COND)
        n = min(els.Length, max_nodes)
    except Exception:
        return None
    for i in range(n):
        try:
            el = els.GetElement(i)
            name = _norm(el.CurrentName)
            if not name or needle not in name:
                continue
            if want_type is not None and el.CurrentControlType != want_type:
                continue
            r = el.CurrentBoundingRectangle
            w, h = r.right - r.left, r.bottom - r.top
            if w <= 0 or h <= 0:
                continue
            # 离屏元素跳过（CurrentIsOffscreen）
            if el.CurrentIsOffscreen:
                continue
            area = w * h
            if best_area is None or area < best_area:
                best_area = area
                best = (int(r.left + w / 2), int(r.top + h / 2))
        except Exception:
            continue
    return best


def focused_element():
    """当前焦点元素 (name, control_type, rect) 或 None。事件校验用：
    点击已聚焦文本框无视觉变化时，焦点转移才是真证据。"""
    try:
        uia = _get_uia()
        el = uia.GetFocusedElement()
        if el is None:
            return None
        r = el.CurrentBoundingRectangle
        return {"name": el.CurrentName or "",
                "type": el.CurrentControlType,
                "rect": (r.left, r.top, r.right, r.bottom)}
    except Exception:
        return None


def foreground_app_name():
    """前台进程名（诊断用）。"""
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = ctypes.c_ulong()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = k32.OpenProcess(0x1000, False, pid.value)
    if not h:
        return ""
    buf = ctypes.create_unicode_buffer(260)
    size = ctypes.c_ulong(260)
    name = ""
    if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
        name = buf.value.rsplit("\\", 1)[-1]
    k32.CloseHandle(h)
    return name


if __name__ == "__main__":
    import sys
    target = " ".join(sys.argv[1:]) or "记事本"
    print("前台:", foreground_app_name())
    t0 = time.time()
    print("find", target, "->", find_element(target, timeout=5),
          f"({time.time()-t0:.2f}s)")
