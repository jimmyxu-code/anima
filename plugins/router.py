# -*- coding: utf-8 -*-
"""router 插件：路由脑收编（P1e）。

chat_brain 的判定结果经 `route/decide` 瀑布发出——任何后挂插件都可
拦截/改写路由决定（级联升级、语义去重、遥测都挂这里，不碰路由本体）。
拔掉本插件 = 全部按闲聊处理（fail-safe：语音侧自答，不派活）。
"""

import chat_brain


def register(ctx):
    def decide(text, **kw):
        kind, payload = chat_brain.respond(text, **kw)
        return ctx.waterfall("route/decide", (kind, payload))

    ctx.provide("router", {
        "decide": decide,
        "note_turn": chat_brain.note_turn,
        "note_task": chat_brain.note_task,
        "abort_current": chat_brain.abort_current,
    })
