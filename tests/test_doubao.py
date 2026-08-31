import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
r"""豆包 TTS 驱动联调用例：key 从 config.json 读，不硬编码。
用法: python test_doubao.py  → 合成一句并存 C:\tmp\doubao_test.mp3
"""
import io
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根（config.json 在根）
sys.path.insert(0, BASE)
import tts_engines

with io.open(os.path.join(BASE, "config.json"), "r", encoding="utf-8-sig") as f:
    cfg = json.load(f)

TEXT = "你好，我是小凯。豆包语音合成已经接通了，以后这就是我的新声音。"
chunks = []
t0 = time.time()
ok = tts_engines._synth_doubao(TEXT, cfg, lambda b: chunks.append((time.time() - t0, b)), log=print)
print("driver:", "OK" if ok else "FAILED",
      "| chunks:", len(chunks),
      "| first: %.2fs" % (chunks[0][0] if chunks else -1),
      "| total: %.2fs" % (time.time() - t0))
if ok:
    with io.open(r"C:\tmp\doubao_test.mp3", "wb") as f:
        f.write(b"".join(b for _, b in chunks))
    print("saved C:\\tmp\\doubao_test.mp3")
