"""Agent Space 后端：SSE 事件推送 + 页面托管 + Chrome kiosk 启动器。

页面渲染交给 Chrome（--kiosk 全屏），状态经 SSE 从 agent_flow 的 HTTP
服务推给页面：用户语音文本、agent 流式回复、聆听/思考/播报状态。
"""

import os
import queue
import subprocess
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
_state_lock = threading.Lock()
_clients = []            # SSE 客户端队列列表
_state = {"mode": "idle", "user": "", "agent": "", "history": []}


def _broadcast(obj):
    import json
    data = json.dumps(obj, ensure_ascii=False)
    with _state_lock:
        clients = list(_clients)
    for q in clients:
        try:
            q.put_nowait(data)
        except queue.Full:
            pass


def emit_state(mode):
    """mode: idle / listening / thinking / speaking"""
    _state["mode"] = mode
    _broadcast({"type": "state", "mode": mode})


def emit_user(text):
    _state["user"] = text
    _state["agent"] = ""
    _state["history"].append(("user", text))
    _broadcast({"type": "user", "text": text})


def emit_agent_delta(accumulated):
    _state["agent"] = accumulated
    _broadcast({"type": "delta", "text": accumulated})


def emit_agent_done(text):
    _state["agent"] = text
    _state["history"].append(("agent", text))
    _broadcast({"type": "agent", "text": text})


def subscribe():
    """SSE 客户端注册，返回其队列。"""
    q = queue.Queue(maxsize=200)
    with _state_lock:
        _clients.append(q)
    return q


def unsubscribe(q):
    with _state_lock:
        if q in _clients:
            _clients.remove(q)


def snapshot():
    return dict(_state)


# ---------------- Chrome kiosk 启动器 ----------------
_space_proc = None


def _chrome():
    c = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    if not os.path.exists(c):
        c = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    return c


def open_space(log=print):
    """全屏打开 Agent Space（Chrome kiosk）。"""
    global _space_proc
    if _space_proc is not None and _space_proc.poll() is None:
        return
    profile = os.path.join(HERE, ".chrome-space")
    _space_proc = subprocess.Popen([
        _chrome(),
        f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check",
        "--kiosk", "http://127.0.0.1:17893/space",
    ])
    log(f"Agent Space 已打开 (pid {_space_proc.pid})")


def close_space(log=print):
    global _space_proc
    if _space_proc is not None and _space_proc.poll() is None:
        try:
            _space_proc.terminate()
            log("Agent Space 已关闭")
        except Exception as e:
            log(f"close_space error: {e}")
    _space_proc = None


def is_open():
    return _space_proc is not None and _space_proc.poll() is None
