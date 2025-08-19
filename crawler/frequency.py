# SPDX-FileCopyrightText: Florian Maurer
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import io
import logging
import zipfile

import pandas as pd
import requests
from sqlalchemy import text

from common.base_crawler import DownloadOnceCrawler, load_config

log = logging.getLogger("frequency")
log.setLevel(logging.INFO)

metadata_info = {
    "schema_name": "frequency",
    "data_date": "2019-09-01",
    "data_source": "https://www.50hertz.com/Portals/1/Dokumente/Transparenz/Regelenergie/Archiv%20Netzfrequenz/Netzfrequenz%20{year}.zip",
    "license": "usage allowed",
    "description": """Electricity net frequency for germany. Time indexed.
No license given, usage is desirable but without any liability: https://www.50hertz.com/Transparenz/Kennzahlen
""",
    "contact": "",
    "temporal_start": "2011-01-01 00:00:00",
    "temporal_end": "2019-09-01 00:00:00",
    "concave_hull_geometry": None,
}


def download_extract_zip(url):
    """
    Download a ZIP file and extract its contents in memory
    yields (filename, file-like object) pairs
    """
    response = requests.get(url)
    with zipfile.ZipFile(io.BytesIO(response.content)) as thezip:
        for zipinfo in thezip.infolist():
            with thezip.open(zipinfo) as thefile:
                yield zipinfo.filename, thefile, len(thezip.infolist())


class FrequencyCrawler(DownloadOnceCrawler):
    def structure_exists(self) -> bool:
        try:
            query = text("SELECT 1 from frequency limit 1")
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() == 1
        except Exception:
            return False

    def crawl_structural(self, recreate: bool = False):
        if not self.structure_exists() or recreate:
            self.crawl_frequency()
        self.create_hypertable_if_not_exists()

    def crawl_year_by_url(self, url):
        for name, thefile, count in download_extract_zip(url):
            log.info("file %s", name)
            if count == 1:  # only 2010
                df = pd.read_csv(
                    thefile,
                    sep=";",
                    decimal=",",
                    header=None,
                    names=["date_time", "frequency"],
                    # index_col='date',
                    # parse_dates=['date_time']
                )
                df.index = pd.to_datetime(
                    df.pop("date_time"), format="%d.%m.%Y %H:%M:%S"
                )

                del df["date_time"]
            else:
                df = pd.read_csv(
                    thefile,
                    sep=",",
                    header=None,
                )
                # timestamps like 2013/10/27 2A:00:00 can't be read
                df.index = pd.to_datetime(df.pop(0) + " " + df.pop(1), errors="coerce")

                if len(df.columns) == 2:
                    # delete the all "Frequ" column
                    del df[2]
                df.columns = ["frequency"]
            try:
                with self.engine.begin() as conn:
                    df.to_sql("frequency", conn, if_exists="append", chunksize=10000)
            except Exception as e:
                log.error(f"Error: {e}")

    def crawl_frequency(self, first=2011, last=2020):
        for year in range(first, last + 1):
            log.info("crawling the year %s", year)
            url = f"https://www.50hertz.com/Portals/1/Dokumente/Transparenz/Regelenergie/Archiv%20Netzfrequenz/Netzfrequenz%20{year}.zip"
            self.crawl_year_by_url(url)

    def create_hypertable_if_not_exists(self):
        self.create_single_hypertable_if_not_exists("frequency", "date_time")


if __name__ == "__main__":
    logging.basicConfig()
    from pathlib import Path

    config = load_config(Path(__file__).parent.parent / "config.yml")
    craw = FrequencyCrawler("frequency", config)
    craw.crawl_structural(recreate=False)
    # craw.crawl_frequency(2013, 2020)

    if False:
        year = 2010
        thefile = "Netzfrequenz 2019/201901_Frequenz.csv"
        thefile = "Netzfrequenz 2011/201101_Frequenz.txt"
        thefile = "Netzfrequenz 2010/Frequenz2010.csv"
        # try parsing 2010 csv files
        conn = "sqlite://freq.db"
        fc = FrequencyCrawler(conn)
        fc.crawl_frequency(first=2014)

        import matplotlib.pyplot as plt

        sql = "select date_time, frequency from frequency where date_time>2019-01-01"
        df = pd.read_sql(sql, conn)
        plt.plot(df["date_time"], df["frequency"])

        year = 2015
        url = f"https://www.50hertz.com/Portals/1/Dokumente/Transparenz/Regelenergie/Archiv%20Netzfrequenz/Netzfrequenz%20{year}.zip"
        fc.crawl_year_by_url(url)
