# -*- coding: utf-8 -*-
"""vd_isolate v2：执行面虚拟桌面隔离（用户拍板：平时严格隔离，要求时能展示）。

v2 基座 = IVirtualDesktopManagerInternal（社区对齐的 24H2/25H2 vtable，
源：MScholtes/VirtualDesktop 的 VirtualDesktop11-24H2.cs）：
- CreateDesktop 不切换视图、GetDesktops 可枚举、FindDesktop 按 GUID 找回——
  全程零键鼠注入、零闪屏（v1 注入法会把用户的屏幕切走，已废弃）。
- 挪窗口仍走公开 IVirtualDesktopManager.MoveWindowToDesktop（实测可靠）。

语义：
- hide_browser()：自动化浏览器（.chrome-space CDP 9222）挪进隔离桌面，
  用户主桌面看不见、Alt-Tab 摸不到，CDP 自动化照常（与显示无关）。
- show_browser()：挪回用户当前桌面并置前台（用户说"让我看看"时）。
- 只动项目自己的 .chrome-space 浏览器窗口，用户日常浏览器/桌面绝不碰。
- 隔离桌面命名"小凯工作台"（可识别、可审计）；GUID 落 .tmp/vd_state.json。
"""

import json
import os
import subprocess
import time

import comtypes
import comtypes.client
import ctypes
from ctypes import wintypes
from comtypes import GUID, COMMETHOD, HRESULT, IUnknown

_HERE = os.path.dirname(os.path.abspath(__file__))
_STATE = os.path.join(_HERE, ".tmp", "vd_state.json")
_DESKTOP_NAME = "小凯工作台"

# --- CLSID/IID（24H2 对齐，build 26100/26200 实测） ---
CLSID_ImmersiveShell = GUID("{C2F03A33-21F5-47FA-B4BB-156362A2F239}")
CLSID_VDManagerInternal = GUID("{C5E0CDCA-7B6E-41B2-9FC4-D93975CC467B}")
IID_VDManagerInternal = GUID("{53F5CA0B-158F-4124-900C-057158060B27}")
IID_VirtualDesktop = GUID("{3F07F4BE-B107-441A-AF0F-39D82529072C}")
IID_ObjectArray = GUID("{92CA9DCD-5622-4BBA-A805-5E9F541BD8C9}")
IID_ServiceProvider = GUID("{6D5140C1-7436-11CE-8034-00AA006009FA}")


class IVirtualDesktop(IUnknown):
    _iid_ = IID_VirtualDesktop
    _methods_ = [
        COMMETHOD([], HRESULT, "IsViewVisible",
                  (["in"], ctypes.c_void_p, "view"),
                  (["out"], ctypes.POINTER(ctypes.c_int), "visible")),
        COMMETHOD([], HRESULT, "GetId",
                  (["out"], ctypes.POINTER(GUID), "id")),
        COMMETHOD([], HRESULT, "GetName",
                  (["out"], ctypes.POINTER(ctypes.c_void_p), "hstr")),
        COMMETHOD([], HRESULT, "GetWallpaperPath",
                  (["out"], ctypes.POINTER(ctypes.c_void_p), "hstr")),
        COMMETHOD([], HRESULT, "IsRemote",
                  (["out"], ctypes.POINTER(ctypes.c_int), "isRemote")),
    ]


class IObjectArray(IUnknown):
    _iid_ = IID_ObjectArray
    _methods_ = [
        COMMETHOD([], HRESULT, "GetCount",
                  (["out"], ctypes.POINTER(ctypes.c_int), "count")),
        COMMETHOD([], HRESULT, "GetAt",
                  (["in"], ctypes.c_int, "index"),
                  (["in"], ctypes.POINTER(GUID), "iid"),
                  (["out"], ctypes.POINTER(ctypes.POINTER(IVirtualDesktop)), "obj")),
    ]


class IVirtualDesktopManagerInternal(IUnknown):
    _iid_ = IID_VDManagerInternal
    _methods_ = [  # 24H2 vtable 顺序，一个都不能错位
        COMMETHOD([], HRESULT, "GetCount",
                  (["out"], ctypes.POINTER(ctypes.c_int), "count")),
        COMMETHOD([], HRESULT, "MoveViewToDesktop",
                  (["in"], ctypes.c_void_p, "view"),
                  (["in"], ctypes.POINTER(IVirtualDesktop), "desktop")),
        COMMETHOD([], HRESULT, "CanViewMoveDesktops",
                  (["in"], ctypes.c_void_p, "view"),
                  (["out"], ctypes.POINTER(ctypes.c_int), "can")),
        COMMETHOD([], HRESULT, "GetCurrentDesktop",
                  (["out"], ctypes.POINTER(ctypes.POINTER(IVirtualDesktop)), "desktop")),
        COMMETHOD([], HRESULT, "GetDesktops",
                  (["out"], ctypes.POINTER(ctypes.POINTER(IObjectArray)), "desktops")),
        COMMETHOD([], HRESULT, "GetAdjacentDesktop",
                  (["in"], ctypes.POINTER(IVirtualDesktop), "from"),
                  (["in"], ctypes.c_int, "direction"),
                  (["out"], ctypes.POINTER(ctypes.POINTER(IVirtualDesktop)), "desktop")),
        COMMETHOD([], HRESULT, "SwitchDesktop",
                  (["in"], ctypes.POINTER(IVirtualDesktop), "desktop")),
        COMMETHOD([], HRESULT, "SwitchDesktopAndMoveForegroundView",
                  (["in"], ctypes.POINTER(IVirtualDesktop), "desktop")),
        COMMETHOD([], HRESULT, "CreateDesktop",
                  (["out"], ctypes.POINTER(ctypes.POINTER(IVirtualDesktop)), "desktop")),
        COMMETHOD([], HRESULT, "MoveDesktop",
                  (["in"], ctypes.POINTER(IVirtualDesktop), "desktop"),
                  (["in"], ctypes.c_int, "nIndex")),
        COMMETHOD([], HRESULT, "RemoveDesktop",
                  (["in"], ctypes.POINTER(IVirtualDesktop), "desktop"),
                  (["in"], ctypes.POINTER(IVirtualDesktop), "fallback")),
        COMMETHOD([], HRESULT, "FindDesktop",
                  (["in"], ctypes.POINTER(GUID), "desktopid"),
                  (["out"], ctypes.POINTER(ctypes.POINTER(IVirtualDesktop)), "desktop")),
        COMMETHOD([], HRESULT, "GetDesktopSwitchIncludeExcludeViews",
                  (["in"], ctypes.POINTER(IVirtualDesktop), "desktop"),
                  (["out"], ctypes.POINTER(ctypes.c_void_p), "unknown1"),
                  (["out"], ctypes.POINTER(ctypes.c_void_p), "unknown2")),
        COMMETHOD([], HRESULT, "SetDesktopName",
                  (["in"], ctypes.POINTER(IVirtualDesktop), "desktop"),
                  (["in"], ctypes.c_void_p, "hstr")),
        COMMETHOD([], HRESULT, "SetDesktopWallpaper",
                  (["in"], ctypes.POINTER(IVirtualDesktop), "desktop"),
                  (["in"], ctypes.c_void_p, "hstr")),
        COMMETHOD([], HRESULT, "UpdateWallpaperPathForAllDesktops",
                  (["in"], ctypes.c_void_p, "hstr")),
        COMMETHOD([], HRESULT, "CopyDesktopState",
                  (["in"], ctypes.c_void_p, "pView0"),
                  (["in"], ctypes.c_void_p, "pView1")),
        COMMETHOD([], HRESULT, "CreateRemoteDesktop",
                  (["in"], ctypes.c_void_p, "hstr"),
                  (["out"], ctypes.POINTER(ctypes.POINTER(IVirtualDesktop)), "desktop")),
        COMMETHOD([], HRESULT, "SwitchRemoteDesktop",
                  (["in"], ctypes.POINTER(IVirtualDesktop), "desktop"),
                  (["in"], ctypes.c_void_p, "switchtype")),
        COMMETHOD([], HRESULT, "SwitchDesktopWithAnimation",
                  (["in"], ctypes.POINTER(IVirtualDesktop), "desktop")),
        COMMETHOD([], HRESULT, "GetLastActiveDesktop",
                  (["out"], ctypes.POINTER(ctypes.POINTER(IVirtualDesktop)), "desktop")),
        COMMETHOD([], HRESULT, "WaitForAnimationToComplete"),
    ]


class IServiceProvider(IUnknown):
    _iid_ = IID_ServiceProvider
    _methods_ = [
        COMMETHOD([], HRESULT, "QueryService",
                  (["in"], ctypes.POINTER(GUID), "guidService"),
                  (["in"], ctypes.POINTER(GUID), "riid"),
                  (["out"], ctypes.POINTER(ctypes.c_void_p), "ppv")),
    ]


IID_AppViewCollection = GUID("{1841C6D7-4F9D-42C0-AF41-8747538F10E5}")


class IApplicationViewCollection(IUnknown):
    _iid_ = IID_AppViewCollection
    _methods_ = [  # 24H2 vtable 顺序
        COMMETHOD([], HRESULT, "GetViews",
                  (["out"], ctypes.POINTER(ctypes.c_void_p), "array")),
        COMMETHOD([], HRESULT, "GetViewsByZOrder",
                  (["out"], ctypes.POINTER(ctypes.c_void_p), "array")),
        COMMETHOD([], HRESULT, "GetViewsByAppUserModelId",
                  (["in"], ctypes.c_wchar_p, "id"),
                  (["out"], ctypes.POINTER(ctypes.c_void_p), "array")),
        COMMETHOD([], HRESULT, "GetViewForHwnd",
                  (["in"], wintypes.HWND, "hwnd"),
                  (["out"], ctypes.POINTER(ctypes.c_void_p), "view")),
        COMMETHOD([], HRESULT, "GetViewForApplication",
                  (["in"], ctypes.c_void_p, "application"),
                  (["out"], ctypes.POINTER(ctypes.c_void_p), "view")),
        COMMETHOD([], HRESULT, "GetViewForAppUserModelId",
                  (["in"], ctypes.c_wchar_p, "id"),
                  (["out"], ctypes.POINTER(ctypes.c_void_p), "view")),
        COMMETHOD([], HRESULT, "GetViewInFocus",
                  (["out"], ctypes.POINTER(ctypes.c_void_p), "view")),
        COMMETHOD([], HRESULT, "Unknown1",
                  (["out"], ctypes.POINTER(ctypes.c_void_p), "view")),
        COMMETHOD([], HRESULT, "RefreshCollection"),
        COMMETHOD([], HRESULT, "RegisterForApplicationViewChanges",
                  (["in"], ctypes.c_void_p, "listener"),
                  (["out"], ctypes.POINTER(ctypes.c_int), "cookie")),
        COMMETHOD([], HRESULT, "UnregisterForApplicationViewChanges",
                  (["in"], ctypes.c_int, "cookie")),
    ]


class IVirtualDesktopManager(IUnknown):
    _iid_ = GUID("{a5cd92ff-29be-454c-8d04-d82879fb3f1b}")
    _methods_ = [
        COMMETHOD([], HRESULT, "IsWindowOnCurrentVirtualDesktop",
                  (["in"], wintypes.HWND, "topLevelWindow"),
                  (["out"], ctypes.POINTER(ctypes.c_int), "onCurrentDesktop")),
        COMMETHOD([], HRESULT, "GetWindowCurrentDesktopId",
                  (["in"], wintypes.HWND, "topLevelWindow"),
                  (["out"], ctypes.POINTER(GUID), "desktopId")),
        COMMETHOD([], HRESULT, "MoveWindowToDesktop",
                  (["in"], wintypes.HWND, "topLevelWindow"),
                  (["in"], ctypes.POINTER(GUID), "desktopId")),
    ]


_u32 = ctypes.windll.user32
_combase = ctypes.windll.combase
_vdmi = None
_vdm = None


def _mi():
    global _vdmi
    if _vdmi is None:
        sp = comtypes.client.CreateObject(CLSID_ImmersiveShell, interface=IServiceProvider)
        p = sp.QueryService(CLSID_VDManagerInternal, IID_VDManagerInternal)
        _vdmi = ctypes.cast(p, ctypes.POINTER(IVirtualDesktopManagerInternal))
    return _vdmi


_avc_inst = None


def _avc():
    global _avc_inst
    if _avc_inst is None:
        sp = comtypes.client.CreateObject(CLSID_ImmersiveShell, interface=IServiceProvider)
        p = sp.QueryService(IID_AppViewCollection, IID_AppViewCollection)
        _avc_inst = ctypes.cast(p, ctypes.POINTER(IApplicationViewCollection))
    return _avc_inst


def _move_window(hwnd, desktop):
    """跨进程窗口挪桌面：shell 同款 MoveViewToDesktop 通道
    （公开 IVirtualDesktopManager.MoveWindowToDesktop 对别的进程窗口会
    E_ACCESSDENIED——26200 实测）。"""
    view = _avc().GetViewForHwnd(hwnd)
    _mi().MoveViewToDesktop(view, desktop)


def _pub():
    global _vdm
    if _vdm is None:
        _vdm = comtypes.client.CreateObject(
            GUID("{aa509086-5ca9-4c25-8f95-589d3c07b48a}"), interface=IVirtualDesktopManager)
    return _vdm


def _guid_of(desktop):
    return str(desktop.GetId()).strip("{}").upper()


def _hstr(text):
    """Python str → HSTRING（调用方负责 WindowsDeleteString）。"""
    h = ctypes.c_void_p()
    _combase.WindowsCreateString(str(text), len(str(text)), ctypes.byref(h))
    return h


def _desktops():
    """[(guid, IVirtualDesktop)]：当前全部虚拟桌面。"""
    arr = _mi().GetDesktops()
    out = []
    for i in range(arr.GetCount()):
        out.append(arr.GetAt(i, IID_VirtualDesktop))
    return [(_guid_of(d), d) for d in out]


def _current_guid():
    return _guid_of(_mi().GetCurrentDesktop())


def _desktop_by_guid(guid):
    try:
        return _mi().FindDesktop(GUID("{%s}" % guid))
    except Exception:
        return None


def ensure_isolated_desktop(log=print):
    """拿回隔离桌面对象（没有就建——CreateDesktop 不切视图，用户完全无感）。"""
    try:
        st = json.load(open(_STATE, encoding="utf-8"))
        guid = st.get("desktop_guid")
    except (OSError, ValueError):
        guid = None
    if guid:
        d = _desktop_by_guid(guid)
        if d is not None:
            h = _hstr(_DESKTOP_NAME)   # 复用也补名（v1 注入时代的桌面没名字）
            try:
                _mi().SetDesktopName(d, h)
            finally:
                _combase.WindowsDeleteString(h)
            return d
        log("vd_isolate: 缓存的隔离桌面已失效，重建")
    d = _mi().CreateDesktop()
    h = _hstr(_DESKTOP_NAME)
    try:
        _mi().SetDesktopName(d, h)
    finally:
        _combase.WindowsDeleteString(h)
    guid = _guid_of(d)
    os.makedirs(os.path.dirname(_STATE), exist_ok=True)
    with open(_STATE, "w", encoding="utf-8") as f:
        json.dump({"desktop_guid": guid}, f)
    log(f"vd_isolate: 隔离桌面已建 {guid}（{_DESKTOP_NAME}）")
    return d


def find_automation_browser_hwnd():
    """项目自动化浏览器（.chrome-space）的顶层可见窗口；找不到返回 None。
    通过命令行里的 .chrome-space 标识区分用户的日常 Chrome（绝不碰）。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" "
             "| Where-Object {$_.CommandLine -match 'chrome-space'} "
             "| Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=15)
        pids = {int(x) for x in out.stdout.split() if x.strip().isdigit()}
    except Exception:
        return None
    if not pids:
        return None
    hits = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, lp):
        pid = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and _u32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(256)
            _u32.GetWindowTextW(hwnd, title, 256)
            if title.value:
                hits.append(hwnd)
        return True

    _u32.EnumWindows(EnumProc(cb), 0)
    return hits[0] if hits else None


def _window_desktop_guid(hwnd):
    try:
        g = _pub().GetWindowCurrentDesktopId(hwnd)
        return str(g).strip("{}").upper() if g else None
    except Exception:
        return None


def hide_browser(log=print):
    """把自动化浏览器挪到隔离桌面（平时状态）。返回是否成功。"""
    hwnd = find_automation_browser_hwnd()
    if not hwnd:
        return False
    iso = ensure_isolated_desktop(log=log)
    cur = _window_desktop_guid(hwnd)
    if cur == _guid_of(iso):
        return True  # 已经藏着
    try:
        _move_window(hwnd, iso)
        log("vd_isolate: 执行面已藏到隔离桌面")
        return True
    except Exception as e:
        log(f"vd_isolate: 隐藏失败 {e}")
        return False


def show_browser(log=print):
    """把自动化浏览器挪回用户当前桌面并置前台（要求展示时）。返回是否成功。"""
    hwnd = find_automation_browser_hwnd()
    if not hwnd:
        return False
    cur_guid = _current_guid()
    if _window_desktop_guid(hwnd) == cur_guid:
        _u32.ShowWindow(hwnd, 9)
        _u32.SetForegroundWindow(hwnd)
        return True  # 已经在用户眼前
    target = _desktop_by_guid(cur_guid)
    if target is None:
        return False
    try:
        _move_window(hwnd, target)
        _u32.ShowWindow(hwnd, 9)  # SW_RESTORE
        _u32.SetForegroundWindow(hwnd)
        log("vd_isolate: 执行面已展示到当前桌面")
        return True
    except Exception as e:
        log(f"vd_isolate: 展示失败 {e}")
        return False


def is_hidden():
    """自动化浏览器当前是否不在用户正在看的桌面上。"""
    hwnd = find_automation_browser_hwnd()
    if not hwnd:
        return None
    return _window_desktop_guid(hwnd) != _current_guid()


def desktop_report():
    """调试用：枚举全部桌面及各自窗口数（EnumWindows + 公开管理器逐窗归属）。"""
    tally = {g: 0 for g, _ in _desktops()}
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, lp):
        if _u32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(256)
            _u32.GetWindowTextW(hwnd, title, 256)
            if title.value:
                g = _window_desktop_guid(hwnd)
                if g in tally:
                    tally[g] += 1
        return True

    _u32.EnumWindows(EnumProc(cb), 0)
    cur = _current_guid()
    return [{"guid": g, "current": g == cur, "windows": tally.get(g, 0)}
            for g, _ in _desktops()]


def cleanup_empty_strays(keep_guid, log=print):
    """清掉 v1 注入时代残留的空桌面：只删"非当前、非隔离、零窗口"的。
    有窗口的桌面一律不碰（可能是用户自己的）。"""
    removed = 0
    cur = _current_guid()
    for g, d in _desktops():
        if g in (cur, keep_guid):
            continue
        tally = 0
        EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, lp):
            nonlocal tally
            if _u32.IsWindowVisible(hwnd):
                title = ctypes.create_unicode_buffer(256)
                _u32.GetWindowTextW(hwnd, title, 256)
                if title.value and _window_desktop_guid(hwnd) == g:
                    tally += 1
            return True

        _u32.EnumWindows(EnumProc(cb), 0)
        if tally == 0:
            try:
                _mi().RemoveDesktop(d, _mi().GetCurrentDesktop())
                removed += 1
                log(f"vd_isolate: 清掉空残留桌面 {g}")
            except Exception as e:
                log(f"vd_isolate: 清理 {g} 失败 {e}")
    return removed
