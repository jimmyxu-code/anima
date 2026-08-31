# -*- coding: utf-8 -*-
"""task_graph：任务图引擎（图工程渐进换心·打样，2026-08-31 阶段 B）。

节点契约（数据不是类——与能力件 jsonl 同哲学）：
    {"id", "goal", "executor", "args", "verify", "fallback", "state", "result"}
  - executor：执行器名（capability/recipe/generic…），注册制可热插拔
  - verify：完成验证口径（MVP="executor"=信任执行器自带的确定性验证；
    阶段 C 扩 artifact/window/uia）
  - fallback：失败回退边（换执行器再试同一 goal，再败=如实停在失败节点）

游标 walk：逐节点执行→记账；失败沿 fallback 边换执行器重试一次；
再败=如实失败（不谎报不闷停）。意图/规划归模型（图由模型/能力件数据生成），
机械游标只做执行与验证边（与工程哲学同构）。

账本缝（契约②）：引擎不写账本格式——游标跑在既有 task_session 里
（调用方建会话，schema 天然不破坏）。
"""

import json
import os
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
EXECUTORS = {}


def register_executor(name, fn):
    """执行器注册口：fn(node, log) -> (ok, message)。可热插拔。"""
    EXECUTORS[name] = fn


def make_node(goal, executor, args=None, fallback="generic", node_id=None):
    """节点工厂：goal 必填；fallback 默认 generic（转通用模型循环）。"""
    return {"id": node_id or f"n-{executor}", "goal": goal,
            "executor": executor, "args": dict(args or {}),
            "verify": {"kind": "executor"}, "fallback": fallback,
            "state": "queued", "result": ""}


def make_graph(goal, nodes):
    """图工厂：MVP 为有序节点表（将来可扩 DAG 边）。"""
    return {"goal": goal, "nodes": list(nodes), "state": "queued",
            "created": time.time()}


def walk(graph, log=print, on_node=None, cancel_check=None):
    """游标走图，返回 (ok, graph)。
    节点：执行→done；失败沿 fallback 边换执行器重试一次；再败 failed 停图。
    on_node(node) 节点级进度回调（面板用）；cancel_check() 外部叫停。"""
    graph["state"] = "running"
    for node in graph["nodes"]:
        if cancel_check and cancel_check():
            node["state"] = "skipped"
            graph["state"] = "cancelled"
            log(f"task_graph: 游标被取消（停在 {node['id']}）")
            return False, graph
        chain = [node["executor"]]
        fb = node.get("fallback")
        if fb and fb != node["executor"]:
            chain.append(fb)
        done = False
        for i, ex_name in enumerate(chain):
            if i:
                log(f"task_graph: 节点 {node['id']} 走 fallback 边 → {ex_name}")
                node["executor"] = ex_name
            node["state"] = "running"
            fn = EXECUTORS.get(ex_name)
            try:
                ok, msg = (fn(node, log) if fn is not None
                           else (False, f"执行器未注册: {ex_name}"))
            except Exception as e:
                ok, msg = False, f"执行器异常: {e}"
            node["result"] = str(msg)
            if ok:
                node["state"] = "done"
                done = True
                break
            log(f"task_graph: 节点 {node['id']} 执行器 {ex_name} 未过："
                f"{str(msg)[:50]}")
        if not done:
            node["state"] = "failed"
            if on_node is not None:
                on_node(node)
            graph["state"] = "failed"
            return False, graph
        if on_node is not None:
            on_node(node)
    graph["state"] = "done"
    return True, graph


_CFG_PATH = os.path.join(_HERE, "config.json")


def graph_engine_on(cfg=None):
    """灰度开关：config.json graph_engine（默认开；false=不走图层全走旧循环）。"""
    try:
        cfg = cfg if cfg is not None else json.load(
            open(_CFG_PATH, encoding="utf-8-sig"))
    except (OSError, ValueError):
        cfg = {}
    return bool(cfg.get("graph_engine", True))
