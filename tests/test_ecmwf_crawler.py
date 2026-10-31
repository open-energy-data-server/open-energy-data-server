# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import UTC, datetime

import pandas as pd
import pytest

pytest.importorskip("cdsapi")
pytest.importorskip("geopandas")
pytest.importorskip("roiti")


def era5_frame(times, ghi, precipitation=None, latitude=50.0):
    return pd.DataFrame(
        {
            "time": pd.to_datetime(times),
            "latitude": latitude,
            "longitude": 6.0,
            "ghi": ghi,
            "precipitation": precipitation if precipitation is not None else ghi,
            "wind_zonal": 1.0,
            "wind_meridional": 1.0,
            "temp_air": 280.0,
        }
    )


def test_single_levels_keep_hourly_values():
    from oeds.crawler.ecmwf_crawler import process_era5_dataframe

    df = era5_frame(
        ["2024-06-01 11:00", "2024-06-01 12:00", "2024-06-01 13:00"], [5.0, 6.0, 4.0]
    )
    result = process_era5_dataframe(df, accumulated=False)
    assert result["ghi"].tolist() == [5.0, 6.0, 4.0]


def test_land_deaccumulates_per_cell_since_00_utc():
    from oeds.crawler.ecmwf_crawler import process_era5_dataframe

    times = [
        "2024-06-01 23:00",
        "2024-06-02 00:00",
        "2024-06-02 01:00",
        "2024-06-02 02:00",
    ]
    # accumulated since 01 UTC: 00 UTC closes the day, 01 UTC starts again
    df = pd.concat(
        [
            era5_frame(times, [90.0, 100.0, 3.0, 7.0]),
            era5_frame(times, [9.0, 10.0, 1.0, 4.0], latitude=51.0),
        ]
    )
    prior = pd.DataFrame(
        {"ghi": [80.0, 8.0], "precipitation": [80.0, 8.0]},
        index=pd.MultiIndex.from_tuples(
            [(50.0, 6.0), (51.0, 6.0)], names=["latitude", "longitude"]
        ),
    )
    result = process_era5_dataframe(df, accumulated=True, prior=prior)
    assert result["ghi"].tolist() == [10.0, 10.0, 3.0, 4.0, 1.0, 1.0, 1.0, 3.0]
    assert result["precipitation"].tolist() == result["ghi"].tolist()


def test_land_first_hour_without_prior_is_missing():
    from oeds.crawler.ecmwf_crawler import process_era5_dataframe

    df = era5_frame(["2024-06-02 05:00", "2024-06-02 06:00"], [50.0, 70.0])
    result = process_era5_dataframe(df, accumulated=True)
    assert pd.isna(result["ghi"].iloc[0])
    assert result["ghi"].iloc[1] == 20.0


def test_land_requests_have_no_product_type():
    from oeds.crawler.ecmwf_crawler import request_list_from_dates

    dates = [datetime(2024, 1, d, tzinfo=UTC) for d in (1, 2)]
    land = request_list_from_dates(dates, "era5-land")[0]
    era5 = request_list_from_dates(dates, "era5")[0]
    assert "product_type" not in land
    assert "10m_u_component_of_wind" in land["variable"]
    assert era5["product_type"] == "reanalysis"
    assert "100m_u_component_of_wind" in era5["variable"]


def test_backfill_is_not_raised_to_latest_data(monkeypatch):
    from oeds.crawler import ecmwf_crawler

    crawler = object.__new__(ecmwf_crawler.EcmwfCrawler)
    first = datetime(2023, 1, 1, tzinfo=UTC)
    latest = datetime(2023, 6, 1, tzinfo=UTC)
    monkeypatch.setattr(crawler, "get_first_data", lambda: first)
    monkeypatch.setattr(crawler, "get_latest_data", lambda: latest)
    requested = []
    monkeypatch.setattr(
        ecmwf_crawler,
        "save_era5_request_to_file",
        lambda request, client, dataset: requested.append(request) or "file",
    )
    crawler.ecmwf_client = None
    crawler.build_dataframe = lambda request, path: None
    crawler.crawl_from_to(datetime(2022, 12, 1, tzinfo=UTC), first)
    assert requested
    assert requested[0]["year"] == "2022"
    assert requested[0]["month"] == "12"


def test_land_without_stored_hours_keeps_float_columns():
    from oeds.crawler.ecmwf_crawler import ACCUMULATED_COLUMNS, process_era5_dataframe

    df = era5_frame(["2019-06-01 00:00", "2019-06-01 01:00"], [5.0, 6.0])
    # nothing stored yet: an empty prior (object columns, as read from an empty query)
    prior = pd.DataFrame(columns=ACCUMULATED_COLUMNS)
    result = process_era5_dataframe(df, accumulated=True, prior=prior)
    # object columns would be dropped by the NUTS averaging
    assert result["ghi"].dtype == "float64"
    assert result["precipitation"].dtype == "float64"


def _deaccumulate(values, hours, prior=None):
    from oeds.crawler.ecmwf_crawler import deaccumulate

    n = len(values)
    times = pd.Series(
        pd.to_datetime([f"2024-06-01 {h:02d}:00" for h in hours], utc=True)
    )
    cell = pd.Series([50.0] * n)
    return deaccumulate(
        pd.Series(values, dtype=float), times, cell, cell, prior
    ).tolist()


def test_missing_value_does_not_merge_two_hours():
    prior = pd.Series(
        [0.0], index=pd.MultiIndex.from_tuples([(50.0, 50.0)], names=["a", "b"])
    )
    result = _deaccumulate([10, float("nan"), 30, 33], [2, 3, 4, 5], prior)
    # hour 4 would span two hours, hour 5 is a regular difference
    assert result[0] == 10
    assert pd.isna(result[1]) and pd.isna(result[2])
    assert result[3] == 3


def test_missing_row_does_not_merge_two_hours():
    # the row of 03 UTC is missing altogether
    result = _deaccumulate([10, 30, 33], [2, 4, 5])
    assert pd.isna(result[0])
    assert pd.isna(result[1])
    assert result[2] == 3


def test_prior_is_only_used_for_the_first_row_of_a_cell():
    prior = pd.Series(
        [5.0], index=pd.MultiIndex.from_tuples([(50.0, 50.0)], names=["a", "b"])
    )
    result = _deaccumulate([float("nan"), 12, 15], [2, 3, 4], prior)
    assert pd.isna(result[0]) and pd.isna(result[1])
    assert result[2] == 3
