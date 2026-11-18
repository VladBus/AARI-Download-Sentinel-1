"""Unit tests for the SMP search-area geometry module."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import shape

from geometry.region_manager import build_search_area, save_search_area


class RegionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.source_path = Path(self.temp_dir.name) / "dateline_region.geojson"
        document = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [170.0, 70.0],
                            [-170.0, 70.0],
                            [-170.0, 75.0],
                            [170.0, 75.0],
                            [170.0, 70.0],
                        ]],
                    },
                }
            ],
        }
        self.source_path.write_text(json.dumps(document), encoding="utf-8")

    def test_buffer_is_positive_and_result_is_valid(self) -> None:
        result = build_search_area(self.source_path, buffer_m=150_000)
        self.assertTrue(result.geometry.is_valid)
        self.assertGreater(result.search_area_km2, result.source_area_km2)
        self.assertEqual(result.buffer_m, 150_000)

    def test_antimeridian_is_split_into_normalized_polygons(self) -> None:
        result = build_search_area(self.source_path, buffer_m=150_000)
        self.assertGreaterEqual(result.polygon_count, 2)
        for polygon in result.geometry.geoms:
            min_lon, _, max_lon, _ = polygon.bounds
            self.assertGreaterEqual(min_lon, -180.000001)
            self.assertLessEqual(max_lon, 180.000001)
            self.assertTrue(polygon.is_valid)

    def test_generated_geojson_can_be_reopened(self) -> None:
        result = build_search_area(self.source_path, buffer_m=150_000)
        target = Path(self.temp_dir.name) / "search_area.geojson"
        save_search_area(result, target)
        generated = json.loads(target.read_text(encoding="utf-8"))
        geometry = shape(generated["features"][0]["geometry"])
        self.assertTrue(geometry.is_valid)
        self.assertEqual(geometry.geom_type, "MultiPolygon")

    def test_invalid_buffer_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_search_area(self.source_path, buffer_m=0)


if __name__ == "__main__":
    unittest.main()
