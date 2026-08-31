# -*- coding: utf-8 -*-
"""阿里云百炼·通义 UI Agent（gui-owl）provider（2026-08-26 立项，探针已闭环）。

云端 PC GUI agent：endpoint /api/v2/apps/gui-owl/gui_agent_server，
模型 pre-gui_owl_7b，限时免费（P-OWL 探针实测 2026-08-26）。

探针实证（.tmp/owl_probe4.py，别凭文档猜——P-OWL 系列台账）：
  P-OWL-1  截图通道：data:image/png;base64 直接收（无需 OSS）
  P-OWL-2  完成信号：action_type=="stop"（original decision 为 done）
  P-OWL-4  坐标系：action_parameter.position 是**原图像素坐标**，直接可用
            （1000x600 图红块中心(720,140)→返回[716,139]）；count=2=双击；
            scroll 带 signed pixels
  延迟实测：7.6-14.5s/步（Seed Ark 实测 2.9-4.2s/步——A/B 的关键差距项）

灰度纪律：注册进 registry 但不改默认主路（seed_ark）；config
gui_provider="aliyun_owl" 一键切换，出问题改回即回退。

凭据零落盘：key 只走 secrets_store（键名 dashscope）。
"""

import base64
import io
import json
import time
import urllib.request

import gui_provider
from gui_provider import (GuiModelProvider, GuiModelRequest,
                          GuiModelResponse, ProviderCapabilities,
                          ProviderUnavailable)

_ENDPOINT = ("https://dashscope.aliyuncs.com/api/v2/apps/"
             "gui-owl/gui_agent_server")
_MODEL = "pre-gui_owl_7b"

# gui-owl PC action_type（探针实测值域） → 我方动作契约
_ACTION_MAP = {
    "click": "click",           # count=2 → left_double
    "double_click": "left_double",
    "drag": "drag",
    "type": "type",
    "hotkey": "hotkey",
    "scroll": "scroll",
    "wait": "wait",
}
_TERMINAL = {"stop", "done", "terminate", "finish"}   # P-OWL-2 实测 stop


def _api_key():
    try:
        import secrets_store
        return secrets_store.get_secret("dashscope") or ""
    except Exception:
        return ""


class AliyunOwlProvider(GuiModelProvider):
    capabilities = ProviderCapabilities(
        name="aliyun_owl",
        action_channels=frozenset({"gui"}),
        supports_batch=False,          # gui-owl 单轮一动作（PC 链路实测）
        supports_history=True,         # session_id 服务端续
        available=bool(_api_key()),
        availability_note=(
            "未配置 secrets_store:dashscope key（百炼控制台创建后写入）"),
        source_urls=("https://help.aliyun.com/zh/model-studio/ui-agent-api",),
    )

    def __init__(self):
        self._session = {"id": ""}     # gui-owl 会话（任务级，服务端续）

    def _invoke(self, request: GuiModelRequest) -> GuiModelResponse:
        t0 = time.time()
        key = _api_key()
        if not key:
            raise ProviderUnavailable(self.capabilities.availability_note)
        # P-OWL-1：data:URL 直接传（JPEG 压到 ~200KB 级，比 PNG 快）
        buf = io.BytesIO()
        request.screenshot.convert("RGB").save(buf, format="JPEG", quality=72)
        img_url = ("data:image/jpeg;base64,"
                   + base64.b64encode(buf.getvalue()).decode("ascii"))
        messages = [
            {"image": img_url},
            {"instruction": request.task},
            {"session_id": self._session["id"]},
            {"device_type": "pc"},
            {"pipeline_type": "agent"},
            {"model_name": _MODEL},
            {"thought_language": "chinese"},
            {"param_list": [{"add_info": ""}]},
        ]
        payload = {"app_id": "gui-owl",
                   "input": [{"role": "user",
                              "content": [{"type": "data",
                                           "data": {"messages": messages}}]}]}
        req = urllib.request.Request(
            _ENDPOINT, data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        self._session["id"] = body.get("session_id", "") or ""
        data = (body.get("output") or [{}])[0].get("content", [{}])[0] \
            .get("data", {})
        act = str(data.get("action_type", "")).strip()
        params = data.get("action_parameter") or {}
        expl = str(data.get("explanation", ""))
        thought = str(data.get("thought", ""))
        # P-OWL-2：stop/done → 终局（explanation 如 "Finished by planner"，
        # 无人话总结时用 thought 兜底）
        if act in _TERMINAL:
            return GuiModelResponse.build(
                provider=self.capabilities.name,
                final_text=(expl if not expl.startswith("Finished")
                            else "") or thought or "任务结束",
                reasoning=thought, latency=time.time() - t0,
                usage=body.get("usage", {}))
        ours = _ACTION_MAP.get(act)
        if ours is None:
            raise gui_provider.ProviderProtocolError(
                f"gui-owl 返回未知动作 {act!r}（动作值域见 P-OWL 台账）")
        pos = params.get("position") or [0, 0]
        action = {"action": ours, "position": tuple(int(v) for v in pos[:2]),
                  "intent": expl[:60]}
        if ours == "click" and int(params.get("count", 1)) >= 2:
            action["action"] = "left_double"
        if ours == "type":
            action["text"] = str(params.get("text", params.get("content", "")))
        if ours == "scroll":
            px = int(params.get("pixels", 0))
            action["direction"] = "up" if px < 0 else "down"
            action["amount"] = min(10, max(1, abs(px) // 100))
            action["position"] = tuple(int(v) for v in pos[:2])
        if ours == "hotkey":
            action["keys"] = str(params.get("keys", params.get("key", "")))
        return GuiModelResponse.build(
            provider=self.capabilities.name, actions=[action],
            reasoning=thought, latency=time.time() - t0,
            usage=body.get("usage", {}))
