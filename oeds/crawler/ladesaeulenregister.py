# SPDX-FileCopyrightText: Vassily Aliseyko
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
The Charging station map is available at:
https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenkarte/Karte/Ladesaeulenkarte.html
One can download the raw file as CSV from this link:
The current CSV link is read from the page above, as its file name contains the date.
"""

import io
import logging
import re

import pandas as pd
import requests
from sqlalchemy import text

from oeds.base_crawler import DEFAULT_CONFIG_LOCATION, DownloadOnceCrawler, load_config

log = logging.getLogger("ladesaeulenregister")
log.setLevel(logging.INFO)

metadata_info = {
    "schema_name": "ladesaeulenregister",
    "data_date": "2025-07-18",
    "data_source": "https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenkarte/start.html",
    "license": "CC-BY-4.0",
    "description": "Charging stations for EV. Coordinate referenced power usage of individual chargers.",
    "contact": "",
    "temporal_start": None,
    "temporal_end": None,
}


LADESAEULEN_PAGE = "https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenkarte/start.html"
CSV_URL_PATTERN = re.compile(
    r"https://data\.bundesnetzagentur\.de/[^\"'\s<>]*Ladesaeulenregister_BNetzA_[\d-]+\.csv"
)


def find_csv_url() -> str:
    """The file name of the CSV contains the date, so read the current link from the BNetzA page."""
    response = requests.get(LADESAEULEN_PAGE, timeout=60)
    response.raise_for_status()
    urls = CSV_URL_PATTERN.findall(response.text)
    if not urls:
        raise RuntimeError(
            f"no Ladesaeulenregister CSV link found on {LADESAEULEN_PAGE}"
        )
    return sorted(set(urls))[-1]


class LadesaeulenregisterCrawler(DownloadOnceCrawler):
    def structure_exists(self) -> bool:
        try:
            query = text("SELECT 1 from ladesaeulenregister limit 1")
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() == 1
        except Exception:
            return False

    def crawl_structural(self, recreate: bool = False):
        if not self.structure_exists() or recreate:
            log.info("Crawling Ladesäulenregister")
            url = find_csv_url()
            log.info("Reading %s", url)
            response = requests.get(url, timeout=300)
            response.raise_for_status()
            df = pd.read_csv(
                io.BytesIO(response.content),
                skiprows=10,
                delimiter=";",
                encoding="iso-8859-1",
                index_col=0,
                decimal=",",
                low_memory=False,
            )

            with self.engine.begin() as conn:
                df.to_sql("ladesaeulenregister", conn, if_exists="replace")
            log.info("Finished writing Ladesäulenregister to Database")


if __name__ == "__main__":
    logging.basicConfig()

    config = load_config(DEFAULT_CONFIG_LOCATION)
    mastr = LadesaeulenregisterCrawler("ladesaeulenregister", config=config)
    mastr.crawl_structural(recreate=False)
