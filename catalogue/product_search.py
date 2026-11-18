"""High-level Sentinel-1 product search and report serialization."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import Settings
from geometry.region_manager import load_source_geometry
from .odata_client import ODataClient, ODataSearchResult


def search_sentinel1(
    settings: Settings,
    search_area_path: str | Path,
    start_date: str,
    end_date: str,
    max_products: int = 500,
    page_size: int = 100,
) -> ODataSearchResult:
    """Run an EW/HH+HV OData search inside the generated SMP search area."""
    geometry = load_source_geometry(search_area_path)
    client = ODataClient(settings.cdse_odata_url)
    return client.search(
        geometry=geometry,
        start_date=start_date,
        end_date=end_date,
        product_type=settings.product_type,
        polarization_code=settings.polarization_code,
        page_size=page_size,
        max_products=max_products,
    )


def save_search_report(
    result: ODataSearchResult,
    destination: str | Path,
    start_date: str,
    end_date: str,
    search_area_path: str | Path,
    product_type: str,
    polarization_code: str,
) -> Path:
    """Save the complete search result as a local UTF-8 JSON report."""
    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)

    document: dict[str, Any] = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "Copernicus Data Space Ecosystem OData",
        "start_date_inclusive": start_date,
        "end_date_inclusive": end_date,
        "search_area": str(search_area_path),
        "product_type": product_type,
        "polarization_code": polarization_code,
        "products_found": result.count,
        "pages_requested": result.pages_requested,
        "max_products_limit_reached": result.limit_reached,
        "products": [product.to_dict() for product in result.products],
    }
    output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output
