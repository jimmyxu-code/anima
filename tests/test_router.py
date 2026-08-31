# -*- coding: utf-8 -*-
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
"""路由混淆矩阵（P4-3）：离线喂话术给 chat_brain 路由，核对分类。
类别：chat 闲聊 / handoff-implicit 隐式任务 / handoff-explicit 显式任务 /
reminder 提醒 / remember 记忆。不占屏、不走语音。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

import chat_brain

CASES = [
    # 闲聊/问答 → chat
    ("给我讲个笑话", "chat"),
    ("你觉得人工智能会取代人类吗", "chat"),
    ("1加1等于几", "chat"),
    ("晚安", "chat"),
    ("你是谁", "chat"),
    # 实时信息（时间天气新闻汇率）→ chat（语音侧联网搜索直接答，不派活；2026-08-21 三车道路由）
    ("今天天气怎么样", "chat"),
    ("查一下现在几点了", "chat"),
    ("今天有什么新闻", "chat"),
    ("现在美元汇率多少", "chat"),
    # 隐式任务（后台本地数据/文件/命令） → handoff implicit
    ("帮我在桌面新建一个文件夹叫测试", ("handoff", "implicit")),
    ("把 D 盘里叫 abc 的文件找出来", ("handoff", "implicit")),
    ("帮我写一首关于春天的诗存到桌面", ("handoff", "implicit")),
    ("看看我电脑还剩多少磁盘空间", ("handoff", "implicit")),
    # 显式任务（要开界面点按） → handoff explicit
    ("打开微信给张三发个消息", ("handoff", "explicit")),
    ("帮我打开浏览器搜一下附近的火锅店", ("handoff", "explicit")),
    ("打开画图软件画个圈", ("handoff", "explicit")),
    ("把记事本打开帮我写点东西", ("handoff", "explicit")),
    # 提醒 → reminder
    ("五分钟后提醒我喝水", "reminder"),
    ("明天早上八点提醒我开会", "reminder"),
    ("晚上九点半提醒我吃药", "reminder"),
    # 记忆 → remember
    ("记住我不吃辣", "remember"),
    ("我女朋友生日是五月二十号，记一下", "remember"),
    ("记住我喜欢喝美式咖啡", "remember"),
]


def norm(kind, payload):
    if kind == "handoff":
        mode = payload.get("mode", "implicit") if isinstance(payload, dict) else "implicit"
        return ("handoff", mode)
    return kind


def main():
    results = []
    for text, expect in CASES:
        try:
            kind, payload = chat_brain.respond(text, on_delta=None,
                                               log=lambda *a: None, context=[])
        except Exception as e:
            kind, payload = "error", str(e)
        got = norm(kind, payload)
        ok = got == expect
        results.append((text, expect, got, ok))
        print(f"[{'OK ' if ok else 'DIFF'}] {text[:22]!r}: 期望 {expect} 实际 {got}")

    n_ok = sum(1 for r in results if r[3])
    print(f"\n{n_ok}/{len(results)} 一致")
    # 混淆对统计
    conf = {}
    for text, expect, got, ok in results:
        if not ok:
            conf.setdefault((str(expect), str(got)), []).append(text)
    for (e, g), texts in conf.items():
        print(f"混淆 {e} → {g}: {len(texts)} 条 {texts}")


if __name__ == "__main__":
    main()
