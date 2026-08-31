"""运行中前后台模式指令的纯文本判定。"""

import re


def target_mode(text):
    """返回 implicit/explicit/None；明确前台词优先于普通路由。"""
    text = str(text or "")
    if re.search(r"转到后台|调到后台|放到后台|后台做|后台运行", text):
        return "implicit"
    if re.search(r"转到前台|调到前面|调到前台|前台做|让我看着|让我看看|我想看|我要看", text):
        return "explicit"
    return None
