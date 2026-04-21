# source/steps/build_helpers/__init__.py
"""
Build step helper modules for clip rendering and composition.

This package contains focused modules for different build tasks:
- clip_renderer: Individual clip encoding with overlays
- minimap_prerenderer: Batch minimap generation
- elevation_prerenderer: Batch elevation plot generation
- gauge_prerenderer: Composite gauge PNG generation
- segment_concatenator: Multi-segment video assembly
"""

from .clip_renderer import ClipRenderer
from .minimap_prerenderer import MinimapPrerenderer
from .elevation_prerenderer import ElevationPrerenderer
from .gauge_prerenderer import GaugePrerenderer
from .segment_concatenator import SegmentConcatenator
from .cleanup import cleanup_temp_files

__all__ = [
    "ClipRenderer",
    "MinimapPrerenderer",
    "ElevationPrerenderer",
    "GaugePrerenderer",
    "SegmentConcatenator",
    "cleanup_temp_files",
]