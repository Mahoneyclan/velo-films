# source/steps/build_helpers/gauge_renderer.py
"""
Gauge rendering for HUD overlays.
Creates telemetry gauge images for speed, cadence, heart rate, elevation, gradient.
"""

from __future__ import annotations
from pathlib import Path
from typing import Dict

from ...config import DEFAULT_CONFIG as CFG
from ...utils.log import setup_logger
from ...utils import gauge_overlay
from ...io_paths import _mk

log = setup_logger("steps.build_helpers.gauge_renderer")


class GaugeRenderer:
    """Renders telemetry gauges for HUD overlay."""

    # Expose sizes for layout calculations - read from config
    @property
    def SPEED_GAUGE_SIZE(self):
        return CFG.SPEED_GAUGE_SIZE

    @property
    def SMALL_GAUGE_SIZE(self):
        return CFG.SMALL_GAUGE_SIZE
    
    def __init__(self, output_dir: Path, select_csv_path: Path):
        """
        Args:
            output_dir: Base directory for gauge images
            select_csv_path: Path to select.csv for computing maxes
        """
        self.output_dir = _mk(output_dir)
        self.gauge_ranges = gauge_overlay.compute_gauge_ranges(select_csv_path)
        log.debug(f"[gauge] Computed gauge ranges: {self.gauge_ranges}")
    
    def render_gauges_for_clip(self, row: Dict, clip_idx: int) -> Dict[str, Path]:
        """
        Render all gauge types for a single clip.

        Args:
            row: Clip metadata with telemetry data
            clip_idx: Clip index number

        Returns:
            Dict mapping gauge_type → image_path
        """

        # Extract telemetry values (clean, minimal schema)
        telemetry = {
            "speed": [float(row.get("speed_kmh") or 0.0)],
            "cadence": [float(row.get("cadence_rpm") or 0.0)],
            "hr": [float(row.get("hr_bpm") or 0.0)],
            "elev": [float(row.get("elevation") or 0.0)],
            "gradient": [float(row.get("gradient_pct") or 0.0)],
        }

        # Create clip-specific gauge directory
        clip_gauge_dir = _mk(self.output_dir / f"clip_{clip_idx:04d}")

        try:
            gauge_images = gauge_overlay.create_all_gauge_images(
                telemetry,
                self.gauge_ranges,
                clip_gauge_dir,
                clip_idx
            )
            return gauge_images

        except Exception as e:
            log.error(f"[gauge] Failed to create gauges for clip {clip_idx}: {e}")
            return {}


    def calculate_gauge_positions(
        self,
        padding: tuple[int, int]
    ) -> Dict[str, tuple[str, str]]:
        """
        Calculate HUD gauge positions for ffmpeg overlay.
        
        Args:
            padding: (x_padding, y_padding) from config
            
        Returns:
            Dict mapping gauge_type → (x_expr, y_expr) for ffmpeg
        """
        gx, gy = padding
        SPACING = 20
        OVERLAP = 15
        
        speed = self.SPEED_GAUGE_SIZE
        small = self.SMALL_GAUGE_SIZE
        
        # Speed gauge bottom-left anchor
        speed_x = gx + small - OVERLAP + SPACING
        speed_y = f"H - {gy} - {speed}"
        
        # Small gauges clustered around speed
        top_y = f"{speed_y}"
        bottom_y = f"{speed_y} + {speed - small}"
        right_x = f"{speed_x} + {speed - OVERLAP} + {SPACING}"
        left_x = f"{speed_x} - {small} + {OVERLAP}"
        
        return {
            "speed": (f"{speed_x}", f"H - h - {gy}"),
            "hr": (left_x, top_y),
            "cadence": (right_x, top_y),
            "elev": (left_x, bottom_y),
            "gradient": (right_x, bottom_y),
        }