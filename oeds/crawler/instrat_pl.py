# SPDX-FileCopyrightText: Florian Maurer, Christian Rieke
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
This retrieves EU ETS prices from a polish website.

The EU Emission Trading System (ETS) sells emission allowances to
companies that emit greenhouse gases.

For this lots of 1000t CO2eq are sold with a given price in €/t CO2eq.
"""

import io
import logging
from datetime import datetime, timedelta

import pandas as pd
import requests
import yfinance as yf
from sqlalchemy import text

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    ContinuousCrawler,
    as_utc,
    load_config,
)

log = logging.getLogger("instrat_pl")
log.setLevel(logging.INFO)

metadata_info = {
    "schema_name": "eu_ets",
    "data_source": "https://energy.instrat.pl/en/prices/eu-ets/",
    "license": "CC-BY-4.0",
    "description": "EU-ETS, coal and gas prices from polish energy provider",
    "contact": "",
    "temporal_start": "2012-01-03",
}

# hint: one can call ?all=1 to get all data
# €/tCO2
EU_ETS_URL = "https://energy-api.instrat.pl/api/prices/co2"
# coal used for electricity generation PLN/GJ or PLN/t
COAL_URL = "https://energy-api.instrat.pl/api/coal/pscmi_1"
# the heat_url switches between Y-m-d and Y-d-m and is not practicable to parse
COAL_HEAT_URL = "https://energy-api.instrat.pl/api/coal/pscmi_2"
# gas used for electricity generation PLN/MWh
GAS_URL = "https://energy-api.instrat.pl/api/prices/gas_price_rdn_daily"
TEMPORAL_START = datetime(2012, 1, 3)


class InstratPlCrawler(ContinuousCrawler):
    TIMEDELTA = timedelta(days=2)

    def get_latest_data(self) -> datetime:
        query = text("SELECT MAX(date) FROM eu_ets")
        try:
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() + timedelta(hours=12)
        except Exception:
            log.error("No instrat_pl data found")
            return TEMPORAL_START

    def get_first_data(self) -> datetime:
        query = text("SELECT MIN(date) FROM eu_ets")
        try:
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() or TEMPORAL_START
        except Exception:
            log.error("No intrat_pl data found")
            return TEMPORAL_START

    def crawl_from_to(self, begin: datetime, end: datetime):
        """Crawls data from begin (inclusive) until end (exclusive)

        Args:
            begin (datetime): included begin datetime from which to crawl
            end (datetime): exclusive end datetime until which to crawl
        """
        # "date" holds calendar days (midnight, no instant): they are stored as
        # timestamp without time zone, so the UTC bounds are compared as dates
        begin = as_utc(begin).tz_localize(None)
        end = as_utc(end).tz_localize(None)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
        }

        def latest_in_table(table: str, default: datetime) -> pd.Timestamp:
            try:
                with self.engine.connect() as conn:
                    latest = conn.execute(
                        text(f"SELECT MAX(date) FROM {table}")
                    ).scalar()
                # the database returns python datetimes (or strings with sqlite)
                return pd.Timestamp(latest if latest is not None else default)
            except Exception:
                return pd.Timestamp(default)

        def download_data(url, table, default_begin):
            """Download rows newer than the latest row already stored in table."""
            latest = latest_in_table(table, default_begin - timedelta(days=1))
            # each table is resumed on its own, as the sources publish at
            # different cadences (daily, monthly)
            params = {
                "date_from": (latest + timedelta(days=1))
                .replace(hour=0, minute=0, second=0)
                .strftime("%d-%m-%YT%H:%M:%SZ"),
                "date_to": end.strftime("%d-%m-%YT%H:%M:%SZ"),
            }
            log.info(
                f"Downloading {url} from {params['date_from']} to {params['date_to']}"
            )
            response = requests.get(url, params=params, headers=headers)
            response.raise_for_status()
            df = pd.read_json(io.StringIO(response.text))
            if df.empty or "date" not in df.columns:
                return pd.DataFrame()
            # the API changed column names to e.g. "PSCMI1 PLN per GJ"
            df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
            df = df.set_index("date").sort_index()
            df.index = df.index.tz_localize(None)
            df = df[df.index > latest]
            return df

        def pln_eur_rates(index):
            start = index[0].strftime("%Y-%m-%d")
            stop = (index[-1] + timedelta(days=1)).strftime("%Y-%m-%d")
            return yf.download("PLNEUR=X", start=start, end=stop, progress=False)[
                "Close"
            ]["PLNEUR=X"]

        tables = {}

        eu_ets_data = download_data(EU_ETS_URL, "eu_ets", begin)
        if not eu_ets_data.empty:
            # daily values; days without auction (weekends, holidays) get the
            # next available price, also directly after the stored data
            first_day = latest_in_table(
                "eu_ets", begin - timedelta(days=1)
            ) + timedelta(days=1)
            days = pd.date_range(
                first_day.normalize(), eu_ets_data.index[-1].normalize()
            )
            eu_ets_data = eu_ets_data.resample("D").bfill().reindex(days).bfill()
            eu_ets_data.index.name = "date"
            eu_ets_data.rename(columns={"price": "eur_per_tco2"}, inplace=True)
            tables["eu_ets"] = eu_ets_data

        ### COAL
        coal_data = download_data(COAL_URL, "coal_price", TEMPORAL_START)
        if not coal_data.empty:
            pln_eur = pln_eur_rates(coal_data.index)
            resample_pln_eur = pln_eur.resample("MS").bfill().ffill()
            resample_pln_eur = resample_pln_eur.reindex(coal_data.index).ffill().bfill()
            coal_data["steam_coal_eur_per_gj"] = (
                coal_data["pscmi1_pln_per_gj"] * resample_pln_eur
            )
            coal_data["steam_coal_eur_per_t"] = (
                coal_data["pscmi1_pln_per_t"] * resample_pln_eur
            )
            # 1 GJ = 1e9 Ws = 1e9/3600 Wh = 1e6/3600 kWh
            coal_data["price_eur_per_kwh"] = coal_data["steam_coal_eur_per_gj"] / (
                1e6 / 3600
            )  # GJ to kWh
            tables["coal_price"] = coal_data

        ### GAS data
        gas_data = download_data(GAS_URL, "gas_price", datetime(2016, 1, 2))
        if not gas_data.empty:
            pln_eur = pln_eur_rates(gas_data.index)
            resample_pln_eur = pln_eur.reindex(gas_data.index).ffill().bfill()
            gas_data.rename(columns={"price": "price_pln_per_mwh"}, inplace=True)
            gas_data["price_eur_per_mwh"] = (
                gas_data["price_pln_per_mwh"] * resample_pln_eur
            )
            gas_data["price_eur_per_kwh"] = gas_data["price_eur_per_mwh"] / 1e3
            tables["gas_price"] = gas_data

        try:
            with self.engine.begin() as conn:
                for name, df in tables.items():
                    df.to_sql(name=name, con=conn, if_exists="append", index=True)
                    log.info(f"wrote {len(df)} rows to {name}")
        except Exception:
            log.exception("error in instat_pl data download")

    def create_hypertable_if_not_exists(self) -> None:
        for table in ["eu_ets", "coal_price", "gas_price"]:
            self.create_single_hypertable_if_not_exists(table, "date")


if __name__ == "__main__":
    logging.basicConfig()

    config = load_config(DEFAULT_CONFIG_LOCATION)
    mastr = InstratPlCrawler("instrat_pl", config=config)
    mastr.crawl_temporal()
