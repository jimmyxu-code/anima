# -*- coding: utf-8 -*-
"""Seed 2.1 Pro/现役 Ark GUI provider。

这里只包含 Ark 请求格式、Seed 动作协议和响应转换；任务选择、授权、坐标落点
与执行证据仍由稳定内核负责。
"""

import json
import re
import time
import urllib.request

import secrets_store
from gui_guidance import COMMON_GUI_GUIDANCE_ZH
from gui_provider import (
    GuiModelProvider,
    GuiModelResponse,
    ProviderCapabilities,
)
from .common import b64_jpeg


MODEL = "doubao-seed-2-1-turbo-260628"
URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"

SEED_PROTOCOL_GUIDE = """## 输出协议（严格遵守）
先输出一行 `意图：<这步动作要达成什么>`，再输出一个或多个动作块：
<seed:tool_call><function name="动作名"><parameter name="point" string="true"><point>x y</point></parameter></function></seed:tool_call>

坐标为 0-1000 相对坐标。机械动作参数如下：
- click / left_double / right_single：point="x y"
- drag：start_point="x1 y1"，end_point="x2 y2"
- scroll：point="x y"，delta="整数"（正向上、负向下）
- type：text="输入内容"
- open_url：url="完整网址或域名"
- launch_app：name="应用名"。启动任何应用一律优先用它（直解析快捷方式，
  不走 Win 键开始菜单——更快，且高频敲 Win 会把系统输入链打崩）。
  如"记事本""微信""calculator""mspaint"；系统提示找不到再回退 Win 键路径
- dom_click / uia_click：text="目标可见名称"。历史里"界面控件"列出了当前窗口
  可交互控件时，点击一律优先用 uia_click 填「」里的完整名字（系统精准落点，
  不猜坐标）；清单里没有的目标（自绘界面/游戏）才用视觉坐标 click
- hotkey：keys="ctrl,s"（逗号分隔）
- wait：ms="毫秒"
只有当前截图已有可验证结果时才输出纯文本总结；否则输出结构化动作。动作参数必须完整。
"""
SYSTEM_PROMPT = COMMON_GUI_GUIDANCE_ZH + "\n" + SEED_PROTOCOL_GUIDE

_TOOL_RE = re.compile(
    r'<seed:tool_call>\s*<function name="(?P<name>\w+)">(?P<body>.*?)</function>\s*</seed:tool_call>',
    re.S,
)
_PARAM_RE = re.compile(
    r'<parameter name="(?P<key>\w+)"[^>]*>(?P<val>.*?)</parameter>', re.S,
)
_POINT_RE = re.compile(r"<point>\s*([\d.]+)\s+([\d.]+)\s*</point>")
_INTENT_RE = re.compile(r"意图[:：]\s*(.+)")


def parse_actions(content):
    """Seed 结构化动作协议 → provider 中立动作。

    不再从自然语言“点击(x,y)”猜动作；协议损坏时 fail-closed 为无动作文本，
    最终是否完成仍由独立证据闸判断。
    """
    intents = _INTENT_RE.findall(content or "")
    actions = []
    for index, match in enumerate(_TOOL_RE.finditer(content or "")):
        action = {
            "action": match.group("name"),
            "intent": intents[index] if index < len(intents) else (
                intents[-1] if intents else ""
            ),
        }
        for parameter in _PARAM_RE.finditer(match.group("body")):
            key = parameter.group("key")
            value = parameter.group("val").strip()
            point = _POINT_RE.search(value)
            action[key] = (
                (float(point.group(1)), float(point.group(2)))
                if point else value.strip('"')
            )
        actions.append(action)
    if actions:
        return actions, None
    return [], _TOOL_RE.sub("", content or "").strip()


class SeedArkProvider(GuiModelProvider):
    capabilities = ProviderCapabilities(
        name="seed_ark",
        # exec 通道 = launch_app（提示词在教模型用它；gui_agent 路由到
        # exec_native 秒启动，不进键鼠注入）
        action_channels=frozenset({"gui", "exec"}),
        source_urls=("https://www.volcengine.com/docs/82379",),
    )

    def _invoke(self, request):
        key = secrets_store.get_secret("ark")
        if not key:
            raise RuntimeError("缺少 ark 凭据（secrets_store.get_secret('ark') 为空）")
        encoded, _width, _height = b64_jpeg(request.screenshot)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for user, assistant in request.history:
            messages.append({"role": "user", "content": user})
            messages.append({"role": "assistant", "content": assistant})
        messages.append({"role": "user", "content": [
            {"type": "text", "text": f"任务：{request.task}"},
            {"type": "image_url", "image_url": {
                "url": f"data:image/jpeg;base64,{encoded}",
            }},
        ]})
        body = {
            "model": MODEL,
            "max_tokens": request.max_tokens,
            "thinking": {"type": "enabled" if request.thinking else "disabled"},
            "temperature": 0.7,
            "messages": messages,
        }
        http_request = urllib.request.Request(
            URL, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
        )
        started = time.time()
        with urllib.request.urlopen(
                http_request, timeout=90 if request.thinking else 45) as response:
            data = json.loads(response.read().decode("utf-8"))
        latency = time.time() - started
        message = data["choices"][0]["message"]
        content = message.get("content") or ""
        actions, final_text = parse_actions(content)
        return GuiModelResponse.build(
            self.capabilities.name,
            actions=actions,
            final_text=final_text,
            reasoning=message.get("reasoning_content") or "",
            latency=latency,
            usage=data.get("usage") or {},
        )
