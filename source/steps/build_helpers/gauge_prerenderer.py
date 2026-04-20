# source/steps/build_helpers/gauge_prerenderer.py
"""
Pre-render composite gauge overlays for all clips.

Supports two modes:
- Static: Single PNG per clip (original behavior)
- Dynamic: Per-second PNGs compiled into video for live gauge updates

Dynamic mode renders static backgrounds once, then composites needle/value
for each second, creating smooth gauge animations.
"""

from __future__ import annotations
import math
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont

from ...config import DEFAULT_CONFIG as CFG
from ...utils.log import setup_logger
from ...utils.hardware import get_worker_count
from ...utils.draw_gauge import (
    draw_speed_gauge,
    draw_cadence_gauge,
    draw_hr_gauge,
    draw_elev_gauge,
    draw_gradient_gauge,
)
from ...utils.gauge_overlay import compute_gauge_ranges
from ...io_paths import _mk, select_path, flatten_path
from ...utils.progress_reporter import report_progress

log = setup_logger("steps.build_helpers.gauge_prerenderer")


class GaugePrerenderer:
    """Pre-renders composite gauge overlays for all selected clips.

    Supports dynamic per-second gauge updates with static background optimization.
    """

    def __init__(self, output_dir: Path, dynamic_mode: bool = True):
        """
        Args:
            output_dir: Directory to save gauge images/videos
            dynamic_mode: If True, render per-second gauge videos; if False, static PNGs
        """
        self.output_dir = _mk(output_dir)
        self.gauge_ranges = compute_gauge_ranges(select_path())
        self.dynamic_mode = dynamic_mode

        # Composite canvas size (matches PIP)
        self.width, self.height = CFG.GAUGE_COMPOSITE_SIZE
        self.layout = CFG.GAUGE_LAYOUT
        self.enabled = CFG.ENABLED_GAUGES

        # Clip duration for per-second rendering
        self.clip_duration = CFG.CLIP_OUT_LEN_S

        # Load telemetry timeline for per-second lookups
        self.telemetry_timeline = self._load_telemetry_timeline() if dynamic_mode else []

        # Cache for static gauge backgrounds (dial, ticks, labels - no needle/value)
        self._background_cache: Dict[str, Image.Image] = {}

    def _load_telemetry_timeline(self) -> List[Dict]:
        """Load telemetry from flatten.csv for per-second lookups."""
        fp = flatten_path()
        if not fp.exists():
            log.warning("[gauge] flatten.csv missing; dynamic gauges will use static values")
            return []

        import csv
        points = []
        try:
            with fp.open() as f:
                reader = csv.DictReader(f)
                for r in reader:
                    try:
                        epoch = float(r.get("gpx_epoch") or 0.0)
                        points.append({
                            "epoch": epoch,
                            "speed": r.get("speed_kmh", ""),
                            "cadence": r.get("cadence_rpm", ""),
                            "hr": r.get("hr_bpm", ""),
                            "elev": r.get("elevation", ""),
                            "gradient": r.get("gradient_pct", ""),
                        })
                    except (ValueError, TypeError):
                        continue
            log.info(f"[gauge] Loaded {len(points)} telemetry points for dynamic gauges")
        except Exception as e:
            log.error(f"[gauge] Failed to load telemetry: {e}")
        return sorted(points, key=lambda p: p["epoch"])

    def _lookup_telemetry(self, epoch: float) -> Dict[str, Optional[float]]:
        """Look up telemetry values at a given epoch timestamp.

        Returns dict with gauge_type -> value (or None if unavailable).
        """
        if not self.telemetry_timeline:
            return {}

        # Binary search for nearest point
        from bisect import bisect_left
        epochs = [p["epoch"] for p in self.telemetry_timeline]
        idx = bisect_left(epochs, epoch)

        # Find closest point within 2 seconds
        best = None
        best_dt = float("inf")
        for offset in (-1, 0, 1):
            i = idx + offset
            if 0 <= i < len(self.telemetry_timeline):
                pt = self.telemetry_timeline[i]
                dt = abs(pt["epoch"] - epoch)
                if dt <= 2.0 and dt < best_dt:
                    best = pt
                    best_dt = dt

        if not best:
            return {}

        # Extract values, converting to float where available
        result = {}
        for gauge_type in ["speed", "cadence", "hr", "elev", "gradient"]:
            raw = best.get(gauge_type, "")
            if self._is_value_available(raw):
                result[gauge_type] = float(raw)
        return result

    def prerender_all(self, rows: List[Dict]) -> Dict[int, Path]:
        """
        Pre-render gauge overlays for all clips.

        Args:
            rows: List of clip metadata dicts from select.csv

        Returns:
            Dict mapping clip_idx -> gauge_path (PNG or video depending on mode)
        """
        num_workers = get_worker_count('io')
        mode_str = "dynamic (per-second)" if self.dynamic_mode else "static"
        log.info(
            f"[gauge] Pre-rendering {len(rows)} {mode_str} gauge overlays "
            f"({self.width}x{self.height}px, layout={self.layout}) "
            f"with {num_workers} workers..."
        )
        paths: Dict[int, Path] = {}

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {
                executor.submit(self._render_clip_gauges, row, idx): idx
                for idx, row in enumerate(rows, start=1)
            }

            completed = 0
            for future in as_completed(futures, timeout=600):  # 10 min timeout
                idx = futures[future]
                completed += 1
                try:
                    result = future.result(timeout=60)  # 60s per clip
                    if result:
                        paths[idx] = result
                except TimeoutError:
                    log.warning(f"[gauge] Timeout rendering gauges for clip {idx}")
                except Exception as e:
                    log.warning(f"[gauge] Failed to render gauges for clip {idx}: {e}")

                if completed % 10 == 0 or completed == len(rows):
                    report_progress(
                        completed, len(rows),
                        f"Rendered {completed}/{len(rows)} gauge overlays"
                    )

        log.info(f"[gauge] Successfully rendered {len(paths)} gauge overlays")
        return paths

    def _render_clip_gauges(self, row: Dict, idx: int) -> Optional[Path]:
        """Render gauge overlay for a single clip.

        In dynamic mode: generates per-second PNGs and compiles to video.
        In static mode: generates single PNG.
        """
        if self.dynamic_mode:
            return self._render_dynamic_gauges(row, idx)
        else:
            return self._render_static_gauge(row, idx)

    def _render_static_gauge(self, row: Dict, idx: int) -> Optional[Path]:
        """Render single static composite gauge for a clip (original behavior)."""
        # Extract telemetry with null detection
        telemetry = self._extract_telemetry(row)
        available_gauges = [g for g in self.enabled if g in telemetry]

        if not available_gauges:
            log.debug(f"[gauge] Clip {idx}: No telemetry data available, skipping gauge overlay")
            return None

        # Create and save composite
        canvas = self._render_gauge_composite(telemetry, available_gauges)
        out_path = self.output_dir / f"gauge_composite_{idx:04d}.png"
        canvas.save(out_path)

        # Log hidden gauges
        hidden = set(self.enabled) - set(available_gauges)
        if hidden:
            log.debug(f"[gauge] Clip {idx}: Hidden gauges (no data): {', '.join(sorted(hidden))}")

        return out_path

    def _render_dynamic_gauges(self, row: Dict, idx: int) -> Optional[Path]:
        """Render per-second gauge PNGs and compile to video."""
        # Use gpx_epoch (matches flatten.csv index)
        try:
            clip_epoch = float(row.get("gpx_epoch") or 0.0)
        except (ValueError, TypeError):
            log.warning(f"[gauge] Clip {idx}: Invalid gpx_epoch, falling back to static")
            return self._render_static_gauge(row, idx)

        # Calculate number of seconds to render (round up)
        num_seconds = int(math.ceil(self.clip_duration)) + 1

        # Create temp directory for per-second PNGs
        temp_dir = self.output_dir / f"_temp_clip_{idx:04d}"
        temp_dir.mkdir(exist_ok=True)

        png_paths = []
        any_data = False

        for sec in range(num_seconds):
            # Look up telemetry at this second
            epoch = clip_epoch + sec
            telemetry = self._lookup_telemetry(epoch)

            # Fall back to row data if timeline lookup fails
            if not telemetry:
                telemetry = self._extract_telemetry(row)

            available_gauges = [g for g in self.enabled if g in telemetry]

            if available_gauges:
                any_data = True
                canvas = self._render_gauge_composite(telemetry, available_gauges)
            else:
                # Transparent frame if no data
                canvas = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))

            png_path = temp_dir / f"gauge_{sec:02d}.png"
            canvas.save(png_path)
            png_paths.append(png_path)

        if not any_data:
            # Clean up temp files
            for p in png_paths:
                p.unlink(missing_ok=True)
            temp_dir.rmdir()
            log.debug(f"[gauge] Clip {idx}: No telemetry data available, skipping gauge overlay")
            return None

        # Compile PNGs to video (1 fps, duration matches clip)
        video_path = self.output_dir / f"gauge_video_{idx:04d}.mov"
        success = self._compile_gauge_video(temp_dir, video_path, num_seconds)

        # Clean up temp files
        for p in png_paths:
            p.unlink(missing_ok=True)
        temp_dir.rmdir()

        if success:
            return video_path
        else:
            log.warning(f"[gauge] Clip {idx}: Video compilation failed, falling back to static")
            return self._render_static_gauge(row, idx)

    def _compile_gauge_video(self, png_dir: Path, output_path: Path, num_frames: int) -> bool:
        """Compile per-second PNGs into a video with transparency."""
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-framerate", "1",  # 1 fps
            "-i", str(png_dir / "gauge_%02d.png"),
            "-c:v", "prores_ks",  # ProRes for alpha channel support
            "-profile:v", "4444",  # ProRes 4444 supports alpha
            "-pix_fmt", "yuva444p10le",
            "-t", f"{self.clip_duration:.3f}",
            str(output_path)
        ]

        try:
            subprocess.run(cmd, check=True, capture_output=True)
            return output_path.exists()
        except subprocess.CalledProcessError as e:
            log.warning(f"[gauge] FFmpeg error: {e.stderr.decode() if e.stderr else 'unknown'}")
            return False

    def _render_gauge_composite(
        self,
        telemetry: Dict[str, float],
        available_gauges: List[str]
    ) -> Image.Image:
        """Render composite gauge image with only available gauges."""
        canvas = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        positions = self._calculate_positions()

        for gauge_type in available_gauges:
            if gauge_type not in positions:
                continue

            x, y, size = positions[gauge_type]
            value = telemetry.get(gauge_type, 0.0)
            lo, hi = self.gauge_ranges.get(gauge_type, (0.0, 100.0))

            # Create gauge image
            gauge_img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            rect = (0, 0, size, size)

            if gauge_type == "speed":
                draw_speed_gauge(gauge_img, rect, value, lo, hi)
            elif gauge_type == "cadence":
                draw_cadence_gauge(gauge_img, rect, value, lo, hi)
            elif gauge_type == "hr":
                draw_hr_gauge(gauge_img, rect, value, lo, hi)
            elif gauge_type == "elev":
                draw_elev_gauge(gauge_img, rect, value, lo, hi)
            elif gauge_type == "gradient":
                draw_gradient_gauge(gauge_img, rect, value, lo, hi)

            canvas.paste(gauge_img, (x, y), gauge_img)

        return canvas

    def _extract_telemetry(self, row: Dict) -> Dict[str, float]:
        """Extract available telemetry values from a row.

        Returns dict with gauge_type -> value (only for available data).
        """
        field_map = {
            "speed": "speed_kmh",
            "cadence": "cadence_rpm",
            "hr": "hr_bpm",
            "elev": "elevation",
            "gradient": "gradient_pct",
        }

        telemetry = {}
        for gauge_type, field_name in field_map.items():
            raw_value = row.get(field_name)
            if self._is_value_available(raw_value):
                telemetry[gauge_type] = float(raw_value)

        return telemetry

    def _is_value_available(self, raw_value) -> bool:
        """Check if a telemetry value is available (not null/empty/invalid).

        Returns False for: None, empty string, whitespace-only, non-numeric
        Returns True for: valid numbers including 0
        """
        if raw_value is None:
            return False
        if isinstance(raw_value, (int, float)):
            return True
        if isinstance(raw_value, str):
            stripped = raw_value.strip()
            if not stripped:
                return False
            try:
                float(stripped)
                return True
            except ValueError:
                return False
        return False

    def _calculate_positions(self) -> Dict[str, Tuple[int, int, int]]:
        """Calculate gauge positions based on layout mode.

        Returns:
            Dict mapping gauge_type -> (x, y, size)
        """
        w, h = self.width, self.height
        speed_size = min(CFG.SPEED_GAUGE_SIZE, h - 20)
        small_size = min(CFG.SMALL_GAUGE_SIZE, (h - 20) // 2)

        if self.layout == "strip":
            # Five equal cells across composite width. Order left→right: Elev, Gradient, Speed, HR, Cadence.
            cell_w = w // 5   # 972 // 5 = 194
            order = ["elev", "gradient", "speed", "hr", "cadence"]
            return {name: (i * cell_w, 0, cell_w) for i, name in enumerate(order)}

        # Default: cluster layout
        speed_x = (w - speed_size) // 2
        speed_y = h - speed_size - 10
        margin = 5
        return {
            "speed": (speed_x, speed_y, speed_size),
            "hr": (margin, margin, small_size),
            "cadence": (w - small_size - margin, margin, small_size),
            "elev": (margin, h - small_size - margin, small_size),
            "gradient": (w - small_size - margin, h - small_size - margin, small_size),
        }
