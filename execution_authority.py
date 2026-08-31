"""执行器权限租约的单一发布/撤销入口。

文件只承载不可协商的 task_id + executor_epoch + cancel_token；语义层不在这里。
"""

import json
import os
import threading
import time


_HERE = os.path.dirname(os.path.abspath(__file__))
AUTHORITY_PATH = os.path.join(_HERE, ".task_authority.json")
_LOCK = threading.RLock()


def _binding(value):
    if not isinstance(value, dict):
        return None
    task_id = value.get("task_id")
    epoch = value.get("executor_epoch")
    token = value.get("cancel_token")
    if (not task_id or not isinstance(epoch, int) or epoch < 1
            or not isinstance(token, str) or len(token) < 16):
        return None
    return str(task_id), epoch, token


def _atomic_write_locked(data):
    temp = (f"{AUTHORITY_PATH}.{os.getpid()}."
            f"{threading.get_ident()}.tmp")
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(temp, AUTHORITY_PATH)


def publish(context, ttl_ms=300_000):
    binding = _binding(context)
    if binding is None:
        return False
    data = {
        "task_id": binding[0],
        "executor_epoch": binding[1],
        "cancel_token": binding[2],
        "expires_at": time.time() * 1000 + int(ttl_ms),
    }
    with _LOCK:
        _atomic_write_locked(data)
    return True


def revoke(context):
    """仅撤销完全匹配的租约；过期执行器不得擦掉新 epoch。"""
    binding = _binding(context)
    if binding is None:
        return False
    with _LOCK:
        try:
            with open(AUTHORITY_PATH, "r", encoding="utf-8") as f:
                active = json.load(f)
        except (OSError, ValueError, TypeError):
            return False
        if _binding(active) != binding:
            return False
        _atomic_write_locked({})
    return True


def revoke_all():
    """进程代次切换时无条件写入空租约，阻断跨重启旧执行器。"""
    with _LOCK:
        _atomic_write_locked({})
    return True


def snapshot():
    with _LOCK:
        try:
            with open(AUTHORITY_PATH, "r", encoding="utf-8") as f:
                value = json.load(f)
        except (OSError, ValueError, TypeError):
            return None
    return value if isinstance(value, dict) else None
