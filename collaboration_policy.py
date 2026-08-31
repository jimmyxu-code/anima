"""纯函数版的人机协作输入策略。

该模块不依赖 Windows、GUI 或模型，便于在离线环境验证协作/接管边界。
事件格式为 (时间、类型、x、y)，类型为 move、mouse 或 key。
"""


def classify_real_input(events, max_coop_px=80.0, max_coop_s=1.25,
                        origin=None):
    """返回 None、cooperate 或 takeover，并对未知输入采取接管策略。"""
    if not events:
        return None
    if any(len(event) < 4 or event[1] != "move" for event in events):
        return "takeover"
    points = [(event[2], event[3]) for event in events]
    try:
        start = ((float(origin[0]), float(origin[1]))
                 if origin is not None else points[0])
    except (TypeError, ValueError, IndexError):
        return "takeover"
    max_distance = max(
        ((x - start[0]) ** 2 + (y - start[1]) ** 2) ** 0.5
        for x, y in points
    )
    duration = events[-1][0] - events[0][0]
    if max_distance <= max_coop_px and duration <= max_coop_s:
        return "cooperate"
    return "takeover"
