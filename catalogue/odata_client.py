"""Client for the Copernicus Data Space Ecosystem OData catalogue."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from shapely import to_wkt
from shapely.geometry import MultiPolygon, Polygon
from urllib3.util.retry import Retry


class ODataError(RuntimeError):
    """Raised when the CDSE OData catalogue cannot complete a request."""


@dataclass(frozen=True)
class SentinelProduct:
    """Small, serializable subset of a Sentinel-1 OData product record."""

    product_id: str
    name: str
    s3_path: str
    content_date_start: str
    content_date_end: str
    geo_footprint: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ODataSearchResult:
    """Products and request statistics returned by a catalogue search."""

    products: list[SentinelProduct]
    pages_requested: int
    limit_reached: bool

    @property
    def count(self) -> int:
        return len(self.products)


def _odata_utc_range(start_date: str, end_date: str) -> tuple[str, str]:
    """Convert inclusive YYYY-MM-DD dates to a half-open UTC datetime interval."""
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError("Даты должны иметь формат YYYY-MM-DD.") from exc

    if start > end:
        raise ValueError("Начальная дата не может быть позже конечной даты.")

    start_dt = datetime.combine(start, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc)

    def format_odata(value: datetime) -> str:
        return value.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    return format_odata(start_dt), format_odata(end_dt)


def build_product_filter(
    polygon: Polygon,
    start_date: str,
    end_date: str,
    product_type: str = "EW_GRDM_1S",
    polarization_code: str = "DH",
) -> str:
    """Build the documented OData filter for one WGS84 search polygon."""
    if polygon.is_empty or not polygon.is_valid:
        raise ValueError("Для пространственного поиска требуется валидный Polygon.")
    if not product_type.strip():
        raise ValueError("product_type не может быть пустым.")
    if not polarization_code.strip():
        raise ValueError("polarization_code не может быть пустым.")

    start_utc, end_utc = _odata_utc_range(start_date, end_date)
    name_token = f"_{product_type.replace('_1S', '_1S' + polarization_code)}_"
    polygon_wkt = to_wkt(polygon, rounding_precision=5, trim=True)

    clauses = [
        "Collection/Name eq 'SENTINEL-1'",
        (
            "Attributes/OData.CSC.StringAttribute/any("
            "att:att/Name eq 'productType' and "
            f"att/OData.CSC.StringAttribute/Value eq '{product_type}')"
        ),
        f"contains(Name, '{name_token}')",
        f"ContentDate/Start ge {start_utc}",
        f"ContentDate/Start lt {end_utc}",
        f"OData.CSC.Intersects(area=geography'SRID=4326;{polygon_wkt}')",
    ]
    return " and ".join(clauses)


class ODataClient:
    """Read-only OData client with retry handling and pagination."""

    def __init__(
        self,
        endpoint_url: str,
        timeout_seconds: int = 60,
        session: requests.Session | None = None,
    ) -> None:
        self.endpoint_url = endpoint_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "AARI-Download-Sentinel-1/0.1",
            }
        )
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.7,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    @staticmethod
    def _parse_product(raw: dict[str, Any]) -> SentinelProduct | None:
        product_id = str(raw.get("Id") or "").strip()
        name = str(raw.get("Name") or "").strip()
        s3_path = str(raw.get("S3Path") or "").strip()
        if not product_id or not name:
            return None

        content_date = raw.get("ContentDate") or {}
        footprint = raw.get("GeoFootprint")
        return SentinelProduct(
            product_id=product_id,
            name=name,
            s3_path=s3_path,
            content_date_start=str(content_date.get("Start") or ""),
            content_date_end=str(content_date.get("End") or ""),
            geo_footprint=footprint if isinstance(footprint, dict) else None,
        )

    def _get_page(
        self, url: str, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        try:
            response = self.session.get(
                url, params=params, timeout=self.timeout_seconds
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise ODataError(
                f"Ошибка HTTP при обращении к CDSE OData: {exc}"
            ) from exc
        except ValueError as exc:
            raise ODataError("CDSE OData вернул некорректный JSON.") from exc

        if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
            raise ODataError("Ответ CDSE OData не содержит ожидаемый массив value.")
        return payload

    def search(
        self,
        geometry: MultiPolygon,
        start_date: str,
        end_date: str,
        product_type: str = "EW_GRDM_1S",
        polarization_code: str = "DH",
        page_size: int = 100,
        max_products: int = 500,
    ) -> ODataSearchResult:
        """Search all polygons in a date interval, de-duplicating product IDs.

        Dates are inclusive. Each polygon is queried separately because this keeps
        antimeridian-split geometries compatible with the OData geography WKT filter.
        """
        if geometry.is_empty or not geometry.is_valid:
            raise ValueError("Поисковая геометрия должна быть непустой и валидной.")
        if page_size < 1 or page_size > 1000:
            raise ValueError("page_size должен находиться в диапазоне 1–1000.")
        if max_products < 1:
            raise ValueError("max_products должен быть не меньше 1.")

        _odata_utc_range(start_date, end_date)

        products_by_id: dict[str, SentinelProduct] = {}
        pages_requested = 0
        limit_reached = False

        for polygon in geometry.geoms:
            if len(products_by_id) >= max_products:
                limit_reached = True
                break

            filter_text = build_product_filter(
                polygon,
                start_date,
                end_date,
                product_type=product_type,
                polarization_code=polarization_code,
            )
            params = {
                "$filter": filter_text,
                "$select": "Id,Name,S3Path,ContentDate,GeoFootprint",
                "$orderby": "ContentDate/Start asc",
                "$top": str(page_size),
            }
            page_url: str | None = self.endpoint_url
            page_params: dict[str, str] | None = params

            while page_url:
                payload = self._get_page(page_url, page_params)
                pages_requested += 1

                for raw_product in payload["value"]:
                    if not isinstance(raw_product, dict):
                        continue
                    product = self._parse_product(raw_product)
                    if product is None:
                        continue
                    products_by_id.setdefault(product.product_id, product)

                    if len(products_by_id) >= max_products:
                        limit_reached = True
                        break

                next_link = payload.get("@odata.nextLink") or payload.get(
                    "@OData.nextLink"
                )
                if limit_reached:
                    break

                if next_link:
                    parsed = urlparse(str(next_link))
                    endpoint_host = urlparse(self.endpoint_url).netloc
                    if parsed.scheme != "https" or parsed.netloc != endpoint_host:
                        raise ODataError(
                            "CDSE OData вернул неожиданный адрес пагинации; запрос остановлен."
                        )
                    page_url = str(next_link)
                    page_params = None
                else:
                    page_url = None

            if limit_reached:
                break

        products = sorted(
            products_by_id.values(),
            key=lambda item: (item.content_date_start, item.name),
        )
        return ODataSearchResult(
            products=products,
            pages_requested=pages_requested,
            limit_reached=limit_reached,
        )
