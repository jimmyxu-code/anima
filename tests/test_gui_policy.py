import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
"""GUI 主循环策略回归：替换输入/截图/模型，不操作真实桌面。"""
import time as _time

import gui_agent


def _run_with_thoughts(thoughts, diff_ratio, watcher=None, interactions=None,
                       step_results=None):
    original = {
        "secure": gui_agent._secure_desktop,
        "black": gui_agent._black_frame,
        "diff": gui_agent._diff_ratio,
        "shot": gui_agent.host_input.screenshot,
        "cursor": gui_agent.host_input.cursor_pos,
        "click": gui_agent.host_input.click,
        "to_phys": gui_agent._to_phys,
        "think": gui_agent.gui_brain.think,
        "get_watcher": gui_agent._get_watcher,
        "recipes": gui_agent._RECIPES_PATH,
    }
    try:
        gui_agent._secure_desktop = lambda: False
        gui_agent._black_frame = lambda _img: False
        gui_agent._diff_ratio = lambda _before, _after: diff_ratio
        gui_agent.host_input.screenshot = lambda: object()
        gui_agent.host_input.cursor_pos = lambda: (100, 100)
        gui_agent.host_input.click = lambda *args, **kwargs: True
        gui_agent._to_phys = lambda point: point
        # 配方回放与 VLM 策略回归隔离（2026-08-31 阶段 C：回放命中会绕过
        # 模型循环——本文件回归的是模型循环策略，配方必须用不存在路径隔离）
        gui_agent._RECIPES_PATH = _o.path.join(
            _o.environ.get("TEMP", "/tmp"),
            f"recipes_test_{_o.getpid()}_{_time.time_ns()}.jsonl")
        if watcher is not None:
            gui_agent._get_watcher = lambda: watcher
        queue = list(thoughts)

        def think(*_args, **_kwargs):
            return queue.pop(0)

        gui_agent.gui_brain.think = think
        return gui_agent.run(
            "测试任务", max_steps=3, watch_takeover=watcher is not None,
            on_interaction=(interactions.append if interactions is not None else None),
            on_step_result=(
                (lambda *args: step_results.append(args))
                if step_results is not None else None))
    finally:
        gui_agent._secure_desktop = original["secure"]
        gui_agent._black_frame = original["black"]
        gui_agent._diff_ratio = original["diff"]
        gui_agent.host_input.screenshot = original["shot"]
        gui_agent.host_input.cursor_pos = original["cursor"]
        gui_agent.host_input.click = original["click"]
        gui_agent._to_phys = original["to_phys"]
        gui_agent.gui_brain.think = original["think"]
        gui_agent._get_watcher = original["get_watcher"]


def test_completion_requires_screen_evidence():
    actions = [{"action": "click", "point": (500, 500), "intent": "点击确定"}]
    step_results = []
    result = _run_with_thoughts(
        [(actions, None, "", 0.0), ([], "已完成", "", 0.0)], 0.01,
        step_results=step_results)
    assert result[0] is True
    assert step_results and step_results[0][0] == 1
    assert step_results[0][2] is True

    result = _run_with_thoughts(
        [([], "已完成", "", 0.0)], 0.01)
    assert result[0] is False
    # 2026-08-28 话术对齐：无独立证据的完成现在是"没能核实"（gui_agent.py）
    assert "核实" in result[1]


def test_small_mouse_move_is_cooperation():
    class Watcher:
        def __init__(self):
            self.calls = 0

        def interaction_since(self, _t0, **_kwargs):
            self.calls += 1
            # 轨迹内会多次检查；轻微移动在整步内都保持 cooperation。
            return "cooperate"

    interactions = []
    actions = [{"action": "click", "point": (500, 500), "intent": "点击确定"}]
    result = _run_with_thoughts(
        [(actions, None, "", 0.0), ([], "已完成", "", 0.0)],
        0.01, watcher=Watcher(), interactions=interactions)
    assert result[0] is True
    assert interactions == ["cooperate"]


def test_click_during_model_gap_is_cooperation_not_stop():
    """2026-08-26 用户裁决口径：动作前检测到用户物理输入=不打断（像远程
    同事协作，下一步重新观察屏幕吸收），动作照常执行；只有钩子死亡才暂停。"""
    clicked = []

    class Watcher:
        def __init__(self):
            self.calls = 0
        def healthy(self):
            return True
        def cursor(self):
            return 0
        def consume_since(self, seq, **_kwargs):
            self.calls += 1
            return ((None, 0) if self.calls == 1 else ("takeover", 1))

    original = {
        "secure": gui_agent._secure_desktop,
        "black": gui_agent._black_frame,
        "diff": gui_agent._diff_ratio,
        "shot": gui_agent.host_input.screenshot,
        "cursor": gui_agent.host_input.cursor_pos,
        "click": gui_agent.host_input.click,
        "think": gui_agent.gui_brain.think,
        "watcher": gui_agent._get_watcher,
    }
    try:
        gui_agent._secure_desktop = lambda: False
        gui_agent._black_frame = lambda _img: False
        # 动作后事件校验（_diff_ratio）加入主循环后本用例的 mock 没跟上
        gui_agent._diff_ratio = lambda _before, _after: 0.0
        gui_agent.host_input.screenshot = lambda: object()
        gui_agent.host_input.cursor_pos = lambda: (0, 0)
        gui_agent.host_input.click = lambda *_args, **_kwargs: clicked.append(True)
        gui_agent.gui_brain.think = lambda *_args, **_kwargs: (
            [{"action": "click", "point": (500, 500), "intent": "测试"}],
            None, "", 0.0)
        gui_agent._get_watcher = lambda: Watcher()
        result = gui_agent.run("测试", max_steps=1, watch_takeover=True)
        assert result[0] is False                      # max_steps=1 用尽，未完成
        assert "超过最大步数" in result[1]
        assert clicked == [True]                       # 物理输入不打断动作执行
    finally:
        gui_agent._secure_desktop = original["secure"]
        gui_agent._black_frame = original["black"]
        gui_agent._diff_ratio = original["diff"]
        gui_agent.host_input.screenshot = original["shot"]
        gui_agent.host_input.cursor_pos = original["cursor"]
        gui_agent.host_input.click = original["click"]
        gui_agent.gui_brain.think = original["think"]
        gui_agent._get_watcher = original["watcher"]


def test_hook_health_and_decisive_latch_survive_queue_overflow():
    partial = gui_agent.TakeoverWatcher()
    partial._ready.set()
    partial.hook_handles = (1, None)
    assert partial.healthy() is False
    assert {0x0104, 0x0105}.issubset(gui_agent._WM_KEY_DECISIVE)
    assert {0x0207, 0x0208, 0x020B, 0x020C, 0x020E}.issubset(
        gui_agent._WM_MOUSE_DECISIVE)

    watcher = gui_agent.TakeoverWatcher()
    watcher._ready.set()
    watcher.hook_handles = (1, 1)
    watcher._record("mouse", 10, 10)
    for _ in range(300):
        watcher._record("move", 10, 10)
    assert len(watcher._events) == 256
    interaction, _seq = watcher.consume_since(0, origin=(10, 10))
    assert interaction == "takeover"


def test_overlay_change_is_not_action_evidence():
    shots = iter(("initial", "overlay", "overlay", "next"))
    pairs = []
    thoughts = iter((
        ([{"action": "click", "point": (500, 500), "intent": "测试"}],
         None, "", 0.0),
        ([], "模型报告完成", "", 0.0),
    ))
    original = {
        "secure": gui_agent._secure_desktop,
        "black": gui_agent._black_frame,
        "shot": gui_agent.host_input.screenshot,
        "cursor": gui_agent.host_input.cursor_pos,
        "click": gui_agent.host_input.click,
        "to_phys": gui_agent._to_phys,
        "diff": gui_agent._diff_ratio,
        "think": gui_agent.gui_brain.think,
        "focus": gui_agent.uia_tools.focused_element,
    }
    try:
        gui_agent._secure_desktop = lambda: False
        gui_agent._black_frame = lambda _img: False
        gui_agent.host_input.screenshot = lambda: next(shots)
        gui_agent.host_input.cursor_pos = lambda: (0, 0)
        gui_agent.host_input.click = lambda *_args, **_kwargs: True
        gui_agent._to_phys = lambda point: point
        # UIA 焦点豁免依赖真实桌面焦点，测试中必须打桩，
        # 否则活动桌面焦点跳变会被当成动作证据（环境脆弱性）。
        gui_agent.uia_tools.focused_element = lambda: None
        gui_agent._diff_ratio = lambda before, after: (
            pairs.append((before, after)) or (0.02 if before != after else 0.0))
        gui_agent.gui_brain.think = lambda *_args, **_kwargs: next(thoughts)
        result = gui_agent.run("测试", max_steps=2, watch_takeover=False)
        assert result[0] is False
        assert pairs == [("overlay", "overlay")]
    finally:
        gui_agent._secure_desktop = original["secure"]
        gui_agent._black_frame = original["black"]
        gui_agent.host_input.screenshot = original["shot"]
        gui_agent.host_input.cursor_pos = original["cursor"]
        gui_agent.host_input.click = original["click"]
        gui_agent._to_phys = original["to_phys"]
        gui_agent._diff_ratio = original["diff"]
        gui_agent.gui_brain.think = original["think"]
        gui_agent.uia_tools.focused_element = original["focus"]


def test_wait_does_not_clear_valid_action_evidence():
    click = [{"action": "click", "point": (500, 500), "intent": "测试"}]
    wait = [{"action": "wait", "ms": "1", "intent": "等待界面稳定"}]
    result = _run_with_thoughts(
        [(click, None, "", 0.0), (wait, None, "", 0.0),
         ([], "模型报告完成", "", 0.0)],
        0.01)
    assert result[0] is True


def test_coordinate_mapping_stays_inside_exclusive_bounds_and_rejects_gaps():
    original_size = gui_agent._screen_size
    original_monitor = gui_agent._point_on_monitor
    try:
        gui_agent._screen_size = lambda: (100, 50, -20, -10)
        gui_agent._point_on_monitor = lambda _x, _y: True
        assert gui_agent._to_phys((0, 0)) == (-20, -10)
        assert gui_agent._to_phys((1000, 1000)) == (79, 39)
        gui_agent._point_on_monitor = lambda _x, _y: False
        try:
            gui_agent._to_phys((500, 500))
        except ValueError:
            pass
        else:
            raise AssertionError("sparse monitor gap was accepted")
    finally:
        gui_agent._screen_size = original_size
        gui_agent._point_on_monitor = original_monitor


def test_gui_execution_context_fails_closed_before_desktop_access():
    invalid = gui_agent.run(
        "测试", execution_context={"task_id": "task-1"},
        cancel_check=lambda: False)
    assert invalid[0] is False
    assert "执行租约" in invalid[1]
    valid = {
        "task_id": "task-1", "executor_epoch": 1,
        "cancel_token": "c" * 32, "checkpoint": {},
        "completed_steps": [],
    }
    missing_cancel = gui_agent.run("测试", execution_context=valid)
    assert missing_cancel[0] is False
    mismatch = gui_agent.run(
        "测试", execution_context=valid, cancel_check=lambda: False,
        resume_context={"task_id": "task-other"})
    assert mismatch[0] is False
    assert "身份不匹配" in mismatch[1]


if __name__ == "__main__":
    test_completion_requires_screen_evidence()
    test_small_mouse_move_is_cooperation()
    test_click_during_model_gap_is_cooperation_not_stop()
    test_hook_health_and_decisive_latch_survive_queue_overflow()
    test_overlay_change_is_not_action_evidence()
    test_wait_does_not_clear_valid_action_evidence()
    test_coordinate_mapping_stays_inside_exclusive_bounds_and_rejects_gaps()
    test_gui_execution_context_fails_closed_before_desktop_access()
    print("GUI_POLICY_TEST PASS")
