# -*- coding: utf-8 -*-
"""execution 插件：执行面统一注册口（P2）。

执行器（前台 GUI / 后台 dsh）由 companion_rt 启动时 bind 进来，
消费方经 ctx 服务与 `exec/dispatch` 瀑布——遥测、策略、隔离等
后挂插件在此拦截，不碰执行器本体。拔掉本插件 = 任务无法执行，
聊天/路由不受影响（铁律：干活层故障不拖死聊天）。

诚实边界：本版是"绑定式收编"——执行器函数体仍在 companion_rt
（深度依赖其全局状态），本插件先把调度口收口；函数体的物理外移
是后续单独评审的重构，不为搬而搬。
"""


def register(ctx):
    runners = {"explicit": None, "implicit": None}

    def bind(explicit=None, implicit=None):
        if explicit is not None:
            runners["explicit"] = explicit
        if implicit is not None:
            runners["implicit"] = implicit

    def dispatch(mode, **kw):
        """按模式派发。exec/dispatch 瀑布：无人拦截 → 正常执行；
        拦截方返回非 dict 结果 → 视为已处理（拦截方全责）。"""
        runner = runners.get(mode) or runners["implicit"]
        if runner is None:
            return None
        out = ctx.waterfall("exec/dispatch",
                            {"mode": mode, "kw": kw, "runner": runner})
        if isinstance(out, dict) and out.get("runner") is runner:
            return runner(**kw)
        return out

    ctx.provide("execution", {
        "bind": bind,
        "dispatch": dispatch,
    })
