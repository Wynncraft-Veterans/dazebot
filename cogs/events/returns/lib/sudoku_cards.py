"""Result cards for the daily sudoku, pure PIL. Call through ``asyncio.to_thread``.

Each card's heatmap shows when each cell was last set and no digits, so it
reveals only clue positions (the same for everyone) and solve order.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Optional

from PIL import Image, ImageDraw

from cogs.events.returns.lib.sudoku import format_time
from lib.fonts import load_font

CARD_W = 430
CARD_H = 134
GAP = 10
PAD = 12
AVATAR = 56
CELL = 11
CELL_GAP = 1
BOX_GAP = 2
HEATMAP = 9 * CELL + 8 * CELL_GAP + 2 * BOX_GAP

CANVAS_BG = (30, 31, 34)
CARD_BG = (43, 45, 49)
TEXT = (242, 243, 245)
MUTED = (181, 186, 193)
CROWN = (240, 178, 50)
AVATAR_FALLBACK = (88, 91, 98)
GIVEN = (24, 25, 28)

# Single hue (blue), so brightness alone carries the solve order: early cells
# are dark, the last cell is brightest. Every channel rises, so luminance does.
RAMP_LOW = (24, 62, 96)
RAMP_HIGH = (190, 225, 255)


@dataclass
class Card:
    name: str
    seconds: int
    streak: int
    fills: list[int]
    crowned: bool = False
    avatar: Optional[bytes] = None


def ramp(t: float) -> tuple[int, int, int]:
    """The heatmap colour at ``t`` in [0, 1]."""
    return tuple(round(lo + (hi - lo) * t) for lo, hi in zip(RAMP_LOW, RAMP_HIGH))


def _fit(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


def _paste_avatar(canvas: Image.Image, avatar: Optional[bytes], x: int, y: int) -> None:
    disc = Image.new("L", (AVATAR * 4, AVATAR * 4), 0)
    ImageDraw.Draw(disc).ellipse((0, 0, AVATAR * 4 - 1, AVATAR * 4 - 1), fill=255)
    mask = disc.resize((AVATAR, AVATAR), Image.LANCZOS)
    face = Image.new("RGB", (AVATAR, AVATAR), AVATAR_FALLBACK)
    if avatar is not None:
        try:
            decoded = Image.open(BytesIO(avatar)).convert("RGBA").resize((AVATAR, AVATAR), Image.LANCZOS)
        except (OSError, ValueError):
            pass  # undecodable: the plain disc stands in
        else:
            face.paste(decoded, mask=decoded)  # the plain disc also shows through transparency
    canvas.paste(face, (x, y), mask)


def _draw_crown(draw: ImageDraw.ImageDraw, cx: int, base_y: int) -> None:
    w, h = 22, 14
    left = cx - w // 2
    draw.polygon(
        [
            (left, base_y),
            (left, base_y - h),
            (left + w // 4, base_y - h // 2),
            (cx, base_y - h),
            (left + w - w // 4, base_y - h // 2),
            (left + w, base_y - h),
            (left + w, base_y),
        ],
        fill=CROWN,
    )


def _draw_heatmap(draw: ImageDraw.ImageDraw, card: Card, x: int, y: int) -> None:
    for i, fill in enumerate(card.fills):
        row, col = divmod(i, 9)
        left = x + col * (CELL + CELL_GAP) + col // 3 * BOX_GAP
        top = y + row * (CELL + CELL_GAP) + row // 3 * BOX_GAP
        colour = GIVEN if fill == 0 else ramp(fill / card.seconds)
        draw.rectangle((left, top, left + CELL - 1, top + CELL - 1), fill=colour)


def _draw_card(canvas: Image.Image, draw: ImageDraw.ImageDraw, card: Card, x: int, y: int) -> None:
    draw.rounded_rectangle((x, y, x + CARD_W - 1, y + CARD_H - 1), radius=8, fill=CARD_BG)
    avatar_x, avatar_y = x + PAD, y + (CARD_H - AVATAR) // 2 + 6
    _paste_avatar(canvas, card.avatar, avatar_x, avatar_y)
    if card.crowned:
        _draw_crown(draw, avatar_x + AVATAR // 2, avatar_y - 3)

    text_x = avatar_x + AVATAR + PAD
    heat_x = x + CARD_W - PAD - HEATMAP
    width = heat_x - text_x - PAD
    name_font, time_font, streak_font = load_font(18), load_font(28), load_font(14)
    draw.text((text_x, y + 24), _fit(draw, card.name, name_font, width), font=name_font, fill=TEXT)
    draw.text((text_x, y + 50), format_time(card.seconds), font=time_font, fill=TEXT)
    draw.text((text_x, y + 88), f"{card.streak} day streak", font=streak_font, fill=MUTED)
    _draw_heatmap(draw, card, heat_x, y + (CARD_H - HEATMAP) // 2)


def render_cards(cards: list[Card]) -> bytes:
    """A PNG of ``cards`` (the caller caps them): one column for one, else two."""
    cols = min(2, len(cards))
    rows = (len(cards) + cols - 1) // cols
    canvas = Image.new(
        "RGB",
        (cols * CARD_W + (cols + 1) * GAP, rows * CARD_H + (rows + 1) * GAP),
        CANVAS_BG,
    )
    draw = ImageDraw.Draw(canvas)
    for i, card in enumerate(cards):
        row, col = divmod(i, cols)
        _draw_card(canvas, draw, card, GAP + col * (CARD_W + GAP), GAP + row * (CARD_H + GAP))
    out = BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()
