"""Tests for storage directory preflight checks."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from storage import check_storage_directory


class StorageCheckTests(unittest.TestCase):
    def test_creates_directory_and_verifies_write_read_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "nested" / "sentinel1"

            result = check_storage_directory(target)

            self.assertTrue(target.is_dir())
            self.assertTrue(result.created)
            self.assertTrue(result.write_read_delete_verified)
            self.assertTrue(result.probe_file_removed)
            self.assertEqual(list(target.iterdir()), [])

    def test_checks_existing_directory_without_removing_user_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "downloads"
            target.mkdir()
            existing_file = target / "keep.txt"
            existing_file.write_text("keep", encoding="utf-8")

            result = check_storage_directory(target)

            self.assertFalse(result.created)
            self.assertTrue(result.write_read_delete_verified)
            self.assertEqual(existing_file.read_text(encoding="utf-8"), "keep")
            self.assertEqual([path.name for path in target.iterdir()], ["keep.txt"])

    def test_rejects_file_as_storage_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "not-a-directory"
            target.write_text("content", encoding="utf-8")

            with self.assertRaises(NotADirectoryError):
                check_storage_directory(target)


if __name__ == "__main__":
    unittest.main()
