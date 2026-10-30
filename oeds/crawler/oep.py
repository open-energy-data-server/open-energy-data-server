# SPDX-FileCopyrightText: Florian Maurer, Christian Rieke
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Synthetic data from OpenEGO project.

More information found here:
https://openenergyplatform.org/database/tables/ego_dp_loadarea

and here: https://github.com/openego/eGon-data
"""

import logging
import os
from pathlib import Path

import pandas as pd
import requests
from sqlalchemy import text

from oeds.base_crawler import DEFAULT_CONFIG_LOCATION, DownloadOnceCrawler, load_config

log = logging.getLogger("oep")
log.setLevel(logging.INFO)

REQUEST_TIMEOUT = 300

oep_ego_file_path = Path(__file__).parent.parent / "oep_ego.csv"
# the file is about 10GB of size
ego_url = "https://openenergyplatform.org/api/v0/tables/ego_dp_loadarea/rows/?form=csv"


class OepCrawler(DownloadOnceCrawler):
    def structure_exists(self) -> bool:
        try:
            query = text("SELECT 1 from ego_demand limit 1")
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() == 1
        except Exception:
            return False

    def crawl_structural(self, recreate: bool = False):
        if not self.structure_exists() or recreate:
            self.crawl_oep()

    def crawl_oep(self, oep_ego_file_path=oep_ego_file_path):
        """
        synthetic demand data for German NUTS areas from OpenEGO project
        """
        if oep_ego_file_path.is_file():
            log.info("%s already exists", oep_ego_file_path)
        else:
            part_path = oep_ego_file_path.with_name(oep_ego_file_path.name + ".part")
            try:
                with requests.get(
                    ego_url, stream=True, timeout=REQUEST_TIMEOUT
                ) as resp:
                    resp.raise_for_status()
                    with open(part_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=1024 * 1024):
                            f.write(chunk)
                os.replace(part_path, oep_ego_file_path)
            except Exception:
                # a broken download leaves up to 10 GB behind
                part_path.unlink(missing_ok=True)
                raise
            log.info("downloaded ego_demand to %s", oep_ego_file_path)
        demand = pd.read_csv(oep_ego_file_path)
        # delete geometries, as NUTS is available in public already
        demand.drop(
            columns=["geom", "geom_centre", "geom_surfacepoint", "geom_centroid"],
            inplace=True,
        )
        with self.engine.begin() as conn:
            demand.to_sql(
                "ego_demand",
                con=conn,
                if_exists="replace",
                chunksize=20000,
            )
        log.info("ego_demand data written successfully")


if __name__ == "__main__":
    logging.basicConfig()

    config = load_config(DEFAULT_CONFIG_LOCATION)
    craw = OepCrawler("oep", config)
    craw.crawl_structural(recreate=True)
