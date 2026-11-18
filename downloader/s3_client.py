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


class CDSES3Client:
    """Create an authenticated boto3 S3 client using local .env settings."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        settings.require_s3_credentials()
        self.bucket_name = settings.cdse_s3_bucket
        self.endpoint_url = settings.cdse_s3_endpoint_url
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

    def check_access(
        self, prefix: str = "Sentinel-1/SAR/GRD/"
    ) -> S3CheckResult:
        """Issue a lightweight ListObjectsV2 request; no EO data is downloaded."""
        try:
            response = self.client.list_objects_v2(
                Bucket=self.bucket_name,
                Prefix=prefix,
                MaxKeys=1,
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

        objects = response.get("Contents") or []
        sample_key = objects[0].get("Key") if objects else None
        return S3CheckResult(
            endpoint_url=self.endpoint_url,
            bucket=self.bucket_name,
            checked_prefix=prefix,
            visible_objects=len(objects),
            sample_key=sample_key,
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
