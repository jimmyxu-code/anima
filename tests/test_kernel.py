# -*- coding: utf-8 -*-
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
"""kernel 微内核回归：服务注册/依赖可见性/瀑布改写与短路/拔除回收/幂等装卸。"""

import kernel


def test_service_lifecycle():
    k = kernel.Kernel(log=lambda *a: None)

    class A:
        def register(self, ctx):
            ctx.provide("foo", {"v": 1})

    class B:
        def register(self, ctx):
            self.seen = ctx.svc("foo")

    b = B()
    k.load("a", A())
    k.load("b", b)
    assert b.seen == {"v": 1}
    k.unload("a")                      # 拔掉提供方
    assert k._services.get("foo") is None   # 服务随 fiber 回收
    assert k.plugins() == ["b"]


def test_waterfall_wrap_and_shortcircuit():
    k = kernel.Kernel(log=lambda *a: None)
    calls = []

    class P1:
        def register(self, ctx):
            def h(payload, nxt):
                calls.append("p1-pre")
                r = nxt(payload + ["p1"])
                calls.append("p1-post")
                return r + ["p1out"]
            ctx.on("ev", h)

    class P2:
        def register(self, ctx):
            def h(payload, nxt):
                calls.append("p2")
                return nxt(payload + ["p2"])
            ctx.on("ev", h)

    k.load("p1", P1())
    k.load("p2", P2())
    out = Ctx = k._fibers["p1"] and None
    ctx = None
    # 通过任一 fiber 的 ctx 发瀑布（后注册的 p2 为最外层）
    f = k._fibers["p1"]
    ctx = kernel.Ctx(k, f)
    r = ctx.waterfall("ev", [])
    assert calls == ["p2", "p1-pre", "p1-post"], calls
    assert r == ["p2", "p1", "p1out"], r

    # 短路：新插件不调 next → 内层 P1/P2 都收不到
    class P0:
        def register(self, ctx):
            ctx.on("ev", lambda payload, nxt: ["blocked"])

    k.load("p0", P0())
    calls.clear()
    r = ctx.waterfall("ev", [])
    assert r == ["blocked"] and calls == [], (r, calls)

    k.unload("p0")
    calls.clear()
    r = ctx.waterfall("ev", [])
    assert r == ["p2", "p1", "p1out"]   # 拔除后链路恢复


def test_unload_undoes_listeners_and_is_idempotent():
    k = kernel.Kernel(log=lambda *a: None)
    hits = []

    class L:
        def register(self, ctx):
            ctx.on("tick", lambda p: hits.append(p))

    k.load("l", L())
    ctx = kernel.Ctx(k, k._fibers["l"])
    ctx.emit("tick", 1)
    k.unload("l")
    ctx.emit("tick", 2)
    assert hits == [1]
    assert k.unload("l") is False      # 幂等
    k.load("l", L())                   # 重装可用
    ctx2 = kernel.Ctx(k, k._fibers["l"])
    ctx2.emit("tick", 3)
    assert hits == [1, 3]


def test_chat_survives_exec_unplug():
    """验收口径：拔掉执行插件不影响聊天插件的服务面。"""
    k = kernel.Kernel(log=lambda *a: None)

    class Chat:
        def register(self, ctx):
            ctx.provide("chat", lambda: "ok")

    class Exec:
        def register(self, ctx):
            ctx.provide("exec", lambda: 1 / 0)

    k.load("chat", Chat())
    k.load("exec", Exec())
    k.unload("exec")
    assert k._services["chat"]() == "ok"
    assert not k.has("exec")


if __name__ == "__main__":
    test_service_lifecycle()
    test_waterfall_wrap_and_shortcircuit()
    test_unload_undoes_listeners_and_is_idempotent()
    test_chat_survives_exec_unplug()
    print("KERNEL_TEST PASS")
