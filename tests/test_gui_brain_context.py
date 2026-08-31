import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
"""GUI 本地引擎恢复上下文回归；替换 HTTP，不联网。"""

import json

import gui_brain
from gui_providers import common, ui_tars_local


def test_local_engine_receives_structured_history():
    captured = {}

    class Image:
        size = (100, 50)

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_exc):
            return False
        def read(self):
            return json.dumps({
                "choices": [{"message": {
                    "content": "Thought: done\nAction: finished(content='完成')"}}]
            }).encode("utf-8")

    original_b64 = ui_tars_local.b64_jpeg
    original_open = ui_tars_local.urllib.request.urlopen
    try:
        ui_tars_local.b64_jpeg = lambda _img: ("encoded", 1280, 640)
        def open_request(req, timeout=None):
            captured.update(json.loads(req.data.decode("utf-8")))
            return Response()
        ui_tars_local.urllib.request.urlopen = open_request
        result = gui_brain._think_local(
            "继续任务", Image(),
            history=[("系统恢复状态", "已完成步骤 1"),
                     ("用户协作状态", "光标位置已改变")])
        assert result[1] == "完成"
        messages = captured["messages"]
        assert messages[1:5] == [
            {"role": "user", "content": "系统恢复状态"},
            {"role": "assistant", "content": "已完成步骤 1"},
            {"role": "user", "content": "用户协作状态"},
            {"role": "assistant", "content": "光标位置已改变"},
        ]
    finally:
        ui_tars_local.b64_jpeg = original_b64
        ui_tars_local.urllib.request.urlopen = original_open


if __name__ == "__main__":
    test_local_engine_receives_structured_history()
    print("GUI_BRAIN_CONTEXT_TEST PASS")
