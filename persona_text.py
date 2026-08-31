# -*- coding: utf-8 -*-
"""人格净化闸（2026-08-27 立项）：一切过嘴的文本不许带内部术语。

用户实锤投诉："还提到了模型显示怎么怎么样，这显然就不是一个人格呀"。
系统腔（模型/执行器/provider/动作/契约/FC/gateway…）只要出现在语音里，
"远程同事"人设就当场穿帮。

用法（唯一的两个出口都接了）：
    - companion_rt._inject_rag 双工分支：say_hello(line) 前过 scrub
    - expression voice 通道：绑定时包 scrub
日志/面板保留原文（技术排障要真话），只洗"嘴"。
"""

# 内部术语 → 人话（顺序敏感：长词先换）
_SCRUB_PAIRS = [
    ("gui_agent", "前台操作"),
    ("exec-native", "后台"),
    ("provider", "服务"),
    ("Provider", "服务"),
    ("API", "接口"),
    ("api", "接口"),
    ("FC", "工具调用"),
    ("GUI", "界面操作"),
    ("execute", "执行"),
    ("launch_app", "启动应用"),
    ("action", "操作指令"),
    ("hotkey", "快捷键"),
    ("click", "点击"),
    ("执行器", "后台"),
    ("执行层", "后台"),
    ("大模型", "我这边"),
    ("视觉模型", "我的眼睛"),
    ("模型那边", "我这边"),
    ("模型", "我这边"),
    ("路由脑", "我"),
    ("大脑调用", "思考"),
    ("令牌", "凭据"),
    ("会话句柄", "对话"),
]

# 净化后仍不许出现的词（兜底黑名单——出现即 scrub 失效，测试会红）
_BANNED_AFTER = ("模型", "执行器", "执行层", "provider", "Provider",
                 "gui_agent", "exec-native", "launch_app")


def scrub(text):
    """洗掉内部术语。输入任意（None 安全），输出人话。"""
    if not text:
        return text or ""
    out = str(text)
    for a, b in _SCRUB_PAIRS:
        out = out.replace(a, b)
    # 重复换词可能产生怪串（如"我这边那边"），再清一轮常见残渣
    out = (out.replace("我这边那边", "我这边")
              .replace("我这边那边", "我这边")
              .replace("我这边显示我这边", "我这边显示"))
    return out


def persona_clean(text):
    """断言用：洗完后没有任何内部术语残留。"""
    return not any(b in (text or "") for b in _BANNED_AFTER)
