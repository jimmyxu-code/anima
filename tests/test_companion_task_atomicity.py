import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
"""任务调度原子性回归；不启动 worker、语音、模型、网络或桌面。"""

from concurrent.futures import ThreadPoolExecutor
import queue
import time

import companion_rt
import task_session


class _IsolatedScheduler:
    def __enter__(self):
        self.original = {
            "sessions": companion_rt._sessions,
            "task_q": companion_rt._task_q,
            "log": companion_rt.log,
            "inject": companion_rt._inject_rag,
            "queued": companion_rt._queued_task_ids,
            "active": companion_rt._active_task_ids,
            "sigs": companion_rt._scheduled_sigs,
            "cancelled": companion_rt._cancelled_leases,
        }
        companion_rt._sessions = task_session.TaskSessionStore()
        companion_rt._task_q = queue.Queue()
        companion_rt.log = lambda *_args, **_kwargs: None
        companion_rt._inject_rag = lambda *_args, **_kwargs: None
        companion_rt._queued_task_ids = set()
        companion_rt._active_task_ids = set()
        companion_rt._scheduled_sigs = {}
        companion_rt._cancelled_leases = set()
        return companion_rt

    def __exit__(self, *_exc):
        companion_rt._sessions = self.original["sessions"]
        companion_rt._task_q = self.original["task_q"]
        companion_rt.log = self.original["log"]
        companion_rt._inject_rag = self.original["inject"]
        companion_rt._queued_task_ids = self.original["queued"]
        companion_rt._active_task_ids = self.original["active"]
        companion_rt._scheduled_sigs = self.original["sigs"]
        companion_rt._cancelled_leases = self.original["cancelled"]


def test_concurrent_resume_enqueues_same_task_once():
    with _IsolatedScheduler() as rt:
        session = rt._sessions.create("继续原任务", "继续", "implicit")
        session.update(state="paused")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _i: rt._resume_session(
                    session.task_id, session.task_text,
                    session.original_text, "implicit"),
                range(8)))
        assert sum(bool(v) for v in results) == 1
        assert rt._task_q.qsize() == 1
        assert rt._task_q.get_nowait() == session.task_id
        assert session.state == "queued"


def test_concurrent_new_request_uses_existing_text_dedup_semantics():
    with _IsolatedScheduler() as rt:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _i: rt._enqueue_task(
                    "整理同一个文件夹", "帮我整理", "implicit"),
                range(8)))
        assert sum(bool(v) for v in results) == 1
        assert rt._task_q.qsize() == 1
        assert len(rt._scheduled_sigs) == 1


def test_awaiting_confirmation_cannot_resume_without_credential():
    with _IsolatedScheduler() as rt:
        session = rt._sessions.create(
            "危险任务", "测试", "implicit", state="awaiting-confirmation")
        assert rt._resume_session(
            session.task_id, session.task_text,
            session.original_text, "implicit") is False
        assert rt._task_q.empty()
        assert session.state == "awaiting-confirmation"


def test_concurrent_bound_approval_is_consumed_once():
    with _IsolatedScheduler() as rt:
        session = rt._sessions.create(
            "危险任务", "测试", "implicit", state="awaiting-confirmation")
        expires = time.time() * 1000 + 30_000
        expected = session.revision + 1
        credential = {
            "confirmation_id": "confirm-1",
            "confirmation_task_id": session.task_id,
            "confirmation_tool_call_id": "call-1",
            "canonical_operation_hash": "a" * 64,
            "confirmation_expires_at": expires,
            "confirmation_expected_revision": expected,
        }
        session.save_checkpoint(**credential)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _i: rt._approve_and_enqueue(
                    session.task_id, credential, "implicit"), range(8)))
        assert sum(bool(v) for v in results) == 1
        assert rt._task_q.qsize() == 1


if __name__ == "__main__":
    test_concurrent_resume_enqueues_same_task_once()
    test_concurrent_new_request_uses_existing_text_dedup_semantics()
    test_awaiting_confirmation_cannot_resume_without_credential()
    test_concurrent_bound_approval_is_consumed_once()
    print("COMPANION_TASK_ATOMICITY_TEST PASS")
