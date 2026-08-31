import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
"""运行中模式切换指令的离线回归。"""

from mode_switch_policy import target_mode


def test_mode_switch_phrases():
    assert target_mode("把这个转到后台继续") == "implicit"
    assert target_mode("调到后台继续") == "implicit"
    assert target_mode("调到前台来，我要看着") == "explicit"
    assert target_mode("让我看看你怎么操作的") == "explicit"
    assert target_mode("我想看一下过程") == "explicit"
    assert target_mode("先别动") is None


if __name__ == "__main__":
    test_mode_switch_phrases()
    print("MODE_SWITCH_POLICY_TEST PASS")
