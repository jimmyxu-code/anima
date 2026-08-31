# -*- coding: utf-8 -*-
"""expression 插件：唯一表达出口（P1 收编）。

一体感的落法：语音（rag 注回）、任务浮层、文字面板，全部从同一个
"表达事件"派生——措辞只在这一处写，通道只负责排版，不各自发挥
（此前三处独立写字符串，是"汇报和实际不切合"的结构性温床）。

通道由 companion_rt 启动时 bind 进来；未 bind 的通道静默跳过（不拔报警）。
"""

# 任务生命周期事件的唯一措辞口：kind → (voice 注入指令, overlay 浮层行)
# voice 是给语音人格的转述指令（它会用自己的话说）；overlay 是事实行。
_TASK_EVENTS = {
    "accepted": (
        "「{task}」刚受理，执行层正在开始，离做完还早。"
        "跟用户说一句你去做（一句话说完）；绝对不许说已经做完/做好了/弄好了。",
        "受理：{task}"),
    "queued": (
        "新任务已收到，但手上的活还没做完。"
        "跟用户说：手上这个还没弄完，完了马上做这个；"
        "要是等太久，可以说'先别做了'。",
        "排队：{task}"),
    "progress_stuck": (
        # 2026-08-27 用户令：屏幕没变化不用嘴说——前台任务里这是要内化的
        # 常态；语音留空，只上文字框（要做详细的是面板，不是嘴）。
        "",
        "这一步屏幕上没有变化：{detail}（在核实有没有生效）"),
    "done": (
        "任务已完成，结果是：{detail}。用一句话自然汇报给用户，可以说做完了。",
        "完成：{detail}"),
    "done_unverified": (
        "执行层说做完了，但屏幕核验没通过：{detail}。"
        "如实把给定的所见告诉用户（比如'我看了眼屏幕，没看到结果'），"
        "禁止编造；需要重做用户自己会说，不向用户求助。",
        "待验证：{detail}"),
    "failed": (
        "{detail}只许用这个给定的原因如实告诉用户卡在哪，"
        "禁止编造你没看到的细节（比如'文件被占用'这类理由）。"
        "只陈述事实，不向用户求助。",
        "受挫：{detail}"),
    "failed_gui": (
        "前后台两种方式都试过了，还是没做成：{detail}。"
        "只许用这个给定的原因如实告诉用户卡在哪，禁止编造。"
        "只陈述事实，不向用户求助。",
        "受挫：{detail}"),
    "failed_bg": (
        "前后台两种方式都试过了，还是没做成：{detail}。"
        "如实告诉用户卡在哪，并给一句下一步的新方向（换个方法/需要他怎么配合），"
        "不许只说'没做成'就停；只陈述事实。",
        "受挫：后台未完成"),
    "paused_takeover": (
        "用户刚才动了鼠标或键盘，屏幕操作任务已暂停。"
        "问用户：这个任务继续还是放弃？",
        "已暂停（用户接管）"),
    "amended": (
        "收到修订：任务改成「{detail}」。跟用户说一句收到、这就按新的来，"
        "一句话。",
        "修订：{detail}"),
    "stopped": ("", "已叫停"),   # 叫停路径已自行播报，语音留空
}


def _render(kind, task="", detail=""):
    """一个事件 → (voice_text, overlay_line)。未知 kind fail-closed 到事实行。"""
    tpl = _TASK_EVENTS.get(kind)
    if tpl is None:
        return f"（未知事件 {kind}）：{detail}", f"{kind}: {detail}"
    voice_t, overlay_t = tpl
    t = (task or "")[:24]
    d = (detail or "")[:60]
    return voice_t.format(task=t, detail=d), overlay_t.format(task=t, detail=d)


def register(ctx):
    channels = {"voice": None, "overlay": None, "panel": None}

    def bind(voice=None, overlay=None, panel=None):
        """companion_rt 启动时绑定通道：voice=rag 注回 fn(text)，
        overlay=浮层行 fn(text)，panel=面板行 fn(text)。"""
        channels.update({k: v for k, v in
                         (("voice", voice), ("overlay", overlay),
                          ("panel", panel)) if v is not None})

    def say(text):
        """自由文本出口（非任务生命周期类：提醒/确认/隐私等），只过语音。"""
        if channels["voice"]:
            channels["voice"](text)

    def event(kind, task="", detail="", voice=True):
        """表达事件：措辞只在本插件写，通道只排版。
        voice=False 时只上框不注音（双工单声道：模型已自行应答的场合，
        再注入一遍受理话术=双声叠放，2026-08-22 实测语无伦次根因之一）。"""
        voice_text, overlay_line = _render(kind, task, detail)
        if voice_text and voice and channels["voice"]:
            channels["voice"](voice_text)
        if overlay_line and channels["overlay"]:
            channels["overlay"](overlay_line)
        if channels["panel"]:
            channels["panel"](overlay_line)
        return voice_text, overlay_line

    ctx.provide("expression", {
        "bind": bind,
        "say": say,
        "event": event,
    })
