# SPDX-FileCopyrightText: Florian Maurer, Jonathan Sejdija
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from sqlalchemy import text

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    ContinuousCrawler,
    CrawlerConfig,
    DownloadOnceCrawler,
    as_utc,
    load_config,
    utcnow,
)

log = logging.getLogger("e2watch")
metadata_info = {
    "schema_name": "e2watch",
    "data_source": "https://stadt-aachen.e2watch.de/",
    "license": "https://www.aachen.de/DE/stadt_buerger/planen_bauen/gebaeudemanagement/SERVICE/2_energieanzeiger/e2watch_Informationen-zum-System.html",
    "description": "Aachen energy. Water, heat and power by building.",
    "contact": "",
    "temporal_start": "2022-01-01 00:00:00",
}
TEMPORAL_START = datetime(2019, 1, 2, 1, 0, 0, tzinfo=UTC)
MAX_DELTA = timedelta(weeks=4)  # We can query a year at once without problems
REQUEST_RETRIES = 4
REQUEST_BACKOFF_SECONDS = 2


class SourceUnavailableError(Exception):
    """The e2watch API is permanently gone (e.g. redirects to a vendor page)."""


def _to_utc(values: pd.Series) -> pd.Series:
    """Converts epoch milliseconds to aware UTC timestamps."""
    return pd.to_datetime(values, unit="ms", utc=True).dt.tz_convert("UTC")


class E2WatchCrawler(ContinuousCrawler, DownloadOnceCrawler):
    TIMEDELTA = timedelta(days=1)

    def __init__(self, schema_name: str, config: CrawlerConfig):
        super().__init__(schema_name, config=config)
        self.create_table_if_not_exists()

    def structure_exists(self) -> bool:
        """Checks if the buildings table exists in the database."""
        try:
            query = text("SELECT 1 from buildings limit 1")
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() == 1
        except Exception:
            return False

    def crawl_structural(self, recreate: bool = False):
        if not self.structure_exists() or recreate:
            buildings = self.get_all_buildings()
            with self.engine.begin() as conn:
                buildings.to_sql("buildings", con=conn, if_exists="replace")

        self.create_hypertable_if_not_exists()

    def get_latest_data(self) -> datetime:
        """Returns the latest timestamp of the e2watch data in the database."""
        sql = text("SELECT MAX(timestamp) FROM e2watch")
        try:
            with self.engine.connect() as conn:
                latest = conn.execute(sql).scalar() or TEMPORAL_START
            log.debug(f"Latest date in the database is {latest}")
            return as_utc(latest)
        except Exception as e:
            log.info(f"Using the default start date: {e}")
            return TEMPORAL_START

    def get_first_data(self) -> datetime:
        """Returns the earliest timestamp of the e2watch data in the database.

        Returns:
            datetime: Earliest timestamp
        """
        sql = text("SELECT MIN(timestamp) FROM e2watch")
        try:
            with self.engine.connect() as conn:
                latest = conn.execute(sql).scalar() or TEMPORAL_START
            log.debug(f"Latest date in the database is {latest}")
            return as_utc(latest)
        except Exception as e:
            log.info("Using the default start date: %s", e)
            return TEMPORAL_START

    def crawl_from_to(self, begin: datetime, end: datetime):
        """Crawls data from begin (inclusive) until end (exclusive)

        Args:
            begin (datetime): included begin datetime from which to crawl
            end (datetime): exclusive end datetime until which to crawl
        """
        sql = "select * from buildings"
        with self.engine.begin() as conn:
            building_data = pd.read_sql(sql, conn, parse_dates=["timestamp"])

        begin, end = as_utc(begin), as_utc(end)
        if begin < TEMPORAL_START:
            begin = TEMPORAL_START

        data_available_until = utcnow() - self.get_minimum_offset()

        if end > data_available_until:
            end = data_available_until

        sliced_begin = begin
        sliced_end = sliced_begin + MAX_DELTA
        try:
            while end > sliced_end:
                self._crawl_single_period(building_data, sliced_begin, sliced_end)
                sliced_begin = sliced_end
                sliced_end += MAX_DELTA
            self._crawl_single_period(building_data, sliced_begin, end)
        except SourceUnavailableError as e:
            log.error("e2watch source is unavailable, aborting the crawl: %s", e)

    def _crawl_single_period(
        self, buildings: pd.DataFrame, begin: datetime, end: datetime
    ):
        log.info("Crawling e2watch data from %s to %s", begin, end)
        for bilanzkreis_id in buildings.index.values:
            last_date_in_db = as_utc(
                self.select_latest_per_bilanzkreis(bilanzkreis_id)
            ) + timedelta(hours=1)
            if last_date_in_db > end:
                continue

            try:
                df_for_building = self.get_data_per_building(bilanzkreis_id, begin, end)
            except SourceUnavailableError:
                raise
            except Exception:
                log.exception(
                    "Skipping building %s for %s - %s", bilanzkreis_id, begin, end
                )
                continue
            if df_for_building.empty:
                continue
            # delete timezone duplicate
            # https://stackoverflow.com/a/34297689
            df_for_building = df_for_building[
                ~df_for_building.index.duplicated(keep="first")
            ]
            with self.engine.begin() as conn:
                df_for_building.to_sql("e2watch", con=conn, if_exists="append")

    def create_table_if_not_exists(self):
        try:
            query_create_hypertable = text(
                "SELECT public.create_hypertable('e2watch', 'timestamp', if_not_exists => TRUE, migrate_data => TRUE);"
            )
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE TABLE IF NOT EXISTS e2watch( "
                        "timestamp timestamptz NOT NULL, "
                        "bilanzkreis_id text, "
                        "strom_kwh double precision, "
                        "wasser_m3 double precision, "
                        "waerme_kwh double precision, "
                        "temperatur double precision, "
                        "PRIMARY KEY (timestamp , bilanzkreis_id));"
                    )
                )
                conn.execute(query_create_hypertable)
            log.info("created hypertable e2watch")
        except Exception as e:
            log.error(f"could not create hypertable: {e}")

        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE TABLE IF NOT EXISTS buildings( "
                        "bilanzkreis_id text, "
                        "building_id text, "
                        "lat double precision, "
                        "lon double precision, "
                        "beschreibung text, "
                        "strasse text, "
                        "plz text, "
                        "stadt text, "
                        "PRIMARY KEY (bilanzkreis_id));"
                    )
                )
            log.info("created table buildings")
        except Exception as e:
            log.error(f"could not create table: {e}")

    def get_all_buildings(self):
        sql = "select * from buildings"

        try:
            with self.engine.begin() as conn:
                building_data = pd.read_sql(sql, conn, parse_dates=["timestamp"])
            if len(building_data) > 0:
                log.info(
                    "Building data already exists in the database. No need to crawl it again."
                )
                building_data = building_data.set_index(["bilanzkreis_id"])
                return building_data
        except Exception as e:
            log.error(
                f"There does not exist a table buildings yet. The buildings will now be crawled. {e}"
            )

        df = pd.read_csv(
            Path(__file__).parent.parent / "data" / "e2watch_building_data.csv"
        )
        df = df.set_index(["bilanzkreis_id"])
        return df

    def _get_json(self, url: str) -> dict | None:
        """Fetches JSON with retries on transient errors.

        Returns None for a single failed request and raises
        SourceUnavailableError if the API redirects away (service gone).
        """
        for attempt in range(1, REQUEST_RETRIES + 1):
            try:
                response = requests.get(url, timeout=60, allow_redirects=False)
            except requests.exceptions.RequestException as e:
                log.warning("Request failed (attempt %s): %s", attempt, e)
            else:
                if response.is_redirect:
                    raise SourceUnavailableError(
                        f"{url} redirects to {response.headers.get('Location')}"
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    log.warning(
                        "HTTP %s (attempt %s) for %s",
                        response.status_code,
                        attempt,
                        url,
                    )
                elif not response.ok:
                    log.error("HTTP %s for %s", response.status_code, url)
                    return None
                else:
                    try:
                        return response.json()
                    except ValueError:
                        log.error(
                            "Response is not JSON for %s: %.200r", url, response.text
                        )
                        return None
            if attempt < REQUEST_RETRIES:
                time.sleep(REQUEST_BACKOFF_SECONDS * 2 ** (attempt - 1))
        return None

    def get_data_per_building(
        self, bilanzkreis_id: str, begin: datetime, end: datetime
    ) -> pd.DataFrame:
        energy = ["strom", "wasser", "waerme"]
        begin_str = begin.strftime("%d.%m.%Y")
        end_str = end.strftime("%d.%m.%Y")

        df_last = pd.DataFrame([])
        for measurement in energy:
            log.debug(
                "Downloading data for building: %s and %s", bilanzkreis_id, measurement
            )
            url = f"https://stadt-aachen.e2watch.de/gebaeude/getMainChartData/{bilanzkreis_id}?medium={measurement}&from={begin_str}&to={end_str}&type=stundenverbrauch"
            log.info(url)
            data = self._get_json(url)
            if data is None:
                log.error("Could not get data for building: %s", bilanzkreis_id)
                continue
            timeseries = pd.DataFrame.from_dict(data["result"]["series"][0]["data"])
            if timeseries.empty:
                log.info(f"Received empty data for building: {bilanzkreis_id}")
                continue
            timeseries[0] = _to_utc(timeseries[0])
            timeseries.columns = [
                "timestamp",
                (
                    measurement + "_kwh"
                    if measurement in ("strom", "waerme")
                    else measurement + "_m3"
                ),
            ]
            temperature = pd.DataFrame.from_dict(data["result"]["series"][1]["data"])
            if temperature.empty:
                log.info(f"Received empty temperature for building: {bilanzkreis_id}")
                continue
            temperature[0] = _to_utc(temperature[0])
            temperature.columns = ["timestamp", "temperatur"]
            timeseries = pd.merge(timeseries, temperature, on=["timestamp"])

            if not df_last.empty:
                df_last = pd.merge(timeseries, df_last, on=["timestamp", "temperatur"])

            else:
                df_last = timeseries

        if not df_last.empty:
            df_last.insert(0, "bilanzkreis_id", bilanzkreis_id)
            df_last = df_last.set_index(["timestamp", "bilanzkreis_id"])
        return df_last

    def select_latest_per_bilanzkreis(self, bilanzkreis_id) -> datetime:
        sql = text(
            f"select timestamp from e2watch where bilanzkreis_id='{bilanzkreis_id}' order by timestamp desc limit 1"
        )
        try:
            with self.engine.connect() as conn:
                latest = conn.execute(sql).scalar() or TEMPORAL_START
            log.info(
                "The latest date in the database for %s is %s", bilanzkreis_id, latest
            )
            return as_utc(latest)
        except Exception as e:
            log.info("Using the default start date: %s", e)
            return TEMPORAL_START


if __name__ == "__main__":
    logging.basicConfig(filename="e2watch.log", encoding="utf-8", level=logging.INFO)
    from pathlib import Path

    config = load_config(DEFAULT_CONFIG_LOCATION)
    e2watch = E2WatchCrawler("e2watch", config=config)
    e2watch.crawl_structural(recreate=False)
    e2watch.crawl_temporal()
