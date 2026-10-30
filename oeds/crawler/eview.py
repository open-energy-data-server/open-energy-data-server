# SPDX-FileCopyrightText: Florian Maurer, Jonathan Sejdija
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import logging
import re
from datetime import UTC, datetime, timedelta

import pandas as pd
import requests
from sqlalchemy import text

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    ContinuousCrawler,
    CrawlerConfig,
    as_utc,
    load_config,
    utcnow,
)

log = logging.getLogger("eview")
log.setLevel(logging.INFO)

metadata_info = {
    "schema_name": "eview",
    "data_date": "2024-06-12",
    "data_source": "http://www.eview.de/solarstromdaten/login.php",
    "license": "https://www.eview.de/solarstromdaten/anb.html",
    "description": "Eview solar energy. Plan specific time indexed performance.",
    "contact": "",
    "temporal_start": "2009-01-02 08:15:00",
    "temporal_end": "2024-06-10 21:45:00",
    "concave_hull_geometry": None,
}

# the export serves data from 2009 on (about 40 of the units that old)
TEMPORAL_START = datetime(2009, 1, 1, tzinfo=UTC)
# the export contains local time (Europe/Berlin), we store timestamptz (UTC)
LOCAL_TIMEZONE = "Europe/Berlin"
# using http instead of https to be faster


def local_to_utc(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Converts naive Europe/Berlin timestamps to aware UTC.

    Timestamps in the skipped hour of the spring DST change become NaT.
    The export has no repeated hour in autumn: unique ambiguous timestamps count
    as standard time (like postgres), for repeated ones the first is summer time.
    """
    index = pd.DatetimeIndex(index)
    first_of_repeated = index.duplicated(keep="last")
    return index.tz_localize(
        LOCAL_TIMEZONE, ambiguous=first_of_repeated, nonexistent="NaT"
    ).tz_convert("UTC")


class EViewCrawler(ContinuousCrawler):
    OFFSET_FROM_NOW = timedelta(days=1)

    def __init__(
        self,
        schema_name: str,
        config: CrawlerConfig,
        temporal_start: datetime = TEMPORAL_START,
        backfill: bool = False,
    ):
        """
        Args:
            temporal_start: first day to crawl for plants without data
            backfill: also crawl from temporal_start until the first stored day of each plant
        """
        super().__init__(schema_name, config)
        self.temporal_start = as_utc(temporal_start)
        self.backfill = backfill

    def get_solar_units(self):
        # crawl available pv units (listed multiple times)
        data = requests.get("http://www.eview.de/solarstromdaten/login.php")
        units = re.findall(r"login\.php\?p=;(\w{2});", data.text)
        return list(dict.fromkeys(units))

    def crawl_unit_date(self, unit, fetch_date):
        day = datetime.strftime(fetch_date, "%d.%m.%Y")
        url = f"http://www.eview.de/solarstromdaten/export.php?p=;{unit};z;dg1;f0;t{day}/1;km250"
        try:
            df = pd.read_csv(
                url,
                decimal=",",
                index_col=0,
                encoding="iso-8859-1",
                skiprows=4,
                parse_dates=True,
                dayfirst=True,
            )
            log.info("fetched %s records at %s for %s ", df.size, fetch_date, unit)
        except Exception:
            log.info("invalid at %s for %s ", fetch_date, unit)
            return
        if df.size < 2:
            log.info("no data at %s for %s ", fetch_date, unit)
            return
        ddf = df.unstack()
        ddf = ddf.reset_index()
        ddf.index = ddf["Datum und Uhrzeit"]
        ddf.index.name = "datetime"
        del ddf["Datum und Uhrzeit"]
        ddf.columns = ["plant", "value"]
        ddf["plant_id"] = unit
        ddf.index = local_to_utc(ddf.index)
        ddf.index.name = "datetime"
        ddf = ddf[ddf.index.notna()]
        with self.engine.begin() as conn:
            # fetching a day again must not duplicate it
            self.replace_overlap(conn, "eview", ddf, "datetime", ["plant_id"])

    def crawl_unit(self, unit: str, begin: datetime, end: datetime):
        log.info("fetching %s from %s until %s", unit, begin, end)
        for fetch_date in pd.date_range(
            as_utc(begin).date().isoformat(), as_utc(end).date().isoformat()
        ):
            self.crawl_unit_date(unit, fetch_date)

    def select_bound_per_unit(self, unit: str, func: str) -> datetime | None:
        """min/max of the stored (UTC) datetime of a unit - None if there is no data"""
        sql = text(f"select {func}(datetime) from eview where plant_id = :unit")
        try:
            with self.engine.begin() as conn:
                return conn.execute(sql, {"unit": unit}).scalar()
        except Exception as e:
            log.error(e)
            return None

    def select_latest_per_unit(self, unit: str) -> datetime:
        return self.select_bound_per_unit(unit, "max") or self.temporal_start

    def create_hypertable_if_not_exists(self) -> None:
        self.create_single_hypertable_if_not_exists("eview", "datetime")

    def get_latest_data(self) -> datetime:
        sql = text("select max(datetime) as datetime from eview")
        try:
            with self.engine.begin() as conn:
                return conn.execute(sql).scalar() or self.temporal_start
        except Exception as e:
            log.error(e)
            return self.temporal_start

    def get_first_data(self) -> datetime:
        sql = text("select min(datetime) as datetime from eview")
        try:
            with self.engine.begin() as conn:
                return conn.execute(sql).scalar() or self.temporal_start
        except Exception as e:
            log.error(e)
            return self.temporal_start

    def crawl_from_to(self, begin: datetime | None = None, end: datetime | None = None):
        """Crawls data from begin (inclusive) until end (exclusive)

        Args:
            begin (datetime): included begin datetime from which to crawl
            end (datetime): exclusive end datetime until which to crawl
        """
        # the last complete day
        last_day = utcnow() - timedelta(days=1)
        plant_end = min(as_utc(end), last_day) if end else last_day
        for plant in self.get_solar_units():
            try:
                if begin:
                    plant_begin = as_utc(begin)
                else:
                    # data of the (UTC) day of the latest value might be incomplete
                    plant_begin = as_utc(self.select_latest_per_unit(plant))
                    first = self.select_bound_per_unit(plant, "min")
                    if self.backfill and first and as_utc(first) > self.temporal_start:
                        self.crawl_unit(plant, self.temporal_start, as_utc(first))
                self.crawl_unit(plant, plant_begin, plant_end)
            except Exception:
                log.exception(f"Error with {plant}")

    def crawl_temporal(
        self, begin: datetime | None = None, end: datetime | None = None
    ):
        latest = as_utc(self.get_latest_data())

        if begin:
            begin = as_utc(begin)
            first = as_utc(self.get_first_data())
            if begin < first:
                self.crawl_from_to(begin, first)
        end = as_utc(end) if end else utcnow()
        log.info("latest date is %s", latest)
        if latest < end - self.__class__.get_minimum_offset():
            # leave begin none to crawl lost data, if it was not available for some parts
            # for this crawler, this selects the latest data per plant automatically
            self.crawl_from_to(begin=None, end=end)
        self.create_hypertable_if_not_exists()


if __name__ == "__main__":
    logging.basicConfig()
    config = load_config(DEFAULT_CONFIG_LOCATION)
    ec = EViewCrawler("eview", config)
    plant = "FI"
    ec.crawl_temporal()
