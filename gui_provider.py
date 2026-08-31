# -*- coding: utf-8 -*-
"""GUI 模型插件契约与稳定内核。

模型插件只负责能力声明、请求/上下文序列化和原生响应转换。这里不判断
用户意图或选择前后台模式，只验证协议、通道和批次原子性。当前 GUI 执行器
只能接收 GUI 通道；CLI/API/control 动作必须交给各自的中央权限路由，尚未
接入时整批 fail-closed，不能先执行批次中的 GUI 子集。
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


DIRECT_GUI_ACTIONS = frozenset({
    "click", "uia_click", "dom_click", "open_url", "left_double",
    "right_single", "drag", "scroll", "type", "hotkey", "wait",
})
ROUTED_ACTION_CHANNELS = {
    "cli_command": "cli",
    "api_call": "api",
    "ask_user": "control",
    "terminate": "control",
    # 启动应用不是键鼠注入，是执行层动作（别名/开始菜单解析）。gui_agent
    # 在结构闸前路由到 exec_native 落地（2026-08-27：seed_ark 提示词一直
    # 在教模型用 launch_app，契约不认=自己拒收自己，B站一单实锤）。
    "launch_app": "exec",
    "open_app": "exec",
}
DEFAULT_MAX_BATCH_ACTIONS = 16


class ProviderError(RuntimeError):
    """GUI provider 契约错误的基类。"""


class ProviderUnavailable(ProviderError):
    """provider 存在但当前没有可调用实现。"""


class ProviderProtocolError(ProviderError):
    """provider 输出违反中立动作契约。"""


class ProviderActionRoutingRequired(ProviderError):
    """动作需要 GUI 执行器以外的中央安全路由。"""


@dataclass(frozen=True)
class ProviderCapabilities:
    name: str
    action_channels: frozenset[str]
    supports_screenshot: bool = True
    supports_history: bool = True
    supports_batch: bool = True
    max_batch_actions: int = DEFAULT_MAX_BATCH_ACTIONS
    available: bool = True
    availability_note: str = ""
    source_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class GuiModelRequest:
    task: str
    screenshot: Any
    history: tuple[tuple[str, str], ...] = ()
    thinking: bool = False
    max_tokens: int = 2048

    @classmethod
    def build(cls, task, screenshot, history=None, thinking=False,
              max_tokens=2048):
        # 插件只收到不可变、已复制的恢复上下文，不能回写调用方的活状态。
        safe_history = tuple(
            (str(user), str(assistant))
            for user, assistant in copy.deepcopy(list(history or []))[-6:]
        )
        return cls(
            task=str(task), screenshot=screenshot, history=safe_history,
            thinking=bool(thinking), max_tokens=int(max_tokens),
        )


@dataclass(frozen=True)
class GuiModelResponse:
    provider: str
    actions: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    final_text: str | None = None
    reasoning: str = ""
    latency: float = 0.0
    usage: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def build(cls, provider, actions=None, final_text=None, reasoning="",
              latency=0.0, usage=None):
        frozen_actions = tuple(copy.deepcopy(list(actions or [])))
        return cls(
            provider=str(provider), actions=frozen_actions,
            final_text=None if final_text is None else str(final_text),
            reasoning=str(reasoning or ""), latency=float(latency),
            usage=copy.deepcopy(dict(usage or {})),
        )

    def require_direct_gui_actions(self):
        """原子返回 GUI 动作；任一动作需别的路由时不返回任何子集。"""
        channels = tuple(action_channel(action) for action in self.actions)
        if any(channel != "gui" for channel in channels):
            routed = sorted(set(channel for channel in channels if channel != "gui"))
            raise ProviderActionRoutingRequired(
                "动作批次需要中央安全路由，当前 GUI 执行器拒绝整批："
                + ",".join(routed)
            )
        return copy.deepcopy(list(self.actions))


def action_channel(action: Mapping[str, Any]) -> str:
    if not isinstance(action, Mapping):
        raise ProviderProtocolError("provider 动作不是结构化对象")
    name = str(action.get("action", "")).strip()
    if name in DIRECT_GUI_ACTIONS:
        return "gui"
    channel = ROUTED_ACTION_CHANNELS.get(name)
    if channel:
        return channel
    raise ProviderProtocolError(f"provider 返回未知动作 {name!r}")


class GuiModelProvider:
    capabilities: ProviderCapabilities

    def invoke(self, request: GuiModelRequest) -> GuiModelResponse:
        caps = self.capabilities
        if not caps.available:
            raise ProviderUnavailable(caps.availability_note or f"{caps.name} 当前不可用")
        response = self._invoke(request)
        return validate_response(response, caps)

    def _invoke(self, request: GuiModelRequest) -> GuiModelResponse:
        raise NotImplementedError


def validate_response(response: GuiModelResponse,
                      capabilities: ProviderCapabilities) -> GuiModelResponse:
    if not isinstance(response, GuiModelResponse):
        raise ProviderProtocolError("provider 未返回 GuiModelResponse")
    if response.provider != capabilities.name:
        raise ProviderProtocolError("provider 响应身份与能力声明不一致")
    if response.actions and response.final_text is not None:
        raise ProviderProtocolError("同一响应不能同时声明动作批次和最终结果")
    max_batch = min(
        max(1, int(capabilities.max_batch_actions)),
        DEFAULT_MAX_BATCH_ACTIONS,
    )
    if len(response.actions) > max_batch:
        raise ProviderProtocolError(
            f"动作批次超过安全上限：{len(response.actions)} > {max_batch}")
    if len(response.actions) > 1 and not capabilities.supports_batch:
        raise ProviderProtocolError("provider 未声明批量动作能力")
    for action in response.actions:
        channel = action_channel(action)
        if channel not in capabilities.action_channels:
            raise ProviderProtocolError(
                f"provider 未声明 {channel!r} 动作通道能力")
    return response


class ProviderRegistry:
    def __init__(self, providers: Iterable[GuiModelProvider] = ()):
        self._providers = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: GuiModelProvider):
        name = provider.capabilities.name
        if not name or name in self._providers:
            raise ProviderProtocolError(f"重复或空 provider 名称：{name!r}")
        self._providers[name] = provider

    def get(self, name: str) -> GuiModelProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise ProviderUnavailable(f"未知 GUI provider：{name}") from exc

    def resolve_name(self, explicit=None):
        if explicit:
            requested = str(explicit).strip()
        else:
            requested = os.environ.get("GUI_PROVIDER", "").strip()
            if not requested:
                # 兼容现有部署配置；这是配置协议映射，不参与任务语义判断。
                requested = {
                    "ark": "seed_ark",
                    "local": "ui_tars_local",
                }.get(os.environ.get("GUI_ENGINE", "ark").strip(), "")
        aliases = {"ark": "seed_ark", "local": "ui_tars_local"}
        return aliases.get(requested, requested)

    def resolve(self, explicit=None) -> GuiModelProvider:
        return self.get(self.resolve_name(explicit))

    def capabilities(self):
        return tuple(provider.capabilities for provider in self._providers.values())

