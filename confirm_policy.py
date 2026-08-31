"""确认请求封套的纯策略校验，不依赖 HTTP、语音或桌面。"""

import hashlib
import json
import math
import time
import threading


def canonical_operation_hash(operation):
    """对完整操作参数做稳定摘要；摘要只用于绑定，不替代自然语言说明。"""
    try:
        raw = json.dumps(
            operation, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(raw).hexdigest()


def validate_confirmation_envelope(body, now_ms=None, max_ttl_ms=60_000):
    """返回强绑定确认封套元组，无效时返回 None。"""
    if not isinstance(body, dict):
        return None
    question = str(body.get("question", "")).strip()
    scope = str(body.get("scope", "")).strip()
    request_id = str(body.get("confirmation_id", "")).strip()
    task_id = str(body.get("task_id", "")).strip()
    tool_call_id = str(body.get("tool_call_id", "")).strip()
    operation_hash = str(body.get("canonical_operation_hash", "")).strip().lower()
    try:
        expires_at = float(body.get("expires_at", 0))
    except (TypeError, ValueError):
        return None
    try:
        now_ms = time.time() * 1000 if now_ms is None else float(now_ms)
    except (TypeError, ValueError):
        return None
    if (not math.isfinite(expires_at) or not math.isfinite(now_ms)
            or not question or not question.endswith(("？", "?"))
            or not scope or not request_id or not task_id or not tool_call_id
            or len(scope) > 4000 or len(request_id) > 128
            or len(task_id) > 160 or len(tool_call_id) > 200
            or len(operation_hash) != 64
            or any(c not in "0123456789abcdef" for c in operation_hash)
            or expires_at <= now_ms
            or expires_at > now_ms + float(max_ttl_ms)):
        return None
    return (question[:200], scope, request_id, task_id, tool_call_id,
            operation_hash, expires_at)


class ConfirmationReplayGuard:
    """短时确认编号的一次性消费器，防止同一批准被重放。"""

    def __init__(self, ttl_s=90.0, clock=None):
        self._ttl_s = float(ttl_s)
        self._clock = clock or time.time
        self._items = {}
        self._lock = threading.Lock()

    def claim(self, request_id, task_id, tool_call_id, operation_hash,
              expires_at=None):
        request_id = str(request_id or "").strip()
        task_id = str(task_id or "").strip()
        tool_call_id = str(tool_call_id or "").strip()
        operation_hash = str(operation_hash or "").strip().lower()
        if not request_id or not task_id or not tool_call_id or not operation_hash:
            return False
        now = float(self._clock())
        expiry_s = now + self._ttl_s
        if expires_at is not None:
            try:
                requested_expiry = float(expires_at) / 1000.0
            except (TypeError, ValueError):
                return False
            if not math.isfinite(requested_expiry) or requested_expiry <= now:
                return False
            expiry_s = min(expiry_s, requested_expiry)
        binding = (task_id, tool_call_id, operation_hash)
        with self._lock:
            self._items = {
                key: value for key, value in self._items.items()
                if value[0] > now
            }
            if request_id in self._items:
                return False
            self._items[request_id] = (expiry_s, binding)
            return True
