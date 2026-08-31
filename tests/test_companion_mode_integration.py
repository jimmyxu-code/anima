import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
"""现役 rt 入口的前后台切换集成回归，不启动语音、模型或真实桌面。"""

import os
import queue
import tempfile

import companion_rt
import task_session


def test_same_session_switches_both_directions():
    original = {
        "sessions": companion_rt._sessions,
        "task_q": companion_rt._task_q,
        "inject": companion_rt._inject_rag,
        "request_stop": companion_rt.gui_agent.request_stop,
        "abort": companion_rt._abort_task_channels_async,
        "current": dict(companion_rt._task_current),
        "queued": companion_rt._queued_task_ids,
        "active": companion_rt._active_task_ids,
        "sigs": companion_rt._scheduled_sigs,
        "cancelled": companion_rt._cancelled_leases,
    }
    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            companion_rt._sessions = task_session.TaskSessionStore(
                os.path.join(temp_dir, "sessions.json"))
            companion_rt._task_q = queue.Queue()
            companion_rt._queued_task_ids = set()
            companion_rt._active_task_ids = set()
            companion_rt._scheduled_sigs = {}
            companion_rt._cancelled_leases = set()
            companion_rt._inject_rag = lambda *_args, **_kwargs: None
            companion_rt.gui_agent.request_stop = lambda: None
            companion_rt._abort_task_channels_async = lambda *_args: None

            session = companion_rt._sessions.create("演示任务", "调到后台", "explicit")
            session.update(state="running")
            companion_rt._task_current["id"] = session.task_id
            companion_rt._task_active.set()
            companion_rt._on_user_text("调到后台")
            assert session.state == "pausing"
            assert companion_rt._consume_mode_request(session.task_id) == "implicit"
            companion_rt._resume_session(
                session.task_id, session.task_text, session.original_text, "implicit")
            assert companion_rt._task_q.get_nowait() == session.task_id
            companion_rt._queued_task_ids.discard(session.task_id)
            assert session.mode == "implicit"

            assert companion_rt._sessions.claim_execution(session.task_id)
            companion_rt._task_current["id"] = session.task_id
            companion_rt._task_active.set()
            companion_rt._on_user_text("调到前台")
            assert session.state == "pausing"
            assert companion_rt._consume_mode_request(session.task_id) == "explicit"
            companion_rt._resume_session(
                session.task_id, session.task_text, session.original_text, "explicit")
            assert companion_rt._task_q.get_nowait() == session.task_id
            companion_rt._queued_task_ids.discard(session.task_id)
            assert session.mode == "explicit"

            assert companion_rt._resume_session(
                "missing-task", "演示任务", "继续", "implicit") is False
            assert companion_rt._task_q.empty()

            assert companion_rt._sessions.claim_execution(session.task_id)
            session.update(state="completed")
            assert companion_rt._resume_session(
                session.task_id, session.task_text, session.original_text,
                "implicit") is False
            assert companion_rt._task_q.empty()

            # 迁移尚未完成时，最新口令覆盖旧口令；“转后台→还是前台”
            # 不得继续执行过时的后台切换。
            migration = companion_rt._sessions.create(
                "另一个迁移任务", "切换", "explicit")
            migration.update(state="running")
            companion_rt._task_current["id"] = migration.task_id
            companion_rt._task_active.set()
            companion_rt._on_user_text("调到后台")
            companion_rt._on_user_text("还是调到前台")
            assert companion_rt._consume_mode_request(migration.task_id) == "explicit"
            companion_rt._resume_session(
                migration.task_id, migration.task_text,
                migration.original_text, "explicit")
            assert companion_rt._task_q.get_nowait() == migration.task_id
            companion_rt._queued_task_ids.discard(migration.task_id)
            assert migration.mode == "explicit"
        finally:
            companion_rt._task_active.clear()
            companion_rt._sessions = original["sessions"]
            companion_rt._task_q = original["task_q"]
            companion_rt._inject_rag = original["inject"]
            companion_rt.gui_agent.request_stop = original["request_stop"]
            companion_rt._abort_task_channels_async = original["abort"]
            companion_rt._task_current.clear()
            companion_rt._task_current.update(original["current"])
            companion_rt._queued_task_ids = original["queued"]
            companion_rt._active_task_ids = original["active"]
            companion_rt._scheduled_sigs = original["sigs"]
            companion_rt._cancelled_leases = original["cancelled"]


if __name__ == "__main__":
    test_same_session_switches_both_directions()
    print("COMPANION_MODE_INTEGRATION_TEST PASS")
