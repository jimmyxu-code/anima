# -*- coding: utf-8 -*-
"""prospective 前瞻记忆回归：待办登记/去重/销账/检索组合/账本收割。
无 pytest，从项目根运行：python tests/test_prospective.py"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import prospective as pm

FAILS = []


def check(ok, desc):
    if not ok:
        FAILS.append(desc)
    print(f"[{'OK ' if ok else 'FAIL'}] {desc}")


# 隔离：待办文件与账本文件都换临时路径
_tmpd = tempfile.mkdtemp()
pm.PENDING_PATH = os.path.join(_tmpd, "pending.json")
_ledger = os.path.join(_tmpd, "task_sessions.json")

# 1) 登记 + 机械快拦（精确/高相似）；低相似改写放行=设计使然
#    （语义判重责任在收割 prompt，机械阈值只兜底——见 harvest 断言）
r1 = pm.note_pending("把唤醒词改成小小，改完验证生效", reason="确认链超时", source="ledger")
r2 = pm.note_pending("把唤醒词改成小小，改完验证生效", source="ledger")
r2b = pm.note_pending("用户的唤醒词改成了小小", source="ledger")
check(r1 is True and r2 is False, "登记成功且精确/高相似被快拦")
check(r2b is True, "低相似改写机械层放行（语义判重在收割 prompt）")
r3 = pm.note_pending("无线网卡驱动安装后测速对比", source="session")
check(r3 is True, "不同主题正常登记")

# 2) overview：旧→新排序 + context_line
items = pm.overview()
check(len(items) == 3 and "唤醒词" in items[0]["text"],
      "overview open 项按旧→新排序")
line = pm.context_line()
check("遗留" in line and "唤醒词" in line, f"context_line 汇总一行: {line[:40]}")

# 3) complete_by_text：任务完成自动销账（相似/overlap 命中）
n = pm.complete_by_text("帮我把唤醒词改成小小并验证生效")
check(n == 1, "完成销账：相似 open 项标 done")
check(len(pm.overview()) == 2, "销账后 open 剩 2 件")
n2 = pm.complete_by_text("完全不沾边的一个任务文本内容")
check(n2 == 0, "不相关任务不误销账")

# 4) 检索组合：待办意图走结构化清单
hits = pm.augment_recall("我还有什么没做完的任务", [])
check(any("遗留清单" in h or "网卡" in h for h in hits) and len(hits) >= 2,
      f"待办意图走结构化清单（{len(hits)} 行）")
base = ["事实：用户喜欢深色主题"]
hits2 = pm.augment_recall("用户喜欢什么主题", base)
check(hits2 == base, "非待办意图透传检索结果")
hits3 = pm.augment_recall("当前还有没有积压的任务", [])
check(any("遗留清单" in h for h in hits3), "待办意图（积压）也走结构化清单")

# 5) 口头销账
hits4 = pm.augment_recall("网卡测速那个不用做了，划掉", [])
check(any("已划掉" in h for h in hits4), "口头销账命中 dismiss 分支")
check(all("网卡" not in it["text"] for it in pm.overview()),
      "销账后清单里不再有该项")

# 6) 账本收割：mock LLM 归并输出；prompt 必须带已有清单（语义判重在收割层）
ledger = [
    {"state": "paused", "latest_goal": "修复Windows更新卡住的问题，清空缓存目录"},
    {"state": "paused", "latest_goal": "以管理员权限修复Windows更新（停服务清缓存）"},
    {"state": "completed", "latest_goal": "已经完成的事不该进待办清单"},
    {"state": "cancelled", "latest_goal": "取消的活也别进待办"},
]
json.dump(ledger, open(_ledger, "w", encoding="utf-8"))
import soul
captured = {}
def _fake_llm(prompt, **kw):
    captured["prompt"] = prompt
    return json.dumps(
        [{"text": "修复 Windows 更新卡住", "reason": "需要管理员权限与用户确认"}],
        ensure_ascii=False)
orig = soul.deepseek_call
soul.deepseek_call = _fake_llm
try:
    n = pm.harvest_ledger(log=lambda m: None, ledger_path=_ledger)
finally:
    soul.deepseek_call = orig
check(n == 1, f"账本收割：LLM 归并写入 {n} 件")
check("已有待办清单" in captured.get("prompt", "")
      and "唤醒词" in captured.get("prompt", ""),
      "收割 prompt 喂了已有清单（LLM 负责语义判重）")
check(any("Windows 更新" in it["text"] for it in pm.overview()),
      "收割产物进 open 清单")
check(not any("已经完成" in it["text"] or "取消的活" in it["text"]
              for it in pm.overview()), "completed/cancelled 不进待办")

# 7) LLM 不可用：fail-safe 放弃（不写错数据）
soul.deepseek_call = lambda *a, **k: None
try:
    n = pm.harvest_ledger(log=lambda m: None, ledger_path=_ledger)
finally:
    soul.deepseek_call = orig
check(n == 0, "LLM 不可用时收割放弃（宁缺勿错）")

# 8) hits 计数（第二步衰减机制的原料）
pm.augment_recall("还有什么没做完", [])
data = json.load(open(pm.PENDING_PATH, encoding="utf-8"))
check(any(it.get("hits", 0) >= 1 for it in data["items"]
          if it["status"] == "open"), "待办被检索命中时 hits 递增")

# 9) 批量销账（2026-08-29 实锤："这些都不用做了"路由判 chat 没进销账通道；
#    通道补上后批量指代 + 60s 内展示过清单才允许全销——语境守卫防误伤）
pm.note_pending("甲任务测试用", source="session")
pm.note_pending("乙任务测试用", source="session")
pm.augment_recall("还有什么没做完", [])          # 展示清单 → 记录展示时刻
hits5 = pm.augment_recall("这些都不用做了", [])
check(any("全部" in h for h in hits5) and pm.overview() == [],
      "批量口语 + 语境成立 → 全部销账")
pm.note_pending("丙任务测试用", source="session")
pm._LAST_LIST_SHOWN["ts"] = 0.0                   # 语境失效（从未展示过）
hits6 = pm.augment_recall("这些都不用做了", [])
check(hits6 == [] and len(pm.overview()) == 1,
      "无语境时批量口语不误销（保守回落单条匹配）")
pm.complete_by_text("丙任务测试用")                # 清场

print()
if FAILS:
    print(f"PROSPECTIVE_TEST FAIL（{len(FAILS)} 项）")
    sys.exit(1)
print("PROSPECTIVE_TEST PASS")
