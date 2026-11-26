"""Command-line entry point for AARI-Download-Sentinel-1."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

import requests

from catalogue.product_search import save_search_report, search_sentinel1
from config import load_settings
from downloader.s3_client import CDSES3Client, S3ConnectionError
from downloader.product_layout import build_product_folder_name, create_product_views
from storage import check_storage_directory
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
        help="Проверить запрос к CDSE S3 по каталожному префиксу.",
    )
    actions.add_argument(
        "--check-s3-product",
        action="store_true",
        help="Проверить первые объекты внутри SAFE-префикса продукта.",
    )
    actions.add_argument(
        "--plan-s3-download",
        action="store_true",
        help="Посчитать число объектов и общий объём выбранного SAFE-продукта без скачивания.",
    )
    actions.add_argument(
        "--download-product",
        action="store_true",
        help="Скачать один полный SAFE-продукт, выбранный из JSON-отчёта.",
    )
    actions.add_argument(
        "--search-s1",
        action="store_true",
        help="Найти продукты Sentinel-1 EW GRDM HH+HV через CDSE OData.",
    )
    actions.add_argument(
        "--check-storage",
        action="store_true",
        help="Проверить создание каталога и доступ на запись, чтение и удаление.",
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
    parser.add_argument(
        "--search-report",
        type=Path,
        help="JSON-отчёт OData для проверки или последующей загрузки выбранного продукта.",
    )
    parser.add_argument(
        "--product-index",
        type=int,
        default=0,
        help="Нулевой индекс продукта в JSON-отчёте (по умолчанию 0).",
    )
    parser.add_argument(
        "--s3-max-keys",
        type=int,
        default=5,
        help="Максимум объектов для быстрой проверки SAFE-префикса (по умолчанию 5).",
    )
    parser.add_argument(
        "--download-dir",
        type=Path,
        help="Корневой каталог для папок продуктов (по умолчанию S1_DOWNLOAD_DIR из .env).",
    )
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


def _load_product_from_report(report_path: Path, product_index: int) -> dict[str, Any]:
    if not report_path.is_file():
        raise FileNotFoundError(f"Не найден JSON-отчёт поиска: {report_path}")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Некорректный JSON-отчёт: {exc}") from exc

    products = report.get("products")
    if not isinstance(products, list) or not products:
        raise ValueError("В JSON-отчёте нет массива products или он пуст.")
    if product_index < 0 or product_index >= len(products):
        raise ValueError(
            f"product-index={product_index} вне диапазона 0–{len(products) - 1}."
        )

    product = products[product_index]
    if not isinstance(product, dict) or not product.get("s3_path"):
        raise ValueError(f"У продукта с индексом {product_index} отсутствует S3Path.")
    return product


def _run_search(args: argparse.Namespace, settings: Any) -> int:
    if not args.start_date or not args.end_date:
        raise ValueError("--search-s1 требует --start-date и --end-date.")
    start_date = date.fromisoformat(args.start_date).isoformat()
    end_date = date.fromisoformat(args.end_date).isoformat()
    if start_date > end_date:
        raise ValueError("--start-date не может быть позже --end-date.")

    buffer_m = settings.buffer_m
    search_area_path = _region_output_path(settings.project_root, buffer_m)
    region_report = _prepare_region(
        settings.region_geojson, buffer_m, search_area_path
    )
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


def _run_product_inventory(args: argparse.Namespace, settings: Any) -> int:
    if args.search_report is None:
        raise ValueError("--plan-s3-download требует --search-report.")
    product = _load_product_from_report(args.search_report, args.product_index)
    product_name = product.get("name")
    if not product_name:
        raise ValueError("В выбранной записи JSON отсутствует имя продукта.")

    result = CDSES3Client(settings).inspect_product(product["s3_path"])
    print(
        json.dumps(
            {
                "status": "PRODUCT_INVENTORY_READY",
                "product_name": product_name,
                "product_id": product.get("product_id"),
                "endpoint": result.endpoint_url,
                "bucket": result.bucket,
                "product_prefix": result.product_prefix,
                "objects_total": result.object_count,
                "total_size_bytes": result.total_size_bytes,
                "total_size_mib": round(result.total_size_bytes / (1024**2), 2),
                "total_size_gib": round(result.total_size_bytes / (1024**3), 3),
                "sample_objects": result.sample_objects,
                "downloaded_data": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _run_product_download(args: argparse.Namespace, settings: Any) -> int:
    if args.search_report is None:
        raise ValueError("--download-product требует --search-report.")
    product = _load_product_from_report(args.search_report, args.product_index)
    product_name = product.get("name")
    if not product_name:
        raise ValueError("В выбранной записи JSON отсутствует имя продукта.")

    destination_dir = args.download_dir or settings.download_dir
    storage_check = check_storage_directory(destination_dir)
    folder_name = build_product_folder_name(product_name)
    result = CDSES3Client(settings).download_product(
        s3_path=product["s3_path"],
        product_name=product_name,
        destination_dir=destination_dir,
        folder_name=folder_name,
    )
    product_views = create_product_views(
        result.safe_path, result.local_path, product_name
    )
    print(
        json.dumps(
            {
                "status": "PRODUCT_DOWNLOADED",
                "product_name": result.product_name,
                "product_id": product.get("product_id"),
                "endpoint": result.endpoint_url,
                "bucket": result.bucket,
                "product_prefix": result.product_prefix,
                "storage_root": storage_check.directory,
                "storage_write_read_delete_verified": storage_check.write_read_delete_verified,
                "local_path": result.local_path,
                "safe_path": result.safe_path,
                "download_manifest": result.manifest_path,
                "primary_files": product_views["primary_files"],
                "objects_total": result.objects_total,
                "objects_downloaded": result.objects_downloaded,
                "objects_skipped": result.objects_skipped,
                "total_size_bytes": result.total_size_bytes,
                "downloaded_size_bytes": result.downloaded_size_bytes,
                "skipped_size_bytes": result.skipped_size_bytes,
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
        if args.check_storage:
            destination_dir = args.download_dir or settings.download_dir
            result = check_storage_directory(destination_dir)
            print(
                json.dumps(
                    {
                        "status": "STORAGE_READY",
                        "storage_root": result.directory,
                        "directory_created": result.created,
                        "write_read_delete_verified": result.write_read_delete_verified,
                        "probe_file_removed": result.probe_file_removed,
                        "downloaded_data": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if args.check_s3:
            result = CDSES3Client(settings).check_access()
            has_objects = result.visible_objects > 0
            print(
                json.dumps(
                    {
                        "request_success": True,
                        "status": "OK" if has_objects else "EMPTY_PREFIX",
                        "credentials_and_list_request_accepted": True,
                        "prefix_contains_objects": has_objects,
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

        if args.check_s3_product:
            if args.search_report is None:
                raise ValueError("--check-s3-product требует --search-report.")
            product = _load_product_from_report(
                args.search_report, args.product_index
            )
            result = CDSES3Client(settings).check_product_prefix(
                product["s3_path"], max_keys=args.s3_max_keys
            )
            has_objects = result.objects_returned > 0
            print(
                json.dumps(
                    {
                        "request_success": True,
                        "status": "PRODUCT_FOUND" if has_objects else "PRODUCT_PREFIX_EMPTY",
                        "product_name": product.get("name"),
                        "product_id": product.get("product_id"),
                        "endpoint": result.endpoint_url,
                        "bucket": result.bucket,
                        "product_prefix": result.product_prefix,
                        "objects_returned": result.objects_returned,
                        "is_truncated": result.is_truncated,
                        "sample_objects": result.sample_objects,
                        "downloaded_data": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if has_objects else 4

        if args.plan_s3_download:
            return _run_product_inventory(args, settings)

        if args.download_product:
            return _run_product_download(args, settings)

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
