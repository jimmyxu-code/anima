# -*- coding: utf-8 -*-
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
"""确认链迁移第一批回归（2026-08-23）：模型先裁、正则兜底。

覆盖：① intent_judge.judge_yesno / judge_intents 的离线口径（monkeypatch _post，
不发真请求）；② companion_rt 两级判定的口径复刻（同 test_gates 家法：不 import
companion_rt——副作用大，正则与两级语义按源码复刻，源码变动须同步本文件）。
服务北极星：①（同义说法不再反复追问）+ 铁律"不堆屎山"。"""
import re

import intent_judge

FAIL = []
TOTAL = 0


def check(name, got, want):
    global TOTAL
    TOTAL += 1
    ok = got == want
    print(("[OK ] " if ok else "[FAIL] ") + name + f" got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


# ---------- ① intent_judge.judge_yesno 离线口径 ----------
def fake_post(content):
    def _p(messages, max_tokens, temperature, timeout):
        return content
    return _p


orig_post = intent_judge._post
try:
    intent_judge._post = fake_post('["yes", "no", "unclear"]')
    check("yesno 归一化 yes/no/unclear",
          intent_judge.judge_yesno(["没问题，弄吧", "别弄了", "再想想"]),
          [True, False, None])

    intent_judge._post = fake_post('[true, false]')
    check("yesno JSON 布尔直通",
          intent_judge.judge_yesno(["弄吧", "算了"]), [True, False])

    intent_judge._post = fake_post('["同意", "拒绝", "随便"]')
    check("yesno 中文词归一化 + 未知词=没说清",
          intent_judge.judge_yesno(["a", "b", "c"]), [True, False, None])

    intent_judge._post = fake_post('["yes", "no"]')  # 长度不符
    check("yesno 数组长度不符 → fail-closed",
          intent_judge.judge_yesno(["只给一条", "x", "y"]), [None, None, None])

    intent_judge._post = fake_post("模型说错了没有数组")
    check("yesno 非法输出 → fail-closed",
          intent_judge.judge_yesno(["嗯"]), [None])

    def boom(*a, **k):
        raise TimeoutError("timeout")
    intent_judge._post = boom
    check("yesno 超时/异常 → fail-closed（不猜同意）",
          intent_judge.judge_yesno(["随便什么"]), [None])

    check("yesno 空输入", intent_judge.judge_yesno([]), [])

    # judge_intents 旧坑回归（"true"/"false" 字符串归一化，2026-08 修复口径）
    intent_judge._post = fake_post('["true", "false"]')
    check("intents 字符串归一化保持",
          intent_judge.judge_intents(["a", "b"]), [True, False])
    intent_judge._post = boom
    check("intents 异常 → fail-closed 全危险",
          intent_judge.judge_intents(["a", "b"]), [True, True])

    intent_judge._post = fake_post('[true, false, false]')
    check("最大自主模型只判不可恢复删除",
          intent_judge.judge_irrecoverable_deletions(
              ["永久删除文件", "发送微信", "关机"]),
          [True, False, False])
    intent_judge._post = boom
    check("不可恢复删除模型异常 → 明确返回 None 交机械兜底",
          intent_judge.judge_irrecoverable_deletions(["del a.txt"]), None)
finally:
    intent_judge._post = orig_post


# ---------- ② companion_rt 两级判定口径复刻（源码：_judge_yesno/_is_dangerous） ----------
_YES_PAT = re.compile(
    r"^(可以|可以的|确认|同意|是|是的|对|继续执行|继续|执行|允许|批准|ok|okay|yes)$", re.I)
_NO_PREFIXES = ("不可以", "不行", "不要", "不用", "不必", "不准", "别", "取消", "算了", "停", "拒绝")
_DANGER_PAT = re.compile(r"删除|删掉|清空|格式化|关机|重启|发送|发给|发消息|转账|付款|支付|红包|下单|购买|付款码")


def _judge_yesno_re(text):  # 复刻第一级（正则白名单）
    t = (text or "").strip().rstrip("。！!~～")
    if not t:
        return None
    low = t.lower()
    if low in ("不", "否", "no", "nope") or t.startswith(_NO_PREFIXES):
        return False
    if _YES_PAT.match(t):
        return True
    return None


for text, want in [("可以。", True), ("确认", True), ("OK", True),
                   ("不可以", False), ("算了别弄了", False), ("no", False),
                   ("好的", None), ("嗯", None), ("没问题弄吧", None)]:
    check(f"第一级正则: {text!r}", _judge_yesno_re(text), want)

# 复刻两级 _is_dangerous：正则召回 → 模型终审 → 异常/关闭维持正则
model_calls = []


def _is_dangerous_repl(task_text, model_on=True, model_ret=False, model_exc=False):
    t = task_text or ""
    if _DANGER_PAT.search(t):
        return True
    if not t.strip() or not model_on:
        return False
    try:
        model_calls.append(t)
        if model_exc:
            raise RuntimeError("model down")
        return model_ret
    except Exception:
        return False


model_calls.clear()
check("危险两级: 正则命中 → 危险（不调模型）",
      _is_dangerous_repl("删掉D盘那个文件"), True)
check("模型未被调用（正则短路）", model_calls, [])

check("危险两级: 正则未中+模型判危险 → 危险",
      _is_dangerous_repl("把那个东西弄没", model_ret=True), True)
check("危险两级: 正则未中+模型判安全 → 放行",
      _is_dangerous_repl("打开记事本写一段话", model_ret=False), False)
check("危险两级: 模型异常 → 维持正则结果",
      _is_dangerous_repl("remove the file", model_exc=True), False)
check("危险两级: config 关闭 → 纯正则",
      _is_dangerous_repl("remove the file", model_on=False), False)

print()
# ---------- ⑤ _ask_confirm 静默看门狗回归（2026-08-30 展示前修复） ----------
# 等确认不能静默 60s，更不能两轮共 120s。本测试用 timeout=1s 真跑，
# 断言中途明确说“没听清”，总窗口到点即诚实收住并返回 unavailable。
import time as _time
import companion_rt as _rt

_injects = []
_orig = {n: getattr(_rt, n, None) for n in ("_inject_rag",)}
_orig_io = {n: getattr(_rt.sense_io, n) for n in ("session_open", "orb_mode")}
_orig_ov = {n: getattr(_rt.task_overlay, n)
            for n in ("work_begin", "work_end", "log_line")}
_rt._inject_rag = lambda t, c: _injects.append((t, c))
_rt.sense_io.session_open = lambda: True
_rt.sense_io.orb_mode = lambda m: None
_rt.task_overlay.work_begin = lambda *a, **k: None
_rt.task_overlay.work_end = lambda *a, **k: None
_rt.task_overlay.log_line = lambda *a, **k: None
try:
    _t0 = _time.time()
    _out = _rt._ask_confirm("测试操作？", timeout=1)
    _dt = _time.time() - _t0
finally:
    _rt._inject_rag = _orig["_inject_rag"]
    for n, v in _orig_io.items():
        setattr(_rt.sense_io, n, v)
    for n, v in _orig_ov.items():
        setattr(_rt.task_overlay, n, v)

check("确认总超时后返回 unavailable", _out, "unavailable")
check("首问+中途没听清反馈+到点收住，共 3 次播报", len(_injects), 3)
check("总耗时不翻倍（约 1×timeout）", 0.8 <= _dt < 1.8, True)

# ---------- ⑥ companion_rt 确认链：模型必须先于正则裁决 ----------
_orig_judge = _rt.intent_judge.judge_yesno
_orig_speak = _rt._speak_fast
_spoken = []
try:
    # “嗯”不在机械白名单；模型判 yes 后必须一次放行并立即有反馈。
    _rt.intent_judge.judge_yesno = lambda texts, timeout=4: [True]
    _rt._speak_fast = lambda text: _spoken.append(text)
    _cid = _rt._begin_confirmation(wait_locked=0.1)
    check("确认测试轮成功建立", bool(_cid), True)
    check("模型主判异步已启动", _rt._judge_yesno_model_async("嗯"), True)
    _deadline = _time.time() + 1.0
    while _rt._confirm_judge_busy["on"] and _time.time() < _deadline:
        _time.sleep(0.01)
    _answer = _rt._confirm["answer"].get_nowait()
    check("口语‘嗯’由模型一次判为同意", _answer, (_cid, True))
    check("模型裁决后立即语音回执", _spoken, ["好的，这就做"])
finally:
    if '_cid' in globals() and _cid:
        _rt._finish_confirmation(_cid)
    _rt.intent_judge.judge_yesno = _orig_judge
    _rt._speak_fast = _orig_speak

print(f"总计 {TOTAL} 项检查，失败 {len(FAIL)} 项" if FAIL else f"CONFIRM_JUDGE_TEST PASS（{TOTAL} 项）")
raise SystemExit(1 if FAIL else 0)
