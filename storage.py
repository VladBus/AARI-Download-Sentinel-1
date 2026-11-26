"""Filesystem preflight checks for Sentinel-1 product storage."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile


@dataclass(frozen=True)
class StorageCheckResult:
    """Result of a write/read/delete check against a storage directory."""

    directory: str
    created: bool
    write_read_delete_verified: bool
    probe_file_removed: bool


def check_storage_directory(directory: str | Path) -> StorageCheckResult:
    """Create the storage directory if needed and verify write/read/delete access.

    The temporary probe is created inside the target directory so the check
    exercises the same filesystem and permissions that product downloads use.
    No existing user files are opened or modified.
    """
    target = Path(directory).expanduser().resolve()
    existed_before = target.exists()

    if existed_before and not target.is_dir():
        raise NotADirectoryError(f"Путь хранилища не является каталогом: {target}")

    target.mkdir(parents=True, exist_ok=True)
    probe_path: Path | None = None
    verified = False

    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=".aari_sentinel1_storage_check_",
            suffix=".tmp",
            dir=target,
            delete=False,
        ) as probe:
            probe_path = Path(probe.name)
            payload = b"AARI Sentinel-1 storage write/read test\n"
            probe.write(payload)
            probe.flush()
            os.fsync(probe.fileno())
            probe.seek(0)
            verified = probe.read() == payload

        if not verified:
            raise OSError(f"Не удалось проверить чтение тестового файла в {target}")

    finally:
        if probe_path is not None:
            probe_path.unlink(missing_ok=True)

    probe_removed = probe_path is None or not probe_path.exists()
    if not probe_removed:
        raise OSError(f"Не удалось удалить временный тестовый файл: {probe_path}")

    return StorageCheckResult(
        directory=str(target),
        created=not existed_before,
        write_read_delete_verified=verified and probe_removed,
        probe_file_removed=probe_removed,
    )
