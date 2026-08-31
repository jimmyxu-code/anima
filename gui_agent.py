# -*- coding: utf-8 -*-
"""gui_agent：显式 GUI 执行循环（P3a 内核）。

循环：截图 → gui_brain 出动作 → 动作闸（action_intent 审计）→
坐标映射（0-1000 相对 → 物理像素）→ host_input 执行 → 再截图校验。
双速率：连续 2 步无进展自动开 thinking（P3c 先埋开关）。

安全边界（报告定的）：
- 动作闸：意图文本命中不可逆关键词（删/发/付/设置）→ 拒绝执行，直接收尾
- 只动不删兜底：delete/backspace 键只允许在明确输入文本场景出现——
  模型协议里没有独立 delete 动作，hotkey 里的 delete 一律拦截
"""

import ctypes
import json
import math
import os
import re
import sys
import threading
import time
from collections import deque
from ctypes import wintypes

import gui_brain
import host_input
import dom_tools
import uia_tools
from collaboration_policy import classify_real_input

_WM_MOUSE_DECISIVE = {
    0x0201, 0x0202, 0x0204, 0x0205, 0x0207, 0x0208,
    0x020B, 0x020C, 0x020A, 0x020E,
}
_WM_KEY_DECISIVE = {0x0100, 0x0101, 0x0104, 0x0105}

# --- 语音纠正注入（2026-08-29 用户裁决"语音即纠正"）：GUI 任务运行期间，
# 用户说的话由 companion_rt 转进来，下一步决策必须吸收——不再聋跑 ---
_user_hints = deque(maxlen=8)
_hints_lock = threading.Lock()


def push_user_hint(text):
    with _hints_lock:
        _user_hints.append(str(text)[:200])


def _drain_user_hints():
    with _hints_lock:
        hints = list(_user_hints)
        _user_hints.clear()
    return hints


def _similar_stall(sig_window):
    """连续 3 轮动作签名（动作名+意图）相似且屏幕全无实质变化 → 死循环。
    移植自封存包并修正：原实现比对 final_text 与 h[0]=="步骤"（永不存在）
    = 死代码，哨兵测试照绿；改为动作签名两两比对（difflib>0.7），
    调用点在 UIA 焦点豁免之后（焦点转移=真变化，不算死循环）。"""
    import difflib
    if len(sig_window) != 3 or any(changed for _, changed in sig_window):
        return False
    first = sig_window[0][0]
    return all(difflib.SequenceMatcher(None, first, s).ratio() > 0.7
               for s, _ in list(sig_window)[1:])


def _uia_key_of(actions):
    """一批动作里具名目标（uia_click/dom_click 的 text）归一化成可比对 key。"""
    return tuple(sorted(_clean_uia_text(a.get("text", "")).lower()
                        for a in actions
                        if a.get("action") in ("uia_click", "dom_click")
                        and _clean_uia_text(a.get("text", ""))))


def _uia_stall(last, key, changed):
    """语义停滞：本批具名目标与上批完全相同，且两批屏幕都零变化 → 同一
    控件怎么点都没反应，2 击即换路（不等通用 3 轮签名判定）。"""
    return bool(key and last and key == last[0] and not changed and not last[1])


_UIA_TYPE_WORDS = "按钮|输入框|菜单项|列表项|链接|标签页|复选框|下拉框"


def _clean_uia_text(text):
    """模型照抄清单格式实锤（'下拉框「Windows 显示语言」'整个塞进 text →
    永远找不到）：剥类型词、「」引号和 @(x,y) 尾巴，只留控件名本体。"""
    t = str(text or "").strip()
    t = re.sub(rf"^(?:{_UIA_TYPE_WORDS})\s*[「『\"']?", "", t)
    t = re.sub(r"@\(\d+,\s*\d+\)\s*$", "", t).strip()
    return t.rstrip("」』\"'").strip()


def _same_spot(points, radius=40, need=4):
    """最近 need 个点击点挤在 radius 半径内 = 物理层同点死磕（菜单开合
    骗过像素差、措辞微变骗过意图签名时的最后一道网）。"""
    if len(points) < need:
        return False
    pts = list(points)[-need:]
    cx = sum(p[0] for p in pts) / need
    cy = sum(p[1] for p in pts) / need
    return all((p[0] - cx) ** 2 + (p[1] - cy) ** 2 <= radius ** 2 for p in pts)


def _foreground_hung(timeout_ms=800):
    """前台窗口挂死检测（SendMessageTimeout 超时=无响应）。2026-08-29 实锤：
    挂死的设置应用变幽灵窗——吃点击不做任何事，agent 对死窗空点 30 步。
    健康窗口立即返回（~0ms）；挂死时最多阻塞 timeout_ms。"""
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return False
        result = ctypes.c_size_t(0)
        ok = ctypes.windll.user32.SendMessageTimeoutW(
            hwnd, 0, 0, 0, 0x0002, timeout_ms, ctypes.byref(result))
        return ok == 0
    except Exception:
        return False

# DPI 感知：保证截图尺寸与 GetSystemMetrics 都是物理像素
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    pass


class TakeoverWatcher:
    """真实用户输入侦测（P3c 接管检测）：LL 鼠标+键盘钩子，
    记录非注入事件；轻微短时纯鼠标移动属于协作，明确输入才暂停。"""

    def __init__(self):
        self.last_real_input = 0.0
        self._events = deque(maxlen=256)
        self._motion_bursts = deque(maxlen=128)
        self._events_lock = threading.Lock()
        self._ready = threading.Event()
        self._takeover_latch = threading.Event()
        self._seq = 0
        self._decisive_seq = 0
        self.hook_handles = (None, None)
        self.hook_error = None

    def _record(self, kind, x=0, y=0):
        now = time.perf_counter()
        self.last_real_input = now
        with self._events_lock:
            self._seq += 1
            seq = self._seq
            point = (int(x), int(y))
            self._events.append((seq, now, kind, point[0], point[1]))
            if kind == "move":
                if (self._motion_bursts
                        and now - self._motion_bursts[-1][3] <= 0.25):
                    burst = self._motion_bursts[-1]
                    burst[1] = seq
                    burst[3] = now
                    burst[6] = point[0]
                    burst[7] = point[1]
                    burst[8] = min(burst[8], point[0])
                    burst[9] = max(burst[9], point[0])
                    burst[10] = min(burst[10], point[1])
                    burst[11] = max(burst[11], point[1])
                else:
                    self._motion_bursts.append([
                        seq, seq, now, now, point[0], point[1], point[0], point[1],
                        point[0], point[0], point[1], point[1],
                    ])
            else:
                self._decisive_seq = seq
                self._takeover_latch.set()

    def last_real_click(self):
        """最近一次真实（非注入）点击的 (x, y, age_s)；无则 None。
        2026-08-29 用户裁决"注入+卡死即问"：用户的物理操作是意图表达，
        坐标要注入 agent 决策（不再只当协作噪声）。"""
        with self._events_lock:
            for seq, now, kind, x, y in reversed(self._events):
                if kind != "move":
                    return (int(x), int(y), time.perf_counter() - now)
        return None

    def start(self):
        threading.Thread(target=self._thread, daemon=True).start()
        if not self._ready.wait(3):
            self.hook_error = "hook startup timeout"
            self._takeover_latch.set()
        return self.healthy()

    def healthy(self):
        return (self._ready.is_set() and self.hook_error is None
                and all(bool(h) for h in self.hook_handles))

    def cursor(self):
        with self._events_lock:
            return self._seq

    def _thread(self):
        u32 = ctypes.windll.user32
        LRESULT_T = ctypes.c_ssize_t
        WPARAM_T = ctypes.c_size_t
        LPARAM_T = ctypes.c_ssize_t

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT_T, ctypes.c_int, WPARAM_T, LPARAM_T)
        u32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, WPARAM_T, LPARAM_T]
        u32.CallNextHookEx.restype = LRESULT_T
        u32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        u32.SetWindowsHookExW.restype = wintypes.HANDLE
        u32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
        u32.UnhookWindowsHookEx.restype = wintypes.BOOL
        LLMHF_INJECTED = 0x01
        LLKHF_INJECTED = 0x10
        WM_MOUSEMOVE = 0x0200

        def mouse_proc(nCode, wParam, lParam):
            if nCode == 0 and (wParam == WM_MOUSEMOVE or wParam in _WM_MOUSE_DECISIVE):
                info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                if not (info.flags & LLMHF_INJECTED):
                    self._record("move" if wParam == WM_MOUSEMOVE else "mouse",
                                 info.pt.x, info.pt.y)
            return u32.CallNextHookEx(None, nCode, wParam, lParam)

        def kbd_proc(nCode, wParam, lParam):
            if nCode == 0 and wParam in _WM_KEY_DECISIVE:
                info = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if not (info.flags & LLKHF_INJECTED):
                    self._record("key")
            return u32.CallNextHookEx(None, nCode, wParam, lParam)

        self._refs = [HOOKPROC(mouse_proc), HOOKPROC(kbd_proc)]
        h1 = u32.SetWindowsHookExW(14, self._refs[0], None, 0)  # WH_MOUSE_LL
        h2 = u32.SetWindowsHookExW(13, self._refs[1], None, 0)  # WH_KEYBOARD_LL
        self.hook_handles = (h1, h2)
        if not h1 or not h2:
            self.hook_error = ctypes.get_last_error()
            self._takeover_latch.set()
        self._ready.set()
        if not h1 or not h2:
            for handle in (h1, h2):
                if handle:
                    u32.UnhookWindowsHookEx(handle)
            return
        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(msg)
            u32.DispatchMessageW(msg)
        self.hook_error = self.hook_error or "hook message loop ended"
        self._takeover_latch.set()

    def real_input_since(self, t0):
        return self.interaction_since(t0) is not None

    def interaction_since(self, t0, max_coop_px=80.0, max_coop_s=1.25,
                          origin=None):
        """区分轻微协作和明确接管。

        纯鼠标小幅移动是协作提示：让模型下一步重新观察，但不暂停任务；
        点击、滚轮、键盘或明显大幅移动才交还控制权。
        """
        with self._events_lock:
            events = [(e[1], e[2], e[3], e[4])
                      for e in self._events if e[1] >= t0]
        return classify_real_input(events, max_coop_px, max_coop_s,
                                   origin=origin)

    def consume_since(self, last_seq, max_coop_px=80.0, max_coop_s=1.25,
                      origin=None):
        """按单调序号消费输入；决定性输入即使挤出原始队列也不会丢。"""
        with self._events_lock:
            end_seq = self._seq
            if not self.healthy():
                # B6：钩子死亡≠用户接管——分开归因，别冤报"用户动了鼠标"
                return "hook_dead", end_seq
            if self._decisive_seq > last_seq:
                return "takeover", end_seq
            raw = [e for e in self._events if e[0] > last_seq]
            bursts = [list(b) for b in self._motion_bursts if b[1] > last_seq]
        if not raw and not bursts:
            return None, end_seq
        events = [(e[1], e[2], e[3], e[4]) for e in raw]
        interaction = classify_real_input(
            events, max_coop_px, max_coop_s, origin=origin)
        if interaction == "takeover":
            return interaction, end_seq
        for burst in bursts:
            duration = burst[3] - burst[2]
            span = math.hypot(burst[9] - burst[8], burst[11] - burst[10])
            if duration > max_coop_s or span > max_coop_px:
                return "takeover", end_seq
        return interaction or "cooperate", end_seq


def _diff_ratio(img_before, img_after):
    """降采样灰度图的平均像素差比例（0-1），用于事件校验（动作是否真的改变了屏幕）。"""
    from PIL import ImageChops
    a = img_before.convert("L").resize((160, 90))
    b = img_after.convert("L").resize((160, 90))
    diff = ImageChops.difference(a, b)
    hist = diff.histogram()
    total = sum(i * c for i, c in enumerate(hist))
    return total / (255.0 * 160 * 90)

_MAX_STEPS = 30
_BANNED_INTENT_RE = re.compile(
    r"删除(?!线)|删掉|卸载|发送|发消息|支付|付款|转账|红包|付款码|下单|购买|"
    r"格式化|清空|注销|关机|重启电脑|改密码|退出登录")
# confirm/auto 档的高危召回面。yolo 不再复用它做最终裁决：用户明确下达
# “给谁发什么”已经构成授权，发送/发布/提交不能在最大自主档再问一遍。
_TOP_INTENT_RE = re.compile(
    r"删除(?!线)|删掉|卸载|发送|发消息|发出|发布|提交|支付|付款|转账|红包|"
    r"付款码|下单|购买|格式化|清空|注销|关机|重启|改密码|退出登录|"
    r"系统设置|注册表|权限")
# 最大自主档机械兜底：只有永久毁数据相关表达。正常由模型主判；付款、发送、
# 关机、卸载、改系统均不在这里，避免再把“高风险”偷换成“不可恢复删除”。
_IRRECOVERABLE_DELETE_FALLBACK_RE = re.compile(
    r"永久删除|彻底删除|不可恢复|无法恢复|清空回收站|格式化|抹除|销毁|"
    r"清空.{0,10}(聊天记录|账户数据|文件|数据)|Shift\s*\+?\s*Delete", re.I)


def _is_external_send_action(action):
    """当前这一下是否真的在触发外部发送。

    只看当前控件/当前动作语义，不把“登录微信，后续发送”这种计划描述当成
    已发送；用于 exactly-once 防重，不参与权限放行。
    """
    name = str(action.get("action", ""))
    target = _clean_uia_text(action.get("text", "")).strip().lower()
    if name in ("uia_click", "dom_click") and target in {
            "发送", "send", "发布", "提交", "确认发送"}:
        return True
    intent = str(action.get("intent", ""))
    return bool(re.search(
        r"点击.{0,8}(发送|发布|提交)|(?:完成|执行).{0,10}(发送|发布|提交).{0,8}(最后一步|操作)|"
        r"^(?:向|给).{0,30}(发送|发出|发布|提交)", intent, re.I))


def _confirmation_actions(danger, mode, task_approved, task_text="", log=print):
    """按权限档返回这一批里仍需语音确认的动作。"""
    danger = list(danger or [])
    if mode == "yolo":
        import intent_judge
        intents = [str(a.get("intent", "")) for a in danger]
        flags = intent_judge.judge_irrecoverable_deletions(intents, timeout=4)
        if flags is None:
            flags = [bool(_IRRECOVERABLE_DELETE_FALLBACK_RE.search(i))
                     for i in intents]
            log("[gui_agent] 最大自主模型裁决不可用，使用不可恢复删除机械兜底")
        kept = [a for a, flag in zip(danger, flags) if flag]
        skipped = [a for a, flag in zip(danger, flags) if not flag]
        if skipped:
            log("[gui_agent] 最大自主档放行非不可恢复删除意图: "
                + "；".join(str(a.get("intent", ""))[:30] for a in skipped))
        return kept
    if mode == "auto" and "intent" in task_approved:
        log("[gui_agent] 自动通过档：本任务已批准过危险意图，放行")
        return []
    return danger


def _dedupe_external_send_actions(actions, performed_effects):
    """返回 (可执行动作, 是否拦到重复发送)。"""
    repeated = [a for a in actions
                if _is_external_send_action(a)
                and "external-send" in performed_effects]
    return [a for a in actions if a not in repeated], bool(repeated)
_BANNED_KEYS = {"delete"}  # hotkey 里禁止出现的键（backspace 允许，输入纠错需要）
_ALLOWED_ACTIONS = {
    "click", "uia_click", "dom_click", "open_url", "left_double",
    "right_single", "drag", "scroll", "type", "hotkey", "wait",
    "launch_app",
}
_POINT_ACTIONS = {"click", "left_double", "right_single", "scroll"}

_STOP = threading.Event()

# TakeoverWatcher 单例：每进程只装一对 LL 钩子。
# 此前每次 run() 新建 watcher → 钩子累积 → 事件队列堵塞 →
# Windows 丢弃超时键盘事件（benchmark 后段"屏幕无变化"全瘫的根因）
_WATCHER = None
_WATCHER_LOCK = threading.Lock()


def _get_watcher():
    global _WATCHER
    with _WATCHER_LOCK:
        if _WATCHER is None:
            _WATCHER = TakeoverWatcher()
            _WATCHER.start()
    return _WATCHER

_METRICS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "压测", "gui-metrics.jsonl")

_RECIPES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "压测", "recipes.jsonl")


def _bigrams(s):
    cjk = re.sub(r"[^一-鿿a-z0-9]", "", (s or "").lower())
    return {cjk[i:i + 2] for i in range(len(cjk) - 1)}


def _match_recipe(task):
    """recipes.jsonl 里找同类任务的成功配方（bigram 重叠≥2，取最近一条）。"""
    try:
        q = _bigrams(task)
        if not q:
            return None
        best = None
        with open(_RECIPES_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if len(q & _bigrams(r.get("task", ""))) >= 2:
                    best = r
        return best
    except OSError:
        return None


def _record_recipe(task, steps_summary, duration, steps=None):
    """验证成功的任务沉淀为配方（Voyager 技能库的最小实现）。
    2026-08-31 阶段 C（GUI 配方回放=图节点类型②）：v2 结构化行——
    {v:2, task, sig, steps[规范化动作], duration_s}，供确定性回放；
    同 sig 旧 v2 行被替换（新者胜）。hint 匹配器照读 task 字段不受影响。"""
    try:
        os.makedirs(os.path.dirname(_RECIPES_PATH), exist_ok=True)
        rec = {"v": 2, "task": task[:120],
               "sig": re.sub(r"\s+", "", (task or "").lower())[:60],
               "summary": steps_summary[:300], "duration_s": round(duration, 1),
               "steps": list(steps or []),
               "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        old_lines, kept = [], []
        try:
            with open(_RECIPES_PATH, encoding="utf-8") as f:
                old_lines = f.readlines()
        except OSError:
            pass
        for line in old_lines:
            try:
                r = json.loads(line)
            except ValueError:
                kept.append(line)
                continue
            if r.get("v") == 2 and r.get("sig") == rec["sig"]:
                continue   # 同 sig 旧 v2 被新者替换
            kept.append(line)
        kept.append(json.dumps(rec, ensure_ascii=False) + "\n")
        with open(_RECIPES_PATH, "w", encoding="utf-8") as f:
            f.writelines(kept)
    except OSError:
        pass


def _match_recipe_v2(task):
    """recipes.jsonl 里找同类任务的可回放配方（v2 行，bigram 重叠≥2，取最新）。"""
    try:
        q = _bigrams(task)
        if not q:
            return None
        best = None
        with open(_RECIPES_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("v") != 2 or not r.get("steps"):
                    continue
                if len(q & _bigrams(r.get("task", ""))) >= 2:
                    best = r
        return best
    except OSError:
        return None


def _norm_step(a):
    """执行过的动作 → 可回放规范化步骤（阶段 C）。"""
    s = {"action": a.get("action")}
    name = a.get("action")
    if name in ("uia_click", "dom_click"):
        s["text"] = _clean_uia_text(a.get("text", ""))
    elif name in ("click", "left_double", "right_single", "scroll"):
        if a.get("point") is not None:
            s["point"] = a.get("point")
        if name == "scroll":
            s["delta"] = a.get("delta", -3)
    elif name == "hotkey":
        s["keys"] = a.get("keys", "")
    elif name == "type":
        s["text"] = a.get("text", "")
    elif name == "wait":
        s["ms"] = a.get("ms", 500)
    elif name == "launch_app":
        s["name"] = a.get("name", "")
    return s


def _replay_steps(steps, log=print, on_step=None, cancel_check=None):
    """配方回放执行器（图节点类型②）：确定性逐步执行 ~0.3s/步。
    uia/dom 步以"找得到目标"为每步验证；失配=如实返回失败原因，
    由调用方回退 VLM 循环并重新沉淀。"""
    total = len(steps)
    for i, s in enumerate(steps, 1):
        if cancel_check and cancel_check():
            return False, f"配方回放被取消（第{i}步）"
        act = s.get("action")
        if on_step:
            on_step(f"回放 {i}/{total}: {act}"
                    + (f"「{str(s.get('text',''))[:12]}」" if s.get("text") else ""))
        if act in ("uia_click", "dom_click"):
            finder = uia_tools.find_element if act == "uia_click" \
                else dom_tools.find_text
            pos = finder(str(s.get("text", "")), timeout=1.5)
            if not pos:
                return False, f"配方失配：第{i}步找不到「{s.get('text', '')}」"
            if host_input.click(*pos, cancel_check=cancel_check) is False:
                return False, "回放被打断"
        elif act in ("click", "left_double", "right_single", "scroll"):
            x, y = _to_phys(tuple(s.get("point", (0, 0))))
            if act == "click":
                ok = host_input.click(x, y, cancel_check=cancel_check)
            elif act == "left_double":
                ok = host_input.double_click(x, y, cancel_check=cancel_check)
            elif act == "right_single":
                ok = host_input.click(x, y, button="right",
                                      cancel_check=cancel_check)
            else:
                ok = host_input.scroll(x, y, int(float(s.get("delta", -3))),
                                       cancel_check=cancel_check)
            if ok is False:
                return False, "回放被打断"
        elif act == "hotkey":
            keys = [k.strip() for k in str(s.get("keys", "")).split(",") if k.strip()]
            if keys and host_input.hotkey(*keys, cancel_check=cancel_check) is False:
                return False, "回放被打断"
        elif act == "type":
            if host_input.type_unicode(str(s.get("text", "")),
                                       cancel_check=cancel_check) is False:
                return False, "回放被打断"
        elif act == "wait":
            time.sleep(min(int(float(s.get("ms", 500))) / 1000, 3.0))
        elif act == "launch_app":
            # 与录制同一路由（exec_native 能力表+新窗口置顶）；另用一套
            # 启动器=行为分叉（2026-08-31：沉底不抢前台=回放效果看不见）。
            try:
                from plugins import exec_native as ex
                ok_launch = ex.launch_app_by_name(
                    str(s.get("name", "")), log=log) is not None
            except Exception:
                ok_launch = False
            if not ok_launch:
                return False, f"配方失配：启动 {s.get('name', '')} 失败"
        else:
            return False, f"配方含未知动作 {act}"
        time.sleep(0.3)   # 确定性节奏（~0.3s/步，无模型往返）
    return True, "配方回放完成"


def request_stop():
    """外部叫停（语音叫停/会话关闭时调用）。"""
    _STOP.set()


def _valid_execution_context(context):
    return (isinstance(context, dict) and bool(context.get("task_id"))
            and isinstance(context.get("executor_epoch"), int)
            and context.get("executor_epoch") >= 1
            and isinstance(context.get("cancel_token"), str)
            and len(context.get("cancel_token")) >= 16
            and isinstance(context.get("checkpoint", {}), dict)
            and isinstance(context.get("completed_steps", []), list))


def _screen_size():
    """虚拟屏 (宽, 高, 原点x, 原点y)。多显示器时原点可为负。"""
    u32 = ctypes.windll.user32
    return (u32.GetSystemMetrics(78), u32.GetSystemMetrics(79),
            u32.GetSystemMetrics(76), u32.GetSystemMetrics(77))


def _to_phys(point):
    """0-1000 相对坐标 → 虚拟屏物理像素。"""
    w, h, ox, oy = _screen_size()
    if w <= 0 or h <= 0:
        raise ValueError("invalid virtual screen bounds")
    x = int(ox + float(point[0]) * max(0, w - 1) / 1000)
    y = int(oy + float(point[1]) * max(0, h - 1) / 1000)
    x = min(max(x, ox), ox + w - 1)
    y = min(max(y, oy), oy + h - 1)
    if not _point_on_monitor(x, y):
        raise ValueError("mapped point is in a sparse virtual-screen gap")
    return x, y


def _point_on_monitor(x, y):
    point = wintypes.POINT(int(x), int(y))
    u32 = ctypes.windll.user32
    u32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    u32.MonitorFromPoint.restype = wintypes.HANDLE
    return bool(u32.MonitorFromPoint(point, 0))


def _secure_desktop():
    """UAC 安全桌面/锁屏检测：输入桌面不是 Default 即视为不可操作（P3a-0）。"""
    u32 = ctypes.windll.user32
    hdesk = u32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_READOBJECTS
    if not hdesk:
        return True
    buf = ctypes.create_unicode_buffer(256)
    n = wintypes.DWORD(0)
    ok = u32.GetUserObjectInformationW(hdesk, 2, buf, 512, ctypes.byref(n))  # UOI_NAME
    u32.CloseDesktop(hdesk)
    return (not ok) or (buf.value.lower() != "default")


def _black_frame(img):
    """锁屏/RDP 黑图检测：灰度均值接近 0 即黑图（P3a-3）。"""
    g = img.convert("L").resize((64, 36))
    hist = g.histogram()
    mean = sum(i * c for i, c in enumerate(hist)) / (64.0 * 36)
    return mean < 5.0


# 注入打不进的受保护前台进程（uiAccess/高 IL 窗口会静默吞掉注入输入，
# benchmark 游荡 10 步的根因——Taskmgr 前台时 Win 键注入 diff 仅 0.0001）
_PROTECTED_FG = {"taskmgr.exe", "consent.exe", "logonui.exe", "credui.exe"}


def _foreground_blocked():
    """前台窗口是注入打不进的受保护/管理员进程 → (True, 进程名)。"""
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    adv = ctypes.windll.advapi32
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return False, ""
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = k32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
    if not h:
        return False, ""
    try:
        # 进程名
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        name = ""
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            name = buf.value.rsplit("\\", 1)[-1].lower()
        if name in _PROTECTED_FG:
            return True, name
        # 完全管理员令牌（TokenElevationType==2）的前台也打不进
        tok = wintypes.HANDLE()
        if adv.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):  # TOKEN_QUERY
            et = wintypes.DWORD()
            n = wintypes.DWORD(0)
            if adv.GetTokenInformation(tok, 18, ctypes.byref(et), 4, ctypes.byref(n)):
                k32.CloseHandle(tok)
                if et.value == 2:
                    return True, name or f"pid{pid.value}(elevated)"
            else:
                k32.CloseHandle(tok)
        return False, name
    finally:
        k32.CloseHandle(h)


def _gate(actions):
    """动作闸：返回 (放行列表, 被拦原因或 None)。"""
    if not isinstance(actions, list):
        return [], "动作闸拦截：模型动作不是列表"
    ok = []
    for act in actions:
        if not isinstance(act, dict):
            return ok, "动作闸拦截：动作不是结构化对象"
        name = str(act.get("action", "")).strip()
        if name not in _ALLOWED_ACTIONS:
            return ok, f"动作闸拦截：未知动作 {name!r}"
        intent = str(act.get("intent", "")).strip()
        # 动作意图是安全元数据，不是可选的装饰字段；缺失时 fail-closed。
        # 不能让模型通过省略 intent 绕过不可逆动作审计。
        if not intent:
            return ok, f"动作闸拦截：动作 {act.get('action', '')} 缺少结构化意图"
        if _BANNED_INTENT_RE.search(intent):
            return ok, f"动作闸拦截：意图含不可逆操作（{intent}）"
        if name in _POINT_ACTIONS:
            point = act.get("point")
            try:
                valid = (len(point) == 2 and all(
                    math.isfinite(float(v)) and 0 <= float(v) <= 1000
                    for v in point))
            except (TypeError, ValueError):
                valid = False
            if not valid:
                return ok, f"动作闸拦截：{name} 缺少合法的 0-1000 坐标"
        if name == "drag":
            for key in ("start_point", "end_point"):
                point = act.get(key)
                try:
                    valid = (len(point) == 2 and all(
                        math.isfinite(float(v)) and 0 <= float(v) <= 1000
                        for v in point))
                except (TypeError, ValueError):
                    valid = False
                if not valid:
                    return ok, f"动作闸拦截：drag 缺少合法的 {key}"
        if name in ("uia_click", "dom_click") and not str(act.get("text", "")).strip():
            return ok, f"动作闸拦截：{name} 缺少目标文本"
        if name == "launch_app" and not str(act.get("name", "")).strip():
            return ok, "动作闸拦截：launch_app 缺少应用名"
        if name == "open_url":
            url = str(act.get("url", "")).strip()
            valid_url = bool(re.match(r"^(?:https?://|www\.)[^\s]+$", url, re.I))
            valid_url = valid_url or bool(re.match(
                r"^[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}(?:/[^\s]*)?$", url))
            if not valid_url:
                return ok, "动作闸拦截：open_url 缺少合法的网页地址"
        if name == "type" and not str(act.get("text", "")):
            return ok, "动作闸拦截：type 缺少文本"
        if name == "hotkey":
            keys = {k.strip().lower() for k in str(act.get("keys", "")).split(",")}
            if not keys or keys == {""}:
                return ok, "动作闸拦截：hotkey 缺少按键"
            if keys & _BANNED_KEYS:
                return ok, f"动作闸拦截：hotkey 含禁用键 {keys & _BANNED_KEYS}"
        ok.append(act)
    return ok, None


def _sleep_cancellable(seconds, cancel_check=None):
    """将等待切成短片段，及时响应叫停/明确用户接管。"""
    deadline = time.perf_counter() + max(0.0, float(seconds))
    while True:
        if cancel_check and cancel_check():
            return False
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return True
        time.sleep(min(0.05, remaining))


def _completion_supported(final_text, screen_evidence):
    """只有模型总结与独立屏幕变化同时存在时才允许完成。"""
    return bool(str(final_text or "").strip()) and bool(screen_evidence)


_ARTIFACT_TASK_RE = re.compile(r"保存|存到|存进|另存|导出|写到|写入|生成.{0,6}(?:文档|文件)")


def _artifact_check(task, since_wall_ts, _dir=None):
    """产物核验（2026-08-30 实锤：模型 4 步谎报"已存桌面"，实际没存——保存/写入
    类任务的完成必须有落盘文件这个硬证据，屏幕变化不等于文件落盘）。
    返回 None=不适用（非产物类任务）；True=目标位置有新文件；False=没有。"""
    if not _ARTIFACT_TASK_RE.search(task or ""):
        return None
    target = _dir
    if target is None:
        m = re.search(r"[A-Za-z]:[\\/][^，。\"'\s]+", task or "")
        if m:
            target = m.group(0)
        elif "桌面" in (task or ""):
            # 桌面真值走注册表（2026-08-31 开源化：桌面可能在 D 盘/OneDrive）
            try:
                from plugins import exec_native as ex
                cands = [ex._desktop_dir(), os.path.expanduser("~/Desktop")]
            except Exception:
                cands = [os.path.expanduser("~/Desktop")]
            for cand in cands:
                if os.path.isdir(cand):
                    target = cand
                    break
    if not target or not os.path.isdir(target):
        return None
    try:
        for name in os.listdir(target):
            p = os.path.join(target, name)
            if os.path.isfile(p) and os.path.getmtime(p) >= since_wall_ts - 2:
                return True
    except OSError:
        return None
    return False


def _toplevel_titles():
    """当前可见顶层窗口标题集（启动类任务的结果核验用）。"""
    titles = set()
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, lp):
        if ctypes.windll.user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value.strip():
                titles.add(buf.value.strip())
        return True

    ctypes.windll.user32.EnumWindows(EnumProc(cb), 0)
    return titles


_LAUNCH_INTENT_RE = re.compile(r"打开|启动|开一下|开启|launch|open", re.I)

# 窗口状态类操作（2026-08-26）：Win32 窗口状态就是完成证据，替代模型自证。
_WINSTATE_MIN_RE = re.compile(r"最小化|收起到任务栏|最小")
_WINSTATE_SHOW_RE = re.compile(r"恢复|还原|前台|置前|让.{0,10}可见")
_WIN_ALIAS = (("b站", "bilibili"), ("哔哩哔哩", "bilibili"), ("bilibili", "bilibili"),
              ("记事本", "记事本"), ("notepad", "notepad"), ("计算器", "计算器"),
              ("calculator", "calculator"), ("画图", "画图"), ("paint", "paint"),
              ("浏览器", "edge"), ("edge", "edge"), ("chrome", "chrome"),
              ("谷歌浏览器", "chrome"), ("微信", "微信"), ("wechat", "wechat"),
              ("资源管理器", "资源管理器"), ("explorer", "explorer"),
              ("文件管理器", "资源管理器"), ("vscode", "code"),
              ("vs code", "code"), ("终端", "终端"), ("word", "word"),
              ("excel", "excel"), ("powerpoint", "powerpoint"), ("ppt", "powerpoint"))


def _route_exec_actions(actions, history, log, capture=None):
    """exec 通道动作（launch_app/open_app）→ exec_native 秒启动（别名/开始
    菜单 .lnk，与语音快路同一张能力表）。返回 (剩余 gui 动作, 启动数)。
    启动不是键鼠注入：路由后下一步重截屏，让大脑亲眼确认窗口出现再收工。
    capture 给定时把成功启动也录进配方（2026-08-31 实锤：不进 execute()
    的动作不补录=纯启动类任务配方恒空，回放永远学不会）。"""
    rest, launched, missed = [], [], []
    for a in actions or []:
        if str(a.get("action", "")) not in ("launch_app", "open_app"):
            rest.append(a)
            continue
        name = str(a.get("name") or a.get("app") or a.get("query") or "").strip()
        r = None
        if name:
            try:
                from plugins import exec_native as ex
                r = ex.launch_app_by_name(name, log=log, verify_window=True)
            except Exception as e:
                log(f"[gui_agent] launch_app 执行异常: {e}")
        (launched if r is not None else missed).append(name or "未给名字")
        if r is not None and capture is not None:
            capture.append(_norm_step(a))
    if launched or missed:
        note = ""
        if launched:
            note += "已启动：" + "、".join(launched) + "。"
        if missed:
            note += "没找到应用：" + "、".join(missed) + "（试试开始菜单里的名字）。"
        note += "下一步重新观察屏幕，确认目标窗口出现后再继续。"
        history.append(("系统", note))
        log(f"[gui_agent] launch_app 路由: {note[:60]}")
    return rest, len(launched)


def _window_state_check(task):
    """窗口状态类任务的确定性核验。返回 "ok"/"no"/None。
    None=非窗口状态任务，或按别名找不到目标窗口（走原视觉链路）。
    最小化=IsIconic；恢复前台=目标窗口是前台窗口。"""
    want_show = bool(_WINSTATE_SHOW_RE.search(task))
    # "从最小化状态恢复到前台"这类说法里"最小化"是描述旧状态——恢复/前台
    # 动词优先，二者同现按恢复前台算。
    want_min = bool(_WINSTATE_MIN_RE.search(task)) and not want_show
    if not (want_min or want_show):
        return None
    low = task.lower()
    keys = [v for k, v in _WIN_ALIAS if k in low]
    if not keys:
        return None
    user32 = ctypes.windll.user32
    target = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, lp):
        if not target and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            t = buf.value
            if t and any(k in t.lower() for k in keys):
                target.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    if not target:
        return None
    hwnd = target[0]
    if want_min:
        return "ok" if user32.IsIconic(hwnd) else "no"
    return "ok" if user32.GetForegroundWindow() == hwnd else "no"


def execute(actions, log=print, cancel_check=None):
    """执行一批动作。返回 (是否全部执行完, uia_misses)。
    uia_misses: uia_click 找不到元素的名字列表——调用方应提示模型回退视觉。"""
    uia_misses = []
    for act in actions:
        if cancel_check and cancel_check():
            log("[gui_agent] 动作批次在下一动作前取消")
            return False, uia_misses
        name = act["action"]
        try:
            if name == "click":
                x, y = _to_phys(act["point"])
                if host_input.click(x, y, cancel_check=cancel_check) is False:
                    return False, uia_misses
            elif name == "uia_click":
                target = _clean_uia_text(act.get("text", ""))
                pos = uia_tools.find_element(target, timeout=2.5) if target else None
                if pos:
                    log(f"[gui_agent] UIA 定位命中 {target!r} -> {pos}")
                    if host_input.click(*pos, cancel_check=cancel_check) is False:
                        return False, uia_misses
                else:
                    log(f"[gui_agent] UIA 找不到 {target!r}，本步跳过")
                    uia_misses.append(target)
            elif name == "dom_click":
                target = str(act.get("text", ""))
                pos = dom_tools.find_text(target, timeout=2.5) if target else None
                if pos:
                    log(f"[gui_agent] DOM 定位命中 {target!r} -> {pos}")
                    if host_input.click(*pos, cancel_check=cancel_check) is False:
                        return False, uia_misses
                else:
                    log(f"[gui_agent] DOM 找不到 {target!r}，本步跳过")
                    uia_misses.append(target + "(网页)")
            elif name == "open_url":
                url = str(act.get("url", ""))
                ok_open = dom_tools.open_url(url) if url else False
                log(f"[gui_agent] open_url {url!r} -> {ok_open}")
                if not ok_open:
                    uia_misses.append(url + "(网址)")
            elif name == "left_double":
                x, y = _to_phys(act["point"])
                if host_input.double_click(x, y, cancel_check=cancel_check) is False:
                    return False, uia_misses
            elif name == "right_single":
                x, y = _to_phys(act["point"])
                if host_input.click(x, y, button="right",
                                    cancel_check=cancel_check) is False:
                    return False, uia_misses
            elif name == "drag":
                x1, y1 = _to_phys(act["start_point"])
                x2, y2 = _to_phys(act["end_point"])
                if host_input.drag(x1, y1, x2, y2,
                                   cancel_check=cancel_check) is False:
                    return False, uia_misses
            elif name == "scroll":
                x, y = _to_phys(act["point"])
                if host_input.scroll(x, y, int(float(act.get("delta", -3))),
                                     cancel_check=cancel_check) is False:
                    return False, uia_misses
            elif name == "type":
                if host_input.type_unicode(str(act.get("text", "")),
                                           cancel_check=cancel_check) is False:
                    return False, uia_misses
            elif name == "hotkey":
                keys = [k.strip() for k in str(act.get("keys", "")).split(",") if k.strip()]
                if keys:
                    if host_input.hotkey(*keys, cancel_check=cancel_check) is False:
                        return False, uia_misses
            elif name == "wait":
                if not _sleep_cancellable(
                        min(int(float(act.get("ms", 1000))) / 1000, 10),
                        cancel_check):
                    return False, uia_misses
            elif name == "launch_app":
                app = str(act.get("name", "")).strip()
                ok_launch, detail = host_input.launch_app(app)
                log(f"[gui_agent] launch_app {app!r} → {detail}")
                if not ok_launch:
                    uia_misses.append(app)   # 提示模型回退开始菜单路径
            else:
                log(f"[gui_agent] 未知动作跳过: {name}")
        except Exception as e:
            log(f"[gui_agent] 动作执行失败 {name}: {e}")
            return False, uia_misses
        # hotkey 后等 UI 稳定。Win 键开始菜单从"固定等待"升级为"UIA 确认就绪"：
        # 搜索编辑框出现才放行后续 type（0.9s 固定等待在高负载下仍会落空，
        # 2026-08-21 微信探针实测）；UIA 不可用时回退固定等待。
        if name == "hotkey":
            keys_now = str(act.get("keys", "")).lower()
            if "win" in keys_now:
                ready = False
                try:
                    ready = bool(uia_tools.find_element(
                        "搜索", control_type="edit", timeout=2.0))
                except Exception:
                    ready = False
                if not ready and not _sleep_cancellable(0.9, cancel_check):
                    return False, uia_misses
            elif not _sleep_cancellable(0.5, cancel_check):
                return False, uia_misses
        else:
            if not _sleep_cancellable(0.15, cancel_check):
                return False, uia_misses
        if cancel_check and cancel_check():
            log("[gui_agent] 动作批次在动作后取消")
            return False, uia_misses
    return True, uia_misses


def run(task, max_steps=_MAX_STEPS, log=print, on_step=None, watch_takeover=True,
        dump_dir=None, resume_context=None, on_interaction=None,
        on_step_result=None, execution_context=None, cancel_check=None,
        on_confirm=None):
    """执行一个 GUI 任务。返回 (成功与否, 最终说明, 步数)。
    状态说明里 "用户接管" 表示执行中检测到真实输入、主动暂停（P3c）。
    dump_dir：逐步截图落盘目录（诊断用，看模型到底看到了什么）。
    on_confirm(scope_text)->bool：GUI 危险意图的中央确认回调（§9.1 等价迁移；
    不提供时保持旧行为——危险意图硬拦）。"""
    _raw_log = log
    _run_t0 = time.perf_counter()
    _run_wall_t0 = time.time()   # 产物核验用墙钟（文件 mtime 是墙钟）
    windows_before = _toplevel_titles()   # 启动类任务的结果核验基线
    def log(msg):
        # GBK 控制台打印不了特殊字符（如实况焦点名里的 ◐）时降级替换，
        # 日志丢精度但执行流程不被打印异常打断。
        try:
            _raw_log(msg)
        except UnicodeEncodeError:
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            _raw_log(str(msg).encode(enc, "replace").decode(enc, "replace"))
    if execution_context is not None:
        if not _valid_execution_context(execution_context) or cancel_check is None:
            return False, "缺少有效任务执行租约，已安全暂停", 0
        if (resume_context and resume_context.get("task_id")
                and resume_context.get("task_id") != execution_context["task_id"]):
            return False, "恢复上下文与当前任务身份不匹配，已安全暂停", 0
    history = []
    if resume_context:
        checkpoint = resume_context.get("checkpoint", resume_context)
        history.append(("系统恢复状态", (
            "这是同一任务的恢复执行，不要把它当成新任务。"
            f"上次模式={resume_context.get('mode', 'unknown')}，"
            f"上次步骤={checkpoint.get('step', '')}，"
            f"当前步骤={resume_context.get('current_step', '')}。"
            f"已有证据的完成步骤={(resume_context.get('completed_steps') or [])[-8:]}。"
            "先重新观察当前屏幕，再从检查点之后继续；不要重复已确认完成的动作。"
        )))
        interaction = checkpoint.get("human_interaction")
        if interaction:
            history.append(("用户协作状态", (
                f"上次执行中记录到用户交互类型：{interaction}。"
                "先重新观察光标和界面，不要假设界面仍停留在上一步。"
            )))
    if not resume_context:
        # 配方回放（2026-08-31 阶段 C，图节点类型②）：同任务有可回放配方
        # → 确定性逐步执行（~0.3s/步，零模型往返）；失配=回退 VLM 循环并
        # 重新沉淀。录制自"完成证据通过"的序列，回放沿用同等可信度。
        recipe_v2 = _match_recipe_v2(task)
        if recipe_v2:
            log(f"[gui_agent] 配方回放: {recipe_v2.get('task', '')[:40]!r}"
                f"（{len(recipe_v2.get('steps', []))} 步）")
            ok, msg = _replay_steps(recipe_v2.get("steps", []), log=log,
                                    on_step=on_step, cancel_check=cancel_check)
            if ok:
                _record_recipe(task, recipe_v2.get("summary", ""),
                               time.perf_counter() - _run_t0,
                               steps=recipe_v2.get("steps"))
                return True, msg, len(recipe_v2.get("steps", []))
            log(f"[gui_agent] 配方失配，回退 VLM 循环: {msg}")
            history.append(("系统提示", (
                f"上次成功的路径这次走不通（{str(msg)[:60]}）——"
                "别照抄老路，按当前屏幕重新来。")))
        # 轨迹配方（P4）：同类任务上次被验证成功的路径注入参考——
        # 治"同一任务这次会上次不会"的方差（记事本多开循环类 flake）。
        recipe = _match_recipe(task)
        if recipe:
            history.append(("成功配方参考", (
                f"同类任务「{recipe['task'][:30]}」上次成功路径（仅供参考，"
                f"以当前屏幕为准）：{recipe['summary']}")))
    thinking = False
    stall = 0  # 连续无进展计数（动作重复或屏幕无变化）
    no_change_streak = 0  # 连续"有动作但屏幕零变化"计数（环境异常硬止损用）
    _recipe_steps = []   # 执行成功的规范化动作序列（阶段 C 配方录制原料）
    _sig_window = deque(maxlen=3)  # 近 3 步动作签名+有无变化（假进展死循环检测）
    _last_uia = None  # (上批具名目标 key, 有无变化)——语义停滞 2 击检测
    _click_points = deque(maxlen=6)  # 近 6 个点击点（同点聚类=物理层死磕检测）
    _loop_nudges = 0   # 换路提示计数：提示没人听 → 2 次升级为止损如实说
    _hung_strikes = 0  # 前台挂死连击（幽灵窗实锤后：死窗不值得再点）
    task_approved = set()  # auto 档任务级授权记忆（任务结束即失效，不持久化）
    performed_effects = set()  # 外部触达 exactly-once：做过后先核验，禁止盲点第二次
    last_sig = None
    last_action_evidence = False
    launch_evidence = False
    watcher = _get_watcher() if watch_takeover else None
    if watcher is not None and hasattr(watcher, "healthy") and not watcher.healthy():
        return False, "输入监控未完整就绪，已安全暂停", 0
    last_consumed_seq = watcher.cursor() if watcher and hasattr(watcher, "cursor") else 0
    legacy_input_t0 = time.perf_counter()
    _STOP.clear()
    if dump_dir:
        os.makedirs(dump_dir, exist_ok=True)

    def _metrics(rec):
        """P4-1 全链路计时打点：每步截屏/推理/执行分解落盘。"""
        try:
            import json as _json
            rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            with open(_METRICS_PATH, "a", encoding="utf-8") as f:
                f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    for step in range(1, max_steps + 1):
        if _STOP.is_set() or (cancel_check and cancel_check()):
            log("[gui_agent] 收到叫停，停止")
            return False, "已叫停", step
        # 窗口状态类任务（最小化/恢复前台）：Win32 状态已达成=直接收工，
        # 不再等模型自证多跑步骤（2026-08-26 B站最小化实锤：操作成功后
        # 又空转 4 步被"无可验证证据"误拒，听感=看不到页面）。
        if step > 1 and _window_state_check(task) == "ok":
            log(f"[gui_agent] 窗口状态已达成，直接收工({step}步)")
            return True, f"已确认窗口状态达成（{task[:60]}）", step
        # P3a-0/3：UAC 安全桌面、锁屏/RDP 黑图 → 立即停
        if _secure_desktop():
            log("[gui_agent] 检测到 UAC 安全桌面/锁屏，暂停（需要用户亲自处理）")
            return False, "UAC 安全桌面或锁屏，已暂停——请亲自处理后让我继续", step
        try:
            step_origin = host_input.cursor_pos()
        except Exception:
            step_origin = None
        t_shot0 = time.perf_counter()
        try:
            img = host_input.screenshot()
        except Exception as e:  # 含 PrivacyBlocked（P-1-2）
            log(f"[gui_agent] 截图被阻止: {e}")
            return False, str(e), step
        t_shot = time.perf_counter() - t_shot0
        if dump_dir:
            try:
                img.convert("RGB").save(
                    os.path.join(dump_dir, f"step_{step:02d}.jpg"), quality=70)
            except OSError:
                pass
        if _black_frame(img):
            log("[gui_agent] 截图为黑图（锁屏/RDP 断开？），停止")
            return False, "截图黑图（锁屏或远程断开），已停止", step
        pending_cooperation = False

        def _consume_real_input():
            nonlocal last_consumed_seq, legacy_input_t0, pending_cooperation
            if watcher is None:
                return None
            if hasattr(watcher, "consume_since"):
                interaction, last_consumed_seq = watcher.consume_since(
                    last_consumed_seq, origin=step_origin)
                if interaction == "cooperate":
                    pending_cooperation = True
                return interaction
            interaction = watcher.interaction_since(
                legacy_input_t0, origin=step_origin)
            legacy_input_t0 = time.perf_counter()
            if interaction == "cooperate":
                pending_cooperation = True
            return interaction

        def _pause_text(interaction, where):
            """B6：钩子死亡如实暂停。物理接管不再暂停（2026-08-26 用户裁决：
            像远程同事协作——用户动鼠标=往前搭把手，任务继续；只有明确
            说停才停）。接管只记录，下一步重新观察屏幕自然吸收用户的改动。"""
            if interaction == "takeover":
                log(f"[gui_agent] 用户物理输入（{where}）——不打断，继续任务")
                if watcher is not None:
                    history.append(("用户协作", "用户在此期间操作了鼠标键盘；"
                                    "下一步重新观察屏幕，基于最新画面继续"))
                return None
            if interaction == "hook_dead":
                return "输入监听已断开（接管检测失效，不是你的操作），为安全已暂停"
            return None

        interaction = _consume_real_input()
        if _pause_text(interaction, "截图期间"):
            return False, _pause_text(interaction, "截图期间"), step
        # 语音纠正注入（用户裁决"语音即纠正"）：用户运行中说的话必须被
        # 下一步吸收；用户的真实点击坐标一并注入（物理操作是意图表达）
        for _h in _drain_user_hints():
            history.append(("用户纠正", f"用户刚才说：{_h}。下一步必须按这个意图调整。"))
        try:
            if watcher is not None:
                _lc = watcher.last_real_click()
                if _lc and _lc[2] < 6.0:
                    history.append(("用户协作",
                                    f"用户 {_lc[2]:.0f}s 前在屏幕 ({_lc[0]},{_lc[1]}) "
                                    "点击过——结合最新画面理解他的意图，"
                                    "不要和他抢点同一位置。"))
        except Exception:
            pass
        # 语义接地（2026-08-29 路线A）：把前台可交互控件清单喂给模型——
        # 按名点（uia_click）优先于像素猜坐标。槽位替换：历史只留最新一份，
        # 防上下文随步数膨胀；UIA 覆盖不了的界面返回 None=纯视觉照旧。
        try:
            _uia_dump = uia_tools.dump_interactive()
        except Exception:
            _uia_dump = None
        history[:] = [h for h in history if h[0] != "界面控件"]
        if _uia_dump:
            history.append(("界面控件", _uia_dump))
        try:
            actions, final_text, reasoning, latency = gui_brain.think(
                task, img, history=history, thinking=thinking)
        except Exception as e:
            log(f"[gui_agent] 大脑调用失败: {e}")
            # 过嘴的话不许带内部术语（模型/provider/动作名）——人格铁律
            return False, "这步没做成，我换个方式再来", step

        interaction = _consume_real_input()
        if _pause_text(interaction, "推理期间"):
            return False, _pause_text(interaction, "推理期间"), step

        # exec 通道动作（launch_app/open_app）：不进键鼠注入，直连秒启动
        # （2026-08-27 实锤：提示词教模型用 launch_app，契约却不认=整单报死）
        actions, n_launched = _route_exec_actions(actions, history, log,
                                                  capture=_recipe_steps)
        if n_launched:
            # launch_app 是 exec 语义动作，不经过下方像素动作证据计算；但启动
            # 成功本身是证据，最终仍由“出现新顶层窗口”二次核验，避免假完成。
            launch_evidence = True

        if final_text is not None and not actions:
            wst = _window_state_check(task)
            if wst == "ok":
                log(f"[gui_agent] 窗口状态核验通过({step}步)")
                seq = [h[1].replace("已执行: ", "")
                       for h in history if h[0].startswith("步骤")]
                _record_recipe(task, " → ".join(seq[-12:]),
                               time.perf_counter() - _run_t0,
                               steps=_recipe_steps[-16:])
                return True, final_text, step
            if wst == "no":
                log("[gui_agent] 窗口状态未达目标，驳回继续")
                history.append(("系统驳回", (
                    "目标窗口还没到要求的状态（最小化/前台）。"
                    "继续执行，状态达成才算完成。")))
                continue
            if _completion_supported(final_text, last_action_evidence or launch_evidence):
                # 产物核验（2026-08-30 实锤：模型 4 步谎报"已存桌面"，屏幕有变化
                # 但文件没落盘）：保存/写入类任务必须目标位置出现新文件才算完成。
                art = _artifact_check(task, _run_wall_t0)
                if art is False:
                    log("[gui_agent] 保存类任务无落盘新文件，驳回继续")
                    history.append(("系统驳回", (
                        "你说完成了，但目标位置没有出现新文件——保存/写入没有真正"
                        "落盘。继续执行：用应用的保存功能（Ctrl+S 或菜单）把文件"
                        "真正存到目标位置，文件出现才算完成。")))
                    continue
                # 启动类任务加一道结果核验：必须有新顶层窗口出现——
                # "菜单开了但没回车启动就报备完成"是实测到的提前收工形态                # （2026-08-21 微信验收：搜索高亮了微信但没启动就说做完了）。
                if _LAUNCH_INTENT_RE.search(task) and not (
                        _toplevel_titles() - windows_before):
                    log("[gui_agent] 启动类报备无新窗口，驳回继续")
                    history.append(("系统驳回", (
                        "你说完成了，但没有任何新窗口打开——目标应用还没起来。"
                        "继续：把目标选中后按回车（或双击）真正启动它，"
                        "看到它的窗口出现才算完成。")))
                    last_action_evidence = False
                    continue
                log(f"[gui_agent] 完成证据通过({step}步): {final_text[:120]}")
                seq = [h[1].replace("已执行: ", "")
                       for h in history if h[0].startswith("步骤")]
                _record_recipe(task, " → ".join(seq[-12:]),
                               time.perf_counter() - _run_t0,
                               steps=_recipe_steps[-16:])
                return True, final_text, step
            # 模型的文字总结不是独立完成证据；没有屏幕变化就安全停下。
            log(f"[gui_agent] 模型报告待验证({step}步): {final_text[:120]}")
            return False, "说是做完了，但我没能核实结果", step
        if not actions:
            # 空动作既不是完成，也不是可执行的等待；不能让空响应循环到“成功”。
            # 例外：本步已执行过 launch_app——重截屏让下一步亲眼确认。
            if n_launched:
                continue
            return False, "这步没拿到可执行的操作指令", step

        # 结构校验（动作名/坐标/hotkey 禁用键等机械层）用意图替身硬拦；
        # 意图安全判定走双层：关键词召回 → 模型级裁决（§3.3-2）。
        # 关键词命中的边界说法（如"清空搜索框"）由模型裁决纠正，不误拦；
        # 模型判不了时 fail-closed 按危险处理。
        structural, blocked = _gate(
            [dict(a, intent="结构校验占位") for a in actions])
        if blocked:
            log(f"[gui_agent] {blocked}")
            return False, blocked, step
        import intent_judge
        kw_hit = any(
            _BANNED_INTENT_RE.search(str(a.get("intent", ""))) for a in actions)
        if kw_hit or on_confirm is not None:
            # 有确认链时每步全量判（换说法兜底的既定设计）；
            # 无确认链时只在关键词命中后才判，离线/无凭据不误伤日常动作。
            flags = intent_judge.judge_intents(
                [a.get("intent", "") for a in actions])
            danger = [a for a, f in zip(actions, flags) if f]
        else:
            danger = []
        cleared = [a for a in actions
                   if a not in danger
                   and _BANNED_INTENT_RE.search(str(a.get("intent", "")))]
        if cleared:
            log(f"[gui_agent] 关键词命中但模型裁决安全，放行: "
                + "；".join(str(a.get("intent", ""))[:30] for a in cleared))
        pmode = "confirm"
        if danger:
            # 权限三档（2026-08-28 设置面板落地，permission_mode 现读现用）：
            # yolo 最大自主=只有模型判为不可恢复的数据删除才问；其余全放行。
            # auto 自动通过=本任务内批准过一次危险意图后不再重问。
            import permission_mode
            pmode = permission_mode.current()
            danger = _confirmation_actions(
                danger, pmode, task_approved, task_text=task, log=log)
        if danger:
            scope = "；".join(
                f"{a['action']}（{str(a.get('intent',''))[:30]}）" for a in danger)
            if on_confirm is not None:
                # §9.1 等价迁移：危险意图动作走中央确认链（语音裁决）。
                log(f"[gui_agent] 危险意图 {len(danger)} 个，交中央确认链: {scope}")
                if not on_confirm(scope):
                    return False, "危险操作未获批准或确认不可用，已暂停", step
                log("[gui_agent] 用户已批准危险操作，放行")
                if pmode == "auto":
                    task_approved.add("intent")
            else:
                # 无确认链（benchmark/后台）→ fail-closed 硬拦。
                log(f"[gui_agent] 危险意图且确认链不可用，fail-closed 拦截: {scope}")
                return False, f"动作闸拦截：意图含不可逆操作（{scope}）", step
        allowed = structural
        allowed, repeated_send = _dedupe_external_send_actions(
            allowed, performed_effects)
        if repeated_send:
            history.append(("系统提示", (
                "这项外部发送已经执行过一次。禁止再次点击发送，先观察聊天记录/"
                "成功提示核实结果；已发出就直接完成，未发出才可换路。")))
            log("[gui_agent] 外部发送已执行过，拦截重复发送并要求先核验")
            if not allowed:
                continue

        sig = tuple((a["action"], str(a.get("point", a.get("text", "")))) for a in allowed)
        stall = stall + 1 if sig == last_sig else 0
        last_sig = sig

        log(f"[gui_agent] 步骤{step} ({latency:.1f}s): "
            + " | ".join(f"{a['action']}({a.get('intent','')[:20]})" for a in allowed)
            + (f"  <<{reasoning[:80]}>>" if reasoning else ""))
        if on_step:
            on_step(step, allowed)
        # 进度浮层更新后重取动作基线，避免把助手自身 UI 变化当成完成证据。
        try:
            action_baseline = host_input.screenshot()
        except Exception:
            action_baseline = img
        focus_before = uia_tools.focused_element()
        interaction = _consume_real_input()
        if _pause_text(interaction, "动作前"):
            return False, _pause_text(interaction, "动作前"), step
        t_exec0 = time.perf_counter()
        def _cancel_batch():
            # 只有明确叫停才中断注入批次（2026-08-26 用户裁决：物理输入
            # 不打断任务）；用户真在动的瞬间我们也不再抢注事件，交下一步
            # 重新观察吸收。钩子死亡也不抢停——如实报备走主循环判定。
            return _STOP.is_set() or (cancel_check and cancel_check())

        exec_ok, uia_misses = execute(allowed, log, cancel_check=_cancel_batch)
        if exec_ok:
            for _a in allowed:
                _recipe_steps.append(_norm_step(_a))   # 配方捕获（阶段 C）
        if not exec_ok:
            if _STOP.is_set():
                return False, "已叫停", step
            if watcher:
                intr = _consume_real_input()
                if _pause_text(intr, "动作中"):
                    return False, _pause_text(intr, "动作中"), step
            return False, "动作执行中断", step
        if not uia_misses and any(_is_external_send_action(a) for a in allowed):
            performed_effects.add("external-send")
            history.append(("系统事实", "外部发送动作已经执行一次；下一步只核验，禁止重复发送。"))
        t_exec = time.perf_counter() - t_exec0
        _metrics({"step": step, "t_screenshot": round(t_shot, 3),
                  "t_model": round(latency, 3), "t_execute": round(t_exec, 3),
                  "thinking": thinking, "actions": [a["action"] for a in allowed]})
        if uia_misses:
            # 告诉模型这些元素 UIA 找不到，下步改用视觉坐标 click
            history.append(("系统提示",
                            f"UIA 找不到元素：{'、'.join(uia_misses)}。"
                            "下一步请用视觉坐标 click，不要再对这些元素用 uia_click。"))

        time.sleep(0.6)  # 等 UI 稳定再截下一帧

        # 协作检测（2026-08-26 裁决后）：轻微移动和明确输入都不再交还控制权，
        # 只把"用户动过"记进历史，下一步重新观察屏幕吸收用户的改动。
        if watcher:
            interaction = _consume_real_input()
            if interaction == "hook_dead":
                log("[gui_agent] 暂停（hook_dead）")
                if on_interaction:
                    on_interaction(interaction)
                return False, _pause_text(interaction, "执行后"), step
            elif interaction == "takeover" or pending_cooperation:
                log("[gui_agent] 检测到用户物理输入，不打断——下一步重新观察屏幕")
                history.append(("用户协作", "用户在此期间操作了鼠标键盘；"
                                "下一步重新观察屏幕，基于最新画面继续"))
                if on_interaction:
                    on_interaction("cooperate")
                pending_cooperation = False

        # 事件校验：动作前后屏幕无变化且本步含点击/按键 → 记一次无进展
        try:
            post = host_input.screenshot()
        except Exception:
            post = img  # 截屏被阻止时按无变化处理
        diff_ratio = _diff_ratio(action_baseline, post)
        if diff_ratio < 0.001 and any(
                a["action"] in ("click", "left_double", "right_single",
                                "uia_click", "dom_click") for a in allowed):
            # 事件校验 UIA 化（P3 遗留 3）：像素无变化时查焦点是否转移——
            # 点已聚焦文本框这类动作，焦点/光标变化才是真证据
            focus_after = uia_tools.focused_element()
            if (focus_before is not None and focus_after is not None
                    and (focus_before.get("rect") != focus_after.get("rect")
                         or focus_before.get("type") != focus_after.get("type")
                         or focus_before.get("name") != focus_after.get("name"))):
                log(f"[gui_agent] 事件校验(UIA)：焦点已转移 "
                    f"{focus_before.get('name','')[:12]}→{focus_after.get('name','')[:12]}，"
                    "像素差豁免")
                diff_ratio = 0.01  # 焦点转移也算屏幕发生了变化
        # 假进展死循环注入（2026-08-29 实锤：连续 8 步"关菜单设默认"措辞
        # 微变绕过同签名 stall——相似意图+无实质变化 → 强注入换路指令）
        _sig = ";".join(f"{a.get('action', '')}:"
                        f"{str(a.get('intent', ''))[:24]}" for a in allowed)
        _sig_window.append((_sig, diff_ratio >= 0.001))
        if _similar_stall(_sig_window):
            history.append(("系统提示",
                            "你已连续 3 轮做几乎相同的操作且屏幕没有实质变化——"
                            "这条路已证明不通。必须换一种方法（换入口/用搜索/"
                            "用快捷键），或如实告诉用户卡在哪。禁止再重复同样的点击。"))
            log("[gui_agent] 相似意图连续 3 轮无进展，已注入换路指令")
            _sig_window.clear()   # 注入后重置，防每步重复注入
            _loop_nudges += 1
        # 语义停滞：同一具名目标两击零反应，立即换路（不等通用 3 轮）
        _uia_key = _uia_key_of(allowed)
        if _uia_stall(_last_uia, _uia_key, diff_ratio >= 0.001):
            history.append(("系统提示",
                            f"「{'、'.join(_uia_key)}」连续点了两次都没反应——"
                            "不要再点它。换一种方法（换入口/用搜索/用快捷键），"
                            "或如实告诉用户卡在哪。"))
            log(f"[gui_agent] 同一具名目标两击无效（{_uia_key}），已注入换路指令")
            _loop_nudges += 1
        _last_uia = (_uia_key, diff_ratio >= 0.001) if _uia_key else None
        # 同点聚类（物理层）：菜单开合骗过像素差时的兜底网
        for _a in allowed:
            if _a.get("action") in _POINT_ACTIONS and isinstance(
                    _a.get("point"), (tuple, list)) and len(_a["point"]) == 2:
                _click_points.append((float(_a["point"][0]), float(_a["point"][1])))
        if _same_spot(_click_points):
            history.append(("系统提示",
                            "你一直在屏幕上同一个位置反复点击，全都没效果——"
                            "那个位置没有你要的东西。换一种方法，或如实告诉用户卡在哪。"))
            log("[gui_agent] 同点聚类死磕（近 4 击半径 40px 内），已注入换路指令")
            _click_points.clear()
            _loop_nudges += 1
        # 挂死检测（幽灵窗实锤）：前台无响应的窗口不值得再点一下
        if _foreground_hung():
            _hung_strikes += 1
            log(f"[gui_agent] 前台窗口无响应（连击 {_hung_strikes}）")
            if _hung_strikes >= 2:
                return False, ("当前这个窗口已经卡死了（系统判定无响应）——它不是不想理我，"
                               "是死了，我点多少次都不会有用。请把它关掉重新打开，"
                               "然后叫我一声，我马上接着做"), step
        else:
            _hung_strikes = 0
        # 步数压力：步数膨胀本身就是信号（20:15 空转 30 步实锤）
        if step in (10, 16):
            history.append(("系统提示",
                            f"已经 {step} 步了还没完成——停下来重新想：目标到底在哪里？"
                            "换一条完全不同的路，或者如实告诉用户卡在哪。"
                            "不要再用刚才反复试过的老办法。"))
        # 换路提示的牙齿：提示没人听，机械止损如实说（卡住必说话）
        if diff_ratio >= 0.001:
            _loop_nudges = 0   # 屏幕真变了=循环已破，重新计数
        if _loop_nudges >= 2:
            log("[gui_agent] 多次换路提示无效，止损如实报告")
            return False, ("这个操作我换了几种方法都在原地打转，继续做也是浪费——"
                           "界面没有正常响应我的操作。你可以手动处理一下，"
                           "或者告诉我换个思路，我再接着来"), step
        evidence_actions = ("click", "left_double", "right_single", "hotkey",
                            "uia_click", "dom_click", "open_url", "type", "drag",
                            "scroll")
        step_has_evidence_action = any(
            a["action"] in evidence_actions for a in allowed)
        step_evidence = bool(diff_ratio >= 0.001 and step_has_evidence_action)
        # wait/观察步不清除上一有效动作证据；它只代表本步没有新增证据。
        if step_has_evidence_action:
            last_action_evidence = step_evidence
        if on_step_result:
            on_step_result(step, allowed, step_evidence, diff_ratio)
        if diff_ratio < 0.001 and any(
                a["action"] in evidence_actions for a in allowed):
            stall += 1
            no_change_streak += 1
            log(f"[gui_agent] 事件校验：屏幕无变化（连续 {stall} 次）")
        elif diff_ratio >= 0.001:
            no_change_streak = 0

        if stall >= 2 and not thinking:
            # API 正慢时升 thinking = 慢上慢（2026-08-30 实锤 47-66s/步把任务拖死）；
            # 保持快档先按换路提示走，latency 回落再说。
            if latency > 20:
                log(f"[gui_agent] 连续无进展但模型响应慢（{latency:.0f}s），暂不升 thinking")
            else:
                thinking = True
                log("[gui_agent] 连续无进展，开启 thinking 模式")
        if stall >= 3:
            # 连续无进展时查前台是否受保护窗口（Taskmgr/管理员——注入被系统吞）
            blocked, pname = _foreground_blocked()
            if blocked:
                log(f"[gui_agent] 前台是受保护窗口 {pname}，注入打不进，停止")
                return False, (f"前台窗口「{pname}」是管理员/受保护窗口，"
                               "我的操作进不去——请先关掉它或手动处理"), step
        if no_change_streak >= 5:
            # 硬止损：连续 5 个动作零屏幕变化（2026-08-21 explorer 输入楔死事故，
            # 空磨 10 步 × thinking 调用 ≈ 每条多烧 4 分钟和 token）——
            # 环境已不可信，停下如实报告，不继续盲试。
            log(f"[gui_agent] 连续 {no_change_streak} 个动作零效果，环境异常，硬止损")
            return False, ("连续 5 步操作后屏幕毫无变化——疑似系统输入被吞或界面卡死，"
                           "已安全停止。动一下真实键鼠或重启资源管理器后再让我试"), step

        history.append((f"步骤{step}截图", f"已执行: {[a['action'] for a in allowed]}"))

    return False, f"超过最大步数 {max_steps}", max_steps


if __name__ == "__main__":
    import sys
    ok, msg, steps = run(" ".join(sys.argv[1:]) or "把鼠标移动到屏幕中央附近")
    print("RESULT:", ok, "|", msg, "| steps:", steps)
