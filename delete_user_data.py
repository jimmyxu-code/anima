# -*- coding: utf-8 -*-
"""P-1-3 一键删除全部用户数据（记忆/转录/日志/会话）。

删除清单（报告 v4/v4.6 口径）：
  MEMORY.md（长期记忆）、persona.yaml（由记忆生成的人格文件）、
  reminders/、sessions/*.jsonl、events/（事件台账）、
  companion_*.log、agent_flow.log、costs.jsonl、.dsh_home（内部 dsh 全部状态）。
  .chrome-space（浏览器 profile，含密码库）——开关在 WebBridge 外部组件侧，
  本脚本只提示，不动它（运行中的浏览器占用也删不干净）。

默认 dry-run 只列出；加 --yes 才真正删除。
用法: python delete_user_data.py [--yes]
"""

import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

ITEMS = [
    ("memory", "dir"),          # MEMORY.md 长期记忆
    ("persona.yaml", "file"),
    ("costs.jsonl", "file"),
    ("events.jsonl", "file"),
    ("agent_flow.log", "file"),
    ("companion_rt.log", "file"),
    ("companion_lite.log", "file"),
    ("reminders", "dir"),
    ("sessions", "dir"),
    ("events", "dir"),
    (".dsh_home", "dir"),
]


def main():
    dry = "--yes" not in sys.argv
    print("DRY-RUN（只列出，不删除）；加 --yes 才执行\n" if dry else "真正删除模式\n")
    found = 0
    for rel, kind in ITEMS:
        p = os.path.join(_HERE, rel)
        if not os.path.exists(p):
            continue
        found += 1
        size = ""
        if kind == "file":
            size = f" ({os.path.getsize(p)} B)"
        print(f"{'[将删除]' if dry else '[删除]'} {rel}{size}")
        if not dry:
            if kind == "file":
                os.remove(p)
            else:
                shutil.rmtree(p, ignore_errors=True)
    chrome = os.path.join(_HERE, ".chrome-space")
    if os.path.isdir(chrome):
        print("[提示] .chrome-space 浏览器 profile 需在 WebBridge 侧改用独立 "
              "--user-data-dir 后另行清理，本脚本不动")
    print(f"\n共 {found} 项{'（未执行）' if dry else '（已删除）'}")


if __name__ == "__main__":
    main()
