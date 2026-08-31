# -*- coding: utf-8 -*-
"""Qwen-UI-Agent 占位 provider。

截至 2026-08-20，官方项目公开了技术报告和项目材料，但未在项目目录公开
Qwen-UI-Agent 专用可调用 API、模型权重、实现代码或稳定线协议。为避免把
报告里的抽象动作表伪装成真实 API，本 provider 只声明报告能力并 fail-closed。
"""

from gui_provider import GuiModelProvider, ProviderCapabilities, ProviderUnavailable


OFFICIAL_REPORT = "https://arxiv.org/html/2607.28227"
OFFICIAL_PROJECT = "https://github.com/Tongyi-MAI/MAI-UI/tree/main/Qwen-UI-Agent"


class QwenUiAgentProvider(GuiModelProvider):
    capabilities = ProviderCapabilities(
        name="qwen_ui_agent",
        action_channels=frozenset({"gui", "cli", "api", "control"}),
        supports_batch=True,
        available=False,
        availability_note=(
            "Qwen-UI-Agent 官方当前未公开可调用实现或稳定线协议；"
            "占位 provider 已安全禁用"
        ),
        source_urls=(OFFICIAL_REPORT, OFFICIAL_PROJECT),
    )

    def _invoke(self, request):
        raise ProviderUnavailable(self.capabilities.availability_note)

