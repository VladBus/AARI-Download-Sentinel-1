"""Build a geodesically sensible SMP search corridor in Arctic stereographic CRS."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pyproj import Transformer
from shapely.affinity import translate
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiPolygon,
    Polygon,
    mapping,
    shape,
)
from shapely.geometry.base import BaseGeometry
from shapely.ops import split, transform, unary_union
from shapely.validation import explain_validity, make_valid

GEOGRAPHIC_CRS = "EPSG:4326"
BUFFER_CRS = "EPSG:3995"


@dataclass(frozen=True)
class RegionSummary:
    """Summary of the source geometry and generated search area."""

    source_area_km2: float
    search_area_km2: float
    buffer_m: float
    polygon_count: int
    geometry: MultiPolygon


def _polygon_parts(geometry: BaseGeometry) -> list[Polygon]:
    """Return polygonal parts, dropping non-area remnants from geometry operations."""
    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        result: list[Polygon] = []
        for item in geometry.geoms:
            result.extend(_polygon_parts(item))
        return result
    return []


def _unwrap_ring(coordinates: Iterable[tuple[float, ...]]) -> list[tuple[float, float]]:
    """Unwrap a longitude/latitude ring so adjacent longitudes differ by <= 180 deg."""
    points = [(float(point[0]), float(point[1])) for point in coordinates]
    if len(points) < 4:
        raise ValueError("A polygon ring must contain at least four coordinates.")

    closed = points[0] == points[-1]
    body = points[:-1] if closed else points
    result: list[tuple[float, float]] = []
    offset = 0.0
    previous: float | None = None

    for longitude, latitude in body:
        unwrapped = longitude + offset
        if previous is not None:
            delta = unwrapped - previous
            if delta > 180.0:
                offset -= 360.0
            elif delta < -180.0:
                offset += 360.0
            unwrapped = longitude + offset
        result.append((unwrapped, latitude))
        previous = unwrapped

    if closed:
        result.append(result[0])
    return result


def _unwrap_polygon(polygon: Polygon) -> Polygon:
    """Unwrap exterior and holes, keeping holes in the exterior's longitude band."""
    exterior = _unwrap_ring(polygon.exterior.coords)
    exterior_center = sum(point[0] for point in exterior[:-1]) / (len(exterior) - 1)
    holes: list[list[tuple[float, float]]] = []

    for ring in polygon.interiors:
        hole = _unwrap_ring(ring.coords)
        hole_center = sum(point[0] for point in hole[:-1]) / (len(hole) - 1)
        shift = 360.0 * round((exterior_center - hole_center) / 360.0)
        holes.append(
            [(longitude + shift, latitude) for longitude, latitude in hole]
        )

    return Polygon(exterior, holes)


def _unwrap_geometry(geometry: BaseGeometry) -> MultiPolygon:
    """Unwrap all polygon rings in a geometry."""
    parts: list[Polygon] = []
    for polygon in _polygon_parts(geometry):
        unwrapped = _unwrap_polygon(polygon)
        if not unwrapped.is_valid:
            unwrapped = make_valid(unwrapped)
        parts.extend(_polygon_parts(unwrapped))

    if not parts:
        raise ValueError("The input GeoJSON does not contain a usable polygon.")
    return MultiPolygon(parts)


def _split_at_antimeridian(geometry: BaseGeometry) -> MultiPolygon:
    """Split unwrapped WGS84 polygons and move each piece into [-180, 180]."""
    output: list[Polygon] = []

    for polygon in _polygon_parts(geometry):
        pieces: list[BaseGeometry] = [polygon]
        min_x, _, max_x, _ = polygon.bounds
        first_k = math.floor((min_x - 180.0) / 360.0) + 1
        last_k = math.ceil((max_x - 180.0) / 360.0) - 1

        for k in range(first_k, last_k + 1):
            meridian = 180.0 + 360.0 * k
            cutter = LineString([(meridian, -90.0), (meridian, 90.0)])
            next_pieces: list[BaseGeometry] = []
            for piece in pieces:
                p_min, _, p_max, _ = piece.bounds
                if p_min < meridian < p_max:
                    next_pieces.extend(split(piece, cutter).geoms)
                else:
                    next_pieces.append(piece)
            pieces = next_pieces

        for piece in pieces:
            for part in _polygon_parts(piece):
                center_longitude = part.representative_point().x
                band_shift = 360.0 * math.floor((center_longitude + 180.0) / 360.0)
                normalized = translate(part, xoff=-band_shift)
                if not normalized.is_valid:
                    normalized = make_valid(normalized)
                output.extend(_polygon_parts(normalized))

    if not output:
        raise ValueError("The buffered region became empty after antimeridian splitting.")

    merged = unary_union(output)
    final_parts = _polygon_parts(merged)
    if not final_parts:
        raise ValueError("Unable to construct a valid output polygon.")

    for polygon in final_parts:
        if not polygon.is_valid:
            raise ValueError(f"Invalid output polygon: {explain_validity(polygon)}")
        min_x, _, max_x, _ = polygon.bounds
        if min_x < -180.000001 or max_x > 180.000001:
            raise ValueError(
                f"Longitude outside [-180, 180] after splitting: {polygon.bounds}"
            )

    return MultiPolygon(final_parts)


def load_source_geometry(path: str | Path) -> MultiPolygon:
    """Load GeoJSON Feature, FeatureCollection or geometry and unwrap its polygons."""
    source_path = Path(path)
    if not source_path.is_file():
        raise FileNotFoundError(f"GeoJSON region file not found: {source_path}")

    try:
        document: dict[str, Any] = json.loads(
            source_path.read_text(encoding="utf-8-sig")
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid GeoJSON in {source_path}: {exc}") from exc

    document_type = document.get("type")
    if document_type == "FeatureCollection":
        raw_geometries = [
            shape(feature["geometry"])
            for feature in document.get("features", [])
            if feature.get("geometry")
        ]
        if not raw_geometries:
            raise ValueError("GeoJSON FeatureCollection does not contain geometries.")
        parts = [
            part
            for geometry in raw_geometries
            for part in _unwrap_geometry(geometry).geoms
        ]
        return MultiPolygon(parts)
    if document_type == "Feature":
        if not document.get("geometry"):
            raise ValueError("GeoJSON Feature has no geometry.")
        source = shape(document["geometry"])
    else:
        source = shape(document)

    return _unwrap_geometry(source)


def build_search_area(
    geojson_path: str | Path, buffer_m: float = 150_000
) -> RegionSummary:
    """Project SMP polygon to EPSG:3995, buffer it, and return dateline-safe WGS84 polygons."""
    if not math.isfinite(buffer_m) or buffer_m <= 0:
        raise ValueError("buffer_m must be a positive finite number.")

    source_geographic = load_source_geometry(geojson_path)
    to_polar = Transformer.from_crs(GEOGRAPHIC_CRS, BUFFER_CRS, always_xy=True)
    to_geographic = Transformer.from_crs(BUFFER_CRS, GEOGRAPHIC_CRS, always_xy=True)

    source_projected = transform(to_polar.transform, source_geographic)
    if not source_projected.is_valid:
        source_projected = make_valid(source_projected)
    source_projected_parts = _polygon_parts(source_projected)
    if not source_projected_parts:
        raise ValueError("The source geometry could not be projected into EPSG:3995.")

    projected_source = unary_union(source_projected_parts)
    source_area_km2 = projected_source.area / 1_000_000.0
    buffered_projected = projected_source.buffer(buffer_m)
    buffered_geographic_unwrapped = transform(
        to_geographic.transform, buffered_projected
    )

    # Inverse projection reintroduces the antimeridian discontinuity; unwrap before splitting.
    unwrapped_geographic = _unwrap_geometry(buffered_geographic_unwrapped)
    output_geometry = _split_at_antimeridian(unwrapped_geographic)

    search_area_km2 = buffered_projected.area / 1_000_000.0
    return RegionSummary(
        source_area_km2=source_area_km2,
        search_area_km2=search_area_km2,
        buffer_m=buffer_m,
        polygon_count=len(output_geometry.geoms),
        geometry=output_geometry,
    )


def save_search_area(summary: RegionSummary, destination: str | Path) -> Path:
    """Write generated search geometry as an RFC 7946-style GeoJSON FeatureCollection."""
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    feature_collection = {
        "type": "FeatureCollection",
        "name": "SMP_search_area",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "buffer_m": summary.buffer_m,
                    "source_area_km2": round(summary.source_area_km2, 3),
                    "search_area_km2": round(summary.search_area_km2, 3),
                    "buffer_crs": BUFFER_CRS,
                },
                "geometry": mapping(summary.geometry),
            }
        ],
    }
    destination_path.write_text(
        json.dumps(feature_collection, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return destination_path
