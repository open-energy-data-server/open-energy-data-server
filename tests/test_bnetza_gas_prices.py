# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import UTC, date, datetime

import pandas as pd
import pytest
from oeds.base_crawler import empty_config
from oeds.crawler import bnetza_gas_prices
from oeds.crawler.bnetza_gas_prices import (
    TEMPORAL_START,
    BnetzaGasPriceCrawler,
    parse_prices,
)

# the chart splits each price at 2022-02-24, both parts share 2022-02-23
CHART = {
    "labels": ["21.02.2022", "22.02.2022", "23.02.2022", "24.02.2022", "25.02.2022"],
    "datasets": [
        {"label": "THE Future (M+1)", "data": ["null", "null", 87.95, 135.08, "null"]},
        {"label": "THE Day Ahead", "data": ["null", "null", 89.6, 126.67, "null"]},
        {
            "label": "THE Future (M+1) Vorkrisenniveau",
            "data": [80.0, 79.0, 87.95, "null", "null"],
        },
        {
            "label": "THE Day Ahead Vorkrisenniveau",
            "data": [80.5, 79.62, 89.6, "null", "null"],
        },
    ],
}


class Response:
    def raise_for_status(self):
        pass

    def json(self):
        return CHART


@pytest.fixture
def crawler(monkeypatch):
    monkeypatch.setattr(
        bnetza_gas_prices.requests, "get", lambda url, **kwargs: Response()
    )
    config = empty_config()
    config["db_uri"] = "sqlite://"
    return BnetzaGasPriceCrawler("test", config)


def test_parse_prices_joins_the_series_before_the_crisis():
    prices = parse_prices(CHART)
    assert list(prices.index) == [
        date(2022, 2, 21),
        date(2022, 2, 22),
        date(2022, 2, 23),
        date(2022, 2, 24),
    ]
    assert prices["day_ahead_eur_per_mwh"].tolist() == [80.5, 79.62, 89.6, 126.67]
    assert prices["month_ahead_eur_per_mwh"].tolist() == [80.0, 79.0, 87.95, 135.08]


def test_parse_prices_keeps_the_first_entry_of_a_repeated_day():
    chart = {
        "labels": ["23.12.2024", "24.12.2024", "24.12.2024", "27.12.2024"],
        "datasets": [
            {"label": label, "data": data}
            for label, data in [
                ("THE Future (M+1)", [47.0, 47.29, 47.29, 48.0]),
                ("THE Day Ahead", [46.0, 46.99, 46.85, 47.5]),
                ("THE Future (M+1) Vorkrisenniveau", ["null"] * 4),
                ("THE Day Ahead Vorkrisenniveau", ["null"] * 4),
            ]
        ],
    }
    prices = parse_prices(chart)
    assert prices.index.is_unique
    assert prices["day_ahead_eur_per_mwh"].tolist() == [46.0, 46.99, 47.5]


def test_parse_prices_fails_if_a_series_is_missing():
    chart = {"labels": CHART["labels"], "datasets": CHART["datasets"][1:]}
    with pytest.raises(ValueError, match="THE Future"):
        parse_prices(chart)


def test_crawl_from_to_stores_only_the_range(crawler):
    crawler.crawl_from_to(
        datetime(2022, 2, 22, tzinfo=UTC), datetime(2022, 2, 24, tzinfo=UTC)
    )
    with crawler.engine.connect() as conn:
        stored = pd.read_sql("SELECT * FROM gas_prices ORDER BY date", conn)
    assert pd.to_datetime(stored["date"]).dt.date.tolist() == [
        date(2022, 2, 22),
        date(2022, 2, 23),
    ]
    assert stored["day_ahead_eur_per_mwh"].tolist() == [79.62, 89.6]


def test_crawl_temporal_resumes_without_duplicates(crawler):
    end = datetime(2022, 2, 26, tzinfo=UTC)
    assert crawler.get_latest_data() == TEMPORAL_START

    crawler.crawl_temporal(end=end)
    assert crawler.get_latest_data() == datetime(2022, 2, 24, tzinfo=UTC)
    assert crawler.get_first_data() == datetime(2022, 2, 21, tzinfo=UTC)

    # the latest day is fetched again and replaced
    crawler.crawl_temporal(end=end)
    with crawler.engine.connect() as conn:
        assert pd.read_sql("SELECT * FROM gas_prices", conn).shape[0] == 4
