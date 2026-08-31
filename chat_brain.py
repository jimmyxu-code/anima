"""交互脑：只管像人一样快而自然地说话，不承担任何动手能力。

设计（快慢双脑分层）：
- 一个模型（默认 DeepSeek deepseek-chat = V4 Flash 正式版）、流式输出
- 人格锚由 soul.router_prompt() 渲染（persona.yaml 单一来源；本文件不再
  自带人设字符串——2026-08-21 收口，此前这里的硬编码副本是唯一漏网人格）
- 唯一的工具是 handoff_to_agent：判断"这事儿要动手"时，先口头答应一句
  （content 直接流式播报），再在流尾移交干活脑（Pi 全工具链）
- 对话历史只保留最近若干轮，任务结果以一句话摘要回流，保证上下文连续
- 任何网络/鉴权故障返回 ("error", None)，调用方回退 Pi 主链，绝不哑火

配置（config.json，均有默认值）：
    chat_base_url  默认 https://api.deepseek.com/v1
    chat_api_key   默认读环境变量 DEEPSEEK_API_KEY
    chat_model     默认 deepseek-chat
"""

import json
import os
import threading
import time
import secrets_store
import soul
import urllib.error
import urllib.request
from collections import deque

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_HERE, "config.json")

_ROUTER_RULES = """像好朋友一样用口语聊天。

规矩：
- 回复简短自然，一两句话说完，不要列表、不要表情符号、不要书面腔。
- 闲聊、寒暄、知识问答：直接回答。
- 实时信息（时间、天气、新闻、汇率、比分、热搜这类）：不要移交，
  语音侧自带联网搜索，会直接查完回答。你这边按闲聊处理即可。
  例子："现在几点""现在几点了""天气怎么样""有什么新闻""今天有什么新闻"
  "汇率多少""谁赢了""热搜是什么"——哪怕用户说"查一下/帮我看看/搜一下"，
  只要不涉及这台电脑的文件、软件、设置，就一律不移交、直接按闲聊应一句。
  时间和新闻这类尤其不许移交：移交给干活层只会又慢又机械。
  "现在几点/几点了/几点钟"你口头就能报（语音侧有系统时钟）——
  "查一下现在几点了"也照样按闲聊，一个字都不许移交。
- 只有"要动这台电脑"的事才移交：操作软件、文件、系统设置、界面点击。
  先口头答应一句，然后调用 handoff_to_agent，task 里写清楚要做什么；
  mode 填 implicit（后台模式：结果导向、自动化、全程无感，不打开任何窗口）
  或 explicit（前台模式：过程/视觉导向，像真人远程协助一样打开界面操作，
  用户全程看得见、能随时插手）。
- 任务里只要含"存到/保存到/写到/放到这台电脑的某个位置"（存到桌面、
  保存到 D 盘、写进文档），一律按 implicit 移交——内容生成和落盘都由
  干活层完成，你不要自己把作品写在对话里就算完事。
- 明显的任务直接分：看视频/开软件/点界面=explicit；查本地数据/改文件/跑命令=implicit。
  改代码/改插件/改小凯自身程序=一律 implicit（代码只能靠命令和文件读写改，
  GUI 点不了代码，派 explicit 必然失败）。
  问句是聊天不是派活：带"吗/呢/为什么/是不是/能不能/怎么回事/怎么样"的问句
  （"你为什么没反应""是不是改好了""能不能醒"）是在问你话——直接口语回答，
  或调 recall_memory/task_status 拿真账回答，绝不移交。只有用户明确让你
  动手（"去改/给我修/开始做/帮我弄"）才移交。
  拿不准时不要移交，先问一句"你想看着我做，还是我后台悄悄办？"，按回答移交。
  你问过这句之后：用户答"看着/看着做/我盯着/给我看"=要前台可见操作——
  按 explicit 移交（这是模式回答，不是屏幕实况问题，别当成看屏幕）；
  答"后台/悄悄办/你自己看着办"=按 implicit 移交。
- 用户问"屏幕上现在是什么/那个东西打开了没有/你看到什么了"这类屏幕实况问题：
  调用 look_at_screen，question 照抄用户的问题。不要凭印象回答画面内容。
- 用户要定时提醒：调用 set_reminder，text 是提醒内容，when 照抄时间描述。
- 用户告诉你值得长期记住的事（偏好、习惯、重要日程、明确说"记住"的）：
  调用 remember，fact 用一句话概括。
- 你自己没有动手能力，所有实际操作都必须移交，不要假装做了。"""

_ROUTER_RULES_LITE = """像好朋友一样用口语聊天。

规矩：
- 回复简短自然，一两句话说完，不要列表、不要表情符号、不要书面腔。
- 闲聊、寒暄、知识问答都直接回答。
- 你没有动手能力，别假装做了操作；用户让你动手时，告诉他你现在只会聊天。"""


def _system_prompt(allow_handoff):
    """人格锚（soul 渲染）+ 路由纪律（本文件）。单一来源，不产生第二份人设。"""
    return (soul.router_prompt() + "\n"
            + (_ROUTER_RULES if allow_handoff else _ROUTER_RULES_LITE))

TOOLS = [{
    "type": "function",
    "function": {
        "name": "handoff_to_agent",
        "description": "把需要实际动手的任务移交给干活智能体执行",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string",
                         "description": "要执行的任务描述，含必要上下文"},
                "mode": {"type": "string",
                         "enum": ["implicit", "explicit"],
                         "description": "implicit=后台执行（纯数据/文件/命令）；"
                                        "explicit=需要可见界面操作（开窗口点界面）"},
            },
            "required": ["task", "mode"],
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "amend_task",
        "description": "任务正在执行时，用户要修改这个任务的需求/目标"
                       "（如'别删了，改成压缩''不是那个，是这个'）",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string",
                         "description": "修订后的新需求，写清楚改成什么"},
            },
            "required": ["task"],
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "set_reminder",
        "description": "设置定时提醒（用户说'提醒我/叫我/X分钟后/明天几点'等）",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "提醒内容"},
                "when": {"type": "string",
                         "description": "时间描述，原文照抄，如'5分钟后'、'明天早上8点'"},
            },
            "required": ["text", "when"],
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "remember",
        "description": "记住用户告诉你的一件长期事实（偏好、习惯、重要日程、明确说'记住'的事）",
        "parameters": {
            "type": "object",
            "properties": {
                "fact": {"type": "string",
                         "description": "要记住的事实，一句话，如'用户周三要体测'"},
            },
            "required": ["fact"],
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "look_at_screen",
        "description": "看一眼当前屏幕并描述（用户问'屏幕上是什么/打开了没有/你看到什么'时用）",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string",
                             "description": "用户关于屏幕的问题，原文照抄"},
            },
            "required": ["question"],
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "recall",
        "description": "检索长期记忆（用户问'上次/之前/还记得吗/我有没有说过'这类过去的事时用）",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "要检索的内容，用用户原话里的关键词"},
            },
            "required": ["query"],
        },
    },
}]

_history = deque(maxlen=16)
_cfg_cache = {"t": 0.0, "v": {}}
_abort = threading.Event()
_respond_lock = threading.Lock()   # 路由调用串行化（线程安全）


def abort_current():
    """打断正在进行的流式回复（用户插话时调用）。下一轮 respond 自动复位。"""
    _abort.set()


def _cfg():
    if time.time() - _cfg_cache["t"] > 5:
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                _cfg_cache["v"] = json.load(f)
        except (OSError, ValueError):
            _cfg_cache["v"] = {}
        _cfg_cache["t"] = time.time()
    return _cfg_cache["v"]


def _endpoint():
    cfg = _cfg()
    base = str(cfg.get("chat_base_url", "https://api.deepseek.com/v1")).rstrip("/")
    key = str(cfg.get("chat_api_key") or secrets_store.get_secret("deepseek"))
    model = str(cfg.get("chat_model", "deepseek-chat"))
    return base, key, model


def note_turn(user, assistant):
    """一轮纯对话落进短期记忆。"""
    _history.append({"role": "user", "content": user})
    _history.append({"role": "assistant", "content": assistant})


def note_task(user, result):
    """任务结果以一句话摘要回流，保持上下文连续（"刚才那个怎么样了"）。"""
    _history.append({"role": "user", "content": user})
    brief = (result or "").strip().replace("\n", " ")[:80]
    _history.append({"role": "assistant",
                     "content": f"（已帮你办好了：{brief}）" if brief else "（已办好）"})


def respond(text, on_delta=None, log=print, allow_handoff=True, context=None):
    """一轮交互。返回 (kind, payload)：
    ("chat", 回复全文) | ("handoff", {"task","mode"}) |
    ("reminder", args) | ("remember", fact) | ("error", None)。
    context：调用方给的近期对话（无状态路由用）；None 时用内部历史（旧路径）。
    线程安全：网络调用串行化。"""
    base, key, model = _endpoint()
    if not key:
        log("CHAT: 没有 API key（chat_api_key / DEEPSEEK_API_KEY）")
        return ("error", None)
    history = context if context is not None else list(_history)
    messages = ([{"role": "system",
                  "content": _system_prompt(allow_handoff)}]
                + list(history)
                + [{"role": "user", "content": text}])
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": 220,
        "temperature": 0.0,   # 路由本质是分类器，零温钉死判定；口头应答一句话不需要发散
        # 关思考模式（V4 默认 effort=high，路由不需要推理，只要快和稳）
        "thinking": {"type": "disabled"},
    }
    if allow_handoff:
        body["tools"] = TOOLS
        body["tool_choice"] = "auto"
    _abort.clear()
    with _respond_lock:
        for attempt in (1, 2):
            try:
                return _stream(base, key, body, on_delta, log)
            except Exception as e:
                if _abort.is_set():
                    return ("aborted", None)
                log(f"CHAT: 第 {attempt} 次调用失败: {e}")
                time.sleep(0.4)
    return ("error", None)


def _stream(base, key, body, on_delta, log):
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    content_parts = []
    tool_calls = {}   # index -> {"name": str, "args": str}
    t0 = time.time()
    first = None
    with urllib.request.urlopen(req, timeout=15) as resp:
        for raw in resp:
            if _abort.is_set():
                log("CHAT: 被打断，中止流")
                return ("aborted", None)
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                ev = json.loads(data)
            except ValueError:
                continue
            for ch in ev.get("choices", []):
                delta = ch.get("delta") or {}
                c = delta.get("content")
                if c:
                    if first is None:
                        first = time.time() - t0
                        log(f"CHAT: 首 token {first:.2f}s")
                    content_parts.append(c)
                    if on_delta:
                        on_delta(c)
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = tool_calls.setdefault(idx, {"name": "", "args": ""})
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]
    full = "".join(content_parts).strip()
    for slot in tool_calls.values():
        if slot["name"] == "handoff_to_agent":
            try:
                args = json.loads(slot["args"] or "{}")
            except ValueError:
                args = {}
            task = args.get("task", "")
            if task:
                mode = args.get("mode", "implicit")
                if mode not in ("implicit", "explicit"):
                    mode = "implicit"
                log(f"CHAT: 移交干活脑[{mode}]: {task!r}（口头应答 {len(full)} 字）")
                return ("handoff", {"task": task, "mode": mode})
        elif slot["name"] == "set_reminder":
            try:
                args = json.loads(slot["args"] or "{}")
            except ValueError:
                args = {}
            if args.get("text") and args.get("when"):
                log(f"CHAT: 设提醒: {args}")
                return ("reminder", args)
        elif slot["name"] == "remember":
            try:
                args = json.loads(slot["args"] or "{}")
            except ValueError:
                args = {}
            if args.get("fact"):
                log(f"CHAT: 记忆: {args['fact'][:40]!r}")
                return ("remember", args["fact"])
        elif slot["name"] == "look_at_screen":
            try:
                args = json.loads(slot["args"] or "{}")
            except ValueError:
                args = {}
            log(f"CHAT: 看屏幕: {args.get('question', '')[:40]!r}")
            return ("look", args.get("question", ""))
        elif slot["name"] == "recall":
            try:
                args = json.loads(slot["args"] or "{}")
            except ValueError:
                args = {}
            if args.get("query"):
                log(f"CHAT: 检索记忆: {args['query'][:40]!r}")
                return ("recall", args["query"])
        elif slot["name"] == "amend_task":
            try:
                args = json.loads(slot["args"] or "{}")
            except ValueError:
                args = {}
            if args.get("task"):
                log(f"CHAT: 修订任务: {args['task'][:40]!r}")
                return ("amend", args["task"])
    log(f"CHAT: 纯对话 {time.time() - t0:.1f}s, {len(full)} 字")
    return ("chat", full)


if __name__ == "__main__":
    # 路由冒烟测试：前两个应纯对话，后三个应移交
    tests = [
        "你好，最近怎么样",
        "给我讲个冷笑话",
        "现在几点了",
        "帮我把音量调到 30",
        "看看我桌面上都有什么",
    ]
    for t in tests:
        kind, payload = respond(t, on_delta=lambda c: print(c, end="", flush=True))
        print(f"\n>>> {t!r} -> {kind}: {str(payload)[:60]!r}\n")
