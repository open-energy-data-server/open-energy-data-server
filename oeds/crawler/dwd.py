# SPDX-FileCopyrightText: Florian Maurer, Christian Rieke
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import bz2
import logging
import multiprocessing as mp
from datetime import UTC, datetime
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pygrib
import requests
from shapely.geometry import Point
from sqlalchemy import text
from tqdm import tqdm

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    ContinuousCrawler,
    as_utc,
    load_config,
)

log = logging.getLogger("openDWD_cosmo")
log.setLevel(logging.INFO)

# open dwd data
base_url = "https://opendata.dwd.de/climate_environment/REA/COSMO_REA6/hourly/2D/"
to_download = dict(
    temp_air="T_2M/T_2M.2D.",
    ghi="ASOB_S/ASOB_S.2D.",
    dni="ASWDIFD_S/ASWDIFD_S.2D.",
    dhi="ASWDIR_S/ASWDIR_S.2D.",
    wind_meridional="V_10M/V_10M.2D.",
    wind_zonal="U_10M/U_10M.2D.",
    rain_con="RAIN_CON/RAIN_CON.2D.",
    rain_gsp="RAIN_GSP/RAIN_GSP.2D.",
    cloud_cover="CLCT/CLCT.2D.",
)

geo_path = Path(__file__).parent.parent / "shapes" / "NUTS_RG_01M_2021_4326.shp"
DOWNLOAD_DIR = Path(__file__).parent.parent / "grb_files"
DATA_PATH = Path(__file__).parent.parent / "data"
COSMO_START = datetime(1995, 1, 1, tzinfo=UTC)

geo_information = None
if geo_path.exists():
    geo_information = gpd.read_file(geo_path)

dwd_latitude = None
if (DATA_PATH / "lat_coordinates.npy").exists():
    dwd_latitude = np.load(DATA_PATH / "lat_coordinates.npy")

dwd_longitude = None
if (DATA_PATH / "lon_coordinates.npy").exists():
    dwd_longitude = np.load(DATA_PATH / "lon_coordinates.npy")


def create_nuts_map(coords):
    i, j = coords
    point = Point(dwd_longitude[i][j], dwd_latitude[i][j])
    zipping = [
        nuts_id
        for geom, nuts_id in zip(
            geo_information["geometry"], geo_information["NUTS_ID"]
        )
        if geom.contains(point)
    ]
    if not zipping:
        return "x"
    else:
        return zipping[0]


class DWDCrawler(ContinuousCrawler):
    def __init__(self, schema_name, config, nuts_matrix):
        super().__init__(schema_name, config)
        self.nuts_matrix = nuts_matrix
        nuts = np.unique(nuts_matrix[[nuts_matrix != "x"]].reshape(-1))
        self.countries = np.asarray([area[:2] for area in nuts])
        self.values = np.zeros_like(nuts)

    def create_table(self):
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS cosmo( "
                    "time timestamptz NOT NULL, "
                    "nuts text, "
                    "country text, "
                    "temp_air double precision, "
                    "ghi double precision, "
                    "dni double precision, "
                    "dhi double precision, "
                    "wind_meridional double precision, "
                    "wind_zonal double precision, "
                    "rain_con double precision,"
                    "rain_gsp double precision, "
                    "cloud_cover double precision, "
                    "PRIMARY KEY (time , nuts));"
                )
            )

    def create_hypertable_if_not_exists(self) -> None:
        self.create_single_hypertable_if_not_exists("cosmo", "time")

    def _download_data(self, key, year, month):
        response = requests.get(f"{base_url}{to_download[key]}{year}{month}.grb.bz2")
        log.info(f"get weather for {key} with status code {response.status_code}")

        weather_data = bz2.decompress(response.content)

        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

        with open(DOWNLOAD_DIR / f"weather{year}{month}", "wb") as file:
            file.write(weather_data)

    def _delete_data(self, year, month):
        path = DOWNLOAD_DIR / f"weather{year}{month}"
        if path.is_file():
            path.unlink()

    def _create_dataframe(self, key, year, month):
        weather_data = pygrib.open(DOWNLOAD_DIR / f"weather{year}{month}")
        selector = str(weather_data.readline()).split("1:")[1].split(":")[0]
        size = len(weather_data.select(name=selector))

        data_frames = []
        for k in tqdm(range(size)):
            data_ = weather_data.select(name=selector)[k]
            df = pd.DataFrame(
                columns=[key, "nuts"],
                data={
                    key: data_.values[nuts_matrix != "x"].reshape(-1),
                    "nuts": nuts_matrix[nuts_matrix != "x"].reshape(-1),
                },
            )
            df = pd.DataFrame(df.groupby(["nuts"])[key].mean())
            df["nuts"] = df.index
            # COSMO-REA6 is in UTC
            df["time"] = pd.to_datetime(
                f"{year}{month}", format="%Y%m", utc=True
            ) + pd.DateOffset(hours=k)
            data_frames.append(df)

        log.info(f"read data with type: {key} in month {month} \n")
        weather_data.close()
        log.info("closed weather file \n")

        return pd.concat(data_frames, ignore_index=True)

    def get_latest_data(self) -> datetime:
        with self.engine.begin() as conn:
            latest = conn.execute(text("SELECT max(time) FROM cosmo")).scalar()
        return as_utc(latest) if latest else COSMO_START

    def get_first_data(self) -> datetime:
        with self.engine.begin() as conn:
            first = conn.execute(text("SELECT min(time) FROM cosmo")).scalar()
        return as_utc(first) if first else COSMO_START

    def crawl_from_to(self, begin: datetime, end: datetime):
        date_range = pd.date_range(
            start=as_utc(begin)
            .tz_localize(None)
            .to_period("M")
            .to_timestamp()
            .tz_localize("UTC"),
            end=as_utc(end),
            freq="MS",
            # month start
        )
        for date in tqdm(date_range):
            with self.engine.begin() as conn:
                try:
                    df = pd.DataFrame(columns=[key for key in to_download.keys()])
                    for key in to_download.keys():
                        self._download_data(key, str(date.year), f"{date.month:02d}")
                        data = self._create_dataframe(
                            key, str(date.year), f"{date.month:02d}"
                        )
                        df["time"] = data["time"]
                        df["nuts"] = data["nuts"]
                        df[key] = data[key]
                        self._delete_data(str(date.year), f"{date.month:02d}")
                    df["country"] = [area[:2] for area in df["nuts"].values]
                    index = pd.MultiIndex.from_arrays(
                        [df["time"], df["nuts"]], names=["time", "nuts"]
                    )
                    df.index = index
                    del df["time"], df["nuts"]
                    log.info(
                        f"built data for  {date.month_name()} and start import to db"
                    )
                    # data is stored per month: replace it if it exists
                    conn.execute(
                        text("DELETE FROM cosmo WHERE time >= :first AND time < :next"),
                        {
                            "first": date.to_pydatetime(),
                            "next": (date + pd.DateOffset(months=1)).to_pydatetime(),
                        },
                    )
                    df.to_sql("cosmo", con=conn, if_exists="append")
                    log.info("import in db complete --> start with next hour")
                except Exception as e:
                    log.error(repr(e))
                    log.exception(f"could not read {date}")


def create_nuts_matrix(nuts_matrix_path):
    max_processes = mp.cpu_count() - 1
    log.info("(re)creating nuts matrix - might take 10 minutes")

    with mp.Pool(max_processes) as pool:
        result = pool.map(
            create_nuts_map, [(i, j) for i in range(824) for j in range(848)]
        )

    result = np.asarray(result).reshape((824, 848))
    np.save(nuts_matrix_path, result)
    log.info(f"created nuts matrix at {nuts_matrix_path}")


if __name__ == "__main__":
    import numpy as np

    logging.basicConfig()

    nuts_matrix_path = DATA_PATH / "nuts_matrix.npy"
    if not nuts_matrix_path.is_file():
        create_nuts_matrix(nuts_matrix_path)

    nuts_matrix = np.load(nuts_matrix_path, allow_pickle=True)

    config = load_config(DEFAULT_CONFIG_LOCATION)
    crawler = DWDCrawler("weather", config, nuts_matrix)
    crawler.create_table()
    crawler.create_hypertable_if_not_exists()

    start = datetime(1995, 1, 1, tzinfo=UTC)
    end = datetime(1995, 12, 1, tzinfo=UTC)
    crawler.crawl_from_to(start, end)
