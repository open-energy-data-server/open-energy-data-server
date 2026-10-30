# SPDX-FileCopyrightText: Marvin Lorber
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Crawler for data from https://www.netztransparenz.de.
The resulting data is not available under an open-source license and should not be reshared but is available for crawling yourself.

Using this crawler requires setting up an Account and client in the
Netztransparenz extranet (see: https://www.netztransparenz.de/de-de/Web-API).

The client expects that the environment contains the variables 'IPNT_CLIENT_ID' and 'IPNT_CLIENT_SECRET'
with the credentials from the previous step.
"""

import logging
from datetime import UTC, datetime, timedelta

import netztransparenz as nt
import pandas as pd
import sqlalchemy as sql

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    ContinuousCrawler,
    CrawlerConfig,
    as_utc,
    ensure_utc,
    load_config,
    utcnow,
)

log = logging.getLogger("netztransparenz")
log.setLevel(logging.INFO)
api_date_format = "%Y-%m-%dT%H:%M:%S"
csv_date_format = "%Y-%m-%d %H:%M %Z"

metadata_info = {
    "schema_name": "netztransparenz",
    "data_date": "2015-02-10",
    "data_source": "https://ds.netztransparenz.de/api/",
    "license": "https://www.netztransparenz.de/en/About-us/Information-platforms/Disclosure-obligations-in-accordance-with-the-EU-Transparency-Regulation",
    "description": "German Energy Network Operations. Activated minimum and secondary reserve levels, forecasted solar and wind energy outputs, net reserve power balance, and redispatch measures.",
    "contact": "",
    "temporal_start": "2011-03-31 00:00:00",
    "temporal_end": "2024-05-22 16:00:00",
    "concave_hull_geometry": None,
}


def database_friendly(string):
    return string.lower().replace("(", "").replace(")", "").replace(" ", "_")


TEMPORAL_START = datetime(2011, 3, 31, tzinfo=UTC)


def to_utc(value) -> pd.Timestamp:
    """Aware UTC; naive datetimes are interpreted as UTC."""
    return as_utc(value)


def is_missing_credential(value) -> bool:
    return not value or str(value).strip().upper().startswith("YOUR_")


class NetztransparenzCrawler(ContinuousCrawler):
    TIMEDELTA = timedelta(days=2)
    # table name -> time column
    TIME_COLUMNS = {
        "prognose_solar": "von",
        "prognose_wind": "von",
        "hochrechnung_solar": "von",
        "hochrechnung_wind": "von",
        "vermarktung_inanspruchnahme_ausgleichsenergie": "von",
        "redispatch": "beginn",
        "nrv_saldo": "von",
        "rz_saldo": "von",
        "aktivierte_srl": "von",
        "aktivierte_mrl": "von",
        "value_of_avoided_activation": "von",
    }

    def __init__(self, schema_name: str, config: CrawlerConfig):
        super().__init__(schema_name, config)
        self.client = None
        self.initialize_token()

    def get_latest_data(self) -> datetime:
        return TEMPORAL_START

    def get_first_data(self) -> datetime:
        return TEMPORAL_START

    def initialize_token(self):
        # add your Client-ID and Client-secret from the API Client configuration GUI to
        # your environment variable first

        ipnt_client_id = self.config.get("ipnt_client_id")
        ipnt_client_secret = self.config.get("ipnt_client_secret")
        if is_missing_credential(ipnt_client_id) or is_missing_credential(
            ipnt_client_secret
        ):
            log.error(
                "Missing client credentials for netztransparenz. Please provide ipnt_client_id and ipnt_client_secret. Skipping crawler."
            )
            return

        try:
            self.client = nt.NetztransparenzClient(
                ipnt_client_id, ipnt_client_secret, strict=False
            )
        except Exception:
            log.exception("netztransparenz login failed - skipping crawler")

    def check_health(self):
        log.info(self.client.check_health())

    def _store(self, table: str, df: pd.DataFrame):
        """Writes df as aware UTC and replaces rows of the same time range, so that
        fetching an overlapping range again does not create duplicates."""
        df = ensure_utc(df)
        df = df.rename(mapper=database_friendly, axis="columns")
        time_column = self.TIME_COLUMNS[table]
        in_index = time_column not in df.columns
        times = df.index if in_index else df[time_column]
        if df.empty:
            return
        with self.engine.begin() as conn:
            if sql.inspect(conn).has_table(table):
                conn.execute(
                    sql.text(
                        f"DELETE FROM {table} WHERE {time_column} BETWEEN :lo AND :hi"
                    ).bindparams(
                        sql.bindparam("lo", type_=sql.DateTime(timezone=True)),
                        sql.bindparam("hi", type_=sql.DateTime(timezone=True)),
                    ),
                    {
                        "lo": times.min().to_pydatetime(),
                        "hi": times.max().to_pydatetime(),
                    },
                )
            df.to_sql(table, conn, if_exists="append", index=in_index)

    def forecast_solar(self):
        df = self.client.prognose_solar(transform_dates=True)
        self._store("prognose_solar", df)

    def forecast_wind(self):
        df = self.client.prognose_wind(transform_dates=True)
        self._store("prognose_wind", df)

    def extrapolation_solar(
        self, begin: datetime | None = None, end: datetime | None = None
    ):
        df = self.client.hochrechnung_solar(begin, end, True)
        self._store("hochrechnung_solar", df)

    def extrapolation_wind(
        self, begin: datetime | None = None, end: datetime | None = None
    ):
        df = self.client.hochrechnung_wind(begin, end, True)
        self._store("hochrechnung_wind", df)

    def utilization_balancing_energy(
        self, begin: datetime | None = None, end: datetime | None = None
    ):
        df = self.client.vermarktung_inanspruchnahme_ausgleichsenergie(begin, end, True)
        self._store("vermarktung_inanspruchnahme_ausgleichsenergie", df)

    def redispatch(self, begin: datetime | None = None, end: datetime | None = None):
        df = self.client.redispatch(begin, end, True)
        self._store("redispatch", df)

    def gcc_balance(self, begin: datetime, end: datetime):
        df = self.client.nrvsaldo_nrvsaldo_qualitaetsgesichert(begin, end, True)
        self._store("nrv_saldo", df)

    def lfc_area_balance(self, begin: datetime, end: datetime):
        df = self.client.nrvsaldo_rzsaldo_qualitaetsgesichert(begin, end, True)
        self._store("rz_saldo", df)

    def activated_automatic_balancing_capacity(self, begin: datetime, end: datetime):
        df = self.client.nrvsaldo_aktivierte_srl_qualitaetsgesichert(begin, end, True)
        self._store("aktivierte_srl", df)

    def activated_manual_balancing_capacity(self, begin: datetime, end: datetime):
        df = self.client.nrvsaldo_aktivierte_mrl_qualitaetsgesichert(begin, end, True)
        self._store("aktivierte_mrl", df)

    def value_of_avoided_activation(self, begin: datetime, end: datetime):
        df = self.client.nrvsaldo_voaa_qualitaetsgesichert(begin, end, True)
        self._store("value_of_avoided_activation", df)

    def find_latest(self, tablename: str, column_name: str, default):
        try:
            with self.engine.begin() as conn:
                query = sql.text(f"SELECT max({column_name}) FROM {tablename}")
                result = conn.execute(query).scalar() or default
                return result
        except Exception:
            return default

    def create_hypertable_if_not_exists(self) -> None:
        for tablename, time_column in self.TIME_COLUMNS.items():
            self.create_single_hypertable_if_not_exists(tablename, time_column)

    def check_table_exists(self, tablename):
        return sql.inspect(self.engine).has_table(tablename)

    def crawl_temporal(
        self, begin: datetime | None = None, end: datetime | None = None
    ):
        if self.client is None:
            log.error("no netztransparenz client available - skipping crawl")
            return
        # forecasts change with every publication, so they are refreshed on each run
        self.forecast_solar()
        self.forecast_wind()

        temporal_funcs = [
            # 0=function, 1=start of data, 2=crawling delay, 3=table name, 4=index name in table
            (
                self.extrapolation_wind,
                datetime(2011, 3, 31, 22, 0, tzinfo=UTC),
                timedelta(days=1),
                "hochrechnung_wind",
                "von",
            ),
            (
                self.extrapolation_solar,
                datetime(2011, 3, 31, 22, 0, tzinfo=UTC),
                timedelta(days=1),
                "hochrechnung_solar",
                "von",
            ),
            (
                self.utilization_balancing_energy,
                datetime(2011, 3, 31, 22, 0, tzinfo=UTC),
                timedelta(days=1),
                "vermarktung_inanspruchnahme_ausgleichsenergie",
                "von",
            ),
            (
                self.redispatch,
                datetime(2021, 10, 1, 0, 0, tzinfo=UTC),
                timedelta(days=1),
                "redispatch",
                "beginn",
            ),
            (
                self.activated_automatic_balancing_capacity,
                datetime(2014, 2, 25, 23, 0, tzinfo=UTC),
                timedelta(days=1),
                "aktivierte_srl",
                "von",
            ),
            (
                self.activated_manual_balancing_capacity,
                datetime(2011, 6, 26, 22, 0, tzinfo=UTC),
                timedelta(days=1),
                "aktivierte_mrl",
                "von",
            ),
            (
                self.value_of_avoided_activation,
                datetime(2023, 11, 1, 0, 0, tzinfo=UTC),
                timedelta(days=1),
                "value_of_avoided_activation",
                "von",
            ),
            (
                self.gcc_balance,
                datetime(2023, 11, 1, 0, 0, tzinfo=UTC),
                timedelta(days=30),
                "nrv_saldo",
                "von",
            ),
            (
                self.lfc_area_balance,
                datetime(2023, 11, 1, 0, 0, tzinfo=UTC),
                timedelta(days=30),
                "rz_saldo",
                "von",
            ),
        ]
        for func_tuple in temporal_funcs:
            log.info(f"Processing Table {func_tuple[3]}")
            latest = to_utc(
                self.find_latest(func_tuple[3], func_tuple[4], func_tuple[1])
            )
            current_begin = to_utc(begin) if begin else None
            current_end = to_utc(end) if end else None
            # overlapping rows are replaced when written, so begin may equal latest
            if current_begin is None or current_begin < latest:
                current_begin = latest
            earliest = as_utc(utcnow() - func_tuple[2])
            if current_end is None or current_end > earliest:
                current_end = earliest
            log.info(f"Crawling from {current_begin} to {current_end}")
            # the client interprets naive datetimes as local time, pass aware ones
            func_tuple[0](current_begin.to_pydatetime(), current_end.to_pydatetime())
        self.create_hypertable_if_not_exists()


if __name__ == "__main__":
    logging.basicConfig()

    config = load_config(DEFAULT_CONFIG_LOCATION)
    crawl = NetztransparenzCrawler("netztransparenz", config=config)
    crawl.crawl_temporal()
    crawl.set_metadata(metadata_info)
