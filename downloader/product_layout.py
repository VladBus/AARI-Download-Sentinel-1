"""Stable, human-readable local layout for downloaded Sentinel-1 SAFE products."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path


_PRODUCT_NAME = re.compile(
    r"^(?P<platform>S1[ABCD])_(?P<mode>EW|IW)_GRDM_1S"
    r"(?P<polarization>DH|DV|SH|SV)_(?P<start>\d{8}T\d{6})_"
    r"(?P<end>\d{8}T\d{6})_\d+_(?P<datatake>[0-9A-Fa-f]{6})_"
    r"[0-9A-Fa-f]{4}\.SAFE$",
    re.IGNORECASE,
)

_POLARIZATIONS = {
    "DH": ("HH", "HV"),
    "DV": ("VV", "VH"),
    "SH": ("HH",),
    "SV": ("VV",),
}


def parse_product_name(product_name: str) -> dict[str, str]:
    """Parse a Sentinel-1 GRD SAFE name; reject unknown naming conventions."""
    match = _PRODUCT_NAME.fullmatch(product_name)
    if not match:
        raise ValueError(
            "Имя продукта не соответствует ожидаемому шаблону Sentinel-1 GRD: "
            f"{product_name!r}."
        )
    values = match.groupdict()
    start = datetime.strptime(values["start"], "%Y%m%dT%H%M%S")
    end = datetime.strptime(values["end"], "%Y%m%dT%H%M%S")
    if end <= start:
        raise ValueError("Время окончания съёмки должно быть позже времени начала.")
    return {
        "platform": values["platform"].upper(),
        "mode": values["mode"].upper(),
        "polarization_code": values["polarization"].upper(),
        "start_utc": start.replace(tzinfo=timezone.utc).isoformat(),
        "end_utc": end.replace(tzinfo=timezone.utc).isoformat(),
        "date": start.strftime("%Y-%m-%d"),
        "start_time": start.strftime("%H%M%S"),
        "end_time": end.strftime("%H%M%S"),
        "datatake_id": values["datatake"].upper(),
    }


def build_product_folder_name(product_name: str) -> str:
    """Build YYYY-MM-DD_HHMMSS-HHMMSS_EW_S1C_014EEB folder name."""
    info = parse_product_name(product_name)
    return (
        f"{info['date']}_{info['start_time']}-{info['end_time']}_"
        f"{info['mode']}_{info['platform']}_{info['datatake_id']}"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _publish_alias(source: Path, destination: Path) -> str:
    if destination.exists():
        if destination.is_file():
            try:
                if os.path.samefile(source, destination):
                    return "existing"
            except OSError:
                pass
            if destination.stat().st_size == source.stat().st_size and _sha256(destination) == _sha256(source):
                return "existing"
        raise FileExistsError(
            f"Целевой файл уже существует и не совпадает с источником: {destination}"
        )
    try:
        os.link(source, destination)
        return "hardlink"
    except OSError:
        # Hard links may be unavailable on some network filesystems. Preserve
        # portability by copying only in that case; the manifest records it.
        shutil.copy2(source, destination)
        if destination.stat().st_size != source.stat().st_size:
            destination.unlink(missing_ok=True)
            raise OSError(f"Размер копии не совпадает с исходным файлом: {destination}")
        return "copy"


def create_product_views(
    safe_path: str | Path,
    product_dir: str | Path,
    product_name: str,
) -> dict[str, object]:
    """Expose polarization TIFFs and quick-look beside an intact original SAFE."""
    safe_root = Path(safe_path).resolve()
    product_root = Path(product_dir).resolve()
    product_root.mkdir(parents=True, exist_ok=True)
    if not safe_root.is_dir():
        raise FileNotFoundError(f"Не найдена папка исходного SAFE: {safe_root}")
    info = parse_product_name(product_name)
    measurement = safe_root / "measurement"
    if not measurement.is_dir():
        raise FileNotFoundError(f"В SAFE отсутствует каталог measurement: {measurement}")

    aliases: dict[str, str] = {}
    for polarization in _POLARIZATIONS[info["polarization_code"]]:
        matches = [
            path for path in measurement.iterdir()
            if path.is_file() and path.suffix.lower() in {".tif", ".tiff"}
            and f"-{polarization.lower()}-" in path.name.lower()
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Ожидался ровно один TIFF для поляризации {polarization}, "
                f"найдено: {len(matches)}."
            )
        aliases[f"{polarization}.tiff"] = _publish_alias(
            matches[0], product_root / f"{polarization}.tiff"
        )

    preview_dir = safe_root / "preview"
    quicklook_candidates = [
        path for path in preview_dir.glob("*")
        if path.is_file() and path.name.lower() == "quick-look.png"
    ] if preview_dir.is_dir() else []
    if len(quicklook_candidates) > 1:
        raise ValueError("В SAFE найдено несколько файлов quick-look.png.")
    if quicklook_candidates:
        quicklook_source = quicklook_candidates[0]
        quicklook_target = product_root / "quick-look.png"
        if quicklook_target.exists():
            if _sha256(quicklook_target) != _sha256(quicklook_source):
                raise FileExistsError(f"Quick-look уже существует и отличается: {quicklook_target}")
            aliases["quick-look.png"] = "existing"
        else:
            shutil.copy2(quicklook_source, quicklook_target)
            aliases["quick-look.png"] = "copy"
    else:
        aliases["quick-look.png"] = "missing"

    metadata = {
        "schema": "aari_sentinel1_product_v1",
        "product_name": product_name,
        "folder_name": product_root.name,
        **info,
        "source_safe": "source.SAFE",
        "primary_files": aliases,
    }
    (product_root / "product.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return metadata
