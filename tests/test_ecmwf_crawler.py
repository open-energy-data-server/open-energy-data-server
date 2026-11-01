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
    crawler.temporal_start = ecmwf_crawler.DATASETS["era5"]["start"]
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


def test_datasets_have_separate_tables():
    from oeds.crawler import ecmwf_crawler

    tables = [
        table
        for dataset in ecmwf_crawler.DATASETS.values()
        for table in (dataset["table"], dataset["table_eu"])
    ]
    assert sorted(tables) == ["era5", "era5_eu", "era5_land", "era5_land_eu"]
    assert ecmwf_crawler.EcmwfCrawler.dataset == "era5"
    assert ecmwf_crawler.EcmwfLandCrawler.dataset == "era5-land"


def test_temporary_files_of_the_datasets_do_not_clash():
    from oeds.crawler.ecmwf_crawler import _request_stem

    request = dict(year="2019", month="06", day=["01", "02"])
    assert _request_stem(request, "era5") != _request_stem(request, "era5-land")
    assert not _request_stem(request, "era5-land").startswith(
        _request_stem(request, "era5") + "."
    )


def test_land_starts_a_day_early_to_complete_the_first_hour_of_2019():
    from oeds.crawler import ecmwf_crawler

    assert ecmwf_crawler.DATASETS["era5"]["start"] == datetime(2019, 1, 1, tzinfo=UTC)
    assert ecmwf_crawler.DATASETS["era5-land"]["start"] == datetime(
        2018, 12, 31, tzinfo=UTC
    )


def _cube(bases, steps, variables, sea=False):
    """Dataset like cfgrib: variables over (time, step) or over time only."""
    import numpy as np
    import xarray as xr

    latitude = [50.0, 50.1]
    longitude = [6.0]
    time = pd.to_datetime(bases)
    if steps is None:
        coords = {"time": time, "latitude": latitude, "longitude": longitude}
        shape = (len(time), 2, 1)
        dims = ("time", "latitude", "longitude")
        valid = ("time", time.to_numpy())
    else:
        step = pd.to_timedelta(steps, unit="h")
        coords = {
            "time": time,
            "step": step,
            "latitude": latitude,
            "longitude": longitude,
        }
        shape = (len(time), len(step), 2, 1)
        dims = ("time", "step", "latitude", "longitude")
        valid = (("time", "step"), time.to_numpy()[:, None] + step.to_numpy()[None, :])
    data = {}
    for name in variables:
        values = np.ones(shape)
        if sea:
            values[..., 1, :] = np.nan
        data[name] = (dims, values)
    return xr.Dataset(data, coords={**coords, "valid_time": valid})


def test_unrequested_valid_times_are_dropped():
    from oeds.crawler.ecmwf_crawler import datasets_to_dataframe

    instant = _cube(["2019-06-10 00:00", "2019-06-10 01:00"], None, ["t2m"])
    accumulated = _cube(["2019-06-09 23:00"], [1, 2, 3], ["ssrd"])
    # only the valid times of the request hold values
    requested = accumulated["valid_time"].isin(
        pd.to_datetime(["2019-06-10 00:00", "2019-06-10 01:00"]).to_numpy()
    )
    accumulated = accumulated.where(requested)

    df = datasets_to_dataframe([instant, accumulated])

    assert df[["t2m", "ssrd"]].notna().any(axis=1).all()
    assert sorted(df["time"].unique()) == sorted(
        pd.to_datetime(["2019-06-10 00:00", "2019-06-10 01:00"])
    )


def test_sea_cells_without_values_are_dropped():
    from oeds.crawler.ecmwf_crawler import datasets_to_dataframe

    land = _cube(["2019-06-10 00:00"], None, ["t2m"], sea=True)
    df = datasets_to_dataframe([land])
    assert df["latitude"].tolist() == [50.0]


def test_crawl_missing_requests_only_months_with_gaps(monkeypatch):
    from oeds.crawler import ecmwf_crawler

    crawler = object.__new__(ecmwf_crawler.EcmwfCrawler)
    crawler.temporal_start = datetime(2019, 1, 1, tzinfo=UTC)
    stored = {
        pd.Timestamp("2019-01-01", tz="UTC"): 31 * 24,
        pd.Timestamp("2019-02-01", tz="UTC"): 28 * 24 - 5,
    }
    crawler.stored_hours_by_month = lambda first, last: stored
    crawled = []
    crawler.crawl_range = lambda first, last: crawled.append((first, last))

    crawler.crawl_missing(
        datetime(2019, 1, 1, tzinfo=UTC), datetime(2019, 3, 5, 12, tzinfo=UTC)
    )

    # January is complete, the requests cover whole days
    assert crawled == [
        (pd.Timestamp("2019-02-01", tz="UTC"), pd.Timestamp("2019-03-01", tz="UTC")),
        (pd.Timestamp("2019-03-01", tz="UTC"), pd.Timestamp("2019-03-05", tz="UTC")),
    ]


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


def test_stored_hours_are_counted_in_one_query_on_the_nuts_table():
    from oeds.crawler import ecmwf_crawler

    crawler = object.__new__(ecmwf_crawler.EcmwfCrawler)
    crawler.table_eu = "era5_eu"
    queries = []

    class Conn:
        def execute(self, query, params):
            queries.append(str(query))
            return type("R", (), {"all": lambda self: [("2019-02-01 00:00:00", 7)]})()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Engine:
        def connect(self):
            return Conn()

    crawler.engine = Engine()
    result = crawler.stored_hours_by_month(
        datetime(2019, 1, 1, tzinfo=UTC), datetime(2019, 3, 1, tzinfo=UTC)
    )
    assert result == {pd.Timestamp("2019-02-01", tz="UTC"): 7}
    assert len(queries) == 1 and "era5_eu" in queries[0]


def _requests_of_range(monkeypatch, begin, end):
    from oeds.crawler import ecmwf_crawler

    crawler = object.__new__(ecmwf_crawler.EcmwfCrawler)
    crawler.dataset = "era5"
    crawler.ecmwf_client = None
    crawler.build_dataframe = lambda request, path: None
    requested = []
    monkeypatch.setattr(
        ecmwf_crawler,
        "save_era5_request_to_file",
        lambda request, client, dataset: requested.append(request) or "file",
    )
    crawler.crawl_range(begin, end)
    return requested


def test_partial_day_is_not_requested_after_the_end(monkeypatch):
    begin = datetime(2026, 10, 20, 5, tzinfo=UTC)
    end = datetime(2026, 10, 20, 14, 37, tzinfo=UTC)
    requested = _requests_of_range(monkeypatch, begin, end)
    assert len(requested) == 1
    assert requested[0]["time"] == [f"{h:02d}:00" for h in range(5, 15)]


def test_partial_day_requests_the_rest_of_the_day_before_later_days(monkeypatch):
    begin = datetime(2026, 10, 20, 5, tzinfo=UTC)
    end = datetime(2026, 10, 23, tzinfo=UTC)
    requested = _requests_of_range(monkeypatch, begin, end)
    assert requested[0]["time"] == [f"{h:02d}:00" for h in range(5, 24)]
    assert requested[1]["day"] == ["21", "22"]
