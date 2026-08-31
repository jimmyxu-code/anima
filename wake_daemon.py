# -*- coding: utf-8 -*-
"""wake_daemon：退下后的极简唤醒守护（2026-08-31 用户裁决：真退下+仍可语音唤醒）。

主助手全退（光球/面板/任务/子进程）后由它常听唤醒词；命中→拉起主助手→自己退出。
主助手在跑→立即退出（不抢麦不抢唤醒）。不 import companion_rt 任何东西，
唤醒链全部件自包含（wake_word 同目录复用：钉本机麦/断流狗/阈值都在那）。
"""
import ctypes
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_VENV = r"<项目根>\.venv\Lib\site-packages"
if _VENV not in sys.path and os.path.isdir(_VENV):
    sys.path.append(_VENV)   # 裸拉起（无 PYTHONPATH）也要能跑（集成测试实锤）
_LOCK = os.path.join(_HERE, ".companion_rt.lock")
_PY = r"<项目根>\runtime\python-3.13.12\pythonw.exe"
_LOG = os.path.join(_HERE, "wake_daemon.log")


def _log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    try:
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _main_alive():
    """单实例闸同口径：lock 里 pid 活着=主助手在跑。"""
    try:
        pid = int(open(_LOCK).read().strip() or "0")
    except (OSError, ValueError):
        return False
    if not pid:
        return False
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong(0)
        if not ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def _launch_main():
    """脱离子进程拉起主助手（与 tools/拉起小凯.ps1 同参数）。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = _VENV
    subprocess.Popen([_PY, os.path.join(_HERE, "companion_rt.py")],
                     cwd=_HERE, env=env, close_fds=True,
                     creationflags=0x00000008 | 0x00000200)  # DETACHED|NEW_GROUP
    _log("唤醒命中：主助手已拉起，守护退出")


def main():
    # 退下拉起时主程序还没死透——等它死透再开听（不抢麦不抢唤醒；竞态实锤：
    # 守护若启动即查 lock，主程序还活着=守护秒退=召回链断）
    for _ in range(60):   # 最多等 120s
        if not _main_alive():
            break
        time.sleep(2)
    if _main_alive():
        _log("主程序 120s 未退，守护退出")
        return
    test_fire = "--test-fire" in sys.argv   # 集成测试：不等语音直接触发
    _log("唤醒守护上线（常听中）")
    import wake_word
    fired = {"on": False}

    def _fire():
        if fired["on"]:
            return
        fired["on"] = True
        _launch_main()
        time.sleep(2)   # 让主程序接管麦/lock
        os._exit(0)

    listener = wake_word.WakeWordListener(on_wake=_fire, log=_log)
    listener.start()
    if test_fire:
        _fire()
    while True:
        time.sleep(5)
        if _main_alive():
            _log("主助手已在跑（别的路拉起），守护退出")
            return


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _log(f"守护异常退出: {e}")
