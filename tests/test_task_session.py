import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
"""任务会话内核的离线回归，不操作桌面、不联网。"""

from concurrent.futures import ThreadPoolExecutor
import os
import time
from tempfile import TemporaryDirectory

from task_session import TaskSessionStore


def test_session_identity_mode_and_checkpoint():
    store = TaskSessionStore()
    session = store.create("整理下载文件夹", "帮我整理一下", "explicit")
    assert session.task_id.startswith("task-")
    session.update(state="running")
    revision = session.save_checkpoint(
        step=1, cursor=(100, 200), window="记事本", mode="explicit"
    )
    assert session.state == "running"
    assert session.mode == "explicit"
    assert session.checkpoint["cursor"] == (100, 200)
    assert revision == 2
    snapshot = store.snapshot(session.task_id)
    assert snapshot["task_id"] == session.task_id
    assert snapshot["checkpoint"]["step"] == 1


def test_checkpoint_writes_are_serialized():
    store = TaskSessionStore()
    session = store.create("并发检查点", "测试", "implicit")

    def write(worker):
        for step in range(25):
            session.save_checkpoint(worker=worker, step=f"{worker}-{step}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(4)))
    snapshot = store.snapshot(session.task_id)
    assert snapshot["revision"] == 100
    assert snapshot["checkpoint"]["step"]


def test_pause_resume_keeps_identity_and_changes_mode():
    store = TaskSessionStore()
    session = store.create("切换前后台", "切换", "explicit")
    task_id = session.task_id
    session.update(state="paused", control="user")
    session.save_checkpoint(step=3, mode="explicit")
    session.update(state="queued", mode="implicit", control="assistant")
    snapshot = store.snapshot(task_id)
    assert snapshot["task_id"] == task_id
    assert snapshot["mode"] == "implicit"
    assert snapshot["checkpoint"]["step"] == 3


def test_evidenced_completed_step_is_atomic_and_persisted():
    store = TaskSessionStore()
    session = store.create("记录完成步骤", "测试", "explicit")
    session.current_step = "点击确定"
    revision = session.complete_step(
        "1. 点击确定", step=1, action_evidence=True, screen_diff=0.02)
    snapshot = store.snapshot(session.task_id)
    assert revision == 1
    assert snapshot["completed_steps"] == ["1. 点击确定"]
    assert snapshot["current_step"] == ""
    assert snapshot["checkpoint"]["action_evidence"] is True


def test_persistent_store_reloads_same_identity_and_checkpoint():
    writable_tmp = os.environ.get(
        "CODEX_TEST_TMP", r"C:\Users\tester\Documents\Codex")
    with TemporaryDirectory(dir=writable_tmp) as tmp:
        path = f"{tmp}/task-sessions.json"
        first = TaskSessionStore(path)
        session = first.create("持久化迁移", "继续刚才的任务", "explicit")
        session.update(state="paused", control="assistant")
        session.save_checkpoint(step=4, cursor=(321, 654))
        second = TaskSessionStore(path)
        snapshot = second.snapshot(session.task_id)
        assert snapshot["task_id"] == session.task_id
        assert snapshot["state"] == "paused"
        assert snapshot["checkpoint"]["cursor"] == [321, 654]


def test_persistent_running_task_does_not_auto_resume():
    writable_tmp = os.environ.get(
        "CODEX_TEST_TMP", r"C:\Users\tester\Documents\Codex")
    with TemporaryDirectory(dir=writable_tmp) as tmp:
        path = f"{tmp}/task-sessions.json"
        first = TaskSessionStore(path)
        session = first.create("不要自动恢复", "测试", "implicit")
        session.update(state="running")
        second = TaskSessionStore(path)
        snapshot = second.snapshot(session.task_id)
        assert snapshot["state"] == "paused"
        assert "未自动恢复" in snapshot["last_error"]


def test_identity_fields_terminal_state_and_control_are_invariants():
    store = TaskSessionStore()
    session = store.create("原任务", "原话", "implicit")
    for changes in ({"task_id": "forged"}, {"task_text": "换任务"},
                    {"original_text": "换原话"}, {"control": "nobody"}):
        try:
            session.update(**changes)
        except (AttributeError, ValueError):
            pass
        else:
            raise AssertionError(f"invariant mutation was accepted: {changes}")
    session.update(state="running")
    session.update(state="completed")
    try:
        session.update(state="queued")
    except ValueError:
        pass
    else:
        raise AssertionError("terminal task returned to queued")


def test_snapshot_and_checkpoint_are_deep_copies():
    store = TaskSessionStore()
    session = store.create("深拷贝", "测试", "implicit")
    source = {"nested": [1, {"value": "safe"}]}
    session.save_checkpoint(payload=source)
    source["nested"][1]["value"] = "mutated-outside"
    first = session.snapshot()
    first["checkpoint"]["payload"]["nested"][1]["value"] = "mutated-snapshot"
    first["completed_steps"].append("forged")
    second = session.snapshot()
    assert second["checkpoint"]["payload"]["nested"][1]["value"] == "safe"
    assert second["completed_steps"] == []


def test_confirmation_requires_bound_one_time_credential():
    store = TaskSessionStore()
    session = store.create("危险任务", "原话", "implicit",
                           state="awaiting-confirmation")
    expires = time.time() * 1000 + 30_000
    expected = session.revision + 1
    session.save_checkpoint(
        confirmation_id="confirm-1",
        confirmation_task_id=session.task_id,
        confirmation_tool_call_id="call-1",
        canonical_operation_hash="a" * 64,
        confirmation_expires_at=expires,
        confirmation_expected_revision=expected,
    )
    credential = dict(
        confirmation_id="confirm-1", task_id=session.task_id,
        tool_call_id="call-1", operation_hash="a" * 64,
        expires_at=expires, expected_revision=expected, mode="implicit")
    forged = dict(credential, tool_call_id="call-other")
    assert session.approve_and_queue(**forged) is False
    assert session.approve_and_queue(**credential) is True
    assert session.approve_and_queue(**credential) is False
    assert session.state == "queued"


def test_executor_epoch_discards_stale_checkpoint_and_duplicate_claim():
    store = TaskSessionStore()
    session = store.create("执行租约", "测试", "explicit")
    lease = store.claim_execution(session.task_id)
    assert lease and lease["state"] == "running"
    assert store.claim_execution(session.task_id) is None
    assert session.save_checkpoint(
        executor_epoch=lease["executor_epoch"],
        cancel_token=lease["cancel_token"], step=1) is not None
    session.update(state="paused")
    assert session.save_checkpoint(
        executor_epoch=lease["executor_epoch"],
        cancel_token=lease["cancel_token"], step=2) is None
    assert session.snapshot()["checkpoint"]["step"] == 1


def test_execution_outcome_and_evidence_commit_is_epoch_atomic():
    store = TaskSessionStore()
    session = store.create("任务", "原话", "implicit")
    lease = session.claim_execution()
    assert lease is not None
    assert session.finish_execution(
        executor_epoch=lease["executor_epoch"],
        cancel_token=lease["cancel_token"], state="paused",
        last_error="待验证",
        checkpoint={"verification_status": "awaiting-verification"}) is True
    assert session.state == "paused"
    assert session.checkpoint["verification_status"] == "awaiting-verification"
    before = session.snapshot()
    assert session.finish_execution(
        executor_epoch=lease["executor_epoch"],
        cancel_token=lease["cancel_token"], state="completed",
        checkpoint={"verification_status": "verified"}) is False
    assert session.snapshot() == before


def test_restart_preserves_confirmation_and_pausing_becomes_paused():
    writable_tmp = os.environ.get(
        "CODEX_TEST_TMP", r"C:\Users\tester\Documents\Codex")
    with TemporaryDirectory(dir=writable_tmp) as tmp:
        path = f"{tmp}/task-sessions.json"
        first = TaskSessionStore(path)
        awaiting = first.create(
            "仍需确认", "测试", "implicit", state="awaiting-confirmation")
        running = first.create("正在暂停", "测试", "explicit")
        running.update(state="running")
        running.update(state="pausing", control="user")
        second = TaskSessionStore(path)
        assert second.snapshot(awaiting.task_id)["state"] == "awaiting-confirmation"
        paused = second.snapshot(running.task_id)
        assert paused["state"] == "paused"
        assert paused["control"] == "user"


if __name__ == "__main__":
    test_session_identity_mode_and_checkpoint()
    test_checkpoint_writes_are_serialized()
    test_pause_resume_keeps_identity_and_changes_mode()
    test_evidenced_completed_step_is_atomic_and_persisted()
    test_persistent_store_reloads_same_identity_and_checkpoint()
    test_persistent_running_task_does_not_auto_resume()
    test_identity_fields_terminal_state_and_control_are_invariants()
    test_snapshot_and_checkpoint_are_deep_copies()
    test_confirmation_requires_bound_one_time_credential()
    test_executor_epoch_discards_stale_checkpoint_and_duplicate_claim()
    test_execution_outcome_and_evidence_commit_is_epoch_atomic()
    test_restart_preserves_confirmation_and_pausing_becomes_paused()
    print("TASK_SESSION_TEST PASS")
