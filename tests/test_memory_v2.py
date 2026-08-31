# -*- coding: utf-8 -*-
"""记忆 v2 回归（②巩固管道/③检索升级/④嘴层排序）。
覆盖：元数据解析（新旧格式兼容）、remember kind、persona_card 排序、
recall 命中记账与账本检索、consolidate 提升/取代/衰减/摘要合并。
无 pytest，从项目根运行：python tests/test_memory_v2.py"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import soul

FAILS = []


def check(ok, desc):
    if not ok:
        FAILS.append(desc)
    print(f"[{'OK ' if ok else 'FAIL'}] {desc}")


# 隔离：MEMORY.md 与账本都换临时路径
_tmpd = tempfile.mkdtemp()
soul.MEMORY_PATH = os.path.join(_tmpd, "MEMORY.md")
soul._LEDGER_PATH = os.path.join(_tmpd, "task_sessions.json")
soul._known_hash["v"] = None

HEADER = ("# 长期记忆\n\n这里记录用户的偏好、习惯和明确要求记住的事。"
          "对话中优先参考。\n\n")

def write_memory(body):
    with open(soul.MEMORY_PATH, "w", encoding="utf-8") as f:
        f.write(HEADER + body)

write_memory("# 长期事实\n\n# 会话摘要\n\n")   # 初始空文件（真实环境由迁移保证）

def read_memory():
    with open(soul.MEMORY_PATH, encoding="utf-8") as f:
        return f.read()


# 1) 元数据解析：新旧格式兼容
d1 = soul._parse_fact_line("用户喜欢深色主题〈2026-08-29·preference·#3〉")
check(d1 == {"text": "用户喜欢深色主题", "date": "2026-08-29",
             "kind": "preference", "hits": 3}, "新格式解析全字段")
d2 = soul._parse_fact_line("旧式事实（2026-08-01）")
check(d2["kind"] == "fact" and d2["date"] == "2026-08-01" and d2["hits"] == 0,
      "旧式（date）行兼容升级")
check(soul._fmt_fact(d2).startswith("旧式事实〈2026-08-01·fact·#0〉"),
      "fmt 规范化输出新格式")

# 2) remember kind 写入 + 去重
check(soul.remember("用户每周三要体测", log=lambda m: None, kind="agreement"),
      "remember 带 kind 写入")
check(not soul.remember("用户每周三要体测", log=lambda m: None),
      "同文本重复被拒")
facts = soul._read_facts()
check(facts and facts[-1]["kind"] == "agreement", "读回 kind=agreement")

# 3) persona_card 嘴层排序（④）：agreement 权重压过更新的一次性 fact
#    （数据去并列：旧 fact 三条都无新近，并列挤出的是稳定序最后一条）
write_memory(
    HEADER + "# 长期事实\n\n"
    + "- 桌面目录固定在 D:\\Desktop〈2026-08-01·fact·#0〉\n"
    + "- 昨天查过天气〈2026-08-28·fact·#0〉\n"
    + "- 用户每周三要体测〈2026-08-01·agreement·#0〉\n"
    + "- 用户喜欢深色主题〈2026-08-02·preference·#0〉\n"
    + "- 刚搜过一次汇率〈2026-08-28·fact·#0〉\n"
    + "- 打开过记事本〈2026-08-01·fact·#0〉\n"
    + "- 又打开过一次记事本〈2026-08-01·fact·#0〉\n")
card = soul.persona_card(log=lambda m: None)
check("每周三要体测" in card and "用户喜欢深色主题" in card,
      "嘴层选中 agreement/preference（权重>新近 fact）")
check("又打开过一次记事本" not in card and "查过天气" in card,
      "一次性旧 fact 被挤出嘴层（并列稳定序挤最后）")

# 4) recall 命中记账 + 账本检索（③）
soul.recall("用户喜欢什么主题", log=lambda m: None)
hits = [d["hits"] for d in soul._read_facts()
        if d["text"] == "用户喜欢深色主题"]
check(hits == [1], f"recall 命中事实 hits+1（实际 {hits}）")
json.dump([{"state": "completed", "created_at": 1755300000.0,
            "latest_goal": "修复Windows更新卡住的问题并清空缓存目录"},
           {"state": "paused", "created_at": 1755300000.0,
            "latest_goal": "在办的不该出现在账本检索里"}],
          open(soul._LEDGER_PATH, "w", encoding="utf-8"))
r = soul.recall("Windows 更新缓存那次处理好了吗", log=lambda m: None)
check(any("账本：" in x and "Windows" in x for x in r),
      f"账本终态记录进检索：{r[:1]}")
check(all("在办的不该" not in x for x in r), "未终态任务不进账本检索")

# 5) consolidate v2（②）：提升/取代/衰减/摘要合并（LLM 按 prompt 分发 mock）
def _fake_llm(prompt, **kw):
    if "巩固器" in prompt:   # 情景提升
        return json.dumps({
            "new_facts": [
                {"text": "用户搜索时要求关键词精准不要多", "kind": "correction"},
                {"text": "用户每周三要体测", "kind": "agreement"}],  # 重复→应被拒
            "outdated": ["旧式事实"]}, ensure_ascii=False)
    if "合并成不超过" in prompt:   # 事实归并
        return json.dumps([
            {"text": "桌面目录固定在 D:\\Desktop", "kind": "fact"},
            {"text": "用户喜欢深色主题", "kind": "preference"},
            {"text": "用户每周三要体测", "kind": "agreement"},
            {"text": "用户搜索时要求关键词精准不要多", "kind": "correction"}],
            ensure_ascii=False)
    if "月度概要" in prompt:   # 摘要压缩
        return "8月下旬主要查天气新闻和开关应用；网卡驱动安装测速未完成"
    return None
orig_llm = soul.deepseek_call
soul.deepseek_call = _fake_llm
try:
    # 造：旧格式事实若干（触发超限归并）+ 90天前未命中条（触发衰减）+ 摘要 14 条（触发压缩）
    lines = ["# 长期事实\n\n",
             "- 旧式事实〈2026-08-01·fact·#0〉\n",
             "- 桌面目录固定在 D:\\Desktop〈2026-08-01·fact·#2〉\n",
             "- 用户喜欢深色主题〈2026-08-02·preference·#1〉\n",
             "- 用户每周三要体测〈2026-08-01·agreement·#0〉\n"]
    for i in range(12):   # 凑超 15 条触发归并
        lines.append(f"- 填充事实第{i}条〈2025-05-01·fact·#0〉\n")   # 也>90天未命中→衰减
    body = "".join(lines) + "\n# 会话摘要\n\n"
    for i in range(14):
        body += f"- 08-{i+10:02d} 10:00 会话：摘要第{i}条（2026-08-{i+10:02d}）\n"
    write_memory(body)
    soul.consolidate(log=lambda m: None, max_facts=15, max_summaries=10)
finally:
    soul.deepseek_call = orig_llm

content = read_memory()
check("搜索时要求关键词精准" in content, "巩固提升：纠正类新事实入库")
check(content.count("用户每周三要体测") == 1, "提升时重复事实被拒（LLM 输出重复）")
check("# 归档" in content and "已过时：旧式事实" in content,
      "过时取代：LLM 指认的旧事实进归档节")
_facts_section = content.split("# 归档")[0]
check("填充事实" not in _facts_section,
      "12 条 90 天未命中填充条全部衰减归档（事实节已清）")
check("填充事实" in content.split("# 归档", 1)[1].split("# 会话摘要")[0],
      "衰减条在归档节可回捞（未删除）")
check("月度概要" in content, "溢出摘要合并为月度概要（不截断丢弃）")
check(content.count("摘要第") <= 10, "摘要节保留近期 10 条内")
check("网卡驱动" in content, "月度概要保留未办完事项")
# 身份豁免衰减：造一条 90 天前的 identity
soul.remember("用户名叫阿明", log=lambda m: None, kind="identity")
import re as _re
content = _re.sub(r"- 用户名叫阿明〈[^〉]*〉",
                  "- 用户名叫阿明〈2026-05-01·identity·#0〉", read_memory())
write_memory(content.split(HEADER, 1)[1])
soul.deepseek_call = lambda *a, **k: None   # LLM 不可用：只做机械衰减
try:
    soul.consolidate(log=lambda m: None)
finally:
    soul.deepseek_call = orig_llm
check("用户名叫阿明" in read_memory().split("# 归档")[0],
      "identity 类豁免 90 天衰减（LLM 不可用时机械部分照跑）")

print()
if FAILS:
    print(f"MEMORY_V2_TEST FAIL（{len(FAILS)} 项）")
    sys.exit(1)
print("MEMORY_V2_TEST PASS")
