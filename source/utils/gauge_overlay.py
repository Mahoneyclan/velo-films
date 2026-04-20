# source/utils/gauge_overlay.py
"""
Gauge overlay generation for HUD.
Creates PNG gauge images for speed, cadence, heart rate, elevation, gradient.
"""

from __future__ import annotations
import csv
from pathlib import Path
from typing import Dict, List
from PIL import Image

from ..config import DEFAULT_CONFIG as CFG
from .draw_gauge import (
    draw_speed_gauge,
    draw_cadence_gauge,
    draw_hr_gauge,
    draw_elev_gauge,
    draw_gradient_gauge,
)

# Exposed sizes for layout math in build.py - now read from config
# These module-level constants are kept for backward compatibility
# but actual rendering uses CFG values
SPEED_GAUGE_SIZE = 300  # Default, actual value from CFG.SPEED_GAUGE_SIZE
SMALL_GAUGE_SIZE = 150  # Default, actual value from CFG.SMALL_GAUGE_SIZE

def compute_gauge_ranges(csv_path: Path) -> Dict[str, tuple]:
    """
    Compute (display_min, display_max) for each gauge from ride data.

    Scans the CSV for actual data range per gauge, then applies a ±10% buffer:
      display_min = data_min × 0.9  (× 1.1 if data_min is negative, to widen the range)
      display_max = data_max × 1.1

    Special rules:
      - Speed and cadence: display_min floored at 0 (physically can't be negative)
      - Gradient: kept symmetric around 0 using the wider absolute bound
      - All ranges are fully data-driven; no hard ceilings applied

    Falls back to safe defaults when a gauge has no data in the CSV.
    """
    import math

    INF = float("inf")
    field_map = {
        "speed":    "speed_kmh",
        "cadence":  "cadence_rpm",
        "hr":       "hr_bpm",
        "elev":     "elevation",
        "gradient": "gradient_pct",
    }
    raw_min = {k: INF  for k in field_map}
    raw_max = {k: -INF for k in field_map}
    has_data = {k: False for k in field_map}

    try:
        with csv_path.open() as f:
            for r in csv.DictReader(f):
                for gauge_type, field in field_map.items():
                    raw = r.get(field, "")
                    if raw and raw.strip():
                        try:
                            v = float(raw)
                            has_data[gauge_type] = True
                            if v < raw_min[gauge_type]:
                                raw_min[gauge_type] = v
                            if v > raw_max[gauge_type]:
                                raw_max[gauge_type] = v
                        except ValueError:
                            pass
    except Exception:
        pass

    # Fallback defaults when no data found for a gauge — read from config caps
    _gcap = CFG.GAUGE_MAXES
    defaults = {
        "speed":    (0.0,  float(_gcap.get("speed",    60))),
        "cadence":  (0.0,  float(_gcap.get("cadence",  120))),
        "hr":       (40.0, float(_gcap.get("hr",        160))),
        "elev":     (0.0,  float(_gcap.get("elev",     5000))),
        "gradient": (-float(_gcap.get("gradient_max", 10)), float(_gcap.get("gradient_max", 10))),
    }

    ranges: Dict[str, tuple] = {}
    for k in field_map:
        if not has_data[k] or raw_max[k] == -INF:
            ranges[k] = defaults[k]
            continue

        data_min = raw_min[k]
        data_max = raw_max[k]

        # ±10% buffer — widen toward the extreme on each end
        display_min = data_min * 0.9 if data_min >= 0 else data_min * 1.1
        display_max = data_max * 1.1 if data_max >= 0 else data_max * 0.9

        ranges[k] = (display_min, display_max)

    # Speed and cadence: floor min at 0
    for k in ("speed", "cadence"):
        lo, hi = ranges[k]
        ranges[k] = (max(0.0, lo), hi)

    # Gradient: symmetric around 0 using the wider absolute bound
    lo, hi = ranges["gradient"]
    abs_bound = max(abs(lo), abs(hi))
    ranges["gradient"] = (-abs_bound, abs_bound)

    # No caps — all ranges are fully data-driven with ±10% buffer

    return ranges

def create_all_gauge_images(
    telemetry: Dict[str, List[float]],
    gauge_ranges: Dict[str, tuple],
    base_dir: Path,
    clip_idx: int,
) -> Dict[str, Path]:
    """
    Create gauge images for all telemetry types and return dict of paths.
    base_dir is a per-clip folder under GAUGE_DIR, created by build.py.
    gauge_ranges: dict of gauge_type -> (display_min, display_max) from compute_gauge_ranges().
    """
    out: Dict[str, Path] = {}
    base_dir.mkdir(parents=True, exist_ok=True)

    speed_size = CFG.SPEED_GAUGE_SIZE
    small_size = CFG.SMALL_GAUGE_SIZE

    _gcap = CFG.GAUGE_MAXES
    _defaults = {
        "speed":    (0.0,  float(_gcap.get("speed",    60))),
        "cadence":  (0.0,  float(_gcap.get("cadence",  120))),
        "hr":       (40.0, float(_gcap.get("hr",        160))),
        "elev":     (0.0,  float(_gcap.get("elev",     5000))),
        "gradient": (-float(_gcap.get("gradient_max", 10)), float(_gcap.get("gradient_max", 10))),
    }

    for gtype, values in telemetry.items():
        if not values:
            continue
        val = float(values[0] or 0.0)
        lo, hi = gauge_ranges.get(gtype, _defaults.get(gtype, (0.0, 100.0)))

        if gtype == "speed":
            size = speed_size
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            draw_speed_gauge(img, (0, 0, size, size), val, lo, hi)
        elif gtype == "cadence":
            size = small_size
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            draw_cadence_gauge(img, (0, 0, size, size), val, lo, hi)
        elif gtype == "hr":
            size = small_size
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            draw_hr_gauge(img, (0, 0, size, size), val, lo, hi)
        elif gtype == "elev":
            size = small_size
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            draw_elev_gauge(img, (0, 0, size, size), val, lo, hi)
        elif gtype == "gradient":
            size = small_size
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            draw_gradient_gauge(img, (0, 0, size, size), val, lo, hi)
        else:
            continue

        fp = base_dir / f"gauge_{gtype}.png"
        img.save(fp)
        out[gtype] = fp

    return out