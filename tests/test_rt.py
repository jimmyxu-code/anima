import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
"""doubao_rt 协议冒烟测试：建连 → StartSession → ChatTextQuery → 看事件流。
用法: python test_rt.py
"""
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import doubao_rt

APP_ID = "3596007629"
import secrets_store
ACCESS_KEY = secrets_store.get_secret("doubao")  # 控制台 API Key

audio_chunks = []
events = []


def on_audio(b):
    audio_chunks.append(b)


def on_event(ev, obj):
    events.append((ev, obj))
    print(f"EVENT {ev}: {json.dumps(obj, ensure_ascii=False)[:120]}", flush=True)


def on_error(m):
    print(f"ERROR: {m}", flush=True)


CONFIG = {
    "asr": {"audio_info": {"format": "pcm", "sample_rate": 16000, "channel": 1},
            "extra": {"end_smooth_window_ms": 800}},
    "tts": {"speaker": "zh_female_vv_jupiter_bigtts",
            "audio_config": {"channel": 1, "format": "pcm_s16le",
                             "sample_rate": 24000},
            "extra": {}},
    "dialog": {"bot_name": "小凯",
               "system_role": "你是用户的电脑伙伴小凯，像好朋友一样用口语聊天，回复简短自然。",
               "speaking_style": "轻松自然的口语，像朋友聊天",
               "extra": {"model": "1.2.1.1", "input_mod": "text"}},
}


def main():
    rt = doubao_rt.DoubaoRT(APP_ID, ACCESS_KEY, on_audio=on_audio,
                            on_event=on_event, on_error=on_error)
    print("connecting...", flush=True)
    rt.start()
    print("connected, start_session...", flush=True)
    rt.start_session(CONFIG)
    time.sleep(1.5)
    print("chat_text 你好", flush=True)
    rt.chat_text("你好，随便说两句")
    t0 = time.time()
    while time.time() - t0 < 15 and not any(e == doubao_rt.EV_TTS_ENDED for e, _ in events):
        time.sleep(0.2)
    rt.finish_session()
    rt.stop()
    total = sum(len(c) for c in audio_chunks)
    print(f"audio chunks: {len(audio_chunks)}, bytes: {total} "
          f"(~{total / 2 / 24000:.1f}s)")
    if total:
        with io.open(r"C:\tmp\rt_reply.pcm", "wb") as f:
            f.write(b"".join(audio_chunks))
        print("saved C:\\tmp\\rt_reply.pcm")
    sys.stdout.flush()


main()
