# -*- coding: utf-8 -*-
"""voice-fc 插件：全双工语音层的 function calling 能力面（P3-FC）。

动机（用户 2026-08-22 明令）：语音人格嘴上承诺"我去看看/我查查"时，
必须有协议级真动作兜底——模型自己调工具，结果直接进对话，不再出现
"说完就沉默、系统什么都没干"的口是心非。

边界：
- 这里只放**无副作用**的快工具（联网查、看屏幕）：语音层直接答，不派活。
  （内置联网在 duplex 端点实测不生效，实时信息只能 FC 外挂——2026-08-22
  三探实锤，别再撤。）
- 有副作用/长任务仍走路由→任务会话→执行层（成熟的检查点/确认闸机制），
  不在语音 FC 里另起炉灶。
- 工具执行实况由 companion_rt 装配时 bind 的 activity 通道上屏
  （沉默可见化铁律）；路由协调（_fc_fired）也是装配层的事。
"""

import json

import gui_brain
import host_input
import privacy
import prospective
import soul

# 语音层 FC 工具定义（豆包全双工 session.tools，标准 JSON Schema）
# 2026-08-22 三探实锤：duplex 端点内置联网不生效（tts_type 全 default，
# 模型自认无实时能力）——实时信息只能走 FC 外挂；为压时延，链路上
# 模型被指示"先说一句再调工具"（人格卡），搜索后端尽量走直连 API。
_TOOLS = [
    {"type": "function",
     "name": "web_search",
     "description": "联网搜索实时信息（天气、新闻、汇率、比分、热搜等）。"
                    "用户问到实时信息时必须调这个，不许凭记忆编。",
     "parameters": {"type": "object",
                    "properties": {"query": {"type": "string",
                                             "description": "搜索关键词"}},
                    "required": ["query"]}},
    {"type": "function",
     "name": "look_screen",
     "description": "看一眼用户电脑屏幕的实时画面并如实描述。用户说"
                    "'看看这个/帮我看下屏幕/这是怎么回事'时调用。",
     "parameters": {"type": "object",
                    "properties": {"question": {"type": "string",
                                                "description": "想看什么"}},
                    "required": ["question"]}},
    {"type": "function",
     "name": "run_task",
     "description": "动手类任务派给你的执行层（这台电脑的文件/终端/软件/界面"
                    "操作它都能做）。用户要你操作电脑、装软件、改文件、开软件"
                    "时调这个。你只是派活，活由执行层干，结果随后给你转述。",
     "parameters": {"type": "object",
                    "properties": {"task": {"type": "string",
                                            "description": "要做什么，说完整具体"},
                                   "mode": {"type": "string",
                                            "enum": ["implicit", "explicit"],
                                            "description": "implicit=后台无感执行"
                                            "（默认）；explicit=前台真实键鼠操作"
                                            "（用户要看过程/需要界面操作时）"}},
                    "required": ["task"]}},
    {"type": "function",
     "name": "task_status",
     "description": "查当前任务的进度实况。用户问'做好了吗/怎么样了/进度'时"
                    "必须调这个拿真实状态再回答，绝对不许凭印象编进度。",
     "parameters": {"type": "object", "properties": {}}},
    {"type": "function",
     "name": "recall_memory",
     "description": "检索长期记忆（用户问'昨天/上次/之前/还记得吗'的事时"
                    "必须先调这个再回答；查不到就老实说没记住，不许编）。",
     "parameters": {"type": "object",
                    "properties": {"query": {"type": "string",
                                             "description": "要找什么"}},
                    "required": ["query"]}},
]


def _do_web_search(args, svc):
    query = str(args.get("query", "")).strip()
    if not query:
        return "没给搜索词"
    if svc is None:
        return "联网搜索未装配"
    return svc(query)


def _do_look_screen(args, log):
    if privacy.is_on():
        return "隐私模式开启中，看不了屏幕（用户可在 config 里关闭隐私模式）"
    question = str(args.get("question", "")).strip() or "屏幕上现在是什么"
    try:
        # 三层感知（2026-08-26）：L0 前台底账拼进问题——"看不到页面"听感的
        # 根治之一：描述天然带上下文（用户在哪个应用、标题是什么）。
        try:
            from plugins import screen_aware
            ctx = screen_aware.context_line()
        except Exception:
            ctx = ""
        img = host_input.screenshot()
        desc, lat = gui_brain.describe(f"{ctx}\n{question}" if ctx else question,
                                       img)
        log(f"voice-fc 看屏幕 ({lat:.1f}s): {desc[:60]!r}")
        return f"屏幕实况（视觉模型如实描述）：{desc}"
    except Exception as e:
        log(f"voice-fc 看屏幕失败: {e}")
        return f"看屏幕失败了（{e}），如实告诉用户看不了，别编画面"


def register(ctx):
    channels = {"dispatcher": None, "status": None}

    def bind(dispatcher=None, status=None):
        """companion_rt 装配时绑定：dispatcher=派活 fn(task, mode)→口播文本，
        status=查进度 fn()→实况文本（两个都接任务会话的同一套机制）。"""
        channels.update({k: v for k, v in
                         (("dispatcher", dispatcher), ("status", status))
                         if v is not None})

    def tools():
        """session.tools 定义（每次开会话调用，允许热改）。"""
        return [dict(t) for t in _TOOLS]

    def run(name, args, log=print):
        """执行一个 FC 工具调用，返回给模型转述的文本。未知工具 fail-closed。"""
        try:
            if name == "web_search":
                svc = ctx.svc("exec-native", {})
                return _do_web_search(args, svc.get("web_search"))
            if name == "look_screen":
                return _do_look_screen(args, log)
            if name == "run_task":
                if channels["dispatcher"] is None:
                    return "执行层没接上，如实告诉用户现在动不了手"
                return channels["dispatcher"](
                    str(args.get("task", "")), str(args.get("mode", "implicit")))
            if name == "task_status":
                if channels["status"] is None:
                    return "进度查询通道没接上，如实说查不到"
                return channels["status"]()
            if name == "recall_memory":
                hits = prospective.augment_recall(
                    str(args.get("query", "")),
                    soul.recall(str(args.get("query", "")), log=log, limit=4),
                    log=log)
                return ("长期记忆里查到这些：\n" + "\n".join(hits)
                        if hits else "长期记忆里没查到相关内容，如实说没记住")
            return f"语音层没有这个工具：{name}"
        except Exception as e:
            log(f"voice-fc 工具异常 {name}: {e}")
            return f"工具 {name} 执行失败（{e}），如实告诉用户"

    ctx.provide("voice-fc", {"tools": tools, "run": run, "bind": bind})
