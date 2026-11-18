"""Geographic search-area construction utilities."""

from .region_manager import (
    RegionSummary,
    build_search_area,
    load_source_geometry,
    save_search_area,
)

__all__ = [
    "RegionSummary",
    "build_search_area",
    "load_source_geometry",
    "save_search_area",
]
