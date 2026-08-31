# -*- coding: utf-8 -*-
"""wake_word：本地唤醒词监听（sherpa-onnx KWS，纯 CPU 3.3M 小模型）。

只在 config.json 的 wake_mode 为 word/both 时开麦常听（默认 press=只长按，
不开常开麦）。检测到"小凯小凯"→ 回调（companion_rt 挂 sense_io.open_session）。
模型：models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01
"""

import os
import threading
import time

import numpy as np
import sounddevice as sd
import sherpa_onnx

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODEL = os.path.join(_HERE, "models",
                      "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01")
_KEYWORDS = os.path.join(_MODEL, "keywords_custom.txt")


class WakeWordListener:
    """后台线程常听唤醒词。start()/stop() 幂等。"""

    def __init__(self, on_wake, log=print, keywords_file=_KEYWORDS,
                 threshold=0.08, input_gain=2.0, silent_reopen_seconds=30.0,
                 pause_check=None):
        self._on_wake = on_wake
        self._log = log
        self._pause_check = pause_check  # 返回 True 时只听不解码（会话进行中让麦）
        self._input_gain = max(1.0, min(float(input_gain), 4.0))
        self._silent_reopen_seconds = max(12.0, float(silent_reopen_seconds))
        self._running = threading.Event()
        self._stream = None
        self._thread = None
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=os.path.join(_MODEL, "tokens.txt"),
            encoder=os.path.join(
                _MODEL, "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            decoder=os.path.join(
                _MODEL, "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            joiner=os.path.join(
                _MODEL, "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            keywords_file=keywords_file,
            keywords_threshold=threshold,
            num_threads=2,
        )
        self._sample_rate = int(self._spotter.sample_rate) \
            if hasattr(self._spotter, "sample_rate") else 16000

    def _loop(self):
        block = 1600  # 100ms @16k
        # 看门狗（2026-08-28 用户实锤"唤醒不灵"）：蓝牙耳机切换等会把输入
        # 设备拆了（MME error 6），旧实现 except 后线程静默死到重启进程。
        # 现在：崩了退避重连（1s→2s→…封顶 10s），设备回来即自愈。
        backoff = 1.0
        while self._running.is_set():
            try:
                # 只跟随 Windows 当前默认输入设备。项目历史已实锤：自行枚举/钉麦
                # 会钉到“能开流但全是电气零”的端点，造成唤醒时灵时不灵。
                stream = self._spotter.create_stream()  # 每次重连也重置 KWS 状态
                with sd.InputStream(device=None, samplerate=self._sample_rate,
                                    channels=1, dtype="float32",
                                    blocksize=block) as mic:
                    self._stream = mic
                    backoff = 1.0
                    self._log("唤醒词监听已开（常开麦：本地 KWS，音频不出本机）")
                    silent_since = None
                    while self._running.is_set():
                        data, _overflowed = mic.read(block)
                        peak = float(np.abs(data).max()) if data.size else 0.0
                        if self._pause_check and self._pause_check():
                            # 会话开着：排空缓冲但不解码（防双路解码抢麦/误触）
                            silent_since = None
                            continue
                        # 电气零静默看门狗（2026-08-30 实锤"时灵时不灵"：蓝牙
                        # HFP 翻转的坑态是回调照发全是静音——流"活着"但收零。
                        # Windows 降噪在安静房间也会压成零，旧 8s 阈值因此制造
                        # 周期性重开空窗；默认放宽到 30s，真异常仍由 except 秒重连。
                        if peak < 0.0005:
                            if silent_since is None:
                                silent_since = time.time()
                            elif time.time() - silent_since > self._silent_reopen_seconds:
                                self._log(
                                    f"唤醒词监听：输入静默 {self._silent_reopen_seconds:.0f}s，"
                                    "重开流自愈")
                                break
                        else:
                            silent_since = None
                        samples = data[:, 0].astype(np.float32)
                        # 输入增益（默认 ×2，可配置）：小声唤醒也能过 KWS；
                        # 截幅防爆音。阈值默认 0.08，比旧 0.10 更敏感。
                        samples = np.clip(samples * self._input_gain, -1.0, 1.0)
                        stream.accept_waveform(self._sample_rate, samples)
                        while self._spotter.is_ready(stream):
                            self._spotter.decode_stream(stream)
                        result = self._spotter.get_result(stream)
                        if result:
                            self._log(f"唤醒词命中: {result}")
                            self._spotter.reset_stream(stream)
                            try:
                                self._on_wake()
                            except Exception as e:
                                self._log(f"唤醒回调异常: {e}")
            except Exception as e:
                self._stream = None
                if not self._running.is_set():
                    break
                self._log(f"唤醒词监听异常（{backoff:.0f}s 后自动重连）: {e}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 3.0)   # 退避封顶 3s（断流窗口越小越灵）
        self._stream = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running.set()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="wake-word")
        self._thread.start()

    def stop(self):
        self._running.clear()
        # 主动关流以唤醒阻塞中的 mic.read；“退下”不能留着监听线程/麦克风。
        mic = self._stream
        if mic is not None:
            try:
                mic.abort()
                mic.close()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)


def make_listener_if_enabled(cfg_get, on_wake, log=print, pause_check=None):
    """按 config 决定是否建监听器。
    cfg_get: 读 config.json 的函数（wake_mode: press/word/both）。
    pause_check: 返回 True 时暂停解码（会话进行中）。
    返回 WakeWordListener 或 None。"""
    mode = str(cfg_get("wake_mode", "press")).lower()
    if mode not in ("word", "both"):
        log(f"唤起方式: 长按（wake_mode={mode}，常开麦未启用）")
        return None
    try:
        threshold = float(cfg_get("wake_threshold", 0.08))
        gain = float(cfg_get("wake_input_gain", 2.0))
        silent_reopen = float(cfg_get("wake_silent_reopen_seconds", 30.0))
    except (TypeError, ValueError):
        threshold, gain, silent_reopen = 0.08, 2.0, 30.0
    listener = WakeWordListener(on_wake=on_wake, log=log,
                                threshold=threshold, input_gain=gain,
                                silent_reopen_seconds=silent_reopen,
                                pause_check=pause_check)
    listener.start()
    log(f"唤起方式: 长按+唤醒词（wake_mode={mode}）")
    return listener
