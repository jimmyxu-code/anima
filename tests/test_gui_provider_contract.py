# -*- coding: utf-8 -*-
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
"""GUI provider 插拔边界与混合动作 fail-closed 回归；不调用模型/网络。"""

import json

import gui_brain
from gui_provider import (
    GuiModelProvider,
    GuiModelRequest,
    GuiModelResponse,
    ProviderActionRoutingRequired,
    ProviderCapabilities,
    ProviderProtocolError,
    ProviderRegistry,
    ProviderUnavailable,
)
from gui_providers.qwen_ui_agent import QwenUiAgentProvider
from gui_providers import seed_ark
from gui_providers.seed_ark import parse_actions as parse_seed_actions


class StaticProvider(GuiModelProvider):
    def __init__(self, actions, *, channels=frozenset({"gui"}), name="static"):
        self.actions = actions
        self.capabilities = ProviderCapabilities(
            name=name, action_channels=channels,
        )

    def _invoke(self, _request):
        return GuiModelResponse.build(self.capabilities.name, actions=self.actions)


def _request(history=None):
    return GuiModelRequest.build("测试", object(), history=history)


def test_request_history_is_copied_and_immutable():
    history = [["恢复状态", "已完成第一步"]]
    request = _request(history)
    history[0][1] = "被外部篡改"
    history.append(["新数据", "不应出现"])
    assert request.history == (("恢复状态", "已完成第一步"),)


def test_mixed_batch_is_rejected_atomically_before_gui_execution():
    provider = StaticProvider([
        {"action": "click", "point": (500, 500), "intent": "选择目标"},
        {"action": "cli_command", "command": "whoami", "intent": "读取身份"},
    ], channels=frozenset({"gui", "cli"}))
    response = provider.invoke(_request())
    try:
        response.require_direct_gui_actions()
    except ProviderActionRoutingRequired as exc:
        assert "整批" in str(exc)
    else:
        raise AssertionError("混合 GUI/CLI 批次不应返回可部分执行的 GUI 子集")


def test_unknown_action_and_oversized_batch_fail_closed():
    unknown = StaticProvider([
        {"action": "model_invented_tool", "intent": "未知动作"},
    ])
    try:
        unknown.invoke(_request())
    except ProviderProtocolError as exc:
        assert "未知动作" in str(exc)
    else:
        raise AssertionError("未知动作不应默认放行")

    oversized = StaticProvider([
        {"action": "wait", "intent": "观察"} for _ in range(17)
    ])
    try:
        oversized.invoke(_request())
    except ProviderProtocolError as exc:
        assert "安全上限" in str(exc)
    else:
        raise AssertionError("超长模型批次不应进入执行器")


def test_registry_selection_is_configuration_only_and_has_no_fallback():
    registry = ProviderRegistry((StaticProvider([], name="seed_ark"),))
    assert registry.resolve("ark").capabilities.name == "seed_ark"
    try:
        registry.resolve("does_not_exist")
    except ProviderUnavailable as exc:
        assert "未知 GUI provider" in str(exc)
    else:
        raise AssertionError("未知 provider 不应静默回退")


def test_qwen_placeholder_is_explicitly_unavailable():
    provider = QwenUiAgentProvider()
    caps = provider.capabilities
    assert caps.available is False
    assert caps.action_channels == frozenset({"gui", "cli", "api", "control"})
    assert len(caps.source_urls) == 2
    try:
        provider.invoke(_request())
    except ProviderUnavailable as exc:
        assert "未公开" in str(exc)
    else:
        raise AssertionError("未开放的 Qwen provider 不应伪装成可调用")


def test_seed_parser_requires_structured_protocol():
    actions, final_text = parse_seed_actions("意图：选择目标\n点击(500,500)")
    assert actions == []
    assert "点击" in final_text

    actions, final_text = parse_seed_actions(
        '意图：选择目标\n<seed:tool_call><function name="click">'
        '<parameter name="point"><point>500 500</point></parameter>'
        '</function></seed:tool_call>')
    assert final_text is None
    assert actions == [{
        "action": "click", "intent": "选择目标", "point": (500.0, 500.0),
    }]


def test_gui_brain_exposes_provider_capabilities_without_calling_models():
    capabilities = {item.name: item for item in gui_brain.provider_capabilities()}
    assert {"seed_ark", "ui_tars_local", "qwen_ui_agent"} <= set(capabilities)
    assert capabilities["qwen_ui_agent"].available is False


def test_seed_current_path_keeps_request_history_and_structured_actions():
    captured = {}

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_exc):
            return False
        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": (
                    '意图：选择目标\n<seed:tool_call><function name="click">'
                    '<parameter name="point"><point>500 500</point></parameter>'
                    '</function></seed:tool_call>'
                )}}],
            }).encode("utf-8")

    original_secret = seed_ark.secrets_store.get_secret
    original_b64 = seed_ark.b64_jpeg
    original_open = seed_ark.urllib.request.urlopen
    try:
        seed_ark.secrets_store.get_secret = lambda name: "offline-test" if name == "ark" else None
        seed_ark.b64_jpeg = lambda _image: ("encoded", 1280, 720)
        def open_request(request, timeout=None):
            captured.update(json.loads(request.data.decode("utf-8")))
            return Response()
        seed_ark.urllib.request.urlopen = open_request
        actions, final_text, _reasoning, _latency = gui_brain.think(
            "继续任务", object(), engine="ark",
            history=[("恢复状态", "已完成第一步")],
        )
        assert final_text is None
        assert actions[0]["action"] == "click"
        assert captured["model"] == seed_ark.MODEL
        assert captured["messages"][1:3] == [
            {"role": "user", "content": "恢复状态"},
            {"role": "assistant", "content": "已完成第一步"},
        ]
    finally:
        seed_ark.secrets_store.get_secret = original_secret
        seed_ark.b64_jpeg = original_b64
        seed_ark.urllib.request.urlopen = original_open


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"GUI_PROVIDER_CONTRACT_TEST PASS {len(tests)}/{len(tests)}")
