# SPDX-FileCopyrightText: Jonathan Sejdija, Florian Maurer
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""ECMWF / Copernicus weather crawler for OEDS.

CDS dataset: ``reanalysis-era5-single-levels`` (~0.25°).

Columns written to ``ecmwf`` / ``ecmwf_eu``
------------------------------------------
- ``ghi``: surface solar radiation downwards (hourly deaccumulated J/m²)
- ``wind_zonal``, ``wind_meridional``, ``wind_speed``: 100 m wind
- ``temp_air``: 2 m temperature
- ``precipitation``: total precipitation
"""

from __future__ import annotations

import csv
import logging
import shutil
import zipfile
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path

import cdsapi
import cfgrib
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point
from sqlalchemy import create_engine, text

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    ContinuousCrawler,
    CrawlerConfig,
    load_config,
)

##################
# IMPORTANT NOTE #
##################
# in order for this crawler to run, you need to run the 'public' crawler first
# see readme
# https://github.com/open-energy-data-server/open-energy-data-server/blob/main/README.md#using-the-ecmwf-crawler

"""
    Note that only requests with no more that 1000 items at a time are valid.
    See the following link for further information:
    https://confluence.ecmwf.int/pages/viewpage.action?pageId=308052947
"""

log = logging.getLogger("ecmwf")

TEMP_DIR = Path(__file__).parent.parent / "ecmwf_grb_files"

# Europe bbox: [N, W, S, E]
# https://web.archive.org/web/20240817225744/https://cds.climate.copernicus.eu/toolbox/doc/how-to/1_how_to_retrieve_data/1_how_to_retrieve_data.html
coords = [75, -15, 30, 42.5]

ERA5_DATASET = "reanalysis-era5-single-levels"

weather_variables = [
    "100m_u_component_of_wind",
    "100m_v_component_of_wind",
    "2m_temperature",
    "total_precipitation",
    "surface_solar_radiation_downwards",
]

# 5 variables × 24 h → max 8 days/chunk under CDS ~1000-item limit
DAY_CHUNK = 8

TEMPORAL_START = datetime(2022, 1, 1)

_TABLE_SQL = (
    "time timestamp without time zone NOT NULL, "
    "latitude double precision, "
    "longitude double precision, "
    "temp_air real, "
    "ghi real, "
    "wind_meridional real, "
    "wind_zonal real, "
    "wind_speed real, "
    "precipitation real, "
)


def create_table_if_not_exists(engine):
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS ecmwf( "
                f"{_TABLE_SQL}"
                "PRIMARY KEY (time , latitude, longitude));"
            )
        )

    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS ecmwf_eu( "
                "nuts_id text, "
                f"{_TABLE_SQL}"
                "PRIMARY KEY (time , latitude, longitude, nuts_id));"
            )
        )


def _request_stem(request: dict) -> str:
    days = request["day"]
    if isinstance(days, str):
        days = [days]
    return (
        f"{request['year']}_{request['month']}_{days[0]}-"
        f"{request['month']}_{days[-1]}_era5"
    )


def save_era5_request_to_file(request: dict, client: cdsapi.Client) -> Path:
    target = TEMP_DIR / f"{_request_stem(request)}.grib"
    client.retrieve(ERA5_DATASET, request, str(target))
    if target.exists():
        return target
    for alt in TEMP_DIR.glob(_request_stem(request) + ".*"):
        return alt
    raise FileNotFoundError(f"CDS download missing → {target}")


def psql_insert_copy(table, conn, keys: list[str], data_iter):
    with conn.connection.cursor() as cur:
        s_buf = StringIO()
        writer = csv.writer(s_buf)
        writer.writerows(data_iter)
        s_buf.seek(0)

        columns = ", ".join(f'"{k}"' for k in keys)
        if table.schema:
            table_name = f"{table.schema}.{table.name}"
        else:
            table_name = table.name

        sql = f"COPY {table_name} ({columns}) FROM STDIN WITH CSV"
        cur.copy_expert(sql=sql, file=s_buf)


def daterange(start_date: datetime, end_date: datetime = None):
    if not end_date:
        end_date = date.today()
    for n in range(
        int((datetime.combine(end_date, datetime.min.time()) - start_date).days)
    ):
        yield start_date + timedelta(days=n)


def divide_month_in_chunks(li, n):
    return [li[i : i + n] for i in range(0, len(li), n)]


def request_list_from_dates(dates: list[datetime]) -> list[dict]:
    dates_dataframe = pd.DataFrame(dates, columns=["Date"])
    dates_dataframe["Date"] = pd.to_datetime(dates_dataframe["Date"])
    grouped_by_months = dates_dataframe.groupby(pd.Grouper(key="Date", freq="ME"))
    months = [group for _, group in grouped_by_months]

    requests_list = []
    for month in months:
        days = [
            f"{month['Date'].dt.day[i]:02d}"
            for i in range(month.index.start, month.index.stop)
        ]
        for chunk in divide_month_in_chunks(days, DAY_CHUNK):
            requests_list.append(
                dict(
                    product_type="reanalysis",
                    format="grib",
                    variable=weather_variables,
                    year=f"{month['Date'].dt.year[month.index.start]}",
                    month=f"{month['Date'].dt.month[month.index.start]:02d}",
                    day=chunk,
                    time=[f"{i:02d}:00" for i in range(24)],
                    area=coords,
                )
            )
    return requests_list


def single_day_request(last_date: datetime) -> dict:
    return dict(
        product_type="reanalysis",
        format="grib",
        variable=weather_variables,
        year=f"{last_date.year}",
        month=f"{last_date.month:02d}",
        day=f"{last_date.day:02d}",
        time=[f"{i:02d}:00" for i in range(last_date.hour, 24)],
        area=coords,
    )


def _resolve_grib_path(file_path: Path) -> tuple[Path, Path | None]:
    """Return (grib_path, extract_dir_or_None)."""
    if not zipfile.is_zipfile(file_path):
        return file_path, None
    extract_dir = TEMP_DIR / (file_path.stem + ".dir")
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(file_path) as z_file:
        z_file.extractall(extract_dir)
    candidates = list(extract_dir.rglob("*.grib")) + list(extract_dir.rglob("*.grb"))
    if not candidates:
        candidates = [p for p in extract_dir.rglob("*") if p.is_file()]
    if not candidates:
        raise FileNotFoundError(f"No grib inside {file_path}")
    return candidates[0], extract_dir


def open_grib_as_dataframe(file_path: Path) -> pd.DataFrame:
    """Load ERA5 GRIB; merge cfgrib hypercubes (instant vs accumulated)."""
    grib_path, extract_dir = _resolve_grib_path(file_path)
    log.info("successfully read file %s", file_path)

    datasets = cfgrib.open_datasets(str(grib_path))
    if not datasets:
        raise FileNotFoundError(f"cfgrib found no datasets in {grib_path}")

    merged: pd.DataFrame | None = None
    for ds in datasets:
        part = ds.to_dataframe().reset_index()
        if "valid_time" in part.columns:
            if "time" in part.columns:
                part = part.drop(columns=["time"])
            part = part.rename(columns={"valid_time": "time"})
        for c in ("step", "number", "surface"):
            if c in part.columns:
                part = part.drop(columns=c)

        keys = ["time", "latitude", "longitude"]
        missing_keys = [k for k in keys if k not in part.columns]
        if missing_keys:
            raise KeyError(f"GRIB slice missing {missing_keys}: {list(part.columns)}")
        if merged is None:
            merged = part
        else:
            merged = merged.merge(part, on=keys, how="outer")

    if extract_dir is not None:
        shutil.rmtree(extract_dir, ignore_errors=True)

    assert merged is not None
    return merged.dropna(subset=["latitude", "longitude", "time"]).reset_index(drop=True)


def deaccumulate_radiation(series: pd.Series, times: pd.Series) -> pd.Series:
    """Convert CDS accumulated J/m² to hourly increments (still J/m²)."""
    out = series.diff().clip(lower=0).fillna(0)
    return out.where(times.dt.hour != 0, 0)


def process_era5_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    rename = {
        "valid_time": "time",
        "u100": "wind_zonal",
        "v100": "wind_meridional",
        "t2m": "temp_air",
        "ssrd": "ghi",
        "tp": "precipitation",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    for a, b in (("100u", "wind_zonal"), ("100v", "wind_meridional")):
        if a in df.columns and b not in df.columns:
            df = df.rename(columns={a: b})

    required = (
        "time",
        "latitude",
        "longitude",
        "ghi",
        "wind_zonal",
        "wind_meridional",
        "temp_air",
    )
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise KeyError(f"ERA5 columns missing {missing}; got {list(df.columns)}")

    df["wind_speed"] = (df["wind_zonal"] ** 2 + df["wind_meridional"] ** 2) ** 0.5
    df = df.round({"latitude": 2, "longitude": 2})
    df = df.sort_values(by=["latitude", "longitude", "time"])
    df["ghi"] = deaccumulate_radiation(df["ghi"], df["time"])
    if "precipitation" not in df.columns:
        df["precipitation"] = 0.0

    return df[
        [
            "time",
            "latitude",
            "longitude",
            "temp_air",
            "ghi",
            "wind_zonal",
            "wind_meridional",
            "wind_speed",
            "precipitation",
        ]
    ]


class EcmwfCrawler(ContinuousCrawler):
    OFFSET_FROM_NOW = timedelta(days=2)

    def __init__(self, schema_name: str, config: CrawlerConfig):
        super().__init__(schema_name, config)
        create_table_if_not_exists(self.engine)
        self.ecmwf_client = cdsapi.Client()
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

        # nuts data must have the public schema configured, otherwise
        # relation "spatial_ref_sys" does not exist
        self.nuts_data = self.get_nuts_data(config["db_uri"].format(DBNAME="public"))

    def get_nuts_data(self, db_uri: str):
        nuts_engine = create_engine(db_uri)
        try:
            query = "select nuts_id, geometry from public.nuts"
            with nuts_engine.begin() as conn:
                return gpd.read_postgis(
                    query, con=conn, geom_col="geometry", index_col="nuts_id"
                )
        except Exception as e:
            raise Exception(
                "No NUTS data available - did you run nuts_mapper before?"
            ) from e

    def get_latest_data(self) -> datetime:
        query = text("SELECT MAX(time) FROM ecmwf")
        try:
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() or TEMPORAL_START
        except Exception:
            log.error("No ecmwf latest found")
            return TEMPORAL_START

    def get_first_data(self) -> datetime:
        query = text("SELECT MIN(time) FROM ecmwf")
        try:
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() or TEMPORAL_START
        except Exception:
            log.error("No ecmwf first found")
            return TEMPORAL_START

    def create_hypertable_if_not_exists(self) -> None:
        self.create_single_hypertable_if_not_exists("ecmwf", "time")
        self.create_single_hypertable_if_not_exists("ecmwf_eu", "time")

    def crawl_from_to(self, begin: datetime, end: datetime):
        """Crawls data from begin (inclusive) until end (exclusive)."""
        if begin < TEMPORAL_START:
            begin = TEMPORAL_START

        data_available_until = datetime.now() - self.get_minimum_offset()
        if end > data_available_until:
            end = data_available_until

        last_date = self.get_latest_data()
        last_date += timedelta(hours=1)
        if last_date > begin:
            begin = last_date

        # the requests are build from 00:00 - 23:00 for each day
        # however, for recent dates the cds API delivers data up until the latest hour of the day it can deliver
        # as the time is stored without time zone, we check for 0
        if begin.hour != 0:
            log.info(
                "Creating request for single day because begin hour is %s", begin.hour
            )
            request = single_day_request(begin)
            log.info("ERA5 request: %s", request)
            path = save_era5_request_to_file(request, self.ecmwf_client)
            self.build_dataframe(request, path)
            begin = self.get_latest_data()

        dates = list(daterange(begin, end))
        for request in request_list_from_dates(dates):
            log.info("ERA5 request: %s", request)
            path = save_era5_request_to_file(request, self.ecmwf_client)
            self.build_dataframe(request, path)

    def build_dataframe(
        self,
        request: dict,
        file_path: Path | None = None,
        write_lat_lon: bool = True,
    ):
        if file_path is None:
            file_path = TEMP_DIR / f"{_request_stem(request)}.grib"

        weather_data = process_era5_dataframe(open_grib_as_dataframe(file_path))
        weather_data = weather_data.set_index(["time", "latitude", "longitude"])

        log.info("preparing to write dataframe into ecmwf database")
        if write_lat_lon:
            try:
                weather_data.to_sql(
                    "ecmwf",
                    con=self.engine,
                    if_exists="append",
                    chunksize=10000,
                    method=psql_insert_copy,
                )
            except Exception:
                log.error(
                    "no postgresql? - could not write using psql_insert_copy - using multi method"
                )
                weather_data.to_sql(
                    "ecmwf",
                    con=self.engine,
                    if_exists="append",
                    chunksize=10000,
                )

        nuts3 = self.nuts_data
        weather_data = weather_data.reset_index()
        weather_data["coords"] = list(
            zip(weather_data["longitude"], weather_data["latitude"])
        )
        weather_data["coords"] = weather_data["coords"].apply(Point)
        weather_data = gpd.GeoDataFrame(weather_data, geometry="coords", crs=nuts3.crs)
        # join weather data to nuts areas
        weather_data = gpd.sjoin(weather_data, nuts3, predicate="within", how="left")
        weather_data = pd.DataFrame(weather_data)
        # coords columns only necessary for the join
        weather_data = weather_data.drop(columns="coords")
        weather_data = weather_data.rename(columns={"NUTS_ID": "nuts_id"})
        weather_data = weather_data.dropna(axis=0)
        # average all grid points inside each NUTS area
        weather_data = weather_data.groupby(["time", "nuts_id"]).mean(numeric_only=True)
        weather_data = weather_data.reset_index()
        weather_data = weather_data.set_index(
            ["time", "latitude", "longitude", "nuts_id"]
        )
        log.info("preparing to write nuts dataframe into ecmwf_eu database")
        try:
            weather_data.to_sql(
                "ecmwf_eu",
                con=self.engine,
                if_exists="append",
                chunksize=10000,
                method=psql_insert_copy,
            )
        except Exception:
            log.error(
                "no postgresql? - could not write using psql_insert_copy - using multi method"
            )
            weather_data.to_sql(
                "ecmwf_eu",
                con=self.engine,
                if_exists="append",
                chunksize=10000,
            )

        for file in Path(file_path.parent).glob(file_path.stem + "*"):
            try:
                if file.is_dir():
                    shutil.rmtree(file)
                else:
                    file.unlink()
                log.info("removed file %s", file)
            except OSError as e:
                log.error("Error removing files: %s", e)


if __name__ == "__main__":
    logging.basicConfig(filename="ecmwf.log", encoding="utf-8", level=logging.INFO)
    config = load_config(DEFAULT_CONFIG_LOCATION)
    ecmwf_crawler = EcmwfCrawler("ecmwf", config=config)
    ecmwf_crawler.crawl_temporal()
