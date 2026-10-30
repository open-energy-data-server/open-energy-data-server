# SPDX-FileCopyrightText: Marvin Lorber
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
This crawler downloads all the data of the GIE transparency platform. (https://agsi.gie.eu/, https://alsi.gie.eu/)
The resulting data is not available under an open-source license and should not be reshared but is available for crawling yourself.

license Information from Websire:
Data usage
    It is mandatory to credit or mention to GIE (Gas Infrastructure Europe), AGSI or ALSI as data source when using or repackaging this data.
Contact
    For data inquiries, please contact us via transparency@gie.eu

API and data documentation: https://www.gie.eu/transparency-platform/GIE_API_documentation_v007.pdf

This crawler uses the roiti-gie client: https://github.com/ROITI-Ltd/roiti-gie
"""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta

import aiohttp
import pandas as pd
from roiti.gie import GieRawClient
from roiti.gie.gie_raw_client import APIType
from sqlalchemy import inspect, text

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    ContinuousCrawler,
    CrawlerConfig,
    as_utc,
    load_config,
)

log = logging.getLogger("gie")
log.setLevel(logging.INFO)
# silence roiti logger
logging.getLogger("GieRawClient").setLevel(logging.WARNING)
logging.getLogger("GiePandasClient").setLevel(logging.WARNING)

metadata_info = {
    "schema_name": "gie",
    "data_date": "2024-06-12",
    "data_source": "https://agsi.gie.eu/",
    "license": "https://www.gie.eu/privacy-policy/",
    "description": "Gas Inventory Transparency. Time and country indexed capacity and consumption of gas.",
    "contact": "",
    "temporal_start": "2012-01-01 00:00:00",
    "temporal_end": "2024-06-11 01:13:10",
}
TEMPORAL_START = datetime(2012, 1, 1, tzinfo=UTC)
DATA_HIERARCHY = ["country", "company", "location"]


FLOATING_COLS = [
    "gasInStorage",
    "consumption",
    "consumptionFull",
    "injection",
    "withdrawal",
    "netWithdrawal",
    "workingGasVolume",
    "injectionCapacity",
    "withdrawalCapacity",
    "trend",
    "full",
    "inventory",
    "sendOut",
    "dtmi",
    "dtrs",
    "volume",
]
GAS_DAY_COLS = ["gasDayStart", "gasDayEnd"]


async def fetch_with_retry(client: GieRawClient, api_type, date, retries: int = 6):
    """The GIE API regularly answers with 5xx/timeouts, so retry with backoff.

    It also sometimes answers without data for days which exist, these are
    retried as well and logged if they stay empty.
    """
    for attempt in range(retries):
        try:
            response = await client.fetch(api_type, date=date)
            if response.get("data") or attempt == retries - 1:
                if not response.get("data"):
                    log.error(f"{api_type.name} {date} has no data")
                return response
            reason = "empty response"
        except (TimeoutError, aiohttp.ClientError) as e:
            if attempt == retries - 1:
                raise
            reason = e
        wait = 10 * 2**attempt
        log.warning(f"{api_type.name} {date} failed ({reason}) - retry in {wait}s")
        await asyncio.sleep(wait)


async def collect_gie_date(date, client: GieRawClient) -> dict[str, pd.DataFrame]:
    """Fetches the data of a gas day and returns the dataframe per table."""
    # query the raw API with the explicit API type: roiti-gie's
    # query_country_alsi_storage queries the AGSI endpoint by mistake
    agsi = await fetch_with_retry(client, APIType.AGSI, date)
    alsi = await fetch_with_retry(client, APIType.ALSI, date)

    frames: dict[str, list[pd.DataFrame]] = {}
    for identifier, response in (("agsi", agsi), ("alsi", alsi)):
        # top level entries are the regions (EU, Non-EU), their children
        # are the countries which are the first level we store
        collect_level(response.get("data", []), identifier, frames, 0)
    return {table: pd.concat(dfs) for table, dfs in frames.items()}


def to_dataframe(entries: list[dict], parent) -> pd.DataFrame:
    df = pd.DataFrame(entries)
    df = df.drop(columns=["children"], errors="ignore")
    # ALSI reports e.g. inventory and dtmi as {"lng": .., "gwh": ..}
    for col in df.columns:
        if df[col].apply(lambda v: isinstance(v, dict)).any():
            expanded = pd.DataFrame(
                [v if isinstance(v, dict) else {} for v in df[col]], index=df.index
            ).add_prefix(f"{col}_")
            df = pd.concat([df.drop(columns=[col]), expanded], axis=1)
    if "info" in df.columns:
        df["info"] = df["info"].apply(json.dumps)
    for col in df.columns:
        if col in FLOATING_COLS or col.split("_")[0] in FLOATING_COLS:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in GAS_DAY_COLS:
        if col in df.columns:
            # gas days are calendar days (06:00 to 06:00 local time), not instants
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
    if "updatedAt" in df.columns:
        # a plain timestamp without offset, which is read as UTC
        df["updatedAt"] = pd.to_datetime(df["updatedAt"], errors="coerce", utc=True)
    df["parent"] = parent
    df.columns = [c.lower() for c in df.columns]
    return df


def collect_level(
    entries: list[dict], data_identifier: str, frames: dict[str, list], level: int
):
    """Collects the children of all entries per table of the given level
    and recurses into their children."""
    for entry in entries:
        children = entry.get("children") or []
        if not children:
            continue
        table = f"gie_{data_identifier}_{DATA_HIERARCHY[level]}"
        frames.setdefault(table, []).append(to_dataframe(children, entry.get("name")))
        if level + 1 < len(DATA_HIERARCHY):
            collect_level(children, data_identifier, frames, level + 1)


GIE_TABLES = [
    f"gie_{identifier}_{level}"
    for identifier in ("agsi", "alsi")
    for level in DATA_HIERARCHY
]


class GieCrawler(ContinuousCrawler):
    # the data of the last gas days is incomplete and gets revised
    OFFSET_FROM_NOW = timedelta(days=2)
    REVISION_WINDOW = timedelta(days=3)

    def __init__(self, schema_name: str, config: CrawlerConfig):
        super().__init__(schema_name, config)
        self.API_KEY = config["gie_api_key"]
        if not self.API_KEY or self.API_KEY == "YOUR_GIE_API_KEY":
            raise Exception("GIE_API_KEY is not defined")

    def get_latest_data(self) -> datetime:
        # returns the first day which is not yet in the database
        # tables are written together, but the oldest table limits what is complete
        try:
            latest = None
            with self.engine.begin() as conn:
                for table in GIE_TABLES:
                    if not inspect(conn).has_table(table):
                        continue
                    table_latest = conn.execute(
                        text(f"SELECT max(gasdaystart) FROM {table}")
                    ).scalar()
                    if table_latest is not None:
                        table_latest = as_utc(table_latest)
                        latest = min(latest or table_latest, table_latest)
            if latest is None:
                return TEMPORAL_START
            return as_utc(latest) + timedelta(days=1)
        except Exception as e:
            log.error(
                f"Could not read start date - using default: {TEMPORAL_START} - {e}"
            )
            return TEMPORAL_START

    def get_first_data(self) -> datetime:
        sql = "SELECT min(gasdaystart) FROM gie_agsi_country AS gie"
        try:
            with self.engine.begin() as conn:
                first = conn.execute(text(sql)).scalar()
            return TEMPORAL_START if first is None else as_utc(first)
        except Exception as e:
            log.error(
                f"Could not read start date - using default: {TEMPORAL_START} - {e}"
            )
            return TEMPORAL_START

    async def crawl_data(self, begin: datetime, end: datetime):
        gie_client = GieRawClient(api_key=self.API_KEY)
        try:
            log.info(f"fetching from {begin} until {end}")
            api_call_count = 0
            for fetch_date in pd.date_range(begin, end):
                log.info(f"Handling {fetch_date}")
                api_call_count += 1
                if api_call_count > 30:
                    # The api limits clients to 60 Requests per second
                    # So we have to make sure to stay below that
                    time.sleep(1)
                    api_call_count = 0
                frames = await collect_gie_date(
                    datetime.strftime(fetch_date, "%Y-%m-%d"), gie_client
                )
                # fetching a gas day again replaces it in all tables at once
                with self.engine.begin() as conn:
                    for table, df in frames.items():
                        self.replace_overlap(conn, table, df, "gasdaystart")
        finally:
            await gie_client.close_session()

    def crawl_from_to(self, begin: datetime, end: datetime):
        """Crawls data from begin (inclusive) until end (exclusive)

        Args:
            begin (datetime): included begin datetime from which to crawl
            end (datetime): exclusive end datetime until which to crawl
        """
        end = end - timedelta(days=1)
        asyncio.run(self.crawl_data(begin, end))

    def create_hypertable_if_not_exists(self) -> None:
        for tablename in GIE_TABLES:
            self.create_single_hypertable_if_not_exists(tablename, "gasdaystart")


if __name__ == "__main__":
    logging.basicConfig()
    config = load_config(DEFAULT_CONFIG_LOCATION)
    ec = GieCrawler("gie", config)
    # ec.crawl_temporal()
    ec.create_hypertable_if_not_exists()
