# source/steps/extract.py
"""
Extract frame metadata from MP4s using GPX-anchored global grid.

TIME MODEL:
- GPX defines the ride timeline (start to end)
- Global sampling grid: gpx_start + N * interval
- Each clip samples at grid points within its recording window
- All cameras get identical abs_time_epoch for the same grid point
- Enables exact moment_id matching for camera pairing
"""

from __future__ import annotations
import csv
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Tuple

from ..config import DEFAULT_CONFIG as CFG
from ..io_paths import extract_path, flatten_path, _mk
from ..utils.log import setup_logger
from ..utils.progress_reporter import progress_iter, report_progress
from ..utils.video_utils import (
    probe_video_metadata,
    fix_cycliq_utc_bug,
    infer_recording_start,
    parse_camera_and_clip
)

log = setup_logger("steps.extract")


def _parse_timezone_string(tz_str: str):
    """Parse timezone string like 'UTC+10:30' to timezone object."""
    if not tz_str:
        return None

    # Parse UTC offset format: "UTC+10:30", "UTC+10", "UTC-5", etc.
    pattern = r'^UTC([+-])(\d{1,2})(?::(\d{2}))?$'
    match = re.match(pattern, tz_str.strip())

    if match:
        sign = match.group(1)
        hours = int(match.group(2))
        minutes = int(match.group(3) or 0)

        total_minutes = hours * 60 + minutes
        if sign == '-':
            total_minutes = -total_minutes

        return timezone(timedelta(minutes=total_minutes))

    return None


def _get_camera_timezone(camera_name: str):
    """
    Get timezone for a specific camera.

    Uses per-camera CAMERA_TIMEZONES if available, otherwise falls back to
    CAMERA_CREATION_TIME_TZ default.
    """
    # Look up per-camera timezone
    tz_str = CFG.CAMERA_TIMEZONES.get(camera_name)
    if tz_str:
        tz_obj = _parse_timezone_string(tz_str)
        if tz_obj:
            return tz_obj

    # Fall back to default
    return CFG.CAMERA_CREATION_TIME_TZ


def _get_gpx_time_range() -> Tuple[float, float]:
    """
    Load GPX ride start and end times for filtering.

    Returns:
        Tuple of (start_epoch, end_epoch), or (0.0, 0.0) if unavailable
    """
    flatten_csv = flatten_path()

    if not flatten_csv.exists():
        log.warning("[extract] No flatten.csv - will extract all frames (no GPX filtering)")
        return 0.0, 0.0

    try:
        with flatten_csv.open() as f:
            reader = csv.DictReader(f)
            rows = list(reader)

            if not rows:
                return 0.0, 0.0

            first_row = rows[0]
            last_row = rows[-1]

            gpx_start = 0.0
            gpx_end = 0.0

            if first_row and "gpx_epoch" in first_row and first_row["gpx_epoch"]:
                gpx_start = float(first_row["gpx_epoch"])

            if last_row and "gpx_epoch" in last_row and last_row["gpx_epoch"]:
                gpx_end = float(last_row["gpx_epoch"])

            if gpx_start > 0 and gpx_end > 0:
                duration_m = (gpx_end - gpx_start) / 60
                log.info(f"[extract] GPX time range: {duration_m:.1f} min")
                log.info(f"[extract]   Start: epoch {gpx_start:.3f}")
                log.info(f"[extract]   End:   epoch {gpx_end:.3f}")

            return gpx_start, gpx_end

    except Exception as e:
        log.warning(f"[extract] Could not read GPX time range: {e}")

    return 0.0, 0.0


def _extract_video_metadata(
    video_path: Path,
    sampling_interval_s: int,
    grid_start_epoch: float,
    grid_end_epoch: float,
    gpx_start_epoch: float,
) -> List[Dict[str, str]]:
    """
    Generate frame metadata for one video clip using global sampling grid.

    Time model:
      - Grid extends beyond GPX to capture pre/post ride footage
      - Global grid: sample at grid_start + N * interval
      - Each clip samples at grid points that fall within its recording window
      - abs_time_epoch = the grid point (real-world time)
      - session_ts_s = time relative to GPX start (can be negative for pre-ride)
      - All cameras sampling at the same grid point get the same abs_time_epoch
    """
    if grid_start_epoch <= 0 or grid_end_epoch <= 0:
        log.warning(f"[extract] {video_path.name}: No grid time bounds - skipping")
        return []

    camera_name, clip_num, clip_id = parse_camera_and_clip(video_path)

    try:
        raw_dt, duration_s, video_fps = probe_video_metadata(video_path, include_fps=True)
    except Exception as e:
        log.error(f"[extract] Metadata probe failed: {video_path.name}: {e}")
        return []

    # Get per-camera timezone (or fall back to default)
    camera_tz = _get_camera_timezone(camera_name)

    # Fix Cycliq UTC bug and get real-world start time
    creation_local = fix_cycliq_utc_bug(
        raw_dt,
        camera_tz,
        CFG.CAMERA_CREATION_TIME_IS_LOCAL_WRONG_Z
    )
    creation_utc = creation_local.astimezone(timezone.utc)
    real_start_utc = infer_recording_start(creation_utc, duration_s, video_path=video_path)

    clip_start_epoch = real_start_utc.timestamp()
    clip_end_epoch = clip_start_epoch + duration_s

    log.info(
        f"[extract] {video_path.name} | duration={duration_s:.1f}s | "
        f"fps={video_fps:.2f} | start={real_start_utc.isoformat()}"
    )

    # Generate global grid points (extended beyond GPX for pre/post ride)
    # Grid: grid_start + 0, grid_start + interval, grid_start + 2*interval, ...
    rows: List[Dict[str, str]] = []

    grid_point = grid_start_epoch
    while grid_point <= grid_end_epoch:
        # Check if this grid point falls within this clip's recording window
        if clip_start_epoch <= grid_point < clip_end_epoch:
            # Compute position within clip
            sec_into_clip = grid_point - clip_start_epoch
            frame_number = int(sec_into_clip * video_fps)
            index = f"{camera_name}_{clip_id}_{int(sec_into_clip):06d}"

            abs_time_iso = datetime.fromtimestamp(grid_point, tz=timezone.utc).isoformat()
            session_ts_s = grid_point - gpx_start_epoch

            rows.append({
                "index": index,
                "camera": camera_name,
                "clip_num": str(clip_num),
                "frame_number": str(frame_number),
                "video_path": str(video_path),
                "abs_time_epoch": f"{grid_point:.3f}",
                "abs_time_iso": abs_time_iso,
                "session_ts_s": f"{session_ts_s:.3f}",
                "clip_start_epoch": f"{clip_start_epoch:.3f}",
                "duration_s": f"{duration_s:.3f}",
                "source": video_path.name,
                "adjusted_start_time": real_start_utc.isoformat().replace("+00:00", "Z"),
                "fps": f"{1.0 / sampling_interval_s:.3f}",
            })

        grid_point += sampling_interval_s

    log.info(f"[extract] Generated {len(rows)} aligned frames from {video_path.name}")
    return rows


def _write_metadata_csv(output_path: Path, all_rows: List[Dict[str, str]]):
    """
    Write frame metadata to CSV using the minimal, correct schema.

    Now includes clip_start_epoch so build can compute t_start
    without re-parsing adjusted_start_time.
    """
    FIELDNAMES = [
        "index",
        "camera",
        "clip_num",
        "frame_number",
        "video_path",

        # Time model
        "abs_time_epoch",
        "abs_time_iso",
        "session_ts_s",
        "clip_start_epoch",
        "adjusted_start_time",

        # Clip metadata
        "duration_s",
        "source",
        "fps",
    ]

    if not all_rows:
        log.warning("[extract] No frames to write - creating empty CSV")
        with output_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
        return

    cleaned_rows = []
    for row in all_rows:
        cleaned = {k: row.get(k, "") for k in FIELDNAMES}
        cleaned_rows.append(cleaned)

    with output_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(cleaned_rows)

    log.info(f"[extract] Wrote {len(cleaned_rows)} frame metadata rows to {output_path.name}")


def run() -> Path:
    """
    Generate frame metadata using GPX-anchored sampling grid.

    Process:
        1. Load GPX time bounds (defines the ride timeline)
        2. Create global sampling grid: gpx_start + N * interval
        3. For each video clip:
           - Infer real-world recording start
           - Sample at grid points that fall within clip's window
           - All cameras get identical abs_time_epoch for same grid point
        4. Write metadata CSV
    
    Returns:
        Path to extract.csv output file
    """
    log.info("=" * 70)
    log.info("EXTRACT: Frame Metadata Generation with Alignment")
    log.info("=" * 70)
    
    # =========================================================================
    # Setup
    # =========================================================================
    report_progress(1, 5, "Initializing extraction...")
    
    output_csv = _mk(extract_path())
    videos = sorted(CFG.INPUT_VIDEOS_DIR.glob("*_*.MP4"))

    if not videos:
        log.warning(f"[extract] No videos found in {CFG.INPUT_VIDEOS_DIR}")
        _write_metadata_csv(output_csv, [])
        return output_csv

    log.info(f"[extract] Found {len(videos)} video clips")

    # Test mode: only process first video from each camera
    if CFG.TEST_MODE:
        seen_cameras = set()
        test_videos = []
        for v in videos:
            camera_name, _, _ = parse_camera_and_clip(v)
            if camera_name not in seen_cameras:
                seen_cameras.add(camera_name)
                test_videos.append(v)
        log.info(f"[extract] 🧪 TEST MODE: Using only first video per camera")
        log.info(f"[extract] 🧪 Reduced from {len(videos)} to {len(test_videos)} videos")
        for v in test_videos:
            log.info(f"[extract] 🧪   → {v.name}")
        videos = test_videos
    
    # =========================================================================
    # Load GPX time bounds and extend grid for pre/post ride video
    # =========================================================================
    report_progress(2, 5, "Loading GPX time bounds...")
    gpx_start_epoch, gpx_end_epoch = _get_gpx_time_range()
    sampling_interval_s = int(CFG.EXTRACT_INTERVAL_SECONDS)

    # Extend grid before/after GPX to capture pre/post ride footage.
    # Check project_config.json for a per-project override; fall back to global CFG.
    grid_extension_m = CFG.GPX_GRID_EXTENSION_M
    try:
        import json as _json
        _proj_cfg_path = CFG.PROJECT_DIR / "project_config.json"
        if _proj_cfg_path.exists():
            _proj_cfg = _json.loads(_proj_cfg_path.read_text())
            if "GPX_GRID_EXTENSION_M" in _proj_cfg:
                grid_extension_m = float(_proj_cfg["GPX_GRID_EXTENSION_M"])
                log.info(f"[extract]   GPX grid extension overridden by project_config.json: {grid_extension_m:.0f} min")
    except Exception as _e:
        log.warning(f"[extract]   Could not read project_config.json for grid extension: {_e}")
    grid_extension_s = grid_extension_m * 60.0

    if gpx_start_epoch > 0 and gpx_end_epoch > 0:
        gpx_start_dt = datetime.fromtimestamp(gpx_start_epoch, tz=timezone.utc)
        gpx_end_dt = datetime.fromtimestamp(gpx_end_epoch, tz=timezone.utc)
        duration_min = (gpx_end_epoch - gpx_start_epoch) / 60

        # Extended grid bounds
        grid_start_epoch = gpx_start_epoch - grid_extension_s
        grid_end_epoch = gpx_end_epoch + grid_extension_s

        log.info(f"[extract] GPX timeline: {duration_min:.1f} min")
        log.info(f"[extract]   GPX start: {gpx_start_dt.isoformat()}")
        log.info(f"[extract]   GPX end:   {gpx_end_dt.isoformat()}")
        log.info(f"[extract]   Grid extended: +/- {grid_extension_m:.0f} min")
        log.info(f"[extract]   Grid interval: {sampling_interval_s}s")
    else:
        log.error("[extract] No GPX data - cannot create timeline grid")
        _write_metadata_csv(output_csv, [])
        return output_csv
    
    # =========================================================================
    # Process videos
    # =========================================================================
    report_progress(3, 5, f"Processing {len(videos)} videos...")
    
    all_rows: List[Dict[str, str]] = []
    
    for video_idx, video_path in enumerate(progress_iter(
        videos,
        desc="Extracting metadata",
        unit="video"
    ), start=1):
        
        report_progress(
            3 + (video_idx - 1) / len(videos),
            5,
            f"Processing {video_path.name} ({video_idx}/{len(videos)})"
        )
        
        try:
            rows = _extract_video_metadata(
                video_path,
                sampling_interval_s,
                grid_start_epoch,
                grid_end_epoch,
                gpx_start_epoch,
            )
            all_rows.extend(rows)
        except Exception as e:
            log.error(f"[extract] Video processing failed: {video_path.name}: {e}")
            continue
    
    # =========================================================================
    # Write output
    # =========================================================================
    report_progress(4, 5, "Writing metadata CSV...")
    
    if all_rows:
        # Sort chronologically
        all_rows.sort(key=lambda r: float(r["abs_time_epoch"]))
        
        log.info("")
        log.info("=" * 70)
        log.info(f"[extract] ✓ Generated {len(all_rows)} frame metadata entries")
        
        # Log statistics
        cameras = set(r["camera"] for r in all_rows)
        for camera in sorted(cameras):
            cam_rows = [r for r in all_rows if r["camera"] == camera]
            log.info(f"[extract]   {camera}: {len(cam_rows)} frames")
        
        # Log time range
        first_time = all_rows[0]["abs_time_iso"]
        last_time = all_rows[-1]["abs_time_iso"]
        log.info(f"[extract] Time range: {first_time} to {last_time}")
        
    else:
        log.warning("[extract] ⚠️ No frames generated (possibly all before GPX start)")
    
    _write_metadata_csv(output_csv, all_rows)
    
    report_progress(5, 5, "Extraction complete")
    
    log.info("=" * 70)
    
    return output_csv


