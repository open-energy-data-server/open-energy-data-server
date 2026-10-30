# SPDX-FileCopyrightText: Christian Rieke, Florian Maurer
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import logging
import zipfile
from io import BytesIO

import pandas as pd
import requests
from sqlalchemy import text

from oeds.base_crawler import DEFAULT_CONFIG_LOCATION, DownloadOnceCrawler, load_config

log = logging.getLogger(__name__)

metadata_info = {
    "schema_name": "ninja",
    "data_date": "2016-12-31",
    "data_source": "https://www.renewables.ninja/downloads",
    "license": "CC-BY-4.0",
    "description": "NINJA renewables capacity. Country specific capacities for wind and solar.",
    "contact": "",
    "temporal_start": "1980-01-01 00:00:00",
    "temporal_end": "2016-12-31 23:00:00",
}

WIND_URL = "https://www.renewables.ninja/downloads/ninja_europe_wind_v1.1.zip"
SOLAR_URL = "https://www.renewables.ninja/downloads/ninja_europe_pv_v1.1.zip"


class NinjaCrawler(DownloadOnceCrawler):
    def structure_exists(self) -> bool:
        try:
            query = text("SELECT 1 from capacity_wind_off limit 1")
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() == 1
        except Exception:
            return False

    def write_wind_capacity_factors(self, url):
        log.info("Crawling renewables.ninja wind data")
        response = requests.get(url)
        with zipfile.ZipFile(BytesIO(response.content)) as z_file:
            with z_file.open(
                "ninja_wind_europe_v1.1_current_on-offshore.csv"
            ) as ninja_wind_file:
                data = pd.read_csv(ninja_wind_file, index_col=0)
                data.index = pd.to_datetime(data.index, utc=True)
                onshore = {
                    col.split("_")[0].lower(): data[col].values
                    for col in data.columns
                    if "ON" in col
                }
                with self.engine.begin() as conn:
                    df_on = pd.DataFrame(data=onshore, index=data.index)
                    df_on.to_sql("capacity_wind_on", conn, if_exists="replace")
                    offshore = {
                        col.split("_")[0].lower(): data[col].values
                        for col in data.columns
                        if "OFF" in col
                    }
                    df_off = pd.DataFrame(data=offshore, index=data.index)

                    df_off.to_sql("capacity_wind_off", conn, if_exists="replace")

    def write_solar_capacity_factors(self, url):
        log.info("Crawling renewables.ninja solar data")
        response = requests.get(url)
        with zipfile.ZipFile(BytesIO(response.content)) as z_file:
            with z_file.open("ninja_pv_europe_v1.1_merra2.csv") as ninja_solar_file:
                data = pd.read_csv(ninja_solar_file, index_col=0)
                data.index = pd.to_datetime(data.index, utc=True)
                data.columns = [col.lower() for col in data.columns]
                with self.engine.begin() as conn:
                    data.to_sql("capacity_solar_merra2", conn, if_exists="replace")

    def create_hypertable_if_not_exists(self) -> None:
        self.create_single_hypertable_if_not_exists("capacity_wind_on", "time")
        self.create_single_hypertable_if_not_exists("capacity_wind_off", "time")
        self.create_single_hypertable_if_not_exists("capacity_solar_merra2", "time")

    def crawl_structural(self, recreate: bool = False):
        if not self.structure_exists() or recreate:
            log.info("Crawling renewables.ninja data")
            self.write_wind_capacity_factors(WIND_URL)
            self.write_solar_capacity_factors(SOLAR_URL)
            log.info("Finished writing renewables.ninja data to Database")
        self.create_hypertable_if_not_exists()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    config = load_config(DEFAULT_CONFIG_LOCATION)
    mastr = NinjaCrawler("ninja", config=config)
    mastr.crawl_structural(recreate=False)
