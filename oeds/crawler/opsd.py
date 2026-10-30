# SPDX-FileCopyrightText: Florian Maurer, Christian Rieke
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import logging
import sqlite3
from io import StringIO
from pathlib import Path

import pandas as pd
import requests
from sqlalchemy import text

from oeds.base_crawler import DEFAULT_CONFIG_LOCATION, DownloadOnceCrawler, load_config

log = logging.getLogger("opsd")
log.setLevel(logging.INFO)

metadata_info = {
    "schema_name": "opsd",
    "data_date": "2020-12-31",
    "data_source": "https://data.open-power-system-data.org/when2heat/latest/when2heat.sqlite",
    "license": "CC-BY-4.0",
    "description": "Open Power System Data. When to heat dataset, heating profiles for differenz countries & systems.",
    "contact": "",
    "temporal_start": "2007-12-31 22:00:00",
    "temporal_end": "2020-12-31 23:00:00",
}


when2heat_path = Path(__file__).parent.parent / "when2heat.db"
when2heat_url = (
    "https://data.open-power-system-data.org/when2heat/latest/when2heat.sqlite"
)
national_generation_capacity_url = "https://data.open-power-system-data.org/national_generation_capacity/2020-10-01/national_generation_capacity_stacked.csv"


def prepare_when2heat(data: pd.DataFrame) -> pd.DataFrame:
    """Indexes when2heat by utc_timestamp as aware UTC.

    cet_cest_timestamp is kept as a column, converted to aware UTC as well.
    """
    data = data.copy()
    utc = pd.to_datetime(data["utc_timestamp"], utc=True)
    data["cet_cest_timestamp"] = pd.to_datetime(data["cet_cest_timestamp"], utc=True)
    data.index = pd.DatetimeIndex(utc)
    data.index.name = "utc_timestamp"
    del data["utc_timestamp"]
    return data


class OpsdCrawler(DownloadOnceCrawler):
    def structure_exists(self) -> bool:
        try:
            query = text("SELECT 1 from national_generation_capacity limit 1")
            with self.engine.connect() as conn:
                capacities_exists = conn.execute(query).scalar() == 1

            query = text("SELECT 1 from when2heat limit 1")
            with self.engine.connect() as conn:
                when2heat_exists = conn.execute(query).scalar() == 1
            return when2heat_exists and capacities_exists
        except Exception:
            return False

    def crawl_structural(self, recreate: bool = False):
        if not self.structure_exists() or recreate:
            self.crawl_capacities()
            self.write_when2_heat()
        self.create_hypertable_if_not_exists()

    def create_hypertable_if_not_exists(self):
        self.create_single_hypertable_if_not_exists("when2heat", "utc_timestamp")

    def write_when2_heat(self, db_path: Path = when2heat_path):
        """
        efficiency of heat pumps in different countries for different types of heatpumps
        """
        if db_path.is_file():
            log.info(f"{db_path} already exists")
        else:
            when2heat_file = requests.get(when2heat_url)
            with open(db_path, "wb") as f:
                f.write(when2heat_file.content)
            log.info(f"downloaded when2heat.db to {db_path}")

        with sqlite3.connect(db_path) as conn:
            data = pd.read_sql("select * from when2heat", conn)
        data = prepare_when2heat(data)
        log.info("data read successfully")

        with self.engine.begin() as conn:
            # when2heat is a hypertable, so it must not be dropped and recreated
            self.replace_overlap(
                conn, "when2heat", data, "utc_timestamp", chunksize=20000
            )
        log.info("when2heat data written successfully")

    def crawl_capacities(self):
        log.info("Fetching data from %s", national_generation_capacity_url)
        response = requests.get(national_generation_capacity_url)
        response.raise_for_status()

        log.info("Loading OPSD capacities into DataFrame")
        data = pd.read_csv(StringIO(response.text))

        log.info("Writing OPSD capacities to the database")
        with self.engine.begin() as conn:
            data.to_sql(
                "national_generation_capacity",
                conn,
                if_exists="replace",
            )
        log.info("OPSD capacities written successfully")


if __name__ == "__main__":
    logging.basicConfig()
    from pathlib import Path

    config = load_config(DEFAULT_CONFIG_LOCATION)
    craw = OpsdCrawler("opsd", config)
    craw.crawl_structural(recreate=True)
