# source/gui/manual_selection_window.py
"""
Manual selection window for reviewing and refining clip selection.

Uses MomentSelectionModel for data, this file is pure UI.
"""

import logging
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QWidget, QGridLayout, QMessageBox, QFrame
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QPainter

from source.config import DEFAULT_CONFIG as CFG
from source.io_paths import select_path, frames_dir, _mk
from source.utils.log import setup_logger
from source.gui.models import MomentSelectionModel, Moment

log = setup_logger("gui.manual_selection_window")


class ManualSelectionWindow(QDialog):
    """
    Manual selection window with PiP layout for moment-based dual perspectives.

    Each moment shows two cards (front/rear as main), user selects preferred view.
    """

    def __init__(self, project_dir: Path, parent=None):
        super().__init__(parent)
        self.project_dir = project_dir
        self.extract_dir = frames_dir()
        _mk(self.extract_dir)

        self.target_clips = int((CFG.HIGHLIGHT_TARGET_DURATION_M * 60) // CFG.CLIP_OUT_LEN_S)

        # Data model
        self.model = MomentSelectionModel(select_path())

        self.setWindowTitle("Review & Refine Clip Selection")
        self.resize(1400, 900)
        self.setModal(True)

        self._setup_ui()
        self._load_data()

    # --------------------------------------------------
    # Logging helper
    # --------------------------------------------------

    def log(self, message: str, level: str = "info"):
        """Route messages to parent GUI log panel or fallback to file logger."""
        if self.parent() and hasattr(self.parent(), "log"):
            self.parent().log(message, level)
        else:
            level_map = {
                "debug": logging.DEBUG,
                "info": logging.INFO,
                "warning": logging.WARNING,
                "error": logging.ERROR,
                "success": logging.INFO,
            }
            log.log(level_map.get(level, logging.INFO), message)

    # --------------------------------------------------
    # Dialog lifecycle
    # --------------------------------------------------

    def accept(self):
        """Save selection and close dialog."""
        try:
            self.model.save()
            self.log("Manual selection saved", "success")
        except Exception as e:
            self.log(f"Error saving selection: {e}", "error")
            QMessageBox.critical(self, "Save Error", f"Failed to save selection: {e}")
            return
        super().accept()

    # --------------------------------------------------
    # UI setup
    # --------------------------------------------------

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        # Title
        title = QLabel("Review & Refine Clip Selection")
        title.setStyleSheet(
            "font-size: 22px; font-weight: 600; color: #1a1a1a; margin-bottom: 5px;"
        )
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        self.status_label = QLabel("Loading candidate clips...")
        self.status_label.setStyleSheet("font-size: 13px; color: #666; margin-bottom: 10px;")
        layout.addWidget(self.status_label, alignment=Qt.AlignCenter)

        self.counter_label = QLabel("Selected: 0 clips")
        self.counter_label.setStyleSheet(
            "font-size: 16px; font-weight: 600; color: #2D7A4F; "
            "padding: 10px 20px; background-color: #F0F9F4; "
            "border: 2px solid #6EBF8B; border-radius: 6px; margin-bottom: 10px;"
        )
        self.counter_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.counter_label)

        # Scrollable grid
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea { border: 1px solid #E5E5E5; background: #FAFAFA; border-radius: 4px; }"
        )

        self.grid_widget = QWidget()
        self.grid_layout = QGridLayout(self.grid_widget)
        self.grid_layout.setSpacing(20)
        self.grid_layout.setContentsMargins(10, 10, 10, 10)

        scroll.setWidget(self.grid_widget)
        layout.addWidget(scroll)

        # Instructions
        instructions = QLabel(
            "Click a perspective card to select/deselect that camera angle.\n"
            "Both views are shown: primary (main) with opposite camera as PiP.\n"
            "Only one perspective per moment can be selected (or none)."
        )
        instructions.setStyleSheet(
            "color: #666; font-size: 12px; font-style: italic; padding: 10px;"
        )
        instructions.setAlignment(Qt.AlignCenter)
        instructions.setWordWrap(True)
        layout.addWidget(instructions)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        cancel_btn.setStyleSheet(self._button_style(primary=False))

        self.ok_btn = QPushButton("Use 0 Clips & Continue")
        self.ok_btn.clicked.connect(self.accept)
        self.ok_btn.setStyleSheet(self._button_style(primary=True))

        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(self.ok_btn)
        layout.addLayout(btn_layout)

    def _button_style(self, primary: bool) -> str:
        """Generate button stylesheet."""
        if primary:
            return """
                QPushButton {
                    background-color: #2D7A4F;
                    color: white;
                    padding: 10px 20px;
                    font-size: 13px;
                    font-weight: 600;
                    border: 2px solid #2D7A4F;
                    border-radius: 6px;
                }
                QPushButton:hover {
                    background-color: #246840;
                    border-color: #246840;
                }
            """
        else:
            return """
                QPushButton {
                    background-color: #FFFFFF;
                    color: #333333;
                    padding: 10px 20px;
                    font-size: 13px;
                    font-weight: 600;
                    border: 2px solid #DDDDDD;
                    border-radius: 6px;
                }
                QPushButton:hover {
                    background-color: #F8F9FA;
                    border-color: #CCCCCC;
                }
            """

    # --------------------------------------------------
    # Data loading
    # --------------------------------------------------

    def _load_data(self):
        """Load data via model and populate grid."""
        if not self.model.load():
            QMessageBox.critical(self, "Error", self.model.error or "Failed to load data")
            self.reject()
            return

        self.log(f"Loaded {self.model.total_count} moments", "info")
        self._update_counters()
        self._populate_grid()

    def _update_counters(self):
        """Update counter label and button text."""
        selected = self.model.selected_count
        total = self.model.total_count
        single_cam = sum(1 for m in self.model.moments if m.is_single_camera)
        dual_cam = total - single_cam

        self.counter_label.setText(f"Selected: {selected} / {total} clips")
        self.ok_btn.setText(f"Use {selected} Clips & Continue")
        parts = [f"Showing {total} moments"]
        if single_cam:
            parts.append(f"{dual_cam} dual-camera, {single_cam} single-camera")
        self.status_label.setText(
            f"{' • '.join(parts)}  •  "
            f"Pre-selected: {selected} / {self.target_clips} target"
        )

    # --------------------------------------------------
    # Grid population
    # --------------------------------------------------

    def _populate_grid(self):
        """Populate grid with moment cards."""
        # Clear existing
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Two columns: both perspectives side-by-side.
        # For single-camera moments, the missing camera column shows a placeholder.
        for row_idx, moment in enumerate(self.model.moments):
            try:
                if moment.is_single_camera:
                    available_idx = 0 if moment.rows[0] is not None else 1
                    missing_idx = 1 - available_idx

                    card = self._create_perspective_card(moment, primary_idx=available_idx)
                    placeholder = self._create_placeholder_card(missing_idx)

                    self.grid_layout.addWidget(card, row_idx, available_idx)
                    self.grid_layout.addWidget(placeholder, row_idx, missing_idx)
                else:
                    card1 = self._create_perspective_card(moment, primary_idx=0)
                    card2 = self._create_perspective_card(moment, primary_idx=1)

                    self.grid_layout.addWidget(card1, row_idx, 0)
                    self.grid_layout.addWidget(card2, row_idx, 1)
            except Exception as e:
                self.log(f"Failed to create widget for moment {row_idx}: {e}", "error")

    # --------------------------------------------------
    # Card creation
    # --------------------------------------------------

    def _create_perspective_card(self, moment: Moment, primary_idx: int) -> QWidget:
        """
        Create a perspective card with PiP layout.

        Args:
            moment: Moment data object
            primary_idx: 0=front camera main, 1=rear camera main
        """
        container = QFrame()
        container.setFrameShape(QFrame.Box)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # Store references for click handling
        container.setProperty("moment_id", moment.moment_id)
        container.setProperty("primary_idx", primary_idx)

        primary_row = moment.get_row(primary_idx)
        partner_row = moment.get_row(1 - primary_idx)

        if not primary_row:
            return container

        # PiP composite image
        pip_widget = self._create_pip_widget(primary_row, partner_row)
        layout.addWidget(pip_widget)

        # Single Camera badge (shown when partner camera was unavailable)
        if moment.is_single_camera:
            cam_name = primary_row.get("camera", "Camera")
            single_cam_badge = QLabel(f"Single Camera  ({cam_name})")
            single_cam_badge.setAlignment(Qt.AlignCenter)
            single_cam_badge.setStyleSheet(
                "font-size: 11px; font-weight: 700; color: #5D4037; "
                "background-color: #FFF8E1; padding: 3px 8px; "
                "border: 1px solid #FFCA28; border-radius: 4px; margin: 2px 0;"
            )
            layout.addWidget(single_cam_badge)

        # Strava PR badge (if applicable)
        is_strava_pr = primary_row.get("strava_pr") == "true"
        if is_strava_pr:
            pr_badge = QLabel("🏆 Strava Segment PR")
            pr_badge.setAlignment(Qt.AlignCenter)
            pr_badge.setStyleSheet(
                "font-size: 12px; font-weight: 700; color: #FF6B00; "
                "background-color: #FFF3E0; padding: 4px 8px; "
                "border: 2px solid #FF9800; border-radius: 4px; margin: 2px 0;"
            )
            layout.addWidget(pr_badge)

        # Metadata with timestamp for alignment debugging
        camera_label = primary_row.get("camera", "Camera")
        source_file = primary_row.get("source", "")
        frame_num = primary_row.get("frame_number", "—")
        abs_time = primary_row.get("abs_time_iso", "")[:19]  # Trim to YYYY-MM-DDTHH:MM:SS

        metadata_lines = [
            f"⏱ {abs_time}" if abs_time else "",
            f"Camera: {camera_label} | File: {source_file} | Frame {frame_num}",
            MomentSelectionModel.format_metadata(primary_row),
        ]
        metadata_lines = [line for line in metadata_lines if line]  # Remove empty lines

        metadata = QLabel("\n".join(metadata_lines))
        metadata.setAlignment(Qt.AlignCenter)
        metadata.setStyleSheet("font-size: 11px; color: #666;")
        metadata.setWordWrap(True)
        layout.addWidget(metadata)

        # Click handler
        container.mousePressEvent = lambda e: self._on_card_clicked(container)

        # Apply selection styling
        self._apply_card_style(container, moment.is_selected(primary_idx))

        return container

    def _create_pip_widget(self, primary_row: Dict, partner_row: Optional[Dict]) -> QLabel:
        """Create a QLabel with PiP composite image."""
        label = QLabel()
        label.setAlignment(Qt.AlignCenter)

        primary_idx = primary_row.get("index", "")
        primary_path = self.extract_dir / f"{primary_idx}_Primary.jpg"

        if not primary_path.exists():
            label.setText(f"[Missing: {primary_path.name}]")
            label.setStyleSheet("color: #999; background-color: #f0f0f0;")
            label.setMinimumSize(640, 360)
            return label

        partner_path = None
        if partner_row:
            partner_idx = partner_row.get("index", "")
            partner_path = self.extract_dir / f"{partner_idx}_Primary.jpg"

        composite = self._create_pip_composite(primary_path, partner_path)
        if composite:
            label.setPixmap(composite)
        else:
            label.setText("[Error creating PiP]")
            label.setMinimumSize(640, 360)

        return label

    def _create_pip_composite(self, primary_path: Path, partner_path: Optional[Path]) -> Optional[QPixmap]:
        """Create PiP composite from two images."""
        primary = QPixmap(str(primary_path))
        if primary.isNull():
            return None

        display_width = 640
        display_height = 360
        primary = primary.scaled(
            display_width,
            display_height,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )

        if partner_path and partner_path.exists():
            partner = QPixmap(str(partner_path))
            if not partner.isNull():
                pip_scale = 0.30
                pip_margin = 15
                pip_width = int(display_width * pip_scale)
                pip_height = int(pip_width * 9 / 16)

                partner = partner.scaled(
                    pip_width,
                    pip_height,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )

                painter = QPainter(primary)
                pip_x = display_width - pip_width - pip_margin
                pip_y = display_height - pip_height - pip_margin

                painter.setOpacity(0.95)
                painter.drawPixmap(pip_x, pip_y, partner)
                painter.end()

        return primary

    def _create_placeholder_card(self, missing_idx: int) -> QWidget:
        """
        Create a non-interactive placeholder card for a missing camera perspective.

        Args:
            missing_idx: 0 if front camera is missing, 1 if rear camera is missing
        """
        camera_label = "Front Camera (Fly12Sport)" if missing_idx == 0 else "Rear Camera (Fly6Pro)"

        container = QFrame()
        container.setFrameShape(QFrame.Box)
        container.setStyleSheet(
            "QFrame { background-color: #E0E0E0; border: 2px dashed #BDBDBD; border-radius: 8px; }"
        )
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # Grey image placeholder matching the PiP widget dimensions
        image_placeholder = QLabel()
        image_placeholder.setMinimumSize(640, 360)
        image_placeholder.setAlignment(Qt.AlignCenter)
        image_placeholder.setStyleSheet(
            "background-color: #424242; border-radius: 4px; color: #9E9E9E; font-size: 13px;"
        )
        image_placeholder.setText("No footage")
        layout.addWidget(image_placeholder)

        # Label identifying which camera is absent
        absent_label = QLabel(f"No footage — {camera_label}")
        absent_label.setAlignment(Qt.AlignCenter)
        absent_label.setStyleSheet(
            "font-size: 12px; font-weight: 600; color: #757575; padding: 4px;"
        )
        layout.addWidget(absent_label)

        return container

    # --------------------------------------------------
    # Selection handling
    # --------------------------------------------------

    def _on_card_clicked(self, container: QFrame):
        """Handle perspective card click."""
        moment_id = container.property("moment_id")
        primary_idx = container.property("primary_idx")

        if moment_id is None or primary_idx is None:
            return

        # Toggle selection in model
        self.model.toggle_selection(moment_id, primary_idx)

        # Update styling for both cards of this moment
        self._refresh_moment_cards(moment_id)

        # Update counters
        self._update_counters()

    def _refresh_moment_cards(self, moment_id: int):
        """Refresh styling for all cards of a moment."""
        moment = self.model.get_moment(moment_id)
        if not moment:
            return

        for i in range(self.grid_layout.count()):
            widget = self.grid_layout.itemAt(i).widget()
            if isinstance(widget, QFrame):
                if widget.property("moment_id") == moment_id:
                    idx = widget.property("primary_idx")
                    if idx is not None:
                        is_selected = moment.is_selected(idx)
                        self._apply_card_style(widget, is_selected)

    def _apply_card_style(self, container: QFrame, is_selected: bool):
        """Apply styling based on selection state."""
        container.setStyleSheet(
            f"""
            QFrame {{
                background-color: {'#E8F5E9' if is_selected else '#FAFAFA'};
                border: {'3' if is_selected else '2'}px solid {'#4CAF50' if is_selected else '#DDDDDD'};
                border-radius: 8px;
            }}
            QFrame:hover {{
                border-color: {'#2E7D32' if is_selected else '#999999'};
                background-color: {'#C8E6C9' if is_selected else '#F5F5F5'};
            }}
            """
        )
