"""Unit tests for application configuration defaults and credential checks."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import load_settings


class ConfigTests(unittest.TestCase):
    def test_default_settings_use_smp_buffer_and_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {}, clear=True):
                settings = load_settings(temp_dir)
        self.assertEqual(settings.buffer_m, 150_000)
        self.assertEqual(settings.product_type, "EW_GRDM_1S")
        self.assertEqual(settings.polarization_code, "DH")
        self.assertEqual(
            settings.region_geojson,
            (Path(temp_dir) / "data" / "geometry" / "SMP.geojson").resolve(),
        )


    def test_download_dir_can_be_overridden_with_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            download_dir = Path(temp_dir) / "sentinel1-storage"
            with patch.dict(
                os.environ, {"S1_DOWNLOAD_DIR": str(download_dir)}, clear=True
            ):
                settings = load_settings(temp_dir)
        self.assertEqual(settings.download_dir, download_dir.resolve())

    def test_s3_credentials_are_required_only_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {}, clear=True):
                settings = load_settings(temp_dir)
        with self.assertRaisesRegex(ValueError, "CDSE_ACCESS_KEY"):
            settings.require_s3_credentials()

    def test_valid_s3_credentials_pass_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {"CDSE_ACCESS_KEY": "access-id", "CDSE_SECRET_KEY": "secret-value"},
                clear=True,
            ):
                settings = load_settings(temp_dir)
        settings.require_s3_credentials()

    def test_invalid_buffer_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"S1_BUFFER_METERS": "invalid"}, clear=True):
                with self.assertRaisesRegex(ValueError, "S1_BUFFER_METERS"):
                    load_settings(temp_dir)


if __name__ == "__main__":
    unittest.main()
