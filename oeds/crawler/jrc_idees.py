# SPDX-FileCopyrightText: Florian Maurer
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
The Joint Research Centre's Integrated Database of the European Energy System (JRC-IDEES) compiles a rich set of information allowing for highly granular analyses of the dynamics of the European energy system, so as to better understand the past and create a robust basis for future policy assessments.
https://data.jrc.ec.europa.eu/dataset/82322924-506a-4c9a-8532-2bdd30d69bf5

This cralwer should be run once - the schema needs to be removed if run again.
"""

import io
import logging
import zipfile

import pandas as pd
import requests
from sqlalchemy import text

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    DownloadOnceCrawler,
    load_config,
)

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

metadata_info = {
    "schema_name": "jrc_idees",
    "data_source": "https://data.jrc.ec.europa.eu/dataset/82322924-506a-4c9a-8532-2bdd30d69bf5",
    "license": "CC-BY-4.0",
    "description": "Joint Research Centre's Integrated Database of the European Energy System (JRC-IDEES) compiles a rich set of information allowing for highly granular analyses of the dynamics of the European energy system",
    "contact": "",
    "temporal_start": "2000-01-01",
}

JRC_IDEES_URL = "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/JRC-IDEES/JRC-IDEES-2021_v1/JRC-IDEES-2021.zip"


class JrcIdeesCrawler(DownloadOnceCrawler):
    def structure_exists(self) -> bool:
        try:
            # the marker table is written last, so partial runs are repeated
            query = text("SELECT 1 FROM crawl_complete LIMIT 1")
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() == 1
        except Exception:
            return False

    def crawl_structural(self, recreate: bool = False):
        if not self.structure_exists() or recreate:
            log.info("Download JRC-IDEES dataset")
            self.download_jrc_dataset()
            self.create_hypertable_if_not_exists()
            pd.DataFrame({"complete": [True]}).to_sql(
                "crawl_complete", self.engine, if_exists="replace", index=False
            )
            log.info("Finished writing JRC-IDEES dataset to Database")

    def download_jrc_dataset(self):
        response = requests.get(JRC_IDEES_URL, timeout=300)
        response.raise_for_status()
        log.info("Write JRC-IDEES dataset to database")
        self.table_names = []
        # tables which were already (re)created in this run, the others are replaced
        written = set()
        with zipfile.ZipFile(io.BytesIO(response.content)) as thezip:
            for zipinfo in thezip.infolist():
                try:
                    with thezip.open(zipinfo) as thefile:
                        # zipinfo = name?
                        zone = thefile.name.split("/")[0]
                        log.info(thefile.name)
                        if "xls" not in thefile.name:
                            continue
                        xl = pd.ExcelFile(thefile)
                        for sheet_name in xl.sheet_names:
                            if sheet_name in ["index", "cover", "RES_hh_eff"]:
                                continue
                            index_col = [0]
                            if (
                                "EmissionBalance" in thefile.name
                                or "EnergyBalance" in thefile.name
                            ):
                                index_col = [0, 1]

                            df = xl.parse(sheet_name, index_col=index_col)
                            df.dropna(how="all", axis=1, inplace=True)
                            df.columns = [
                                col.strip() if isinstance(col, str) else col
                                for col in df.columns
                            ]
                            df = df[~df.index.duplicated(keep="first")]
                            df = df.T
                            df.index = pd.to_datetime(
                                df.index, format="%Y", errors="coerce"
                            )
                            df.index.name = "year"
                            if len(index_col) > 1:
                                df.columns = df.columns.map("_".join).map(
                                    lambda x: x.strip("_")
                                )
                            splitted = thefile.name.split(".")[0].split("_")
                            zone = splitted[-1]
                            # the middle part is the section - might be more than one word
                            section = "_".join(splitted[1:-1])
                            # insert as to leftmost columns
                            df.insert(0, "zone", zone)

                            # skip rows whose label is no year
                            df = df[df.index.notna()]
                            if df.empty:
                                continue
                            table_name = f"{section}_{sheet_name}".lower()
                            with self.engine.begin() as conn:
                                df.to_sql(
                                    table_name,
                                    conn,
                                    if_exists="append"
                                    if table_name in written
                                    else "replace",
                                )
                            written.add(table_name)
                            if table_name not in self.table_names:
                                self.table_names.append(table_name)
                except Exception as e:
                    log.error(f"Error: {e} - {zipinfo}")

    def create_hypertable_if_not_exists(self) -> None:
        for table_name in self.table_names:
            self.create_single_hypertable_if_not_exists(table_name, "year")


if __name__ == "__main__":
    logging.basicConfig()

    config = load_config(DEFAULT_CONFIG_LOCATION)
    crawler = JrcIdeesCrawler("jrc_idees", config)
    crawler.crawl_structural()
    crawler.set_metadata(metadata_info)
