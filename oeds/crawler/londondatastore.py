# SPDX-FileCopyrightText: Florian Maurer
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Consumption data of various households in london gathered from 2011 to 2014.
A sub-set of 1,100 customers (Dynamic Time of Use or dToU) were given specific times when their electricity tariff would be higher or lower price than normal
High (67.20p/kWh), Low (3.99p/kWh) or normal (11.76p/kWh).
The rest of the sample (around 4,500) were on a flat rate of 14.228p/kWh.
https://data.london.gov.uk/blog/electricity-consumption-in-a-sample-of-london-households/
"""

import logging
import tempfile
import zipfile

import pandas as pd
import requests
from sqlalchemy import text

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    DownloadOnceCrawler,
    copy_insert,
    load_config,
)

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


# DateTime is GMT (no gap on 2013-03-31, repeated midnight on 2013-10-27),
# so it is stored as timestamptz in UTC without conversion.
metadata_info = {
    "schema_name": "londondatastore",
    "data_date": "2014-02-28",
    "data_source": "https://data.london.gov.uk/download/smartmeter-energy-use-data-in-london-households/3527bf39-d93e-4071-8451-df2ade1ea4f2/LCL-FullData.zip",
    "license": "CC-BY-4.0",
    "description": "London energy consumption data. Real consumption data from london, timestamped.",
    "contact": "",
    "temporal_start": "2011-11-23 09:00:00",
    "temporal_end": "2014-02-28 00:00:00",
}

LONDON_FULL_URL = "https://data.london.gov.uk/download/smartmeter-energy-use-data-in-london-households/3527bf39-d93e-4071-8451-df2ade1ea4f2/LCL-FullData.zip"
STAGING_TABLE = "consumption_staging"
LONDON_PARTITIONED_URL = "https://data.london.gov.uk/download/smartmeter-energy-use-data-in-london-households/04feba67-f1a3-4563-98d0-f3071e3d56d1/Partitioned%20LCL%20Data.zip"


class LondonLoadData(DownloadOnceCrawler):
    def structure_exists(self) -> bool:
        # data is written to a staging table first, which is only renamed
        # to consumption once all files were written
        try:
            query = text("SELECT 1 from consumption limit 1")
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() == 1
        except Exception:
            return False

    def crawl_structural(self, recreate: bool = False, max_members: int | None = None):
        """
        Args:
            max_members: only import the first n files of the archive (for tests)
        """
        if not self.structure_exists() or recreate:
            self.download_london_data(max_members=max_members)
        self.create_hypertable_if_not_exists()

    def download_london_data(self, max_members: int | None = None):
        log.info("Download london smartmeter energy dataset")
        with self.engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {STAGING_TABLE}"))
        failed = []
        with tempfile.TemporaryFile() as archive:
            with requests.get(LONDON_PARTITIONED_URL, stream=True, timeout=60) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    archive.write(chunk)
            with zipfile.ZipFile(archive) as thezip:
                # should be single file only if full_data
                log.info("Write london energy dataset to database")
                members = [i for i in thezip.infolist() if not i.is_dir()]
                for zipinfo in members[:max_members]:
                    try:
                        with thezip.open(zipinfo) as thefile:
                            self.write_member(thefile)
                    except Exception:
                        log.exception(f"failed to write {zipinfo.filename}")
                        failed.append(zipinfo.filename)
                        continue
                    log.info(f"wrote {zipinfo.filename}")
        if failed:
            raise RuntimeError(
                f"{len(failed)} files failed, consumption was not replaced: {failed}"
            )
        with self.engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS consumption"))
            conn.execute(text(f"ALTER TABLE {STAGING_TABLE} RENAME TO consumption"))
        log.info("Finished writing london smartmeter energy dataset to Database")

    def write_member(self, thefile) -> None:
        df = pd.read_csv(thefile, na_values=["Null"])
        df.columns = [col.strip() for col in df.columns]
        df = df.rename(
            columns={
                "KWH/hh (per half hour)": "power",
                "stdorToU": "tariff",
            }
        )
        # DateTime is GMT, so it is UTC
        df["DateTime"] = pd.to_datetime(df["DateTime"], utc=True)
        # The data contains a few exactly duplicated rows (e.g. 2013-10-27 00:00).
        df = df.drop_duplicates(["LCLid", "DateTime"])
        df.to_sql(
            STAGING_TABLE,
            self.engine,
            if_exists="append",
            index=False,
            chunksize=500000,
            method=copy_insert
            if self.engine.url.drivername.startswith("postgresql")
            else None,
        )

    def create_hypertable_if_not_exists(self) -> None:
        self.create_single_hypertable_if_not_exists("consumption", "DateTime")


if __name__ == "__main__":
    logging.basicConfig()

    config = load_config(DEFAULT_CONFIG_LOCATION)
    craw = LondonLoadData("londondatastore", config)
    craw.crawl_structural(recreate=False)
