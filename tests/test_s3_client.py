"""Unit tests for CDSE S3 path normalization and access checks."""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from config import load_settings
from downloader.s3_client import CDSES3Client


class S3ClientTests(unittest.TestCase):
    def test_normalize_odata_s3_path(self) -> None:
        self.assertEqual(
            CDSES3Client.normalize_s3_path(
                "/eodata/Sentinel-1/SAR/GRD/2026/11/17/product.SAFE",
                "eodata",
            ),
            "Sentinel-1/SAR/GRD/2026/11/17/product.SAFE",
        )

    def test_normalize_s3_uri(self) -> None:
        self.assertEqual(
            CDSES3Client.normalize_s3_path(
                "s3://eodata/Sentinel-1/SAR/GRD/product.SAFE",
                "eodata",
            ),
            "Sentinel-1/SAR/GRD/product.SAFE",
        )

    def test_wrong_bucket_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CDSES3Client.normalize_s3_path(
                "s3://other-bucket/Sentinel-1/product.SAFE",
                "eodata",
            )

    def test_access_check_requests_only_one_object(self) -> None:
        settings = load_settings()
        settings = settings.__class__(
            **{
                **settings.__dict__,
                "cdse_access_key": "test-access",
                "cdse_secret_key": "test-secret",
            }
        )
        fake_client = Mock()
        fake_client.list_objects_v2.return_value = {
            "Contents": [{"Key": "Sentinel-1/SAR/EW_GRDM_1S/2026/11/10/example/manifest.safe"}]
        }
        client = CDSES3Client(settings, client=fake_client)
        result = client.check_access()
        fake_client.list_objects_v2.assert_called_once_with(
            Bucket="eodata",
            Prefix="Sentinel-1/SAR/EW_GRDM_1S/",
            MaxKeys=1,
        )
        self.assertEqual(result.visible_objects, 1)
        self.assertEqual(result.checked_prefix, "Sentinel-1/SAR/EW_GRDM_1S/")


    def test_default_check_prefix_matches_odata_product_storage_path(self) -> None:
        settings = load_settings()
        self.assertEqual(
            settings.cdse_s3_check_prefix,
            "Sentinel-1/SAR/EW_GRDM_1S/",
        )


if __name__ == "__main__":
    unittest.main()
