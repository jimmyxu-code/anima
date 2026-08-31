"""凭据统一入口（P0a-2 终态）：Windows 凭据管理器（DPAPI/keyring）为主，
环境变量兜底。任何模块不得再直接读 config.json 里的 key。

用法: get_secret("doubao") / get_secret("ark") / get_secret("deepseek")
2026-08-28 用户裁决：Moonshot/Kimi 下线——条目移除（历史 key 若还躺在
凭据管理器里无害，不入表即不可达）。
"""

import os

_SERVICES = {
    "doubao": ("companion-rt/doubao", "DOUBAO_API_KEY"),
    "ark": ("companion-rt/ark", "ARK_API_KEY"),
    "deepseek": ("companion-rt/deepseek", "DEEPSEEK_API_KEY"),
    "websearch": ("companion-rt/websearch", "VOLC_WEBSEARCH_KEY"),
    "dashscope": ("companion-rt/dashscope", "DASHSCOPE_API_KEY"),
}


def get_secret(name):
    """keyring(DPAPI) → env 兜底；都没有返回 \"\"。"""
    svc, envvar = _SERVICES[name]
    try:
        import keyring
        v = keyring.get_password(svc, "key")
        if v:
            return v
    except Exception:
        pass
    return os.environ.get(envvar, "")
