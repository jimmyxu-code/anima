"""各厂商 API 额度探针（2026-08-28 设置面板落地）。

probe 纪律：只有实测通过的端点才直查余额——
- DeepSeek  GET https://api.deepseek.com/user/balance（2026-08-28 probe 实测 200）
火山系（doubao/ark/websearch）与阿里 dashscope 无 bearer key 直查余额的
公开端点（费用中心需 AK/SK 另套凭据）——不猜不写死，显示控制台入口。

2026-08-28 用户裁决：Moonshot/Kimi 已不需要——条目整体移除（余额查询
形态代码保留在 _fetch_balance 的注释历史里，接回来时看 git 台账）。

凭据零落盘：key 现取 secrets_store，不写文件、不进返回值、不进日志。
"""

import json
import threading
import time
import urllib.request
import urllib.error

import secrets_store

_CACHE_TTL = 300.0   # 5 分钟缓存，面板刷新不打爆厂商 API
_cache = {"ts": 0.0, "data": None}
_lock = threading.Lock()

_VENDORS = [
    {"name": "deepseek", "label": "DeepSeek", "use": "对话脑 / 执行 / 记忆整理",
     "balance_url": "https://api.deepseek.com/user/balance",
     "console_url": "https://platform.deepseek.com/usage"},
    {"name": "doubao", "label": "豆包（火山引擎）", "use": "语音对话 / 语音合成",
     "balance_url": None,
     "console_url": "https://console.volcengine.com/finance/"},
    {"name": "ark", "label": "火山方舟", "use": "GUI 视觉 / 联网搜索兜底",
     "balance_url": None,
     "console_url": "https://console.volcengine.com/finance/"},
    {"name": "dashscope", "label": "阿里云百炼", "use": "GUI 视觉（OWL）",
     "balance_url": None,
     "console_url": "https://bailian.console.aliyun.com/"},
    {"name": "websearch", "label": "火山联网搜索", "use": "联网搜索",
     "balance_url": None,
     "console_url": "https://console.volcengine.com/finance/"},
]


def _fetch_balance(url, key, timeout=8):
    """直查余额。返回 (余额文本, 错误类别, 错误文本)；错误类别 invalid/error。"""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, "invalid", "凭据无效或已过期"
        return None, "error", f"查询失败（HTTP {e.code}）"
    except Exception as e:
        return None, "error", f"查询失败（{type(e).__name__}）"
    # 余额形态：DeepSeek balance_infos[{currency, total_balance, ...}]
    # （曾支持 Moonshot data{available_balance}——08-28 用户裁决下线 Kimi）
    infos = data.get("balance_infos")
    if isinstance(infos, list) and infos:
        parts = [f"{i.get('total_balance', '?')} {i.get('currency', '')}".strip()
                 for i in infos]
        return " / ".join(parts), None, None
    return None, "error", "返回格式未识别"


def snapshot(force=False):
    """六厂商额度快照，返回 [{name,label,use,configured,status,balance,
    console_url}]；status ∈ ok / missing / invalid / error / manual。
    带 5 分钟缓存；force=True 绕缓存（面板"刷新额度"按钮）。
    任何厂商查询失败只影响该条目，绝不抛异常（额度区不能拖死面板）。"""
    now = time.time()
    with _lock:
        if (not force and _cache["data"] is not None
                and now - _cache["ts"] < _CACHE_TTL):
            return _cache["data"]
    out = []
    for v in _VENDORS:
        item = {"name": v["name"], "label": v["label"], "use": v["use"],
                "configured": False, "status": "missing", "balance": None,
                "console_url": v["console_url"]}
        try:
            key = secrets_store.get_secret(v["name"])
        except Exception:
            key = ""
        if not key:
            out.append(item)
            continue
        item["configured"] = True
        if not v["balance_url"]:
            item["status"] = "manual"   # 已配置；余额请到控制台查
            out.append(item)
            continue
        balance, kind, err = _fetch_balance(v["balance_url"], key)
        if balance:
            item["status"] = "ok"
            item["balance"] = balance
        else:
            item["status"] = kind or "error"
            item["balance"] = err
        out.append(item)
    with _lock:
        _cache["ts"] = time.time()
        _cache["data"] = out
    return out
