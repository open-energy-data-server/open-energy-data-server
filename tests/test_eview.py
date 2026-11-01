# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import datetime

import pandas as pd
import requests
from oeds import base_crawler
from oeds.crawler import eview
from oeds.crawler.eview import EViewCrawler, local_to_utc


def test_local_to_utc_summer_and_winter():
    index = pd.DatetimeIndex(["2023-07-01 13:00", "2023-12-01 13:00"])
    result = local_to_utc(index)
    assert list(result) == [
        pd.Timestamp("2023-07-01 11:00", tz="UTC"),
        pd.Timestamp("2023-12-01 12:00", tz="UTC"),
    ]
    assert str(result.tz) == "UTC"


def test_local_to_utc_spring_gap_becomes_nat():
    index = pd.date_range("2023-03-26 01:45", "2023-03-26 03:00", freq="15min")
    result = local_to_utc(index)
    assert result.isna().sum() == 4  # 02:00 - 02:45 do not exist
    assert result[0] == pd.Timestamp("2023-03-26 00:45", tz="UTC")
    assert result[-1] == pd.Timestamp("2023-03-26 01:00", tz="UTC")


def test_local_to_utc_autumn_repeated_hour_by_order():
    index = pd.DatetimeIndex(
        ["2023-10-29 01:45", "2023-10-29 02:00", "2023-10-29 02:00", "2023-10-29 03:00"]
    )
    result = local_to_utc(index)
    assert list(result) == [
        pd.Timestamp("2023-10-28 23:45", tz="UTC"),
        pd.Timestamp("2023-10-29 00:00", tz="UTC"),
        pd.Timestamp("2023-10-29 01:00", tz="UTC"),
        pd.Timestamp("2023-10-29 02:00", tz="UTC"),
    ]


def test_local_to_utc_autumn_unique_is_standard_time():
    result = local_to_utc(pd.DatetimeIndex(["2023-10-29 02:15"]))
    assert result[0] == pd.Timestamp("2023-10-29 01:15", tz="UTC")


CSV = (
    '"Anlage: X","1 kWp"\n"",""\n"Mittlere Leistung","" \n"",""\n'
    '"Datum und Uhrzeit","X"\n"10.06.2024 05:45","0,500"\n'
).encode("iso-8859-1")


def make_crawler():
    config = base_crawler.empty_config()
    config["db_uri"] = "sqlite://"
    return EViewCrawler("eview", config)


class Response:
    def __init__(self, status_code, content=CSV):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def test_fetch_retries_then_succeeds(monkeypatch):
    responses = [Response(503), requests.Timeout(), Response(200)]

    def get(url, timeout):
        assert timeout
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(eview.requests, "get", get)
    monkeypatch.setattr(eview.time, "sleep", lambda s: None)
    assert make_crawler().fetch("http://x") == CSV
    assert not responses


def test_crawl_unit_stops_at_failed_day(monkeypatch):
    crawler = make_crawler()
    days = []

    def get(url, timeout):
        days.append(url.split(";t")[1][:10])
        return Response(500) if len(days) >= 2 else Response(200)

    monkeypatch.setattr(eview.requests, "get", get)
    monkeypatch.setattr(eview.time, "sleep", lambda s: None)
    crawler.crawl_unit("X", datetime(2024, 6, 10), datetime(2024, 6, 14))
    # day 1 fetched, day 2 failed after all attempts, days 3+ are not tried
    assert days == ["10.06.2024"] + ["11.06.2024"] * eview.FETCH_ATTEMPTS


def test_header_only_response_is_skipped(monkeypatch):
    header_only = CSV.split(b'"10.06')[0]
    monkeypatch.setattr(
        eview.requests, "get", lambda url, timeout: Response(200, header_only)
    )
    crawler = make_crawler()
    crawler.crawl_unit_date("X", datetime(2024, 6, 10))
