# -*- coding: utf-8 -*-
"""内置 GUI 模型 provider。"""

from .aliyun_owl import AliyunOwlProvider
from .qwen_ui_agent import QwenUiAgentProvider
from .seed_ark import SeedArkProvider
from .ui_tars_local import UiTarsLocalProvider

__all__ = ["AliyunOwlProvider", "QwenUiAgentProvider",
           "SeedArkProvider", "UiTarsLocalProvider"]

