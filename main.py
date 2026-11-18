"""Command-line entry point for AARI-Download-Sentinel-1."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

import requests

from catalogue.product_search import save_search_report, search_sentinel1
from config import load_settings
from downloader.s3_client import CDSES3Client, S3ConnectionError
from geometry.region_manager import build_search_area, save_search_area


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Поиск и последующая загрузка Sentinel-1 для рабочей территории СМП."
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--prepare-region",
        action="store_true",
        help="Построить поисковую область вокруг исходного SMP.geojson.",
    )
    actions.add_argument(
        "--check-s3",
        action="store_true",
        help="Проверить авторизацию и доступность CDSE S3 без скачивания данных.",
    )
    actions.add_argument(
        "--search-s1",
        action="store_true",
        help="Найти продукты Sentinel-1 EW GRDM HH+HV через CDSE OData.",
    )

    parser.add_argument("--geojson", type=Path, help="Путь к исходному GeoJSON.")
    parser.add_argument("--buffer-km", type=float, help="Ширина буфера в километрах.")
    parser.add_argument("--output", type=Path, help="Путь для результирующего GeoJSON.")
    parser.add_argument("--start-date", help="Начальная дата поиска, YYYY-MM-DD (включительно).")
    parser.add_argument("--end-date", help="Конечная дата поиска, YYYY-MM-DD (включительно).")
    parser.add_argument(
        "--max-products",
        type=int,
        default=500,
        help="Максимум продуктов в одном поиске (по умолчанию 500).",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="Размер страницы OData (по умолчанию 100).",
    )
    parser.add_argument("--search-output", type=Path, help="Путь для JSON-отчёта поиска.")
    return parser


def _region_output_path(project_root: Path, buffer_m: float) -> Path:
    return (
        project_root
        / "data"
        / "geometry"
        / f"SMP_search_area_{round(buffer_m / 1000):03d}km.geojson"
    )


def _prepare_region(
    source_path: Path, buffer_m: float, output_path: Path
) -> dict[str, object]:
    summary = build_search_area(source_path, buffer_m)
    saved_path = save_search_area(summary, output_path)
    return {
        "source_geojson": str(source_path),
        "buffer_crs": "EPSG:3995",
        "buffer_km": buffer_m / 1000.0,
        "source_area_km2": round(summary.source_area_km2, 3),
        "search_area_km2": round(summary.search_area_km2, 3),
        "output_polygons": summary.polygon_count,
        "output_geojson": str(saved_path),
    }


def _run_search(args: argparse.Namespace, settings: object) -> int:
    if not args.start_date or not args.end_date:
        raise ValueError("--search-s1 требует --start-date и --end-date.")
    start_date = date.fromisoformat(args.start_date).isoformat()
    end_date = date.fromisoformat(args.end_date).isoformat()
    if start_date > end_date:
        raise ValueError("--start-date не может быть позже --end-date.")

    buffer_m = settings.buffer_m
    search_area_path = _region_output_path(settings.project_root, buffer_m)
    source_path = settings.region_geojson

    # Regenerate every time to avoid using a stale polygon after local edits.
    region_report = _prepare_region(source_path, buffer_m, search_area_path)
    logging.info(
        "Поисковая область подготовлена: %s; полигонов: %s",
        search_area_path,
        region_report["output_polygons"],
    )

    result = search_sentinel1(
        settings=settings,
        search_area_path=search_area_path,
        start_date=start_date,
        end_date=end_date,
        max_products=args.max_products,
        page_size=args.page_size,
    )
    output_path = args.search_output or (
        settings.project_root
        / "data"
        / "search_results"
        / (
            f"sentinel1_{settings.product_type}_{settings.polarization_code}_"
            f"{start_date}_{end_date}.json"
        )
    )
    saved_report = save_search_report(
        result=result,
        destination=output_path,
        start_date=start_date,
        end_date=end_date,
        search_area_path=search_area_path,
        product_type=settings.product_type,
        polarization_code=settings.polarization_code,
    )

    preview = [
        {
            "name": product.name,
            "content_date_start": product.content_date_start,
            "s3_path": product.s3_path,
        }
        for product in result.products[:20]
    ]
    print(
        json.dumps(
            {
                "products_found": result.count,
                "pages_requested": result.pages_requested,
                "max_products_limit_reached": result.limit_reached,
                "product_type": settings.product_type,
                "polarization_code": settings.polarization_code,
                "date_range_inclusive_utc": [start_date, end_date],
                "search_area": str(search_area_path),
                "search_report": str(saved_report),
                "first_products": preview,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = build_parser()
    args = parser.parse_args()
    settings = load_settings()

    try:
        if args.check_s3:
            client = CDSES3Client(settings)
            result = client.check_access()
            print(
                json.dumps(
                    {
                        "status": "OK",
                        "endpoint": result.endpoint_url,
                        "bucket": result.bucket,
                        "checked_prefix": result.checked_prefix,
                        "visible_objects": result.visible_objects,
                        "sample_key": result.sample_key,
                        "downloaded_data": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if args.search_s1:
            return _run_search(args, settings)

        if args.prepare_region:
            source_path = args.geojson or settings.region_geojson
            buffer_m = (
                args.buffer_km * 1000.0
                if args.buffer_km is not None
                else settings.buffer_m
            )
            output_path = args.output or _region_output_path(
                settings.project_root, buffer_m
            )
            report = _prepare_region(source_path, buffer_m, output_path)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0

        parser.print_help()
        return 0

    except S3ConnectionError as exc:
        logging.error("%s", exc)
        return 2
    except requests.RequestException as exc:
        logging.error("Сетевая ошибка: %s", exc)
        return 3
    except (OSError, ValueError, RuntimeError) as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
