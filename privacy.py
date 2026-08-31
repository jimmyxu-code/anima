# -*- coding: utf-8 -*-
"""P-1-2 一键隐私开关：停上行 + 禁截屏的唯一状态源。

- 状态持久化在 config.json 的 "privacy_mode" 字段（进程内缓存，写时落盘）。
- 门控点（报告 v4.2 口径）：
  * 停上行：companion_rt._mic_to_pcm（rt.send_audio 之前丢帧）；
    裸流模式 start_stream_capture 干脆不开。
  * 禁截屏：host_input.screenshot 抛 PrivacyBlocked（覆盖休眠件复活后的按需截屏）。
- 打开：语音"打开隐私模式"即可（说完这句后生效，随后零上行）。
- 关闭：语音上行已断，只能本地关——编辑 config.json 置 false 或运行
  `python privacy.py off`。这是设计使然：关闭动作不该经过云端耳朵。
"""


class PrivacyBlocked(Exception):
    """隐私模式下截屏被拦截。"""


import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_CFG = os.path.join(_HERE, "config.json")
_mem = {"on": None, "mtime": 0.0}


def _read_cfg():
    try:
        with open(_CFG, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def is_on():
    # mtime 感知：外部改 config（或 CLI 开关）1 秒内对运行中进程生效
    try:
        mt = os.path.getmtime(_CFG)
    except OSError:
        mt = 0.0
    if _mem["on"] is None or mt != _mem["mtime"]:
        _mem["on"] = bool(_read_cfg().get("privacy_mode"))
        _mem["mtime"] = mt
    return _mem["on"]


def set_on(v):
    _mem["on"] = bool(v)
    cfg = _read_cfg()
    cfg["privacy_mode"] = bool(v)
    tmp = _CFG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _CFG)  # 原子写（P0a-9 定案）


if __name__ == "__main__":
    import sys
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if arg == "on":
        set_on(True)
    elif arg == "off":
        set_on(False)
    print("privacy_mode:", is_on())
