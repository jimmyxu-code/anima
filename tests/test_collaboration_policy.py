import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
"""人机协作输入策略的离线回归，不安装 hook、不操作桌面。"""

from collaboration_policy import classify_real_input


def test_no_input_is_ignored():
    assert classify_real_input([]) is None


def test_small_mouse_adjustment_is_cooperation():
    events = [(10.0, "move", 100, 100), (10.2, "move", 118, 112)]
    assert classify_real_input(events) == "cooperate"


def test_large_or_long_mouse_motion_is_takeover():
    assert classify_real_input([(1.0, "move", 100, 100),
                                (1.2, "move", 200, 100)]) == "takeover"
    assert classify_real_input([(1.0, "move", 100, 100),
                                (2.4, "move", 110, 100)]) == "takeover"


def test_single_move_uses_step_origin():
    event = [(1.0, "move", 110, 108)]
    assert classify_real_input(event, origin=(100, 100)) == "cooperate"
    assert classify_real_input(event, origin=(0, 0)) == "takeover"


def test_malformed_origin_fails_closed():
    event = [(1.0, "move", 110, 108)]
    assert classify_real_input(event, origin=(100,)) == "takeover"
    assert classify_real_input(event, origin=("bad", 100)) == "takeover"


def test_click_key_and_unknown_input_are_takeover():
    for kind in ("mouse", "key", "unknown"):
        assert classify_real_input([(1.0, kind, 100, 100)]) == "takeover"
    assert classify_real_input([(1.0, "move")]) == "takeover"


if __name__ == "__main__":
    test_no_input_is_ignored()
    test_small_mouse_adjustment_is_cooperation()
    test_large_or_long_mouse_motion_is_takeover()
    test_single_move_uses_step_origin()
    test_malformed_origin_fails_closed()
    test_click_key_and_unknown_input_are_takeover()
    print("COLLABORATION_POLICY_TEST PASS")
