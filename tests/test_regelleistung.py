# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import pandas as pd
from oeds.crawler.regelleistung import berlin_to_utc, utc_to_berlin_date


def test_berlin_to_utc_is_aware_and_handles_dst():
    series = pd.Series(pd.to_datetime(["2024-01-01 00:00", "2024-07-01 00:00"]))
    result = berlin_to_utc(series)
    assert str(result.dt.tz) == "UTC"
    assert list(result) == [
        pd.Timestamp("2023-12-31 23:00", tz="UTC"),
        pd.Timestamp("2024-06-30 22:00", tz="UTC"),
    ]


def test_utc_to_berlin_date_accepts_aware_and_naive():
    aware = pd.Timestamp("2024-06-30 22:00", tz="UTC")
    assert utc_to_berlin_date(aware).isoformat() == "2024-07-01"
    assert utc_to_berlin_date(aware.tz_localize(None)).isoformat() == "2024-07-01"


def test_latest_data_of_missing_table_is_the_day_before_the_start():
    from datetime import timedelta

    from oeds.base_crawler import empty_config
    from oeds.crawler.regelleistung import TEMPORAL_START, RegelleistungCrawler

    config = empty_config()
    config["db_uri"] = "sqlite://"
    crawler = RegelleistungCrawler("test", config)
    table = "capacity_missing"
    assert crawler.get_latest_data(table) == TEMPORAL_START - timedelta(days=1)
