"""Centralized configuration for AARI-Download-Sentinel-1."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    """Application settings resolved from environment variables and project defaults."""

    project_root: Path
    cdse_access_key: str
    cdse_secret_key: str
    cdse_s3_endpoint_url: str
    cdse_s3_bucket: str
    cdse_s3_region_name: str
    cdse_odata_url: str
    cdse_credentials_expires_at: str
    region_geojson: Path
    buffer_m: float
    product_type: str
    polarization_code: str
    download_dir: Path
    log_dir: Path

    def require_s3_credentials(self) -> None:
        """Raise a safe configuration error when S3 credentials are missing."""
        missing = []
        if not self.cdse_access_key:
            missing.append("CDSE_ACCESS_KEY")
        if not self.cdse_secret_key:
            missing.append("CDSE_SECRET_KEY")
        if missing:
            names = ", ".join(missing)
            raise ValueError(
                f"Не заданы параметры {names}. Заполните их в локальном файле .env. "
                "Не публикуйте этот файл в GitHub."
            )


def _resolve_path(project_root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (project_root / path).resolve()


def load_settings(project_root: str | Path | None = None) -> Settings:
    """Load .env and return normalized settings.

    Credentials are intentionally optional here: geometry preparation and tests do not
    require access to CDSE. Call require_s3_credentials before creating an S3 client.
    """
    root = Path(project_root or Path(__file__).resolve().parent).resolve()
    load_dotenv(root / ".env", override=False)

    def env(name: str, default: str = "") -> str:
        return os.getenv(name, default).strip()

    try:
        buffer_m = float(env("S1_BUFFER_METERS", "150000"))
    except ValueError as exc:
        raise ValueError("S1_BUFFER_METERS должен быть числом в метрах.") from exc
    if buffer_m <= 0:
        raise ValueError("S1_BUFFER_METERS должен быть больше нуля.")

    return Settings(
        project_root=root,
        cdse_access_key=env("CDSE_ACCESS_KEY"),
        cdse_secret_key=env("CDSE_SECRET_KEY"),
        cdse_s3_endpoint_url=env(
            "CDSE_S3_ENDPOINT_URL", "https://eodata.dataspace.copernicus.eu"
        ).rstrip("/"),
        cdse_s3_bucket=env("CDSE_S3_BUCKET", "eodata"),
        cdse_s3_region_name=env("CDSE_S3_REGION_NAME", "default"),
        cdse_odata_url=env(
            "CDSE_ODATA_URL", "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
        ).rstrip("/"),
        cdse_credentials_expires_at=env("CDSE_CREDENTIALS_EXPIRES_AT"),
        region_geojson=_resolve_path(
            root, env("S1_REGION_GEOJSON", "data/geometry/SMP.geojson")
        ),
        buffer_m=buffer_m,
        product_type=env("S1_PRODUCT_TYPE", "EW_GRDM_1S"),
        polarization_code=env("S1_POLARIZATION_CODE", "DH"),
        download_dir=_resolve_path(root, env("S1_DOWNLOAD_DIR", "data/downloads")),
        log_dir=_resolve_path(root, env("S1_LOG_DIR", "logs")),
    )
