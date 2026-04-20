# source/config.py
"""
Hard-fork configuration for MP4 streaming pipeline.
All working files moved to project directories. Source files remain untouched.
Loads user preferences from persistent storage.
"""

from __future__ import annotations
from dataclasses import dataclass, field, fields
from pathlib import Path
from datetime import timezone, timedelta
from typing import Any

# Import persistent config loader
try:
    from source.utils.persistent_config import load_persistent_config
    _PERSISTENT_CONFIG = load_persistent_config()
except ImportError:
    _PERSISTENT_CONFIG = {}


def _get_config_value(key: str, default: Any) -> Any:
    """Get config value from persistent storage or use default."""
    return _PERSISTENT_CONFIG.get(key, default)



# Default weights for YOLO classes, used if not overridden by user config
DEFAULT_YOLO_CLASS_WEIGHTS = {
    "person": 0.5,
    "bicycle": 3.0,
    "car": 0.5,
    "motorcycle": 0.5,
    "bus": 0.5,
    "truck": 0.5,
    "traffic light": 0.5,
    "stop sign": 0.5,
}

@dataclass
class Config:
    # --- Logging ---
    LOG_LEVEL: str = field(default_factory=lambda: _get_config_value('LOG_LEVEL', 'INFO'))

    # --- Project paths ---
    INPUT_BASE_DIR: Path = field(
        default_factory=lambda: Path(_get_config_value('INPUT_BASE_DIR', '/Volumes/AData/Fly_Raw'))
    )
    PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
    PROJECTS_ROOT: Path = field(
        default_factory=lambda: Path(_get_config_value('PROJECTS_ROOT', '/Volumes/AData/Fly_Projects'))
    )

    # --- Core pipeline settings ---
    RIDE_FOLDER: str = ""
    SOURCE_FOLDER: str = ""

    # Test mode: only process first video from each camera for faster testing
    TEST_MODE: bool = field(
        default_factory=lambda: _get_config_value('TEST_MODE', False)
    )

    # Sampling interval in seconds (time-based, not FPS)
    EXTRACT_INTERVAL_SECONDS: int = field(
        default_factory=lambda: _get_config_value('EXTRACT_INTERVAL_SECONDS', 5)
    )

    HIGHLIGHT_TARGET_DURATION_M: float = field(
        default_factory=lambda: _get_config_value('HIGHLIGHT_TARGET_DURATION_M', 5.0)
    )
    CLIP_PRE_ROLL_S: float = field(default_factory=lambda: _get_config_value('CLIP_PRE_ROLL_S', 0.5))
    CLIP_OUT_LEN_S: float = field(default_factory=lambda: _get_config_value('CLIP_OUT_LEN_S', 3.5))

    MIN_GAP_BETWEEN_CLIPS: float = field(
        default_factory=lambda: _get_config_value('MIN_GAP_BETWEEN_CLIPS', 10.0)
    )

    # --- Scene-aware selection ---
    # High scene_boost clips can be placed closer together (reduced gap)
    SCENE_HIGH_THRESHOLD: float = field(
        default_factory=lambda: _get_config_value('SCENE_HIGH_THRESHOLD', 0.50)
    )
    SCENE_HIGH_GAP_MULTIPLIER: float = field(
        default_factory=lambda: _get_config_value('SCENE_HIGH_GAP_MULTIPLIER', 0.5)
    )
    # How far back (seconds) to compare frames for scene change detection
    SCENE_COMPARISON_WINDOW_S: float = field(
        default_factory=lambda: _get_config_value('SCENE_COMPARISON_WINDOW_S', 15.0)
    )

    # --- YOLO settings ---
    YOLO_CLASS_MAP = {
        "person": 0,
        "bicycle": 1,
        "car": 2,
        "motorcycle": 3,
        "bus": 5,
        "truck": 7,
        "traffic light": 9,
        "stop sign": 11
    }

    YOLO_AVAILABLE_CLASSES = [
        "person",
        "bicycle",
        "car",
        "motorcycle",
        "bus",
        "truck",
        "traffic light",
        "stop sign"
    ]

    YOLO_CLASS_WEIGHTS: dict = field(
        default_factory=lambda: _get_config_value('YOLO_CLASS_WEIGHTS', DEFAULT_YOLO_CLASS_WEIGHTS)
    )

    # --- Detection settings ---
    YOLO_MODEL: str = field(default_factory=lambda: _get_config_value('YOLO_MODEL', 'yolo11s.pt'))
    YOLO_DETECT_CLASSES: list = field(
        default_factory=lambda: _get_config_value('YOLO_DETECT_CLASSES', [0, 1, 2, 3, 5, 7, 9, 11])
    )
    YOLO_IMAGE_SIZE: int = field(default_factory=lambda: _get_config_value('YOLO_IMAGE_SIZE', 640))
    YOLO_MIN_CONFIDENCE: float = field(
        default_factory=lambda: _get_config_value('YOLO_MIN_CONFIDENCE', 0.10)
    )
    YOLO_BATCH_SIZE: int = field(default_factory=lambda: _get_config_value('YOLO_BATCH_SIZE', 8))


    # --- Candidate selection ---
    # Multiplier for candidate pool size (candidates = target_clips * CANDIDATE_FRACTION)
    # Higher = more clips shown in manual_selection for user to choose from
    CANDIDATE_FRACTION: float = field(
        default_factory=lambda: _get_config_value('CANDIDATE_FRACTION', 2.5)
    )
    REQUIRE_GPS_FOR_SELECTION: bool = field(
        default_factory=lambda: _get_config_value('REQUIRE_GPS_FOR_SELECTION', False)
    )

    # --- Zone filtering (additional clips beyond target) ---
    START_ZONE_DURATION_M: float = field(
        default_factory=lambda: _get_config_value('START_ZONE_DURATION_M', 20.0)
    )
    # Max additional clips from start zone (first N minutes of ride)
    MAX_START_ZONE_CLIPS: int = field(
        default_factory=lambda: int(_get_config_value('MAX_START_ZONE_CLIPS', 4))
    )

    END_ZONE_DURATION_M: float = field(
        default_factory=lambda: _get_config_value('END_ZONE_DURATION_M', 20.0)
    )
    # Max additional clips from end zone (last N minutes of ride)
    MAX_END_ZONE_CLIPS: int = field(
        default_factory=lambda: int(_get_config_value('MAX_END_ZONE_CLIPS', 4))
    )

    # --- Scoring weights ---
    CAMERA_WEIGHTS: dict = field(default_factory=lambda: {
        "Fly12Sport": 1.0,
        "Fly6Pro": 1.0,
    })
    SCORE_WEIGHTS: dict = field(default_factory=lambda: {
        "detect_score": 0.30,
        "scene_boost": 0.10,   
        "speed_kmh": 0.20,
        "gradient": 0.20,
        "bbox_area": 0.05,
        "segment_boost": 0.05,    # Strava PR/top-3 segment efforts
        "dual_camera": 0.10,      # Bonus for moments with both front/rear cameras
    })  # Must sum to 1.0

    # --- M1 hardware acceleration ---
    USE_MPS: bool = field(default_factory=lambda: _get_config_value('USE_MPS', True))
    FFMPEG_HWACCEL: str = field(default_factory=lambda: _get_config_value('FFMPEG_HWACCEL', 'videotoolbox'))
    # Video codec: 'auto' = detect optimal, or specify: 'hevc_videotoolbox', 'h264_videotoolbox', 'libx264'
    PREFERRED_CODEC: str = field(default_factory=lambda: _get_config_value('PREFERRED_CODEC', 'auto'))

    # --- Time alignment ---
    # Default timezone (fallback if per-camera timezone not set)
    CAMERA_CREATION_TIME_TZ = timezone(timedelta(hours=10))
    CAMERA_CREATION_TIME_IS_LOCAL_WRONG_Z: bool = True

    # Per-camera timezones (cameras may sync to different phones/locations)
    # Format: {"Fly12Sport": "UTC+10", "Fly6Pro": "UTC+10:30"}
    CAMERA_TIMEZONES: dict = field(default_factory=lambda: _get_config_value(
        'CAMERA_TIMEZONES', {
            "Fly12Sport": "UTC+10",
            "Fly6Pro": "UTC+10",
        }
    ))

    # Known creation_time offsets per camera model (seconds to add to duration)
    # Different Cycliq cameras may record creation_time at different points relative to recording end
    KNOWN_OFFSETS: dict = field(default_factory=lambda: _get_config_value(
        'KNOWN_OFFSETS', {
            "Fly12Sport": 0.0,
            "Fly6Pro": 0.0,
        }
    ))

    GPX_TIME_OFFSET_S: float = field(default_factory=lambda: _get_config_value('GPX_TIME_OFFSET_S', 0.0))
    GPX_TOLERANCE: float = field(default_factory=lambda: _get_config_value('GPX_TOLERANCE', 1.0))
    GPX_GRID_EXTENSION_M: float = field(
        default_factory=lambda: _get_config_value('GPX_GRID_EXTENSION_M', 5.0)
    )  # Minutes to extend sampling grid before/after GPX ride data


    # --- Path properties ---
    @property
    def PROJECT_DIR(self) -> Path:
        return self.PROJECTS_ROOT / self.RIDE_FOLDER

    @property
    def INPUT_DIR(self) -> Path:
        return self.INPUT_BASE_DIR / self.SOURCE_FOLDER

    @property
    def INPUT_VIDEOS_DIR(self) -> Path:
        return self.INPUT_DIR

    @property
    def INPUT_GPX_FILE(self) -> Path:
        """Legacy: GPX in raw movies folder. Prefer GPX_FILE for project-scoped location."""
        return self.INPUT_DIR / "ride.gpx"

    @property
    def GPX_FILE(self) -> Path:
        """Project-scoped GPX file in working directory."""
        return self.WORKING_DIR / "ride.gpx"

    @property
    def FINAL_REEL_PATH(self) -> Path:
        return self.PROJECT_DIR / f"{self.RIDE_FOLDER}.mp4"

    @property
    def LOG_DIR(self) -> Path:
        return self.PROJECT_DIR / "logs"

    @property
    def WORKING_DIR(self) -> Path:
        return self.PROJECT_DIR / "working"

    @property
    def CLIPS_DIR(self) -> Path:
        return self.PROJECT_DIR / "clips"

    @property
    def FRAMES_DIR(self) -> Path:
        return self.PROJECT_DIR / "frames"

    @property
    def CALIBRATION_FRAMES_DIR(self) -> Path:
        return self.PROJECT_DIR / "calibration_frames"

    @property
    def SPLASH_ASSETS_DIR(self) -> Path:
        return self.PROJECT_DIR / "splash_assets"

    @property
    def MINIMAP_DIR(self) -> Path:
        return self.PROJECT_DIR / "minimaps"

    @property
    def GAUGE_DIR(self) -> Path:
        return self.PROJECT_DIR / "gauges"

    @property
    def ELEVATION_DIR(self) -> Path:
        return self.PROJECT_DIR / "elevation"

    @property
    def TROPHY_DIR(self) -> Path:
        return self.PROJECT_DIR / "trophies"

    # --- Audio assets ---
    ASSETS_DIR = PROJECT_ROOT / "assets"
    MUSIC_DIR: Path = ASSETS_DIR / "music"
    INTRO_MUSIC = ASSETS_DIR / "intro.mp3"
    OUTRO_MUSIC = ASSETS_DIR / "outro.mp3"

    MUSIC_VOLUME: float = field(default_factory=lambda: _get_config_value('MUSIC_VOLUME', 0.7))
    RAW_AUDIO_VOLUME: float = field(default_factory=lambda: _get_config_value('RAW_AUDIO_VOLUME', 0.3))
    SELECTED_MUSIC_TRACK: str = field(default_factory=lambda: _get_config_value('SELECTED_MUSIC_TRACK', ""))  # Empty = random

    # --- PiP & minimap overlay ---
    PIP_SCALE_RATIO: float = field(default_factory=lambda: _get_config_value('PIP_SCALE_RATIO', 0.30))
    PIP_MARGIN: int = field(default_factory=lambda: _get_config_value('PIP_MARGIN', 0))
    # Minimap size as fraction of video width (legacy ratio; actual minimap is padded
    # to exactly MAP_W × MAP_W = 390×390 by minimap_prerenderer, not derived from this)
    MINIMAP_SIZE_RATIO: float = field(
        default_factory=lambda: _get_config_value('MINIMAP_SIZE_RATIO', 0.30)
    )
    MINIMAP_MARGIN: int = field(default_factory=lambda: _get_config_value('MINIMAP_MARGIN', 30))
    MINIMAP_ANCHOR: str = "top_right"
    SHOW_ELEVATION_PLOT: bool = field(
        default_factory=lambda: _get_config_value('SHOW_ELEVATION_PLOT', True)
    )
    MAP_ROUTE_COLOR: tuple[int, int, int] = (40, 180, 60)
    MAP_ROUTE_WIDTH: int = 12  # Route width for minimap overlay
    MAP_SPLASH_ROUTE_WIDTH: int = 24  # Route width for splash map (larger image)
    MAP_MARKER_COLOR: tuple[int, int, int] = (230, 175, 0)
    MAP_MARKER_RADIUS: int = 36  # Larger marker for visibility
    MAP_PADDING_PCT: float = 0.25
    MAP_ZOOM_PIP: int = 15
    MAP_ZOOM_SPLASH: int = 12
    MAP_SPLASH_SIZE: tuple[int, int] = (2560, 1440)
    MAP_BASEMAP_PROVIDER: str = field(
        default_factory=lambda: _get_config_value('MAP_BASEMAP_PROVIDER', 'OpenStreetMap.Mapnik')
    )

    # --- HUD ---
    HUD_ANCHOR: str = "bottom_left"
    HUD_SCALE: float = 1.0
    # x=0 → flush left; y=ELEV_STRIP_H → gauge composite sits just above the elevation strip
    HUD_PADDING: tuple[int, int] = (0, 75)
    SPEED_GAUGE_SIZE: int = field(default_factory=lambda: _get_config_value('SPEED_GAUGE_SIZE', 300))
    SMALL_GAUGE_SIZE: int = field(default_factory=lambda: _get_config_value('SMALL_GAUGE_SIZE', 150))
    GAUGE_ORDER: list[str] = field(default_factory=lambda: ["cadence", "hr", "gradient", "speed", "elev"])
    GAUGE_MAXES: dict = field(default_factory=lambda: {
        "speed": 100, "cadence": 130, "hr": 160, "elev": 99999,
        "gradient_min": -25, "gradient_max": 25,
    })

    # --- Bottom-bar geometry ---
    # Output frame: 1920×1080 (source footage 2560×1440 is scaled down at render time)
    # Layout left → right:
    #   [Gauge 972px][MAP 390px][8px gap][PiP ≈693px] → fills to ~2063px; main video behind
    #   Below MAP+PiP: [elevation strip 948px × 75px]
    #
    #   Gauge  x=0..972      y=811..1005  h=194  (5 × 194px cells)
    #   MAP    x=972..1362   y=615..1005  h=390  w=390 (square padded canvas)
    #   PiP    x=1370..~2063 y=615..1005  h=390  w≈693 (scale=-1:390 from 2560×1440 source)
    #   Elev   x=972..1920   y=1005..1080 h=75   w=948
    #
    #   Both MAP and PiP share bottom edge at y=1005 via H-h-MAP_PIP_BOTTOM expression.
    GAUGE_BAR_H: int = 194      # height of each gauge cell
    GAUGE_PAD: int = 8          # padding between cell edge and arc circle
    GAUGE_DIAM: int = 194       # arc circle diameter = GAUGE_COMPOSITE_SIZE[0] // 5
    PIP_H: int = 390            # PiP height; pip_w ≈ 693 (scale=-1:390 from 2560×1440 source)
    MAP_W: int = 390            # Map canvas size (square); matches PIP_H for shared bottom edge
    MAP_GAP: int = 8            # gap in px between map right edge and PiP left edge
    ELEV_STRIP_H: int = 75      # height of elevation strip at very bottom
    # MAP_PIP_BOTTOM = ELEV_STRIP_H → panel bottom at y = H - 75 = 1005 = gauge bottom
    MAP_PIP_BOTTOM: int = 75    # px from frame bottom to bottom edge of map and PiP

    # Composite gauge settings (pre-rendered PNG or video)
    # 5 equal cells × 194px = 972px wide, 194px tall
    GAUGE_COMPOSITE_SIZE: tuple[int, int] = (972, 194)
    GAUGE_LAYOUT: str = field(default_factory=lambda: _get_config_value('GAUGE_LAYOUT', 'strip'))
    ENABLED_GAUGES: list[str] = field(default_factory=lambda: _get_config_value(
        'ENABLED_GAUGES', ["speed", "cadence", "hr", "elev", "gradient"]
    ))
    # Dynamic gauge mode: True = per-second updates (video), False = static PNG per clip
    DYNAMIC_GAUGES: bool = field(default_factory=lambda: _get_config_value('DYNAMIC_GAUGES', True))

    # --- Output resolution ---
    # Source footage (Cycliq) is 2560×1440; all overlay geometry is designed for 1920×1080.
    # The main video is always scaled to OUTPUT_W×OUTPUT_H before overlays are applied.
    OUTPUT_W: int = 1920
    OUTPUT_H: int = 1080

    # --- Encoding ---
    VIDEO_CODEC: str = field(default_factory=lambda: _get_config_value('VIDEO_CODEC', 'libx264'))
    BITRATE: str = field(default_factory=lambda: _get_config_value('BITRATE', '8M'))
    MAXRATE: str = field(default_factory=lambda: _get_config_value('MAXRATE', '12M'))
    BUFSIZE: str = field(default_factory=lambda: _get_config_value('BUFSIZE', '24M'))

DEFAULT_CONFIG = Config()


def reload_config() -> None:
    """
    Reload persistent config and update DEFAULT_CONFIG in-place.

    Updates the existing DEFAULT_CONFIG object rather than replacing it,
    so all modules that imported it as CFG will see the new values.

    Preserves runtime-set values (RIDE_FOLDER, SOURCE_FOLDER, INPUT_BASE_DIR)
    that are set when a project is selected.
    """
    global _PERSISTENT_CONFIG

    # Preserve runtime-set project values before reload
    preserved = {
        'RIDE_FOLDER': DEFAULT_CONFIG.RIDE_FOLDER,
        'SOURCE_FOLDER': DEFAULT_CONFIG.SOURCE_FOLDER,
        'INPUT_BASE_DIR': DEFAULT_CONFIG.INPUT_BASE_DIR,
    }

    # Reload from file
    try:
        from source.utils.persistent_config import load_persistent_config
        _PERSISTENT_CONFIG = load_persistent_config()
    except ImportError:
        _PERSISTENT_CONFIG = {}

    # Create new config with updated values
    new_config = Config()

    # Update DEFAULT_CONFIG in-place so all existing references see new values
    for f in fields(Config):
        if not f.name.startswith('_'):  # Skip private fields
            try:
                setattr(DEFAULT_CONFIG, f.name, getattr(new_config, f.name))
            except AttributeError:
                pass  # Skip properties and computed fields

    # Restore preserved runtime values
    for key, value in preserved.items():
        if value:  # Only restore if it was set
            setattr(DEFAULT_CONFIG, key, value)
