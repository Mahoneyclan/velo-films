# source/gui/controllers/project_controller.py
"""
Project management controller.
Handles project creation, loading, validation, and configuration.
"""

from pathlib import Path
from typing import List, Tuple, Optional, Callable

from ...config import DEFAULT_CONFIG as CFG
from ...utils.log import reconfigure_loggers
from ...utils.log import setup_logger

controller_log = setup_logger("gui.project_controller")

class ProjectController:
    """Manages project CRUD operations and validation."""
    
    def __init__(self, log_callback: Optional[Callable] = None):
        """
        Initialize project controller.
        
        Args:
            log_callback: Function to call for logging (message, level)
        """
        self.current_project: Optional[Path] = None
        self.log = log_callback or (lambda msg, lvl: controller_log.info(f"[{lvl}] {msg}"))
    
    def get_all_projects(self) -> List[Tuple[str, Path]]:
        """
        Get list of all projects in PROJECTS_ROOT.

        Returns:
            List of (project_name, project_path) tuples
        """
        projects = []
        projects_root = CFG.PROJECTS_ROOT

        if not projects_root.exists():
            self.log("Projects folder not found", "error")
            return projects

        for folder in projects_root.iterdir():
            if folder.is_dir():
                projects.append((folder.name, folder))

        return sorted(projects, key=lambda x: x[0])
    
    def select_project(self, project_path: Path) -> bool:
        """
        Select and configure a project.
        
        Args:
            project_path: Path to project folder
            
        Returns:
            True if successful, False otherwise
        """
        try:
            self.current_project = project_path
            CFG.RIDE_FOLDER = project_path.name
            
            # Determine source location
            symlink_path = project_path / "source_videos"
            if symlink_path.exists() and symlink_path.is_symlink():
                # Project-local symlink -> prefer the symlink target as the raw source
                actual_target = symlink_path.resolve()
                # Use the symlink target's parent as INPUT_BASE_DIR and its name as SOURCE_FOLDER
                CFG.INPUT_BASE_DIR = actual_target.parent
                CFG.SOURCE_FOLDER = actual_target.name
                self.log(f"Using project-local symlink (target): {symlink_path} → {actual_target}", "info")
                
            elif (project_path / "source_path.txt").exists():
                # Imported source reference
                source_meta = project_path / "source_path.txt"
                source_path = Path(source_meta.read_text().strip())
                CFG.INPUT_BASE_DIR = source_path.parent
                CFG.SOURCE_FOLDER = source_path.name
                self.log(f"Using imported source: {source_path}", "info")
                
            else:
                # Legacy: project folder is source
                CFG.SOURCE_FOLDER = project_path.name
                self.log("Using project folder as source", "info")
            
            # Reconfigure logging for this project
            reconfigure_loggers()

            # Load project-specific config overrides (e.g., KNOWN_OFFSETS)
            self._load_project_config(project_path)

            return True
            
        except Exception as e:
            self.log(f"Failed to select project: {e}", "error")
            return False
    
    def _load_project_config(self, project_path: Path) -> None:
        """
        Load project-specific config overrides from project_config.json.

        This allows per-project settings like KNOWN_OFFSETS for camera calibration
        and CAMERA_TIMEZONES for per-camera time alignment.

        Args:
            project_path: Path to project folder
        """
        import json
        from datetime import timezone, timedelta
        from ...models import reset_registry

        config_path = project_path / "project_config.json"
        if not config_path.exists():
            return

        try:
            with config_path.open() as f:
                overrides = json.load(f)

            # Apply known overrides to CFG
            if "KNOWN_OFFSETS" in overrides:
                CFG.KNOWN_OFFSETS = dict(overrides["KNOWN_OFFSETS"])
                self.log(f"Loaded project offsets: {CFG.KNOWN_OFFSETS}", "info")
                # Reset camera registry to pick up new offsets
                reset_registry()

            # Allow per-project override of how far before GPX session start to extract frames.
            # Increase this when a camera started recording before the GPX session began.
            if "GPX_GRID_EXTENSION_M" in overrides:
                CFG.GPX_GRID_EXTENSION_M = float(overrides["GPX_GRID_EXTENSION_M"])
                self.log(f"Loaded GPX grid extension: {CFG.GPX_GRID_EXTENSION_M} min", "info")

            # Apply per-camera timezones (new format)
            if "CAMERA_TIMEZONES" in overrides:
                CFG.CAMERA_TIMEZONES = dict(overrides["CAMERA_TIMEZONES"])
                self.log(f"Loaded per-camera timezones: {CFG.CAMERA_TIMEZONES}", "info")
            # Legacy: single timezone for all cameras
            elif "CAMERA_TIMEZONE" in overrides:
                tz_str = overrides["CAMERA_TIMEZONE"]
                # Apply to both cameras for backwards compatibility
                CFG.CAMERA_TIMEZONES = {
                    "Fly12Sport": tz_str,
                    "Fly6Pro": tz_str,
                }
                # Also set the default timezone object for any code still using it
                tz_obj = self._parse_timezone_string(tz_str)
                if tz_obj:
                    CFG.CAMERA_CREATION_TIME_TZ = tz_obj
                self.log(f"Loaded legacy timezone (applied to all cameras): {tz_str}", "info")

            controller_log.info(f"Loaded project config from {config_path}")

        except Exception as e:
            controller_log.warning(f"Failed to load project config: {e}")

    def _parse_timezone_string(self, tz_str: str):
        """
        Parse timezone string to timezone object.

        Supports formats like:
        - "UTC+10:30" or "UTC-5"
        - "+10:30" or "-05:00"
        - "Australia/Adelaide" (requires pytz, fallback to offset parsing)

        Args:
            tz_str: Timezone string

        Returns:
            timezone object or None if parsing fails
        """
        from datetime import timezone, timedelta
        import re

        if not tz_str:
            return None

        # Try named timezone first (requires pytz)
        try:
            import pytz
            return pytz.timezone(tz_str)
        except (ImportError, Exception):
            pass

        # Parse UTC offset format: "UTC+10:30", "+10:30", "UTC-5", etc.
        pattern = r'^(?:UTC)?([+-])?(\d{1,2})(?::(\d{2}))?$'
        match = re.match(pattern, tz_str.strip())

        if match:
            sign = match.group(1) or '+'
            hours = int(match.group(2))
            minutes = int(match.group(3) or 0)

            total_minutes = hours * 60 + minutes
            if sign == '-':
                total_minutes = -total_minutes

            return timezone(timedelta(minutes=total_minutes))

        controller_log.warning(f"Could not parse timezone: {tz_str}")
        return None

    def create_project(self, source_folder: Path, timezone: str = "") -> Optional[Path]:
        """
        Create new project from source folder.

        Args:
            source_folder: Path to folder containing video files
            timezone: Timezone string (e.g., "UTC+10:30") for camera time correction

        Returns:
            Path to created project, or None on failure
        """
        # Validate source folder has videos
        video_extensions = {'.mp4', '.mov', '.avi', '.mkv', '.m4v'}
        video_files = [
            f for f in source_folder.iterdir()
            if f.is_file() and f.suffix.lower() in video_extensions
        ]

        if not video_files:
            self.log("Error: No video files found in source folder", "error")
            return None

        # Check for GPX (warning only)
        gpx_files = list(source_folder.glob("*.gpx"))
        if not gpx_files:
            self.log("Warning: No GPX file found in source folder", "warning")

        # Create project structure
        project_name = source_folder.name
        project_folder = CFG.PROJECTS_ROOT / project_name

        try:
            # Create directories
            project_folder.mkdir(parents=True, exist_ok=True)
            for sub in ["logs", "working", "clips", "frames", "calibration_frames",
                        "splash_assets", "minimaps", "gauges"]:
                (project_folder / sub).mkdir(exist_ok=True)

            # Create symlink to source videos
            video_link = project_folder / "source_videos"
            if not video_link.exists():
                video_link.symlink_to(source_folder)
                self.log(f"Created symlink to source videos: {video_link}", "success")

            # Add metadata file linking to source
            metadata_file = project_folder / "source_path.txt"
            metadata_file.write_text(str(source_folder))

            # Save project config with timezone
            if timezone:
                self._save_project_config(project_folder, {"CAMERA_TIMEZONE": timezone})
                self.log(f"Set timezone: {timezone}", "info")

            self.log(f"Created project: {project_folder}", "success")
            self.log(f"Linked {len(video_files)} video file(s) from source", "info")

            return project_folder

        except Exception as e:
            self.log(f"Error creating project: {str(e)}", "error")
            return None

    def _save_project_config(self, project_path: Path, config: dict) -> None:
        """
        Save config to project_config.json.

        Args:
            project_path: Path to project folder
            config: Dict of config values to save
        """
        import json

        config_path = project_path / "project_config.json"

        # Load existing config if present
        existing = {}
        if config_path.exists():
            try:
                with config_path.open() as f:
                    existing = json.load(f)
            except Exception:
                pass

        # Merge new config
        existing.update(config)

        # Save
        with config_path.open("w") as f:
            json.dump(existing, f, indent=2)

        controller_log.info(f"Saved project config to {config_path}")
    
    def validate_project(self, project_path: Path) -> Tuple[bool, str]:
        """
        Validate project structure and files.
        
        Args:
            project_path: Path to project folder
            
        Returns:
            (is_valid, error_message) tuple
        """
        if not project_path.exists():
            return False, "Project folder does not exist"
        
        # Check required directories
        required_dirs = ["logs", "working", "clips"]
        for dirname in required_dirs:
            if not (project_path / dirname).exists():
                return False, f"Missing required directory: {dirname}"
        
        # Check for source videos
        symlink_path = project_path / "source_videos"
        if symlink_path.exists():
            if not symlink_path.is_symlink():
                return False, "source_videos exists but is not a symlink"
            if not symlink_path.resolve().exists():
                return False, "source_videos symlink is broken"
        
        return True, ""
    
    def get_project_info(self, project_path: Path) -> dict:
        """
        Get project information and statistics.
        
        Args:
            project_path: Path to project folder
            
        Returns:
            Dict with project info (name, source, video_count, etc.)
        """
        info = {
            "name": project_path.name,
            "path": str(project_path),
            "video_count": 0,
            "has_gpx": False,
            "has_extract": False,
            "has_enriched": False,
            "has_select": False,
        }
        
        # Count videos
        symlink_path = project_path / "source_videos"
        if symlink_path.exists() and symlink_path.is_symlink():
            source_path = symlink_path.resolve()
            video_extensions = {'.mp4', '.mov', '.avi', '.mkv', '.m4v'}
            info["video_count"] = len([
                f for f in source_path.iterdir()
                if f.is_file() and f.suffix.lower() in video_extensions
            ])
        
        # Check pipeline progress
        working_dir = project_path / "working"
        if working_dir.exists():
            info["has_extract"] = (working_dir / "extract.csv").exists()
            info["has_enriched"] = (working_dir / "enriched.csv").exists()
            info["has_select"] = (working_dir / "select.csv").exists()
        
        return info