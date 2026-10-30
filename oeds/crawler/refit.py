# SPDX-FileCopyrightText: Florian Maurer
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Data from REFIT paper
https://www.nature.com/articles/sdata2016122

REFIT (An electrical load measurements dataset of United Kingdom households from a two-year longitudinal study)

This dataset is typically used for NILM applications (non-intrusive load monitoring).
"""

import logging
import tempfile
from pathlib import Path

import cloudscraper
import pandas as pd
import py7zr
from sqlalchemy import text

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    DownloadOnceCrawler,
    copy_insert,
    load_config,
)

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


metadata_info = {
    "schema_name": "refit",
    "data_date": "2024-06-12",
    "data_source": "https://pure.strath.ac.uk/ws/portalfiles/portal/52873459/Processed_Data_CSV.7z",
    "license": "CC-BY-4.0",
    "description": "University of Strathclyde household energy usage. Time-stamped data on various household appliances' energy consumption, detailing usage patterns across different homes.",
    "contact": "",
    "temporal_start": "2013-10-09 13:06:17",
    "temporal_end": "2015-07-10 11:56:32",
}


REFIT_URL = (
    "https://pure.strath.ac.uk/ws/portalfiles/portal/52873459/Processed_Data_CSV.7z"
)
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
}
STAGING_TABLE = "refit_staging"


class RefitCrawler(DownloadOnceCrawler):
    def structure_exists(self) -> bool:
        try:
            query = text("SELECT 1 from refit limit 1")
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() == 1
        except Exception:
            return False

    def crawl_structural(self, recreate: bool = False):
        if not self.structure_exists() or recreate:
            log.info("Download refit dataset")
            self.download_refit_data()
            log.info("Finished writing REFIT to Database")
        self.create_hypertable_if_not_exists()

    def create_hypertable_if_not_exists(self):
        self.create_single_hypertable_if_not_exists("refit", "Time")

    def download_refit_data(self):
        # 2025-08-19 this only works with cloudflare circumvention
        scraper = cloudscraper.create_scraper()
        with tempfile.TemporaryDirectory() as tmpdir:
            archive = Path(tmpdir) / "refit.7z"
            with scraper.get(REFIT_URL, headers=headers, stream=True) as response:
                response.raise_for_status()
                with open(archive, "wb") as f:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
            target = Path(tmpdir) / "csv"
            with py7zr.SevenZipFile(archive, mode="r") as z:
                # py7zr >= 1.0 has no read(), extract to disk instead
                names = [info.filename for info in z.list() if not info.is_directory]
                z.extractall(path=target)
            log.info("Write refit to database")
            self.write_houses(target, names)

    def write_houses(self, target: Path, names: list[str]):
        # houses are appended to a staging table, which replaces refit only when
        # complete, so an interrupted crawl is not mistaken for a finished one
        with self.engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {STAGING_TABLE}"))
        for name in names:
            df = pd.read_csv(target / name)
            # the Time column is UK local time, Unix is epoch seconds (UTC)
            df["Time"] = pd.to_datetime(df["Unix"], unit="s", utc=True)
            df = df.drop(columns="Unix").set_index("Time")
            df["house"] = name
            log.info(f"writing {name}")

            with self.engine.begin() as conn:
                df.to_sql(
                    STAGING_TABLE,
                    conn,
                    if_exists="append",
                    chunksize=500000,
                    method=copy_insert if conn.dialect.name == "postgresql" else None,
                )
        with self.engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS refit"))
            conn.execute(text(f"ALTER TABLE {STAGING_TABLE} RENAME TO refit"))


if __name__ == "__main__":
    logging.basicConfig()

    config = load_config(DEFAULT_CONFIG_LOCATION)
    craw = RefitCrawler("refit", config)
    craw.crawl_structural()
