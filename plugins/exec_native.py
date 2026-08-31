# -*- coding: utf-8 -*-
"""exec-native：小凯自有执行循环（不依赖超强模型的自进化载体之一）。

动机（用户裁决 2026-08-21）：dsh 只是执行插件之一；当本循环在同口径
验收打平或反超 exec-dsh 时，dsh 可一行卸载（kernel.unload("exec-dsh")）。

设计（极简 ReAct）：
- DeepSeek function calling 循环：模型出工具调用 → 本地执行 → 结果回灌 →
  直到模型给最终文本或触上限。
- 工具注册表就是能力面：bash / fs_read / fs_write / fs_list（v1 后台无屏件）。
- 安全：每个工具调用先过 intent_judge 模型级裁决；判危险且有 on_confirm →
  走中央语音确认链；无确认链 → fail-closed 拒绝执行。
- 口径：返回 ExecutionResult 同款 (status/value/tools)，companion_rt 的
  完成播报分级（纯回答型/副作用型）原样适用。
"""

import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from collections import namedtuple

import secrets_store
import soul
import permission_mode
import intent_judge

Result = namedtuple("Result", "status value tools")

_MAX_ROUNDS = 12
_MODEL = "deepseek-v4-flash-vision-exp"   # 日常脑：文字同 flash，需要时有眼
_MODEL_PRO = "deepseek-v4-pro"            # 重任务档（配档判定后直派）


def _plan(task_text, log=print):
    """智能配档（用户 2026-08-22 裁决：不是慢慢升档，是 flash 关思考秒判，
    吃不消的直接 pro）：返回 (model, thinking_extra)。
    判定失败默认轻档——判错轻活的代价是多花一点，判错重活会失败如实报。"""
    key = secrets_store.get_secret("deepseek")
    if not key:
        return _MODEL, {"type": "disabled"}
    sys_p = ("你是任务难度评估器。判断这个电脑操作任务需要的执行档位。"
             "只回 JSON：{\"heavy\": true/false, \"effort\": \"none|low\"}。"
             "轻档：单步命令、查询状态、读写小文件、简单联网问答。"
             "重档：多步骤链路、装软件/改配置、调试排错、资料综合、界面操作编排。"
             "effort：轻=none；重=low（不给 high——实测 high 一轮 96 秒，"
             "卡死级时延，效率杀手）。")
    body = {"model": "deepseek-chat", "max_tokens": 40, "temperature": 0,
            "thinking": {"type": "disabled"},
            "messages": [{"role": "system", "content": sys_p},
                         {"role": "user", "content": task_text[:400]}]}
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = (data["choices"][0]["message"].get("content") or "").strip()
        m = json.loads(text[text.find("{"):text.rfind("}") + 1])
        heavy = bool(m.get("heavy"))
        effort = str(m.get("effort", "none")).lower()
        if heavy:
            if effort not in ("low",):
                effort = "low"   # high 封顶为 low（96s/轮的卡死级时延禁用）
            log(f"配档: pro/{effort}（{time.time()-t0:.1f}s）")
            return _MODEL_PRO, {"type": "enabled"}, effort
        log(f"配档: flash/none（{time.time()-t0:.1f}s）")
        return _MODEL, {"type": "disabled"}, None
    except Exception as e:
        log(f"配档判定失败（默认轻档）: {e}")
        return _MODEL, {"type": "disabled"}, None


def _deepseek_call(messages, tools, timeout=60, model=None,
                   thinking=None, effort=None):
    key = secrets_store.get_secret("deepseek")
    if not key:
        raise RuntimeError("缺少 deepseek 凭据")
    # 思考模式按需配档（默认关——2026-08-22 实测默认 effort=high
    # 首轮 96s 卡死级时延）。注意：思考开启且带 tools 时，后续轮次必须
    # 回传 reasoning_content 否则 400（run_task 的拼消息处已处理）。
    body = {"model": model or _MODEL, "messages": messages, "tools": tools,
            "tool_choice": "auto", "temperature": 0.3, "max_tokens": 2048,
            "thinking": thinking or {"type": "disabled"}}
    if effort and body["thinking"].get("type") == "enabled":
        body["reasoning_effort"] = effort
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]


# ---------------------------------------------------------------- 工具实现
_NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # 铁律"隐就要隐得彻底"：
# 模型循环一秒能弹十几个终端闪窗（2026-08-27 用户实锤"大量弹出终端又消失"
# = 无感违规）——本模块所有子进程一律无窗。


_CHILDREN = set()
_CHILDREN_LOCK = threading.Lock()


def _desktop_dir():
    """当前用户真实桌面（2026-08-31 开源化：读注册表 User Shell Folders——
    桌面被移到 D 盘/OneDrive 的机器都拿真值），失败回退 ~/Desktop。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion"
                            r"\Explorer\User Shell Folders") as k:
            v, _ = winreg.QueryValueEx(k, "Desktop")
            d = os.path.expandvars(v)
            if os.path.isdir(d):
                return d
    except OSError:
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def _t_bash(cmd):
    """执行内部命令并登记子进程，使“退下”能把助手进程树真正收干净。"""
    p = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command", cmd],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=_desktop_dir(), creationflags=_NO_WIN)
    with _CHILDREN_LOCK:
        _CHILDREN.add(p)
    try:
        out, err = p.communicate(timeout=60)
        out = (out or "")[-3000:]
        err = (err or "")[-1000:]
        return out if p.returncode == 0 else f"(exit {p.returncode}) {err or out}"
    except subprocess.TimeoutExpired:
        _terminate_child_tree(p, log=lambda _m: None)
        return "(timeout) 命令执行超过 60 秒，已停止"
    finally:
        with _CHILDREN_LOCK:
            _CHILDREN.discard(p)


def _terminate_child_tree(p, log=print):
    if p is None or p.poll() is not None:
        return
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                       capture_output=True, timeout=5, creationflags=_NO_WIN)
    except Exception as e:
        try:
            p.kill()
        except Exception:
            pass
        log(f"exec-native: 内部子进程终止降级: {e}")


def stop_all_children(log=print):
    """只停止助手内部命令进程；用户要求打开的应用不在此登记，不误杀。"""
    with _CHILDREN_LOCK:
        children = list(_CHILDREN)
    for child in children:
        _terminate_child_tree(child, log=log)
    if children:
        log(f"exec-native: 已停止 {len(children)} 个内部命令进程树")


def _t_fs_read(path, offset=0, limit=6000):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    total = len(text)
    offset = max(0, min(int(offset or 0), total))
    limit = max(200, min(int(limit or 6000), 20000))
    chunk = text[offset:offset + limit]
    more = (f"\n…（还有 {total - offset - len(chunk)} 字符，"
            f"用 offset={offset + len(chunk)} 续读）"
            if offset + len(chunk) < total else "")
    return f"（全长 {total} 字符，本段 {offset}-{offset + len(chunk)}）\n{chunk}{more}"


_GREP_EXTS = (".py", ".md", ".json", ".yaml", ".yml", ".txt",
              ".ps1", ".bat", ".ts", ".mjs", ".html")
_GREP_SKIP_DIRS = ("node_modules", ".git", "__pycache__", "models",
                   ".venv", ".chrome-space", ".workbench")


def _t_grep(pattern, path):
    """代码/日志全文搜索（2026-08-28 自迭代实锤：没有 grep，改自己代码=
    fs_read 6000 字符盲人摸象）。path 可为文件或目录（目录递归常见代码
    后缀）；返回 路径:行号: 内容，封顶 120 行。"""
    rx = re.compile(pattern, re.I)
    files = []
    if os.path.isfile(path):
        files = [path]
    elif os.path.isdir(path):
        for root, dirs, names in os.walk(path):
            dirs[:] = [d for d in dirs if d not in _GREP_SKIP_DIRS]
            for n in names:
                if os.path.splitext(n)[1].lower() in _GREP_EXTS:
                    files.append(os.path.join(root, n))
            if len(files) >= 400:
                break
    else:
        return f"路径不存在: {path}"
    hits = []
    for fp in files[:400]:
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if rx.search(line):
                        hits.append(f"{fp}:{i}: {line.rstrip()[:160]}")
                        if len(hits) >= 120:
                            return "\n".join(hits) + "\n…（已截断 120 行）"
        except OSError:
            continue
    return "\n".join(hits) if hits else "（无匹配）"


def _t_fs_write(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"已写入 {path}（{len(content)} 字符）"


def _t_fs_edit(path, old_text, new_text, replace_all=False):
    """精准编辑现有文件（2026-08-28 自迭代实锤：整文件 fs_write 大文件
    会被模型输出上限截断→参数 JSON 解析失败→空参数连环确认，改代码任务
    必死）。只传补丁片段，参数小永不截断。BOM/换行符原样保留。"""
    raw = open(path, "rb").read()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    n = text.count(old_text)
    if n == 0:
        return ("替换失败：old_text 没有命中。先 fs_read/grep 核对原文，"
                "old_text 必须与文件内容逐字符一致（含缩进和换行）。")
    if n > 1 and not replace_all:
        return (f"替换失败：old_text 命中 {n} 处（不唯一）。"
                "扩大上下文让它唯一，或确需全部替换时 replace_all=true。")
    text = text.replace(old_text, new_text) if replace_all \
        else text.replace(old_text, new_text, 1)
    with open(path, "w", encoding="utf-8-sig" if has_bom else "utf-8",
              newline="") as f:
        f.write(text)
    return (f"已编辑 {path}（替换 {n if replace_all else 1} 处，"
            f"{len(old_text)}→{len(new_text)} 字符）")


def _t_fs_list(path):
    import os
    entries = os.listdir(path)[:200]
    return "\n".join(entries) if entries else "（空目录）"


def _t_web_search(query):
    """联网搜索：直连豆包搜索 API 主路（open.feedcoopapi.com，亚秒级，
    websearch 凭据；Summary 字段官方推荐用于大模型场景）；
    失败落 Ark Responses API 代理式搜索（慢但口味不同，真冗余）。"""
    key = secrets_store.get_secret("websearch")
    if key:
        try:
            body = {"Query": query, "SearchType": "web", "Count": 5,
                    "Filter": {"NeedUrl": True}}
            req = urllib.request.Request(
                "https://open.feedcoopapi.com/search_api/web_search",
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            result = data.get("Result") or {}
            items = result.get("WebResults") or []
            parts = []
            for it in items:
                s = (it.get("Summary") or it.get("Snippet") or "").strip()
                if s:
                    parts.append(f"【{it.get('SiteName', '')}】{s}")
            if parts:
                return "\n".join(parts)[:1800]
        except Exception:
            pass   # 落 Ark 慢路
    ark = secrets_store.get_secret("ark")
    if not ark:
        return "联网搜索未配置凭据"
    body = {"model": "doubao-seed-2-1-turbo-260628", "input": query,
            "tools": [{"type": "web_search"}]}
    req = urllib.request.Request(
        "https://ark.cn-beijing.volces.com/api/v3/responses",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {ark}"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    for item in reversed(data.get("output", [])):
        if item.get("type") == "message":
            texts = [c.get("text", "") for c in item.get("content", [])]
            out = "".join(texts).strip()
            if out:
                return out[:2000]
    return "搜索没有返回结果"


def _t_screen_qa(question):
    """工作层自己的眼睛（2026-08-22：DeepSeek 视觉能力接入的落点之一）：
    截屏 → gui_brain 双眼描述链（Ark seed 主路 / DeepSeek V4 Vision 兜底）。
    只读无副作用——任务里需要"看懂屏幕/窗口/报错内容"时用，不点不按。"""
    import gui_brain
    import host_input
    img = host_input.screenshot()
    desc, lat = gui_brain.describe(question or "屏幕上现在是什么", img,
                                   log=lambda m: None)
    return f"（截屏实况，{lat:.1f}s）{desc}"


_TOOLS_IMPL = {
    "bash": lambda a: _t_bash(a["command"]),
    "fs_read": lambda a: _t_fs_read(a["path"], a.get("offset", 0),
                                    a.get("limit", 6000)),
    "fs_write": lambda a: _t_fs_write(a["path"], a["content"]),
    "fs_edit": lambda a: _t_fs_edit(a["path"], a["old_text"],
                                    a["new_text"], a.get("replace_all", False)),
    "fs_list": lambda a: _t_fs_list(a["path"]),
    "grep": lambda a: _t_grep(a["pattern"], a["path"]),
    "web_search": lambda a: _t_web_search(a["query"]),
    "screen_qa": lambda a: _t_screen_qa(a.get("question", "")),
}

_TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "bash",
        "description": "执行 PowerShell 命令（Windows 11）。查系统信息、跑程序、"
                       "操作文件都能用。工作目录为当前用户的桌面。",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}},
                       "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "fs_read",
        "description": "读文本文件内容。大文件用 offset/limit 分页续读"
                       "（返回头会告诉你全长和续读位置）。",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "offset": {"type": "integer"},
                                      "limit": {"type": "integer"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "grep",
        "description": "全文搜索：在文件或目录（递归）里按正则找内容，"
                       "返回 路径:行号: 内容。改代码前先 grep 定位，不要整文件瞎读。",
        "parameters": {"type": "object",
                       "properties": {"pattern": {"type": "string"},
                                      "path": {"type": "string"}},
                       "required": ["pattern", "path"]}}},
    {"type": "function", "function": {
        "name": "fs_write",
        "description": "写文本文件（覆盖）。只用于新建文件或小文件全量重写；"
                       "修改现有文件一律用 fs_edit（大文件整写会失败）",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "fs_edit",
        "description": "精准编辑现有文件（改代码/配置首选）：把文件里一段"
                       "old_text 原样替换为 new_text。old_text 必须与文件内容"
                       "逐字符一致（含缩进换行），先 grep/fs_read 核对再编辑。"
                       "只传补丁片段，不要整文件重写。",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "old_text": {"type": "string"},
                                      "new_text": {"type": "string"},
                                      "replace_all": {"type": "boolean"}},
                       "required": ["path", "old_text", "new_text"]}}},
    {"type": "function", "function": {
        "name": "fs_list",
        "description": "列目录内容",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "联网搜索实时信息（天气、新闻、汇率、比分、热搜等），"
                       "只动嘴不动电脑",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "screen_qa",
        "description": "截屏并看懂屏幕内容（报错、界面状态、文件内容等），"
                       "只读不动鼠标键盘。需要'看'才能判断时用。",
        "parameters": {"type": "object",
                       "properties": {"question": {"type": "string"}},
                       "required": ["question"]}}},
]

# 副作用工具名（与 companion_rt 的完成分级口径一致）
_SIDE_EFFECT_TOOLS = {"bash", "fs_write", "fs_edit"}

# 必填参数（执行循环前置校验：缺失不进闸，以 tool result 回给模型重试。
# 2026-08-28 实锤：空参数进闸 = 确认文案"范围{}" + 空 path 误判自身代码）
_REQUIRED_ARGS = {
    "bash": ("command",), "fs_write": ("path", "content"),
    "fs_edit": ("path", "old_text", "new_text"),
    "fs_read": ("path",), "fs_list": ("path",), "grep": ("pattern", "path"),
    "web_search": ("query",),
}

# 代码类任务识别（2026-08-31 收编：判定件=scaffold.code_task_gate，词表
# 单一真源在 plugins/scaffold.py；与 companion_rt 换路保护共用同一判定。
# 插件不装配=脚手架退役模型单跑→返回 False）
_svc_getter = None


def _code_task_gate(text):
    svc = _svc_getter("scaffold") if _svc_getter else None
    fn = (svc or {}).get("code_task_gate")
    if not fn:
        return False
    try:
        return bool(fn(text))
    except Exception:
        return False


# 明确只读的命令白名单：免裁决直接放行——含管道/重定向/连接符的不算
# （Get-Process | kill 这种链不允许）。
_SAFE_CMD = re.compile(
    r"^\s*(Get-\w+|ls|dir|cat|echo|pwd|where\.exe|Test-Path|whoami|hostname|"
    r"ipconfig|systeminfo|tasklist|winget\s+(list|show|search)\b)", re.I)
_CMD_CHAIN = re.compile(r"[;|>&<]")

# 最高危类（2026-08-28 用户拍板：删/付/系统变更在任何权限档都过语音确认）：
# 删数据/清回收站/格式化 + 转账付款 + 关机重启/卸载/账户/执行策略/防火墙。
_DANGER_CMD_TOP = re.compile(
    # format 排除 Format-List/Table/Wide/Custom/Hex（2026-08-29 实锤：输出
    # 格式化 cmdlet 是纯读，Get-X | Format-List 被 \bformat\b 误判 top 连环问）；
    # Format-Volume/裸 format 仍是真格式化。
    r"Remove-|del\b|erase|rmdir|\brm\b|format\b(?!-(?:list|table|wide|custom|hex)\b)|Clear-RecycleBin|"
    r"shutdown|Restart-Computer|Stop-Computer|logoff|Uninstall-|"
    r"net\s+user|Set-LocalUser|Rename-Computer|Set-ExecutionPolicy|"
    r"禁用|启用.*(防火墙| defender)|删除|删掉|清空|格式化|"
    r"关机|重启|注销|卸载|转账|付款|支付|红包|下单", re.I)

# 其余高危（完全自主档自动放行，逐条/自动通过档照问）：杀进程类。
_DANGER_CMD_MID = re.compile(r"taskkill|Stop-Process|\bkill\b", re.I)

# 高危命令模式（用户 2026-08-22 明令：只有高危才确认，其它一律不问）：
# 删数据/毁系统/杀进程/关机重启/卸载/改账户与执行策略/清回收站。
# 中间地带（装软件、写用户文件、开程序）一律放行不过问。
# 2026-08-28 拆档后 = _DANGER_CMD_TOP ∪ _DANGER_CMD_MID（判定集保持不变）。
_DANGER_CMD = re.compile(
    _DANGER_CMD_TOP.pattern + "|" + _DANGER_CMD_MID.pattern, re.I)

# 自身代码写入闸（2026-08-28 自进化配套）：fs_write 写项目根下、非数据目录
# 的文件 = 改自身代码，视同系统变更进语音确认（任何档都问）。
# bash 写文件不做路径 sniff（误报是"确认太频繁"的根因）——模型改代码正常
# 走 fs_write；bash 里的删除/系统变更仍被上面的高危正则接住。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SELF_OK_DIRS = ("workspace", "tmp", ".tmp", "memory", "压测",
                 "docs", "events", "sessions", "reminders")


def _is_self_code_path(path):
    """path 在项目根下且不在数据目录白名单里 → 属自身代码。
    空 path 恒 False（2026-08-28 实锤：参数被截断成 {} 时空 path 因
    cwd 恰在项目根被误判为改自身代码；空参数现在更早被拒绝，这里只防御）。"""
    path = str(path or "").strip()
    if not path:
        return False
    try:
        root = os.path.abspath(_PROJECT_ROOT)
        p = os.path.abspath(path)
        if os.path.commonpath([p, root]) != root:
            return False
    except (OSError, ValueError):
        return False
    first = os.path.relpath(p, root).split(os.sep)[0].lower()
    return first not in _SELF_OK_DIRS


_QUOTED = re.compile(r'"[^"\n]*"|\'[^\'\n]*\'')
_PS_COMMENT = re.compile(r"#[^\n]*")   # PS 注释也算载荷（"# 重启服务"误伤实锤）


def _strip_payload(cmd):
    """剥离引号载荷再判危险（2026-08-28 自迭代实锤）：搜索词/文本内容里的
    "删除/重启"是数据不是操作意图——Select-String -Pattern "重启" 搜自己
    代码曾被误判 cat=top 连环问。命令动词在引号外：del "path" 的 del
    本身仍命中，语义不丢。"""
    return _PS_COMMENT.sub(" ", _QUOTED.sub(" ", cmd))


# 最大自主档机械兜底：只有“永久毁数据”相关词。正常由模型先判；只有模型
# 不可用/输出坏掉才查这里。付款、发送、关机、卸载、改系统/代码全部不在内。
_IRRECOVERABLE_DELETE_FALLBACK_RE = re.compile(
    r"\b(del|erase|rm|rmdir)\b|Remove-Item|Clear-RecycleBin|"
    r"\bformat\b(?!-(?:list|table|wide|custom|hex)\b)|\bwipe\b|"
    r"永久删除|彻底删除|不可恢复|无法恢复|清空回收站|格式化|抹除|销毁|"
    r"清空.{0,10}(聊天记录|账户数据|文件|数据)", re.I)


def _yolo_needs_confirmation(text, log=print):
    """模型主判不可恢复删除；失败才用窄规则兜底。"""
    verdict = intent_judge.judge_irrecoverable_deletions([text], timeout=4)
    if verdict is not None and verdict:
        log(f"exec-native: 最大自主模型裁决={'需确认' if verdict[0] else '放行'}")
        return bool(verdict[0])
    fallback = bool(_IRRECOVERABLE_DELETE_FALLBACK_RE.search(text or ""))
    log(f"exec-native: 最大自主模型不可用，机械兜底={'需确认' if fallback else '放行'}")
    return fallback


def _gate(tool_name, args, on_confirm, log, task_approved=None):
    """工具调用安全闸（2026-08-22 用户改规矩后）：本地规则两极判定——
    明确只读秒放行；命中高危模式走中央语音确认链；其余一律放行不过问。
    不再过 intent_judge 模型裁决（它把 Get-Process 这种纯读也拦，
    假阳性是"确认太频繁"的根因）。

    权限三档（2026-08-29 用户终裁，permission_mode.current() 现读现用）：
    confirm 逐条确认=高危逐条问（默认）；auto 自动通过=同一任务内同一类别
    问过一次就不再重问（task_approved 任务级记忆，任务结束即失效）；
    yolo 完全自主=只有模型判为不可恢复的数据删除才问；发送、付款、关机、
    卸载、系统变更和自身代码全部直接放行。模型不可用时由窄规则兜底。"""
    # 确认/日志文案专门化（2026-08-28 实锤：裸 json.dumps 截断出
    # "范围是{}"，用户听不懂是什么操作；fs 类要让人听出是哪个文件）：
    if tool_name in ("fs_write", "fs_edit"):
        desc = f"{'写入' if tool_name == 'fs_write' else '编辑'}文件 {args.get('path', '')}"
    else:
        desc = json.dumps(args, ensure_ascii=False)[:80]
    if tool_name in ("fs_read", "fs_list", "grep", "web_search", "screen_qa"):
        return True
    category = None
    if tool_name == "bash":
        cmd = str(args.get("command", ""))
        if _SAFE_CMD.match(cmd) and not _CMD_CHAIN.search(cmd):
            return True
        cmd_ops = _strip_payload(cmd)
        if _DANGER_CMD_TOP.search(cmd_ops):
            category = "top"
        elif _DANGER_CMD_MID.search(cmd_ops):
            category = "mid"
    elif tool_name in ("fs_write", "fs_edit"):
        if _is_self_code_path(args.get("path", "")):
            category = "self"
        else:
            return True   # 写用户文件=日常干活，不问（危险动作在命令层拦）
    elif _DANGER_CMD_TOP.search(desc):
        category = "top"
    elif _DANGER_CMD_MID.search(desc):
        category = "mid"
    if category is None:
        return True
    mode = permission_mode.current()
    if mode == "yolo":
        probe = cmd_ops if tool_name == "bash" else desc
        if category != "top" or not _yolo_needs_confirmation(probe, log=log):
            log(f"exec-native: 最大自主档直接放行（{desc[:40]}）")
            return True
        if task_approved is not None and "irrecoverable-delete" in task_approved:
            return True
    if (mode == "auto" and task_approved is not None
            and category in task_approved):
        return True
    log(f"exec-native: 高危工具调用 {tool_name}（{desc[:40]}）"
        f" mode={mode} cat={category}")
    if on_confirm is not None:
        ok = bool(on_confirm(desc[:60]))
        if ok and mode in ("auto", "yolo") and task_approved is not None:
            remembered = ("irrecoverable-delete" if mode == "yolo" else category)
            task_approved.add(remembered)
            log(f"exec-native: 记住本任务授权类别 {remembered}")
        return ok
    log("exec-native: 无确认链，fail-closed 拒绝")
    return False


# --- delegate：主模型自己判断拆活，只读子任务真并发（2026-08-22 上线） ---
# （旧 _DELEGATE_SEM 固定 3 封顶已由下方负载敏感保险丝取代，2026-08-30）
_SUB_SAFE = {"fs_read", "fs_list", "grep", "web_search", "screen_qa"}   # 子任务只读面

_DELEGATE_SCHEMA = {"type": "function", "function": {
    "name": "delegate",
    "description": "把可独立完成的只读子任务（查资料/读文件/看屏幕/汇总信息）"
                   "派给并发子任务。同一轮里可以派多个，它们真并发跑。"
                   "子任务只有只读工具：不能跑命令、不能写文件、不能动键鼠。",
    "parameters": {"type": "object",
                   "properties": {"task": {"type": "string",
                                           "description": "子任务，说完整具体"}},
                   "required": ["task"]}}}


# 子智能体并发保险丝（2026-08-30 用户令：数量不是固定的）——拆几个由模型按
# 任务判断（真判断在它那），这里只做负载敏感的风控：平常 4、系统吃紧 2。
_SUB_COND = threading.Condition()
_SUB_ACTIVE = {"n": 0}


def _cpu_pct():
    """GetSystemTimes 采样（自包含，与 companion_rt 同款，避免反向依赖）。"""
    import ctypes as _ct

    class _FT(_ct.Structure):
        _fields_ = [("dwLow", _ct.c_ulong), ("dwHigh", _ct.c_ulong)]

    def _times():
        idle, kern, usr = _FT(), _FT(), _FT()
        _ct.windll.kernel32.GetSystemTimes(
            _ct.byref(idle), _ct.byref(kern), _ct.byref(usr))
        return ((idle.dwHigh << 32) | idle.dwLow,
                (kern.dwHigh << 32) | kern.dwLow,
                (usr.dwHigh << 32) | usr.dwLow)

    i1, k1, u1 = _times()
    time.sleep(0.2)
    i2, k2, u2 = _times()
    total = (k2 - k1) + (u2 - u1)
    return 0.0 if total <= 0 else (1.0 - (i2 - i1) / total) * 100.0


def _max_sub_agents():
    """子智能体并发上限（负载敏感保险丝，非配额）。"""
    try:
        return 2 if _cpu_pct() >= 85 else 4
    except Exception:
        return 3


def _sub_acquire():
    with _SUB_COND:
        while _SUB_ACTIVE["n"] >= _max_sub_agents():
            _SUB_COND.wait(0.5)
        _SUB_ACTIVE["n"] += 1


def _sub_release():
    with _SUB_COND:
        _SUB_ACTIVE["n"] -= 1
        _SUB_COND.notify()


def _run_delegate(task, log, cancel_check, on_activity):
    """跑一个只读子任务（depth=1：不配档不嵌套，固定轻档快打快收）。"""
    task = (task or "").strip()
    if not task:
        return "子任务为空，没跑"
    _sub_acquire()
    try:
        r = run_task(task, log=log, on_confirm=None,
                     cancel_check=cancel_check, on_activity=on_activity,
                     timeout=180, _depth=1)
    finally:
        _sub_release()
    if r.status == "completed":
        return f"子任务完成：{str(r.value)[:800]}"
    return f"子任务未完成（{r.status}）"


# 应用启动快路（2026-08-26 demo 版 = P4 launch_app 语义动作提前上线）：
# 桌面智能体开软件必须快——单一"打开X"指令零模型调用直接拉起。
# 只认精确别名（防误吞"打开记事本写句话"这类复合指令→照走模型循环）。
_LAUNCH_ALIAS = {
    "记事本": "notepad", "notepad": "notepad",
    "计算器": "calc", "calc": "calc",
    "画图": "mspaint", "画图工具": "mspaint", "mspaint": "mspaint",
    "写字板": "write",
    "资源管理器": "explorer", "文件管理器": "explorer", "我的电脑": "explorer",
    "任务管理器": "taskmgr",
    "设置": "ms-settings:", "系统设置": "ms-settings:",
    "浏览器": "msedge", "edge": "msedge",
    "命令行": "cmd", "命令提示符": "cmd", "终端": "wt",
    "powershell": "powershell", "控制面板": "control",
}
_LAUNCH_RE = re.compile(
    r"^(?:帮(?:我)?|请)?(?:快(?:速)?|马(?:上)?)?(?:打开|启动|运行|拉起|开一下|开个)"
    r"(?P<name>[A-Za-z0-9\u4e00-\u9fff .+-]{1,24}?)"
    r"(?:软件|应用|程序|app)?(?:吧|呗|谢谢|好吗|一下)?$")

# 关闭类（2026-08-26 用户令：无快捷方式软件的开闭）：名字在后/在前两种说法
_CLOSE_RE = re.compile(
    r"^(?:帮(?:我)?|请)?(?:把)?(?P<name>[A-Za-z0-9\u4e00-\u9fff .+-]{1,24}?)"
    r"(?:软件|应用|程序|app|窗口)?(?:给)?(?:关闭|关掉|关了|关一下|退出|退了|退掉)"
    r"(?:吧|呗|谢谢|好吗|一下)?$")
_CLOSE_RE2 = re.compile(
    r"^(?:帮(?:我)?|请)?(?:关闭|关掉|关了|关一下|退出|退了|退掉)"
    r"(?P<name>[A-Za-z0-9\u4e00-\u9fff .+-]{1,24}?)"
    r"(?:软件|应用|程序|app|窗口)?(?:吧|呗|谢谢|好吗|一下)?$")

# 关闭时的进程名提示（别名→exe 名片段；与 _LAUNCH_ALIAS 分离：那是启动目标）
_CLOSE_PROC_HINT = {
    "浏览器": "msedge", "edge": "msedge", "chrome": "chrome",
    "谷歌浏览器": "chrome", "b站": "bilibili", "哔哩哔哩": "bilibili",
    "bilibili": "bilibili", "微信": "weixin", "wechat": "weixin",
    "qq": "qq", "网易云音乐": "cloudmusic", "网易云": "cloudmusic",
    "记事本": "notepad", "notepad": "notepad", "计算器": "calculator",
    "calc": "calculator", "画图": "mspaint", "mspaint": "mspaint",
    "vscode": "code", "vs code": "code", "终端": "windowsterminal",
    "wt": "windowsterminal", "资源管理器": "explorer",
    "文件管理器": "explorer", "我的电脑": "explorer", "浏览器edge": "msedge",
    "word": "winword", "excel": "excel", "ppt": "powerpnt",
    "powerpoint": "powerpnt", "spotify": "spotify", "钉钉": "dingtalk",
    "飞书": "feishu", "腾讯会议": "wemeet",
}

# 快路禁入名单（2026-08-26 仿真实锤）："关闭电脑/系统"会撞上标题带"电脑"
# 的任意窗口（如此电脑）；这类词指的不是某个应用，必须回落模型循环+确认链。
_CLOSE_BLOCK = {"电脑", "系统", "主机", "所有", "全部", "一切", "电脑上"}

# 开始菜单快捷方式缓存（无桌面快捷方式软件的通用秒开路：一切已装软件
# 都在开始菜单有 .lnk——扫一次缓存 60s，命中即 cmd start，零模型调用）
_SM_DIRS = [
    os.path.join(os.environ.get("APPDATA", ""),
                 r"Microsoft\Windows\Start Menu\Programs"),
    os.path.join(os.environ.get("ProgramData", ""),
                 r"Microsoft\Windows\Start Menu\Programs"),
]
_lnk_cache = {"ts": 0.0, "items": []}


def _startmenu_lnks():
    if time.time() - _lnk_cache["ts"] < 60:
        return _lnk_cache["items"]
    items = []
    for d in _SM_DIRS:
        if not d or not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for f in files:
                if f.lower().endswith(".lnk"):
                    items.append((os.path.splitext(f)[0].lower(),
                                  os.path.join(root, f)))
    _lnk_cache.update(ts=time.time(), items=items)
    return items


def _find_startmenu_lnk(name):
    """名字 → 开始菜单 .lnk 路径。精确名优先，其次互相包含。"""
    n = name.strip().lower()
    if not n:
        return None
    items = _startmenu_lnks()
    for base, path in items:
        if base == n:
            return path
    for base, path in items:
        if n in base or base in n:
            return path
    return None


def _enum_windows_with_proc():
    """[(hwnd, title, proc_name)]——可见且有标题的顶层窗口。"""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    out = []

    def _proc(hwnd):
        try:
            import ctypes as C
            k32 = C.windll.kernel32
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, C.byref(pid))
            h = k32.OpenProcess(0x0410, False, pid.value)
            if not h:
                return ""
            try:
                buf = C.create_unicode_buffer(260)
                size = wintypes.DWORD(260)
                if k32.QueryFullProcessImageNameW(h, 0, buf, C.byref(size)):
                    return buf.value.split("\\")[-1].lower()
            finally:
                k32.CloseHandle(h)
        except Exception:
            pass
        return ""

    proc_memo = {}

    def _title(hwnd):
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value.strip()

    from ctypes import wintypes as wt
    EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, lp):
        if user32.IsWindowVisible(hwnd):
            t = _title(hwnd)
            if t:
                p = proc_memo.get(hwnd) or _proc(hwnd)
                proc_memo[hwnd] = p
                out.append((hwnd, t, p))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return out


# 批量关闭：整句"关闭所有页面/窗口"（2026-08-27 用户实测卡死根因：走模型
# 循环一窗一关 4s/步 × N 窗；批量 WM_CLOSE 一次到位，未保存由应用自问）
_CLOSE_ALL_RE = re.compile(
    r"^(?:帮(?:我)?|请)?(?:把)?(?:所有|全部)(?:的)?(?:页面|窗口|程序|应用)"
    r"(?:都|全部)?(?:关闭|关掉|关了|退出|退了|收起来|最小化)(?:吧|呗|谢谢|好吗|一下)?$")
# 动词在前的语序："关闭所有的页面"/"关掉全部窗口"
_CLOSE_ALL_RE2 = re.compile(
    r"^(?:帮(?:我)?|请)?(?:关闭|关掉|关了|退出|退了|收起来|最小化)"
    r"(?:掉|了)?(?:所有|全部)(?:的)?(?:页面|窗口|程序|应用)(?:都|全部)?"
    r"(?:关闭|关掉|关了|退出)?(?:吧|呗|谢谢|好吗|一下)?$")


def match_close_all(text):
    """批量关闭指令两种语序统一入口。"""
    t = (text or "").strip()
    return bool(_CLOSE_ALL_RE.match(t) or _CLOSE_ALL_RE2.match(t))
# 批量关不许碰的：系统桌面壳/自家进程/语音输入产品/宿主编码工具
_CLOSE_ALL_SKIP_PROC = ("progman", "workerw", "pythonw", "textinputhost",
                        "shellexperiencehost", "searchhost", "zcode",
                        "startmenuexperiencehost", "applicationframehost")


def close_all_windows(log=print):
    """批量优雅关可见顶层窗口。返回发送关闭指令数（None=枚举失败）。"""
    try:
        wins = _enum_windows_with_proc()
    except Exception as e:
        log(f"批量关闭枚举失败: {e}")
        return None
    n = 0
    for hwnd, title, proc in wins:
        if not title or any(s in proc for s in _CLOSE_ALL_SKIP_PROC):
            continue
        try:
            if _post_wm_close(hwnd):
                n += 1
        except Exception:
            pass
    log(f"批量关闭: 已发 {n} 条关闭指令（未保存内容由各应用自己弹窗）")
    return n


def _post_wm_close(hwnd):
    """WM_CLOSE 薄封装（仿真可替换；不直接碰 windll——共享对象的属性
    改型会全进程污染，老教训）。"""
    import ctypes
    return ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)


def _win_gone(hwnd):
    """窗口是否已关/不可见（关闭核验）。"""
    import ctypes
    u = ctypes.windll.user32
    return (not u.IsWindow(hwnd)) or (not u.IsWindowVisible(hwnd))


def _try_fast_close(task_text, log):
    """关闭类指令快路：找窗口 → WM_CLOSE 优雅关（未保存内容由应用自己弹窗
    问用户，不替用户决定）。找不到开着的目标=本来就关着，也算完成。"""
    m = _CLOSE_RE.match((task_text or "").strip()) \
        or _CLOSE_RE2.match((task_text or "").strip())
    if not m:
        return None
    name = m.group("name").strip()
    if name.lower() in _CLOSE_BLOCK:
        return None   # "关闭电脑/系统"不是关某个应用——回落模型循环+危险确认链
    low = name.lower()
    hints = [v for k, v in _CLOSE_PROC_HINT.items() if k in low or low in k]
    try:
        wins = _enum_windows_with_proc()
        target = None
        for hwnd, title, proc in wins:
            if any(h in proc for h in hints) or (low and low in title.lower()):
                target = (hwnd, title, proc)
                break
        if target is None:
            log(f"关闭快路: {name!r} 没有开着的窗口（视为已关）")
            return Result("completed", f"{name} 本来就没开着", set())
        hwnd, title, proc = target
        ok = _post_wm_close(hwnd)
        time.sleep(0.4)
        gone = _win_gone(hwnd)
        if ok and gone:
            log(f"关闭快路: 已关闭 {proc}《{title[:24]}》")
            return Result("completed", f"已关闭 {name}", set())
        if ok:
            log(f"关闭快路: 关闭指令已发（{proc} 可能有未保存内容在等确认）")
            return Result("completed",
                          f"已发关闭指令，{name} 可能有未保存内容在等你确认", set())
        return None   # 发送失败（提权窗口）→ 回落模型循环如实处理
    except Exception as e:
        log(f"关闭快路失败（回落模型循环）: {e}")
        return None


def find_file(query, log=print, limit=5):
    """全电脑模糊文件搜索（2026-08-27 用户令：藏得深/信息少都要快速找到）。
    索引 D:\\Desktop + 用户 Desktop/Documents/Downloads（剪枝 node_modules 等），
    30 分钟缓存。匹配两层：文件名（全等 > 前缀 > 子串 > 相似度）+ 相对路径
    词（目录名也算线索）；浅路径优先。"""
    q = (query or "").strip().lower()
    if not q:
        return []
    m = _build_file_index(log)
    import difflib
    scored = []
    for base, paths in m.items():
        if q == base:
            s = 100.0
        elif q in base:
            s = 70.0 + (10.0 if base.startswith(q) else 0.0)
        else:
            r = difflib.SequenceMatcher(None, q, base).ratio()
            s = r * 50.0 if r >= 0.62 else 0.0
        for p in paths:
            score = s
            # 路径词匹配：文件名不含线索但路径（目录名）含也算命中
            if score < 70.0 and q in p.lower():
                score = max(score, 60.0)
            if score <= 0.0:
                continue
            scored.append((score - min(len(p), 260) * 0.01, p))
    scored.sort(reverse=True)
    return [p for _, p in scored[:limit]]


_FILE_INDEX = {"ts": 0.0, "map": None}
_INDEX_ROOTS = None   # 首次构建时解析（桌面=注册表真值，见 _desktop_dir）


def _index_roots():
    global _INDEX_ROOTS
    if _INDEX_ROOTS is None:
        home = os.path.expanduser("~")
        _INDEX_ROOTS = [_desktop_dir(),
                        os.path.join(home, "Documents"),
                        os.path.join(home, "Downloads")]
    return _INDEX_ROOTS
_INDEX_PRUNE = {"node_modules", ".git", "__pycache__", ".venv", "site-packages",
                "$recycle.bin", "system volume information", ".tmp", ".cache",
                "runtime"}
_INDEX_TTL = 1800.0


def _build_file_index(log=print):
    if _FILE_INDEX["map"] is not None and time.time() - _FILE_INDEX["ts"] < _INDEX_TTL:
        return _FILE_INDEX["map"]
    m = {}
    for root_dir in _index_roots():
        if not os.path.isdir(root_dir):
            continue
        for root, dirs, files in os.walk(root_dir):
            dirs[:] = [d for d in dirs if d.lower() not in _INDEX_PRUNE]
            for f in files:
                m.setdefault(os.path.splitext(f)[0].lower(), []).append(
                    os.path.join(root, f))
    _FILE_INDEX.update(ts=time.time(), map=m)
    log(f"文件索引就绪: {sum(len(v) for v in m.values())} 文件 / {len(m)} 名字")
    return m


# 模糊直开的安全扩展名（.exe 不在——深路径模糊搜到的 exe 不许盲开）
_SAFE_OPEN_EXT = {".html", ".htm", ".pdf", ".docx", ".doc", ".xlsx", ".xls",
                  ".pptx", ".ppt", ".md", ".txt", ".csv", ".png", ".jpg",
                  ".jpeg", ".url", ".lnk", ".mp4", ".mp3", ".wav"}
# 路由脑补噪声词（"打开桌面上的报名表文件（可能是Excel或Word格式）"→"报名表"）
_NAME_NOISE = ["可能", "格式", "文件", "表格", "文档", "桌面", "目录", "找到",
               "展示", "默认程序", "默认", "程序", "打开", "并", "一下", "用户",
               "帮忙", "帮", "请", "让", "它", "用", "excel", "word", "pdf",
               "or", "和", "以及", "后", "给", "看", "d盘", "c盘", "桌面上"]


def _try_open_by_fuzzy(task_text, log):
    """打开/找 + 文件名（允许路由脑补修饰）→ 模糊文件搜索直开，零模型调用。
    这是"打开报名表"事件（2026-08-27：111 秒模型长征）的根治层。"""
    if not re.search(r"打开|找出|找到|找一下|开一下", task_text or ""):
        return None
    s = task_text.lower()   # 噪声词表按小写匹配（Excel/Word 大小写都剥）
    for w in sorted(_NAME_NOISE, key=len, reverse=True):
        s = s.replace(w, " ")
    cands = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,24}", s)
    if not cands:
        return None
    name = max(cands, key=len)
    paths = [p for p in find_file(name, log=log, limit=3)
             if os.path.splitext(p)[1].lower() in _SAFE_OPEN_EXT]
    if not paths:
        return None
    target = paths[0]
    try:
        subprocess.Popen(["cmd", "/c", "start", "", target],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log(f"模糊找开({name!r}) → {target}")
        where = os.path.basename(os.path.dirname(target))
        return Result("completed", f"找到并打开了 {name}（在 {where} 目录）",
                      set())
    except Exception as e:
        log(f"模糊找开失败: {e}")
        return None


def _launch_target(name):
    """名字 → (启动目标, 方式)。别名 → 开始菜单 .lnk → 模糊文件搜索（安全
    扩展名）三级。找不到 None。"""
    low = name.strip().lower()
    target = _LAUNCH_ALIAS.get(low)
    if target:
        return target, "别名"
    lnk = _find_startmenu_lnk(low)
    if lnk:
        return lnk, "开始菜单"
    for p in find_file(low, limit=3):
        if os.path.splitext(p)[1].lower() in _SAFE_OPEN_EXT:
            return p, "文件搜索"
    return None, None


def launch_app_by_name(name, log=print, verify_window=True):
    """按名字启动应用（gui_agent 的 launch_app 动作与快路共用这一份能力表）。
    速度优先（2026-08-27 用户令：还是太慢）：Popen 即返回即播报"已启动"；
    窗口出现核验放后台线程（发现没起来也只记日志——用户看得见，纠正
    走下一句指令）。Result.tools 置空 = companion 视为已验证立即播报。"""
    target, how = _launch_target(name)
    if not target:
        return None
    try:
        # 2026-08-31 验收实锤：后台上下文 cmd start 拉起的窗口沉底不抢前台，
        # 被现有前台窗口整个盖住=模型/用户都看不见。启动前快照窗口集（hwnd-only
        # 裸枚举，微秒级——带进程名的慢枚举会拖垮"启动即返回"指标），核验
        # 线程发现新窗口时主动提到前台（host_input.activate_hwnd）。
        import ctypes as _ct
        from ctypes import wintypes as _wt
        _u32 = _ct.windll.user32
        _before = []

        @_ct.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
        def _snap_cb(hwnd, _lp):
            _before.append(hwnd)
            return True

        _u32.EnumWindows(_snap_cb, 0)
        before_hwnds = set(_before)
        subprocess.Popen(["cmd", "/c", "start", "", target],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log(f"启动({how}): {name!r} → {target}")
        if verify_window:

            def _bg_check():
                try:
                    deadline = time.time() + 2.5
                    while time.time() < deadline:
                        time.sleep(0.3)
                        for _hwnd, _t, proc in _enum_windows_with_proc():
                            if _hwnd in before_hwnds:
                                continue
                            if proc != "explorer.exe" and (
                                    _CLOSE_PROC_HINT.get(
                                        name.strip().lower(), "") in proc
                                    or how == "别名"):
                                try:
                                    import host_input
                                    host_input.activate_hwnd(_hwnd)
                                except Exception:
                                    pass
                                return
                    log(f"启动后 {name!r} 2.5s 内未见新窗口（记档）")
                except Exception:
                    pass

            threading.Thread(target=_bg_check, daemon=True).start()
        return Result("completed", f"已启动 {name.strip()}", set())
    except Exception as e:
        log(f"启动失败: {e}")
        return None


def _try_fast_launch(task_text, log):
    """单一启动指令 → 直接拉起（不经模型循环）。命中返回 Result，未命中 None。
    两级：内置别名 → 开始菜单 .lnk（一切已装软件都在那里，无快捷方式也秒开）。"""
    m = _LAUNCH_RE.match((task_text or "").strip())
    if not m:
        return None
    name = m.group("name").strip()
    r = launch_app_by_name(name, log=log, verify_window=True)
    if r is None:
        return None
    return r


def _activity_desc(name, args):
    """面板/步骤的人话描述（2026-08-29 用户令：文字框不许出现 bash 这类
    技术名词——在干什么、动哪个文件，一眼看懂）。"""
    def _base(p):
        p = str(p or "").replace("\\", "/").rstrip("/")
        return p.rsplit("/", 1)[-1] or p
    if name == "bash":
        return f"运行命令：{str(args.get('command', ''))[:30]}"
    if name == "fs_read":
        return f"读文件：{_base(args.get('path'))}"
    if name == "fs_write":
        return f"写入文件：{_base(args.get('path'))}"
    if name == "fs_edit":
        return f"编辑文件：{_base(args.get('path'))}"
    if name == "fs_list":
        return f"查看目录：{_base(args.get('path'))}"
    if name == "grep":
        return f"搜索代码：{str(args.get('pattern', ''))[:24]}"
    if name == "web_search":
        return f"联网搜索：{str(args.get('query', ''))[:24]}"
    if name == "screen_qa":
        return "看屏幕"
    return f"执行：{name}"


# --- 能力件库（2026-08-31 L1，前台能力重构计划）：能力=数据不是代码。
# capabilities.jsonl 每件：{id,name,intents[],risk(low/system),func}。
# 命中=确定性执行（系统接口/命令）亚秒级 + 确定性验证（读回/落盘）；
# 高危口径照权限档（risk=system 在 confirm/auto 档过语音确认，yolo 可逆放行，
# 与 _gate 终裁同口径）；low 不问。命中不了→None 落回模型循环（视觉路兜底）。
_CAP_PATH = os.path.join(_PROJECT_ROOT, "capabilities.jsonl")
_CAP_CACHE = {"mtime": 0.0, "items": []}


def _load_capabilities():
    try:
        m = os.path.getmtime(_CAP_PATH)
        if m != _CAP_CACHE["mtime"]:
            items = []
            with open(_CAP_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        items.append(json.loads(line))
            _CAP_CACHE.update(mtime=m, items=items)
    except (OSError, ValueError):
        _CAP_CACHE.update(mtime=0.0, items=[])
    return _CAP_CACHE["items"]


def _try_capability(task_text, log, on_confirm, task_approved):
    """能力件快路：命中意图→确定性执行+验证。返回 Result 或 None（落回模型）。"""
    for cap in _load_capabilities():
        try:
            if any(re.search(p, task_text or "") for p in cap.get("intents", [])):
                return _run_capability(cap, task_text, log, on_confirm,
                                       task_approved)
        except re.error:
            continue
    return None


def _try_graph(task_text, log, on_confirm, task_approved, on_step=None,
               cancel_check=None):
    """图工程渐进换心·打样（2026-08-31 阶段 B）：能力件命中的任务走"单节点图"
    （capability 节点 + generic 回退边=转旧模型循环），节点级状态可见。
    未命中/灰度关闭/图失败 → None（落回旧模型循环，行为与图前完全一致）。"""
    import task_graph
    if not task_graph.graph_engine_on():
        return None
    for cap in _load_capabilities():
        try:
            if not any(re.search(p, task_text or "")
                       for p in cap.get("intents", [])):
                continue
        except re.error:
            continue
        node = task_graph.make_node(
            cap.get("name", cap.get("id")), "capability",
            args={"cap": cap, "task_text": task_text,
                  "on_confirm": on_confirm, "task_approved": task_approved},
            fallback="generic", node_id=f"cap-{cap.get('id', '?')}")
        graph = task_graph.make_graph(task_text, [node])

        def _on_node(n):
            if on_step:
                mark = "√" if n["state"] == "done" else "×"
                on_step(f"{mark} 节点 {n['goal'][:18]}"
                        + ("" if n["state"] == "done"
                           else f"（{n['result'][:24]}）"))
        ok, graph = task_graph.walk(graph, log=log, on_node=_on_node,
                                    cancel_check=cancel_check)
        if ok:
            last = graph["nodes"][-1]
            return Result("completed", str(last["result"]),
                          {f"capability:{cap.get('id')}", "graph"})
        return None   # 图失败=落回旧模型循环（generic 回退边在层外闭环）
    return None


def _run_capability(cap, task_text, log, on_confirm, task_approved):
    cid = cap.get("id", "?")
    fn = _CAP_FUNCS.get(cap.get("func"))
    if fn is None:
        log(f"能力件 {cid}: 实现函数缺失，落回模型")
        return None
    desc = f"{cap.get('name', cid)}（{task_text[:24]}）"
    # 权限档口径：system=confirm/auto 档过语音确认、yolo 可逆放行；low=不问
    risk = cap.get("risk", "system")
    mode = permission_mode.current()
    if risk == "system" and mode in ("confirm", "auto"):
        key = f"cap:{cid}"
        if not (mode == "auto" and task_approved is not None
                and key in task_approved):
            if on_confirm is None:
                log(f"能力件 {cid} 无确认链，fail-closed 落回模型")
                return None
            if not on_confirm(desc[:60]):
                return Result("cancelled", None, {f"capability:{cid}"})
            if mode == "auto" and task_approved is not None:
                task_approved.add(key)
    log(f"能力件命中: {cid}（{desc}）")
    try:
        ok, msg = fn(task_text, log)
    except Exception as e:
        ok, msg = False, f"能力件 {cid} 执行异常: {e}"
    if ok:
        log(f"能力件完成: {cid} — {msg[:60]}")
        return Result("completed", msg, {f"capability:{cid}"})
    log(f"能力件失败（落回模型兜底）: {cid} — {msg[:60]}")
    return None   # 失败落回模型循环/视觉路（不谎称完成）


# ---- 薄包装（仿真可 mock；真实副作用只走这几个口子） ----
def _spi_set_wallpaper(path):
    """SystemParametersInfo 设壁纸（立即生效+写配置）。"""
    import ctypes
    return bool(ctypes.windll.user32.SystemParametersInfoW(0x0014, 0, path, 3))


def _read_wallpaper_registry():
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop")
        v, _ = winreg.QueryValueEx(k, "Wallpaper")
        winreg.CloseKey(k)
        return str(v)
    except OSError:
        return ""


def _read_current_wallpaper():
    """当前壁纸真值（2026-08-31 实锤：Win11 24H2 上 Control Panel Desktop 的
    Wallpaper 是滞后旧值，Explorer Wallpapers 的 BackgroundHistoryPath0 才是当前）。"""
    import winreg
    try:
        k = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Wallpapers")
        v, _ = winreg.QueryValueEx(k, "BackgroundHistoryPath0")
        winreg.CloseKey(k)
        if v:
            return str(v)
    except OSError:
        pass
    return _read_wallpaper_registry()   # 老 Windows 回退


def _write_theme_light(v):
    import winreg
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                       r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                       0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(k, "AppsUseLightTheme", 0, winreg.REG_DWORD, v)
    winreg.SetValueEx(k, "SystemUsesLightTheme", 0, winreg.REG_DWORD, v)
    winreg.CloseKey(k)


def _read_theme_light():
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
        winreg.CloseKey(k)
        return int(v)
    except OSError:
        return -1


def _broadcast_settingchange():
    """广播 WM_SETTINGCHANGE 让主题立即生效（不重开资源管理器）。"""
    import ctypes
    ctypes.windll.user32.SendMessageTimeoutW(
        0xFFFF, 0x001A, 0, "ImmersiveColorSet", 0x0002, 2000, None)


_VOL_SCAN = {0xAF: 0x30, 0xAE: 0x2E, 0xAD: 0x20}   # E0 前缀媒体键的硬件扫描码


def _vol_key(vk, n):
    """媒体键发键（带扫描码+扩展键标志——裸 vk 在部分机器不注册，实锤"不工作"）。"""
    import ctypes
    scan = _VOL_SCAN[vk]
    EXT = 0x0001  # KEYEVENTF_EXTENDEDKEY
    for _ in range(n):
        ctypes.windll.user32.keybd_event(vk, scan, EXT, 0)
        ctypes.windll.user32.keybd_event(vk, scan, EXT | 0x0002, 0)


# ---- 能力件实现 ----
_COLOR_WORDS = {
    "黑": (16, 16, 20), "深灰蓝": (20, 24, 34), "深蓝": (16, 30, 58),
    "蓝": (25, 60, 120), "绿": (22, 62, 42), "红": (120, 30, 30),
    "紫": (64, 42, 92), "白": (238, 238, 240), "灰": (88, 90, 96),
}


def cap_wallpaper(task_text, log):
    """换壁纸：指定图片路径/文件名，或纯色（颜色词→生成纯色 PNG）。
    验证=注册表读回 Wallpaper 值（确定性）。"""
    from PIL import Image
    m_path = re.search(r"([A-Za-z]:[\\/][^，。\"'\s]+?\.(?:png|jpg|jpeg|bmp))",
                       task_text or "", re.I)
    if m_path and os.path.isfile(m_path.group(1)):
        path = os.path.abspath(m_path.group(1))
        label = os.path.basename(path)
    elif re.search(r"纯色|深色|黑|蓝|绿|红|紫|白|灰", task_text or ""):
        rgb = (20, 24, 34)
        cname = "深灰蓝"
        for w, c in _COLOR_WORDS.items():
            if w in task_text:
                rgb, cname = c, w
                break
        path = os.path.join(_PROJECT_ROOT, "workspace", f"壁纸-纯色{cname}.png")
        Image.new("RGB", (1920, 1080), rgb).save(path)
        label = f"纯色{cname}"
    else:
        return False, "没说清换成什么壁纸——指定图片路径或说个颜色（如'换个深蓝色壁纸'）"
    if not _spi_set_wallpaper(path):
        return False, "SystemParametersInfo 调用失败（系统接口拒绝）"
    if os.path.normpath(_read_current_wallpaper()) != os.path.normpath(path):
        return False, "设置调用返回成功但读回不是新值（没真生效）"
    return True, f"壁纸已换成{label}"


def cap_volume(task_text, log):
    """调音量（媒体键层）：调大/调小/静音；绝对值=先归零再步进到目标（每格 2%，
    Windows 固定步进）。COM 音量接口在这台机器（Senary/Intel 智音 DSP 音频栈）
    E_INVALIDARG 实锤六探针全灭——媒体键是这台机器上唯一可靠路（台账在案）。"""
    t = task_text or ""
    if re.search(r"取消静音|解除静音|别静音|不静音|恢复声音", t):
        _vol_key(0xAD, 1)
        return True, "已按静音键恢复声音（没恢复就再叫我一声）"
    if re.search(r"静音", t):
        _vol_key(0xAD, 1)
        return True, "已静音（要恢复就说'取消静音'）"
    m = (re.search(r"调[到至成]?百分之?(\d{1,3})", t)
         or re.search(r"(\d{1,3})\s*%", t)
         or (type("M", (), {"group": lambda s, i: "50"})()
             if re.search(r"一半", t) else None))
    if m:
        target = max(0, min(100, int(m.group(1))))
        _vol_key(0xAE, 50)              # 先归零（2%×50=100%）
        time.sleep(0.1)
        _vol_key(0xAF, round(target / 2))   # 再步进到目标
        return True, f"音量已调到约 {target}%（先归零再步进，固定 2% 步进）"
    if re.search(r"大|高|响|加|开[一点]*", t):
        n = 5 if re.search(r"一点|一些", t) else 10
        _vol_key(0xAF, n)
        return True, f"音量调大 {n} 格（约 +{n * 2}%）"
    if re.search(r"小|低|轻|减", t):
        n = 5 if re.search(r"一点|一些", t) else 10
        _vol_key(0xAE, n)
        return True, f"音量调小 {n} 格（约 -{n * 2}%）"
    return False, "没说清要调大还是调小（或'静音'/'调到百分之几'）"


def _win_d():
    """Win+D 一键（薄包装，仿真可 mock）。"""
    import ctypes
    u32 = ctypes.windll.user32
    u32.keybd_event(0x5B, 0, 0, 0)
    u32.keybd_event(ord("D"), 0, 0, 0)
    u32.keybd_event(ord("D"), 0, 0x0002, 0)
    u32.keybd_event(0x5B, 0, 0x0002, 0)


def _foreground_class():
    """前台窗口类名（薄包装）。"""
    import ctypes
    u32 = ctypes.windll.user32
    buf = ctypes.create_unicode_buffer(64)
    u32.GetClassNameW(u32.GetForegroundWindow(), buf, 64)
    return buf.value


def cap_show_desktop(task_text, log):
    """回到桌面（Win+D 毫秒级——2026-08-31 实锤：这类事派 46s GUI 漫游是路线错配）。
    验证=前台窗口类名落回 Progman/WorkerW（真到桌面）。"""
    _win_d()
    time.sleep(0.4)
    if _foreground_class() in ("Progman", "WorkerW"):
        return True, "已回到桌面"
    return True, "已按 Win+D（前台还有窗口的话就再按一次）"


def cap_dark_mode(task_text, log):
    """深色/浅色模式（注册表+广播即时生效）。验证=读回 AppsUseLightTheme。"""
    on = not re.search(r"浅色|关闭|关掉|换浅", task_text or "")
    v = 0 if on else 1
    _write_theme_light(v)
    _broadcast_settingchange()
    if _read_theme_light() != v:
        return False, "注册表写入后读回不一致（没真生效）"
    return True, "深色模式已开" if on else "已切回浅色模式"


_CAP_FUNCS = {"cap_wallpaper": cap_wallpaper, "cap_volume": cap_volume,
              "cap_dark_mode": cap_dark_mode,
              "cap_show_desktop": cap_show_desktop}


def _register_graph_executors():
    """图执行器注册（阶段 B，幂等）：capability=能力件确定性执行（含闸与验证）；
    generic=回退边出口标记（转旧模型循环——B3 阶段全图化前不在图内递归）。"""
    import task_graph

    def _exec_capability(node, log):
        a = node["args"]
        r = _run_capability(a["cap"], a["task_text"], log,
                            a.get("on_confirm"), a.get("task_approved"))
        if r is None:
            return False, "能力件未过（落回退边）"
        return (r.status == "completed"), str(r.value)

    task_graph.register_executor("capability", _exec_capability)
    task_graph.register_executor(
        "generic", lambda node, log: (False, "转通用模型循环"))


_register_graph_executors()


def run_task(task_text, log=print, on_step=None, on_confirm=None,
             cancel_check=None, execution_context=None, timeout=240,
             on_activity=None, _depth=0):
    """执行一个后台任务。返回 Result(status, value, tools)。
    status: completed / cancelled / failed。
    on_activity(desc|None)：不可见工作实况（沉默可见化）——等模型/执行工具
    都上报，None 表示清空。
    _depth=1 是 delegate 子任务：只读工具面、不配档、无确认链、不能再拆。"""
    def _activity(desc):
        if on_activity:
            try:
                on_activity(desc)
            except Exception:
                pass

    used_tools = set()
    task_approved = set() if _depth == 0 else None  # auto 档任务级授权记忆
    last = {"desc": "刚开工"}   # 卡点追踪（2026-08-30 用户铁律：失败必须说清卡在哪）
    t0 = time.time()
    if _depth == 0:
        fast = _try_fast_launch(task_text, log=log)
        if fast is None:
            fast = _try_fast_close(task_text, log=log)
        if fast is None:
            # 模糊文件直开：接住路由脑补过的话术（"打开桌面上的X文件（可能
            # 是Excel格式）"）——"打开报名表"111 秒长征的根治层
            fast = _try_open_by_fuzzy(task_text, log=log)
        if fast is not None:
            _activity(None)
            if on_step:
                on_step("已启动（快路）")
            return fast
        cap = _try_graph(task_text, log=log, on_confirm=on_confirm,
                         task_approved=task_approved, on_step=on_step,
                         cancel_check=cancel_check)
        if cap is not None:
            _activity(None)
            return cap
        _activity("评估任务难度…")
        model, thinking, effort = _plan(task_text, log=log)
        if _code_task_gate(task_text):
            # 改代码要 读多轮→编辑→跑测试自验，240s 实锤不够（224.3s 白跑）。
            timeout = max(timeout, 900)
        schemas = _TOOLS_SCHEMA + [_DELEGATE_SCHEMA]
        # 轮次按档给（2026-08-28 自迭代实锤：改代码 12 轮读不完文件就超轮
        # 假死）：重档 28 轮，轻档 16 轮。
        max_rounds = 28 if model == _MODEL_PRO else 16
    else:
        model, thinking, effort = _MODEL, {"type": "disabled"}, None
        schemas = [t for t in _TOOLS_SCHEMA
                   if t["function"]["name"] in _SUB_SAFE]
        max_rounds = _MAX_ROUNDS
    messages = [
        {"role": "system", "content": soul.dsh_prompt()},
        {"role": "user", "content": task_text},
    ]
    for rnd in range(1, max_rounds + 1):
        if cancel_check and cancel_check():
            _activity(None)
            return Result("cancelled", None, used_tools)
        if time.time() - t0 > timeout:
            log("exec-native: 超时")
            _activity(None)
            return Result("failed",
                          f"{timeout}s 内没跑完，卡在「{last['desc']}」",
                          used_tools)
        _activity("思考中（等模型回复）…" if rnd == 1
                  else f"思考中（第 {rnd} 轮）…")
        # 长轮次心跳：等模型期间每 3s 刷面板计时，用户看得到它活着
        _tick = {"on": True}

        def _ticker():
            n = 0
            while _tick["on"]:
                time.sleep(3)
                n += 3
                if _tick["on"]:
                    _activity(f"思考中（第 {rnd} 轮，已等 {n}s）…")
        threading.Thread(target=_ticker, daemon=True).start()
        try:
            msg = _deepseek_call(messages, schemas, model=model,
                                 thinking=thinking, effort=effort)
        except Exception as e:
            log(f"exec-native: 模型调用失败: {e}")
            _activity(None)
            return Result("failed",
                          f"模型调用失败（{e}），卡在「{last['desc']}」",
                          used_tools)
        finally:
            _tick["on"] = False
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            final = (msg.get("content") or "").strip()
            _activity(None)
            if final:
                log(f"exec-native: 完成 ({time.time()-t0:.1f}s, {rnd}轮"
                    f"{'，子任务' if _depth else ''})")
                return Result("completed", final, used_tools)
            return Result("failed", None, used_tools)
        messages.append({"role": "assistant",
                         "content": msg.get("content") or "",
                         **({"reasoning_content": msg["reasoning_content"]}
                            if msg.get("reasoning_content") else {}),
                         "tool_calls": tool_calls})
        parsed = []
        for tc in tool_calls:
            fn = tc.get("function") or {}
            bad_args = None
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError as e:
                args = {}
                # 2026-08-28 实锤：fs_write 整文件重写大代码文件时 arguments
                # 被模型输出上限截断→解析失败。带着空 args 继续会连环触发
                # "范围{}"确认（用户听不懂）→任务白跑；必须 fail-closed 回模型。
                bad_args = str(e)
            parsed.append((tc, fn.get("name", ""), args, bad_args))
        # 主模型一轮派多个 delegate → 真并发（信号量封顶）
        if (_depth == 0 and len(parsed) > 1
                and all(n == "delegate" for _, n, _, _ in parsed)):
            if cancel_check and cancel_check():
                _activity(None)
                return Result("cancelled", None, used_tools)
            used_tools.add("delegate")
            if on_step:
                try:
                    on_step(f"拆成 {len(parsed)} 个子任务并行")
                except Exception:
                    pass
            _activity(f"并发跑 {len(parsed)} 个子任务…")
            results = [None] * len(parsed)

            def _one(i, sub):
                results[i] = _run_delegate(sub, log, cancel_check, _activity)

            threads = [threading.Thread(
                target=_one, args=(i, a.get("task", "")), daemon=True)
                for i, (_, _, a, _) in enumerate(parsed)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            for (tc, _, _), res in zip(parsed, results):
                messages.append({"role": "tool",
                                 "tool_call_id": tc.get("id"),
                                 "content": str(res)[:4000]})
            continue
        for tc, name, args, bad_args in parsed:
            if cancel_check and cancel_check():
                _activity(None)
                return Result("cancelled", None, used_tools)
            if bad_args is not None:
                messages.append({
                    "role": "tool", "tool_call_id": tc.get("id"),
                    "content": f"工具 {name} 的参数 JSON 解析失败（{bad_args[:60]}）"
                               "——通常是参数太大被输出上限截断。改用更小的调用重试："
                               "改现有文件用 fs_edit 只传补丁片段，不要 fs_write 整文件。"})
                continue
            lacking = [k for k in _REQUIRED_ARGS.get(name, ())
                       if not str(args.get(k) or "")]
            if lacking:
                messages.append({
                    "role": "tool", "tool_call_id": tc.get("id"),
                    "content": f"工具 {name} 缺必填参数 {lacking}，补全后重试。"})
                continue
            if name == "delegate":
                if _depth != 0:
                    result_text = "子任务不能再派子任务"
                else:
                    used_tools.add("delegate")
                    if on_step:
                        try:
                            on_step(f"子任务：{str(args.get('task', ''))[:24]}")
                        except Exception:
                            pass
                    _activity(f"子任务：{str(args.get('task', ''))[:24]}")
                    result_text = _run_delegate(str(args.get("task", "")),
                                                log, cancel_check, _activity)
                messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                                 "content": result_text[:4000]})
                continue
            impl = _TOOLS_IMPL.get(name)
            if impl is None or (_depth != 0 and name not in _SUB_SAFE):
                result_text = (f"未知工具 {name}" if impl is None
                               else f"子任务不允许用 {name}（只读面外）")
            else:
                used_tools.add(name)
                _desc = _activity_desc(name, args)
                last["desc"] = _desc   # 卡点追踪：最后一个真实活动
                if on_step:
                    try:
                        on_step(_desc)
                    except Exception:
                        pass
                _activity(_desc)
                # 子任务全是只读工具，免闸；主任务照常过闸
                if _depth != 0 or _gate(name, args, on_confirm, log,
                                        task_approved):
                    try:
                        # 面板心跳（2026-08-28 实锤：长 bash（Git 备份/语言包
                        # 下载）阻塞期间面板零刷新，用户视角"文字框定住了"）。
                        # 报真实已耗时，不造假进度（用户明令：一切要真的）。
                        _hb = {"on": True}
                        _hb_desc = _desc

                        def _heartbeat():
                            n = 0
                            while _hb["on"]:
                                time.sleep(3)
                                n += 3
                                if _hb["on"]:
                                    _activity(f"{_hb_desc}（已 {n}s）")
                        threading.Thread(target=_heartbeat, daemon=True).start()
                        try:
                            result_text = str(impl(args))
                        finally:
                            _hb["on"] = False
                    except Exception as e:
                        result_text = f"工具执行失败: {e}"
                else:
                    # 安全闸拒放=立即收任务（2026-08-22 卡死教训：拒绝后
                    # 模型空转 12 轮烧到超轮次上限，用户对着死任务干等）。
                    # 取消路径各自已播过报（确认超时/拒绝/叫停），这里静默收尾。
                    log(f"exec-native: 安全闸未放行，任务终止（{name}）")
                    _activity(None)
                    return Result("cancelled", None, used_tools)
            messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                             "content": result_text[:4000]})
    log("exec-native: 超轮次上限")
    _activity(None)
    return Result("failed",
                  f"{max_rounds} 轮没跑完，卡在「{last['desc']}」",
                  used_tools)


def register(ctx):
    """内核插件形态：提供 exec-native 服务。
    web_search 单独暴露：语音层 FC（voice-fc）复用同一联网能力，
    不重复造轮子。scaffold 判定件经 ctx.svc 懒取（可热插拔）。"""
    global _svc_getter
    _svc_getter = ctx.svc
    ctx.provide("exec-native", {"run_task": run_task,
                                "web_search": _t_web_search})
