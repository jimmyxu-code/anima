# -*- coding: utf-8 -*-
"""本地 UI-TARS provider（llama.cpp OpenAI 兼容口）。"""

import json
import re
import time
import urllib.request

from gui_guidance import COMMON_GUI_GUIDANCE_ZH
from gui_provider import GuiModelProvider, GuiModelResponse, ProviderCapabilities
from .common import b64_jpeg


LOCAL_URL = "http://127.0.0.1:11435/v1/chat/completions"

UITARS_PROTOCOL_GUIDE = """Output exactly:
Thought: ...
Action: ...

Action space: click(start_box='(x,y)'), left_double(start_box='(x,y)'),
right_single(start_box='(x,y)'), drag(start_box='(x,y)', end_box='(x,y)'),
hotkey(key=''), type(content=''), scroll(start_box='(x,y)', direction='down or up or right or left'),
wait(), finished(content=''). Coordinates are absolute pixels in the supplied image.
"""
SYSTEM_PROMPT = COMMON_GUI_GUIDANCE_ZH + "\n" + UITARS_PROTOCOL_GUIDE

_ACTION_RE = re.compile(r"Action:\s*(\w+)\((.*?)\)\s*$", re.S | re.M)
_BOX_RE = re.compile(r"\((\d+)\s*,\s*(\d+)\)")
_PARAM_RE = re.compile(r"(\w+)\s*=\s*'(.*?)'", re.S)


def parse_actions(content, img_w, img_h):
    thought = ""
    thought_match = re.search(r"Thought:\s*(.+?)(?=Action:|$)", content or "", re.S)
    if thought_match:
        thought = thought_match.group(1).strip()
    match = _ACTION_RE.search(content or "")
    if not match:
        return [], (content or "").strip() or None
    name, raw_parameters = match.group(1), match.group(2)
    parameters = dict(_PARAM_RE.findall(raw_parameters))

    def relative(key):
        box = _BOX_RE.search(parameters.get(key, ""))
        if not box:
            return None
        return (
            float(box.group(1)) * 1000 / img_w,
            float(box.group(2)) * 1000 / img_h,
        )

    if name == "finished":
        return [], parameters.get("content", thought or "完成")
    if name == "wait":
        return [{"action": "wait", "intent": thought[:30]}], None
    action = {"intent": thought[:30]}
    if name in ("click", "left_double", "right_single"):
        point = relative("start_box")
        if point is None:
            return [], None
        action.update(action=name, point=point)
    elif name == "drag":
        start, end = relative("start_box"), relative("end_box")
        if start is None or end is None:
            return [], None
        action.update(action="drag", start_point=start, end_point=end)
    elif name == "type":
        action.update(action="type", text=parameters.get("content", ""))
    elif name == "hotkey":
        action.update(action="hotkey", keys=parameters.get("key", "enter"))
    elif name == "scroll":
        point = relative("start_box") or (500, 500)
        direction = parameters.get("direction", "down")
        action.update(
            action="scroll", point=point,
            delta="3" if direction in ("up", "left") else "-3",
        )
    else:
        return [], None
    return [action], None


class UiTarsLocalProvider(GuiModelProvider):
    capabilities = ProviderCapabilities(
        name="ui_tars_local",
        action_channels=frozenset({"gui"}),
        source_urls=("https://github.com/bytedance/UI-TARS",),
    )

    def _invoke(self, request):
        encoded, width, height = b64_jpeg(request.screenshot)
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
            "model": "ui-tars",
            "max_tokens": request.max_tokens,
            "temperature": 0.3,
            "messages": messages,
        }
        http_request = urllib.request.Request(
            LOCAL_URL, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"},
        )
        started = time.time()
        with urllib.request.urlopen(http_request, timeout=600) as response:
            data = json.loads(response.read().decode("utf-8"))
        latency = time.time() - started
        content = data["choices"][0]["message"].get("content") or ""
        actions, final_text = parse_actions(content, width, height)
        return GuiModelResponse.build(
            self.capabilities.name,
            actions=actions,
            final_text=final_text,
            reasoning=content,
            latency=latency,
            usage=data.get("usage") or {},
        )

