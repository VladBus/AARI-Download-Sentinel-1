"""S3-compatible client for Copernicus Data Space Ecosystem."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

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
                retries={"max_attempts": 5, "mode": "standard"},
            ),
        )

    def _list_objects(
        self, prefix: str, max_keys: int
    ) -> dict[str, Any]:
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

        product_prefix = self.normalize_s3_path(
            s3_path, bucket_name=self.bucket_name
        ).rstrip("/") + "/"
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
