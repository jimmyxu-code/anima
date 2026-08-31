# -*- coding: utf-8 -*-
"""任务链路仿真（2026-08-26 用户令：自己跑仿真循环，没问题了再停）。

覆盖（全部断言化，不问模型、不占屏、不注键鼠）：
  1. 开/关指令解析：正则覆盖面（名字在后/在前/别名/口语尾巴）
  2. 开始菜单 .lnk 解析：真机扫描（自洽：扫到的名字必能再找到）
  3. 窗口枚举：真机（可见+有标题+进程名小写）
  4. 关闭快路：mock 窗口表 + mock WM_CLOSE——关掉/未保存弹窗/没开着三分支
  5. 暂存结果保鲜期：过期不报、新鲜报、超上限丢最旧
  6. 前台 GUI 串行：两线程并发进锁，任意时刻在锁内 ≤1
  7. 端到端干跑：run_task("打开X") 全链（Popen mock，不真开窗口）
从项目根运行：python tools/task_sim.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, r"<项目根>")

FAILS = []


def check(name, cond, detail=""):
    print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        FAILS.append(name)


def sim_parsing():
    from plugins import exec_native as ex
    opens = ["打开记事本", "帮我打开微信", "打开 bilibili 软件",
             "快速打开谷歌浏览器", "开个终端吧", "打开网易云音乐"]
    for t in opens:
        m = ex._LAUNCH_RE.match(t)
        check(f"开指令解析: {t}", bool(m), m.group("name") if m else "未命中")
    closes = [("关闭微信", "微信"), ("帮我关掉B站", "B站"),
              ("把浏览器关了", "浏览器"), ("退出记事本", "记事本"),
              ("关一下网易云音乐", "网易云音乐")]
    for t, want in closes:
        m = ex._CLOSE_RE.match(t) or ex._CLOSE_RE2.match(t)
        check(f"关指令解析: {t}", bool(m) and m.group("name") == want,
              m.group("name") if m else "未命中")
    # 非开/关句不得在【产品层】误启动：正则可能命中"打开思路"，但解析不到
    # 任何已装软件必须返回 None（回落模型循环），绝不能瞎启动。
    check("抽象宾语不瞎启动: 打开思路说一说",
          ex._try_fast_launch("打开思路说一说", print) is None)
    for t in ["把文件重命名为报告", "关闭思考，直接回答"]:
        check(f"不得误命中: {t}",
              not (ex._LAUNCH_RE.match(t) or ex._CLOSE_RE.match(t)
                   or ex._CLOSE_RE2.match(t)))
    # 危险类关闭不得走快路（留给确认链）——仿真实锤：会撞上标题带"电脑"的窗口
    for t in ["关闭电脑", "关闭系统", "把电脑关了"]:
        check(f"关电脑类不进快路: {t}",
              ex._try_fast_close(t, print) is None)


def sim_startmenu():
    from plugins import exec_native as ex
    items = ex._startmenu_lnks()
    check("开始菜单扫描非空（已装软件可发现）", len(items) > 10,
          f"{len(items)} 条")
    if items:
        base, path = items[0]
        check("自洽：扫到的名字必能再解析到", ex._find_startmenu_lnk(base) == path,
              f"{base} → {os.path.basename(path)}")
    check("乱名解析返回 None", ex._find_startmenu_lnk("不存在的软件xyzq") is None)


def sim_enum_windows():
    from plugins import exec_native as ex
    wins = ex._enum_windows_with_proc()
    if not wins:
        # CI/受限终端可能没有挂到交互桌面；这不是枚举逻辑失败。字段和关闭
        # 行为由下方全 mock 的 sim_close_fast 覆盖，真人桌面上仍做 live smoke。
        print("[SKIP] 窗口枚举 live smoke（当前进程无交互桌面）")
    if wins:
        check("窗口枚举非空", True, f"{len(wins)} 个可见窗口")
        hwnd, title, proc = wins[0]
        check("窗口字段合法", hwnd > 0 and isinstance(title, str)
              and proc == proc.lower(), f"proc={proc} title={title[:16]}")


def sim_close_fast():
    from plugins import exec_native as ex
    orig_enum, orig_post, orig_gone = (ex._enum_windows_with_proc,
                                       ex._post_wm_close, ex._win_gone)
    try:
        ex._enum_windows_with_proc = lambda: [(4321, "微信 (工作)", "weixin.exe")]
        sent = []

        def fake_post(hwnd):
            sent.append(hwnd)
            return True

        ex._post_wm_close = fake_post
        # 分支一：关掉了
        ex._win_gone = lambda hwnd: True
        r = ex._try_fast_close("关闭微信", print)
        check("关闭分支：已关", r and r.status == "completed"
              and "已关闭" in r.value and sent == [4321], r.value if r else "None")
        # 分支二：发过指令但窗口还在（未保存弹窗）
        ex._win_gone = lambda hwnd: False
        r = ex._try_fast_close("帮我关掉微信", print)
        check("关闭分支：等用户确认保存", r and "未保存" in r.value,
              r.value if r else "None")
        # 分支三：目标没开着
        ex._enum_windows_with_proc = lambda: []
        r = ex._try_fast_close("退出记事本", print)
        check("关闭分支：本来就没开", r and "本来就没开着" in r.value,
              r.value if r else "None")
    finally:
        ex._enum_windows_with_proc = orig_enum
        ex._post_wm_close = orig_post
        ex._win_gone = orig_gone


def sim_pending_ttl():
    """暂存结果保鲜期 + 唤醒不主动播报（2026-08-30 用户令：唤醒=刷新，
    旧进度留档但不许自嗨上嘴；用户要问自会问，task_status 有真账）。"""
    import companion_rt as cr
    injected = []
    logged = []
    orig_inject = cr._inject_rag
    orig_log = cr.task_overlay.log_line
    cr._inject_rag = lambda tag, text: injected.append((tag, text))
    cr.task_overlay.log_line = lambda t: logged.append(t)
    try:
        cr._pending_reports[:] = []
        # 旧的（40 分钟前）+ 新的（1 分钟前）→ 只留档新的，都零播报
        cr._pending_reports.append((time.time() - 2400, "任务「旧的」完成了"))
        cr._pending_reports.append((time.time() - 60, "任务「新的」完成了"))
        cr._flush_pending_reports()
        check("唤醒零播报（自嗨根治）", injected == [])
        check("新鲜结果留档面板", any("新的" in l for l in logged)
              and not any("旧的" in l for l in logged))
        check("清队", cr._pending_reports == [])
        # 全部过期 → 零留档零注入
        cr._pending_reports.append((time.time() - 3600, "任务「远古」没完成"))
        logged.clear()
        cr._flush_pending_reports()
        check("全过期=零留档零注入", injected == [] and logged == [])
    finally:
        cr._inject_rag = orig_inject
        cr.task_overlay.log_line = orig_log


def sim_gui_serialize():
    import companion_rt as cr
    overlaps = []
    active = {"n": 0}
    lock = cr._gui_exec_lock

    def worker(tag, hold):
        with lock:
            active["n"] += 1
            if active["n"] > 1:
                overlaps.append(tag)
            time.sleep(hold)
            active["n"] -= 1

    ts = [threading.Thread(target=worker, args=(f"T{i}", 0.15 + i * 0.03))
          for i in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("前台 GUI 三并发串行（任意时刻 ≤1 在锁内）", not overlaps,
          f"overlap={overlaps}")


def sim_e2e_launch_dry():
    from plugins import exec_native as ex
    launched = []
    orig_popen = ex.subprocess.Popen
    ex.subprocess.Popen = lambda cmd, **kw: (launched.append(cmd), True)[1]
    try:
        r = ex.run_task("打开记事本", log=lambda m: None)
        check("E2E 干跑：打开记事本走快路",
              r.status == "completed" and launched
              and launched[0][4] == "notepad", launched[0][4] if launched else "None")
        # 无桌面快捷方式的路：用真机开始菜单里实际存在的名字
        items = ex._startmenu_lnks()
        if items:
            base, path = items[0]
            launched.clear()
            r = ex.run_task(f"打开{base}", log=lambda m: None)
            check(f"E2E 干跑：打开{base[:12]} 走 .lnk 快路",
                  r.status == "completed" and launched
                  and launched[0][4].lower().endswith(".lnk"),
                  os.path.basename(launched[0][4]) if launched else "None")
    finally:
        ex.subprocess.Popen = orig_popen


def sim_persona_scrub():
    import persona_text as pt
    cases = [
        "卡住了，模型那边返回了不认识的指令，没操作成",
        "模型报告了结果，但没有提供可验证的完成证据",
        "执行器说做完了，但屏幕核验没通过：provider 返回未知动作 'launch_app'",
        "gui_agent 步骤超时，FC 工具调用失败",
        "搞定了，屏幕已经回到桌面了",   # 干净话必须原样保留
    ]
    for t in cases:
        out = pt.scrub(t)
        check(f"净化后无人话禁区词: {t[:18]}…", pt.persona_clean(out), out[:50])
    out = pt.scrub("模型那边返回了不认识的指令")
    check("洗后可读（含'我这边'）", "我这边" in out, out)


def sim_launch_routing():
    from plugins import exec_native as ex
    import gui_agent as g
    launched = []
    orig_lnf = ex.launch_app_by_name

    def fake_launch(name, log=print, verify_window=True):
        launched.append(name)
        if name == "不存在的软件xyz":
            return None
        return ex.Result("completed", f"已启动 {name}", set())

    ex.launch_app_by_name = fake_launch
    try:
        history = []
        actions = [{"action": "launch_app", "name": "哔哩哔哩",
                    "intent": "启动B站客户端"},
                   {"action": "click", "position": (500, 500), "intent": "点图标"}]
        rest, n = g._route_exec_actions(actions, history, print)
        check("launch_app 被路由（1 启动）", n == 1 and launched == ["哔哩哔哩"])
        check("gui 动作原样保留", [a["action"] for a in rest] == ["click"])
        check("历史记了启动结果", any("已启动" in h[1] for h in history))
        rest, n = g._route_exec_actions(
            [{"action": "launch_app", "name": "不存在的软件xyz", "intent": "x"}],
            history, print)
        check("找不到的应用不计数、动作清空", n == 0 and rest == [])
    finally:
        ex.launch_app_by_name = orig_lnf


def sim_yolo_send_and_dedup():
    """最大自主档明确发送不确认；真正点击发送的动作可被 exactly-once 识别，
    登录/打开阶段仅提到“后续发送”不能误记成已经发送。"""
    import gui_agent as g
    check("yolo 普通发送不属于不可逆确认面",
          not g._IRREVERSIBLE_INTENT_RE.search("给文件传输助手发送消息你好"))
    check("yolo 删除仍属于不可逆确认面",
          bool(g._IRREVERSIBLE_INTENT_RE.search("删除这个文件")))
    check("发送按钮识别为外部触达",
          g._is_external_send_action({"action": "uia_click", "text": "发送",
                                      "intent": "点击发送按钮"}))
    check("登录步骤提到后续发送不算已发送",
          not g._is_external_send_action({
              "action": "uia_click", "text": "登录",
              "intent": "登录微信，后续找到文件传输助手发送消息"}))
    send = {"action": "uia_click", "text": "发送",
            "intent": "给文件传输助手发送消息你好"}
    delete = {"action": "uia_click", "text": "删除",
              "intent": "删除这个文件"}
    check("yolo 权限过滤实际放行发送",
          g._confirmation_actions(
              [send], "yolo", set(), task_text="给文件传输助手发送消息你好",
              log=lambda m: None) == [])
    check("yolo 不放行任务外自行扩出的发送",
          g._confirmation_actions(
              [send], "yolo", set(), task_text="点击右下角红色按钮",
              log=lambda m: None) == [send])
    check("yolo 权限过滤实际保留删除确认",
          g._confirmation_actions(
              [delete], "yolo", set(), task_text="删除这个文件",
              log=lambda m: None) == [delete])
    kept, repeated = g._dedupe_external_send_actions([send], {"external-send"})
    check("同一任务第二次发送被机械拦下", kept == [] and repeated)
    kept, repeated = g._dedupe_external_send_actions([send], set())
    check("同一任务第一次发送正常放行", kept == [send] and not repeated)
    src = open(g.__file__, encoding="utf-8").read()
    check("launch_app 成功被纳入完成证据", "last_action_evidence or launch_evidence" in src)


def sim_no_closeout_on_verified():
    """回归哨（2026-08-27 延迟投诉 + 2026-08-30 升级）：已验证的 GUI 完成不许
    再套视觉收尾；implicit 后台任务根本不截屏看前台（装驱动/改系统类后台
    改动前台屏幕本就无变化，截屏核验把真做成的报成"没做成"）——companion_rt
    内不允许任何 _visual_closeout（视觉核验只属 gui_agent 事件校验证据闸）。"""
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "companion_rt.py"), encoding="utf-8").read()
    bad = 'detail=msg + _visual_closeout'
    check("已验证完成路径无视觉收尾（立即说话）", bad not in src)
    check("后台任务无机械截屏核验（执行器状态即完成证据）",
          "_visual_closeout" not in src)


def sim_fast_result_instant():
    """快路结果自证（tools 空）→ companion 视为纯回答，立即播报。
    ⚠️ 防真闸（2026-08-27 事故教训：本测试曾未 mock 窗口枚举，真把用户
    开着的记事本关了）——凡能触发真实副作用的路径，一律先 mock 到底。"""
    from plugins import exec_native as ex
    orig_popen = ex.subprocess.Popen
    orig_enum, orig_post, orig_gone = (ex._enum_windows_with_proc,
                                       ex._post_wm_close, ex._win_gone)
    ex._enum_windows_with_proc = lambda: []   # 假桌面：什么都没开
    ex._post_wm_close = lambda hwnd: True
    ex._win_gone = lambda hwnd: True
    ex.subprocess.Popen = lambda cmd, **kw: True
    try:
        r = ex._try_fast_launch("打开记事本", print)
        check("快路启动 Result.tools 为空（=已验证，不等视觉）",
              r is not None and r.tools == set(), str(r.tools))
        r = ex._try_fast_close("关闭记事本", print)
        check("快路关闭 Result.tools 为空",
              r is not None and r.tools == set(), str(r.tools))
    finally:
        ex.subprocess.Popen = orig_popen
        ex._enum_windows_with_proc = orig_enum
        ex._post_wm_close = orig_post
        ex._win_gone = orig_gone


def sim_close_all():
    from plugins import exec_native as ex
    orig_enum, orig_post = ex._enum_windows_with_proc, ex._post_wm_close
    posted = []
    ex._enum_windows_with_proc = lambda: [
        (101, "程序管理器", "progman.exe"),          # 系统桌面壳：跳过
        (102, "ZCode", "zcode.exe"),                  # 宿主工具：跳过
        (103, "pythonw 后台", "pythonw.exe"),         # 自家进程：跳过
        (201, "微信", "weixin.exe"),                  # 关
        (202, "bilibili - 首页", "bilibili.exe"),     # 关
        (203, "校园策划书.md", "notepad.exe")]        # 关
    ex._post_wm_close = lambda hwnd: (posted.append(hwnd), True)[1]
    try:
        n = ex.close_all_windows(log=lambda m: None)
        check("批量关：只关应用窗口（3 个），系统/自家跳过", n == 3, f"n={n}")
        check("批量关：跳过名单确实没被发指令",
              posted == [201, 202, 203], str(posted))
        check("批量关正则：整句命中（两种语序）",
              ex.match_close_all("关闭所有的页面")
              and ex.match_close_all("把所有窗口都关了")
              and ex.match_close_all("关掉全部应用"))
        check("批量关正则：单窗指令不误吃",
              not ex.match_close_all("关闭微信"))
    finally:
        ex._enum_windows_with_proc = orig_enum
        ex._post_wm_close = orig_post


def sim_voice_fast_lane():
    import companion_rt as cr
    from plugins import exec_native as ex
    launched, spoken = [], []
    orig_launch, orig_close = ex._try_fast_launch, ex._try_fast_close
    orig_speak = cr._speak_fast
    ex._try_fast_launch = lambda t, log=None: (
        launched.append(t), ex.Result("completed", f"已启动 X", set()))[1] \
        if t == "打开微信" else None
    ex._try_fast_close = lambda t, log=None: None
    cr._speak_fast = lambda text: spoken.append(text)
    try:
        cr._fast_done.clear()
        check("快车道吃整句开指令", cr._try_voice_fast_lane("打开微信") is True
              and spoken and "已启动" in spoken[-1])
        check("快车道记了签（重复派活可拦）",
              cr._fast_dedup("打开微信") is True)
        check("复合指令不吃快车道",
              cr._try_voice_fast_lane("打开微信然后给妈妈发消息") is False)
        check("抽象目标回落路由",
              cr._try_voice_fast_lane("打开思路说一说") is False)
        # FC 派活对快车道结果的去重（缩进事故回归：enqueue 不得是死代码）
        ret = cr._fc_dispatch_task("打开微信")
        check("FC 对快车道重复派活回执'不重复派'", "没有重复派" in ret, ret[:30])
        ret2 = cr._fc_dispatch_task("")   # 空文本护栏
        check("FC 空任务回执让用户重说", "再讲" in ret2)
    finally:
        ex._try_fast_launch = orig_launch
        ex._try_fast_close = orig_close
        cr._speak_fast = orig_speak
        cr._fast_done.clear()


def sim_fc_enqueue_alive():
    """回归哨：_fc_dispatch_task 正常路径必须真的 enqueue（2026-08-27
    缩进事故——enqueue 两行在 return 后=死代码，FC 派活空转了一整天）。"""
    import companion_rt as cr
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "companion_rt.py"), encoding="utf-8").read()
    fn = src.split("def _fc_dispatch_task")[1].split("def _fc_task_status")[0]
    # 死代码形态：return 之后、函数尾之前没有可达的 _enqueue_task 调用
    check("FC 派活 enqueue 可达（非死代码）",
          "_enqueue_task(task_text, orig, mode)" in fn
          and fn.index("_enqueue_task(task_text, orig, mode)")
          < fn.index('return ("任务已派给执行层'))


def sim_form_page():
    p = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "workspace", "表单实战", "报名表.html")
    check("表单实战页存在", os.path.exists(p), p)
    if os.path.exists(p):
        s = open(p, encoding="utf-8").read()
        for field in ("姓名", "手机号", "电子邮箱", "所在城市", "公司", "职位",
                      "参展项目名称", "紧急联系人", "饮食忌口", "尺码", "提交报名"):
            check(f"表单字段齐: {field}", field in s)
        from plugins import exec_native as ex
        # 报名表别名已随开源化移除（指向本机 workspace 私有路径，2026-08-31）；
        # 该文件仍由模糊文件搜索覆盖（sim_fuzzy_file 盯着）。


def sim_fuzzy_file():
    from plugins import exec_native as ex
    hits = ex.find_file("报名表", log=lambda m: None, limit=3)
    check("模糊搜'报名表'命中报名表.html",
          any(h.lower().endswith("报名表.html") for h in hits),
          "; ".join(os.path.basename(h) for h in hits))
    check("模糊搜乱名返回空", ex.find_file("不存在xyzq123", log=lambda m: None) == [])
    # 路由脑补容错：整句带修饰也能抽出真名直开
    ex._FILE_INDEX["ts"] = 0.0   # 强制重建，模拟首次
    launched = []
    orig_popen = ex.subprocess.Popen
    ex.subprocess.Popen = lambda cmd, **kw: (launched.append(cmd), True)[1]
    try:
        r = ex._try_open_by_fuzzy(
            "打开桌面上的报名表文件（可能是Excel或Word格式），找到并打开它",
            log=lambda m: None)
        check("脑补话术抽出真名并直开", r is not None
              and r.status == "completed" and launched
              and launched[0][4].endswith("报名表.html"),
              launched[0][4] if launched else "None")
        check("抽象句不瞎开", ex._try_open_by_fuzzy("打开思路说一说",
                                                    log=lambda m: None) is None)
    finally:
        ex.subprocess.Popen = orig_popen


def sim_vocative_strip():
    import companion_rt as cr
    check("剥'小可爱'前缀", cr._strip_vocative("小可爱打开报名表") == "打开报名表")
    check("剥'帮我'前缀", cr._strip_vocative("帮我打开微信") == "打开微信")
    check("否定前缀不剥（别打开≠打开）",
          cr._strip_vocative("别打开微信") == "别打开微信")
    check("无前缀原样", cr._strip_vocative("打开记事本") == "打开记事本")


def sim_auto_fallback():
    import companion_rt as cr
    calls = []
    orig_enq = cr._enqueue_task
    cr._enqueue_task = lambda t, o, m, force=False: (
        calls.append((t, m, force)), True)[1]
    try:
        cr._mode_attempts.clear()
        ok = cr._auto_fallback("整理下载目录", "原话", "implicit")
        check("后台失败→自动换前台（静默重跑）",
              ok and calls == [("整理下载目录", "explicit", True)],
              str(calls))
        cr._note_mode_attempt("整理下载目录", "explicit")
        ok2 = cr._auto_fallback("整理下载目录", "原话", "implicit")
        check("另一条路也试过→不再换路（防乒乓）", ok2 is False and len(calls) == 1)
    finally:
        cr._enqueue_task = orig_enq
        cr._mode_attempts.clear()


def sim_template_no_asks():
    """人格+废话双重 lint：失败路径模板不许出现任何问句式求助
    （2026-08-27 用户令：'继续还是不弄'是废话，轮不到问）。"""
    from plugins import expression as ep
    banned = ("要不要", "继续还是", "再做一次吗", "试试？", "继续吗")
    for kind, (hint, _label) in ep._TASK_EVENTS.items():
        if kind == "paused_takeover":   # 已死路径（接管不再暂停），留着仅存档
            continue
        hits = [b for b in banned if b in hint]
        check(f"模板无废话问句: {kind}", not hits, str(hits))


def sim_no_console_flash():
    """闪窗根治 lint（2026-08-27 用户令：无感是原则）：exec_native 与
    companion 里所有 powershell/cmd 子进程调用必须带 CREATE_NO_WINDOW
    （或 _NO_WIN 别名）——模型循环一次弹十几个终端闪窗是铁律违规。"""
    import re as _re
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ("plugins/exec_native.py", "companion_rt.py"):
        src = open(os.path.join(base, rel), encoding="utf-8").read()
        # 逐个 subprocess.run/Popen(["powershell"|"cmd" 调用块（跨到收尾括号）
        for m in _re.finditer(
                r"subprocess\.(?:run|Popen)\(\[\"(?:powershell|cmd)", src):
            start = m.start()
            window = src[start:start + 700]
            check(f"{rel} 子进程无窗（偏移 {start}）",
                  "CREATE_NO_WINDOW" in window or "_NO_WIN" in window)


def sim_speak_immediate():
    """回归哨：_speak_fast 不得再等 _tts_busy（等模型把受理废话说完才出
    结果=速度慢+用户重复说第二遍的主因）。"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(base, "companion_rt.py"), encoding="utf-8").read()
    body = src.split("def _speak_fast")[1].split("\ndef ")[0]
    check("_speak_fast 立即出声（无 busy 等待）", "_tts_busy" not in body)
    worker = src.split('if mode == "explicit":')[1].split('if mode ==')[0]
    check("新前台任务取代旧循环（request_stop 先于锁）",
          "request_stop" in worker and "_gui_exec_lock" in worker)


def sim_progress_stuck_silent():
    from plugins import expression as ep
    hint = ep._TASK_EVENTS["progress_stuck"][0]
    check("屏幕无变化不再出声（内化）", hint == "", repr(hint[:30]))
    check("面板行仍在（详细上框）", "没有变化" in ep._TASK_EVENTS["progress_stuck"][1])


def sim_panel_reset():
    import task_overlay as t
    t.log_line("旧任务的行")
    t.work_begin("old", "旧活动", grace=0)
    t._ensure_thread()
    t.show_task("新任务标题")
    check("新任务翻新页：steps 清空", t._state["steps"] == [])
    check("新任务翻新页：activities 清空", t._state["activities"] == {})
    check("新任务标题就位", t._state["title"] == "新任务标题")
    check("面板帧率提到 20", t._FPS == 20)


def sim_launch_async_verify():
    from plugins import exec_native as ex
    launched = []
    orig_popen, orig_enum = ex.subprocess.Popen, ex._enum_windows_with_proc
    ex.subprocess.Popen = lambda cmd, **kw: (launched.append(cmd), True)[1]

    def slow_enum():   # 模拟窗口核验很慢——不得拖住启动返回
        time.sleep(1.0)
        return []
    ex._enum_windows_with_proc = slow_enum
    try:
        t0 = time.time()
        r = ex.launch_app_by_name("记事本", log=lambda m: None)
        dt = time.time() - t0
        check("启动即返回（核验异步，<0.4s）", r is not None and dt < 0.4,
              f"dt={dt:.2f}s")
        check("播报词不带核验尾巴（速度优先）",
              r.value == "已启动 记事本", r.value)
    finally:
        ex.subprocess.Popen = orig_popen
        ex._enum_windows_with_proc = orig_enum


def sim_repeat_guard():
    import companion_rt as cr
    spoken = []
    orig_speak = cr._speak_fast
    cr._speak_fast = lambda t: spoken.append(t)
    # _try_fast_launch 不该被调到（用会抛错的替身验证）
    from plugins import exec_native as ex
    orig_launch = ex._try_fast_launch
    ex._try_fast_launch = lambda t, log=None: (_ for _ in ()).throw(
        AssertionError("重复指令不应再执行"))
    try:
        cr._fast_done.clear()
        cr._fast_done[cr._task_sig("打开微信")] = time.time() - 2
        ok = cr._try_voice_fast_lane("打开微信")
        check("10s 内重复指令：回'刚做过'且不再执行",
              ok is True and spoken == ["刚已经做过了"], str(spoken))
    finally:
        cr._speak_fast = orig_speak
        ex._try_fast_launch = orig_launch
        cr._fast_done.clear()


def sim_gui_ears():
    """GUI agent 的耳朵（2026-08-29 移植自封存包并修正死代码）：语音纠正注入/
    点击坐标注入/假进展换路/混批拆分；companion_rt 转告接线。源码哨+真行为断言。"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ga = open(os.path.join(base, "gui_agent.py"), encoding="utf-8").read()
    gb = open(os.path.join(base, "gui_brain.py"), encoding="utf-8").read()
    rt = open(os.path.join(base, "companion_rt.py"), encoding="utf-8").read()
    check("语音纠正通道在位（push_user_hint+消费）",
          "def push_user_hint" in ga and "用户刚才说" in ga)
    check("点击坐标注入在位（不与用户抢点）",
          "last_real_click" in ga and "不要和他抢点" in ga)
    check("假进展换路注入在位", "这条路已证明不通" in ga)
    check("混批拆分在位（不再整批报废）",
          "ProviderActionRoutingRequired" in gb and "已剥离" in gb)
    check("GUI 纠正接线在位（companion_rt 转告，叫停不拦）",
          "push_user_hint(text)" in rt and "已转告前台任务" in rt
          and "not _is_stop(text)" in rt)
    check("纠正回执不硬编码（罐头'收到'已退役）",
          '_speak_fast("收到")' not in rt)

    import gui_agent as g
    # 行为：纠正队列进出
    g.push_user_hint("别点那个")
    g.push_user_hint("往左一点")
    hints = g._drain_user_hints()
    check("纠正队列 drain 出全部且清空",
          hints == ["别点那个", "往左一点"] and g._drain_user_hints() == [])
    # 行为：真实点击坐标（move 不算，取最近非 move 事件）
    w = g.TakeoverWatcher()
    w._record("move", 1, 1)
    w._record("click", 500, 300)
    w._record("move", 9, 9)
    lc = w.last_real_click()
    check("last_real_click 取最近非移动事件坐标",
          lc is not None and lc[0] == 500 and lc[1] == 300 and lc[2] < 2)
    check("空记录返回 None", g.TakeoverWatcher().last_real_click() is None)
    # 行为：假进展签名判定（修正后的真实现，封存版比对永不命中）
    from collections import deque as _dq
    win = _dq(maxlen=3)
    for sig, ch in [("click:关菜单设默认", False), ("click:关闭菜单设默认", False),
                    ("click:关掉菜单并设为默认", False)]:
        win.append((sig, ch))
    check("3 轮相似意图+零变化=死循环", g._similar_stall(win))
    win.clear()
    for sig, ch in [("click:关菜单设默认", False), ("click:关闭菜单设默认", True),
                    ("click:关掉菜单并设为默认", False)]:
        win.append((sig, ch))
    check("其中有真变化不算死循环", not g._similar_stall(win))
    win.clear()
    for sig, ch in [("click:点保存", False), ("type:输入账号", False),
                    ("scroll:向下滚动", False)]:
        win.append((sig, ch))
    check("不同动作不算死循环", not g._similar_stall(win))


def sim_uia_grounding():
    """路线A 语义接地（2026-08-29）：UIA 控件清单喂模型、按名点击优先、
    语义停滞 2 击换路。源码哨 + 纯函数行为断言。"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ut = open(os.path.join(base, "uia_tools.py"), encoding="utf-8").read()
    ga = open(os.path.join(base, "gui_agent.py"), encoding="utf-8").read()
    sa = open(os.path.join(base, "gui_providers", "seed_ark.py"),
              encoding="utf-8").read()
    check("dump_interactive 在位（OrCondition 提速）",
          "def dump_interactive" in ut and "CreateOrCondition" in ut)
    check("界面控件注入在位（槽位替换防膨胀）",
          'h[0] != "界面控件"' in ga and "dump_interactive()" in ga)
    check("提示词教按名点击优先", "精准落点" in sa)
    check("语义停滞检测在位", "_uia_stall" in ga and "两击无效" in ga)
    check("挂死检测+止损在位", "_foreground_hung" in ga and "系统判定无响应" in ga)
    check("换路提示带牙齿（2 次升级止损）", "原地打转" in ga)
    check("步数压力注入在位", "步了还没完成" in ga)

    import uia_tools, gui_agent as g
    # 行为：清单渲染——排序/去重/空名丢弃/上限余量
    raw = [("按钮", "确定", 100, 200, 80, 30),
           ("输入框", "搜索", 50, 100, 200, 28),
           ("按钮", "确定", 100, 200, 80, 30),
           ("按钮", "", 0, 0, 10, 10),
           ("链接", "更多", 10, 150, 60, 20)]
    out = uia_tools._render_dump(raw)
    check("渲染：阅读顺序+去重+空名丢弃",
          out.index("输入框「搜索」@(150,114)") < out.index("链接「更多」@(40,160)")
          < out.index("按钮「确定」@(140,215)") and out.count("确定") == 1)
    check("渲染：头部教按名点击", "uia_click" in out)
    many = [("按钮", f"b{i}", 0, i * 10, 10, 10) for i in range(45)]
    out2 = uia_tools._render_dump(many)
    check("渲染：40 截断+余量注明",
          out2.count("按钮「") == 40 and "另有 5 个未列出" in out2)
    check("渲染：空列表回 None（纯视觉兜底）",
          uia_tools._render_dump([]) is None)
    # 行为：具名目标 key 与 2 击停滞判定
    key = g._uia_key_of([{"action": "uia_click", "text": " 添加语言 "},
                         {"action": "click", "point": (1, 2)}])
    check("具名 key 归一化（去空白小写）", key == ("添加语言",))
    check("非具名动作为空 key",
          g._uia_key_of([{"action": "click", "point": (1, 2)}]) == ())
    check("同名两击零变化=停滞",
          g._uia_stall((("添加语言",), False), ("添加语言",), False) is True)
    check("其中有变化不算停滞",
          g._uia_stall((("添加语言",), False), ("添加语言",), True) is False)
    check("首批无上批不算停滞",
          g._uia_stall(None, ("添加语言",), False) is False)
    check("空 key 不算停滞",
          g._uia_stall((("x",), False), (), False) is False)
    # 行为：uia text 清洗（模型照抄格式实锤：'下拉框「Windows 显示语言」'整个塞入）
    check("清洗：类型词+括号剥离",
          g._clean_uia_text("下拉框「Windows 显示语言」") == "Windows 显示语言")
    check("清洗：坐标尾巴剥离",
          g._clean_uia_text("按钮「确定」@(140,215)") == "确定")
    check("清洗：纯名字不动", g._clean_uia_text("确定") == "确定")
    # 行为：同点聚类（物理层死磕检测）
    check("同点聚类=死磕",
          g._same_spot([(100, 100), (110, 105), (95, 98), (102, 103)]) is True)
    check("散开不算",
          g._same_spot([(100, 100), (500, 500), (900, 100), (100, 900)]) is False)
    check("点数不够不算", g._same_spot([(100, 100), (101, 101)]) is False)


def _bind_scaffold():
    """仿真装配 scaffold 插件（与 main() 同口径；返回 companion_rt 的调用口）。"""
    from plugins import scaffold as sc
    import companion_rt as rt
    rt._kernel.load("scaffold", sc)
    rt._ksvc("scaffold")["bind"](lambda k, d=None: d)   # 默认全开
    return rt


def sim_realtime_guard():
    """实时信息护栏（scaffold 临时件）：纯实时问句永不派活；
    沾电脑动作边的放行给模型判；开关关掉=退役（模型单跑）。"""
    rt = _bind_scaffold()
    def guarded(t):
        return rt._scaffold_call("realtime_guard", t)
    for t in ["查一下现在几点了", "今天天气怎么样", "今天有什么新闻",
              "现在美元汇率多少"]:
        check(f"纯实时问句进护栏: {t}", guarded(t))
    for t in ["把今天的天气写进记事本", "看看我电脑还剩多少磁盘空间",
              "帮我打开浏览器搜一下附近的火锅店", "五分钟后提醒我喝水"]:
        check(f"沾电脑/非实时不进护栏: {t[:14]}", not guarded(t))
    rt._ksvc("scaffold")["bind"](
        lambda k, d=None: False if k == "scaffold_realtime_guard" else d)
    check("scaffold 关掉=护栏退役（热插拔模型单跑）",
          not guarded("现在几点了"))
    _bind_scaffold()   # 复原默认开


def sim_elastic_workers():
    """弹性工人（2026-08-30 用户令：子进程/子智能体数量不是固定的 3）——
    按需派生、队深/负载定上限、闲退归还计数；子智能体保险丝负载敏感。"""
    import companion_rt as rt
    import queue as _q
    old_q, old_high = rt._task_q, rt._load["high"]
    try:
        rt._task_q = _q.Queue()
        rt._load["high"] = False
        check("空队上限=2", rt._max_workers() == 2)
        rt._task_q.put("a")
        rt._task_q.put("b")
        check("队深 2 上限=4", rt._max_workers() == 4)
        rt._task_q.put("c")
        check("队深 ≥3 上限=5", rt._max_workers() == 5)
        rt._load["high"] = True
        check("高负载收缩到下界 2", rt._max_workers() == 2)
    finally:
        rt._task_q = old_q
        rt._load["high"] = old_high
    # 闲退归还计数（空队 5s 自退，计数必须平衡——漏计数=工人越派越少到停摆）
    rt._WORKERS["active"] = 1
    rt._task_worker_guarded()
    check("闲退归还计数", rt._WORKERS["active"] == 0)
    # 子智能体保险丝：有界且负载敏感
    from plugins import exec_native as ex
    n = ex._max_sub_agents()
    check("子智能体保险丝有界(2-4)", 2 <= n <= 4)
    orig_cpu = ex._cpu_pct
    try:
        ex._cpu_pct = lambda: 95.0
        check("高负载子智能体收缩=2", ex._max_sub_agents() == 2)
        ex._cpu_pct = lambda: 10.0
        check("平常子智能体=4", ex._max_sub_agents() == 4)
    finally:
        ex._cpu_pct = orig_cpu


def sim_stop_words():
    """叫停词表（2026-08-30 用户令：我说停所有都停）——裸"停"不在表的实锤教训；
    单字只认裸句防误伤；两路叫停+光球全停都清队。"""
    import companion_rt as rt
    for t in ["停", "停停停", "停下来", "别改了", "别弄了", "打住", "先别弄了",
              "不用做了", "停止", "算了"]:
        check(f"叫停命中: {t}", rt._is_stop(t))
    for t in ["停在桌面上的文件别动", "不要停", "别停", "继续", "停到哪儿了"]:
        check(f"不叫停误伤: {t}", not rt._is_stop(t))
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "companion_rt.py"), encoding="utf-8").read()
    check("两路叫停+光球全停都清队", src.count('_drain_queue("叫停")') >= 2
          and '_drain_queue("光球全停")' in src)


def sim_context_carry():
    """任务携带对话上下文 + FC 派活抑制（2026-08-30：148s/16 轮+双派活实锤）。"""
    import companion_rt as rt
    rt._session_turns[:] = [("怎么把代理设成中文", "你可以这样调……"),
                            ("好", "已经调好了")]
    out = rt._enrich_task_context("把刚才整理的文档保存到桌面")
    check("指代任务携带对话上下文", "对话上下文" in out and "已经调好了" in out)
    out2 = rt._enrich_task_context("在桌面新建一个叫测试的文件夹")
    check("无指代不附上下文", out2 == "在桌面新建一个叫测试的文件夹")
    rt._fc_fired.update(text="把文档存到桌面", tool="run_task", ts=time.time())
    check("FC 已派活同句抑制", rt._fc_already_tasked("把文档存到桌面"))
    check("FC 派活后别的句子不抑制", not rt._fc_already_tasked("现在几点了"))
    rt._fc_fired.update(text="把文档存到桌面", tool="web_search", ts=time.time())
    check("非 run_task 的 FC 不抑制", not rt._fc_already_tasked("把文档存到桌面"))
    rt._fc_fired.update(text=None, tool=None, ts=0.0)


def sim_task_smell_net():
    """chat 误判兜底网（scaffold 临时件）：任务气味祈使句被判 chat → 强制派活；
    疑问句/闲聊/抱怨不兜。生产路确定性覆盖模型方差。
    2026-08-31 收紧实锤：带语气词的抱怨话（"我要的是关闭呀"）原样派活=荒谬。"""
    rt = _bind_scaffold()
    def hit(t):
        return rt._scaffold_call("task_smell_net", t)
    for t in ["打开一个新的文档", "帮我把文档存到桌面", "关闭所有窗口",
              "新建一个文件夹", "把刚才整理的文档保存到桌面"]:
        check(f"任务气味兜底: {t}", hit(t))
    for t in ["你会打开文档吗", "打开Word是什么意思", "现在几点了", "给我讲个笑话",
              "我要的是关闭呀。", "我要的是关闭呀", "这不是我想要的啊"]:
        check(f"闲聊/疑问/抱怨不兜底: {t[:12]}", not hit(t))
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "companion_rt.py"), encoding="utf-8").read()
    check("兜底走 scaffold 调用口", '_scaffold_call("task_smell_net"' in src)


def sim_artifact_gate():
    """产物核验闸（2026-08-30 实锤：模型谎报"已存桌面"实际没落盘）——
    保存/写入类任务完成必须有目标位置新文件；非产物类任务不介入。"""
    import gui_agent as g
    import tempfile
    check("非产物任务不介入", g._artifact_check("把窗口最小化", 0) is None)
    with tempfile.TemporaryDirectory() as td:
        old = os.path.join(td, "旧文件.txt")
        open(old, "w").write("x")
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        check("目标位置只有旧文件=没落盘",
              g._artifact_check("把文档保存到桌面", time.time(), _dir=td) is False)
        new = os.path.join(td, "新文档.txt")
        open(new, "w").write("x")
        check("目标位置有新文件=通过",
              g._artifact_check("把文档保存到桌面", time.time() - 5, _dir=td) is True)
        check("显式路径优先",
              g._artifact_check(f"保存到 {td}", time.time() - 5) is True)
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "gui_agent.py"), encoding="utf-8").read()
    check("产物核验接入完成闸", "无落盘新文件，驳回继续" in src)


def sim_wake_feedback():
    """唤醒敏感+反馈（2026-08-30 用户令"不敏感+没反馈"）：阈值放宽、
    裸"小凯"入表、命中即本地说"在呢"+光球亮闪。"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    kw = open(os.path.join(base, "models",
              "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01",
              "keywords_custom.txt"), encoding="utf-8").read()
    check("裸'小凯'入唤醒表", any(l.endswith("@小凯") for l in kw.splitlines()))
    check("老词都在", "小凯小凯" in kw and "你好小凯" in kw)
    wsrc = open(os.path.join(base, "wake_word.py"), encoding="utf-8").read()
    check("KWS 阈值放宽 0.08（可配置）", "threshold=0.08" in wsrc
          and "wake_threshold" in wsrc)
    check("断流看门狗在位（电气零静默重开流）",
          "重开流自愈" in wsrc and "silent_since" in wsrc)
    check("退避封顶 3s", "min(backoff * 2, 3.0)" in wsrc)
    check("增益+静默秒可配置", "wake_input_gain" in wsrc
          and "wake_silent_reopen_seconds" in wsrc)
    rsrc = open(os.path.join(base, "companion_rt.py"), encoding="utf-8").read()
    check("唤醒即说'在呢'反馈接线",
          'args=("在呢",)' in rsrc and "_on_wake_word" in rsrc)


def sim_stall_detail():
    """竭尽所能+卡点如实说（2026-08-30 用户铁律）：失败带卡点+新方向；
    受理/承诺话术不压住真实结果播报。"""
    import companion_rt as cr
    from plugins import exec_native as ex
    _bind_scaffold()   # _already_answered 的承诺过滤走 scaffold 插件
    # 承诺话术不压住结果（实锤："正在改马上就好"压住了"已写入配置"的完成播报）
    cr._session_turns[:] = [("把唤醒词改一下",
                            "正在改语音识别的配置文件，马上就能测效果了")]
    check("承诺≠答过（结果照常报）",
          cr._already_answered("把唤醒词改一下") is False)
    cr._session_turns[:] = [("今天天气怎么样",
                            "上海今天中雨，26到31度，出门记得带伞哦")]
    check("实质回答=答过（防双答照旧）",
          cr._already_answered("今天天气怎么样") is True)
    cr._session_turns[:] = []
    # 失败带卡点（行为级：模型调用失败 → Result 带"卡在「…」"）
    orig_plan, orig_call = ex._plan, ex._deepseek_call
    try:
        ex._plan = lambda t, log=print: (ex._MODEL, {"type": "disabled"}, None)

        def _boom(*a, **k):
            raise RuntimeError("断网")
        ex._deepseek_call = _boom
        r = ex.run_task("查一下我的磁盘还剩多少空间", log=lambda *a: None,
                        timeout=10)
        check("失败带卡点如实说", r.status == "failed" and "卡在" in str(r.value))
    finally:
        ex._plan, ex._deepseek_call = orig_plan, orig_call
    # 失败话术模板必须给新方向
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "plugins", "expression.py"),
        encoding="utf-8").read()
    check("失败播报带下一步新方向", "新方向" in src)


def sim_wake_daemon():
    """退下召回守护（2026-08-31 用户裁决：真退下+仍可语音唤醒）——
    极简守护在位、等主程序死透再听、退下分支接线。"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    wd = open(os.path.join(base, "wake_daemon.py"), encoding="utf-8").read()
    check("守护脚本在位且自包含", "def _main_alive" in wd and "_launch_main" in wd)
    check("守护等主程序死透再听（竞态防线）", "最多等 120s" in wd)
    check("守护命中拉起+自退", "os._exit(0)" in wd)
    rt = open(os.path.join(base, "companion_rt.py"), encoding="utf-8").read()
    check("退下接线拉起守护", "_start_wake_daemon()" in rt
          and "_retire_companion" in rt)
    check("press 档不拉守护（尊重用户选择）", "wake_mode=press" in rt)


def sim_capability():
    """能力件库（2026-08-31 L1 前台能力重构）：匹配/权限档口径/执行验证。
    薄包装全 mock——仿真绝不碰真壁纸/主题/音量（真实副作用纪律）。"""
    from plugins import exec_native as ex
    import permission_mode
    orig_mode = permission_mode.current
    orig_funcs = dict(ex._CAP_FUNCS)
    try:
        # 引擎：匹配 + 闸口径
        ex._CAP_FUNCS["cap_wallpaper"] = (
            lambda t, log: (True, "壁纸已换成纯色深灰蓝"))
        ex._CAP_FUNCS["cap_volume"] = (lambda t, log: (True, "音量调大 10 格"))
        permission_mode.current = lambda: "yolo"
        r = ex._try_capability("换个深色壁纸", lambda *a: None, None, set())
        check("yolo 档能力件直接执行不问", r is not None
              and r.status == "completed")
        permission_mode.current = lambda: "confirm"
        calls = []
        r = ex._try_capability("换个深色壁纸", lambda *a: None,
                               lambda d: calls.append(d) or True, set())
        check("confirm 档 system 能力件过确认", len(calls) == 1)
        calls = []
        r = ex._try_capability("把音量调大一点", lambda *a: None,
                               lambda d: calls.append(d) or True, set())
        check("low 风险任何档不问", r is not None and len(calls) == 0)
        # 失败落回模型不谎称
        ex._CAP_FUNCS["cap_wallpaper"] = (lambda t, log: (False, "没找到图"))
        permission_mode.current = lambda: "yolo"
        check("能力件失败落回模型不谎称",
              ex._try_capability("换个壁纸", lambda *a: None, None, set()) is None)
        # 不命中落回模型
        check("不相关任务不命中",
              ex._try_capability("帮我打开微信", lambda *a: None, None,
                                 set()) is None)
    finally:
        permission_mode.current = orig_mode
        ex._CAP_FUNCS.clear()
        ex._CAP_FUNCS.update(orig_funcs)

    # 壁纸纯色：mock SPI+读回一致 → 完成且验证通过（真值源=Explorer\Wallpapers）
    sent = {}
    orig_spi, orig_read = ex._spi_set_wallpaper, ex._read_current_wallpaper
    ex._spi_set_wallpaper = lambda p: (sent.setdefault("path", p), True)[1]
    ex._read_current_wallpaper = lambda: sent.get("path", "")
    try:
        ok, msg = ex.cap_wallpaper("换个深蓝色壁纸", lambda *a: None)
        check("纯色壁纸执行+读回验证", ok and "深蓝" in msg
              and sent["path"].endswith("壁纸-纯色深蓝.png"))
        ex._read_current_wallpaper = lambda: r"C:\别的.png"
        ok, msg = ex.cap_wallpaper("换个深蓝色壁纸", lambda *a: None)
        check("读回不一致=如实没生效", not ok)
    finally:
        ex._spi_set_wallpaper, ex._read_current_wallpaper = orig_spi, orig_read

    # 深色模式：mock 写/读回/广播
    state = {}
    orig_w, orig_r, orig_b = (ex._write_theme_light, ex._read_theme_light,
                              ex._broadcast_settingchange)
    ex._write_theme_light = lambda v: state.update(v=v)
    ex._read_theme_light = lambda: state.get("v", -1)
    ex._broadcast_settingchange = lambda: state.update(bc=True)
    try:
        ok, _ = ex.cap_dark_mode("开深色模式", lambda *a: None)
        check("深色模式写读回一致+广播", ok and state["v"] == 0 and state["bc"])
        ok2, _ = ex.cap_dark_mode("换回浅色模式", lambda *a: None)
        check("浅色模式写读回一致", ok2 and state["v"] == 1)
    finally:
        ex._write_theme_light, ex._read_theme_light = orig_w, orig_r
        ex._broadcast_settingchange = orig_b

    # 音量键：mock 计数
    pressed = []
    orig_vk = ex._vol_key
    ex._vol_key = lambda vk, n: pressed.extend([vk] * n)
    try:
        ok, _ = ex.cap_volume("把音量调大一点", lambda *a: None)
        check("调大一点=5 格上键", ok and pressed == [0xAF] * 5)
        pressed.clear()
        ok, _ = ex.cap_volume("静音", lambda *a: None)
        check("静音=1 次静音键", ok and pressed == [0xAD])
        pressed.clear()
        ok, _ = ex.cap_volume("音量调到 50%", lambda *a: None)
        check("绝对值=归零再步进（50 下+25 上）",
              ok and pressed == [0xAE] * 50 + [0xAF] * 25)
    finally:
        ex._vol_key = orig_vk

    # 回桌面：mock Win+D+前台类名
    orig_wd, orig_fc = ex._win_d, ex._foreground_class
    state = {}
    ex._win_d = lambda: state.update(win_d=True)
    ex._foreground_class = lambda: "Progman"
    try:
        ok, msg = ex.cap_show_desktop("回到桌面", lambda *a: None)
        check("回桌面 Win+D+验证到桌面", ok and state["win_d"] and "已回到桌面" in msg)
        ex._foreground_class = lambda: "Chrome_WidgetWin_1"
        ok2, msg2 = ex.cap_show_desktop("回到桌面", lambda *a: None)
        check("没真到桌面=如实报", ok2 and "再按一次" in msg2)
    finally:
        ex._win_d, ex._foreground_class = orig_wd, orig_fc

    # 注入鉴别：agent 的注入左键不触发全停；真实点击照触发（2026-08-31 换维度实锤——
    # 位置硬编码把压在光球上的记事本误杀；注入鉴别无任何位置例外）
    import sense_io as si
    si._injected_click.update(t=time.time(), pos=(150, 150))
    check("注入点击近 2s=忽略全停", si.recent_injected_click() is True)
    si._injected_click.update(t=time.time() - 10, pos=(150, 150))
    check("10s 前注入=不算近（真实点击照触发）",
          si.recent_injected_click() is False)
    import companion_rt as crt2
    check("全停入口带注入过滤", "recent_injected_click" in
          open(os.path.join(os.path.dirname(os.path.dirname(
              os.path.abspath(__file__))), "companion_rt.py"),
              encoding="utf-8").read())


def sim_task_graph():
    """图工程打样（2026-08-31 阶段 B）：节点契约/游标顺序/回退边/灰度开关/
    能力件节点接入+落回旧循环；账本缝=游标跑在既有会话流里（schema 不动）。"""
    import task_graph
    from plugins import exec_native as ex
    import permission_mode
    # 契约+游标：两节点顺序执行、状态记账
    order = []
    task_graph.register_executor("t1", lambda n, log: (order.append(n["id"]), (True, "ok1"))[1])
    task_graph.register_executor("t2", lambda n, log: (order.append(n["id"]), (True, "ok2"))[1])
    g = task_graph.make_graph("测试图", [
        task_graph.make_node("第一步", "t1", node_id="a"),
        task_graph.make_node("第二步", "t2", node_id="b")])
    ok, g = task_graph.walk(g, log=lambda m: None)
    check("游标顺序执行两节点", ok and order == ["a", "b"])
    check("节点状态 done", all(n["state"] == "done" for n in g["nodes"]))
    check("图状态 done", g["state"] == "done")
    # 回退边：主执行器失败 → fallback 成功
    task_graph.register_executor("bad", lambda n, log: (False, "炸"))
    task_graph.register_executor("good", lambda n, log: (True, "救回"))
    g2 = task_graph.make_graph("回退图", [
        task_graph.make_node("会炸的一步", "bad", fallback="good", node_id="c")])
    ok2, g2 = task_graph.walk(g2, log=lambda m: None)
    check("fallback 边救回", ok2 and g2["nodes"][0]["executor"] == "good"
          and g2["nodes"][0]["state"] == "done")
    # 回退也失败 → 如实停图不谎报
    task_graph.register_executor("bad2", lambda n, log: (False, "也炸"))
    g3 = task_graph.make_graph("双炸图", [
        task_graph.make_node("没救的一步", "bad", fallback="bad2", node_id="d"),
        task_graph.make_node("不该执行的一步", "t1", node_id="e")])
    ok3, g3 = task_graph.walk(g3, log=lambda m: None)
    check("双炸=图如实失败", not ok3 and g3["state"] == "failed"
          and g3["nodes"][0]["state"] == "failed")
    check("失败停图后续节点不跑", g3["nodes"][1]["state"] == "queued")
    # 面板回调每节点触发
    seen = []
    g4 = task_graph.make_graph("回调图", [task_graph.make_node("x", "t1", node_id="f")])
    task_graph.walk(g4, log=lambda m: None, on_node=lambda n: seen.append(n["id"]))
    check("on_node 节点级回调", seen == ["f"])

    # 能力件节点接入（mock 能力件函数，不碰真副作用）
    orig_funcs = dict(ex._CAP_FUNCS)
    orig_mode = permission_mode.current
    permission_mode.current = lambda: "yolo"
    steps = []
    try:
        ex._CAP_FUNCS["cap_wallpaper"] = (lambda t, log: (True, "壁纸已换成纯色深灰蓝"))
        r = ex._try_graph("换个深色壁纸", lambda *a: None, None, set(),
                          on_step=lambda s: steps.append(s))
        check("能力件走图完成", r is not None and r.status == "completed"
              and "graph" in r.tools)
        check("面板节点级进度", any("√" in s and "节点" in s for s in steps))
        # 能力件失败 → generic 回退边 → 图失败 → None 落回旧循环
        ex._CAP_FUNCS["cap_wallpaper"] = (lambda t, log: (False, "SPI 失败"))
        r2 = ex._try_graph("换个深色壁纸", lambda *a: None, None, set())
        check("图失败落回旧循环（不谎称）", r2 is None)
        # 灰度开关关掉 → 不走图层
        import json as _json
        check("灰度开关 off=旧循环", task_graph.graph_engine_on(
            {"graph_engine": False}) is False)
    finally:
        ex._CAP_FUNCS.clear()
        ex._CAP_FUNCS.update(orig_funcs)
        permission_mode.current = orig_mode


def sim_recipe():
    """GUI 配方回放（2026-08-31 阶段 C，图节点类型②）：v2 录制/同 sig 替换/
    匹配/回放执行与失配回退；老 v1 行不被 v2 匹配器误收。"""
    import gui_agent as g
    import tempfile, json
    tmpd = tempfile.mkdtemp()
    orig_path = g._RECIPES_PATH
    g._RECIPES_PATH = os.path.join(tmpd, "recipes.jsonl")
    try:
        # v1 老行 + v2 录制回合
        open(g._RECIPES_PATH, "w", encoding="utf-8").write(
            json.dumps({"task": "打开记事本写内容", "summary": "click → type"},
                       ensure_ascii=False) + "\n")
        steps = [{"action": "uia_click", "text": "文件"},
             {"action": "hotkey", "keys": "ctrl,s"},
             {"action": "type", "text": "hello"}]
        g._record_recipe("打开记事本写内容", "click → type", 42.0, steps=steps)
        rec = g._match_recipe_v2("帮我在记事本里写内容")
        check("v2 录制→匹配回合", rec is not None and len(rec["steps"]) == 3)
        check("v1 老行不进 v2 匹配", rec.get("v") == 2)
        # 同 sig 替换：新者胜
        g._record_recipe("打开记事本写内容", "new", 9.0,
                         steps=[{"action": "wait", "ms": 100}])
        lines = open(g._RECIPES_PATH, encoding="utf-8").readlines()
        v2s = [json.loads(l) for l in lines if json.loads(l).get("v") == 2]
        check("同 sig 旧配方被新者替换",
              len(v2s) == 1 and v2s[0]["duration_s"] == 9.0)
        # 回放执行：uia 命中顺序执行（mock 全薄包装，零真实输入）
        calls = []
        orig_find, orig_click = g.uia_tools.find_element, g.host_input.click
        g.uia_tools.find_element = lambda t, timeout=1.5, **kw: (100, 100)
        g.host_input.click = lambda *a, **kw: calls.append(a) or True
        orig_hotkey = g.host_input.hotkey
        g.host_input.hotkey = lambda *a, **kw: calls.append(a) or True
        orig_type = g.host_input.type_unicode
        g.host_input.type_unicode = lambda t, **kw: calls.append(t) or True
        try:
            ok, msg = g._replay_steps(steps, log=lambda m: None)
            check("回放全中顺序执行", ok and len(calls) == 3
                  and calls[1] == ("ctrl", "s"))
        finally:
            g.uia_tools.find_element = orig_find
            g.host_input.click, g.host_input.hotkey = orig_click, orig_hotkey
            g.host_input.type_unicode = orig_type
        # 失配：uia 找不到 → 如实失败原因（回退 VLM）
        orig_find2 = g.uia_tools.find_element
        g.uia_tools.find_element = lambda t, timeout=1.5, **kw: None
        try:
            ok, msg = g._replay_steps(steps, log=lambda m: None)
            check("失配如实回报（第1步）", not ok and "失配" in msg and "文件" in msg)
        finally:
            g.uia_tools.find_element = orig_find2
        # 无配方任务 → v2 匹配 None
        check("无配方不落回放", g._match_recipe_v2("把回收站清空") is None)
    finally:
        g._RECIPES_PATH = orig_path


def sim_confirm_chain():
    """确认链三修（2026-08-28 用户实锤：问句没送达/被顶掉→超时→莫名其妙
    '不弄了'）。行为断言：闩锁等待不即弃、中途没听清反馈、放弃词诚实、
    结果播报给确认让路。"""
    import companion_rt as cr
    # 1) 闩锁被占：_begin_confirmation 等待而非瞬间 None
    cr._confirm["active_id"] = "someone-else"
    t0 = time.time()
    done = []

    def _bg_free():
        time.sleep(1.0)
        with cr._confirm_lock:
            cr._confirm["active_id"] = None

    import threading as _th
    _th.Thread(target=_bg_free, daemon=True).start()
    cid = cr._begin_confirmation(wait_locked=5.0)
    dt = time.time() - t0
    check("闩锁被占→等待释放再开（不瞬间弃）", cid is not None and 0.5 < dt < 5,
          f"dt={dt:.1f}s cid={bool(cid)}")
    cr._finish_confirmation(cid)
    # 真占死（无人释放）→ 有限等待后 fail-closed
    cr._confirm["active_id"] = "stuck"
    t0 = time.time()
    cid2 = cr._begin_confirmation(wait_locked=0.8)
    check("闩锁占死→有限等待后 None", cid2 is None and time.time() - t0 < 2.5)
    cr._confirm["active_id"] = None
    # 2) _speak_fast 确认期间让路（源码哨）
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "companion_rt.py"), encoding="utf-8").read()
    body = src.split("def _speak_fast")[1].split("\ndef ")[0]
    check("结果播报给确认让路（waiting 检查在）", '_confirm["waiting"]' in body)
    # 3) 等待期间有没听清反馈，总窗口不翻倍
    ask_body = src.split("def _ask_confirm")[1].split("\ndef ")[0]
    check("确认静默看门狗（reminder_at 机制在）", "reminder_at" in ask_body
          and "我还没听清" in ask_body and "asked_rounds" not in ask_body)
    check("放弃词诚实（没听到答复，不是用户拒绝）",
          "没听到明确答复" in ask_body)


def sim_panel_instant_wake():
    """零延迟渲染（2026-08-28 用户令：文字框主观无延迟）：内容变更必须
    立即置唤醒事件。"""
    import task_overlay as t
    t._wake.clear()
    t.log_line("新行")
    check("log_line 立即唤醒", t._wake.is_set())
    t._wake.clear()
    t.add_step("步骤")
    check("add_step 立即唤醒", t._wake.is_set())
    t._wake.clear()
    t._ensure_thread()
    t.session_open(True)   # work_begin 无会话时合法跳过——开会在测
    t._wake.clear()
    t.work_begin("k", "活", grace=0)
    check("work_begin 立即唤醒", t._wake.is_set())
    t.work_end("k")
    t.session_open(False)
    t.complete_step()
    check("complete_step 立即唤醒", t._wake.is_set())
    t._wake.clear()


def main():
    sim_parsing()
    sim_startmenu()
    sim_enum_windows()
    sim_close_fast()
    sim_pending_ttl()
    sim_gui_serialize()
    sim_e2e_launch_dry()
    sim_persona_scrub()
    sim_launch_routing()
    sim_yolo_send_and_dedup()
    sim_no_closeout_on_verified()
    sim_fast_result_instant()
    sim_close_all()
    sim_voice_fast_lane()
    sim_fc_enqueue_alive()
    sim_form_page()
    sim_fuzzy_file()
    sim_vocative_strip()
    sim_auto_fallback()
    sim_template_no_asks()
    sim_no_console_flash()
    sim_speak_immediate()
    sim_progress_stuck_silent()
    sim_panel_reset()
    sim_launch_async_verify()
    sim_repeat_guard()
    sim_confirm_chain()
    sim_gui_ears()
    sim_uia_grounding()
    sim_realtime_guard()
    sim_elastic_workers()
    sim_stop_words()
    sim_context_carry()
    sim_task_smell_net()
    sim_artifact_gate()
    sim_wake_feedback()
    sim_stall_detail()
    sim_wake_daemon()
    sim_capability()
    sim_task_graph()
    sim_recipe()
    sim_panel_instant_wake()
    print()
    if FAILS:
        print(f"任务仿真失败 {len(FAILS)} 项：{FAILS}")
        sys.exit(1)
    print("TASK SIM 全过")


if __name__ == "__main__":
    main()
