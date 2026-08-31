# -*- coding: utf-8 -*-
"""P3 分层验收 benchmark（标杆载体=无风险系统载体：计算器/记事本/画图/资源管理器）。

每条任务：语音可说的自然指令 → gui_agent 真实操作 → 程序化核验（进程/窗口）→ 记成功。
全程鼠标被接管，运行前确保用户不使用电脑。
结果写 压测/gui-benchmark.json。验收口径（报告 §P3）：≥30 次样本成功率 ≥60%，
总时长 P50 <30s/任务（不含重想）。
用法: python tools/benchmark_gui.py [样本数，默认3条冒烟]（从项目根运行）
"""
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))

import json
import os
import subprocess
import time
from pathlib import Path

import gui_agent
import host_input

OUT = Path(__file__).parent / "压测" / "gui-benchmark.json"


def _inject_alive():
    """注入探针：SetCursorPos 往返 +1px 再复位。False = 系统注入链楔死
    （2026-08-21 事故：explorer 开始菜单模态被高频 Win 键打崩，全系统注入零效果）。
    光标必须复位，探针本身不能成为屏幕噪声。"""
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    pt0 = wintypes.POINT()
    u32.GetCursorPos(ctypes.byref(pt0))
    u32.SetCursorPos(pt0.x + 1, pt0.y + 1)
    time.sleep(0.15)
    pt1 = wintypes.POINT()
    u32.GetCursorPos(ctypes.byref(pt1))
    u32.SetCursorPos(pt0.x, pt0.y)
    return (pt1.x, pt1.y) != (pt0.x, pt0.y)


def _revive_shell(log=print):
    """explorer 重启自愈（两次事故均实证有效）。返回注入是否恢复。"""
    log("[基建] 注入探针失败，重启 explorer 自愈…")
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Stop-Process -Name explorer -Force"], capture_output=True)
    time.sleep(3)
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "if (-not (Get-Process explorer -EA SilentlyContinue)) "
                    "{ Start-Process explorer.exe }"], capture_output=True)
    time.sleep(3)
    alive = _inject_alive()
    log(f"[基建] 自愈后注入探针：{'恢复' if alive else '仍死'}")
    return alive

# (任务指令, 核验函数名, 收尾清理)
# explorer 的核验依赖"之前窗口数"，在 main 里动态实例化
TASKS = [
    ("打开 Windows 自带的计算器（可以按 Win 键后输入 calculator 再回车）",
     "calc", ["powershell", "-Command", "Stop-Process -Name CalculatorApp,Calculator -Force -EA SilentlyContinue"]),
    ("打开记事本（可以按 Win 键后输入 notepad 再回车）",
     "notepad", ["powershell", "-Command", "Stop-Process -Name notepad -Force -EA SilentlyContinue"]),
    ("打开画图（可以按 Win 键后输入 mspaint 再回车）",
     "mspaint", ["powershell", "-Command", "Stop-Process -Name mspaint -Force -EA SilentlyContinue"]),
    ("打开 Windows 设置（可以按 Win 键后输入 settings 再回车）",
     "settings", ["powershell", "-Command", "Stop-Process -Name SystemSettings -Force -EA SilentlyContinue"]),
    ("打开远程桌面连接（可以按 Win 键后输入 mstsc 再回车）",
     "mstsc", ["powershell", "-Command", "Stop-Process -Name mstsc -Force -EA SilentlyContinue"]),
    ("打开截图工具（可以按 Win 键后输入 截图工具 再回车）",
     "snippingtool", ["powershell", "-Command", "Stop-Process -Name SnippingTool -Force -EA SilentlyContinue"]),
    ("打开文件资源管理器（可以按 Win 键后输入 explorer 再回车，或直接按 Win+E）",
     "explorer", [__import__("sys").executable, "-c",
                  "import sys, os; sys.path.insert(0, os.path.dirname(r'%s')); import benchmark_gui; benchmark_gui._close_explorer_windows()"
                  % os.path.abspath(__file__).replace("'", "\\'")]),
]


def _proc_running(names):
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Get-Process {','.join(names)} -ErrorAction SilentlyContinue | Measure-Object | Select-Object -ExpandProperty Count"],
        capture_output=True, text=True, timeout=15).stdout.strip()
    try:
        return int(out) > 0
    except ValueError:
        return False


def _explorer_windows():
    """资源管理器文件窗口数（CabinetWClass 类名）。"""
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    count = {"n": 0}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(64)
        u32.GetClassNameW(hwnd, buf, 64)
        if buf.value == "CabinetWClass":
            count["n"] += 1
        return True

    u32.EnumWindows(cb, 0)
    return count["n"]


def _close_explorer_windows():
    """关掉所有资源管理器文件窗口（CabinetWClass 投 WM_CLOSE）。
    比 Shell.Application.Quit 可靠（后者静默失败污染核验基线）。"""
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(64)
        u32.GetClassNameW(hwnd, buf, 64)
        if buf.value == "CabinetWClass":
            u32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        return True

    u32.EnumWindows(cb, 0)


class _ExplorerCheck:
    """打开资源管理器的核验：窗口数比之前 +1。"""

    def __init__(self):
        self.before = _explorer_windows()

    def __call__(self):
        return _explorer_windows() > self.before


VERIFIERS = {
    "calc": lambda: _proc_running(["CalculatorApp", "Calculator"]),
    "notepad": lambda: _proc_running(["notepad"]),
    "mspaint": lambda: _proc_running(["mspaint"]),
    "settings": lambda: _proc_running(["SystemSettings"]),
    "mstsc": lambda: _proc_running(["mstsc"]),
    "snippingtool": lambda: _proc_running(["SnippingTool"]),
}


def _wait_human_rescue(reason, timeout=300):
    """人机协同：受保护窗口/用户接管时不判失败，叫人处理。
    浮层自然语言提示；等障碍消失且 3 秒无真实输入后返回 True，超时 False。
    蜂鸣是最后手段：只在长时间没被注意到（>120s）才响一下，常规全靠说。"""
    import task_overlay
    print(f"[人机协同] 需要人工: {reason}")
    try:
        task_overlay.show_task(f"请手动处理：{reason}"[:40])
        task_overlay.add_step("处理完我自己接着干，不用叫我")
    except Exception:
        pass
    t0 = time.time()
    last_nag = 0.0
    beeped = False
    while time.time() - t0 < timeout:
        time.sleep(2)
        blocked, pname = gui_agent._foreground_blocked()
        watcher = gui_agent._get_watcher()
        quiet = time.perf_counter() - watcher.last_real_input > 3.0
        if not blocked and quiet:
            try:
                task_overlay.finish(ok=True, note="人工已处理，继续")
            except Exception:
                pass
            time.sleep(1.0)
            return True
        # 持续表达：每 10 秒刷新浮层（卡住时必须让人知道它在等什么）
        if time.time() - last_nag > 10:
            last_nag = time.time()
            remain = int(timeout - (time.time() - t0))
            what = f"请关掉「{pname}」" if blocked else "你刚接管了鼠标，我在等你腾出手"
            try:
                task_overlay.show_task(f"{what}（{remain}s 内）"[:44])
            except Exception:
                pass
        # 最后手段：等了 120 秒还没被注意到，才蜂鸣一次
        if not beeped and time.time() - t0 > 120:
            beeped = True
            try:
                import winsound
                winsound.Beep(660, 200)
            except Exception:
                pass
    try:
        task_overlay.finish(ok=False, note="等待超时")
    except Exception:
        pass
    return False


def main():
    n = int(__import__("sys").argv[1]) if len(__import__("sys").argv) > 1 else 3
    # 防闲置锁屏：跑 benchmark 期间保持系统/显示器唤醒（锁屏会污染样本）
    import ctypes as _ct
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2
    _ct.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
    tasks = (TASKS * ((n // len(TASKS)) + 1))[:n]
    # 先全场清理一次：上一轮残留窗口（尤其 Taskmgr 这类受保护前台）会污染首样本
    for _, _, cleanup in TASKS:
        subprocess.run(cleanup, capture_output=True)
    time.sleep(2.0)
    results = []
    invalid = 0  # 基建故障（403/网络）样本不计入成功率
    wedge_recoveries = 0  # explorer 楔死自愈次数（基建事件，记台账）
    for i, (task, verifier, cleanup) in enumerate(tasks):
        print(f"\n=== [{i+1}/{len(tasks)}] {task[:40]} ===")
        # 每条前注入探针：楔死则自愈一次；自愈失败整批中止（不污染成功率）
        if not _inject_alive():
            wedge_recoveries += 1
            if not _revive_shell():
                print("[基建] 注入无法恢复，整批安全中止")
                break
        # explorer 核验需要在任务开始前记基线窗口数（先清场再记，防残留污染）
        if verifier == "explorer":
            subprocess.run(cleanup, capture_output=True)
            time.sleep(1.0)
            verify_fn = _ExplorerCheck()
        else:
            verify_fn = VERIFIERS[verifier]
        t0 = time.time()
        dump = os.path.join(Path(__file__).parent, "压测", "frames", f"s{i+1:02d}")
        ok, msg, steps = gui_agent.run(task, max_steps=10, dump_dir=dump)
        assisted = False
        # 人机协同：受保护窗口/用户接管 → 叫人处理，处理完重跑这条（最多 2 次）
        for _ in range(2):
            rescue = (msg.startswith("用户接管") or "受保护" in msg
                      or "管理员" in msg or "UAC" in msg)
            if ok or not rescue:
                break
            if not _wait_human_rescue(msg[:40]):
                break  # 叫人超时，按失败记
            assisted = True
            print("[人机协同] 人工已介入，重跑本条")
            t0 = time.time()
            ok, msg, steps = gui_agent.run(task, max_steps=10, dump_dir=dump)
        if not ok and ("403" in msg or "Forbidden" in msg):
            # 基建故障（欠费/限流）：退避 30s 重试一次；仍 403 则样本作废
            print("基建 403，30s 后重试一次…")
            time.sleep(30)
            t0 = time.time()
            ok, msg, steps = gui_agent.run(task, max_steps=10)
            if not ok and ("403" in msg or "Forbidden" in msg):
                print("仍 403，样本作废（不计入成功率）")
                invalid += 1
                subprocess.run(cleanup, capture_output=True)
                time.sleep(2.0)
                continue
        dur = time.time() - t0
        time.sleep(1.5)  # 等应用起来
        verified = verify_fn()
        success = ok and verified
        results.append({"i": i, "task": task, "agent_ok": ok, "verified": verified,
                        "success": success, "assisted": assisted, "msg": msg[:120],
                        "steps": steps, "duration_s": round(dur, 1)})
        print(f"结果: agent={ok} 核验={verified} 耗时={dur:.1f}s 步数={steps}")
        # 增量落盘：进程被杀/超时时已完成样本不丢（正式汇总仍在结尾写 OUT）
        try:
            partial = OUT.with_name("gui-benchmark.partial.json")
            d = sorted(r["duration_s"] for r in results)
            partial.write_text(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "partial": True,
                "samples": len(results), "invalid_infra": invalid,
                "success": sum(1 for r in results if r["success"]),
                "duration_p50": d[len(d) // 2] if d else None,
                "results": results}, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            pass
        subprocess.run(cleanup, capture_output=True)  # 收尾
        time.sleep(1.0)

    succ = [r for r in results if r["success"]]
    n_assist = sum(1 for r in results if r.get("assisted"))
    durs = sorted(r["duration_s"] for r in results)
    valid = len(results)  # results 里已不含作废样本
    summary = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "samples": valid,
        "invalid_infra": invalid,
        "wedge_recoveries": wedge_recoveries,
        "human_assists": n_assist,
        "success": len(succ),
        "success_rate": round(len(succ) / max(valid, 1), 3),
        "duration_p50": durs[len(durs) // 2] if durs else None,
        "results": results,
    }
    # 累积写：多次运行合并样本
    old = []
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8")).get("all_results", [])
        except Exception:
            old = []
    summary["all_results"] = old + results
    summary["cum_samples"] = len(summary["all_results"])
    summary["cum_success"] = sum(1 for r in summary["all_results"] if r["success"])
    summary["cum_rate"] = round(summary["cum_success"] / max(summary["cum_samples"], 1), 3)
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n本次 {len(succ)}/{len(results)} 成功；累计 {summary['cum_success']}/{summary['cum_samples']} "
          f"= {summary['cum_rate']*100:.1f}%；P50={summary['duration_p50']}s")
    print(f"已写 {OUT}")


if __name__ == "__main__":
    main()
