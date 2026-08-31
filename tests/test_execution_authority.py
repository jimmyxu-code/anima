import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
"""执行租约发布/撤销竞态回归；只使用临时文件。"""

import json
import os
import threading
from tempfile import TemporaryDirectory

import execution_authority


def _context(epoch):
    return {
        "task_id": "task-1",
        "executor_epoch": epoch,
        "cancel_token": str(epoch) * 32,
    }


def test_stale_revoke_never_erases_new_epoch():
    writable_tmp = os.environ.get(
        "CODEX_TEST_TMP", r"C:\Users\tester\Documents\Codex")
    old_path = execution_authority.AUTHORITY_PATH
    try:
        with TemporaryDirectory(dir=writable_tmp) as tmp:
            execution_authority.AUTHORITY_PATH = os.path.join(tmp, "authority.json")
            old = _context(1)
            new = _context(2)
            assert execution_authority.publish(old)
            start = threading.Barrier(3)

            def revoke_old():
                start.wait()
                execution_authority.revoke(old)

            def publish_new():
                start.wait()
                execution_authority.publish(new)

            threads = [threading.Thread(target=revoke_old),
                       threading.Thread(target=publish_new)]
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join()
            active = execution_authority.snapshot()
            assert active["executor_epoch"] == 2
            assert active["cancel_token"] == new["cancel_token"]
            assert execution_authority.revoke(old) is False
            assert execution_authority.snapshot()["executor_epoch"] == 2
            assert execution_authority.revoke(new) is True
            assert execution_authority.snapshot() == {}
            assert execution_authority.publish(new) is True
            assert execution_authority.revoke_all() is True
            assert execution_authority.snapshot() == {}
    finally:
        execution_authority.AUTHORITY_PATH = old_path


if __name__ == "__main__":
    test_stale_revoke_never_erases_new_epoch()
    print("EXECUTION_AUTHORITY_TEST PASS")
