# -*- coding: utf-8 -*-
"""emb_local + soul.recall 语义补漏验收（2026-08-31 阶段：本地 embedding 档）。
模型/依赖缺件时全部 skip 式通过（fail-soft 本身就是合同的一部分）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import emb_local
import soul

ok = fail = 0


def check(cond, name, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"[OK ] {name}")
    else:
        fail += 1
        print(f"[FAIL] {name} {detail}")


# 1) fail-soft：开关关掉=整体静默不可用
import json
_cfg_path = os.path.join(emb_local._HERE, "config.json")
_cfg = json.load(open(_cfg_path, encoding="utf-8"))
had_key = "memory_semantic" in _cfg
old_val = _cfg.get("memory_semantic")
_cfg["memory_semantic"] = False
json.dump(_cfg, open(_cfg_path, "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
try:
    check(not emb_local.available(), "config memory_semantic=false 即停用")
finally:
    if had_key:
        _cfg["memory_semantic"] = old_val
    else:
        _cfg.pop("memory_semantic", None)
    json.dump(_cfg, open(_cfg_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
check(emb_local.available(), "config 复原后恢复可用（模型在位）")

if emb_local.available():
    # 2) 语义命中质量：同义改写命中 / 无关串不中
    hits = emb_local.semantic_hits("界面配色偏好",
                                   ["用户喜欢深色主题", "昨天点了外卖"])
    check(hits and hits[0][0] == 0, "同义改写命中目标事实", str(hits))
    check(not emb_local.semantic_hits("绝不相干随机串",
                                      ["用户喜欢深色主题", "昨天点了外卖"]),
          "无关串零命中（阈值纪律）")
    # 3) 磁盘缓存：同文本二次 embed 不再推理（命中即返回同向量）
    v1 = emb_local.embed(["缓存探针文本"])
    v2 = emb_local.embed(["缓存探针文本"])
    check(v1 == v2 and v1 and len(v1[0]) == 512, "embed 维度+缓存一致")

    # 4) recall 语义补漏：关键词零命中的问法补出目标事实（隔离记忆文件）
    import tempfile
    tmpd = tempfile.mkdtemp()
    orig_mem, orig_cache = soul.MEMORY_PATH, emb_local._CACHE_PATH
    soul.MEMORY_PATH = os.path.join(tmpd, "MEMORY.md")
    emb_local._CACHE_PATH = os.path.join(tmpd, "emb_cache.json")
    emb_local._state["cache"] = {}
    try:
        open(soul.MEMORY_PATH, "w", encoding="utf-8").write(
            "# 长期事实\n- 用户喜欢深色主题〈2026-08-20·preference·#2〉\n"
            "# 会话摘要\n# 任务账本\n# 归档\n")
        h0 = soul._read_facts()[0]["hits"]
        r = soul.recall("界面配色的偏好是什么", log=lambda m: None)
        check(any("深色主题" in x for x in r),
              f"recall 零词面命中→语义补漏：{r}")
        h1 = soul._read_facts()[0]["hits"]
        check(h1 == h0 + 1, "语义命中同权 hits+1 记账", f"{h0}→{h1}")
        # 词面有命中时语义不出手（排序不受扰动）
        r2 = soul.recall("用户喜欢什么主题", log=lambda m: None)
        check(r2 and "深色主题" in r2[0] and len(r2) == 1,
              f"词面命中时语义不插嘴：{r2}")
    finally:
        soul.MEMORY_PATH, emb_local._CACHE_PATH = orig_mem, orig_cache
        emb_local._state["cache"] = None
        emb_local._state["tried"], emb_local._state["ok"] = False, False
else:
    print("[SKIP] 模型/依赖缺件，语义用例全跳（fail-soft 合同内）")

print(f"\nemb_local 验收 {ok} 过 {fail} 失败")
sys.exit(1 if fail else 0)
