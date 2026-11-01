# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import UTC, datetime

import pandas as pd
import pytest

pytest.importorskip("cdsapi")
pytest.importorskip("geopandas")
pytest.importorskip("roiti")


def test_deaccumulate_radiation_is_grouped_by_cell():
    from oeds.crawler.ecmwf_crawler import deaccumulate_radiation

    df = pd.DataFrame(
        {
            "time": pd.to_datetime(["2024-01-01 01:00", "2024-01-01 02:00"] * 2),
            "latitude": [50.0, 50.0, 51.0, 51.0],
            "longitude": [6.0] * 4,
            "ghi": [100.0, 150.0, 10.0, 30.0],
        }
    )
    result = deaccumulate_radiation(
        df["ghi"], df["time"], df["latitude"], df["longitude"]
    )
    assert result.tolist() == [0.0, 50.0, 0.0, 20.0]


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
        lambda request, client: requested.append(request) or "file",
    )
    crawler.ecmwf_client = None
    crawler.build_dataframe = lambda request, path: None
    crawler.crawl_from_to(datetime(2022, 12, 1, tzinfo=UTC), first)
    assert requested
    assert requested[0]["year"] == "2022"
    assert requested[0]["month"] == "12"
