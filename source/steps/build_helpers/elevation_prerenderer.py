# source/steps/build_helpers/elevation_prerenderer.py
"""
Pre-render elevation profile plots for all clips.
Uses parallel processing for faster rendering.
"""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List

from ...utils.log import setup_logger
from ...utils.elevation_plot import load_elevation_data, render_elevation_plot
from ...utils.hardware import get_worker_count
from ...io_paths import flatten_path, _mk
from ...config import DEFAULT_CONFIG as CFG
from ...utils.progress_reporter import report_progress

log = setup_logger("steps.build_helpers.elevation_prerenderer")


class ElevationPrerenderer:
    """Pre-renders elevation plots for all selected clips."""

    def __init__(self, output_dir: Path):
        """
        Args:
            output_dir: Directory to save elevation plot images
        """
        self.output_dir = _mk(output_dir)
        self.elevation_data = load_elevation_data(flatten_path())

        # Elevation strip spans from the last gauge's right edge to the frame edge.
        # Anchored at x=GAUGE_COMPOSITE_SIZE[0] so it always covers the map+PiP area
        # regardless of the map's variable width.
        self.width  = 1920 - CFG.GAUGE_COMPOSITE_SIZE[0]   # 1920 - 1080 = 840
        self.height = CFG.ELEV_STRIP_H                      # 75 px

    def prerender_all(self, rows: List[Dict]) -> Dict[int, Path]:
        """
        Pre-render elevation plots for all clips using parallel processing.

        Args:
            rows: List of clip metadata dicts from select.csv

        Returns:
            Dict mapping clip_idx → elevation_plot_path
        """
        if not self.elevation_data:
            log.warning("[elev] No elevation data available, skipping plots")
            return {}

        num_workers = get_worker_count('io')
        log.info(f"[elev] Pre-rendering {len(rows)} elevation plots ({self.width}x{self.height}px) with {num_workers} workers...")
        paths: Dict[int, Path] = {}

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
                    result = future.result()
                    if result:
                        paths[idx] = result
                except Exception as e:
                    log.warning(f"[elev] Failed to render plot for clip {idx}: {e}")

                # Progress update
                if completed % 10 == 0 or completed == len(rows):
                    report_progress(completed, len(rows), f"Rendered {completed}/{len(rows)} elevation plots")

        log.info(f"[elev] Successfully rendered {len(paths)} elevation plots")
        return paths

    def _render_single(self, row: Dict, idx: int) -> Path | None:
        """Render single elevation plot for a clip."""
        # Use gpx_epoch if available, fallback to abs_time_epoch
        epoch_str = row.get("gpx_epoch") or row.get("abs_time_epoch") or "0"
        epoch = float(epoch_str)

        if epoch <= 0:
            return None

        out_path = self.output_dir / f"elev_{idx:04d}.png"
        render_elevation_plot(
            self.elevation_data, epoch, out_path,
            self.width, self.height
        )
        return out_path
