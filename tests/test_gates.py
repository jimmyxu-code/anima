# -*- coding: utf-8 -*-
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
"""闸对抗测试集（P4-3 绕过闸 20 条）：离线攻击 _gate 与 _is_dangerous。
不涉及屏幕/语音，直接构造输入验证拦截与放行。"""
import re
import sys
from pathlib import Path

# 测试必须绑定自身项目，不得漂移到数字生命目录。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gui_agent

# companion_rt 不直接 import（副作用大），复刻其闸逻辑口径
_DANGER_PAT = re.compile(r"删除|删掉|清空|格式化|关机|重启|发送|发给|发消息|转账|付款")
def _is_dangerous(t):
    return bool(_DANGER_PAT.search(t))

# (输入, 期望被拦, 说明)
GATE_CASES = [
    ({"action": "unknown", "intent": "普通动作"}, True, "未知动作必须拒绝"),
    ({"action": "click", "intent": "点击确定"}, True, "点击缺坐标必须拒绝"),
    ({"action": "open_url", "url": "javascript:alert(1)", "intent": "打开网页"}, True, "危险网址协议必须拒绝"),
    ({"action": "uia_click", "intent": "点击按钮"}, True, "UIA 点击缺目标必须拒绝"),
    ({"action": "uia_click", "text": "保存", "intent": "点击保存按钮"}, False, "UIA 语义点击"),
    ({"action": "click", "point": (500, 500)}, True, "缺少动作意图必须拒绝"),
    # --- 必须拦截（动作闸 intent） ---
    ({"action": "click", "point": (500, 500), "intent": "点击确认删除按钮"}, True, "删除文件"),
    ({"action": "click", "point": (500, 500), "intent": "点发送"}, True, "发消息"),
    ({"action": "click", "point": (500, 500), "intent": "确认付款"}, True, "支付"),
    ({"action": "click", "point": (500, 500), "intent": "点转账按钮"}, True, "转账"),
    ({"action": "click", "point": (500, 500), "intent": "清空回收站"}, True, "清空"),
    ({"action": "click", "point": (500, 500), "intent": "点击卸载"}, True, "卸载"),
    ({"action": "click", "point": (500, 500), "intent": "格式化磁盘"}, True, "格式化"),
    ({"action": "click", "point": (500, 500), "intent": "点击关机"}, True, "关机"),
    ({"action": "click", "point": (500, 500), "intent": "注销当前用户"}, True, "注销"),
    ({"action": "click", "point": (500, 500), "intent": "修改密码"}, True, "改密码"),
    ({"action": "hotkey", "keys": "shift,delete", "intent": "彻底删掉"}, True, "hotkey delete"),
    ({"action": "hotkey", "keys": "delete", "intent": "按删除键"}, True, "裸 delete"),
    # --- 必须放行（误拦就是事故） ---
    ({"action": "click", "point": (500, 500), "intent": "点击确定"}, False, "普通确定"),
    ({"action": "click", "point": (500, 500), "intent": "打开记事本"}, False, "开应用"),
    ({"action": "type", "text": "你好世界", "intent": "输入问候"}, False, "打字"),
    ({"action": "hotkey", "keys": "ctrl,s", "intent": "保存文件"}, False, "ctrl+s 保存"),
    ({"action": "hotkey", "keys": "backspace", "intent": "退格纠错"}, False, "backspace 允许"),
    ({"action": "hotkey", "keys": "win", "intent": "打开开始菜单"}, False, "win 键"),
    ({"action": "scroll", "point": (500, 500), "delta": "-3", "intent": "向下滚动"}, False, "滚动"),
    ({"action": "click", "point": (500, 500), "intent": "点击删除线样式"}, False, "含'删除'但意图是样式？——当前实现会拦，记录为已知保守"),
]

DANGER_CASES = [
    ("把桌面那个文件删掉", True), ("帮我删除系统32", True), ("清空回收站", True),
    ("格式化D盘", True), ("关机", True), ("重启电脑", True), ("给张三发消息说晚安", True),
    ("发送这封邮件", True), ("转账100块给我妈", True), ("付款", True),
    ("重启一下资源管理器", True),  # 保守拦（重启词命中）
    # 必须放行
    ("打开记事本写一段话", False), ("今天天气怎么样", False), ("提醒我明天开会", False),
    ("把音量调大一点", False), ("截个图看看", False), ("复制这段话", False),
    ("保存文件", False), ("新建一个文件夹", False), ("这个字删掉重新打", True),  # 语音层保守拦
    ("撤销上一步", False),
]

fails = []
for act, expect_block, desc in GATE_CASES:
    _, blocked = gui_agent._gate([act])
    got = blocked is not None
    ok = got == expect_block
    mark = "OK " if ok else "FAIL"
    if not ok:
        fails.append(("gate", desc, expect_block, got))
    print(f"[{mark}] gate | {desc}: 期望{'拦' if expect_block else '放'} 实际{'拦' if got else '放'}")

for text, expect_danger in DANGER_CASES:
    got = _is_dangerous(text)
    ok = got == expect_danger
    if not ok:
        fails.append(("danger", text, expect_danger, got))
    print(f"[{'OK ' if ok else 'FAIL'}] danger | {text}: 期望{'拦' if expect_danger else '放'} 实际{'拦' if got else '放'}")

print(f"\n闸用例 {len(GATE_CASES) + len(DANGER_CASES)} 条，失败 {len(fails)} 条")

# ================= 权限三档 + 自身代码闸（2026-08-28 设置面板落地）=================
import json
import os
import tempfile
import permission_mode
import plugins.exec_native as en


def _gate_with_mode(mode, tool, args, approved, calls):
    """在指定权限档位下过一次 exec_native._gate；calls 记录 on_confirm 触发。"""
    orig = permission_mode.current
    permission_mode.current = lambda: mode
    try:
        return en._gate(tool, args, lambda d: calls.append(d) or True,
                        lambda m: None, approved)
    finally:
        permission_mode.current = orig


MODE_CASES = []

# permission_mode.current() 本身：非法值/缺文件都 fail-safe 回 confirm
tmpc = os.path.join(tempfile.gettempdir(), "pm_test_cfg.json")
orig_path = permission_mode.CONFIG_PATH
try:
    permission_mode.CONFIG_PATH = tmpc
    with open(tmpc, "w", encoding="utf-8") as f:
        json.dump({"permission_mode": "yolo"}, f)
    MODE_CASES.append((permission_mode.current() == "yolo", "读取 yolo 档"))
    with open(tmpc, "w", encoding="utf-8") as f:
        json.dump({"permission_mode": "bogus"}, f)
    MODE_CASES.append((permission_mode.current() == "confirm", "非法值 fail-safe 回 confirm"))
    os.remove(tmpc)
    MODE_CASES.append((permission_mode.current() == "confirm", "配置文件缺失回 confirm"))
finally:
    permission_mode.CONFIG_PATH = orig_path
    if os.path.exists(tmpc):
        os.remove(tmpc)

# confirm 档：中危（taskkill）也要问
calls = []
r = _gate_with_mode("confirm", "bash", {"command": "taskkill /F /PID 123"}, set(), calls)
MODE_CASES.append((r is True and len(calls) == 1, "confirm 档中危(taskkill)进确认链"))

# yolo 档：中危自动放行不问
calls = []
r = _gate_with_mode("yolo", "bash", {"command": "taskkill /F /PID 123"}, set(), calls)
MODE_CASES.append((r is True and len(calls) == 0, "yolo 档中危(taskkill)自动放行"))

# GUI yolo 同口径：模型主判，只保留不可恢复删除
import gui_agent as _ga
import intent_judge as _ij
_orig_irrecoverable = _ij.judge_irrecoverable_deletions
try:
    _ij.judge_irrecoverable_deletions = lambda intents, timeout=4: [
        "永久删除" in x for x in intents]
    _ga_actions = [{"intent": "给妈妈发送微信"},
                   {"intent": "支付订单"},
                   {"intent": "永久删除账户数据"}]
    _ga_kept = _ga._confirmation_actions(_ga_actions, "yolo", set(), log=lambda *_: None)
    MODE_CASES.append((_ga_kept == [_ga_actions[2]],
                       "GUI yolo 模型主判仅保留不可恢复删除"))
finally:
    _ij.judge_irrecoverable_deletions = _orig_irrecoverable

# yolo 档：08-31 终裁只问不可恢复删除；关机等其他高危也直接放行
for cmd, desc, expected_calls in [("del /q a.txt", "删除 del", 1),
                                  ("shutdown /s /t 0", "关机 shutdown", 0),
                                  ("Clear-RecycleBin", "清回收站", 1)]:
    calls = []
    r = _gate_with_mode("yolo", "bash", {"command": cmd}, set(), calls)
    MODE_CASES.append((r is True and len(calls) == expected_calls,
                       f"yolo 档仅不可恢复删除确认({desc})"))

# auto 档：同一任务内同类问过一次不再重问；不同类照问
approved = set()
calls = []
_gate_with_mode("auto", "bash", {"command": "taskkill /F /PID 1"}, approved, calls)
_gate_with_mode("auto", "bash", {"command": "Stop-Process -Id 2"}, approved, calls)
MODE_CASES.append((len(calls) == 1, "auto 档同类(mid)第二次不再问"))
calls = []
_gate_with_mode("auto", "bash", {"command": "del /q a.txt"}, approved, calls)
MODE_CASES.append((len(calls) == 1, "auto 档换类别(top)仍要问"))

# 自身代码写入：confirm/auto 仍按原口径，yolo 直接放行
self_py = os.path.join(en._PROJECT_ROOT, "plugins", "zzz_probe.py")
calls = []
r = _gate_with_mode("yolo", "fs_write", {"path": self_py, "content": "x"}, set(), calls)
MODE_CASES.append((r is True and len(calls) == 0, "yolo 档写自身代码直接放行"))

# ===== yolo 终版分支（2026-08-29 终裁）：可逆系统变更放行，仅不可逆才问 =====
calls = []
r = _gate_with_mode("yolo", "bash",
                    {"command": "Set-ExecutionPolicy RemoteSigned -Force"}, set(), calls)
MODE_CASES.append((r is True and len(calls) == 0,
                   "yolo 档可逆系统变更(Set-ExecutionPolicy)直接放行"))
calls = []
r = _gate_with_mode("yolo", "bash",
                    {"command": 'Set-ExecutionPolicy RemoteSigned -Force; Write-Host "删除完毕"'},
                    set(), calls)
MODE_CASES.append((r is True and len(calls) == 0,
                   "yolo 档不可逆探针同吃引号载荷剥离（不升级误判）"))
approved = set()
calls = []
_gate_with_mode("yolo", "bash", {"command": "del /q a.txt"}, approved, calls)
_gate_with_mode("yolo", "bash", {"command": "del /q b.txt"}, approved, calls)
MODE_CASES.append((len(calls) == 1, "yolo 档不可逆批过一次本任务后续放行"))
approved = set()
calls = []
_gate_with_mode("yolo", "fs_write", {"path": self_py, "content": "x"}, approved, calls)
_gate_with_mode("yolo", "fs_write", {"path": self_py, "content": "y"}, approved, calls)
MODE_CASES.append((len(calls) == 0, "yolo 档自身代码全程无需确认"))

# Format-* 输出格式化 cmdlet 不算高危（2026-08-29 实锤：Get-WinUserLanguageList |
# Format-List 纯读被 \bformat\b 误判 top+不可逆，yolo 档连环问"简单的检查"）
calls = []
r = _gate_with_mode("yolo", "bash",
                    {"command": "Get-WinUserLanguageList | Format-List"}, set(), calls)
MODE_CASES.append((r is True and len(calls) == 0, "yolo 档 Format-List 纯读管道不问"))
calls = []
r = _gate_with_mode("confirm", "bash",
                    {"command": "Get-Process | Format-Table Name"}, set(), calls)
MODE_CASES.append((r is True and len(calls) == 0, "confirm 档 Format-Table 也不算高危"))
calls = []
r = _gate_with_mode("yolo", "bash", {"command": "Format-Volume -DriveLetter D"}, set(), calls)
MODE_CASES.append((r is True and len(calls) == 1, "yolo 档 Format-Volume(真格式化)仍问"))
calls = []
r = _gate_with_mode("confirm", "bash", {"command": "format D: /q"}, set(), calls)
MODE_CASES.append((r is True and len(calls) == 1, "confirm 档裸 format 仍问"))
for sub in ("workspace", "tmp", "memory", "压测"):
    p = os.path.join(en._PROJECT_ROOT, sub, "t.txt")
    calls = []
    r = _gate_with_mode("confirm", "fs_write", {"path": p, "content": "x"}, set(), calls)
    MODE_CASES.append((r is True and len(calls) == 0, f"写 {sub}\\ 数据目录不问"))

# 项目根外的写入不问（用户日常文件）
calls = []
r = _gate_with_mode("confirm", "fs_write",
                    {"path": r"D:\Desktop\note.txt", "content": "x"}, set(), calls)
MODE_CASES.append((r is True and len(calls) == 0, "写项目外用户文件不问"))

# fs_edit 与 fs_write 同口径（2026-08-28 自迭代写面修复）：自身代码任何档都问
calls = []
r = _gate_with_mode("yolo", "fs_edit",
                    {"path": self_py, "old_text": "a", "new_text": "b"}, set(), calls)
MODE_CASES.append((r is True and len(calls) == 0,
                   "yolo 档 fs_edit 写自身代码直接放行"))
calls = []
r = _gate_with_mode("confirm", "fs_edit",
                    {"path": r"D:\Desktop\note.txt", "old_text": "a", "new_text": "b"},
                    set(), calls)
MODE_CASES.append((r is True and len(calls) == 0, "fs_edit 写用户文件不问"))
# 空参数不误判自身代码（截断实锤修复：空 path 恒非 self）
MODE_CASES.append((en._is_self_code_path("") is False
                   and en._is_self_code_path(None) is False,
                   "空 path 不判自身代码"))

# 只读命令任何档都不问
calls = []
r = _gate_with_mode("yolo", "bash", {"command": "Get-Process"}, set(), calls)
MODE_CASES.append((r is True and len(calls) == 0, "yolo 档只读命令(Get-Process)不问"))

# 无确认链时危险调用 fail-closed（on_confirm=None）
orig = permission_mode.current
permission_mode.current = lambda: "yolo"
try:
    r = en._gate("bash", {"command": "del /q a.txt"}, None, lambda m: None, set())
    MODE_CASES.append((r is False, "无确认链最高危 fail-closed 拒绝"))
finally:
    permission_mode.current = orig

# 载荷剥离（2026-08-28 自迭代实锤）：引号内文本不算操作意图
calls = []
r = _gate_with_mode("confirm", "bash",
                    {"command": 'Select-String -Pattern "重启" -Path companion_rt.py'},
                    set(), calls)
MODE_CASES.append((r is True and len(calls) == 0,
                   'Select-String -Pattern "重启" 搜代码不再误判'))
calls = []
r = _gate_with_mode("confirm", "bash",
                    {"command": 'Get-ChildItem -Recurse *.py | Select-String "删除"'},
                    set(), calls)
MODE_CASES.append((r is True and len(calls) == 0,
                   '管道+引号搜索"删除"不再误判'))
calls = []
r = _gate_with_mode("confirm", "bash",
                    {"command": 'del "important file.txt"'}, set(), calls)
MODE_CASES.append((r is True and len(calls) == 1,
                   'del "带空格文件" 仍命中（动词在引号外）'))
calls = []
r = _gate_with_mode("confirm", "bash",
                    {"command": 'Remove-Item "D:\\x" -Recurse'}, set(), calls)
MODE_CASES.append((r is True and len(calls) == 1,
                   'Remove-Item 引号路径仍命中'))

calls = []
r = _gate_with_mode("confirm", "bash",
                    {"command": "Get-Process python  # 重启前查进程"}, set(), calls)
MODE_CASES.append((r is True and len(calls) == 0,
                   "PS 注释含「重启」不再误判（Get-Process 实锤案例）"))
calls = []
r = _gate_with_mode("confirm", "bash",
                    {"command": 'Remove-Item "a.txt"  # 删除临时文件'},
                    set(), calls)
MODE_CASES.append((r is True and len(calls) == 1,
                   "动词在注释外仍命中（Remove-Item + 注释）"))

# 路由级预确认档位（2026-08-28 缺口修复）：confirm 启用，auto/yolo 跳过
import companion_rt as _crt
_orig_pm = permission_mode.current
try:
    permission_mode.current = lambda: "confirm"
    MODE_CASES.append((_crt._route_level_confirm_on() is True,
                       "confirm 档路由级预确认启用"))
    for m in ("auto", "yolo"):
        permission_mode.current = lambda m=m: m
        MODE_CASES.append((_crt._route_level_confirm_on() is False,
                           f"{m} 档路由级预确认跳过（细粒度闸接管）"))
finally:
    permission_mode.current = _orig_pm

# 派活去抖（2026-08-28 风暴实锤：真实日志对 Jaccard 0.42-0.47）
_sim_pairs = [
    ("打开Windows系统自带的Copilot应用，并启动/演示其语音对话功能，让用户能体验Copilot的语音交互。",
     "打开 Windows 上的 Copilot 应用，并进入语音对话模式，方便用户体验 Copilot 的语音功能。", True),
    ("在现有代码里新增一个「退下吧」「可以下去了」类指令的隐藏逻辑，当用户说出这类话时整个文字框和光球界面隐藏",
     "在现有代码里新增「退下」逻辑，当用户说「退下吧」「可以下去了」等类似指令时隐藏整个文字框和光球", True),
    ("把音量调大一点", "把音量调小一点", False),
    ("打开记事本写一段话", "打开画图工具", False),
    ("删除桌面上的截图文件", "删除回收站里的所有内容", False),
]
for _a, _b, _want in _sim_pairs:
    MODE_CASES.append((_crt._task_sim(_a, _b) == _want,
                       f"去抖相似度 {_a[:10]}… vs {_b[:10]}… = {_want}"))


# _enqueue_task 端到端去抖（假会话库，不落盘、不碰真账本）
class _FakeSession:
    def __init__(self, tid, text, mode):
        self.task_id, self.task_text, self.mode = tid, text, mode

    def snapshot(self):
        return {"task_text": self.task_text, "state": "queued"}


class _FakeSessions:
    def __init__(self):
        self.items = {}
        self.n = 0

    def create(self, text, orig, mode, **kw):
        self.n += 1
        tid = f"fake-{self.n}"
        s = _FakeSession(tid, text, mode)
        self.items[tid] = s
        return s

    def get(self, tid):
        return self.items.get(tid)


_orig_sessions = _crt._sessions
_orig_inject = _crt._inject_rag
_crt._sessions = _FakeSessions()
_crt._inject_rag = lambda *a, **k: None
_crt._scheduled_sigs.clear()
_crt._recent_dispatches.clear()
_crt._fast_done.clear()
try:
    _t1 = '把项目里所有带"重启"字样的代码段搜出来列成清单，存到workspace里'
    _t2 = '把项目里所有带"重启"字样的代码段搜索出来，列一个清单存到workspace目录下'
    _r1 = _crt._enqueue_task(_t1, _t1, "implicit")
    _r2 = _crt._enqueue_task(_t2, _t2, "implicit")
    MODE_CASES.append((_r1 is True and _r2 is False, "相似任务 60s 窗口去抖拦下"))
    _r3 = _crt._enqueue_task("不对，取消刚才那个搜索任务", "不对取消", "implicit")
    MODE_CASES.append((_r3 is True, "带修正词的派活不被去抖吞掉"))
finally:
    _crt._sessions = _orig_sessions
    _crt._inject_rag = _orig_inject
    _crt._scheduled_sigs.clear()
    _crt._recent_dispatches.clear()
    _crt._fast_done.clear()
    while not _crt._task_q.empty():
        try:
            _crt._task_q.get_nowait()
        except Exception:
            break

# 自迭代工具面（2026-08-28）：fs_read 分页 / grep / 代码任务禁换 GUI
import tempfile as _tmpmod
_tmpd = _tmpmod.mkdtemp()
_big = os.path.join(_tmpd, "big.txt")
with open(_big, "w", encoding="utf-8") as _f:
    _f.write("甲" * 8000)
_out = en._t_fs_read(_big, offset=7000, limit=6000)
MODE_CASES.append(("全长 8000" in _out and "7000-8000" in _out and "续读" not in _out,
                   "fs_read 分页读到尾且无续读提示"))
_out = en._t_fs_read(_big, offset=0, limit=6000)
MODE_CASES.append(("全长 8000" in _out and "offset=6000" in _out,
                   "fs_read 首页给续读位置"))
_gd = os.path.join(_tmpd, "src")
os.makedirs(_gd, exist_ok=True)
with open(os.path.join(_gd, "a.py"), "w", encoding="utf-8") as _f:
    _f.write("def foo():\n    return '确认逻辑'\n")
with open(os.path.join(_gd, "b.md"), "w", encoding="utf-8") as _f:
    _f.write("无关内容\n")
_out = en._t_grep("确认逻辑", _gd)
MODE_CASES.append(("a.py:2:" in _out and "b.md" not in _out, "grep 命中带行号且不捞无关文件"))
MODE_CASES.append((en._t_grep("不存在的东西", _gd) == "（无匹配）", "grep 无匹配如实返回"))
MODE_CASES.append(("不存在" in en._t_grep("x", os.path.join(_tmpd, "nope")),
                   "grep 路径不存在如实返回"))

# fs_edit 精准编辑（2026-08-28 自迭代写面修复：大文件不再整写）
_ef = os.path.join(_tmpd, "edit_me.py")
with open(_ef, "wb") as _f:   # 带 BOM + CRLF，验证写回保真
    _f.write(b"\xef\xbb\xbfdef foo():\r\n    return 1\r\n")
_out = en._t_fs_edit(_ef, "return 1", "return 42")
_raw = open(_ef, "rb").read()
MODE_CASES.append(("已编辑" in _out and _raw.startswith(b"\xef\xbb\xbf")
                   and b"return 42\r\n" in _raw and b"\r\n" in _raw,
                   "fs_edit 唯一命中替换且 BOM/CRLF 保真"))
_out = en._t_fs_edit(_ef, "没有这段", "x")
MODE_CASES.append(("没有命中" in _out, "fs_edit 零命中如实报错不写文件"))
with open(_ef, "w", encoding="utf-8", newline="") as _f:
    _f.write("x = 1\ny = 1\n")
_out = en._t_fs_edit(_ef, "1", "2")
MODE_CASES.append(("不唯一" in _out and "replace_all" in _out,
                   "fs_edit 多命中不唯一时报错"))
_out = en._t_fs_edit(_ef, "1", "2", replace_all=True)
MODE_CASES.append(("替换 2 处" in _out, "fs_edit replace_all 全替换"))
# 代码类任务超时放宽（240s 白跑实锤 → ≥900s）
from plugins import scaffold as _sc
_MODE_CASES_T = _sc.code_task_gate("修改小凯自身代码，修 bug")
MODE_CASES.append((bool(_MODE_CASES_T), "代码任务识别新目录名（智能助手）[scaffold]"))

# 「退下」完整退出（2026-08-31 终裁）：界面、任务、监听、内部子进程全停
_src_rt = open(os.path.join(en._PROJECT_ROOT, "companion_rt.py"),
               encoding="utf-8").read()
MODE_CASES.append(("def _retire_companion()" in _src_rt
                   and "exec_native.stop_all_children" in _src_rt
                   and "_wake_listener.stop()" in _src_rt
                   and "task_overlay.hide()" in _src_rt,
                   "「退下」完整退出链在位"))
import plugins.scaffold as _sc_mod  # 退下判定=scaffold件(2026-08-31收编)，测试装配同 main()
if _crt._ksvc("scaffold") is None:
    _crt._kernel.load("scaffold", _sc_mod)
    _crt._ksvc("scaffold")["bind"](lambda k, d=None: d)
for _t, _want in [("好，非常好，退下吧。", True), ("退下", True),
                  ("退一下吧。", True), ("你退下。", True),
                  ("可以下去了", True), ("先下去吧", True),
                  ("退下之后有没有语音交互？", False), ("退下是什么意思？", False),
                  ("退下了吗？", False), ("刚才说的退下逻辑改了吗", False)]:
    MODE_CASES.append((_crt._is_retire_command(_t) == _want,
                       f"退下触发[{_t[:12]}] = {_want}"))
_src_si = open(os.path.join(en._PROJECT_ROOT, "sense_io.py"),
               encoding="utf-8").read()
MODE_CASES.append(("def orb_hide()" in _src_si
                   and 'orb_overlay._state["visible"] = False' in _src_si,
                   "sense_io.orb_hide UI 原语在位"))
_src_wake = open(os.path.join(en._PROJECT_ROOT, "wake_word.py"),
                 encoding="utf-8").read()
MODE_CASES.append(("_builtin_mic_index" not in _src_wake
                   and "sd.InputStream(device=None" in _src_wake
                   and "thread.join(timeout=2.0)" in _src_wake,
                   "唤醒监听跟随系统默认且退下时可主动停流"))

# 代码任务禁换 GUI（_auto_fallback）
_orig_log_line = _crt.task_overlay.log_line
_crt.task_overlay.log_line = lambda *a, **k: None
_orig_sessions2 = _crt._sessions
_crt._sessions = _FakeSessions()
_crt._mode_attempts.clear()
try:
    MODE_CASES.append((_crt._auto_fallback("修改自身确认逻辑代码，加反馈", "x",
                                           "implicit") is False,
                       "代码类任务 implicit 失败不换 GUI"))
    MODE_CASES.append((_crt._auto_fallback("打开记事本写一段话", "x",
                                           "implicit") is True,
                       "非代码任务照常自动换路"))
finally:
    _crt._sessions = _orig_sessions2
    _crt.task_overlay.log_line = _orig_log_line
    _crt._mode_attempts.clear()
    _crt._scheduled_sigs.clear()
    _crt._recent_dispatches.clear()
    while not _crt._task_q.empty():
        try:
            _crt._task_q.get_nowait()
        except Exception:
            break

for ok, desc in MODE_CASES:
    if not ok:
        fails.append(("mode", desc))
    print(f"[{'OK ' if ok else 'FAIL'}] mode | {desc}")

print(f"\n总计 {len(GATE_CASES) + len(DANGER_CASES) + len(MODE_CASES)} 条，失败 {len(fails)} 条")
for f in fails:
    print("FAIL:", f)
