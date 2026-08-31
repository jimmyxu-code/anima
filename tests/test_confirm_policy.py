import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
"""确认封套离线回归，不启动 HTTP 服务。"""

import math

from confirm_policy import (
    ConfirmationReplayGuard, canonical_operation_hash,
    validate_confirmation_envelope,
)


def _body(expires_at=1100):
    return {
        "question": "是否执行操作？",
        "scope": "修改文档 A 的完整参数",
        "confirmation_id": "req-1",
        "task_id": "task-1",
        "tool_call_id": "call-1",
        "canonical_operation_hash": "a" * 64,
        "expires_at": expires_at,
    }


def test_valid_envelope_is_scoped_and_trimmed():
    result = validate_confirmation_envelope(_body(), now_ms=1000)
    assert result == (
        "是否执行操作？", "修改文档 A 的完整参数", "req-1",
        "task-1", "call-1", "a" * 64, 1100.0)


def test_missing_or_expired_envelope_fails_closed():
    base = _body(expires_at=1000)
    for key in ("scope", "confirmation_id", "task_id", "tool_call_id",
                "canonical_operation_hash", "expires_at"):
        body = dict(base)
        body.pop(key)
        assert validate_confirmation_envelope(body, now_ms=999) is None
    assert validate_confirmation_envelope(base, now_ms=1000) is None
    assert validate_confirmation_envelope(_body(math.nan), now_ms=999) is None
    assert validate_confirmation_envelope(_body(math.inf), now_ms=999) is None


def test_scope_length_is_bounded():
    body = _body(expires_at=2000)
    body["scope"] = "x" * 4001
    assert validate_confirmation_envelope(body, now_ms=1000) is None


def test_expiry_and_request_id_are_bounded():
    body = _body(expires_at=61_001)
    assert validate_confirmation_envelope(body, now_ms=1000) is None
    body["expires_at"] = 2000
    body["confirmation_id"] = "x" * 129
    assert validate_confirmation_envelope(body, now_ms=1000) is None
    body = _body(expires_at=2000)
    body["question"] = "不是明确问句"
    assert validate_confirmation_envelope(body, now_ms=1000) is None


def test_confirmation_id_is_one_time():
    now = [100.0]
    guard = ConfirmationReplayGuard(ttl_s=10, clock=lambda: now[0])
    assert guard.claim("req-1", "task-1", "call-1", "a" * 64) is True
    assert guard.claim("req-1", "task-1", "call-1", "a" * 64) is False
    assert guard.claim("req-1", "task-2", "call-2", "b" * 64) is False
    now[0] = 111.0
    assert guard.claim("req-1", "task-2", "call-2", "b" * 64) is True


def test_canonical_hash_is_stable_and_complete():
    first = canonical_operation_hash({"tool": "write", "input": {"b": 2, "a": 1}})
    same = canonical_operation_hash({"input": {"a": 1, "b": 2}, "tool": "write"})
    changed = canonical_operation_hash({"tool": "write", "input": {"a": 1, "b": 3}})
    assert first == same
    assert first != changed
    assert canonical_operation_hash({"x": math.nan}) is None


if __name__ == "__main__":
    test_valid_envelope_is_scoped_and_trimmed()
    test_missing_or_expired_envelope_fails_closed()
    test_scope_length_is_bounded()
    test_expiry_and_request_id_are_bounded()
    test_confirmation_id_is_one_time()
    test_canonical_hash_is_stable_and_complete()
    print("CONFIRM_POLICY_TEST PASS")
