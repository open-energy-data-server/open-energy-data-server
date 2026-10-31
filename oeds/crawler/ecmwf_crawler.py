# SPDX-FileCopyrightText: Jonathan Sejdija, Florian Maurer
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""ECMWF / Copernicus weather crawler for OEDS.

Two CDS datasets are crawled, each by its own crawler and into its own tables of the
schema ``ecmwf``, so that they never mix:

- ``ecmwf`` (``EcmwfCrawler``): ``reanalysis-era5-single-levels``, ~0.25°, land and
  sea, 100 m wind, tables ``era5`` (grid) and ``era5_eu`` (NUTS regions)
- ``ecmwf_land`` (``EcmwfLandCrawler``): ``reanalysis-era5-land``, ~0.1°, land only,
  10 m wind, tables ``era5_land`` and ``era5_land_eu``

Columns written to the grid and NUTS tables
-------------------------------------------
- ``ghi``: surface solar radiation downwards of the past hour (J/m²)
- ``wind_zonal``, ``wind_meridional``, ``wind_speed``: wind (m/s)
- ``temp_air``: 2 m temperature (K)
- ``precipitation``: total precipitation of the past hour (m)
"""

from __future__ import annotations

import logging
import shutil
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import cdsapi
import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point
from sqlalchemy import create_engine, text

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    ContinuousCrawler,
    CrawlerConfig,
    as_utc,
    copy_insert,
    load_config,
    utcnow,
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

# ERA5 single levels holds radiation and precipitation of the past hour, ERA5-Land
# accumulates them since 00 UTC (the value at 00 UTC is the sum of the previous day)
DATASETS = {
    "era5": dict(
        name="reanalysis-era5-single-levels",
        table="era5",
        table_eu="era5_eu",
        start=datetime(2019, 1, 1, tzinfo=UTC),
        accumulated=False,
        variables=[
            "100m_u_component_of_wind",
            "100m_v_component_of_wind",
            "2m_temperature",
            "total_precipitation",
            "surface_solar_radiation_downwards",
        ],
    ),
    "era5-land": dict(
        name="reanalysis-era5-land",
        table="era5_land",
        table_eu="era5_land_eu",
        # 00 UTC closes the previous day: the hourly value of 2019-01-01 00 UTC needs
        # the day before, so that 2019 is complete
        start=datetime(2018, 12, 31, tzinfo=UTC),
        accumulated=True,
        variables=[
            "10m_u_component_of_wind",
            "10m_v_component_of_wind",
            "2m_temperature",
            "total_precipitation",
            "surface_solar_radiation_downwards",
        ],
    ),
}
ACCUMULATED_COLUMNS = ["ghi", "precipitation"]

# 5 variables × 24 h → max 8 days/chunk under CDS ~1000-item limit
DAY_CHUNK = 8

SCHEMA = "ecmwf"

_TABLE_SQL = (
    "time timestamptz NOT NULL, "
    "latitude double precision, "
    "longitude double precision, "
    "temp_air real, "
    "ghi real, "
    "wind_meridional real, "
    "wind_zonal real, "
    "wind_speed real, "
    "precipitation real, "
)


def create_table_if_not_exists(engine, table: str, table_eu: str):
    with engine.begin() as conn:
        conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {table}( "
                f"{_TABLE_SQL}"
                "PRIMARY KEY (time , latitude, longitude));"
            )
        )

    with engine.begin() as conn:
        conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {table_eu}( "
                "nuts_id text, "
                f"{_TABLE_SQL}"
                "PRIMARY KEY (time , latitude, longitude, nuts_id));"
            )
        )


def _request_stem(request: dict, dataset: str = "era5") -> str:
    days = request["day"]
    if isinstance(days, str):
        days = [days]
    return (
        f"{request['year']}_{request['month']}_{days[0]}-"
        f"{request['month']}_{days[-1]}_{dataset}"
    )


def save_era5_request_to_file(
    request: dict, client: cdsapi.Client, dataset: str = "era5"
) -> Path:
    target = TEMP_DIR / f"{_request_stem(request, dataset)}.grib"
    client.retrieve(DATASETS[dataset]["name"], request, str(target))
    if target.exists():
        return target
    for alt in TEMP_DIR.glob(_request_stem(request, dataset) + ".*"):
        return alt
    raise FileNotFoundError(f"CDS download missing → {target}")


def daterange(start_date: datetime, end_date: datetime = None):
    if not end_date:
        end_date = date.today()
    end = datetime.combine(end_date, datetime.min.time(), tzinfo=start_date.tzinfo)
    for n in range(int((end - start_date).days)):
        yield start_date + timedelta(days=n)


def divide_month_in_chunks(li, n):
    return [li[i : i + n] for i in range(0, len(li), n)]


def base_request(dataset: str) -> dict:
    request = dict(
        data_format="grib",
        download_format="unarchived",
        variable=DATASETS[dataset]["variables"],
        area=coords,
    )
    if dataset == "era5":
        request["product_type"] = "reanalysis"
    return request


def request_list_from_dates(dates: list[datetime], dataset: str = "era5") -> list[dict]:
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
                    base_request(dataset),
                    year=f"{month['Date'].dt.year[month.index.start]}",
                    month=f"{month['Date'].dt.month[month.index.start]:02d}",
                    day=chunk,
                    time=[f"{i:02d}:00" for i in range(24)],
                )
            )
    return requests_list


def single_day_request(last_date: datetime, dataset: str = "era5") -> dict:
    return dict(
        base_request(dataset),
        year=f"{last_date.year}",
        month=f"{last_date.month:02d}",
        day=f"{last_date.day:02d}",
        time=[f"{i:02d}:00" for i in range(last_date.hour, 24)],
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
    import cfgrib

    grib_path, extract_dir = _resolve_grib_path(file_path)
    log.info("successfully read file %s", file_path)

    datasets = cfgrib.open_datasets(str(grib_path))
    if not datasets:
        raise FileNotFoundError(f"cfgrib found no datasets in {grib_path}")
    merged = datasets_to_dataframe(datasets)

    if extract_dir is not None:
        shutil.rmtree(extract_dir, ignore_errors=True)
    return merged


def datasets_to_dataframe(datasets: list) -> pd.DataFrame:
    """Merges the cfgrib datasets to one frame with the valid time as time."""
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
        # the cube of base time x step of accumulated variables holds valid times
        # which were not requested (e.g. the hours before and after the request), and
        # ERA5-Land has no values on the sea: these rows have no data at all
        part = part.dropna(subset=[c for c in part.columns if c not in keys], how="all")
        if merged is None:
            merged = part
        else:
            merged = merged.merge(part, on=keys, how="outer")

    assert merged is not None
    return merged.dropna(subset=["latitude", "longitude", "time"]).reset_index(
        drop=True
    )


def deaccumulate(
    values: pd.Series,
    times: pd.Series,
    latitudes: pd.Series,
    longitudes: pd.Series,
    prior: pd.Series | None = None,
) -> pd.Series:
    """Converts values accumulated since 00 UTC (ERA5-Land) to the sum of the past hour.

    The value at 01 UTC is already hourly, every other hour is the difference to the
    previous hour, including 00 UTC which closes the previous day. The first row of
    each cell needs the accumulated value before it, prior (indexed by latitude and
    longitude), otherwise it is NaN. A row whose previous row is missing, is not the
    hour before or has no value is NaN too, as the difference would span several
    hours. The data must be sorted by cell and time.
    """
    cells = [latitudes, longitudes]
    grouped = values.groupby(cells, sort=False)
    previous = grouped.shift(1)
    gap = times - times.groupby(cells, sort=False).shift(1) != pd.Timedelta(hours=1)
    previous = previous.where(~gap, np.nan)
    first = grouped.cumcount() == 0
    if prior is not None:
        index = pd.MultiIndex.from_arrays([latitudes, longitudes])
        previous = previous.where(
            ~first, prior.reindex(index).to_numpy(dtype="float64")
        )
    hourly = (values - previous).where(times.dt.hour != 1, values)
    return hourly.clip(lower=0)


def process_era5_dataframe(
    df: pd.DataFrame, accumulated: bool = False, prior: pd.DataFrame | None = None
) -> pd.DataFrame:
    """prior: for accumulated data, the values accumulated before the first hour of
    each cell (index latitude/longitude, columns ACCUMULATED_COLUMNS)."""
    rename = {
        "valid_time": "time",
        "u100": "wind_zonal",
        "v100": "wind_meridional",
        "u10": "wind_zonal",
        "v10": "wind_meridional",
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

    # cfgrib times are naive and refer to UTC
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df["wind_speed"] = (df["wind_zonal"] ** 2 + df["wind_meridional"] ** 2) ** 0.5
    df = df.round({"latitude": 2, "longitude": 2})
    df = df.sort_values(by=["latitude", "longitude", "time"]).reset_index(drop=True)
    if "precipitation" not in df.columns:
        df["precipitation"] = 0.0
    if accumulated:
        for col in ACCUMULATED_COLUMNS:
            df[col] = deaccumulate(
                df[col],
                df["time"],
                df["latitude"],
                df["longitude"],
                None if prior is None else prior[col],
            )

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
    """ERA5 single levels, see the module docstring for ERA5-Land."""

    # ERA5T and ERA5-Land are published about five days after real time
    OFFSET_FROM_NOW = timedelta(days=6)
    dataset = "era5"

    def __init__(self, schema_name: str, config: CrawlerConfig):
        # all datasets share one schema, whatever name the crawler is registered with
        super().__init__(SCHEMA, config)
        self.table = DATASETS[self.dataset]["table"]
        self.table_eu = DATASETS[self.dataset]["table_eu"]
        self.temporal_start = DATASETS[self.dataset]["start"]
        create_table_if_not_exists(self.engine, self.table, self.table_eu)
        self.ecmwf_client = cdsapi.Client()
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

        # nuts data must have the public schema configured, otherwise
        # relation "spatial_ref_sys" does not exist
        self.nuts_data = self.get_nuts_data(
            self.config["db_uri"].format(DBNAME="public")
        )

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
        # the grid and the NUTS table are written together, but may differ after a crash
        try:
            with self.engine.connect() as conn:
                latest = [
                    conn.execute(text(f"SELECT MAX(time) FROM {table}")).scalar()
                    for table in (self.table, self.table_eu)
                ]
            if None in latest:
                return self.temporal_start
            return as_utc(min(latest))
        except Exception:
            log.error("No ecmwf latest found")
            return self.temporal_start

    def get_first_data(self) -> datetime:
        query = text(f"SELECT MIN(time) FROM {self.table}")
        try:
            with self.engine.connect() as conn:
                return as_utc(conn.execute(query).scalar() or self.temporal_start)
        except Exception:
            log.error("No ecmwf first found")
            return self.temporal_start

    def accumulated_before(self, first: pd.Timestamp) -> pd.DataFrame:
        """Sums the stored hourly values of the accumulation window before first.

        ERA5-Land accumulates since 00 UTC (window 01 UTC to 00 UTC of the next day), so
        the value accumulated before first is the sum of the stored hours since 01 UTC.
        """
        window_start = (first - timedelta(hours=1)).floor("D") + timedelta(hours=1)
        if first <= window_start:
            # first is 01 UTC, its value is already hourly
            return pd.DataFrame(columns=ACCUMULATED_COLUMNS, dtype="float64")
        sums = ", ".join(f"sum({c}) as {c}" for c in ACCUMULATED_COLUMNS)
        query = text(
            f"SELECT latitude, longitude, {sums} FROM {self.table} "
            "WHERE time >= :lo AND time < :hi GROUP BY latitude, longitude"
        )
        with self.engine.connect() as conn:
            prior = pd.read_sql(
                query,
                conn,
                params={
                    "lo": window_start.to_pydatetime(),
                    "hi": first.to_pydatetime(),
                },
            )
        hours = int((first - window_start) / timedelta(hours=1))
        log.info("accumulated before %s from %s stored hours", first, hours)
        return prior.round({"latitude": 2, "longitude": 2}).set_index(
            ["latitude", "longitude"]
        )

    def create_hypertable_if_not_exists(self) -> None:
        self.create_single_hypertable_if_not_exists(self.table, "time")
        self.create_single_hypertable_if_not_exists(self.table_eu, "time")

    def crawl_from_to(self, begin: datetime, end: datetime):
        """Crawls data from begin (inclusive) until end (exclusive)."""
        if begin < self.temporal_start:
            begin = self.temporal_start

        data_available_until = utcnow() - self.get_minimum_offset()
        if end > data_available_until:
            end = data_available_until

        # only skip what is already stored when the range reaches into it; an
        # older begin (backfill before the first row) is honoured
        last_date = self.get_latest_data() + timedelta(hours=1)
        if begin >= self.get_first_data() and last_date > begin:
            begin = last_date
        if begin >= end:
            return
        self.crawl_range(begin, end)

    def crawl_temporal(
        self, begin: datetime | None = None, end: datetime | None = None
    ):
        if begin:
            # a backfill: also fill the gaps between the stored data
            self.crawl_missing(begin, end)
        super().crawl_temporal(begin, end)

    def stored_hours_by_month(self, begin: datetime, end: datetime) -> dict:
        """Number of hours with values per month of [begin, end), by month start (UTC).

        The NUTS table is much smaller than the grid table. Both are written in one
        transaction, so a complete month in the NUTS table is complete in the grid
        table as well. One query for the whole range.
        """
        query = text(
            "SELECT date_trunc('month', time AT TIME ZONE 'UTC') AS month, "
            "COUNT(DISTINCT time) FILTER (WHERE ghi IS NOT NULL) AS hours "
            f"FROM {self.table_eu} WHERE time >= :begin AND time < :end GROUP BY 1"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(query, {"begin": begin, "end": end}).all()
        return {pd.Timestamp(month).tz_localize("UTC"): hours for month, hours in rows}

    def crawl_missing(self, begin: datetime | None = None, end: datetime | None = None):
        """Crawls the months which have hours missing, also between stored data.

        Resumes an interrupted backfill: a month with all hours is not requested again.
        """
        begin = max(as_utc(begin), as_utc(self.temporal_start)) if begin else None
        begin = begin or as_utc(self.temporal_start)
        end = as_utc(end) if end else utcnow()
        # the CDS requests are made for whole days
        end = as_utc(min(end, utcnow() - self.get_minimum_offset())).floor("D")
        if begin >= end:
            return
        stored = self.stored_hours_by_month(begin, end)
        for month in pd.date_range(
            begin.floor("D").replace(day=1), end, freq="MS", inclusive="left"
        ):
            first = max(month, begin)
            last = min(month + pd.DateOffset(months=1), end)
            expected = int((last - first) / timedelta(hours=1))
            if first == self.temporal_start and DATASETS[self.dataset]["accumulated"]:
                # the first hour of accumulated data can not be converted
                expected -= 1
            if expected <= 0 or stored.get(month, 0) >= expected:
                continue
            log.info("hours missing in %s, crawling %s to %s", month, first, last)
            self.crawl_range(first, last)

    def crawl_range(self, begin: datetime, end: datetime):
        """Requests and stores the days from begin until end (exclusive)."""
        # the requests are build from 00:00 - 23:00 for each day
        # however, for recent dates the cds API delivers data up until the latest hour of the day it can deliver
        # the hour is UTC, we check for 0
        if begin.hour != 0:
            log.info(
                "Creating request for single day because begin hour is %s", begin.hour
            )
            request = single_day_request(begin, self.dataset)
            log.info("ERA5 request: %s", request)
            path = save_era5_request_to_file(request, self.ecmwf_client, self.dataset)
            self.build_dataframe(request, path)
            # the rest of the day was just stored, continue with the next day
            begin = (as_utc(begin) + timedelta(days=1)).floor("D")

        dates = list(daterange(begin, end))
        for request in request_list_from_dates(dates, self.dataset):
            log.info("ERA5 request: %s", request)
            path = save_era5_request_to_file(request, self.ecmwf_client, self.dataset)
            self.build_dataframe(request, path)

    def build_dataframe(
        self,
        request: dict,
        file_path: Path | None = None,
        write_lat_lon: bool = True,
    ):
        if file_path is None:
            file_path = TEMP_DIR / f"{_request_stem(request, self.dataset)}.grib"

        raw = open_grib_as_dataframe(file_path)
        accumulated = DATASETS[self.dataset]["accumulated"]
        prior = None
        if accumulated:
            prior = self.accumulated_before(pd.to_datetime(raw["time"], utc=True).min())
        weather_data = process_era5_dataframe(raw, accumulated, prior)
        weather_data = weather_data.set_index(["time", "latitude", "longitude"])

        grid_data = weather_data

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
        # points outside of all NUTS areas
        weather_data = weather_data.dropna(subset=["nuts_id"])
        # average all grid points inside each NUTS area
        weather_data = weather_data.groupby(["time", "nuts_id"]).mean(numeric_only=True)
        weather_data = weather_data.reset_index()
        weather_data = weather_data.set_index(
            ["time", "latitude", "longitude", "nuts_id"]
        )
        # both tables are written in one transaction, so resuming from the
        # latest time stays consistent
        log.info("preparing to write %s and %s", self.table, self.table_eu)
        with self.engine.begin() as conn:
            if write_lat_lon:
                self.replace_overlap(
                    conn,
                    self.table,
                    grid_data,
                    "time",
                    chunksize=10000,
                    method=copy_insert,
                )
            self.replace_overlap(
                conn,
                self.table_eu,
                weather_data,
                "time",
                chunksize=10000,
                method=copy_insert,
            )

        for file in Path(file_path.parent).glob(file_path.stem + ".*"):
            try:
                if file.is_dir():
                    shutil.rmtree(file)
                else:
                    file.unlink()
                log.info("removed file %s", file)
            except OSError as e:
                log.error("Error removing files: %s", e)


class EcmwfLandCrawler(EcmwfCrawler):
    """ERA5-Land, accumulations since 00 UTC are converted to hourly values."""

    dataset = "era5-land"


if __name__ == "__main__":
    logging.basicConfig(filename="ecmwf.log", encoding="utf-8", level=logging.INFO)
    config = load_config(DEFAULT_CONFIG_LOCATION)
    ecmwf_crawler = EcmwfCrawler("ecmwf", config=config)
    ecmwf_crawler.crawl_temporal()
