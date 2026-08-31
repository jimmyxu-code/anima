# -*- coding: utf-8 -*-
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
"""Provider 与取消/租约/证据链组合压力回归；不触碰桌面、模型或网络。"""

import threading
from concurrent.futures import ThreadPoolExecutor

import companion_rt
import gui_agent
import task_session
from gui_provider import (
    GuiModelProvider,
    GuiModelRequest,
    GuiModelResponse,
    ProviderActionRoutingRequired,
    ProviderCapabilities,
    ProviderRegistry,
    ProviderUnavailable,
)
from gui_providers.qwen_ui_agent import QwenUiAgentProvider


def _request(history=None):
    return GuiModelRequest.build("组合回归", object(), history=history)


def _gui_action():
    return {"action": "click", "point": (500, 500), "intent": "选择目标"}


def test_mixed_batches_never_leak_gui_prefix_under_concurrency():
    response = GuiModelResponse.build("hybrid", actions=[
        _gui_action(),
        {"action": "cli_command", "command": "whoami", "intent": "读取身份"},
    ])
    barrier = threading.Barrier(33)
    executed = []
    lock = threading.Lock()

    def route_once():
        barrier.wait()
        try:
            actions = response.require_direct_gui_actions()
        except ProviderActionRoutingRequired:
            return "rejected"
        with lock:
            executed.extend(actions)
        return "leaked"

    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = [pool.submit(route_once) for _ in range(32)]
        barrier.wait()
        outcomes = [future.result() for future in futures]
    assert outcomes == ["rejected"] * 32
    assert executed == []


def test_qwen_and_unknown_provider_never_fallback_to_seed():
    class CountingSeed(GuiModelProvider):
        capabilities = ProviderCapabilities(
            name="seed_ark", action_channels=frozenset({"gui"}))

        def __init__(self):
            self.calls = 0
            self.lock = threading.Lock()

        def _invoke(self, _request_value):
            with self.lock:
                self.calls += 1
            return GuiModelResponse.build("seed_ark", actions=[_gui_action()])

    seed = CountingSeed()
    registry = ProviderRegistry((seed, QwenUiAgentProvider()))

    def rejected(name):
        try:
            registry.resolve(name).invoke(_request())
        except ProviderUnavailable:
            return True
        return False

    names = ["qwen_ui_agent", "missing-provider"] * 24
    with ThreadPoolExecutor(max_workers=16) as pool:
        assert all(pool.map(rejected, names))
    assert seed.calls == 0


def test_resume_history_snapshots_survive_concurrent_external_mutation():
    history = [["恢复状态", "已完成第一步"]]
    with ThreadPoolExecutor(max_workers=16) as pool:
        requests = list(pool.map(lambda _index: _request(history), range(64)))
    history[0][1] = "外部修改"
    history.append(["额外状态", "不得注入旧快照"])
    assert all(
        request.history == (("恢复状态", "已完成第一步"),)
        for request in requests
    )


def test_cancelled_lease_rejects_late_provider_checkpoint_and_evidence():
    session = task_session.TaskSessionStore().create(
        "组合回归", "原始请求", "explicit")
    lease = session.claim_execution()
    assert lease is not None
    late_response = GuiModelResponse.build("late", actions=[_gui_action()])
    assert late_response.require_direct_gui_actions() == [_gui_action()]

    session.update(state="cancelled", control="user")
    epoch = lease["executor_epoch"]
    token = lease["cancel_token"]
    before = session.snapshot()
    assert session.save_checkpoint(
        executor_epoch=epoch, cancel_token=token, late_model=True) is None
    assert session.complete_step(
        "模型迟到动作", executor_epoch=epoch, cancel_token=token,
        evidence="untrusted") is None
    assert session.finish_execution(
        executor_epoch=epoch, cancel_token=token, state="completed",
        checkpoint={"late_completion": True}) is False
    assert companion_rt._record_background_model_report(
        session, "模型说完成", lease) is False
    assert session.snapshot() == before


def test_cancel_vs_verified_finish_has_single_atomic_winner():
    for _index in range(40):
        session = task_session.TaskSessionStore().create(
            "竞态回归", "原始请求", "explicit")
        lease = session.claim_execution()
        barrier = threading.Barrier(3)
        outcomes = {}

        def finish():
            barrier.wait()
            outcomes["finish"] = session.finish_execution(
                executor_epoch=lease["executor_epoch"],
                cancel_token=lease["cancel_token"], state="completed",
                checkpoint={"verified_evidence": True})

        def cancel():
            barrier.wait()
            try:
                session.update(state="cancelled", control="user")
                outcomes["cancel"] = True
            except ValueError:
                outcomes["cancel"] = False

        finish_thread = threading.Thread(target=finish)
        cancel_thread = threading.Thread(target=cancel)
        finish_thread.start()
        cancel_thread.start()
        barrier.wait()
        finish_thread.join()
        cancel_thread.join()
        assert int(outcomes["finish"]) + int(outcomes["cancel"]) == 1
        snapshot = session.snapshot()
        assert snapshot["state"] in {"completed", "cancelled"}
        assert bool(snapshot["checkpoint"].get("verified_evidence")) is bool(
            outcomes["finish"])


def test_stale_gui_lease_stops_before_screenshot_or_model():
    session = task_session.TaskSessionStore().create(
        "过期租约", "原始请求", "explicit")
    lease = session.claim_execution()
    session.update(state="cancelled", control="user")
    calls = {"screenshot": 0, "model": 0}
    original_screenshot = gui_agent.host_input.screenshot
    original_think = gui_agent.gui_brain.think
    try:
        gui_agent._STOP.clear()
        gui_agent.host_input.screenshot = lambda: calls.__setitem__(
            "screenshot", calls["screenshot"] + 1)
        gui_agent.gui_brain.think = lambda *_args, **_kwargs: calls.__setitem__(
            "model", calls["model"] + 1)
        result = gui_agent.run(
            "过期租约", watch_takeover=False, execution_context=lease,
            cancel_check=lambda: not session.executor_is_current(
                lease["executor_epoch"], lease["cancel_token"]),
        )
        assert result[0] is False
        assert calls == {"screenshot": 0, "model": 0}
    finally:
        gui_agent.host_input.screenshot = original_screenshot
        gui_agent.gui_brain.think = original_think
        gui_agent._STOP.clear()


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"PROVIDER_EXECUTION_COMPOSITION_TEST PASS {len(tests)}/{len(tests)}")
