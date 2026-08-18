# -*- coding: utf-8 -*-
"""Chinese text overlay helper for the YOLO detection window.

OpenCV's built-in Hershey fonts cannot render CJK characters, so Chinese
labels are drawn with PIL using a Windows Chinese font (微软雅黑 / 黑体 etc.).
When no CJK font is available the caller's English fallback text is drawn
with cv2.putText instead, so the window always shows something readable.
"""

from __future__ import annotations

import threading

import cv2
import numpy as np

_FONT_PATHS = (
    r"C:\Windows\Fonts\msyh.ttc",       # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",     # 黑体
    r"C:\Windows\Fonts\simsun.ttc",     # 宋体
)
_lock = threading.Lock()
_loaded: dict[int, object] = {}


def _pil_font(pixel_size: int):
    """Load (and cache) a PIL truetype font for the given pixel height."""

    if pixel_size in _loaded:
        return _loaded[pixel_size]
    with _lock:
        if pixel_size in _loaded:
            return _loaded[pixel_size]
        font = None
        try:
            from PIL import ImageFont

            for path in _FONT_PATHS:
                try:
                    font = ImageFont.truetype(path, pixel_size)
                    break
                except OSError:
                    continue
        except Exception:
            font = None
        _loaded[pixel_size] = font
        return font


def put_cn(
    img: np.ndarray,
    text_cn: str,
    text_en: str,
    org,
    scale: float = 0.8,
    color=(255, 255, 255),
    thickness: int = 2,
) -> None:
    """Draw ``text_cn`` with a CJK font, or ``text_en`` via cv2 as fallback."""

    try:
        from PIL import Image, ImageDraw

        pixel = max(10, int(round(scale * 32)))
        font = _pil_font(pixel)
        if font is not None:
            pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            draw = ImageDraw.Draw(pil_img)
            draw.text((int(org[0]), int(org[1])), text_cn,
                      font=font, fill=(int(color[2]), int(color[1]), int(color[0])))
            img[:] = cv2.cvtColor(np.asarray(pil_img), cv2.COLOR_RGB2BGR)
            return
    except Exception:
        pass
    cv2.putText(img, text_en, tuple(int(v) for v in org),
                cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness)
