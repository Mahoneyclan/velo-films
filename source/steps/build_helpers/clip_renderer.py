# source/steps/build_helpers/clip_renderer.py
"""
Individual clip rendering with overlays.
Handles PiP, minimap, gauges, and audio muxing.

Moment-based version:
- main_row: selected perspective for this moment (recommended=true).
- pip_row:  opposite camera for same moment (always used as PiP).

FIXED:
- Each camera gets its own t_start calculated from its own adjusted_start_time
- This ensures perfect alignment even when cameras have different offsets
"""

from __future__ import annotations
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ...config import DEFAULT_CONFIG as CFG
from ...utils.log import setup_logger
from ...utils.ffmpeg import mux_audio
from ...utils.trophy_overlay import create_trophy_overlay
from ...utils.hardware import get_optimal_video_codec, is_apple_silicon
from ...io_paths import _mk, trophy_dir

log = setup_logger("steps.build_helpers.clip_renderer")

AUDIO_SAMPLE_RATE = "48000"


class ClipRenderer:
    """Renders individual highlight clips with all overlays."""

    def __init__(self, output_dir: Path):
        """
        Args:
            output_dir: Directory for rendered clips
        """
        self.output_dir = _mk(output_dir)

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def render_clip(
        self,
        main_row: Dict,
        pip_row: Optional[Dict],
        clip_idx: int,
        minimap_path: Optional[Path],
        elevation_path: Optional[Path],
        gauge_path: Optional[Path],
    ) -> Optional[Path]:
        """
        Render single clip with all overlays (main + PiP + minimap + gauges).

        For single-camera clips (pip_row=None), renders main camera full-width
        without PiP overlay.

        Time model:
            abs_time_epoch      = world-aligned timestamp of the moment
            adjusted_start_time = real start time of the source clip (UTC)
            clip_start_epoch    = parsed adjusted_start_time
            offset_in_clip      = abs_time_epoch - clip_start_epoch
            t_start             = max(0, offset_in_clip - CLIP_PRE_ROLL_S)

        CRITICAL: main and pip videos need separate t_start values
        because they have different adjusted_start_time values.
        """

        main_video = CFG.INPUT_VIDEOS_DIR / main_row["source"]

        # Handle single-camera clips (pip_row may be None)
        is_single_camera = pip_row is None
        pip_video = None if is_single_camera else CFG.INPUT_VIDEOS_DIR / pip_row["source"]

        # ---------------------------------------------------------------------
        # Compute t_start for cameras (pip only if available)
        # ---------------------------------------------------------------------
        t_start_main = self._compute_t_start(main_row, clip_idx, "main")

        if t_start_main is None:
            log.error(f"[clip] Failed to compute t_start for main camera (clip {clip_idx})")
            return None

        t_start_pip = None
        if not is_single_camera:
            t_start_pip = self._compute_t_start(pip_row, clip_idx, "pip")
            if t_start_pip is None:
                log.warning(f"[clip] Failed to compute t_start for pip camera (clip {clip_idx})")
                t_start_pip = t_start_main  # Fallback to main timing

        duration = CFG.CLIP_OUT_LEN_S
        output_path = self.output_dir / f"clip_{clip_idx:04d}.mp4"

        # Build ffmpeg command with separate timing for each camera
        inputs, filter_complex, final_stream = self._build_ffmpeg_inputs_and_filters(
            main_video=main_video,
            pip_video=pip_video,
            t_start_main=t_start_main,
            t_start_pip=t_start_pip,
            minimap_path=minimap_path,
            elevation_path=elevation_path,
            duration=duration,
            main_row=main_row,
            clip_idx=clip_idx,
            gauge_path=gauge_path,
        )

        cmd = self._build_encode_command(inputs, filter_complex, final_stream, output_path)

        try:
            subprocess.run(cmd, check=True)
            if not output_path.exists():
                log.error(f"[clip] FFmpeg reported success but {output_path} was not created")
                return None
            if is_single_camera:
                log.debug(f"[clip] Encoded clip {clip_idx:04d} (single-camera, main@{t_start_main:.3f}s)")
            else:
                log.debug(
                    f"[clip] Encoded clip {clip_idx:04d} "
                    f"(main@{t_start_main:.3f}s, pip@{t_start_pip:.3f}s)"
                )
        except subprocess.CalledProcessError as e:
            log.error(f"[clip] FFmpeg failed for clip {clip_idx}: {e}")
            return None

        # Mux audio from main camera
        return self._mux_audio(output_path, main_video, t_start_main, duration, clip_idx)

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _compute_t_start(
        self,
        row: Dict,
        clip_idx: int,
        camera_role: str
    ) -> Optional[float]:
        """
        Compute extraction start time for a single camera.

        Uses:
        - abs_time_epoch: world time of the selected moment
        - clip_start_epoch: real start time of this source clip (from extract)
        - duration_s: clip duration for bounds checking

        Returns:
        t_start (float): seconds into the clip to begin extraction,
        or None if invalid.
        """
        try:
            abs_epoch = float(row.get("abs_time_epoch") or 0.0)
            clip_start_epoch = float(row.get("clip_start_epoch") or 0.0)
            duration_s = float(row.get("duration_s") or 0.0)
        except (ValueError, TypeError) as e:
            log.error(
                f"[clip] Invalid time fields for {camera_role} camera in clip {clip_idx}: {e}"
            )
            return None

        if abs_epoch == 0.0 or clip_start_epoch == 0.0:
            log.error(
                f"[clip] Missing abs_time_epoch or clip_start_epoch for "
                f"{camera_role} camera in clip {clip_idx}"
            )
            return None

        # Offset of the desired moment inside the clip
        offset_in_clip = abs_epoch - clip_start_epoch

        if offset_in_clip < 0:
            log.warning(
                f"[clip] Negative offset_in_clip ({offset_in_clip:.3f}s) "
                f"for {camera_role} camera in clip {clip_idx:04d} "
                f"({row.get('source')})"
            )

        # Apply pre-roll
        t_start = max(0.0, offset_in_clip - CFG.CLIP_PRE_ROLL_S)

        # Bounds check
        if duration_s > 0 and t_start >= duration_s:
            log.error(
                f"[clip] t_start={t_start:.3f}s beyond clip duration "
                f"{duration_s:.3f}s for {camera_role} camera "
                f"({row.get('source')}) in clip_idx={clip_idx}"
            )
            return None

        return t_start



    def _build_ffmpeg_inputs_and_filters(
        self,
        main_video: Path,
        pip_video: Optional[Path],
        t_start_main: float,
        t_start_pip: Optional[float],
        minimap_path: Optional[Path],
        elevation_path: Optional[Path],
        duration: float,
        main_row: Dict,
        clip_idx: int,
        gauge_path: Optional[Path],
    ) -> Tuple[List[str], List[str], str]:
        """
        Build ffmpeg inputs and filter_complex for all overlays.

        For single-camera clips (pip_video=None), renders main camera full-width.
        CRITICAL: Uses separate t_start values for main and pip cameras when both present.
        """
        inputs: List[str] = [
            "-ss",
            f"{t_start_main:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(main_video),
        ]
        # Always scale main video to target output resolution first.
        # Source footage (Cycliq) is 2560×1440; overlays are designed for 1920×1080.
        filters: List[str] = [
            f"[0:v]scale={CFG.OUTPUT_W}:{CFG.OUTPUT_H}:"
            f"force_original_aspect_ratio=decrease,"
            f"pad={CFG.OUTPUT_W}:{CFG.OUTPUT_H}:(ow-iw)/2:(oh-ih)/2[vmain]"
        ]
        current_stream = "[vmain]"

        # ── Layout (1920×1080), left → right: ────────────────────────────────────
        #   [Gauge 972px][MAP 390px][8px gap][PiP ~693px] ≈ 2063px; main vid behind all
        #   Below MAP+PiP: [Elev strip 948×75px]
        #
        #   Gauge x=0..972     y=811..1005  h=194  (overlay=0:H-h-75)
        #   MAP   x=972..1362  y=615..1005  h=390  w=390 (square padded canvas)
        #   PiP   x=1370..~2063 y=615..1005 h=390  w≈693 (scale=-1:390 from 2560×1440)
        #   Elev  x=972..1920  y=1005..1080 h=75   w=948 (overlay=972:H-h)
        _gauge_right = CFG.GAUGE_COMPOSITE_SIZE[0]              # 972
        _map_w       = CFG.MAP_W                                # 390 (square padded canvas)
        _map_gap     = CFG.MAP_GAP                              # 8   (gap between map and PiP)
        _pip_w       = 1920 - _gauge_right - _map_w - _map_gap # nominal; actual ≈693 from scale=-1:PIP_H
        _pip_x       = _gauge_right + _map_w + _map_gap         # 1370 (flush right of gap)
        _map_x       = _gauge_right                             # 972 (flush right of gauge)
        _panel_y     = f"H-h-{CFG.MAP_PIP_BOTTOM}"             # H-h-75 → bottom at y=1005

        # PiP overlay (with its own t_start!) - skip for single-camera clips
        if pip_video is not None and pip_video.exists() and t_start_pip is not None:
            inputs.extend(
                [
                    "-ss",
                    f"{t_start_pip:.3f}",  # ✓ CORRECT - uses pip timing!
                    "-t",
                    f"{duration:.3f}",
                    "-i",
                    str(pip_video),
                ]
            )
            # Fixed-height scale; placed right of map
            filters.append(
                f"[1:v]scale=-1:{CFG.PIP_H}[pip];"
                f"{current_stream}[pip]overlay={_pip_x}:{_panel_y}[v1]"
            )
            current_stream = "[v1]"
        elif pip_video is None:
            # Single-camera clip: render main camera full-width (no PiP)
            log.debug(f"[clip] Single-camera clip {clip_idx}: rendering without PiP")
        else:
            log.warning("[clip] PiP video missing; rendering main camera only")

        # Route-map overlay — fixed x at gauge right edge; canvas is padded square
        if minimap_path and minimap_path.exists():
            inputs.extend(["-i", str(minimap_path)])
            minimap_idx = len([a for a in inputs if a == "-i"]) - 1
            filters.append(
                f"{current_stream}[{minimap_idx}:v]overlay={_map_x}:{_panel_y}[vmap]"
            )
            current_stream = "[vmap]"

        # Elevation strip — very bottom edge, spanning gauge right to frame right
        if elevation_path and elevation_path.exists() and CFG.SHOW_ELEVATION_PLOT:
            inputs.extend(["-i", str(elevation_path)])
            elev_idx = len([a for a in inputs if a == "-i"]) - 1
            filters.append(
                f"{current_stream}[{elev_idx}:v]overlay={_gauge_right}:H-h[velev]"
            )
            current_stream = "[velev]"

        # PR Trophy badge overlay (top-left, only for Strava PR clips)
        if str(main_row.get("strava_pr", "false")).lower() == "true":
            segment_name = main_row.get("segment_name", "PR Segment")
            # Parse segment details for badge display
            try:
                segment_distance = float(main_row.get("segment_distance", 0) or 0)
            except (ValueError, TypeError):
                segment_distance = 0
            try:
                segment_grade = float(main_row.get("segment_grade", 0) or 0)
            except (ValueError, TypeError):
                segment_grade = 0

            trophy_path = _mk(trophy_dir()) / f"trophy_{clip_idx:04d}.png"
            try:
                create_trophy_overlay(
                    segment_name,
                    trophy_path,
                    distance_m=segment_distance,
                    grade_pct=segment_grade,
                )
                inputs.extend(["-loop", "1", "-t", f"{duration:.3f}", "-i", str(trophy_path)])
                trophy_idx = len([a for a in inputs if a == "-i"]) - 1
                # Position: top-left with same margin as minimap
                filters.append(
                    f"{current_stream}[{trophy_idx}:v]overlay={CFG.MINIMAP_MARGIN}:{CFG.MINIMAP_MARGIN}[vtrophy]"
                )
                current_stream = "[vtrophy]"
                log.debug(f"[clip] Added PR badge for clip {clip_idx}: {segment_name}")
            except Exception as e:
                log.warning(f"[clip] Failed to create trophy badge for clip {clip_idx}: {e}")

        # Composite gauge overlay (single pre-rendered PNG at bottom-left)
        current_stream = self._add_gauge_overlay(
            filters, inputs, current_stream, gauge_path, duration
        )

        return inputs, filters, current_stream

    def _add_gauge_overlay(
        self,
        filters: List[str],
        inputs: List[str],
        current_stream: str,
        gauge_path: Optional[Path],
        duration: float,
    ) -> str:
        """Add gauge overlay to filter chain.

        Supports both static PNG (looped) and dynamic video (per-second updates).
        """
        if not gauge_path or not gauge_path.exists():
            return current_stream

        # Check if gauge is a video (dynamic) or PNG (static)
        is_video = gauge_path.suffix.lower() in ('.mov', '.mp4', '.webm')

        if is_video:
            # Video gauge: use directly without looping
            inputs.extend(["-t", f"{duration:.3f}", "-i", str(gauge_path)])
        else:
            # Static PNG: loop for clip duration
            inputs.extend(
                ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(gauge_path)]
            )
        idx_in = len([a for a in inputs if a == "-i"]) - 1

        # Position at bottom-left with HUD_PADDING
        x, y = CFG.HUD_PADDING
        filters.append(
            f"{current_stream}[{idx_in}:v]overlay={x}:H-h-{y}[vhud]"
        )

        return "[vhud]"

    def _build_encode_command(
        self,
        inputs: List[str],
        filters: List[str],
        final_stream: str,
        output_path: Path,
    ) -> List[str]:
        """Build complete ffmpeg encoding command with optimal hardware acceleration."""
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]

        # Add hardware acceleration for decoding on Apple Silicon
        if is_apple_silicon() and CFG.FFMPEG_HWACCEL == "videotoolbox":
            cmd.extend(["-hwaccel", "videotoolbox"])

        cmd.extend(inputs)

        # filters always contains at least the main-video scale; never empty
        filter_str = ";".join(filters)
        cmd.extend(["-filter_complex", filter_str, "-map", final_stream])

        # Select optimal video codec based on hardware and config
        if CFG.PREFERRED_CODEC == 'auto':
            video_codec = get_optimal_video_codec()
        else:
            video_codec = CFG.PREFERRED_CODEC

        cmd.extend(
            [
                "-c:v",
                video_codec,
                "-b:v",
                CFG.BITRATE,
                "-maxrate",
                CFG.MAXRATE,
                "-bufsize",
                CFG.BUFSIZE,
                "-pix_fmt",
                "yuv420p",
                str(output_path),
            ]
        )

        return cmd

    def _mux_audio(
        self,
        video_path: Path,
        source_video: Path,
        t_start: float,
        duration: float,
        clip_idx: int,
    ) -> Optional[Path]:
        """Mux camera audio into rendered clip."""
        try:
            temp_path = video_path.with_suffix(".mux.mp4")
            mux_audio(video_path, source_video, temp_path, t_start, duration)
            temp_path.replace(video_path)
            return video_path
        except Exception as e:
            log.warning(f"[clip] Audio mux failed for clip {clip_idx}: {e}")
            return video_path  # Return video without audio

    @staticmethod
    def _anchor_expr(anchor: str, margin: int) -> str:
        """Generate ffmpeg overlay position expression."""
        anchors = {
            "top_right": f"W-w-{margin}:{margin}",
            "top_left": f"{margin}:{margin}",
            "bottom_right": f"W-w-{margin}:H-h-{margin}",
            "bottom_left": f"{margin}:H-h-{margin}",
        }
        return anchors.get(anchor, f"W-w-{margin}:{margin}")