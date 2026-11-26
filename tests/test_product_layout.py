"""Tests for Sentinel-1 product folder naming and primary-file views."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from downloader.product_layout import build_product_folder_name, create_product_views, parse_product_name


PRODUCT = "S1C_EW_GRDM_1SDH_20261124T030553_20261124T030653_010473_014EEB_635F.SAFE"


class ProductLayoutTests(unittest.TestCase):
    def test_builds_stable_folder_name(self) -> None:
        self.assertEqual(
            build_product_folder_name(PRODUCT),
            "2026-11-24_030553-030653_EW_S1C_014EEB",
        )

    def test_parses_metadata_as_utc(self) -> None:
        info = parse_product_name(PRODUCT)
        self.assertEqual(info["platform"], "S1C")
        self.assertEqual(info["mode"], "EW")
        self.assertEqual(info["polarization_code"], "DH")
        self.assertEqual(info["datatake_id"], "014EEB")
        self.assertTrue(info["start_utc"].endswith("+00:00"))

    def test_rejects_unrecognized_product_name(self) -> None:
        with self.assertRaises(ValueError):
            build_product_folder_name("not-a-sentinel-product.SAFE")

    def test_rejects_nonpositive_acquisition_duration(self) -> None:
        invalid = "S1C_EW_GRDM_1SDH_20261124T030553_20261124T030553_010473_014EEB_635F.SAFE"
        with self.assertRaises(ValueError):
            parse_product_name(invalid)

    def test_creates_primary_views_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            product_dir = root / "2026-11-24_030553-030653_EW_S1C_014EEB"
            safe = product_dir / "source.SAFE"
            measurement = safe / "measurement"
            preview = safe / "preview"
            measurement.mkdir(parents=True)
            preview.mkdir()
            (measurement / "s1c-ew-grd-hh-test.tiff").write_bytes(b"hh-data")
            (measurement / "s1c-ew-grd-hv-test.tiff").write_bytes(b"hv-data")
            (preview / "quick-look.png").write_bytes(b"png-data")

            result = create_product_views(safe, product_dir, PRODUCT)

            self.assertEqual((product_dir / "HH.tiff").read_bytes(), b"hh-data")
            self.assertEqual((product_dir / "HV.tiff").read_bytes(), b"hv-data")
            self.assertEqual((product_dir / "quick-look.png").read_bytes(), b"png-data")
            metadata = json.loads((product_dir / "product.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["product_name"], PRODUCT)
            self.assertEqual(result["folder_name"], product_dir.name)
            self.assertEqual(metadata["primary_files"]["HH.tiff"], "hardlink")


    def test_quicklook_is_optional(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            product_dir = root / "product"
            safe = product_dir / "source.SAFE"
            measurement = safe / "measurement"
            measurement.mkdir(parents=True)
            (measurement / "s1c-ew-grd-hh-test.tiff").write_bytes(b"hh-data")
            (measurement / "s1c-ew-grd-hv-test.tiff").write_bytes(b"hv-data")

            result = create_product_views(safe, product_dir, PRODUCT)

            self.assertEqual(result["primary_files"]["quick-look.png"], "missing")
            self.assertFalse((product_dir / "quick-look.png").exists())

    def test_requires_exactly_one_file_per_polarization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            safe = root / PRODUCT
            (safe / "measurement").mkdir(parents=True)
            (safe / "preview").mkdir()
            (safe / "measurement" / "image-hh-test.tiff").write_bytes(b"hh")
            (safe / "preview" / "quick-look.png").write_bytes(b"png")
            with self.assertRaisesRegex(ValueError, "HV"):
                create_product_views(safe, root / "product", PRODUCT)


if __name__ == "__main__":
    unittest.main()
