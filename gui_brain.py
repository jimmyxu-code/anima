# -*- coding: utf-8 -*-
"""GUI 模型入口兼容层。

provider 的能力、请求/上下文序列化与动作协议在 ``gui_providers``；本模块
保留屏幕描述和旧调用签名。任务语义、前后台选择和人格不在这里用规则分支
决定；provider 输出先经过稳定契约验证，再交给 GUI 执行器。
"""

import base64
import io
import json
import time
import urllib.request

import secrets_store
from gui_provider import GuiModelRequest, ProviderRegistry
from gui_providers import (AliyunOwlProvider, QwenUiAgentProvider,
                           SeedArkProvider, UiTarsLocalProvider)


_REGISTRY = ProviderRegistry((
    SeedArkProvider(),
    UiTarsLocalProvider(),
    QwenUiAgentProvider(),
    AliyunOwlProvider(),   # 百炼通义 UI Agent（gui-owl）：config 一键切换，主路默认不动
))


def provider_capabilities():
    """只读返回 provider 能力声明，不触发模型调用。"""
    return _REGISTRY.capabilities()

MODEL = "doubao-seed-2-1-turbo-260628"   # 注：仅 SeedArkProvider(GUI 定位) 用
URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
# 屏幕问答唯一眼睛（2026-08-22 用户裁决）：DeepSeek V4 Flash Vision——
# 与工作脑同厂同价、实测同速（4.2s），不再叠豆包视觉（工具层有一只眼就够，
# 双眼是重复建设）。GUI 前台定位仍走 gui_providers 的专精模型，与此无关。
DS_MODEL = "deepseek-v4-flash-vision-exp"
DS_URL = "https://api.deepseek.com/v1/chat/completions"


def _b64_jpeg(img, width=1280, quality=70):
    w, h = img.size
    img = img.convert("RGB").resize((width, int(h * width / w)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


_DESCRIBE_SYS = ("你是屏幕实况描述员。看用户给的屏幕截图，如实、简短地回答问题。"
                 "只描述截图里真实可见的内容，不确定就说不确定，禁止编造。"
                 "两三句话以内，口语化。")


def _post_chat(url, key, model, question, b64, max_tokens, extra=None):
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": _DESCRIBE_SYS},
            {"role": "user", "content": [
                {"type": "text", "text": question or "描述一下这个屏幕"},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]},
        ],
    }
    if extra:
        body.update(extra)
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data["choices"][0]["message"].get("content") or "").strip(), time.time() - t0


def _describe_deepseek(question, b64, max_tokens):
    key = secrets_store.get_secret("deepseek")
    if not key:
        raise RuntimeError("缺少 deepseek 凭据")
    # vision-exp 默认思考模式：推理也要吃 max_tokens——给足余量，
    # 否则 256 之类的小额度全被思考吃掉、正文放空（2026-08-22 实测实锤）。
    return _post_chat(DS_URL, key, DS_MODEL, question, b64,
                      max(max_tokens * 3, 1024))


def describe(question, screenshot_img, max_tokens=512, log=print):
    """截图 + 问题 → 屏幕实况描述（给语音层转述用）。返回 (文本, 延迟s)。
    单眼定稿：DeepSeek V4 Vision（2026-08-22 用户裁决）。"""
    b64 = _b64_jpeg(screenshot_img)
    text, lat = _describe_deepseek(question, b64, max_tokens)
    log(f"视觉描述 deepseek ({lat:.1f}s)")
    return text, lat


def _think_local(task, screenshot_img, history=None, max_tokens=2048):
    """旧入口兼容；本地请求/历史/解析由 provider 持有。"""
    return _invoke_provider(
        _REGISTRY.get("ui_tars_local"), task, screenshot_img,
        history=history, thinking=False, max_tokens=max_tokens)


def _log_usage(usage):
    """成本台账（S2/R2）：每次 GUI 模型调用的 token 用量落盘，定价用数据说话。"""
    if not usage:
        return
    try:
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "costs.jsonl")
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "type": "gui_usage",
               "input_tokens": usage.get("prompt_tokens", 0),
               "output_tokens": usage.get("completion_tokens", 0),
               "cached": usage.get("prompt_cache_hit_tokens", 0)}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def think(task, screenshot_img, history=None, thinking=False, max_tokens=2048,
          engine=None):
    """调用配置的 provider，未知/不可用 provider 不静默回退。"""
    return _invoke_provider(
        _REGISTRY.resolve(engine), task, screenshot_img, history=history,
        thinking=thinking, max_tokens=max_tokens)


def _invoke_provider(provider, task, screenshot_img, history=None,
                     thinking=False, max_tokens=2048):
    request = GuiModelRequest.build(
        task, screenshot_img, history=history,
        thinking=thinking, max_tokens=max_tokens)
    response = provider.invoke(request)
    # 任一 CLI/API/control 动作都使整个批次在 GUI 路径 fail-closed。
    try:
        actions = response.require_direct_gui_actions()
    except Exception as e:
        from gui_provider import ProviderActionRoutingRequired, action_channel
        if not isinstance(e, ProviderActionRoutingRequired):
            raise
        # 2026-08-29 修复（12:23 实锤：混批整批报废→换路三连死）：GUI 动作
        # 与 exec 动作（launch_app，gui_agent 有秒启动路由）继续执行；其余
        # 通道动作剥离，并在 reasoning 注明——模型下一步会看到提示改用点击。
        kept = [a for a in response.actions
                if action_channel(a) in ("gui", "exec")]
        dropped = sorted({str(a.get("action", "?")) for a in response.actions
                          if action_channel(a) not in ("gui", "exec")})
        _log_usage(response.usage)
        note = ("系统提示：本批混入了非界面动作（" + ",".join(dropped)
                + "）已剥离——界面任务请只用点击/打字/滚动/快捷键类动作。"
                if dropped else "系统提示：动作批次已按界面通道过滤。")
        return kept, response.final_text, (
            response.reasoning or "") + "〔" + note + "〕", response.latency
    _log_usage(response.usage)
    return actions, response.final_text, response.reasoning, response.latency
