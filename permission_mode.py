"""权限档位单一读取口（2026-08-28 设置面板落地）。

config.json 的 permission_mode 键，三档 confirm/auto/yolo；
缺省/非法值/读取失败一律 confirm（fail-safe，宁可多问不放过）。

三档语义（用户 2026-08-31 终裁）：
- confirm 逐条确认：所有高危操作逐条语音确认（默认档，= 2026-08-22 以来现状）。
- auto    自动通过：高危逐条问，但同一任务内同一类操作问过一次就不再重问
          （任务级记忆授权，任务结束即失效，不持久化）。
- yolo    最大自主：只有永久毁掉有价值数据、正常手段无法恢复的删除任务仍
           语音确认；发送、付款、关机、卸载、系统变更、改自身代码等其余
           操作全部放行。是否不可恢复由模型主判，窄规则仅在模型不可用时兜底。

确认链本身不变——所有确认依然通过语音完成（_ask_confirm）。
"""

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_HERE, "config.json")

MODES = ("confirm", "auto", "yolo")


def current():
    """读取当前权限档位；任何异常都回 confirm（fail-safe）。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            mode = str(json.load(f).get("permission_mode", "")).strip().lower()
    except (OSError, ValueError):
        return "confirm"
    return mode if mode in MODES else "confirm"
