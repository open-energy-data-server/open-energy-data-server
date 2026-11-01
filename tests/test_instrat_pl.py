# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
from datetime import UTC, date, datetime

import pandas as pd
from oeds.base_crawler import empty_config
from oeds.crawler import instrat_pl


class Response:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def test_eu_ets_resumes_from_stored_datetime(monkeypatch):
    config = empty_config()
    config["db_uri"] = "sqlite://"
    crawler = instrat_pl.InstratPlCrawler("test", config)
    stored = pd.DataFrame(
        {"eur_per_tco2": [70.0]}, index=pd.DatetimeIndex(["2024-01-02"], name="date")
    )
    with crawler.engine.begin() as conn:
        stored.to_sql("eu_ets", conn)

    rows = [
        {"date": "2024-01-03T00:00:00Z", "price": 71.0},
        {"date": "2024-01-05T00:00:00Z", "price": 73.0},
    ]
    monkeypatch.setattr(
        instrat_pl.requests,
        "get",
        lambda url, **k: Response(json.dumps(rows if "co2" in url else [])),
    )
    crawler.crawl_from_to(
        datetime(2024, 1, 3, tzinfo=UTC), datetime(2024, 1, 6, tzinfo=UTC)
    )
    with crawler.engine.connect() as conn:
        result = pd.read_sql("SELECT * FROM eu_ets ORDER BY date", conn)
    assert len(result) == 4
    assert result["eur_per_tco2"].tolist() == [70.0, 71.0, 73.0, 73.0]


def _crawler():
    config = empty_config()
    config["db_uri"] = "sqlite://"
    return instrat_pl.InstratPlCrawler("test", config)


def _patch_api(monkeypatch, rows, calls):
    def get(url, **k):
        calls.append(k["params"])
        return Response(json.dumps(rows if "co2" in url else []))

    monkeypatch.setattr(instrat_pl.requests, "get", get)


def test_rerun_does_not_duplicate_rows(monkeypatch):
    crawler = _crawler()
    rows = [{"date": "2024-01-03T00:00:00Z", "price": 71.0}]
    _patch_api(monkeypatch, rows, [])
    begin, end = datetime(2024, 1, 3, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC)
    crawler.crawl_from_to(begin, end)
    crawler.crawl_from_to(begin, end)
    with crawler.engine.connect() as conn:
        assert len(pd.read_sql("SELECT * FROM eu_ets", conn)) == 1


def test_backfill_before_stored_data_is_requested(monkeypatch):
    crawler = _crawler()
    stored = pd.DataFrame(
        {"eur_per_tco2": [70.0]}, index=pd.Index([date(2024, 1, 10)], name="date")
    )
    with crawler.engine.begin() as conn:
        stored.to_sql("eu_ets", conn)
    rows = [{"date": "2024-01-03T00:00:00Z", "price": 71.0}]
    calls = []
    _patch_api(monkeypatch, rows, calls)
    crawler.crawl_from_to(
        datetime(2024, 1, 3, tzinfo=UTC), datetime(2024, 1, 10, tzinfo=UTC)
    )
    assert calls[0]["date_from"].startswith("03-01-2024")
    with crawler.engine.connect() as conn:
        result = pd.read_sql("SELECT * FROM eu_ets ORDER BY date", conn)
    assert len(result) == 2
    assert result["date"].iloc[0].startswith("2024-01-03")


def test_pln_eur_rates_look_back_over_non_trading_days(monkeypatch):
    crawler = _crawler()
    coal = [
        {
            "date": "2024-01-01T00:00:00Z",
            "PSCMI1 PLN per GJ": 10.0,
            "PSCMI1 PLN per t": 100.0,
        }
    ]
    monkeypatch.setattr(
        instrat_pl.requests,
        "get",
        lambda url, **k: Response(json.dumps(coal if "pscmi_1" in url else [])),
    )
    fx = pd.DataFrame(
        {("Close", "PLNEUR=X"): [0.25]}, index=pd.DatetimeIndex(["2023-12-29"])
    )
    starts = []

    def download(ticker, start, end, progress):
        starts.append(start)
        return fx

    monkeypatch.setattr(instrat_pl.yf, "download", download)
    crawler.crawl_from_to(
        datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC)
    )
    assert starts[0] < pd.Timestamp("2023-12-29")
    with crawler.engine.connect() as conn:
        result = pd.read_sql("SELECT * FROM coal_price", conn)
    assert result["steam_coal_eur_per_gj"].iloc[0] == 2.5


def test_no_request_before_the_source_begins_or_for_stored_days(monkeypatch):
    crawler = _crawler()
    stored = pd.DataFrame(
        {"price_pln_per_mwh": [100.0]},
        index=pd.Index([date(2016, 1, 5)], name="date"),
    )
    with crawler.engine.begin() as conn:
        stored.to_sql("gas_price", conn)
    requested = {}

    def get(url, **k):
        requested.setdefault(url, []).append(k["params"]["date_from"])
        return Response("[]")

    monkeypatch.setattr(instrat_pl.requests, "get", get)
    crawler.crawl_from_to(
        datetime(2015, 1, 1, tzinfo=UTC), datetime(2016, 1, 6, tzinfo=UTC)
    )
    # gas data starts 2016-01-02, although stored data starts later
    assert requested[instrat_pl.GAS_URL] == ["02-01-2016T00:00:00Z"]

    requested.clear()
    crawler.crawl_from_to(
        datetime(2016, 1, 6, tzinfo=UTC), datetime(2016, 1, 6, tzinfo=UTC)
    )
    assert requested == {}
