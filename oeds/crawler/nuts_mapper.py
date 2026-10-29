# SPDX-FileCopyrightText: Florian Maurer, Christian Rieke
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
This crawls the NUTS regions. There are various versions available.
Changes are available here:
https://ec.europa.eu/eurostat/web/nuts/history

More information on the avilable download data can be found here:
https://ec.europa.eu/eurostat/web/gisco/geodata/statistical-units/territorial-units-statistics
"""

import io
import logging
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from oeds.base_crawler import DEFAULT_CONFIG_LOCATION, DownloadOnceCrawler, load_config

log = logging.getLogger(__name__)

# Download shp zip for EU NUTS here:
# https://ec.europa.eu/eurostat/web/gisco/geodata/statistical-units/territorial-units-statistics
EU_SHP_NUTS_URL = "https://gisco-services.ec.europa.eu/distribution/v2/nuts/shp/NUTS_RG_01M_2024_4326.shp.zip"
EU_SHP_NUTS_FILENAME = "NUTS_RG_01M_2024_4326.shp"

# https://gisco-services.ec.europa.eu/tercet/flat-files
# download zip
EU_DE_ZIP_URL = (
    "https://gisco-services.ec.europa.eu/tercet/NUTS-2024/pc2025_DE_NUTS-2024_v1.0.zip"
)
EU_DE_ZIP_FILENAME = "pc2025_DE_NUTS-2024_v1.0.csv"


class NutsCrawler(DownloadOnceCrawler):
    def structure_exists(self) -> bool:
        try:
            query = text("SELECT 1 from plz limit 1")
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() == 1
        except Exception:
            return False

    def crawl_structural(self, recreate: bool = False):
        if not self.structure_exists() or recreate:
            log.info("download NUTS")
            self.download_nuts()
            log.info("finished downloading NUTS")

    def download_nuts(self):
        # download file
        log.info("download EU NUTS shapefile")
        r = requests.get(EU_SHP_NUTS_URL)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        # extract to shapes folder
        shapes_path = Path(__file__).parent.parent / "shapes"
        z.extractall(shapes_path)
        geo_path = shapes_path / EU_SHP_NUTS_FILENAME

        geo_information = gpd.read_file(geo_path)
        geo_information = geo_information.to_crs(4326)

        query = text("CREATE EXTENSION postgis;")
        try:
            with self.engine.connect() as conn:
                conn.execute(query)
        except ProgrammingError:
            pass

        # columns to lower
        geo_information.columns = map(str.lower, geo_information.columns)
        # ignore warning, geographic CRS centroid are enough for us
        # also see here: https://github.com/openclimatefix/nowcasting_dataset/issues/154#issuecomment-927148746
        centroids = geo_information["geometry"].centroid
        geo_information["longitude"] = centroids.x
        geo_information["latitude"] = centroids.y
        with self.engine.begin() as conn:
            geo_information.to_postgis("nuts", con=conn, if_exists="replace")
        log.info("finished writing EU NUTS shapefile")

        log.info("download EU DE zipcode list")
        r = requests.get(EU_DE_ZIP_URL)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        # open csv with pandas, handling possible directory prefix in archive
        csv_filename = next(
            (name for name in z.namelist() if name.endswith(".csv")), EU_DE_ZIP_FILENAME
        )
        with z.open(csv_filename) as f:
            plz_list = pd.read_csv(f, sep=";", index_col="CODE", quotechar="'")

        # remove str literals from plzlist with read_csv
        # where levl_code == 1 and country == DE
        geo_information = geo_information[geo_information["levl_code"] == 3]
        geo_information = geo_information[geo_information["cntr_code"] == "DE"]
        geo_information["nuts3"] = geo_information["nuts_id"]

        plz_list.columns = map(str.lower, plz_list.columns)
        plz_list["nuts2"] = plz_list["nuts3"].str[:5]
        plz_list["nuts1"] = plz_list["nuts3"].str[:4]
        plz_list.index.name = "code"

        # join geo on plz_list
        plz_join = plz_list.join(geo_information.set_index("nuts3"), on="nuts3")
        plz_join = plz_join[["nuts1", "nuts2", "nuts3", "longitude", "latitude"]]
        with self.engine.begin() as conn:
            plz_join.to_sql("plz", con=conn, if_exists="replace")
        log.info("finished writing EU DE zipcode list")


if __name__ == "__main__":
    logging.basicConfig()
    from pathlib import Path

    config = load_config(DEFAULT_CONFIG_LOCATION)
    crawler = NutsCrawler("public", config)
    crawler.crawl_structural(True)
