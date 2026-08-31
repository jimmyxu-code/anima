# -*- coding: utf-8 -*-
"""前瞻记忆（prospective memory）：未办完事项的显式层。

对应人脑前瞻记忆——"记得还要去做什么"。2026-08-28 实证：用户四次问
"之前/未完成的任务"小凯都答不全，因为最权威的任务账本不在 recall 检索
范围，而摘要里的未办完事项淹在流水账字缝里。

架构与智能的分工（用户 2026-08-28 裁决）：
- 硬编码（架构）：pending.json 的结构、open/done/dismissed 状态机、
  读写口、注入点、文本相似键匹配（机械操作，非认知）。
- 智能体工作（认知）：历史账本 110 个 paused 残留里哪些是真遗留、归并
  措辞、卡壳原因概括——全交 LLM；会话摘要的未办完事项提取也由摘要
  蒸馏 prompt 顺带结构化输出（见 soul.write_session_summary）。

数据三路汇入：
1. harvest_ledger()：启动时一次，LLM 归并任务账本未终态残留为真实待办。
2. note_pending()：会话摘要收割（source=session）。
3. complete_by_text()/dismiss_by_text()：任务完成自动销账 / 用户口头销账。
"""

import json
import os
import re
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
PENDING_PATH = os.path.join(_HERE, "memory", "pending.json")
LEDGER_PATH = os.path.join(_HERE, "task_sessions.json")

_WRITE_LOCK = threading.Lock()
_LAST_LIST_SHOWN = {"ts": 0.0}   # 待办意图命中（清单展示给用户）的时刻——
# 批量销账语境守卫："这些都不用做了"只有在刚看过清单时才允许全销


def _mark_list_shown():
    _LAST_LIST_SHOWN["ts"] = time.time()
# 待办意图关键词（机械路由，不是认知判断：命中即走结构化清单，不撞 bigram）
_TODO_INTENT_RE = re.compile(
    r"没做完|未完成|没完成|没办完|还有(什么|哪).*(事|任务)|遗留|待办|"
    r"积压|没收尾|做到哪|卡(在|着)|收尾")
_DISMISS_RE = re.compile(r"划掉|销掉|去掉|不用做了|不用办了|从待办里")
# 回顾时间词（2026-08-29 实锤："昨天的微软语音/自进化任务"被待办通道截胡，
# 答成很久前的测试清单=记忆错乱体感）。带回顾时间词且无前瞻词的查询是
# "回顾"不是"前瞻"——透传 recall 走会话摘要，不抢清单通道。
_RETROSPECT_RE = re.compile(
    r"昨天|今天|早上|上午|中午|下午|晚上|刚才|前天|上周|上周|之前(说|聊|谈)")


def _load():
    try:
        with open(PENDING_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"items": []}


def _save(data):
    os.makedirs(os.path.dirname(PENDING_PATH), exist_ok=True)
    tmp = PENDING_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PENDING_PATH)


def _tokens(s):
    cjk = re.sub(r"[^一-鿿a-z0-9]", "", (s or "").lower())
    if not cjk:
        return set()
    bigrams = {cjk[i:i + 2] for i in range(len(cjk) - 1)}
    return bigrams or {cjk}   # 单字符没有 bigram，整串兜底


def _similar(a, b):
    """bigram Jaccard（与 companion_rt._task_sim 同族的机械键匹配）。"""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _open_items(data):
    return [it for it in data["items"] if it.get("status") == "open"]


def _overlap(a, b):
    """overlap 系数：|交|/min(|A|,|B|)。口语片段匹配的正确度量——
    "网卡测速那个划掉" 是待办全文的片段（Jaccard 会被并集稀释）。"""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def note_pending(text, reason="", source="session", log=print):
    """写入一条待办。机械层只做高相似快拦（Jaccard≥0.5）——中文改写
    （"改成X"vs"改成了X"）字面 Jaccard 天生低，语义判重的责任在收割
    prompt（喂已有清单让 LLM 去重），这里只兜底 LLM 失手。"""
    text = (text or "").strip().rstrip("。")
    if len(text) < 4:
        return False
    with _WRITE_LOCK:
        data = _load()
        for it in _open_items(data):
            if _similar(it["text"], text) >= 0.5:
                log(f"prospective: 待办已存在，跳过: {text[:24]!r}")
                return False
        data["items"].append({
            "id": "p-" + os.urandom(3).hex(),
            "text": text, "reason": str(reason or "")[:120],
            "source": source, "since": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "open", "hits": 0,
        })
        _save(data)
    log(f"prospective: 待办登记: {text[:30]!r}")
    return True


def complete_by_text(task_text, log=print):
    """任务完成自动销账：与 completed 任务文本相似的 open 项标 done。
    机械键匹配（Jaccard≥0.45 或 overlap≥0.6）；谁也不冤枉——匹配不上
    就留着，宁可多挂一件也不谎报完成。"""
    if not task_text:
        return 0
    n = 0
    with _WRITE_LOCK:
        data = _load()
        for it in _open_items(data):
            if (_similar(it["text"], task_text) >= 0.45
                    or _overlap(it["text"], task_text) >= 0.6):
                it["status"] = "done"
                it["done_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                n += 1
        if n:
            _save(data)
    if n:
        log(f"prospective: 完成销账 {n} 项 ← {task_text[:26]!r}")
    return n


def dismiss_by_text(text, log=print):
    """用户口头销账："把X划掉/不用做了"。口语是碎片（"网卡测速那个"），
    用 overlap 找**最接近的**一件销掉（≥0.4）——只销一件：用户指哪件销
    哪件，连锁批量销是事故面。"""
    text = (text or "").strip()
    with _WRITE_LOCK:
        data = _load()
        best, best_score = None, 0.0
        for it in _open_items(data):
            score = _overlap(it["text"], text)
            if score > best_score:
                best, best_score = it, score
        if best is not None and best_score >= 0.4:
            best["status"] = "dismissed"
            best["done_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save(data)
            log(f"prospective: 口头销账 1 项 ← {best['text'][:26]!r}")
            return 1
    log(f"prospective: 口头销账没有匹配项 ← {text[:26]!r}")
    return 0


def overview(limit=8):
    """open 待办（旧→新排序；旧事先办）。"""
    items = sorted(_open_items(_load()), key=lambda x: x.get("since", ""))[:limit]
    return items


def context_line():
    """会话开启注入用的一行实况（无遗留返回空——顺利时闭嘴纪律）。"""
    items = overview()
    if not items:
        return ""
    head = "；".join(f"{it['text'][:22]}"
                     + (f"（{it['reason'][:16]}）" if it.get("reason") else "")
                     for it in items[:3])
    more = f" 等共 {len(items)} 件" if len(items) > 3 else ""
    return f"{len(items)} 件遗留：{head}{more}"


# 批量销账指代词（"这些/全部/它们"）——必须 60s 内刚展示过清单才允许全销
_BATCH_DISMISS_RE = re.compile(r"这些|全部|它们|都|通通|一并")
_BATCH_WINDOW = 60.0


def dismiss_all(log=print):
    """全部销账（批量口语："这些都不用做了"）。只在 60s 内刚展示过清单时
    由 augment_recall 的 dismiss 分支调用——语境守卫防误伤。"""
    with _WRITE_LOCK:
        data = _load()
        n = 0
        for it in _open_items(data):
            it["status"] = "dismissed"
            it["done_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            n += 1
        if n:
            _save(data)
    log(f"prospective: 批量销账 {n} 项（60s 内展示过清单）")
    return n


def augment_recall(query, base_hits, log=print):
    """recall 组合口：待办意图的问题直接走结构化清单（不撞统计检索），
    其余问题原样返回检索结果。双工 FC 与半双工路由两路共用。"""
    q = query or ""
    if _DISMISS_RE.search(q):
        # 批量指代（"这些都不用做了"）+ 60s 内刚展示过清单 → 全销；
        # 否则按文本找对应那一件销
        if (_BATCH_DISMISS_RE.search(q)
                and time.time() - _LAST_LIST_SHOWN["ts"] < _BATCH_WINDOW):
            n = dismiss_all(log=log)
            if n:
                return [f"已划掉全部 {n} 项遗留待办。一句话确认即可。"]
        n = dismiss_by_text(re.sub(_DISMISS_RE, "", q), log=log)
        if n:
            return [f"已划掉 {n} 项遗留待办。一句话确认即可。"]
    if _TODO_INTENT_RE.search(q) and not _RETROSPECT_RE.search(q):
        items = overview()
        _mark_list_shown()
        with _WRITE_LOCK:   # 检索强化计数（第二步衰减机制的原料）
            data = _load()
            for it in data["items"]:
                if it.get("status") == "open":
                    it["hits"] = it.get("hits", 0) + 1
            _save(data)
        if items:
            lines = [f"- {it['text']}"
                     + (f"（卡在：{it['reason'][:30]}）" if it.get("reason") else "")
                     + f"［{it['since']}起］" for it in items]
            return ([f"任务账本里的真实遗留清单（唯一事实来源，共 {len(items)} 件）："]
                    + lines
                    + ["照实回答，一两句带过最旧的几件；账本没说完成的不许说完成。"])
        return ["当前没有登记在案的未办完任务。如实说没有积压。"]
    return base_hits


def harvest_ledger(log=print, ledger_path=None):
    """历史任务账本收割：未终态残留 → LLM 归并成真实待办清单。
    架构在此处硬编码（查账本/写 pending），认知判断（哪些是真遗留、
    措辞归并、卡壳原因）全交 LLM。LLM 不可用则本次放弃（fail-safe，
    下次启动重试），宁缺勿错。"""
    import soul
    path = ledger_path or LEDGER_PATH
    try:
        items = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        log("prospective: 任务账本不可读，跳过收割")
        return 0
    unfinished = []
    for t in items if isinstance(items, list) else []:
        if t.get("state") not in ("completed", "failed", "cancelled"):
            txt = (t.get("latest_goal") or t.get("task_text") or "").strip()
            if len(txt) >= 8:
                unfinished.append(txt)
    if not unfinished:
        return 0
    raw = "\n".join(f"- {t[:150]}" for t in unfinished[:120])
    existing = "\n".join(f"- {it['text']}" for it in overview(12)) or "（空）"
    merged = soul.deepseek_call(
        "下面是一个语音助手的任务账本里所有未正常收尾的任务记录，其中大量是"
        "重复派活/系统残留。请归并出**真实的、用户还会想要完成的遗留事项**："
        "去掉重复与琐碎残留，每件一行，格式严格的 JSON 数组，元素形如 "
        '{"text": "事项", "reason": "卡在哪/为什么没成"}。text 不超过 40 字。'
        "已有待办清单如下——语义重复的（哪怕措辞不同）绝对不要再输出：\n"
        + existing + "\n\n没有真实遗留就输出 []。只输出 JSON。\n\n" + raw,
        log=log, max_tokens=800, timeout=60)
    if not merged:
        log("prospective: LLM 不可用，账本收割本次放弃（下次启动重试）")
        return 0
    try:
        m = re.search(r"\[.*\]", merged, re.S)
        parsed = json.loads(m.group(0)) if m else []
    except ValueError:
        log(f"prospective: 归并输出不可解析，放弃: {merged[:60]!r}")
        return 0
    n = 0
    for it in parsed[:12]:
        if isinstance(it, dict) and note_pending(str(it.get("text", "")),
                                                 str(it.get("reason", "")),
                                                 source="ledger", log=log):
            n += 1
    log(f"prospective: 账本收割完成（账本残留 {len(unfinished)} 条 → 待办 {n} 件）")
    return n
