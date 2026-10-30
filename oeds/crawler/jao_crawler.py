# SPDX-FileCopyrightText: Simon Hesselmann, Florian Maurer
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Downloads latest data from the Joint Allocation Office (JAO).

Currently, this only includes market results and their bids.
Data from the https://publicationtool.jao.eu/ is not yet included.

Good analysis of this data is included in https://boerman.dev/ and
https://data.boerman.dev/d/5CYxW2JVz/flows-scheduled-commercial-exchanges-day-ahead
"""

import logging
import time
from datetime import UTC, datetime, timedelta

import pandas as pd
import requests
from jao import JaoAPIClient
from sqlalchemy import MetaData, text
from sqlalchemy.exc import OperationalError

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    ContinuousCrawler,
    as_utc,
    ensure_utc,
    load_config,
    utcnow,
)

log = logging.getLogger("jao")

metadata_info = {
    "schema_name": "jao",
    "data_date": "2024-04-30",
    "data_source": "https://www.jao.eu/auctions#/",
    "license": "https://www.jao.eu/terms-conditions",
    "description": "JAO energy auction. Energy bids by country and timestamp.",
    "contact": "",
    "temporal_start": "2019-01-01 00:00:00",
    "temporal_end": "2024-04-30 00:00:00",
    "concave_hull_geometry": None,
}

MAX_QUERY_DAYS = 30  # the JAO API rejects periods longer than 31 days

TEMPORAL_START = datetime(2019, 1, 1, tzinfo=UTC)

# nested fields of the auction json, which are stored in their own tables or dropped
NESTED_AUCTION_FIELDS = ["results", "products", "maintenances", "winningParties"]

AUCTION_TIME_COLUMNS = [
    "lastDataUpdate",
    "marketPeriodStart",
    "marketPeriodStop",
    "atcGateOpening",
    "atcGateClosure",
    "bidGateOpening",
    "bidGateClosure",
    "disputeSubmissionGateOpening",
    "disputeSubmissionGateClosure",
    "disputeProcessGateOpening",
    "disputeProcessGateClosure",
    "provisionalAuctionresult",
    "ltResaleGateOpening",
    "ltResaleGateClosure",
    "periodToBeSecuredStart",
    "periodToBeSecuredStop",
]
BID_TIME_COLUMNS = ["lastDataUpdate", "latestCreationTime"]

# columns which can be entirely empty in a batch and must not be inferred as text
RESULT_NUMERIC_COLUMNS = [
    "atc",
    "resoldCapacity",
    "offeredCapacity",
    "requestedCapacity",
    "allocatedCapacity",
    "auctionPrice",
    "bidderPartyCount",
    "winnerPartyCount",
]
BID_NUMERIC_COLUMNS = [
    "price",
    "awardedQuantity",
    "awardedPrice",
    "resoldQuantity",
    "productMinutesDelivered",
    "quantity",
]


def to_utc(series: pd.Series) -> pd.Series:
    """Parses timestamps and returns them as timezone aware UTC."""
    return pd.to_datetime(series, utc=True, format="ISO8601").dt.tz_convert("UTC")


def convert_numeric_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    for column in columns:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce").astype("float64")
    return df


def convert_time_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    for column in columns:
        if column in df.columns:
            df[column] = to_utc(df[column])
    return df


class JaoClientWrapper:
    def __init__(self, api_key: str | None):
        if not api_key:
            raise Exception("No JAO API key provided")
        self.client = JaoAPIClient(api_key)

    def get_bids(self, auction_id: str):
        try:
            return self.client.query_auction_bids_by_id(auction_id)
        except requests.exceptions.HTTPError as e:
            log.error(f"Error fetching bids for auction {auction_id}: {e}")
            return pd.DataFrame()

    def get_auctions(
        self,
        corridor: str,
        from_date: datetime,
        to_date: datetime,
        horizon="Monthly",
    ) -> list[dict]:
        """Returns the raw auctions of a corridor, whose market period starts in [from_date, to_date].

        The API only allows queries of up to 31 days, so the interval is split.
        """
        auctions = {}
        day = from_date.date()
        last_day = to_date.date()
        while day <= last_day:
            chunk_end = min(day + timedelta(days=MAX_QUERY_DAYS), last_day)
            r = self.client.s.get(
                self.client.BASEURL + "getauctions",
                params={
                    "corridor": corridor,
                    "fromdate": day.strftime("%Y-%m-%d"),
                    "todate": chunk_end.strftime("%Y-%m-%d"),
                    "horizon": horizon,
                    "shadow": 0,
                },
            )
            day = chunk_end + timedelta(days=1)
            if r.status_code == 400:
                # "No Data found" is reported as a 400
                continue
            try:
                r.raise_for_status()
            except requests.exceptions.HTTPError as e:
                log.error(
                    f"Error fetching auctions for corridor {corridor} - {horizon} up to {chunk_end}: {e}"
                )
                continue
            for auction in r.json():
                auctions[auction["identification"]] = auction
        return list(auctions.values())

    def get_horizons(self):
        return self.client.query_auction_horizons()

    def get_corridors(self):
        return self.client.query_auction_corridors()


def auctions_to_frames(
    auctions: list[dict], horizon: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Splits raw auctions into one row per auction and one row per auction result (product)."""
    auction_rows = []
    result_rows = []
    for auction in auctions:
        auction_id = auction["identification"]
        row = {k: v for k, v in auction.items() if k not in NESTED_AUCTION_FIELDS}
        row["id"] = auction_id
        del row["identification"]
        row["corridor"] = row.pop("corridorCode")
        row["horizon"] = horizon
        row["winningPartyCount"] = len(auction.get("winningParties") or [])
        auction_rows.append(row)

        products = {
            (p.get("productIdentification"), p.get("productHour")): p
            for p in auction.get("products") or []
        }
        for result in auction.get("results") or []:
            product = products.get(
                (result.get("productIdentification"), result.get("productHour")), {}
            )
            merged = {**product, **result}
            merged["auctionId"] = auction_id
            merged["marketPeriodStart"] = auction["marketPeriodStart"]
            result_rows.append(merged)

    auctions_df = pd.DataFrame(auction_rows)
    results_df = pd.DataFrame(result_rows)
    if not auctions_df.empty:
        auctions_df = convert_time_columns(auctions_df, AUCTION_TIME_COLUMNS)
        auctions_df["date"] = auctions_df["marketPeriodStart"]
    if not results_df.empty:
        results_df = convert_numeric_columns(results_df, RESULT_NUMERIC_COLUMNS)
        results_df["date"] = to_utc(results_df.pop("marketPeriodStart"))
    return auctions_df, results_df


class JaoCrawler(ContinuousCrawler):
    TIMEDELTA = timedelta(days=2)
    # auctions are published late, known auctions are skipped so refetching is cheap
    REVISION_WINDOW = timedelta(days=45)

    def get_latest_data(self) -> datetime:
        query = text("SELECT MAX(date) FROM auctions")
        try:
            with self.engine.connect() as conn:
                return as_utc(conn.execute(query).scalar() or TEMPORAL_START)
        except Exception:
            log.info("No jao auctions found")
            return TEMPORAL_START

    def get_first_data(self) -> datetime:
        query = text("SELECT MIN(date) FROM auctions")
        try:
            with self.engine.connect() as conn:
                return as_utc(conn.execute(query).scalar() or TEMPORAL_START)
        except Exception:
            log.info("No jao auctions found")
            return TEMPORAL_START

    def get_tables(self):
        metadata = MetaData()
        metadata.reflect(bind=self.engine)
        return metadata.tables.keys()

    def create_hypertable_if_not_exists(self):
        for table_name in self.get_tables():
            self.create_single_hypertable_if_not_exists(table_name, "date")

    def get_known_auctions(self, horizon: str) -> set[str]:
        # auctions like CEEXD-M-BASE are listed for several corridors,
        # so the stored corridor can differ from the queried one
        query = text("SELECT id FROM auctions WHERE horizon = :h")
        try:
            with self.engine.connect() as conn:
                rows = conn.execute(query, {"h": horizon})
                return {row[0] for row in rows}
        except Exception:
            # table does not exist yet
            return set()

    def write_frame(self, df: pd.DataFrame, table_name: str, connection=None):
        df = ensure_utc(df)
        df.to_sql(
            table_name,
            connection,
            if_exists="append",
            index=False,
            method="multi",
            # postgres allows at most 65535 parameters per statement
            chunksize=60_000 // len(df.columns),
        )

    def crawl_single_horizon(
        self,
        jao_client,
        from_date,
        to_date,
        corridor,
        horizon,
    ):
        table_name = f"bids_{horizon.lower()}"
        table_name = table_name.replace("-", "_").replace(" ", "_")

        try:
            auctions = jao_client.get_auctions(corridor, from_date, to_date, horizon)
        except Exception as e:
            log.error(f"Did not get Auctions for {corridor} - {horizon}: {e}")
            return

        known = self.get_known_auctions(horizon)
        now = utcnow()
        new_auctions = []
        for auction in auctions:
            if auction["identification"] in known:
                continue
            # auctions without published results have no bids yet and are crawled later.
            # Auctions before about 2022-11 have no provisionalAuctionresult time.
            result_time = (
                auction.get("provisionalAuctionresult")
                or auction.get("disputeProcessGateClosure")
                or auction.get("bidGateClosure")
            )
            if auction.get("cancelled") or not result_time:
                continue
            if to_utc(pd.Series([result_time])).iloc[0] > now:
                continue
            new_auctions.append(auction)
        if not new_auctions:
            return

        log.info(
            f"started crawling bids of {corridor} - {horizon} for {len(new_auctions)} auctions"
        )
        auctions_df, results_df = auctions_to_frames(new_auctions, horizon)

        for auction_id in auctions_df["id"]:
            auction_date = auctions_df.loc[
                auctions_df["id"] == auction_id, "date"
            ].iloc[0]
            auction_df = auctions_df[auctions_df["id"] == auction_id]
            result_df = (
                results_df[results_df["auctionId"] == auction_id]
                if not results_df.empty
                else results_df
            )
            bids_data = jao_client.get_bids(auction_id)
            if not bids_data.empty:
                bids_data["auctionId"] = auction_id
                bids_data["date"] = auction_date
                bids_data = convert_time_columns(bids_data, BID_TIME_COLUMNS)
                bids_data = convert_numeric_columns(bids_data, BID_NUMERIC_COLUMNS)

            for attempt in range(2):
                try:
                    # one transaction per auction, the auction row marks it as done
                    with self.engine.begin() as connection:
                        if not bids_data.empty:
                            self.write_frame(bids_data, table_name, connection)
                        if not result_df.empty:
                            self.write_frame(result_df, "auction_results", connection)
                        self.write_frame(auction_df, "auctions", connection)
                    break
                except OperationalError:
                    log.exception(
                        f"database error writing auction {auction_id} - trying again"
                    )
                    time.sleep(5)

    def crawl_from_to(self, begin: datetime, end: datetime):
        jao_client = JaoClientWrapper(self.config.get("jao_api_key"))
        log.info(f"starting run_data_crawling from {begin} to {end}")
        horizons = jao_client.get_horizons()
        corridors = jao_client.get_corridors()
        for horizon in horizons:
            if "intraday" == horizon.lower():
                continue
            for corridor in corridors:
                log.info(f"crawl {horizon}, {corridor} - {begin} - {end}")
                self.crawl_single_horizon(jao_client, begin, end, corridor, horizon)
                log.info(f"finished crawling bids of {corridor} - {horizon}")
        log.info(f"finished run_data_crawling from {begin} to {end}")


if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    config = load_config(DEFAULT_CONFIG_LOCATION)
    craw = JaoCrawler("jao", config)

    now = utcnow()
    from_date = TEMPORAL_START
    # not sure if this is exclusive, so substract a ms to be safe
    to_date = datetime(now.year, now.month, 1, tzinfo=UTC) - timedelta(microseconds=1)

    craw.crawl_from_to(from_date, to_date)
    craw.set_metadata(metadata_info)
