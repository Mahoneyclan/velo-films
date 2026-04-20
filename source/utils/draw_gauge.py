# source/utils/draw_gauge.py
"""
Arc-style gauge drawing primitives for VeloFilms telemetry overlay.

All five gauges use the same visual style:
  • 240° arc open at the bottom, spanning from lower-left (150°) to
    lower-right (390° = 30°) in PIL's clockwise-from-3-o'clock system.
  • Thick bright-green filled arc sweeping clockwise from the start,
    proportional to value in [min_val, max_val].
  • Dim dark arc for the unfilled remainder.
  • Dark semi-transparent circular background (black at ~63 % opacity).
  • Large white value text centred inside; small white unit label below.
"""

from __future__ import annotations
from PIL import Image, ImageDraw, ImageFont

# ── Colour palette ────────────────────────────────────────────────────────────
_GREEN = (0, 230, 77)       # bright green ≈ #00E64D
_DIM   = (0, 55, 22)        # dim dark green for the unfilled arc segment
_BG    = (0, 0, 0, 100)    # semi-transparent black  (alpha 100 ≈ 39 %)
_WHITE = (255, 255, 255, 255)

# ── Arc geometry ──────────────────────────────────────────────────────────────
# PIL degrees: 0° = 3 o'clock, increasing clockwise (y-axis points down).
#   150° = lower-left  (between 6 o'clock and 9 o'clock)
#   390° = lower-right (= 30°, between 3 o'clock and 6 o'clock)
# The arc sweeps 240° from lower-left → left → top → right → lower-right.
# The open gap at the bottom spans 120° (from 30° back to 150° clockwise).
_ARC_START = 150
_ARC_END   = 390   # PIL draws start→end clockwise, so 150→390 = 240°
_ARC_SPAN  = 240


# ── Helpers ───────────────────────────────────────────────────────────────────

def safe_font(size: int):
    """Load system font with fallback to PIL default."""
    try:
        return ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", size)
    except Exception:
        return ImageFont.load_default()


def _sc(base: int, gauge_size: int, ref: int = 160) -> int:
    """Scale *base* pixels proportionally to *gauge_size* (reference = 160 px)."""
    return max(1, int(base * gauge_size / ref))


# ── Core drawing function ─────────────────────────────────────────────────────

def draw_arc_gauge(
    img: Image.Image,
    rect: tuple,
    value: float,
    min_val: float,
    max_val: float,
    unit: str,
    title: str = "",
) -> None:
    """
    Draw an arc-style gauge onto *img* inside *rect* = (x, y, w, h).

    The filled green arc sweeps clockwise from the lower-left start point
    by an amount proportional to (*value* − *min_val*) / (*max_val* − *min_val*).
    An optional *title* label is drawn in white inside the open gap at the bottom.
    """
    x, y, w, h = rect
    cx = x + w // 2
    cy = y + h // 2
    gauge_size = min(w, h)

    # Geometry: leave a small margin so the arc doesn't clip the cell edges
    pad   = _sc(6, gauge_size)            # margin from rect edge to arc outer edge
    r     = gauge_size // 2 - pad         # arc centreline radius
    arc_w = max(6, _sc(10, gauge_size))   # arc stroke width (≈ 10 px at 160 px gauge)

    draw = ImageDraw.Draw(img)

    # ── Background: dark semi-transparent circle ──────────────────────────────
    bg_r = r + arc_w // 2 + 1  # cover full arc width plus 1-px border
    draw.ellipse(
        (cx - bg_r, cy - bg_r, cx + bg_r, cy + bg_r),
        fill=_BG,
    )

    # Arc bounding box (PIL centres the stroke on the bbox perimeter)
    arc_box = (cx - r, cy - r, cx + r, cy + r)

    # ── Unfilled arc (dim dark green, full 240° span) ─────────────────────────
    draw.arc(arc_box, start=_ARC_START, end=_ARC_END, fill=_DIM, width=arc_w)

    # ── Filled arc (bright green, proportional to value) ─────────────────────
    span = max_val - min_val
    frac = (value - min_val) / span if span != 0 else 0.0
    frac = max(0.0, min(frac, 1.0))
    if frac > 0.0:
        val_end = _ARC_START + _ARC_SPAN * frac
        draw.arc(arc_box, start=_ARC_START, end=val_end, fill=_GREEN, width=arc_w)

    # ── Value and unit text ───────────────────────────────────────────────────
    val_txt  = f"{int(round(value))}"
    unit_txt = unit

    val_font  = safe_font(_sc(44, gauge_size))
    unit_font = safe_font(_sc(17, gauge_size))

    val_w  = int(draw.textlength(val_txt,  font=val_font))
    unit_w = int(draw.textlength(unit_txt, font=unit_font))

    # Value height via getbbox (Pillow ≥ 8) with getsize fallback
    try:
        bb   = val_font.getbbox(val_txt)
        val_h = bb[3] - bb[1]
    except AttributeError:
        _, val_h = val_font.getsize(val_txt)  # type: ignore[attr-defined]

    # Value: centred horizontally, nudged slightly above the gauge centre
    nudge = _sc(8, gauge_size)
    val_x = cx - val_w // 2
    val_y = cy - val_h // 2 - nudge

    draw.text((val_x, val_y), val_txt,  fill=_WHITE, font=val_font)

    # Unit: centred horizontally, below the value text with clear separation
    gap    = _sc(10, gauge_size)
    unit_x = cx - unit_w // 2
    unit_y = val_y + val_h + gap

    draw.text((unit_x, unit_y), unit_txt, fill=_WHITE, font=unit_font)

    # ── Title label: sits in the open gap at the bottom of the arc ───────────
    # Arc endpoints are at 150° and 30°; their y-offset = r × sin(30°) = r × 0.5
    # Title is placed just below that point, centred horizontally.
    if title:
        title_font = safe_font(_sc(13, gauge_size))
        title_w    = int(draw.textlength(title, font=title_font))
        title_x    = cx - title_w // 2
        title_y    = cy + int(r * 0.5) + arc_w // 2 + _sc(3, gauge_size)
        draw.text((title_x, title_y), title, fill=_WHITE, font=title_font)


# ── Public gauge wrappers ─────────────────────────────────────────────────────
# Signatures are identical to the originals so call-sites need no changes.

def draw_speed_gauge(img: Image.Image, rect, value: float, min_val: float, max_val: float) -> None:
    draw_arc_gauge(img, rect, value, min_val, max_val, "km/h", "SPEED")


def draw_cadence_gauge(img: Image.Image, rect, value: float, min_val: float, max_val: float) -> None:
    draw_arc_gauge(img, rect, value, min_val, max_val, "rpm", "CADENCE")


def draw_hr_gauge(img: Image.Image, rect, value: float, min_val: float, max_val: float) -> None:
    draw_arc_gauge(img, rect, value, min_val, max_val, "bpm", "HR")


def draw_elev_gauge(img: Image.Image, rect, value: float, min_val: float, max_val: float) -> None:
    draw_arc_gauge(img, rect, value, min_val, max_val, "m", "ELEVATION")


def draw_gradient_gauge(
    img: Image.Image, rect, value: float, min_val: float, max_val: float
) -> None:
    draw_arc_gauge(img, rect, value, min_val, max_val, "%", "GRADIENT")
