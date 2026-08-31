# -*- coding: utf-8 -*-
"""本地语义 embedding（2026-08-31 用户裁决安装）：bge-small-zh-v1.5 量化 ONNX
（models/bge-small-zh/）+ tokenizers，第三方件隔离在 vendor/emb/（pip --target，
不碰共享 .venv）。

设计纪律（与总纲同效）：
- 全惰性 + fail-soft：首次调用才加载；缺模型/缺依赖/推理异常一律
  available()=False，调用方（soul.recall）静默走关键词老路；
- 开关：config.json `memory_semantic`（默认 true），false 即整体停用；
- 磁盘缓存 memory/emb_cache.json（sha1(文本)→向量，约 5 位小数），
  记忆条目稳定所以缓存命中率高，冷启动只 embedding 新增条目。
"""
import hashlib
import json
import os
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODEL_DIR = os.path.join(_HERE, "models", "bge-small-zh")
_VENDOR = os.path.join(_HERE, "vendor", "emb")
_CACHE_PATH = os.path.join(_HERE, "memory", "emb_cache.json")
_THRESHOLD = 0.45   # 余弦相似度下限：低于它宁可说不知道（与关键词路同纪律）

_state = {"tried": False, "ok": False, "sess": None, "tok": None,
          "cache": None, "np": None}
_lock = threading.Lock()


def _cfg_on():
    try:
        with open(os.path.join(_HERE, "config.json"), encoding="utf-8") as f:
            return bool(json.load(f).get("memory_semantic", True))
    except Exception:
        return True


def _lazy_init():
    if _state["tried"]:
        return _state["ok"]
    with _lock:
        if _state["tried"]:
            return _state["ok"]
        try:
            if not _cfg_on():
                return False   # 开关关着不记"tried"：开回来即恢复可试
            mp = os.path.join(_MODEL_DIR, "model_quantized.onnx")
            tp = os.path.join(_MODEL_DIR, "tokenizer.json")
            if not (os.path.exists(mp) and os.path.exists(tp)):
                return False   # 模型没下载完同理：下次调用再试
            _state["tried"] = True   # 真加载过才记（加载成败都钉住本进程）
            if _VENDOR not in sys.path:
                sys.path.insert(0, _VENDOR)
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
            so = ort.SessionOptions()
            so.intra_op_num_threads = 2   # 小模型短文本，别抢人机交互的核
            _state["sess"] = ort.InferenceSession(
                mp, sess_options=so, providers=["CPUExecutionProvider"])
            tok = Tokenizer.from_file(tp)
            tok.enable_truncation(512)
            _state["tok"] = tok
            _state["np"] = np
            try:
                with open(_CACHE_PATH, encoding="utf-8") as f:
                    _state["cache"] = json.load(f)
            except Exception:
                _state["cache"] = {}
            _state["ok"] = True
        except Exception:
            _state["ok"] = False
        return _state["ok"]


def available():
    """开关+模型+依赖都在位才 True；任何缺件静默 False（走关键词老路）。"""
    return _cfg_on() and _lazy_init() and _cfg_on()


def _sha(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _forward(texts):
    """bge 前向：last_hidden_state 按 attention_mask 均值池化 + L2 归一。"""
    np = _state["np"]
    enc = _state["tok"].encode_batch(texts)
    ids = [e.ids for e in enc]
    mask = [e.attention_mask for e in enc]
    n = max(len(x) for x in ids)
    ids = np.array([x + [0] * (n - len(x)) for x in ids], dtype=np.int64)
    mask = np.array([x + [0] * (n - len(x)) for x in mask], dtype=np.int64)
    out = _state["sess"].run(
        None, {"input_ids": ids, "attention_mask": mask,
               "token_type_ids": np.zeros_like(ids)})[0]
    m = mask.astype(np.float32)[..., None]
    vec = (out * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
    norm = np.linalg.norm(vec, axis=1, keepdims=True)
    return (vec / np.clip(norm, 1e-9, None)).tolist()


def embed(texts):
    """[str] → [向量]；sha1 命中磁盘缓存，新文本批量推理后落盘。"""
    if not _lazy_init():
        return None
    texts = [str(t or "") for t in texts]
    cache = _state["cache"]
    vecs = [None] * len(texts)
    missing, miss_idx = [], []
    for i, t in enumerate(texts):
        v = cache.get(_sha(t))
        if v is None:
            missing.append(t)
            miss_idx.append(i)
        else:
            vecs[i] = v
    if missing:
        try:
            got = _forward(missing)
        except Exception:
            return None
        for i, v in zip(miss_idx, got):
            rv = [round(x, 5) for x in v]
            vecs[i] = rv
            cache[_sha(texts[i])] = rv
        try:
            os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
            with open(_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(cache, f)
        except OSError:
            pass
    return vecs


def semantic_hits(query, candidates, top_k=3, exclude=None, threshold=_THRESHOLD):
    """query 对 candidates 的余弦命中 [(下标, 分数)]，降序；exclude=已命中文本。
    任何异常返回 []（调用方按无语义补漏处理）。"""
    if not query or not candidates:
        return []
    try:
        vecs = embed([query] + list(candidates))
        if not vecs:
            return []
        q, ex = vecs[0], set(exclude or ())
        sims = []
        for i, c in enumerate(candidates):
            if c in ex:
                continue
            s = sum(a * b for a, b in zip(q, vecs[i + 1]))
            if s >= threshold:
                sims.append((s, i))
        sims.sort(key=lambda x: -x[0])
        return [(i, round(s, 4)) for s, i in sims[:top_k]]
    except Exception:
        return []
