import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
"""Ark GUI Agent (doubao-seed) 冒烟：截屏 → 模型出动作块 → 打印坐标。
用法: python test_gui.py
"""
import base64
import io
import json
import sys
import time
import urllib.request

import os
import secrets_store
KEY = secrets_store.get_secret("ark")
MODEL = "doubao-seed-2-1-turbo-260628"
URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"

SYS = (
    "你是一个 GUI 操作代理。根据用户任务和屏幕截图，输出下一步动作。\n"
    "动作写进 <seed:tool_call> 块，格式：\n"
    "<seed:tool_call><function name=\"click\">"
    "<parameter name=\"point\" string=\"true\"><point>x y</point></parameter>"
    "</function></seed:tool_call>\n"
    "坐标为 0-1000 的相对坐标（与分辨率无关）。可用动作："
    "click / drag / hotkey / left_double / right_single / scroll / type / wait。"
    "任务完成时输出纯文本总结，不含 tool_call。"
)


def shot_b64():
    from PIL import ImageGrab
    img = ImageGrab.grab().convert("RGB")
    w, h = img.size
    nw = 1280
    img = img.resize((nw, int(h * nw / w)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode(), w, h


def main():
    # 用法: python test_gui.py [on|off] [任务]
    # thinking 从命令行控制（解除硬编码——§2.1 补录要求：4.8s/169s 旧数字重测）
    thinking_on = len(sys.argv) > 1 and sys.argv[1] in ("on", "enabled", "1")
    args = sys.argv[2:] if len(sys.argv) > 1 and sys.argv[1] in (
        "on", "enabled", "1", "off", "disabled", "0") else sys.argv[1:]
    task = " ".join(args) or "把鼠标移到屏幕正中央"
    b64, w, h = shot_b64()
    body = {
        "model": MODEL,
        "max_tokens": 2048,
        "thinking": {"type": "enabled" if thinking_on else "disabled"},
        "temperature": 0.7,
        "messages": [
            {"role": "system", "content": SYS},
            {"role": "user", "content": [
                {"type": "text", "text": f"任务：{task}"},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]},
        ],
    }
    req = urllib.request.Request(
        URL, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {KEY}"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    msg = data["choices"][0]["message"]
    print(f"--- {time.time()-t0:.1f}s ---")
    print("reasoning:", (msg.get("reasoning_content") or "")[:200])
    print("content:", (msg.get("content") or "")[:400])
    print("usage:", data.get("usage"))


main()
