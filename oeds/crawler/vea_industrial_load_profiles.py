# SPDX-FileCopyrightText: Florian Maurer
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import logging
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from sqlalchemy import text

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    DownloadOnceCrawler,
    copy_insert,
    ensure_utc,
    load_config,
)

log = logging.getLogger("vea-industrial-load-profiles")


metadata_info = {
    "schema_name": "vea_industrial_load_profiles",
    "data_date": "2016-01-01",
    "data_source": "https://zenodo.org/records/13910298",
    "license": "CC-BY-4.0",
    "description": """The data consists of 5359 one-year quarterhourly industrial load profiles (2016, leap year, 35136 values).
    Each describes the electricity consumption of one industrial commercial site in Germany used for official accounting.
    Local electricity generation was excluded from the data as far as it could be discovered (no guarantee of completeness).
    Together with load profiles comes respective master data of the industrial sites as well as the information wether each quarterhour was a high load time of the connected German grid operator in 2016.
    The data was collected by the VEA.
    The dataset as a whole was assembled by Paul Hendrik Tiemann in 2017 by selecting complete load profiles without effects of renewable generation from a VEA internal database.
    It is a research dataset and was used for master theses and publications.""",
    "contact": "komanns@fh-aachen.de",
    "temporal_start": "2016-01-01 00:00:00",
    "temporal_end": "2016-12-31 23:45:00",
    "concave_hull_geometry": None,
}


class IndustrialLoadProfileCrawler(DownloadOnceCrawler):
    def structure_exists(self) -> bool:
        # master is written last, so it only exists if the whole crawl was completed
        try:
            query = text("SELECT 1 from master limit 1")
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() == 1
        except Exception:
            return False

    def crawl_structural(self, recreate: bool = False, max_profiles: int | None = None):
        """
        Args:
            max_profiles: only import the first n profiles of load and hlt (for tests)
        """
        if not self.structure_exists() or recreate:
            with tempfile.TemporaryDirectory() as tmpdir:
                self.data_dir = Path(tmpdir)
                self.request_extract_zip_archive(self.data_dir)

                # remove leftovers of aborted runs, as data is appended
                with self.engine.begin() as conn:
                    for table in ["load", "high_load_times", "master"]:
                        conn.execute(text(f'DROP TABLE IF EXISTS "{table}"'))

                self.timestamps = self.create_timestamps()
                self.write_profiles("load_profiles_tabsep.csv", "load", max_profiles)
                self.write_profiles(
                    "hlt_profiles_tabsep.csv", "high_load_times", max_profiles
                )

                # master data is written last, as it marks the crawl as complete
                df = pd.read_csv(self.data_dir / "master_data_tabsep.csv", sep="\t")
                df.columns = [x.lower() for x in df.columns]
                self.write_to_database(df, "master")
        self.create_hypertable_if_not_exists()

    def request_extract_zip_archive(self, directory: Path):
        """
        Downloads the zip archive for industrial load profiles from zenodo
        to disk and extracts it into directory.
        """

        url = (
            "https://zenodo.org/records/13910298/files/load-profile-data.zip?download=1"
        )

        log.info("Requesting zip archive from zenodo")

        archive = directory / "load-profile-data.zip"
        with requests.get(url, stream=True, timeout=60) as response:
            response.raise_for_status()
            with open(archive, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)

        log.info("Succesfully requested zip archive from zenodo")

        with zipfile.ZipFile(archive) as thezip:
            thezip.extractall(
                directory,
                members=[
                    "master_data_tabsep.csv",
                    "hlt_profiles_tabsep.csv",
                    "load_profiles_tabsep.csv",
                ],
            )
        archive.unlink()

    def create_timestamps(self) -> pd.DatetimeIndex:
        """Creates the timestamps of timestep time0, time1, ... as aware UTC.

        The profiles start at 2016-01-01 00:00 in Europe/Berlin.
        """
        timestamps = pd.date_range(
            start="2016-01-01 00:00:00",
            end="2016-12-31 23:45:00",
            freq="15min",
            tz="Europe/Berlin",
        )
        return timestamps.tz_convert("UTC")

    def write_profiles(
        self, filename: str, table: str, max_profiles: int | None = None
    ) -> None:
        """Reads a wide profile file (one row per profile, one column per timestep)
        in chunks of profiles, converts it to long format and writes it to the database.
        """
        log.info(f"Writing {filename} to {table}")
        rows_per_chunk = 100
        written = 0
        path = self.data_dir / filename
        for full_chunk in pd.read_csv(path, sep="\t", chunksize=rows_per_chunk):
            chunk = full_chunk
            if max_profiles is not None:
                chunk = full_chunk.iloc[: max_profiles - written]
                if chunk.empty:
                    break
            # timestep columns are named time0, time1, ...; ignore the empty trailing column
            steps = {
                int(c[4:]): c
                for c in chunk.columns
                if isinstance(c, str) and c.startswith("time")
            }
            columns = [steps[i] for i in sorted(steps)]
            n_steps = len(columns)
            df = pd.DataFrame(
                {
                    "id": np.repeat(chunk["id"].to_numpy(), n_steps),
                    "timestamp": self.timestamps[sorted(steps)].take(
                        np.tile(np.arange(n_steps), len(chunk))
                    ),
                    "value": chunk[columns].to_numpy().ravel(),
                }
            )
            self.write_to_database(df, table)
            written += len(chunk)
            log.info(f"{table}: {written} profiles written")

    def write_to_database(self, df: pd.DataFrame, name: str) -> None:
        """Appends dataframe to the table name in the database."""
        ensure_utc(df).to_sql(
            name=name,
            con=self.engine,
            if_exists="append",
            index=False,
            chunksize=500000,
            method=copy_insert
            if self.engine.url.drivername.startswith("postgresql")
            else None,
        )

    def create_hypertable_if_not_exists(self):
        self.create_single_hypertable_if_not_exists("high_load_times", "timestamp")
        self.create_single_hypertable_if_not_exists("load", "timestamp")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)s %(module)s - %(funcName)s: %(message)s",
        datefmt="%d-%m-%Y %H:%M:%S",
    )

    config = load_config(DEFAULT_CONFIG_LOCATION)
    crawler = IndustrialLoadProfileCrawler("vea_industrial_load_profiles", config)
    crawler.crawl_structural()
    crawler.set_metadata(metadata_info)
