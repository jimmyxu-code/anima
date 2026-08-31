# -*- coding: utf-8 -*-
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
"""test_voice_fc：voice-fc 插件 + companion_rt FC 胶水层离线测试。

覆盖（2026-08-22 全双工 FC 上线）：
- 工具声明形态（session.tools 标准 JSON Schema）
- run() 分发：web_search 委托 exec-native 服务 / look_screen 隐私闸 /
  未知工具 fail-closed
- _handle_function_call：items 数组形态 + 单条平铺形态解析、call_id 原样
  回传、坏 arguments 容错、_fc_fired 实况登记（路由协调用）
"""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import plugins.voice_fc as voice_fc
import companion_rt
import privacy
import task_overlay


class _FakeCtx:
    def __init__(self, svcs):
        self._svcs = svcs

    def svc(self, name, default=None):
        return self._svcs.get(name, default)

    def provide(self, name, service):
        self._svcs[name] = service

    def effect(self, fn):
        pass


def test_tools_schema():
    ctx = _FakeCtx({})
    voice_fc.register(ctx)
    tools = ctx.svc("voice-fc")["tools"]()
    names = {t["name"] for t in tools}
    # 统一人格（2026-08-22）：语音层有手（run_task）有账（task_status）
    # 有记忆（recall_memory），模型不再自称"没法操作电脑/记不住"
    assert names == {"web_search", "look_screen", "run_task", "task_status",
                     "recall_memory"}, names
    for t in tools:
        assert t["type"] == "function"
        assert t["parameters"]["type"] == "object"
    print("ok 工具声明形态（四件）")


def test_run_dispatch():
    calls = []
    ctx = _FakeCtx({"exec-native": {
        "web_search": lambda q: calls.append(q) or f"结果:{q}"}})
    voice_fc.register(ctx)
    svc = ctx.svc("voice-fc")
    svc["bind"](dispatcher=lambda t, m: f"已受理:{t[:10]}",
                status=lambda: "任务X 状态=running")
    run = svc["run"]
    out = run("web_search", {"query": "上海天气"}, log=lambda m: None)
    assert calls == ["上海天气"] and "结果" in out
    # run_task / task_status 走绑定通道
    assert run("run_task", {"task": "打开记事本写一句话"},
               log=lambda m: None).startswith("已受理")
    assert "running" in run("task_status", {}, log=lambda m: None)
    # 未装配 exec-native → 如实报未装配
    ctx2 = _FakeCtx({})
    voice_fc.register(ctx2)
    run2 = ctx2.svc("voice-fc")["run"]
    out2 = run2("web_search", {"query": "x"}, log=lambda m: None)
    assert "未装配" in out2
    # 未绑定通道 → 如实报（不崩不装）
    assert "没接上" in run2("run_task", {"task": "x"}, log=lambda m: None)
    assert "查不到" in run2("task_status", {}, log=lambda m: None)
    # 未知工具 fail-closed
    out3 = run2("rm_rf", {}, log=lambda m: None)
    assert "没有这个工具" in out3
    print("ok run 分发+绑定通道+兜底")


def test_look_screen_privacy_gate():
    ctx = _FakeCtx({})
    voice_fc.register(ctx)
    run = ctx.svc("voice-fc")["run"]
    orig = privacy.is_on
    privacy.__dict__["is_on"] = lambda: True   # 隐私开 → 必须拒看
    try:
        out = run("look_screen", {"question": "屏幕"}, log=lambda m: None)
    finally:
        privacy.__dict__["is_on"] = orig
    assert "隐私模式" in out
    print("ok look_screen 隐私闸")


class _FakeRT:
    def __init__(self):
        self.sent = []

    def send_function_output(self, call_id, output):
        self.sent.append((call_id, output))


def _install_fake(companion_svc, fake_rt):
    companion_rt._kernel._services["voice-fc"] = companion_svc
    old_rt = companion_rt.rt
    old_conn = companion_rt._state["connected"]
    companion_rt.rt = fake_rt
    companion_rt._state["connected"] = True
    # 不弹真实浮层窗
    old_b, old_e = task_overlay.work_begin, task_overlay.work_end
    task_overlay.work_begin = lambda k, d, grace=None: None
    task_overlay.work_end = lambda k: None
    return old_rt, old_conn, old_b, old_e


def _restore(fake, old_rt, old_conn, old_b, old_e):
    companion_rt.rt = old_rt
    companion_rt._state["connected"] = old_conn
    task_overlay.work_begin, task_overlay.work_end = old_b, old_e
    companion_rt._kernel._services.pop("voice-fc", None)


def test_handle_fc_items_form():
    seen = []
    svc = {"run": lambda n, a, log=None: seen.append((n, a)) or "OK"}
    fake = _FakeRT()
    saved = _install_fake(svc, fake)
    try:
        obj = {"type": "response.function_call_arguments.done",
               "items": [{"call_id": "cid-1", "name": "web_search",
                          "arguments": json.dumps({"query": "天气"})}]}
        companion_rt._handle_function_call(obj)
    finally:
        _restore(fake, *saved)
    assert seen == [("web_search", {"query": "天气"})], seen
    assert fake.sent == [("cid-1", "OK")], fake.sent
    assert companion_rt._fc_fired["tool"] == "web_search"
    assert companion_rt._fc_fired["ts"] > 0
    print("ok items 数组形态 + call_id 原样回传 + 实况登记")


def test_handle_fc_flat_form_and_bad_args():
    seen = []
    svc = {"run": lambda n, a, log=None: seen.append((n, a)) or "OK"}
    fake = _FakeRT()
    saved = _install_fake(svc, fake)
    try:
        companion_rt._handle_function_call(
            {"call_id": "cid-2", "name": "look_screen",
             "arguments": "{坏JSON"})
    finally:
        _restore(fake, *saved)
    assert seen == [("look_screen", {})], seen
    assert fake.sent == [("cid-2", "OK")], fake.sent
    print("ok 单条平铺形态 + 坏 arguments 容错")


def test_handle_fc_no_service_no_crash():
    fake = _FakeRT()
    old_rt, old_conn = companion_rt.rt, companion_rt._state["connected"]
    companion_rt.rt = fake
    companion_rt._state["connected"] = True
    companion_rt._kernel._services.pop("voice-fc", None)
    try:
        companion_rt._handle_function_call({"name": "web_search",
                                            "call_id": "x",
                                            "arguments": "{}"})
    finally:
        companion_rt.rt = old_rt
        companion_rt._state["connected"] = old_conn
    assert fake.sent == []
    print("ok 插件未装配静默不崩（fail-closed）")


def test_recall_memory_real_path():
    """recall_memory 真调用（2026-08-31 实锤：用了 prospective 没 import，
    三次生产调用全 NameError，模型如实说"没记住"=用户感知记忆不存在）。"""
    import tempfile
    import soul as _soul
    import prospective as _pro
    ctx = _FakeCtx({})
    voice_fc.register(ctx)
    run = ctx.svc("voice-fc")["run"]
    tmpd = tempfile.mkdtemp()
    orig_mem, orig_pending = _soul.MEMORY_PATH, _pro.PENDING_PATH
    _soul.MEMORY_PATH = os.path.join(tmpd, "MEMORY.md")
    _pro.PENDING_PATH = os.path.join(tmpd, "pending.json")
    try:
        with open(_soul.MEMORY_PATH, "w", encoding="utf-8") as f:
            f.write("# 长期事实\n- 用户喜欢深色主题〈2026-08-20·preference·#2〉\n"
                    "# 会话摘要\n# 任务账本\n# 归档\n")
        out = run("recall_memory", {"query": "用户喜欢什么主题"},
                  log=lambda m: None)
        assert "执行失败" not in out and "没查到" not in out, out
        assert "深色主题" in out, out
        out2 = run("recall_memory", {"query": "八竿子打不着的随机词"},
                   log=lambda m: None)
        assert "没查到" in out2 or "没记住" in out2, out2
    finally:
        _soul.MEMORY_PATH, _pro.PENDING_PATH = orig_mem, orig_pending
    print("ok recall_memory 真链：命中/零命中都诚实（无 NameError）")


if __name__ == "__main__":
    test_tools_schema()
    test_run_dispatch()
    test_look_screen_privacy_gate()
    test_recall_memory_real_path()
    test_handle_fc_items_form()
    test_handle_fc_flat_form_and_bad_args()
    test_handle_fc_no_service_no_crash()
    print("test_voice_fc: ALL PASS")
