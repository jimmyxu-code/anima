# -*- coding: utf-8 -*-
"""memory 插件：四层记忆的持有口（P1 收编）。

四层：核心记忆（persona_card 的事实段，有界）/ 会话账本（chat_brain 16 轮 +
realtime 服务端）/ 会话摘要（LLM 蒸馏，摘要节）/ 归档检索（recall 按需注入）。
写入只有 remember（门控去重）与 write_session_summary（蒸馏）两个口；
会话关闭时 consolidate 睡眠整理（去重合并、摘要限长）。
拔掉本插件 = 记忆服务整体下线（recall 返回空、写入拒绝），不影响聊天。
"""

import soul


def register(ctx):
    ctx.provide("memory", {
        "remember": soul.remember,
        "recall": soul.recall,
        "iter_facts": soul.iter_facts,
        "write_session_summary": soul.write_session_summary,
        "consolidate": soul.consolidate,
    })
