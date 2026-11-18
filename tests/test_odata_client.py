"""Unit tests for the CDSE OData query builder and result handling."""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from shapely.geometry import MultiPolygon, box

from catalogue.odata_client import ODataClient, _odata_utc_range, build_product_filter


class ODataClientTests(unittest.TestCase):
    def test_inclusive_date_range_becomes_half_open_utc_interval(self) -> None:
        self.assertEqual(
            _odata_utc_range("2026-11-17", "2026-11-18"),
            ("2026-11-17T00:00:00.000Z", "2026-11-19T00:00:00.000Z"),
        )

    def test_invalid_date_range_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _odata_utc_range("2026-11-19", "2026-11-18")

    def test_query_contains_collection_product_polarization_dates_and_geometry(self) -> None:
        query = build_product_filter(
            box(30, 70, 40, 75),
            "2026-11-17",
            "2026-11-18",
        )
        self.assertIn("Collection/Name eq 'SENTINEL-1'", query)
        self.assertIn("Value eq 'EW_GRDM_1S'", query)
        self.assertIn("contains(Name, '_EW_GRDM_1SDH_')", query)
        self.assertIn("2026-11-17T00:00:00.000Z", query)
        self.assertIn("2026-11-19T00:00:00.000Z", query)
        self.assertIn("OData.CSC.Intersects(area=geography'SRID=4326;POLYGON", query)

    def test_pagination_and_product_deduplication(self) -> None:
        first = {
            "value": [
                {
                    "Id": "id-1",
                    "Name": "S1A_EW_GRDM_1SDH_20261117T000000.SAFE",
                    "S3Path": "/eodata/Sentinel-1/SAR/GRD/id-1",
                    "ContentDate": {
                        "Start": "2026-11-17T00:00:00.000Z",
                        "End": "2026-11-17T00:01:00.000Z",
                    },
                }
            ],
            "@odata.nextLink": "https://catalogue.test/odata/v1/Products?$skip=1",
        }
        second = {
            "value": [
                {
                    "Id": "id-1",
                    "Name": "S1A_EW_GRDM_1SDH_20261117T000000.SAFE",
                    "S3Path": "/eodata/Sentinel-1/SAR/GRD/id-1",
                    "ContentDate": {"Start": "", "End": ""},
                },
                {
                    "Id": "id-2",
                    "Name": "S1A_EW_GRDM_1SDH_20261117T010000.SAFE",
                    "S3Path": "/eodata/Sentinel-1/SAR/GRD/id-2",
                    "ContentDate": {"Start": "2026-11-17T01:00:00.000Z"},
                },
            ]
        }

        client = ODataClient("https://catalogue.test/odata/v1/Products")
        client._get_page = Mock(side_effect=[first, second])  # type: ignore[method-assign]
        result = client.search(
            geometry=MultiPolygon([box(30, 70, 40, 75)]),
            start_date="2026-11-17",
            end_date="2026-11-17",
            max_products=10,
        )
        self.assertEqual(result.count, 2)
        self.assertEqual(result.pages_requested, 2)
        self.assertFalse(result.limit_reached)


if __name__ == "__main__":
    unittest.main()
