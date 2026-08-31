# -*- coding: utf-8 -*-
"""scaffold 插件：临时脚手架层（2026-08-31 用户终裁，与总纲同效）。

凡因模型能力暂时不足而加的机械兜底（意图正则表/语气词表/话术表），
全部收编到本插件，登记为【临时脚手架 · 挂账待拆】：

- 这些机械件**不是系统本身**，是过渡支架——意图识别/逻辑判断的终态全归模型智能；
- **拔掉本插件**（kernel 不装配）或 config `scaffold_<name>=false` 即单件退役，
  主路（模型路由）不受任何影响——这同时就是"拆除验收"通道；
- 安全闸（删/付/不可逆召回面）不在此列：机械 fail-closed 是底线，不是脚手架。

登记件与拆除条件：
| 件 | 做什么 | 因何而生（实锤） | 拆除条件 |
| realtime_guard | 纯实时问句（几点/天气/新闻）硬拦不派活 | 08-30 模型 ±2 条漂移、prompt 打地鼠无解 | test_router 相关用例连续 7 天全绿且生产无回归 |
| task_smell_net | chat 误判的任务气味祈使句强制派活 | 08-30"打开一个新的文档"三路全哑 | 同上（且"抱怨不派活"回归零发生） |
| promise_filter | 受理/承诺话术不算"已答过" | 08-30 受理话术压住真实完成播报 | 语音侧时态纪律稳定后（连续 7 天无防双答误压） |
| retire_gate | "退下吧"类打发指令判定（讨论退下不误退） | 语音指令词表的历史形态 | 模型路由能稳定区分"退下"与"讨论退下"（test_gates 退下用例走模型路连续 7 天全绿） |
| code_task_gate | 代码类任务识别（换 GUI 路保护+超时档） | 08-28 explicit 通道改代码零成功率 | generic 规划器能自辨代码任务并拒走 GUI（连续实测） |
"""
import re
import time

_REALTIME_Q_RE = re.compile(
    r"几点|几点钟|天气|气温|新闻|热搜|汇率|比分|谁赢|股票|股价|金价|油价", re.I)
_COMPUTER_ACTION_RE = re.compile(
    r"电脑|文件|文件夹|桌面|软件|打开|安装|设置|系统|截图|屏幕|窗口|"
    r"下载|磁盘|记事本|浏览器|微信|Excel|word|ppt|代码|插件", re.I)

_TASK_SMELL_RE = re.compile(
    r"打开|新建|创建|保存|存到|存进|关掉|关闭|删除|删掉|启动|运行|下载|安装|"
    r"整理|清理|写一|写个|写篇|改一|改下|设置")
_QUESTION_RE = re.compile(
    r"吗|呢|呀|啊|吧|？|\?|什么|怎么|能不能|会不会|哪|~")
_IMPERATIVE_LEAD_RE = re.compile(
    r"^(?:帮[帮我]?|麻烦|请你?|把|给|去|让|先|打开|新建|创建|保存|关掉|关闭|"
    r"删除|删掉|启动|运行|下载|安装|整理|清理|写|改|设置|切换|换成?|调|找|查)")

_PROMISE_RE = re.compile(r"正在|马上|这就|我去|我来|稍等|一会|等下|接下来|这就去")

# —— 退下指令判定（2026-08-31 自 companion_rt 收编；词表原形 08-31 终裁）——
_RETIRE_RE = re.compile(
    r"退下吧|退一下吧|可以下去了|先下去吧|你休息吧|"
    r"(?:^|[，,。！!]\s*)(?:你)?(?:先)?(?:退下|退一下)[吧啊呀。！!]*$", re.I)
_RETIRE_MENTION_RE = re.compile(
    r"[？?]|什么意思|怎么|之后|以后|有没有|能不能|是否|是不是|"
    r"退下了吗|退下逻辑|退下指令|退下功能|改.{0,8}退下", re.I)

# —— 代码类任务识别（2026-08-31 收编：原 companion_rt/exec_native 两处互指
# 防漂移，单一真源搬进本插件；旧词"助手级"随更名迁移动态失效）——
_CODE_TASK_RE = re.compile(
    r"代码|插件|\.py|程序|脚本|逻辑|智能助手|修改自身|自身代码|bug", re.I)

_state = {"cfg": None}


def _on(name):
    """单件开关：config scaffold_<name>（默认开=过渡态）。bind 可给字典或取值函数。"""
    cfg = _state["cfg"]
    if callable(cfg):
        return bool(cfg(f"scaffold_{name}", True))
    return bool((cfg or {}).get(f"scaffold_{name}", True))


def realtime_guard(text):
    """纯实时信息问句→True（不派活）。沾电脑边的一律放行给模型判。"""
    if not _on("realtime_guard"):
        return False
    t = text or ""
    return bool(_REALTIME_Q_RE.search(t) and not _COMPUTER_ACTION_RE.search(t))


def task_smell_net(text):
    """chat 误判兜底：任务气味的祈使句→True（强制派活）。
    语气词/疑问/抱怨话一律不兜（08-31"我要的是关闭呀"实锤）。"""
    if not _on("task_smell_net"):
        return False
    t = text or ""
    return bool(_TASK_SMELL_RE.search(t) and not _QUESTION_RE.search(t)
                and _IMPERATIVE_LEAD_RE.search(t))


def is_promise(text):
    """受理/承诺话术（"正在改马上就好"）→True；不当"已答过"用。"""
    if not _on("promise_filter"):
        return False
    return bool(_PROMISE_RE.search(text or ""))


def retire_gate(text):
    """只认明确的打发指令（"退下吧"）；讨论"退下"本身绝不误退出。"""
    if not _on("retire_gate"):
        return False
    t = (text or "").strip()
    return bool(t and not _RETIRE_MENTION_RE.search(t)
                and _RETIRE_RE.search(t))


def code_task_gate(text):
    """代码类任务气味→True（换路保护/超时档的临时判据，终态归模型规划器）。"""
    if not _on("code_task_gate"):
        return False
    return bool(_CODE_TASK_RE.search(text or ""))


def register(ctx):
    """kernel 插件形态：可热插拔的临时脚手架（拔掉=模型单跑）。"""
    def bind(cfg_get):
        _state["cfg"] = cfg_get
    ctx.provide("scaffold", {
        "bind": bind,
        "realtime_guard": realtime_guard,
        "task_smell_net": task_smell_net,
        "is_promise": is_promise,
        "retire_gate": retire_gate,
        "code_task_gate": code_task_gate,
    })
