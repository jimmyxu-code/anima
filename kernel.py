# -*- coding: utf-8 -*-
"""kernel：小凯微内核（cordis 语义的最小 Python 实现）。

只实现三语义，不追全量（2026-08-21 批准的插件原生重构 P1）：
1. 服务注册表：ctx.provide(name, svc) / ctx.svc(name)——插件间只经服务键通信，
   不发生直接 import 引用。
2. waterfall 事件：ctx.on(event, fn) / ctx.waterfall(event, payload)——
   每个监听器拿 (payload, next)；调 next() 委托下游可改写结果；不调直接返回
   = 短路接管。拦截/策略一律走这，不写死分支。
3. fiber 式回收：插件 register(ctx) 内 ctx.effect(disposer) 登记的一切，
   unload 时按 LIFO 全部撤销——插件能真正拔掉，进程不重启。

插件形态：一个带 register(ctx) 的模块/对象（可选 name 属性）；放 plugins/ 下
或直接由 companion_rt 启动时装配。Kernel 本身不知道什么叫智能体。
"""

import threading


class _Fiber:
    """一个插件实例的运行时句柄 + 撤销账本。"""

    __slots__ = ("name", "disposers", "active")

    def __init__(self, name):
        self.name = name
        self.disposers = []
        self.active = True


class Ctx:
    """插件看到的上下文：服务面 + 事件面 + 回收登记。绑定某个 fiber。"""

    def __init__(self, kernel, fiber):
        self._k = kernel
        self._fiber = fiber

    # --- 服务面 ---
    def provide(self, name, service):
        self._k._services[name] = service
        self._k._owners.setdefault(name, []).append(self._fiber.name)
        self.effect(lambda: self._k._retract(name, self._fiber.name))

    def svc(self, name, default=None):
        return self._k._services.get(name, default)

    # --- 事件面 ---
    def on(self, event, fn):
        self._k._listeners.setdefault(event, []).append(fn)
        listeners = self._k._listeners[event]

        def _off():
            try:
                listeners.remove(fn)
            except ValueError:
                pass
        self.effect(_off)

    def emit(self, event, payload=None):
        for fn in list(self._k._listeners.get(event, [])):
            fn(payload)

    def waterfall(self, event, payload):
        """cordis 瀑布：监听器拿 (payload, next)；**后注册的为最外层**
        （后挂载=高优先，可拦截/包装先注册的，与 patch 层叠序同构）。
        无监听器 → payload 原样。"""
        handlers = list(reversed(self._k._listeners.get(event, [])))

        def _call(i, p):
            if i >= len(handlers):
                return p
            return handlers[i](p, lambda np=p: _call(i + 1, np))

        return _call(0, payload)

    # --- 回收面 ---
    def effect(self, disposer):
        self._fiber.disposers.append(disposer)


class Kernel:
    """微内核本体：装配/卸载插件，持有服务与事件注册表。线程安全（粗锁）。"""

    def __init__(self, log=print):
        self._services = {}
        self._listeners = {}
        self._owners = {}          # service name -> [fiber names]
        self._fibers = {}          # plugin name -> _Fiber
        self._lock = threading.RLock()
        self._log = log

    def load(self, name, plugin):
        """plugin：带 register(ctx) 的模块/对象。重复加载同名插件 = 先卸再装。"""
        with self._lock:
            if name in self._fibers:
                self.unload(name)
            fiber = _Fiber(name)
            self._fibers[name] = fiber
            ctx = Ctx(self, fiber)
            register = getattr(plugin, "register", None) or getattr(plugin, "apply", None)
            if register is None and callable(plugin):
                register = plugin
            if register is None:
                raise TypeError(f"插件 {name} 没有 register/apply")
            register(ctx)
            fiber.active = True
            self._log(f"kernel: 插件 {name} 已装配")
            return fiber

    def unload(self, name):
        """LIFO 撤销该插件登记的一切副作用。"""
        with self._lock:
            fiber = self._fibers.pop(name, None)
            if fiber is None:
                return False
            fiber.active = False
            for dispose in reversed(fiber.disposers):
                try:
                    dispose()
                except Exception as e:
                    self._log(f"kernel: {name} 回收异常: {e}")
            self._log(f"kernel: 插件 {name} 已拔除")
            return True

    def _retract(self, name, fiber_name):
        owners = self._owners.get(name, [])
        if fiber_name in owners:
            owners.remove(fiber_name)
        if not owners:
            self._services.pop(name, None)
            self._owners.pop(name, None)

    def has(self, service):
        return service in self._services

    def plugins(self):
        return sorted(self._fibers)
