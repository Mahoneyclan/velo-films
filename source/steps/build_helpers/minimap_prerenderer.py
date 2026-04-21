# source/steps/build_helpers/minimap_prerenderer.py
"""
Minimap pre-rendering for clips.
Generates all minimap overlays before video encoding begins.
Uses parallel processing for faster rendering.
"""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Dict

from ...utils.log import setup_logger
from ...utils.map_overlay import render_overlay_minimap
from ...utils.gpx import GpxPoint
from ...utils.hardware import get_worker_count
from ...io_paths import _mk
from ...utils.progress_reporter import report_progress

log = setup_logger("steps.build_helpers.minimap_prerenderer")


class MinimapPrerenderer:
    """Pre-renders minimaps for all selected clips."""

    def __init__(self, output_dir: Path, gpx_points: List[GpxPoint]):
        """
        Args:
            output_dir: Directory to save minimap images
            gpx_points: GPS trackpoints for map rendering
        """
        from ...config import DEFAULT_CONFIG as CFG

        self.output_dir = _mk(output_dir)
        self.gpx_points = gpx_points

        # Map canvas is a fixed square (MAP_W × MAP_W).
        # render_overlay_minimap produces an image that fits within the square
        # while maintaining route aspect ratio; we pad with transparency to
        # exactly MAP_W × MAP_W so the FFmpeg overlay x position is always fixed.
        self.map_w = CFG.MAP_W          # 390 — square canvas dimension

        log.info(f"[minimap] Minimap canvas size: {self.map_w}x{self.map_w}px (square)")
    
    def prerender_all(self, rows: List[Dict]) -> Dict[int, Path]:
        """
        Pre-render all minimaps for selected clips using parallel processing.

        Args:
            rows: List of clip metadata dicts from select.csv

        Returns:
            Dict mapping clip_idx → minimap_path
        """
        if not self.gpx_points:
            log.warning("[minimap] No GPX data available, skipping minimap rendering")
            return {}

        num_workers = get_worker_count('io')
        log.info(f"[minimap] Pre-rendering {len(rows)} minimaps with {num_workers} workers...")
        minimap_paths: Dict[int, Path] = {}

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            # Submit all tasks
            futures = {
                executor.submit(self._render_single, row, idx): idx
                for idx, row in enumerate(rows, start=1)
            }

            # Collect results as they complete
            completed = 0
            for future in as_completed(futures):
                idx = futures[future]
                completed += 1
                try:
                    minimap_path = future.result()
                    if minimap_path:
                        minimap_paths[idx] = minimap_path
                except Exception as e:
                    log.warning(f"[minimap] Failed to render minimap {idx}: {e}")

                # Progress update
                if completed % 10 == 0 or completed == len(rows):
                    report_progress(completed, len(rows), f"Rendered {completed}/{len(rows)} minimaps")

        log.info(f"[minimap] Successfully rendered {len(minimap_paths)} minimaps")
        return minimap_paths
    
    def _render_single(self, row: Dict, clip_idx: int) -> Path | None:
        """
        Render single minimap for a clip.

        Args:
            row: Clip metadata (from select.csv)
            clip_idx: Clip index number

        Returns:
            Path to rendered minimap PNG, or None if failed
        """
        # Use GPX epoch as the authoritative ride timeline
        gpx_epoch = row.get("gpx_epoch")
        if not gpx_epoch:
            log.debug(f"[minimap] Clip {clip_idx} has no GPX timestamp")
            return None

        try:
            epoch = float(gpx_epoch)
            img = render_overlay_minimap(
                self.gpx_points,
                epoch,
                size=(self.map_w, self.map_w)
            )

            # Fit map within square, preserving aspect ratio, then pad with
            # transparency to EXACT map_w × map_w. This guarantees h = MAP_W
            # in the FFmpeg overlay, matching PiP height (scale=-1:PIP_H = MAP_W).
            from PIL import Image as PILImage
            # Scale to fit within map_w × map_w
            scale = min(self.map_w / img.width, self.map_w / img.height)
            fit_w = round(img.width * scale)
            fit_h = round(img.height * scale)
            img_fit = img.resize((fit_w, fit_h), PILImage.LANCZOS)
            # Bottom-align on transparent square canvas so map bottom edge
            # aligns with the PiP and gauge strip bottom edge.
            canvas = PILImage.new("RGBA", (self.map_w, self.map_w), (0, 0, 0, 0))
            x_off = (self.map_w - fit_w) // 2
            y_off = self.map_w - fit_h
            canvas.paste(img_fit, (x_off, y_off))

            minimap_path = self.output_dir / f"minimap_{clip_idx:04d}.png"
            canvas.save(minimap_path)

            return minimap_path

        except Exception as e:
            log.warning(f"[minimap] Render failed for clip {clip_idx}: {e}")
            return None
    
