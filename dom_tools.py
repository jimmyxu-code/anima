# -*- coding: utf-8 -*-
"""dom_tools：浏览器 DOM 通道（混合层的浏览器侧）。

原理：Chrome/Edge 开 --remote-debugging-port 后，CDP（DevTools 协议）能直接
读到页面的 DOM 文字结构和每个元素的精确矩形——对网页任务这是百分之百
准确的"文字资料"，不用看截图。找到坐标后仍用真鼠标点（真人观感）。

动作：
- ensure_browser()：没有调试端口的 Chrome 就用 .chrome-space 配置起一个
- open_url(url)：新标签页直达（"打开B站"类任务的必杀，跳过开始菜单）
- click_text(text)：当前标签页内按文字/aria-label 找元素 → 屏幕坐标 → 真鼠标点
"""

import asyncio
import json
import subprocess
import time
import urllib.request

CDP_PORT = 9222
CDP_HTTP = f"http://127.0.0.1:{CDP_PORT}"

_FIND_JS = r"""
(() => {
  const needle = (%s).toLowerCase();
  const cand = document.querySelectorAll(
    'button,a,[role=button],[role=link],input,[aria-label],span,div,li,img');
  let best = null;
  for (const el of cand) {
    const t = ((el.innerText||'') + '|' + (el.getAttribute('aria-label')||'')
               + '|' + (el.title||'') + '|' + (el.getAttribute('placeholder')||'')
               + '|' + (el.alt||'')).toLowerCase();
    if (!t.includes(needle)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    if (r.bottom < 0 || r.right < 0 || r.top > innerHeight || r.left > innerWidth) continue;
    const area = r.width * r.height;
    if (!best || area < best.area)
      best = {x: r.left + r.width/2, y: r.top + r.height/2, area, text: t.slice(0, 50)};
  }
  if (!best) return null;
  return {x: best.x, y: best.y, text: best.text,
          sx: window.screenX, sy: window.screenY,
          cw: window.outerWidth - window.innerWidth,
          ch: window.outerHeight - window.innerHeight};
})()
"""


def _http_json(path, timeout=3):
    with urllib.request.urlopen(CDP_HTTP + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _chrome_exe():
    import os
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(p):
            return p
    return "chrome.exe"


def browser_up():
    try:
        _http_json("/json/version")
        return True
    except Exception:
        return False


def ensure_browser():
    """没有调试浏览器就用项目自己的 .chrome-space 配置起一个（不动用户日常浏览器）。
    起完默认藏到隔离虚拟桌面（用户拍板：平时严格隔离，要看时说一声才展示）。"""
    if browser_up():
        return True
    import os
    profile = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           ".chrome-space")
    subprocess.Popen([_chrome_exe(), f"--remote-debugging-port={CDP_PORT}",
                      f"--user-data-dir={profile}", "--no-first-run",
                      "--disable-session-crashed-bubble",
                      "--hide-crash-restore-bubble", "about:blank"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        time.sleep(0.5)
        if browser_up():
            break
    else:
        return False
    try:
        import vd_isolate
        for _ in range(10):     # 等顶层窗口创建出来再挪
            if vd_isolate.hide_browser(log=lambda m: None):
                break
            time.sleep(0.5)
    except Exception:
        pass  # 隔离失败不阻塞自动化（最坏情况：窗口留在用户桌面）
    return True


async def _eval(tab_ws, expr, timeout=8):
    import aiohttp
    async with aiohttp.ClientSession() as sess:
        async with sess.ws_connect(tab_ws, max_msg_size=0) as ws:
            await ws.send_json({"id": 1, "method": "Runtime.evaluate",
                                "params": {"expression": expr,
                                           "returnByValue": True}})
            async for m in ws:
                if m.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(m.data)
                    if data.get("id") == 1:
                        return data.get("result", {}).get("result", {}).get("value")
                elif m.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
    return None


def _active_tab():
    for t in _http_json("/json"):
        if t.get("type") == "page" and t.get("url", "").startswith(("http", "about")):
            return t
    return None


def open_url(url):
    """新标签页打开 url，返回是否成功。"""
    if not ensure_browser():
        return False
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        req = urllib.request.Request(CDP_HTTP + "/json/new?" + urllib.parse.quote(url, safe=""),
                                     method="PUT")
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:
        # 老版本 CDP 用 GET
        try:
            _http_json("/json/new?" + urllib.parse.quote(url, safe=""))
            return True
        except Exception:
            return False


def find_text(text, timeout=3.0):
    """当前标签页内按文字找元素，返回物理屏幕坐标 (x, y) 或 None。"""
    if not browser_up():
        return None
    deadline = time.time() + timeout
    expr = _FIND_JS % json.dumps(text, ensure_ascii=False)
    while time.time() < deadline:
        try:
            tab = _active_tab()
            if tab:
                v = asyncio.run(_eval(tab["webSocketDebuggerUrl"], expr))
                if v:
                    # 视口坐标 → 屏幕物理像素（sx/sy=窗口屏幕原点，ch=标题栏高，cw≈0）
                    x = v["sx"] + v["cw"] // 2 + v["x"]
                    y = v["sy"] + v["ch"] + v["y"]
                    return int(x), int(y)
        except Exception:
            pass
        time.sleep(0.3)
    return None


def click_text(text):
    """找元素并返回坐标（点击动作由 host_input 真鼠标执行）。返回 (x,y) 或 None。"""
    return find_text(text)


def eval_in_tab(url_part, expr, timeout=15):
    """在 URL 含 url_part 的标签页里执行 JS（CDP Runtime.evaluate）。"""
    if not ensure_browser():
        return None
    try:
        for t in _http_json("/json"):
            if t.get("type") == "page" and url_part in t.get("url", ""):
                return asyncio.run(_eval(t["webSocketDebuggerUrl"], expr, timeout))
    except Exception:
        return None
    return None


def current_url():
    try:
        tab = _active_tab()
        return tab.get("url", "") if tab else ""
    except Exception:
        return ""


if __name__ == "__main__":
    import sys
    print("browser up:", browser_up())
    if len(sys.argv) > 1 and sys.argv[1] == "open":
        print("open_url:", open_url(sys.argv[2] if len(sys.argv) > 2 else "bilibili.com"))
    elif len(sys.argv) > 1:
        print("find", sys.argv[1], "->", find_text(sys.argv[1], timeout=5))
