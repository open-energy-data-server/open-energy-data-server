# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Daily wholesale gas prices of the German market area THE, as published by the
Bundesnetzagentur.

FOR INTERNAL USE ONLY: the prices are provided to the Bundesnetzagentur by the
European Energy Exchange (EEX). They are not published under an open license and
must not be redistributed, e.g. through a public PostgREST or Grafana.

The Bundesnetzagentur states that the data is not available in machine-readable
form. This crawler reads the undocumented JSON endpoint behind the chart on
https://www.bundesnetzagentur.de/DE/Gasversorgung/aktuelle_gasversorgung/_svg/Gaspreise/Gaspreise.html
which may change without notice.

The endpoint always returns the full history since 2022-02-01, one value per
trading day in EUR/MWh:

- day_ahead_eur_per_mwh: day-ahead price for the next gas day
- month_ahead_eur_per_mwh: price of the front month future (M+1)

The chart shows the prices before 2022-02-24 as separate series
("Vorkrisenniveau"). They are joined with the later prices into one series.
"""

import logging
from datetime import UTC, datetime

import pandas as pd
import requests
from sqlalchemy import inspect, text

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    ContinuousCrawler,
    as_utc,
    load_config,
)

log = logging.getLogger("bnetza_gas_prices")
log.setLevel(logging.INFO)

metadata_info = {
    "schema_name": "bnetza_gas_prices",
    "data_source": "https://www.bundesnetzagentur.de/DE/Gasversorgung/aktuelle_gasversorgung/_svg/Gaspreise/Gaspreise.html",
    "license": "internal use only, data by EEX via Bundesnetzagentur",
    "description": "Daily wholesale gas prices of the German market area THE in EUR/MWh: day-ahead and front month future (M+1).",
    "contact": "",
    "temporal_start": "2022-02-01",
}

DATA_URL = (
    "https://www.bundesnetzagentur.de/SiteGlobals/Functions/SVG/_functions/json.html"
)
CHART_ID = 870302
REQUEST_TIMEOUT = 60
TABLE = "gas_prices"
TEMPORAL_START = datetime(2022, 2, 1, tzinfo=UTC)
# column: (chart series, chart series of the same price before 2022-02-24)
SERIES = {
    "day_ahead_eur_per_mwh": ("THE Day Ahead", "THE Day Ahead Vorkrisenniveau"),
    "month_ahead_eur_per_mwh": (
        "THE Future (M+1)",
        "THE Future (M+1) Vorkrisenniveau",
    ),
}


def parse_prices(chart: dict) -> pd.DataFrame:
    """Converts the chart JSON into one row per trading day.

    Args:
        chart: response of the chart endpoint, with "labels" (days as dd.mm.yyyy)
            and "datasets" (each with a "label" and its "data", missing values
            are the string "null").

    Returns:
        DataFrame indexed by "date" (datetime.date) with one column per price in
        EUR/MWh. Days without any price are dropped.

    Raises:
        ValueError: if a series of SERIES is missing, e.g. after a change of the chart.
    """
    series = {
        dataset["label"]: pd.Series(
            [None if value == "null" else value for value in dataset["data"]],
            dtype=float,
        )
        for dataset in chart["datasets"]
    }
    missing = {label for labels in SERIES.values() for label in labels} - set(series)
    if missing:
        raise ValueError(f"chart series missing: {sorted(missing)}")

    prices = pd.DataFrame(
        {
            column: series[label].fillna(series[before_crisis])
            for column, (label, before_crisis) in SERIES.items()
        }
    )
    prices.index = pd.Index(
        pd.to_datetime(chart["labels"], format="%d.%m.%Y").date, name="date"
    )
    # some holidays (e.g. 24.12.) are listed several times with different
    # day-ahead prices, presumably for the following gas days. The source does
    # not tell the gas day, so the first entry is kept.
    prices = prices[~prices.index.duplicated(keep="first")]
    return prices.dropna(how="all")


def fetch_prices() -> pd.DataFrame:
    """Downloads the full price history, see parse_prices for the result."""
    response = requests.get(
        DATA_URL, params={"view": "json", "id": CHART_ID}, timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    return parse_prices(response.json())


class BnetzaGasPriceCrawler(ContinuousCrawler):
    """Stores the daily THE gas prices in the table gas_prices.

    The table is a plain table, not a hypertable: about 250 rows per year do not
    need chunking.
    """

    def _stored_day(self, aggregate: str) -> datetime | None:
        """Returns MIN or MAX of the stored days as UTC midnight, None if empty."""
        with self.engine.connect() as conn:
            if not inspect(conn).has_table(TABLE):
                return None
            day = conn.execute(text(f"SELECT {aggregate}(date) FROM {TABLE}")).scalar()
        return None if day is None else as_utc(day)

    def get_latest_data(self) -> datetime:
        """Returns the latest stored day, which is fetched again on the next run."""
        return self._stored_day("MAX") or TEMPORAL_START

    def get_first_data(self) -> datetime:
        """Returns the first stored day."""
        return self._stored_day("MIN") or TEMPORAL_START

    def crawl_from_to(self, begin: datetime, end: datetime):
        """Stores the prices of the days from begin (inclusive) until end (exclusive).

        The endpoint has no date filter, so the full history is downloaded and
        cut to the range. Stored days within the range are replaced.

        Args:
            begin (datetime): included begin datetime from which to crawl
            end (datetime): exclusive end datetime until which to crawl
        """
        prices = fetch_prices()
        day_starts = pd.to_datetime(prices.index).tz_localize(UTC)
        prices = prices[(day_starts >= as_utc(begin)) & (day_starts < as_utc(end))]
        if prices.empty:
            log.info("no new prices from %s until %s", begin, end)
            return

        with self.engine.begin() as conn:
            self.replace_overlap(conn, TABLE, prices, "date")
        log.info(
            "stored %d days from %s to %s",
            len(prices),
            prices.index[0],
            prices.index[-1],
        )


if __name__ == "__main__":
    logging.basicConfig()
    config = load_config(DEFAULT_CONFIG_LOCATION)
    crawler = BnetzaGasPriceCrawler("bnetza_gas_prices", config)
    crawler.crawl_temporal()
    crawler.set_metadata(metadata_info)
