"""TTS 引擎抽象层：引擎即插件，config.json 一键切换。

edge-tts 已移除（GPL-3.0 + 微软未授权端点，商业化红线，2026-08-21 用户拍板删除）。
这层把引擎解耦，key 到位即切，切换时上层（tts_speaker）无感。

引擎契约：
    synth(text, cfg, feed, log) -> bool
    - text: 一句文本（已按句切好）
    - cfg:  config.json 字典
    - feed: fn(bytes)，接收 mp3 分片（24kHz mono）
    - 返回 True = 该引擎播完；False = 调用方回退 SAPI 离线保底，绝不哑火

引擎表：
    doubao  默认。豆包语音合成大模型2.0，V3 HTTP Chunked 单向流式（官方推荐），
            X-Api-Key 认证，边收边播
    azure   预留位。需要时按 registry 加一个函数即可

config.json 相关键：
    tts_engine        引擎名，默认 "doubao"
    doubao_api_key    豆包语音控制台 API Key（新版控制台，非旧 appid+token）；
                      不配置则取凭据管理器 companion-rt/doubao
    tts_voice_doubao  豆包音色，默认 zh_female_vv_uranus_bigtts（Vivi 2.0）
    tts_rate          语速（如 '+20%'），换算成豆包 speech_rate
"""

import base64
import os
import secrets_store
import json
import time
import urllib.request
import uuid


# ---------------------------------------------------------------- doubao
# V3 HTTP Chunked 单向流式（官方推荐，时延优于 V1）：
#   POST https://openspeech.bytedance.com/api/v3/tts/unidirectional
#   认证头 X-Api-Key + X-Api-Resource-Id（新版控制台 API Key 方式）
#   文档: https://www.volcengine.com/docs/6561/2528925
_DOUBAO_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
_DOUBAO_RESOURCE = "seed-tts-2.0"
_DOUBAO_DEFAULT_VOICE = "zh_female_vv_uranus_bigtts"   # Vivi 2.0（TTS2.0 旗舰女声）


def _rate_to_speech_rate(rate):
    """'+20%' → 豆包 speech_rate 20（范围 [-50,100]，100=2 倍速）。"""
    try:
        s = str(rate).strip()
        pct = float(s[:-1]) if s.endswith("%") else (float(s) - 1.0) * 100.0
        return max(-50, min(100, int(round(pct))))
    except (TypeError, ValueError):
        return 20


def _json_objects(stream):
    """从 chunked 流里增量切出完整 JSON 对象（按花括号深度配对）。
    pos 指针续扫不重扫；base64 不含花括号，对象边界可靠。"""
    buf = ""
    depth = 0
    start = -1
    pos = 0
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        buf += chunk.decode("utf-8", "replace")
        while pos < len(buf):
            ch = buf[pos]
            if ch == "{":
                if depth == 0:
                    start = pos
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    yield buf[start:pos + 1]
                    buf = buf[pos + 1:]
                    pos = -1
                    start = -1
            pos += 1


def _synth_doubao(text, cfg, feed, log):
    """豆包 TTS 2.0 单向流式：JSON 块边收边解码喂给播放器。

    失败一律 False（调用方回退 SAPI），绝不影响出声。
    """
    key = str(cfg.get("doubao_api_key") or secrets_store.get_secret("doubao"))
    if not key:
        log("TTS/doubao: 未配置 doubao_api_key")
        return False
    payload = {
        "req_params": {
            "text": text,
            "speaker": str(cfg.get("tts_voice_doubao", _DOUBAO_DEFAULT_VOICE)),
            "audio_params": {
                "format": "mp3",
                "sample_rate": 24000,
                "speech_rate": _rate_to_speech_rate(cfg.get("tts_rate", "+20%")),
            },
        },
    }
    req = urllib.request.Request(
        _DOUBAO_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": key,
            "X-Api-Resource-Id": _DOUBAO_RESOURCE,
            "X-Api-Request-Id": uuid.uuid4().hex,
        },
    )
    t0 = time.time()
    got_audio = False
    with urllib.request.urlopen(req, timeout=15) as resp:
        for raw in _json_objects(resp):
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            code = obj.get("code")
            if code not in (0, None, 20000000):   # 20000000 = 流结束哨兵（msg 为 OK）
                log(f"TTS/doubao: 拒绝 code={code} msg={obj.get('message')}")
                return got_audio
            data = obj.get("data")
            if data:
                audio = base64.b64decode(data)
                if audio:
                    if not got_audio:
                        log(f"TTS/doubao: 首包 {time.time() - t0:.2f}s")
                    got_audio = True
                    feed(audio)
    if got_audio:
        log(f"TTS/doubao: 完成 {time.time() - t0:.2f}s")
    return got_audio


# ---------------------------------------------------------------- registry
_ENGINES = {
    "doubao": _synth_doubao,
    # "azure": _synth_azure,   # 预留位：拿到 key 再实现
}


def current_engine(cfg):
    name = str(cfg.get("tts_engine", "doubao")).lower()
    return name if name in _ENGINES else "doubao"


def synth(text, cfg, feed, log=print):
    """按配置引擎合成；失败 → False（调用方回退 SAPI 离线保底）。"""
    name = current_engine(cfg)
    try:
        return bool(_ENGINES[name](text, cfg, feed, log))
    except Exception as e:
        log(f"TTS/{name}: 异常 {type(e).__name__}: {e}")
        return False
