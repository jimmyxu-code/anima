# -*- coding: utf-8 -*-
"""“退下”完整退出链离线回归；所有外部动作均替身，不碰活服务和桌面。"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import companion_rt as rt
import tts_speaker
from plugins import exec_native


events = []
stopped = threading.Event()
rt.log = lambda message: events.append(("log", message))


class FakeWake:
    def stop(self):
        events.append("wake_stop")


class FakeIcon:
    def stop(self):
        events.append("icon_stop")
        stopped.set()


rt._tray_icon = FakeIcon()
rt._wake_listener = FakeWake()
rt.sense_io.orb_hide = lambda: events.append("orb_hide")
rt.sense_io.stop_stream_capture = lambda: events.append("mic_stop")
rt.task_overlay.set_panel = lambda v: events.append(("panel", v))
rt.task_overlay.session_open = lambda v: events.append(("overlay_session", v))
rt.task_overlay.hide = lambda: events.append("overlay_hide")
rt.gui_agent.request_stop = lambda: events.append("gui_stop")
rt._mark_execution_cancelled = lambda *a, **k: (None, None, None)
rt._abort_task_channels_async = lambda *a, **k: events.append("channels_stop")
rt._drain_queue = lambda why: events.append(("drain", why))
rt._task_active_sync = lambda: events.append("task_sync")
exec_native.stop_all_children = lambda log=print: events.append("children_stop")
rt.player.interrupt = lambda **k: events.append("player_stop")
rt.rt_close = lambda: events.append("rt_close")
tts_speaker.speak = lambda text, log=print: events.append(("speak", text))

assert rt._retire_companion() is True
assert rt._retire_companion() is False  # 幂等，不并发退出两次
assert stopped.wait(1.5), events

required = [
    "orb_hide", ("panel", False), ("overlay_session", False), "overlay_hide",
    "gui_stop", "children_stop", "wake_stop", "mic_stop", "player_stop",
    "rt_close", ("speak", "好，我退下了"), "icon_stop",
]
for item in required:
    assert item in events, (item, events)
assert events.index("orb_hide") < events.index("children_stop")
assert events.index("rt_close") < events.index(("speak", "好，我退下了"))
assert events[-1] == "icon_stop"

print("RETIRE_SHUTDOWN_TEST PASS")
