"""S3-compatible client for Copernicus Data Space Ecosystem."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from tqdm import tqdm

from config import Settings


class S3ConnectionError(RuntimeError):
    """Raised when CDSE object storage cannot be reached or accessed."""


@dataclass(frozen=True)
class S3CheckResult:
    endpoint_url: str
    bucket: str
    checked_prefix: str
    visible_objects: int
    sample_key: str | None


@dataclass(frozen=True)
class S3ProductCheckResult:
    """A small listing from the exact S3 prefix of one OData product."""

    endpoint_url: str
    bucket: str
    product_prefix: str
    objects_returned: int
    is_truncated: bool
    sample_objects: list[dict[str, Any]]


@dataclass(frozen=True)
class S3ProductInventoryResult:
    """Complete object inventory for one SAFE product."""

    endpoint_url: str
    bucket: str
    product_prefix: str
    object_count: int
    total_size_bytes: int
    sample_objects: list[dict[str, Any]]


@dataclass(frozen=True)
class S3ProductDownloadResult:
    """Summary of a completed or resumed SAFE product download."""

    endpoint_url: str
    bucket: str
    product_prefix: str
    product_name: str
    local_path: str
    safe_path: str
    objects_total: int
    objects_downloaded: int
    objects_skipped: int
    total_size_bytes: int
    downloaded_size_bytes: int
    skipped_size_bytes: int
    manifest_path: str


@dataclass(frozen=True)
class _S3Object:
    key: str
    size_bytes: int


class CDSES3Client:
    """Create an authenticated boto3 S3 client using local .env settings."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        settings.require_s3_credentials()
        self.bucket_name = settings.cdse_s3_bucket
        self.endpoint_url = settings.cdse_s3_endpoint_url
        self.check_prefix = settings.cdse_s3_check_prefix
        self.client = client or boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=settings.cdse_access_key,
            aws_secret_access_key=settings.cdse_secret_key,
            region_name=settings.cdse_s3_region_name,
            config=Config(
                signature_version="s3v4",
                connect_timeout=20,
                read_timeout=60,
                max_pool_connections=10,
                retries={"max_attempts": 5, "mode": "standard"},
            ),
        )

    def _list_objects(self, prefix: str, max_keys: int) -> dict[str, Any]:
        try:
            return self.client.list_objects_v2(
                Bucket=self.bucket_name,
                Prefix=prefix,
                MaxKeys=max_keys,
            )
        except ClientError as exc:
            error = exc.response.get("Error", {})
            code = error.get("Code", "Unknown")
            message = error.get("Message", "No additional details")
            raise S3ConnectionError(
                f"CDSE S3 отклонил запрос ({code}): {message}"
            ) from exc
        except BotoCoreError as exc:
            raise S3ConnectionError(
                f"Не удалось выполнить запрос к CDSE S3: {exc}"
            ) from exc

    def check_access(self, prefix: str | None = None) -> S3CheckResult:
        """Check a catalogue prefix. A valid empty response is not a data-access error."""
        checked_prefix = (prefix or self.check_prefix).lstrip("/")
        response = self._list_objects(checked_prefix, max_keys=1)

        objects = response.get("Contents") or []
        sample_key = objects[0].get("Key") if objects else None
        return S3CheckResult(
            endpoint_url=self.endpoint_url,
            bucket=self.bucket_name,
            checked_prefix=checked_prefix,
            visible_objects=len(objects),
            sample_key=sample_key,
        )

    def check_product_prefix(
        self, s3_path: str, max_keys: int = 5
    ) -> S3ProductCheckResult:
        """List up to max_keys objects under the exact SAFE product prefix."""
        if max_keys < 1 or max_keys > 1000:
            raise ValueError("max_keys должен находиться в диапазоне 1–1000.")

        product_prefix = self._product_prefix(s3_path)
        response = self._list_objects(product_prefix, max_keys=max_keys)

        sample_objects = [
            {
                "key": str(item.get("Key") or ""),
                "size_bytes": int(item.get("Size") or 0),
                "storage_class": item.get("StorageClass"),
            }
            for item in response.get("Contents", [])
        ]
        return S3ProductCheckResult(
            endpoint_url=self.endpoint_url,
            bucket=self.bucket_name,
            product_prefix=product_prefix,
            objects_returned=len(sample_objects),
            is_truncated=bool(response.get("IsTruncated", False)),
            sample_objects=sample_objects,
        )

    def inspect_product(self, s3_path: str, sample_limit: int = 10) -> S3ProductInventoryResult:
        """List every object in one SAFE prefix and calculate its total size."""
        if sample_limit < 1 or sample_limit > 100:
            raise ValueError("sample_limit должен находиться в диапазоне 1–100.")

        product_prefix = self._product_prefix(s3_path)
        objects = self._list_product_objects(product_prefix)
        return S3ProductInventoryResult(
            endpoint_url=self.endpoint_url,
            bucket=self.bucket_name,
            product_prefix=product_prefix,
            object_count=len(objects),
            total_size_bytes=sum(item.size_bytes for item in objects),
            sample_objects=[
                {"key": item.key, "size_bytes": item.size_bytes}
                for item in objects[:sample_limit]
            ],
        )

    def _write_download_manifest(self, path: Path, manifest: dict[str, Any]) -> None:
        """Atomically persist the current state of a product download."""
        temporary_path = path.with_name(path.name + ".tmp")
        manifest["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
        temporary_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, path)

    def download_product(
        self,
        s3_path: str,
        product_name: str,
        destination_dir: str | Path,
        folder_name: str | None = None,
    ) -> S3ProductDownloadResult:
        """Download one SAFE product using atomic per-file publication.

        Objects are first downloaded to a .part file. The final filename is only
        published after the transfer completes and its byte size matches S3.
        A JSON manifest is atomically updated after every object so interrupted
        downloads can be audited and safely resumed.
        """
        self._validate_product_name(product_name)
        product_prefix = self._product_prefix(s3_path)
        objects = self._list_product_objects(product_prefix)
        if not objects:
            raise FileNotFoundError(
                f"В CDSE S3 не найдено объектов для продукта {product_name}."
            )

        output_root = Path(destination_dir).expanduser().resolve()
        local_folder_name = folder_name or product_name
        self._validate_local_folder_name(local_folder_name)
        product_root = (output_root / local_folder_name).resolve()
        safe_root = product_root / "source.SAFE" if folder_name else product_root
        planned_files: list[tuple[_S3Object, Path]] = []

        # Validate every object key before creating or writing any local files.
        for item in objects:
            relative_path = self._safe_relative_path(item.key, product_prefix)
            local_path = (safe_root / relative_path).resolve()
            if not local_path.is_relative_to(safe_root.resolve()):
                raise ValueError(
                    f"Недопустимый путь объекта S3: {item.key!r}."
                )
            planned_files.append((item, local_path))

        product_root.mkdir(parents=True, exist_ok=True)
        safe_root.mkdir(parents=True, exist_ok=True)
        manifest_path = (
            product_root / "download_manifest.json"
            if folder_name
            else output_root / f"{product_name}.download_manifest.json"
        )
        total_size = sum(item.size_bytes for item in objects)
        downloaded_count = 0
        skipped_count = 0
        downloaded_size = 0
        skipped_size = 0
        object_states: dict[str, dict[str, Any]] = {
            item.key: {
                "relative_path": local_path.relative_to(safe_root).as_posix(),
                "size_bytes": item.size_bytes,
                "status": "pending",
                "verification": None,
            }
            for item, local_path in planned_files
        }
        manifest: dict[str, Any] = {
            "schema": "aari_sentinel1_download_v1",
            "product_name": product_name,
            "bucket": self.bucket_name,
            "product_prefix": product_prefix,
            "status": "in_progress",
            "objects_total": len(objects),
            "total_size_bytes": total_size,
            "objects": object_states,
        }
        self._write_download_manifest(manifest_path, manifest)

        transfer_config = TransferConfig(
            multipart_threshold=64 * 1024 * 1024,
            multipart_chunksize=64 * 1024 * 1024,
            max_concurrency=4,
            num_download_attempts=5,
            use_threads=True,
        )

        with tqdm(
            total=total_size,
            unit="B",
            unit_scale=True,
            desc=product_name,
            dynamic_ncols=True,
        ) as progress:
            for item, local_path in planned_files:
                state = object_states[item.key]
                if local_path.is_file() and local_path.stat().st_size == item.size_bytes:
                    skipped_count += 1
                    skipped_size += item.size_bytes
                    state["status"] = "skipped_existing"
                    state["verification"] = "size_match"
                    progress.update(item.size_bytes)
                    self._write_download_manifest(manifest_path, manifest)
                    continue

                if local_path.exists() and not local_path.is_file():
                    raise S3ConnectionError(
                        f"По пути файла уже существует каталог: {local_path}"
                    )

                local_path.parent.mkdir(parents=True, exist_ok=True)
                temporary_path = local_path.with_name(local_path.name + ".part")
                if temporary_path.is_dir():
                    raise S3ConnectionError(
                        f"Временный путь загрузки занят каталогом: {temporary_path}"
                    )
                temporary_path.unlink(missing_ok=True)

                try:
                    self.client.download_file(
                        self.bucket_name,
                        item.key,
                        str(temporary_path),
                        Config=transfer_config,
                        Callback=progress.update,
                    )
                    if not temporary_path.is_file():
                        raise S3ConnectionError(
                            f"После загрузки временный файл не найден: {temporary_path}"
                        )
                    actual_size = temporary_path.stat().st_size
                    if actual_size != item.size_bytes:
                        raise S3ConnectionError(
                            "Размер скачанного объекта не совпадает с размером в S3: "
                            f"{item.key}; ожидалось {item.size_bytes} байт, "
                            f"получено {actual_size} байт."
                        )
                    os.replace(temporary_path, local_path)
                except Exception as exc:
                    temporary_path.unlink(missing_ok=True)
                    state["status"] = "failed"
                    state["verification"] = None
                    state["error"] = str(exc)
                    manifest["status"] = "incomplete"
                    self._write_download_manifest(manifest_path, manifest)
                    if isinstance(exc, S3ConnectionError):
                        raise
                    raise S3ConnectionError(
                        f"Не удалось скачать объект {item.key}: {exc}"
                    ) from exc

                downloaded_count += 1
                downloaded_size += item.size_bytes
                state["status"] = "downloaded"
                state["verification"] = "size_match"
                self._write_download_manifest(manifest_path, manifest)

        manifest["status"] = "completed"
        self._write_download_manifest(manifest_path, manifest)

        return S3ProductDownloadResult(
            endpoint_url=self.endpoint_url,
            bucket=self.bucket_name,
            product_prefix=product_prefix,
            product_name=product_name,
            local_path=str(product_root),
            safe_path=str(safe_root),
            objects_total=len(objects),
            objects_downloaded=downloaded_count,
            objects_skipped=skipped_count,
            total_size_bytes=total_size,
            downloaded_size_bytes=downloaded_size,
            skipped_size_bytes=skipped_size,
            manifest_path=str(manifest_path),
        )

    def _list_product_objects(self, product_prefix: str) -> list[_S3Object]:
        """Read all object pages under a product prefix."""
        objects: list[_S3Object] = []
        continuation_token: str | None = None

        while True:
            request: dict[str, Any] = {
                "Bucket": self.bucket_name,
                "Prefix": product_prefix,
                "MaxKeys": 1000,
            }
            if continuation_token:
                request["ContinuationToken"] = continuation_token

            try:
                response = self.client.list_objects_v2(**request)
            except ClientError as exc:
                error = exc.response.get("Error", {})
                code = error.get("Code", "Unknown")
                message = error.get("Message", "No additional details")
                raise S3ConnectionError(
                    f"CDSE S3 отклонил запрос ({code}): {message}"
                ) from exc
            except BotoCoreError as exc:
                raise S3ConnectionError(
                    f"Не удалось получить список объектов CDSE S3: {exc}"
                ) from exc

            for item in response.get("Contents") or []:
                key = str(item.get("Key") or "")
                if not key.startswith(product_prefix):
                    raise S3ConnectionError(
                        f"CDSE S3 вернул объект вне запрошенного префикса: {key!r}."
                    )
                if key.endswith("/"):
                    continue
                size_bytes = int(item.get("Size") or 0)
                if size_bytes < 0:
                    raise S3ConnectionError(
                        f"CDSE S3 вернул отрицательный размер объекта: {key!r}."
                    )
                objects.append(_S3Object(key=key, size_bytes=size_bytes))

            if not response.get("IsTruncated", False):
                break

            next_token = response.get("NextContinuationToken")
            if not next_token or next_token == continuation_token:
                raise S3ConnectionError(
                    "CDSE S3 сообщил о следующей странице, но не вернул новый "
                    "ContinuationToken."
                )
            continuation_token = str(next_token)

        return sorted(objects, key=lambda item: item.key)

    def _product_prefix(self, s3_path: str) -> str:
        return self.normalize_s3_path(
            s3_path, bucket_name=self.bucket_name
        ).rstrip("/") + "/"

    @staticmethod
    def _validate_product_name(product_name: str) -> None:
        invalid_characters = '<>:"|?*'
        if (
            not product_name
            or product_name != PurePosixPath(product_name).name
            or "/" in product_name
            or "\\" in product_name
            or any(char in product_name for char in invalid_characters)
            or not product_name.endswith(".SAFE")
        ):
            raise ValueError(
                f"Недопустимое имя SAFE-продукта для локального каталога: "
                f"{product_name!r}."
            )

    @staticmethod
    def _validate_local_folder_name(folder_name: str) -> None:
        invalid_characters = '<>:"|?*'
        if (
            not folder_name
            or folder_name in {".", ".."}
            or folder_name != PurePosixPath(folder_name).name
            or "/" in folder_name
            or "\\" in folder_name
            or any(char in folder_name for char in invalid_characters)
            or folder_name.endswith((" ", "."))
        ):
            raise ValueError(f"Недопустимое имя локальной папки продукта: {folder_name!r}.")

    @staticmethod
    def _safe_relative_path(key: str, prefix: str) -> Path:
        relative = key[len(prefix):]
        parts = relative.split("/")
        invalid_characters = '<>:"|?*'
        if (
            not relative
            or relative.startswith("/")
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in parts)
            or any(
                any(char in part for char in invalid_characters)
                or part.endswith((" ", "."))
                for part in parts
            )
        ):
            raise ValueError(f"Недопустимый относительный путь объекта S3: {key!r}.")
        return Path(*parts)

    @staticmethod
    def normalize_s3_path(s3_path: str, bucket_name: str = "eodata") -> str:
        """Convert an OData S3Path (usually /eodata/...) to an S3 object key."""
        value = s3_path.strip()
        if not value:
            raise ValueError("S3Path пустой.")

        if value.startswith("s3://"):
            parsed = urlparse(value)
            if parsed.netloc != bucket_name:
                raise ValueError(
                    f"S3Path указывает на бакет {parsed.netloc!r}, ожидался {bucket_name!r}."
                )
            key = parsed.path.lstrip("/")
        else:
            key = value.lstrip("/")
            bucket_prefix = f"{bucket_name}/"
            if key.startswith(bucket_prefix):
                key = key[len(bucket_prefix):]

        if not key or any(part == ".." for part in key.split("/")):
            raise ValueError("S3Path содержит пустой или недопустимый ключ.")
        return key
