# -*- coding: utf-8 -*-
"""identity 插件：persona.yaml 的唯一消费口（P1 收编）。

三层渲染：嘴全量（persona_card）/ 脑薄量（router_prompt）/ 手零人格（dsh_prompt
只给纪律）；persona_hash 供审计——任何一层用旧人格，日志一眼可辨。
拔掉本插件 = 全系统没有人格可用（fail-closed，而不是各自偷偷用副本）。
"""

import soul


def register(ctx):
    ctx.provide("identity", {
        "persona_card": soul.persona_card,
        "router_prompt": soul.router_prompt,
        "dsh_prompt": soul.dsh_prompt,
        "speaking_style": soul.speaking_style,
        "persona_hash": soul.persona_hash,
    })
