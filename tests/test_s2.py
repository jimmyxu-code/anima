import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
"""S2 成本实测 spike：真实测三种场景的 token 用量（UsageResponse 事件）。

场景 A：文本注入闲聊一轮（chat_text）
场景 B：麦克风模式 + 10s 真实语音 PCM（mp3 重采样）
场景 C：麦克风模式 + 20s 纯静默（"常开听"烧钱实测）
"""
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import doubao_rt
import secrets_store

KEY = secrets_store.get_secret("doubao")
APP_ID = "3596007629"

usages = []


def on_event(ev, obj):
    if ev == doubao_rt.EV_USAGE:
        usages.append(obj.get("usage", {}))
        print("USAGE:", obj.get("usage"), flush=True)
    elif ev in (doubao_rt.EV_ASR_RESPONSE,):
        for r in obj.get("results", []):
            if not r.get("is_interim"):
                print("ASR:", r.get("text"), flush=True)


BASE = {
    "asr": {"audio_info": {"format": "pcm", "sample_rate": 16000, "channel": 1},
            "extra": {}},
    "tts": {"speaker": "zh_female_vv_jupiter_bigtts",
            "audio_config": {"channel": 1, "format": "pcm_s16le",
                             "sample_rate": 24000},
            "extra": {}},
    "dialog": {"bot_name": "小凯", "extra": {"model": "1.2.1.1"}},
}


def session(input_mod, tag, feed):
    cfg = json.loads(json.dumps(BASE))
    if input_mod:
        cfg["dialog"]["extra"]["input_mod"] = input_mod
    rt = doubao_rt.DoubaoRT(APP_ID, KEY, on_audio=lambda b: None,
                            on_event=on_event, on_error=lambda m: None)
    rt.start()
    rt.start_session(cfg)
    time.sleep(1)
    before = len(usages)
    feed(rt)
    rt.finish_session()
    rt.stop()
    got = usages[before:]
    print(f"== {tag}: {len(got)} 条 usage ==", flush=True)
    for u in got:
        print(json.dumps(u), flush=True)


def feed_text(rt):
    rt.chat_text("你好，给我讲个一句话笑话")
    time.sleep(8)


def load_voice_pcm():
    import miniaudio
    import numpy as np
    dec = miniaudio.decode_file(r"C:\tmp\doubao_test.mp3")
    a = np.frombuffer(dec.samples, dtype=np.int16).reshape(-1, dec.nchannels)
    mono = a[:, 0].astype(np.float32)
    # 24k → 16k 线性重采样
    ratio = dec.sample_rate / 16000
    idx = np.arange(0, len(mono) - 1, ratio)
    down = mono[idx.astype(int)].astype(np.int16)
    return down.tobytes()


def feed_voice(rt):
    pcm = load_voice_pcm()
    chunk = 640  # 20ms @16kHz s16le
    for i in range(0, min(len(pcm), 640 * 50 * 10), chunk):  # 至多 10s
        rt.send_audio(pcm[i:i + chunk])
        time.sleep(0.02)
    time.sleep(6)


def feed_silence(rt):
    silence = b"\x00" * 640
    for _ in range(50 * 20):   # 20s 静默
        rt.send_audio(silence)
        time.sleep(0.02)
    time.sleep(3)


print("=== A: 文本轮 ===", flush=True)
session("text", "文本闲聊", feed_text)
print("=== B: 语音轮 ===", flush=True)
session(None, "真实语音 10s", feed_voice)
print("=== C: 静默 20s（常开听烧钱） ===", flush=True)
session(None, "静默", feed_silence)
print("DONE", flush=True)
