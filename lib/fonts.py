"""TrueType font loading for the Pillow card renderers."""

from __future__ import annotations

import os

from PIL import ImageFont

# TrueType font search order. First hit wins. fonts-dejavu-core (installed
# via the vets-deploy Dockerfile) provides the Linux path; Windows dev has
# Arial. If none are found we fall back to Pillow's bundled default which
# is legible in Pillow 10+ (the size argument routes through a scalable
# vector font, not the tiny 8px bitmap of older releases).
_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)

_FONT_CACHE: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Try ``_FONT_PATHS`` in order; cache per size; fall back to Pillow's
    bundled default (which in Pillow 10+ is a scalable TrueType that
    accepts a ``size`` argument — legible, unlike the tiny bitmap of older
    releases).
    """
    cached = _FONT_CACHE.get(size)
    if cached is not None:
        return cached
    for path in _FONT_PATHS:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size)
                _FONT_CACHE[size] = font
                return font
            except OSError:
                continue
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10 doesn't accept size on load_default
        font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font
