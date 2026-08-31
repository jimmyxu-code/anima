# -*- coding: utf-8 -*-
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
"""test_duplex_asr：双工 ASR 事件归一化离线测试（2026-08-22 重大修复）。

实锤过的坑：适配层此前只映射事件 ID、不归一载荷——双工 delta/completed
没有 results 数组，companion_rt 的 results 循环空转，用户语音终稿从未
进入路由（_on_user_text）。本测试锁死归一化契约：
- delta（累计快照）→ EV_ASR_RESPONSE {results:[{text, is_interim:True}]}
- delta 增量形态（非前缀）→ 累加防御
- completed → 先 EV_ASR_RESPONSE 终稿（is_interim:False）再 EV_ASR_ENDED
- completed 空文本 → 不进路由，只发 ENDED
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import doubao_duplex


def _client():
    got = []
    c = doubao_duplex.DoubaoDuplex("", "k",
                                   on_event=lambda ev, obj: got.append((ev, obj)),
                                   log=lambda m: None)
    return c, got


def test_delta_cumulative_passthrough():
    c, got = _client()
    c._dispatch({"type": "conversation.item.input_audio_transcription.delta",
                 "item_id": "i1", "delta": "查"})
    c._dispatch({"type": "conversation.item.input_audio_transcription.delta",
                 "item_id": "i1", "delta": "查一下天气"})
    rs = [o for ev, o in got if ev == doubao_duplex.EV_ASR_RESPONSE]
    assert len(rs) == 2
    assert rs[0]["results"][0]["text"] == "查"
    assert rs[1]["results"][0]["text"] == "查一下天气"   # 累计快照不重复累加
    assert rs[1]["results"][0]["is_interim"] is True
    print("ok delta 累计快照直通")


def test_delta_incremental_defended():
    c, got = _client()
    c._dispatch({"type": "conversation.item.input_audio_transcription.delta",
                 "item_id": "i2", "delta": "今天"})
    c._dispatch({"type": "conversation.item.input_audio_transcription.delta",
                 "item_id": "i2", "delta": "很热"})   # 增量形态（非前缀）
    rs = [o for ev, o in got if ev == doubao_duplex.EV_ASR_RESPONSE]
    assert rs[-1]["results"][0]["text"] == "今天很热"
    print("ok delta 增量形态累加防御")


def test_completed_order_and_final():
    c, got = _client()
    c._dispatch({"type": "conversation.item.input_audio_transcription.completed",
                 "item_id": "i3", "text": "帮我打开记事本"})
    kinds = [ev for ev, _ in got]
    assert kinds == [doubao_duplex.EV_ASR_RESPONSE, doubao_duplex.EV_ASR_ENDED], kinds
    final = got[0][1]["results"][0]
    assert final["text"] == "帮我打开记事本" and final["is_interim"] is False
    print("ok completed 先终稿后句尾")


def test_completed_empty_no_route():
    c, got = _client()
    c._dispatch({"type": "conversation.item.input_audio_transcription.completed",
                 "item_id": "i4", "text": "   "})
    kinds = [ev for ev, _ in got]
    assert kinds == [doubao_duplex.EV_ASR_ENDED], kinds
    print("ok completed 空文本不进路由")


def test_started_maps_info():
    c, got = _client()
    c._dispatch({"type": "conversation.item.input_audio_transcription.started",
                 "item_id": "i5"})
    assert got == [(doubao_duplex.EV_ASR_INFO, {"item_id": "i5",
                   "type": "conversation.item.input_audio_transcription.started"})] \
        or got[0][0] == doubao_duplex.EV_ASR_INFO
    print("ok started → EV_ASR_INFO")


if __name__ == "__main__":
    test_delta_cumulative_passthrough()
    test_delta_incremental_defended()
    test_completed_order_and_final()
    test_completed_empty_no_route()
    test_started_maps_info()
    print("test_duplex_asr: ALL PASS")
