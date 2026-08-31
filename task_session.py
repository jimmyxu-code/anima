"""任务会话状态（助手级前后台迁移的第一阶段内核）。

这里先提供进程内、线程安全的任务身份和检查点；执行器仍可逐步接入。
任何执行通道都应携带同一个 task_id，禁止用相似文本重新创建副本。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


MODES = {"implicit", "explicit", "isolated"}
STATES = {
    "queued", "awaiting-confirmation", "running", "pausing", "paused",
    "promoting", "demoting", "completed", "failed", "cancelled",
}
CONTROLS = {"assistant", "user", "system"}
TERMINAL_STATES = {"completed", "failed", "cancelled"}
STATE_TRANSITIONS = {
    "queued": {"awaiting-confirmation", "running", "paused", "cancelled"},
    "awaiting-confirmation": {"cancelled"},
    "running": {"pausing", "paused", "promoting", "demoting",
                "completed", "failed", "cancelled"},
    "pausing": {"paused", "queued", "cancelled", "failed"},
    "paused": {"queued", "cancelled", "failed"},
    "promoting": {"paused", "queued", "cancelled", "failed"},
    "demoting": {"paused", "queued", "cancelled", "failed"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}
_IMMUTABLE_FIELDS = {
    "task_id", "task_text", "original_text", "created_at", "revision",
    "executor_epoch", "cancel_token", "_lock", "_on_change",
}


@dataclass
class TaskSession:
    task_id: str
    task_text: str
    original_text: str
    mode: str = "implicit"
    state: str = "queued"
    revision: int = 0
    control: str = "assistant"
    executor_epoch: int = 0
    cancel_token: str = field(default_factory=lambda: uuid.uuid4().hex)
    completed_steps: list[str] = field(default_factory=list)
    current_step: str = ""
    checkpoint: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    _lock: threading.RLock = field(default_factory=threading.RLock,
                                    init=False, repr=False, compare=False)
    _on_change: Any = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"unsupported task mode: {self.mode}")
        if self.state not in STATES:
            raise ValueError(f"unsupported task state: {self.state}")
        if self.control not in CONTROLS:
            raise ValueError(f"unsupported task control: {self.control}")
        if not isinstance(self.executor_epoch, int) or self.executor_epoch < 0:
            raise ValueError("executor_epoch must be a non-negative integer")
        if not isinstance(self.cancel_token, str) or not self.cancel_token:
            raise ValueError("cancel_token is required")

    def _validate_transition(self, new_state: str) -> None:
        if new_state == self.state:
            return
        if new_state not in STATE_TRANSITIONS[self.state]:
            raise ValueError(
                f"illegal task state transition: {self.state} -> {new_state}")

    def update(self, expected_revision: int | None = None, **changes: Any) -> None:
        with self._lock:
            if expected_revision is not None and self.revision != expected_revision:
                raise RuntimeError("task revision conflict")
            forbidden = _IMMUTABLE_FIELDS.intersection(changes)
            if forbidden:
                raise AttributeError(
                    f"immutable task fields: {', '.join(sorted(forbidden))}")
            for key, value in changes.items():
                if key == "mode" and value not in MODES:
                    raise ValueError(f"unsupported task mode: {value}")
                if key == "state":
                    if value not in STATES:
                        raise ValueError(f"unsupported task state: {value}")
                    self._validate_transition(value)
                if key == "control" and value not in CONTROLS:
                    raise ValueError(f"unsupported task control: {value}")
                if not hasattr(self, key):
                    raise AttributeError(key)
            old_state = self.state
            for key, value in changes.items():
                setattr(self, key, deepcopy(value))
            if old_state == "running" and self.state != "running":
                self.executor_epoch += 1
                self.cancel_token = uuid.uuid4().hex
            self.revision += 1
            self.updated_at = time.time()
            callback = self._on_change
        if callback:
            callback()

    def _executor_matches(self, executor_epoch: int | None,
                          cancel_token: str | None) -> bool:
        if executor_epoch is None and cancel_token is None:
            return True
        return (self.state == "running"
                and executor_epoch == self.executor_epoch
                and cancel_token == self.cancel_token)

    def save_checkpoint(self, expected_revision: int | None = None,
                        executor_epoch: int | None = None,
                        cancel_token: str | None = None, **data: Any) -> int | None:
        with self._lock:
            if expected_revision is not None and self.revision != expected_revision:
                return None
            if not self._executor_matches(executor_epoch, cancel_token):
                return None
            self.checkpoint.update(deepcopy(data))
            self.revision += 1
            self.updated_at = time.time()
            revision = self.revision
            callback = self._on_change
        if callback:
            callback()
        return revision

    def complete_step(self, label: str, executor_epoch: int | None = None,
                      cancel_token: str | None = None,
                      **checkpoint: Any) -> int | None:
        """原子记录有证据的已完成步骤及其检查点。"""
        label = str(label or "").strip()[:240]
        with self._lock:
            if not self._executor_matches(executor_epoch, cancel_token):
                return None
            if label and label not in self.completed_steps:
                self.completed_steps.append(label)
                if len(self.completed_steps) > 200:
                    del self.completed_steps[:-200]
            self.current_step = ""
            self.checkpoint.update(deepcopy(checkpoint))
            self.revision += 1
            self.updated_at = time.time()
            revision = self.revision
            callback = self._on_change
        if callback:
            callback()
        return revision

    def set_current_step(self, label: str, *, executor_epoch: int,
                         cancel_token: str) -> bool:
        with self._lock:
            if not self._executor_matches(executor_epoch, cancel_token):
                return False
            self.current_step = str(label or "")[:120]
            self.revision += 1
            self.updated_at = time.time()
            callback = self._on_change
        if callback:
            callback()
        return True

    def add_amendment(self, text: str, executor_epoch: int | None = None,
                      cancel_token: str | None = None) -> int | None:
        """目标版本化：任务进行中用户修订需求，追加进 checkpoint.amendments
        （task_text/original_text 不可变——保留审计；执行器读 latest_goal()）。
        修订走与模式切换相同的暂停-检查点-同 task_id 恢复通道。"""
        text = str(text or "").strip()[:500]
        if not text:
            return None
        with self._lock:
            if not self._executor_matches(executor_epoch, cancel_token):
                return None
            amendments = self.checkpoint.setdefault("amendments", [])
            amendments.append({"text": text, "revision": self.revision,
                               "ts": time.time()})
            self.revision += 1
            self.updated_at = time.time()
            revision = self.revision
            callback = self._on_change
        if callback:
            callback()
        return revision

    def latest_goal(self) -> str:
        """当前有效目标 = 最后一条修订；无修订即原始任务文本。"""
        amendments = self.checkpoint.get("amendments") or []
        if amendments:
            return str(amendments[-1].get("text") or self.task_text)
        return self.task_text

    def finish_execution(self, *, executor_epoch: int, cancel_token: str,
                         state: str, control: str = "assistant",
                         current_step: str = "", last_error: str = "",
                         checkpoint: dict[str, Any] | None = None) -> bool:
        """由当前执行租约原子提交结果；旧 epoch 无权写终态或检查点。"""
        if state not in {"paused", "completed", "failed", "cancelled"}:
            return False
        if control not in CONTROLS:
            return False
        with self._lock:
            if not self._executor_matches(executor_epoch, cancel_token):
                return False
            self._validate_transition(state)
            self.state = state
            self.control = control
            self.current_step = str(current_step or "")[:120]
            self.last_error = str(last_error or "")[:1000]
            if checkpoint:
                self.checkpoint.update(deepcopy(checkpoint))
            self.executor_epoch += 1
            self.cancel_token = uuid.uuid4().hex
            self.revision += 1
            self.updated_at = time.time()
            callback = self._on_change
        if callback:
            callback()
        return True

    def resume_to_queue(self, mode: str, expected_revision: int | None = None,
                        **checkpoint: Any) -> bool:
        """CAS 普通恢复；只允许 paused/pausing，绝不消费确认态。"""
        if mode not in MODES:
            raise ValueError(f"unsupported task mode: {mode}")
        with self._lock:
            if expected_revision is not None and self.revision != expected_revision:
                return False
            if self.state not in {"paused", "pausing"}:
                return False
            previous_state = self.state
            self.state = "queued"
            self.mode = mode
            self.control = "assistant"
            self.last_error = ""
            self.checkpoint.update(deepcopy(checkpoint))
            self.checkpoint["resumed_from"] = previous_state
            self.revision += 1
            self.updated_at = time.time()
            callback = self._on_change
        if callback:
            callback()
        return True

    def approve_and_queue(self, *, confirmation_id: str, task_id: str,
                          tool_call_id: str, operation_hash: str,
                          expires_at: float, expected_revision: int,
                          mode: str, now_ms: float | None = None) -> bool:
        """校验并一次性消费批准凭据，再原子转 queued。"""
        if mode not in MODES:
            return False
        now_ms = time.time() * 1000 if now_ms is None else float(now_ms)
        with self._lock:
            cp = self.checkpoint
            if (self.state != "awaiting-confirmation"
                    or self.task_id != str(task_id)
                    or self.revision != int(expected_revision)
                    or cp.get("confirmation_consumed") is True
                    or cp.get("confirmation_id") != str(confirmation_id)
                    or cp.get("confirmation_task_id") != str(task_id)
                    or cp.get("confirmation_tool_call_id") != str(tool_call_id)
                    or cp.get("canonical_operation_hash") != str(operation_hash)
                    or float(cp.get("confirmation_expires_at", 0)) != float(expires_at)
                    or not now_ms < float(expires_at)):
                return False
            self.state = "queued"
            self.mode = mode
            self.control = "assistant"
            self.last_error = ""
            for key in ("confirmation_id", "confirmation_task_id",
                        "confirmation_tool_call_id", "canonical_operation_hash",
                        "confirmation_expires_at", "confirmation_expected_revision"):
                cp.pop(key, None)
            cp["confirmation_consumed"] = True
            cp["confirmation_consumed_id"] = str(confirmation_id)
            self.revision += 1
            self.updated_at = time.time()
            callback = self._on_change
        if callback:
            callback()
        return True

    def claim_execution(self) -> dict[str, Any] | None:
        """worker 唯一合法的 queued->running CAS；重复队列项会失败。"""
        with self._lock:
            if self.state != "queued":
                return None
            self.state = "running"
            self.executor_epoch += 1
            self.cancel_token = uuid.uuid4().hex
            self.revision += 1
            self.updated_at = time.time()
            callback = self._on_change
            snapshot = self._snapshot_locked()
        if callback:
            callback()
        return snapshot

    def executor_is_current(self, executor_epoch: int,
                            cancel_token: str) -> bool:
        with self._lock:
            return self._executor_matches(executor_epoch, cancel_token)

    def _snapshot_locked(self) -> dict[str, Any]:
        return deepcopy({
            "task_id": self.task_id,
            "task_text": self.task_text,
            "original_text": self.original_text,
            "mode": self.mode,
            "state": self.state,
            "revision": self.revision,
            "control": self.control,
            "executor_epoch": self.executor_epoch,
            "cancel_token": self.cancel_token,
            "completed_steps": self.completed_steps,
            "current_step": self.current_step,
            "checkpoint": self.checkpoint,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        })

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()


class TaskSessionStore:
    """线程安全的任务身份注册表。

    迁移必须先在旧执行器写 checkpoint，再由新执行器以同一 task_id 接管。
    """

    def __init__(self, path: str | None = None) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, TaskSession] = {}
        self._path = os.path.abspath(path) if path else None
        self._load()

    def _bind(self, session: TaskSession) -> TaskSession:
        session._on_change = self._persist
        return session

    def _load(self) -> None:
        if not self._path:
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, list):
                return
            for item in raw:
                if not isinstance(item, dict):
                    continue
                allowed = {
                    key: item[key] for key in (
                        "task_id", "task_text", "original_text", "mode", "state",
                        "revision", "control", "completed_steps", "current_step",
                        "executor_epoch", "cancel_token", "checkpoint", "last_error",
                        "created_at", "updated_at",
                    ) if key in item
                }
                session = TaskSession(**allowed)
                self._items[session.task_id] = session
                if session.state == "pausing":
                    session.update(
                        state="paused", control="user",
                        last_error="进程重启后暂停已完成，等待用户明确继续")
                elif session.state in {
                        "running", "queued", "promoting", "demoting"}:
                    session.update(
                        state="paused", control="assistant",
                        last_error="进程重启后任务未自动恢复，等待用户明确继续")
                elif session.state == "awaiting-confirmation":
                    session.last_error = "进程重启后仍需重新确认，不能普通恢复"
                self._bind(session)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # 坏的持久化文件不能阻塞启动；新任务仍从空仓开始。
            return

    def _persist(self) -> None:
        if not self._path:
            return
        with self._lock:
            self._prune_locked()
            parent = os.path.dirname(self._path)
            os.makedirs(parent, exist_ok=True)
            data = [session.snapshot() for session in self._items.values()]
            temp = f"{self._path}.{os.getpid()}.tmp"
            try:
                with open(temp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(temp, self._path)
            finally:
                try:
                    if os.path.exists(temp):
                        os.remove(temp)
                except OSError:
                    pass

    _TERMINAL_KEEP_SECONDS = 7 * 86400   # 终态会话保留 7 天
    _MAX_SESSIONS = 200                  # 文件有界：最多留 200 个会话

    def _prune_locked(self) -> None:
        """B4 收口：终态超龄清除 + 总量封顶——否则 task_sessions.json
        随时间无界增长，每次 checkpoint 全量重写变 O(n²) 写入。"""
        import time as _time
        now = _time.time()
        items = list(self._items.values())
        for s in items:
            if (s.state in TERMINAL_STATES
                    and now - (s.updated_at or s.created_at or 0)
                    > self._TERMINAL_KEEP_SECONDS):
                self._items.pop(s.task_id, None)
        if len(self._items) > self._MAX_SESSIONS:
            ordered = sorted(self._items.values(),
                             key=lambda s: s.updated_at or 0)
            for s in ordered[:len(self._items) - self._MAX_SESSIONS]:
                if s.state in TERMINAL_STATES:
                    self._items.pop(s.task_id, None)

    def create(self, task_text: str, original_text: str, mode: str,
               state: str = "queued") -> TaskSession:
        with self._lock:
            session = TaskSession(
                task_id=f"task-{uuid.uuid4().hex[:12]}",
                task_text=task_text,
                original_text=original_text,
                mode=mode,
                state=state,
            )
            self._items[session.task_id] = self._bind(session)
            self._persist()
            return session

    def get(self, task_id: str) -> TaskSession | None:
        with self._lock:
            return self._items.get(task_id)

    def update(self, task_id: str, **changes: Any) -> TaskSession:
        with self._lock:
            session = self._items[task_id]
            session.update(**changes)
            return session

    def checkpoint(self, task_id: str, **data: Any) -> int:
        with self._lock:
            return self._items[task_id].save_checkpoint(**data)

    def resume_to_queue(self, task_id: str, mode: str,
                        expected_revision: int | None = None,
                        **checkpoint: Any) -> bool:
        with self._lock:
            return self._items[task_id].resume_to_queue(
                mode, expected_revision=expected_revision, **checkpoint)

    def approve_and_queue(self, session_id: str, **credential: Any) -> bool:
        with self._lock:
            return self._items[session_id].approve_and_queue(**credential)

    def claim_execution(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            session = self._items.get(task_id)
            return session.claim_execution() if session else None

    def snapshot(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            session = self._items.get(task_id)
            return session.snapshot() if session else None
