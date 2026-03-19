import codecs
import logging
import re
import tarfile
from collections.abc import Generator
from pathlib import Path

import pandas as pd
import requests
from sqlalchemy import text
from tqdim import tqdm

from oeds.base_crawler import DEFAULT_CONFIG_LOCATION, DownloadOnceCrawler, load_config

PARKENDD_ARCHIVE_URL = "https://parkendd.de/dumps/Archive.tar.xz"


def iter_archive_dataframes(
    filename: str | Path,
    resampling: str = "1h",
) -> Generator[tuple[str, pd.DataFrame], None, None]:
    
    # tarfile does handle the gzip automatically
    with tarfile.open(filename) as tfp:
        
        # build map of lot_id to available csv filenames 
        #   i ignore 2015 since it's incomplete
        lot_id_filenames = dict()
        for filename in sorted(tfp.getnames()):
            if "backup" not in filename:
                match = re.match("(.*)-(20\d\d).csv", filename)
                if match:
                    lot_id, year = match.groups()
                    if year != "2015":
                        lot_id_filenames.setdefault(lot_id, []).append(filename)
        
        # for each lot
        for lot_id, filenames in lot_id_filenames.items():
            # if we have years 2016 - 2020
            if len(filenames) == 5:
                # build one DataFrame, resampled to 1 hour
                dfs = []
                for filename in filenames:
                    fp = tfp.extractfile(filename)
                    dfs.append(pd.read_csv(
                        codecs.getreader("utf-8")(fp), 
                        names=["date", "free"]
                    ))
                df = pd.concat(dfs, axis=0)
                df["date"] = pd.to_datetime(df["date"])
                try:
                    df = df.set_index("date").resample(resampling).mean()
                    yield lot_id, df
                except Exception:
                    pass

class ParkenDDCrawler(DownloadOnceCrawler):
    def structure_exists(self) -> bool:
        try:
            query = text("SELECT 1 from high_load_times limit 1")
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() == 1
        except Exception:
            return False

    def crawl_structural(self, recreate: bool = False):
        if not self.structure_exists() or recreate:
            result = requests.get(PARKENDD_ARCHIVE_URL)
            big_df = None
            for lot_id, df in tqdm(iter_archive_dataframes(result.content)):
                df["lot_id"] = lot_id
                single_data = df.reset_index().set_index(["date", "lot_id"])
                if big_df is None:
                    big_df = single_data
                else:
                    # append rows and sort by date
                    big_df = pd.concat([big_df, single_data]).sort_index()
            
            # x = lot_id, y = date
            big_df = big_df.unstack("lot_id")
            # drop the "free" label from columns, just keep lot_id
            big_df.columns = big_df.columns.droplevel()
            # store
            with self.engine.begin() as conn:
                big_df.to_sql("parkendd", conn)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)s %(module)s - %(funcName)s: %(message)s",
        datefmt="%d-%m-%Y %H:%M:%S",
    )

    config = load_config(DEFAULT_CONFIG_LOCATION)
    crawler = ParkenDDCrawler("vea_industrial_load_profiles", config)
    crawler.crawl_structural()
