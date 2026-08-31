"""灵魂层（P0b-1/P0b-2）：persona.yaml + MEMORY.md 的唯一读写口。

- load_persona()：persona.yaml → dict（人格单一来源）
- persona_card()：渲染 realtime 的 system_role（人格+记忆事实，定长压缩）
- dsh_prompt()：渲染深思手 system prompt（人格+环境事实+工作纪律）
- remember(fact)：记忆写入唯一入口（内容指纹去重 + 写入门控）
- write_session_summary(turns)：会话结束摘要（跨天连续性的本地载体）
- iter_facts()：MEMORY.md 事实迭代（去重后）

写入门控：MEMORY.md 只允许经 remember()/write_session_summary() 写入；
其他进程直接改文件会在下次读取时被发现并告警（内容校验）。
"""

import hashlib
import json
import os
import re
import threading
import time

import secrets_store
import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
PERSONA_PATH = os.path.join(_HERE, "persona.yaml")
# persona.yaml 不入库（含用户私人信息）；缺省时回退到随仓库的
# persona.example.yaml（2026-09-01 开源化），用户复制改名即定制。
_PERSONA_EXAMPLE = os.path.join(_HERE, "persona.example.yaml")
MEMORY_PATH = os.path.join(_HERE, "memory", "MEMORY.md")
_LEDGER_PATH = os.path.join(_HERE, "task_sessions.json")   # 任务账本（recall 检索面）

_write_lock = threading.Lock()
_known_hash = {"v": None}   # 上次写入时的文件指纹（门控校验用）

# 记忆条目元数据格式（2026-08-29 四步路线②③④）：-内容〈YYYY-MM-DD·kind·#hits〉
# kind：identity/agreement/correction/preference/fact；旧式（date）行读取自动升级。
_FACT_LINE_RE = re.compile(
    r"^(?P<text>.*?)〈(?P<date>\d{4}-\d{2}-\d{2})·(?P<kind>[a-z]+)·#(?P<hits>\d+)〉\s*$")
_FACT_OLD_RE = re.compile(r"^(?P<text>.*?)（(?P<date>\d{4}-\d{2}-\d{2})）\s*$")


def _parse_fact_line(line):
    """一行记忆条目 → {text,date,kind,hits}（新旧格式兼容）。"""
    s = (line or "").strip().lstrip("- ").strip()
    m = _FACT_LINE_RE.match(s)
    if m:
        return {"text": m.group("text").strip(), "date": m.group("date"),
                "kind": m.group("kind"), "hits": int(m.group("hits"))}
    m = _FACT_OLD_RE.match(s)
    if m:
        return {"text": m.group("text").strip(), "date": m.group("date"),
                "kind": "fact", "hits": 0}
    return {"text": s, "date": "", "kind": "fact", "hits": 0}


def _fmt_fact(d):
    """{text,date,kind,hits} → 新格式行文本（不含 '- ' 前缀）。"""
    return f"{d['text']}〈{d.get('date') or time.strftime('%Y-%m-%d')}" \
           f"·{d.get('kind', 'fact')}·#{int(d.get('hits', 0))}〉"


def _read_facts():
    """事实节全部条目（dict 列表，文件序）。"""
    return [_parse_fact_line(s) for s in _section_lines_raw(_FACTS_HEAD)]


def _write_facts(facts):
    """事实节整体重写（dict 列表 → 新格式行）。"""
    _rewrite_sections(_FACTS_HEAD, ["- " + _fmt_fact(d) for d in facts])


def _section_lines_raw(head):
    """读某节下全部 '- ' 原始行（不去重，供元数据操作）。"""
    out = []
    try:
        with open(MEMORY_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return out
    in_section = False
    for line in lines:
        s = line.strip()
        if s.startswith("# "):
            in_section = (s == head)
            continue
        if in_section and s.startswith("- "):
            out.append(s)
    return out


def _read_sections_full():
    """全文按节切分：(header, {head: [lines...]})。
    "# 长期记忆"是文件头题记不是节——归进 header 保留。"""
    try:
        with open(MEMORY_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None, {}
    managed = [_FACTS_HEAD, "# 归档", _SUMM_HEAD]
    positions = [(content.find(h), h) for h in managed if content.find(h) >= 0]
    positions.sort()
    header = content[:positions[0][0]] if positions else content
    sections = {}
    for idx, (pos, head) in enumerate(positions):
        end = positions[idx + 1][0] if idx + 1 < len(positions) else len(content)
        body = content[pos + len(head):end]
        sections[head] = [ln for ln in body.split("\n")
                          if ln.strip().startswith("- ")]
    return header, sections


def load_persona():
    path = PERSONA_PATH if os.path.exists(PERSONA_PATH) else _PERSONA_EXAMPLE
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except OSError:
        return None


def iter_facts(log=print):
    """MEMORY.md 事实迭代（按内容指纹去重，保留最后一次出现）。
    分区后只读"长期事实"节；摘要/流水账绝不进人格卡。"""
    _migrate_sections(log=log)
    return [t for t in _section_lines(_FACTS_HEAD) if t]


def _section_lines(head):
    """读某节下的 '- ' 条目（归一化去重，保留最后出现）。"""
    out, seen = [], set()
    try:
        with open(MEMORY_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return out
    in_section = False
    for line in lines:
        s = line.strip()
        if s.startswith("# "):
            in_section = (s == head)
            continue
        if not in_section or not s.startswith("- "):
            continue
        text = re.sub(r"（\d{4}-\d{2}-\d{2}）", "", s[2:].strip())
        text = text.rstrip("。").strip()
        if not text:
            continue
        fp = hashlib.md5(text.encode("utf-8")).hexdigest()
        if fp in seen:
            continue
        seen.add(fp)
        out.append(text)
    return out


_FACTS_HEAD = "# 长期事实"
_SUMM_HEAD = "# 会话摘要"


def _migrate_sections(log=print):
    """一次性把扁平 MEMORY.md 迁成两节（事实/摘要分流）。
    含"会话："的行 → 摘要节；其余 '- ' 行 → 事实节。幂等。"""
    try:
        with open(MEMORY_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return
    if _FACTS_HEAD in content:
        return
    facts, sums = [], []
    for line in content.split("\n"):
        s = line.strip()
        if not s.startswith("- "):
            continue
        if "会话：" in s:
            sums.append(s)
        else:
            facts.append(s)
    header = ("# 长期记忆\n\n这里记录用户的偏好、习惯和明确要求记住的事。"
              "对话中优先参考。\n")
    body = (header + f"\n{_FACTS_HEAD}\n\n" + "\n".join(facts)
            + f"\n\n{_SUMM_HEAD}\n\n" + "\n".join(sums) + "\n")
    with _write_lock:
        with open(MEMORY_PATH, "w", encoding="utf-8") as f:
            f.write(body)
        _known_hash["v"] = _file_hash(MEMORY_PATH)
    log(f"soul: MEMORY.md 已分区（事实 {len(facts)} 条 / 摘要 {len(sums)} 条）")


def persona_card(log=print, max_facts=6):
    """realtime 的 system_role：人格 + 记忆事实（类别×命中×新近选条，2026-08-29
    ④嘴层排序——约定/纠正/身份/偏好永不被一次性流水事实挤出）。
    服务端把 system_role 当 YAML 解析——记忆里的路径反斜杠会触发
    "yaml: found unknown escape character" 把整个会话搞哑（2026-08-20 实测）。
    渲染时统一把反斜杠转正。"""
    p = load_persona()
    facts = _read_facts()
    _KIND_RANK = {"identity": 0, "agreement": 1, "correction": 2, "preference": 3}
    weighted = [d for d in facts if d["kind"] in _KIND_RANK]
    weighted.sort(key=lambda d: _KIND_RANK[d["kind"]])
    plain = [d for d in facts if d["kind"] not in _KIND_RANK]
    plain.sort(key=lambda d: (d["date"],), reverse=True)   # 新近优先，并列稳定序
    chosen = [d["text"] for d in (weighted + plain)][:max_facts]
    base = (f"你是{p['name']}，{p['role']}，{p['capabilities'].strip()}"
            f"用户叫{p['user']['name']}，{p['user']['note']}。")
    if chosen:
        base += "关于用户：" + "；".join(chosen) + "。"
    base += p["rules"].strip()
    return base.replace("\\", "/")


def speaking_style():
    return load_persona()["style"]


def router_prompt():
    """路由脑的薄人格锚（三层渲染：嘴全量/脑薄量/手零人格）。
    只给身份一句——路由纪律在 chat_brain，人格细节在 persona_card。
    任何一层都不得再自带人设字符串（chat_brain 硬编码副本已清）。"""
    p = load_persona()
    return (f"你是{p['name']}，{p['role']}。"
            f"用户叫{p['user']['name']}，{p['user']['note']}。")


def persona_hash():
    """人格指纹前 8 位（审计：任何一层用了旧人格，日志一眼可辨）。"""
    return (_file_hash(PERSONA_PATH) or "?")[:8]


def dsh_prompt():
    """执行层 system prompt（exec-native 用；名字是 dsh 时代遗留，与人格同源）。"""
    p = load_persona()
    return (f"你是\"{p['name']}\"电脑伙伴的执行手，通过系统能力"
            f"操作这台电脑。用户叫{p['user']['name']}。\n\n"
            f"【环境事实（不要试探，直接用）】\n{p['work_env'].strip()}\n\n"
            f"【工作方式】\n{p['work_rules'].strip()}")


def remember(fact, log=print, kind="fact"):
    """记忆写入唯一入口（内容指纹去重；重复事实返回 False）。写入事实节，
    新元数据格式（2026-08-29 四步路线：kind=identity/agreement/correction/
    preference/fact）。"""
    fact = (fact or "").strip().rstrip("。")
    if not fact:
        return False
    _migrate_sections(log=log)
    fp = hashlib.md5(fact.encode("utf-8")).hexdigest()
    with _write_lock:
        if fp in {hashlib.md5(d["text"].encode("utf-8")).hexdigest()
                  for d in _read_facts()}:
            log(f"记忆重复，跳过: {fact[:30]!r}")
            return False
        _append_to_section(
            _FACTS_HEAD,
            "- " + _fmt_fact({"text": fact, "date": time.strftime("%Y-%m-%d"),
                              "kind": kind, "hits": 0}))
        _known_hash["v"] = _file_hash(MEMORY_PATH)
    log(f"记忆已写入: {fact[:40]!r}（{kind}）")
    return True


def _append_to_section(head, line):
    """把一行追加到指定节末尾（节内最后一条 '- ' 之后）。"""
    with open(MEMORY_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()
    out = []
    in_section = False
    inserted = False
    for i, raw in enumerate(lines):
        s = raw.strip()
        if s.startswith("# "):
            if in_section and not inserted:
                out.append(line + "\n")
                inserted = True
            in_section = (s == head)
        out.append(raw)
    if not inserted:
        # 节在文件末尾或不存在
        if in_section:
            out.append(line + "\n")
        else:
            out.append(f"\n{head}\n\n{line}\n")
    with open(MEMORY_PATH, "w", encoding="utf-8") as f:
        f.writelines(out)


def _rewrite_sections(head, new_lines):
    """把某节的 '- ' 条目整体替换为 new_lines（其余节原样保留）。"""
    try:
        with open(MEMORY_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        lines = []
    out, in_section, wrote = [], False, False
    for raw in lines:
        s = raw.strip()
        if s.startswith("# "):
            if in_section and not wrote:
                out.extend(ln + "\n" for ln in new_lines)
                wrote = True
            in_section = (s == head)
            out.append(raw)
            continue
        if in_section and s.startswith("- "):
            continue   # 旧条目丢弃（整体替换）
        out.append(raw)
    if not wrote:
        out.append(f"{head}\n\n")
        out.extend(ln + "\n" for ln in new_lines)
    with _write_lock:
        with open(MEMORY_PATH, "w", encoding="utf-8") as f:
            f.writelines(out)
        _known_hash["v"] = _file_hash(MEMORY_PATH)


def _deepseek(prompt, log=print, max_tokens=220, timeout=20):
    """睡眠整理/摘要蒸馏用的最小 DeepSeek 调用（凭据走凭据管理器）。"""
    import urllib.request
    key = secrets_store.get_secret("deepseek")
    if not key:
        return None
    body = {"model": "deepseek-chat", "max_tokens": max_tokens,
            "temperature": 0.3,
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:
        log(f"soul: DeepSeek 调用失败: {e}")
        return None


def write_session_summary(turns, log=print):
    """会话结束摘要（LLM 结构化蒸馏：brief+pending_items，2026-08-29 前瞻层接点）。
    turns=[(user, reply), ...]；LLM 不可用时回退旧口径一句话流水。
    2026-08-22 修法（"昨天的事记不住了"）：不让 LLM 判"有没有价值"——价值判断
    是丢数据的根源。只要不是纯寒暄（总字数<12）就留一行事实流水。"""
    if not turns:
        return
    _migrate_sections(log=log)
    text = "\n".join(f"用户：{u}\n小凯：{r}" for u, r in turns[-12:])
    if len(re.sub(r"\s", "", text)) < 12:
        log("soul: 纯寒暄会话，摘要不写")
        return
    brief = None
    pending = []
    out = deepseek_call(
        "把这段语音助手的会话蒸馏成 JSON：{\"brief\": \"一句话流水账（用户要了"
        "什么、做了什么、结果如何）\", \"pending_items\": [\"未办完/待跟进的事项"
        "（没有就空数组）\"]}。只写事实，不评价。\n\n" + text,
        log=log, max_tokens=300)
    if out:
        try:
            m = re.search(r"\{.*\}", out, re.S)
            data = json.loads(m.group(0)) if m else {}
            brief = str(data.get("brief", "")).strip() or None
            pending = [str(x).strip() for x in data.get("pending_items", [])
                       if str(x).strip()]
        except ValueError:
            brief = None
    if not brief or brief.strip() == "略":
        log("soul: 摘要蒸馏为空，回退原话截断")
        brief = "；".join(f"用户说{u[:30]}" for u, _ in turns[-3:])[:120]
    line = f"- {time.strftime('%m-%d %H:%M')} 会话：{brief}（{time.strftime('%Y-%m-%d')}）"
    with _write_lock:
        _append_to_section(_SUMM_HEAD, line)
        _known_hash["v"] = _file_hash(MEMORY_PATH)
    for item in pending:
        try:
            import prospective
            prospective.note_pending(item, source="session", log=log)
        except Exception as e:
            log(f"soul: 待办进前瞻层失败（不阻塞摘要）: {e}")
    log("soul: 会话摘要已蒸馏写入")


_STOP_BIGRAMS = frozenset({
    "没有", "什么", "我们", "可以", "一下", "不是", "就是", "为什", "什么",
    "怎么", "这样", "那样", "那个", "这个", "已经", "还是", "不要", "不用",
    "这么", "那么", "知道", "告诉", "现在", "时候", "然后", "因为", "所以",
    "如果", "但是", "而且", "或者", "是不是", "有没", "我有", "你有",
})


def recall(query, log=print, limit=3):
    """检索长期记忆（事实节+摘要节+任务账本终态记录）：二字组重叠打分
    （单字噪声太大），返回最相关条目；命中事实 hits+1 记账（2026-08-29 ③
    检索升级：检索强化=衰减原料）。
    时间直查：问"昨天/今天/前天做了什么"按日期取当天摘要全量。"""
    _migrate_sections(log=log)
    qtext = (query or "").strip()
    day = None
    if "昨天" in qtext:
        day = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    elif "今天" in qtext:
        day = time.strftime("%Y-%m-%d")
    elif "前天" in qtext:
        day = time.strftime("%Y-%m-%d", time.localtime(time.time() - 172800))
    if day:
        hits = [f"摘要：{s[2:].strip()}" for s in _section_lines_raw(_SUMM_HEAD)
                if day in s or s[2:].strip().startswith(day[5:])]
        log(f"soul: 时间直查 {day} 命中 {len(hits)} 条摘要")
        return hits[:limit * 3]

    facts = _read_facts()
    items = ([("事实", d["text"], d) for d in facts]
             + [("摘要", s[2:].strip(), None) for s in _section_lines_raw(_SUMM_HEAD)])

    def _tokens(s):
        s = (s or "").lower()
        words = set(re.findall(r"[a-z0-9]+", s))
        cjk = re.sub(r"[^一-鿿]", "", s)
        bigrams = {cjk[i:i + 2] for i in range(len(cjk) - 1)}
        return (words | bigrams) - _STOP_BIGRAMS

    q = _tokens(query)
    scored = []
    for kind, text, d in items:
        score = len(q & _tokens(text)) if q else 0
        if score >= 2:   # 至少两个二字组命中才算相关；弱命中宁可说不知道
            scored.append((score, kind, text, d))
    scored.sort(key=lambda x: -x[0])
    picked = scored[:limit]
    # 语义补漏（2026-08-31 本地 embedding，emb_local 全程 fail-soft）：
    # 只在关键词零命中时出手——同义改写查不到的痛点就是零命中；
    # 词面有命中时不动排序（词面准=词面先），测试面也因此零影响。
    if not picked and qtext:
        try:
            import emb_local
            for _i, _cos in emb_local.semantic_hits(
                    qtext, [t for _, t, _ in items], top_k=limit):
                kind, text, d = items[_i]
                picked.append((0, kind, text, d))
                log(f"soul: 语义补漏命中 cos={_cos}: {text[:20]!r}")
        except Exception:
            pass
    out = [f"{kind}：{text}" for score, kind, text, d in picked]
    # 命中事实 hits+1 并落盘（检索强化；语义命中的事实同权记账）
    bumped = [d for _, _, _, d in picked if d]
    if bumped:
        for d in facts:
            if any(d is b for b in bumped):
                d["hits"] = int(d.get("hits", 0)) + 1
        _write_facts(facts)
    # 任务账本终态记录入检索（只终态：completed/failed/cancelled；在办不进）
    if q:
        try:
            with open(_LEDGER_PATH, "r", encoding="utf-8") as f:
                ledger = json.load(f)
        except (OSError, ValueError):
            ledger = []
        for rec in ledger if isinstance(ledger, list) else []:
            if rec.get("state") not in ("completed", "failed", "cancelled"):
                continue
            goal = str(rec.get("latest_goal") or rec.get("task_text") or "")
            if len(q & _tokens(goal)) >= 2:
                out.append(f"账本：{goal}（{rec['state']}）")
    return out[:limit * 2]


def consolidate(log=print, max_facts=15, max_summaries=30):
    """睡眠整理 v2（2026-08-29 ②巩固管道，会话关闭时异步跑）：
    情景提升（LLM 从摘要提取纠正/约定/偏好，喂已有清单判重）、
    过时取代（LLM 指认→归档节可回捞）、机械衰减（90 天未命中非 identity→归档）、
    事实归并（超限 LLM 归并）、摘要压缩（溢出合并月度概要，代截断丢弃）。
    LLM 不可用只做机械部分（衰减照跑，提升/归并/压缩放弃，不写错数据）。"""
    _migrate_sections(log=log)
    facts = _read_facts()
    summaries = _section_lines_raw(_SUMM_HEAD)
    archive = _section_lines_raw("# 归档")
    today = time.strftime("%Y-%m-%d")

    def _days_old(date):
        try:
            return (time.time() - time.mktime(time.strptime(date, "%Y-%m-%d"))) / 86400
        except (ValueError, TypeError):
            return 0.0

    # ① 机械衰减（先行：90 天未命中且非 identity → 归档）
    keep, decayed = [], []
    for d in facts:
        if d["kind"] != "identity" and int(d.get("hits", 0)) == 0 \
                and d["date"] and _days_old(d["date"]) > 90:
            decayed.append(d)
        else:
            keep.append(d)
    for d in decayed:
        archive.append("- " + _fmt_fact(d).replace("〈", "〈已衰减：", 1)
                       if False else f"- 已衰减：{d['text']}〈{d['date']}·{d['kind']}·#{d['hits']}〉")
    if decayed:
        log(f"soul: 机械衰减 {len(decayed)} 条入归档")
    facts = keep

    # ② 情景提升 + 过时取代（LLM 巩固器，喂已有清单语义判重）
    llm_text = deepseek_call(
        "你是记忆巩固器。下面是一位语音助手用户的长期事实清单和最近会话摘要。"
        "从摘要里提取值得长期记住的新事实（纠正/约定/偏好类，跳过已在清单里的"
        "重复项），并指认清单里已过时该归档的旧事实。\n"
        "只回 JSON：{\"new_facts\": [{\"text\": \"...\", \"kind\": "
        "\"agreement|correction|preference|fact\"}], \"outdated\": [\"清单里过时条目的原文\"]}\n\n"
        "已有清单：\n" + "\n".join("- " + d["text"] for d in facts)
        + "\n\n最近摘要：\n" + "\n".join(summaries[-12:]),
        log=log, max_tokens=500)
    if llm_text:
        try:
            m = re.search(r"\{.*\}", llm_text, re.S)
            data = json.loads(m.group(0)) if m else {}
        except ValueError:
            data = {}
        existing_texts = {d["text"] for d in facts}
        for nf in data.get("new_facts", []):
            text = str(nf.get("text", "")).strip()
            if text and text not in existing_texts:
                facts.append({"text": text, "date": today,
                              "kind": str(nf.get("kind", "fact")),
                              "hits": 0})
                existing_texts.add(text)
                log(f"soul: 巩固提升 1 条（{nf.get('kind')}）: {text[:30]!r}")
        for old in data.get("outdated", []):
            old = str(old).strip()
            rest = [d for d in facts if d["text"] != old]
            if len(rest) < len(facts):
                d = next(d for d in facts if d["text"] == old)
                archive.append(
                    f"- 已过时：{d['text']}〈{d['date']}·{d['kind']}·#{d['hits']}〉")
                facts = rest
                log(f"soul: 过时取代入归档: {old[:30]!r}")

    # ③ 事实归并（超上限 → LLM 合并成不超过 max_facts-3 条）
    if len(facts) > max_facts:
        merged_text = deepseek_call(
            "这是语音助手关于用户的长期事实清单（含元数据）。把语义重复/近似的"
            f"条目合并成不超过 {max_facts - 3} 条，只保留长期有效的偏好/习惯/约定。"
            "只回 JSON 数组：[{\"text\": \"...\", \"kind\": "
            "\"agreement|correction|preference|fact\"}]\n"
            + "\n".join("- " + _fmt_fact(d) for d in facts),
            log=log, max_tokens=600)
        if merged_text:
            try:
                m = re.search(r"\[.*\]", merged_text, re.S)
                merged = json.loads(m.group(0)) if m else []
            except ValueError:
                merged = []
            if merged:
                facts = [{"text": str(x.get("text", "")).strip(),
                          "date": today,
                          "kind": str(x.get("kind", "fact")), "hits": 0}
                         for x in merged if str(x.get("text", "")).strip()]
                log(f"soul: 事实归并 → {len(facts)} 条")

    # ④ 摘要压缩（溢出合并月度概要，不截断丢弃）
    if len(summaries) > max_summaries:
        older, recent = summaries[:-max_summaries], summaries[-max_summaries:]
        monthly = deepseek_call(
            "把下面若干条会话摘要汇总为一段不超过 120 字的月度概要：保留做过的事"
            "和未办完的事，不写日期流水。\n" + "\n".join(older),
            log=log, max_tokens=200)
        if monthly:
            summaries = [f"- 月度概要（{today[:7]}）：{monthly.strip()}"] + recent
            log(f"soul: 摘要压缩 {len(older)} 条 → 月度概要")
        else:
            summaries = recent   # LLM 不可用才截断

    # 重写：事实节 + 归档节 + 摘要节（其余节不动）
    header, sections = _read_sections_full()
    if header is None:
        header = ("# 长期记忆\n\n这里记录用户的偏好、习惯和明确要求记住的事。"
                  "对话中优先参考。\n\n")
    body = (header + _FACTS_HEAD + "\n\n"
            + "\n".join("- " + _fmt_fact(d) for d in facts)
            + "\n\n# 归档\n\n" + "\n".join(archive)
            + "\n\n" + _SUMM_HEAD + "\n\n" + "\n".join(summaries) + "\n")
    with _write_lock:
        with open(MEMORY_PATH, "w", encoding="utf-8") as f:
            f.write(body)
        _known_hash["v"] = _file_hash(MEMORY_PATH)
    log(f"soul: 睡眠整理完成（事实 {len(facts)} / 摘要 {len(summaries)} / "
        f"归档 {len(archive)}）")


deepseek_call = _deepseek   # 记忆侧认知工作唯一调用口（四步路线：可 mock/可换实现）
