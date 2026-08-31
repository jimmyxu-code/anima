# -*- coding: utf-8 -*-
"""GUI provider 的无状态图像编码工具。"""

import base64
import io


def b64_jpeg(img, width=1280, quality=70):
    source_w, source_h = img.size
    height = int(source_h * width / source_w)
    resized = img.convert("RGB").resize((width, height))
    buf = io.BytesIO()
    resized.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode(), width, height

