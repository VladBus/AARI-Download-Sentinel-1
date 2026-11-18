"""Command-line entry point for AARI-Download-Sentinel-1."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from config import load_settings
from geometry.region_manager import build_search_area, save_search_area


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Подготовка территории поиска и автоматизация загрузки Sentinel-1 для СМП."
    )
    parser.add_argument(
        "--prepare-region",
        action="store_true",
        help="Построить поисковую область вокруг исходного SMP.geojson.",
    )
    parser.add_argument(
        "--geojson",
        type=Path,
        help="Путь к исходному GeoJSON (по умолчанию берётся из .env).",
    )
    parser.add_argument(
        "--buffer-km",
        type=float,
        help="Ширина буфера в километрах (по умолчанию берётся из .env).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Путь для результирующего GeoJSON.",
    )
    return parser


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = build_parser().parse_args()
    settings = load_settings()

    if not args.prepare_region:
        logging.info("Пока реализована подготовка геометрии. Используйте --prepare-region.")
        return 0

    source_path = args.geojson or settings.region_geojson
    buffer_m = (
        args.buffer_km * 1000.0
        if args.buffer_km is not None
        else settings.buffer_m
    )
    output_path = args.output or (
        settings.project_root
        / "data"
        / "geometry"
        / f"SMP_search_area_{round(buffer_m / 1000):03d}km.geojson"
    )

    try:
        summary = build_search_area(source_path, buffer_m)
        saved_path = save_search_area(summary, output_path)
    except (OSError, ValueError) as exc:
        logging.error("Не удалось подготовить геометрию: %s", exc)
        return 1

    report = {
        "source_geojson": str(source_path),
        "buffer_crs": "EPSG:3995",
        "buffer_km": buffer_m / 1000.0,
        "source_area_km2": round(summary.source_area_km2, 3),
        "search_area_km2": round(summary.search_area_km2, 3),
        "output_polygons": summary.polygon_count,
        "output_geojson": str(saved_path),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
