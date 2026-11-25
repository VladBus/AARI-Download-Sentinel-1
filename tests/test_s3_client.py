"""Unit tests for CDSE S3 path normalization, inventory and downloads."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from config import load_settings
from downloader.s3_client import CDSES3Client, S3ConnectionError


class S3ClientTests(unittest.TestCase):
    def _settings_with_credentials(self):
        settings = load_settings()
        return settings.__class__(
            **{
                **settings.__dict__,
                "cdse_access_key": "test-access",
                "cdse_secret_key": "test-secret",
            }
        )

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
        fake_client = Mock()
        fake_client.list_objects_v2.return_value = {
            "Contents": [{"Key": "Sentinel-1/SAR/EW_GRDM_1S/2026/11/10/example/manifest.safe"}]
        }
        client = CDSES3Client(self._settings_with_credentials(), client=fake_client)
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

    def test_product_prefix_is_exact_and_has_trailing_slash(self) -> None:
        fake_client = Mock()
        fake_client.list_objects_v2.return_value = {
            "Contents": [
                {
                    "Key": "Sentinel-1/SAR/EW_GRDM_1S/2026/11/24/example.SAFE/manifest.safe",
                    "Size": 12345,
                    "StorageClass": "STANDARD",
                }
            ],
            "IsTruncated": False,
        }
        result = CDSES3Client(
            self._settings_with_credentials(), client=fake_client
        ).check_product_prefix(
            "/eodata/Sentinel-1/SAR/EW_GRDM_1S/2026/11/24/example.SAFE"
        )
        fake_client.list_objects_v2.assert_called_once_with(
            Bucket="eodata",
            Prefix="Sentinel-1/SAR/EW_GRDM_1S/2026/11/24/example.SAFE/",
            MaxKeys=5,
        )
        self.assertEqual(result.objects_returned, 1)
        self.assertEqual(result.sample_objects[0]["size_bytes"], 12345)
        self.assertFalse(result.is_truncated)

    def test_inventory_reads_all_pages_and_sums_sizes(self) -> None:
        prefix = "Sentinel-1/SAR/EW_GRDM_1S/2026/11/24/example.SAFE/"
        fake_client = Mock()
        fake_client.list_objects_v2.side_effect = [
            {
                "Contents": [{"Key": prefix + "manifest.safe", "Size": 100}],
                "IsTruncated": True,
                "NextContinuationToken": "next-page",
            },
            {
                "Contents": [{"Key": prefix + "measurement/image.tiff", "Size": 900}],
                "IsTruncated": False,
            },
        ]
        result = CDSES3Client(
            self._settings_with_credentials(), client=fake_client
        ).inspect_product("/eodata/" + prefix.rstrip("/"))

        self.assertEqual(result.object_count, 2)
        self.assertEqual(result.total_size_bytes, 1000)
        self.assertEqual(len(result.sample_objects), 2)
        self.assertEqual(fake_client.list_objects_v2.call_count, 2)
        second_call = fake_client.list_objects_v2.call_args_list[1]
        self.assertEqual(second_call.kwargs["ContinuationToken"], "next-page")

    def test_inventory_rejects_truncated_response_without_next_token(self) -> None:
        fake_client = Mock()
        fake_client.list_objects_v2.return_value = {
            "Contents": [],
            "IsTruncated": True,
        }
        client = CDSES3Client(
            self._settings_with_credentials(), client=fake_client
        )
        with self.assertRaises(S3ConnectionError):
            client.inspect_product(
                "/eodata/Sentinel-1/SAR/EW_GRDM_1S/2026/11/24/example.SAFE"
            )

    def test_download_product_preserves_structure_and_checks_sizes(self) -> None:
        prefix = "Sentinel-1/SAR/EW_GRDM_1S/2026/11/24/example.SAFE/"
        file_payloads = {
            prefix + "manifest.safe": b"manifest-data",
            prefix + "annotation/calibration.xml": b"xml-data",
        }
        fake_client = Mock()
        fake_client.list_objects_v2.return_value = {
            "Contents": [
                {"Key": key, "Size": len(value)}
                for key, value in file_payloads.items()
            ],
            "IsTruncated": False,
        }

        def fake_download_file(bucket, key, filename, Config, Callback):
            payload = file_payloads[key]
            Path(filename).write_bytes(payload)
            Callback(len(payload))

        fake_client.download_file.side_effect = fake_download_file
        client = CDSES3Client(
            self._settings_with_credentials(), client=fake_client
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = client.download_product(
                "/eodata/" + prefix.rstrip("/"),
                "example.SAFE",
                temp_dir,
            )
            product_dir = Path(temp_dir) / "example.SAFE"
            self.assertEqual(
                (product_dir / "manifest.safe").read_bytes(),
                b"manifest-data",
            )
            self.assertEqual(
                (product_dir / "annotation" / "calibration.xml").read_bytes(),
                b"xml-data",
            )
            self.assertEqual(result.objects_total, 2)
            self.assertEqual(result.objects_downloaded, 2)
            self.assertEqual(result.objects_skipped, 0)
            self.assertEqual(result.total_size_bytes, len(b"manifest-data") + len(b"xml-data"))

    def test_download_skips_files_already_present_with_matching_size(self) -> None:
        prefix = "Sentinel-1/SAR/EW_GRDM_1S/2026/11/24/example.SAFE/"
        file_payload = b"same-size"
        fake_client = Mock()
        fake_client.list_objects_v2.return_value = {
            "Contents": [
                {"Key": prefix + "manifest.safe", "Size": len(file_payload)}
            ],
            "IsTruncated": False,
        }
        fake_client.download_file.side_effect = AssertionError(
            "Файл с совпадающим размером не должен скачиваться повторно."
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            product_dir = Path(temp_dir) / "example.SAFE"
            product_dir.mkdir()
            (product_dir / "manifest.safe").write_bytes(file_payload)

            result = CDSES3Client(
                self._settings_with_credentials(), client=fake_client
            ).download_product(
                "/eodata/" + prefix.rstrip("/"),
                "example.SAFE",
                temp_dir,
            )

        fake_client.download_file.assert_not_called()
        self.assertEqual(result.objects_downloaded, 0)
        self.assertEqual(result.objects_skipped, 1)
        self.assertEqual(result.skipped_size_bytes, len(file_payload))

    def test_download_rejects_path_traversal_before_writing(self) -> None:
        prefix = "Sentinel-1/SAR/EW_GRDM_1S/2026/11/24/example.SAFE/"
        fake_client = Mock()
        fake_client.list_objects_v2.return_value = {
            "Contents": [
                {"Key": prefix + "../outside.txt", "Size": 1}
            ],
            "IsTruncated": False,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                CDSES3Client(
                    self._settings_with_credentials(), client=fake_client
                ).download_product(
                    "/eodata/" + prefix.rstrip("/"),
                    "example.SAFE",
                    temp_dir,
                )
            self.assertFalse((Path(temp_dir) / "outside.txt").exists())

    def test_download_rejects_wrong_local_size(self) -> None:
        prefix = "Sentinel-1/SAR/EW_GRDM_1S/2026/11/24/example.SAFE/"
        fake_client = Mock()
        fake_client.list_objects_v2.return_value = {
            "Contents": [
                {"Key": prefix + "manifest.safe", "Size": 10}
            ],
            "IsTruncated": False,
        }

        def fake_download_file(bucket, key, filename, Config, Callback):
            Path(filename).write_bytes(b"short")
            Callback(5)

        fake_client.download_file.side_effect = fake_download_file
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(S3ConnectionError):
                CDSES3Client(
                    self._settings_with_credentials(), client=fake_client
                ).download_product(
                    "/eodata/" + prefix.rstrip("/"),
                    "example.SAFE",
                    temp_dir,
                )

    def test_empty_product_prefix_raises_file_not_found(self) -> None:
        fake_client = Mock()
        fake_client.list_objects_v2.return_value = {
            "Contents": [],
            "IsTruncated": False,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(FileNotFoundError):
                CDSES3Client(
                    self._settings_with_credentials(), client=fake_client
                ).download_product(
                    "/eodata/Sentinel-1/SAR/EW_GRDM_1S/2026/11/24/example.SAFE",
                    "example.SAFE",
                    temp_dir,
                )


if __name__ == "__main__":
    unittest.main()
