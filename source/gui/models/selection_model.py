# source/gui/models/selection_model.py
"""
Moment selection model for manual clip review.

Pure data model with no Qt dependencies.
Handles loading, saving, and selection state for moment-based clip pairs.
"""

from __future__ import annotations
import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from source.models import get_registry
from source.utils.log import setup_logger

log = setup_logger("gui.models.selection_model")


@dataclass
class Moment:
    """
    A moment in time with up to two camera perspectives.

    Each moment has:
    - moment_id: Unique identifier from select step
    - epoch: Aligned world time (earliest of available cameras)
    - rows: Always a 2-element list [front_row_or_None, rear_row_or_None].
            Either element may be None for single-camera moments.
    """
    moment_id: int
    epoch: float
    rows: List[Optional[Dict]] = field(default_factory=list)

    @property
    def front_row(self) -> Optional[Dict]:
        """Get front camera row (index 0), or None if unavailable."""
        return self.rows[0] if len(self.rows) > 0 else None

    @property
    def rear_row(self) -> Optional[Dict]:
        """Get rear camera row (index 1), or None if unavailable."""
        return self.rows[1] if len(self.rows) > 1 else None

    @property
    def is_single_camera(self) -> bool:
        """True if only one camera perspective is available for this moment."""
        return self.rows[0] is None or self.rows[1] is None

    def get_row(self, primary_idx: int) -> Optional[Dict]:
        """Get row by primary index (0=front, 1=rear). Returns None if not available."""
        if 0 <= primary_idx < len(self.rows):
            return self.rows[primary_idx]
        return None

    def is_selected(self, primary_idx: int) -> bool:
        """Check if perspective at primary_idx is selected (recommended=true)."""
        row = self.get_row(primary_idx)
        return row is not None and row.get("recommended") == "true"

    def has_any_selected(self) -> bool:
        """Check if any perspective is selected."""
        return any(r is not None and r.get("recommended") == "true" for r in self.rows)


class MomentSelectionModel:
    """
    Model for moment-based clip selection.

    Loads paired camera perspectives from select.csv,
    manages selection state, and saves back to CSV.

    Usage:
        model = MomentSelectionModel(csv_path)
        model.load()

        for moment in model.moments:
            # Display moment...

        model.toggle_selection(moment_id=5, primary_idx=0)
        model.save()
    """

    def __init__(self, csv_path: Path):
        """
        Initialize model.

        Args:
            csv_path: Path to select.csv file
        """
        self.csv_path = csv_path
        self._moments: List[Moment] = []
        self._error: Optional[str] = None

    # --------------------------------------------------
    # Properties
    # --------------------------------------------------

    @property
    def moments(self) -> List[Moment]:
        """Read-only list of moments."""
        return self._moments

    @property
    def total_count(self) -> int:
        """Total number of moments."""
        return len(self._moments)

    @property
    def selected_count(self) -> int:
        """Number of moments with a selected perspective."""
        return sum(1 for m in self._moments if m.has_any_selected())

    @property
    def error(self) -> Optional[str]:
        """Last error message, if any."""
        return self._error

    # --------------------------------------------------
    # Load / Save
    # --------------------------------------------------

    def load(self) -> bool:
        """
        Load moments from CSV.

        Returns:
            True if successful, False if error occurred.
            Check self.error for error message.
        """
        self._error = None
        self._moments = []

        if not self.csv_path.exists():
            self._error = "No selection data. Run pipeline steps first."
            return False

        try:
            with self.csv_path.open() as f:
                rows = list(csv.DictReader(f))

            if not rows:
                self._error = "Selection list is empty."
                return False

            log.info(f"[model] Loaded {len(rows)} rows from {self.csv_path.name}")

            # Group rows by moment_id
            by_moment: Dict[str, List[Dict]] = {}
            for r in rows:
                mid = r.get("moment_id")
                if mid in (None, ""):
                    log.warning(f"[model] Row {r.get('index', '?')} missing moment_id")
                    continue
                by_moment.setdefault(str(mid), []).append(r)

            # Build moment objects
            registry = get_registry()
            dropped = 0
            single_camera_count = 0

            for mid, group in by_moment.items():
                front_row: Optional[Dict] = None
                rear_row: Optional[Dict] = None

                for r in group:
                    cam = r.get("camera", "")
                    if registry.is_front_camera(cam):
                        front_row = r
                    elif registry.is_rear_camera(cam):
                        rear_row = r

                # Require at least one camera perspective
                if not front_row and not rear_row:
                    dropped += 1
                    continue

                # Use earliest available aligned world time
                epochs = []
                if front_row:
                    epochs.append(float(front_row.get("abs_time_epoch", 0) or 0.0))
                if rear_row:
                    epochs.append(float(rear_row.get("abs_time_epoch", 0) or 0.0))
                epoch = min(epochs)

                # rows is always [front_or_None, rear_or_None] — position encodes camera identity
                is_single = front_row is None or rear_row is None
                if is_single:
                    single_camera_count += 1

                self._moments.append(Moment(
                    moment_id=int(mid),
                    epoch=epoch,
                    rows=[front_row, rear_row],
                ))

            # Sort by time
            self._moments.sort(key=lambda m: m.epoch)

            dual_count = len(self._moments) - single_camera_count
            log.info(
                f"[model] Created {len(self._moments)} moments "
                f"({dual_count} dual-camera, {single_camera_count} single-camera), "
                f"dropped {dropped} with no camera data, "
                f"{self.selected_count} pre-selected"
            )

            if not self._moments:
                self._error = f"Could not create any moments from {len(rows)} rows."
                return False

            return True

        except Exception as e:
            log.error(f"[model] Load failed: {e}")
            self._error = str(e)
            return False

    def save(self) -> None:
        """
        Save selection state back to CSV.

        Raises:
            IOError: If write fails
        """
        all_rows: List[Dict] = []
        for moment in self._moments:
            all_rows.extend(r for r in moment.rows if r is not None)

        if not all_rows:
            # Write minimal header for empty case
            with self.csv_path.open("w", newline="") as f:
                csv.writer(f).writerow(["index"])
            log.warning("[model] No candidates to save; wrote minimal header")
            return

        # Sort by time
        all_rows.sort(key=lambda r: float(r.get("abs_time_epoch", 0) or 0.0))

        selected_count = sum(1 for r in all_rows if r.get("recommended") == "true")
        log.info(f"[model] Saving {len(all_rows)} rows ({selected_count} recommended)")

        try:
            fieldnames = list(all_rows[0].keys())
            with self.csv_path.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(all_rows)
            log.info(f"[model] Selection saved: {selected_count} clips selected")
        except Exception as e:
            log.error(f"[model] Save failed: {e}")
            raise IOError(f"Failed to save selection: {e}") from e

    # --------------------------------------------------
    # Selection operations
    # --------------------------------------------------

    def toggle_selection(self, moment_id: int, primary_idx: int) -> None:
        """
        Toggle selection for a perspective.

        Rules:
        - At most 1 selected per moment
        - Clicking selected → deselects it (0 selected)
        - Clicking unselected → selects it, deselects other

        Args:
            moment_id: Moment identifier
            primary_idx: Perspective index (0=front, 1=rear)
        """
        moment = self._find_moment(moment_id)
        if not moment:
            log.warning(f"[model] Moment {moment_id} not found")
            return

        selected_row = moment.get_row(primary_idx)
        other_row = moment.get_row(1 - primary_idx)

        if not selected_row:
            return

        currently_selected = selected_row.get("recommended") == "true"

        if currently_selected:
            # Deselect this perspective
            selected_row["recommended"] = "false"
        else:
            # Select this, deselect other
            selected_row["recommended"] = "true"
            if other_row:
                other_row["recommended"] = "false"

    def is_selected(self, moment_id: int, primary_idx: int) -> bool:
        """
        Check if a perspective is selected.

        Args:
            moment_id: Moment identifier
            primary_idx: Perspective index (0=front, 1=rear)

        Returns:
            True if selected (recommended=true)
        """
        moment = self._find_moment(moment_id)
        if not moment:
            return False
        return moment.is_selected(primary_idx)

    def get_moment(self, moment_id: int) -> Optional[Moment]:
        """Get moment by ID."""
        return self._find_moment(moment_id)

    def _find_moment(self, moment_id: int) -> Optional[Moment]:
        """Find moment by ID (linear search, fine for typical sizes)."""
        for m in self._moments:
            if m.moment_id == moment_id:
                return m
        return None

    # --------------------------------------------------
    # Formatting helpers
    # --------------------------------------------------

    @staticmethod
    def format_metadata(row: Dict) -> str:
        """
        Format metadata for display.

        Args:
            row: CSV row dict

        Returns:
            Formatted string like "Speed 25 km/h | Detection 0.8"
        """
        parts = []
        if row.get("speed_kmh"):
            parts.append(f"Speed {row['speed_kmh']} km/h")
        if row.get("detect_score"):
            parts.append(f"Detection {row['detect_score']}")
        if row.get("scene_boost"):
            parts.append(f"Scene {row['scene_boost']}")
        return " | ".join(parts) if parts else "—"
