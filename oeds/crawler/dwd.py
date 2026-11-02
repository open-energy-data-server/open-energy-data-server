# SPDX-FileCopyrightText: Florian Maurer, Christian Rieke
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""DWD COSMO-REA6 reanalysis (6 km, hourly, 1995-01 until 2019-08).

Source: https://opendata.dwd.de/climate_environment/REA/COSMO_REA6/hourly/2D/
(parameter tables: ParameterTables_REA6.pdf in the parent directory).

The crawl starts in 2019-01, as most uses need only recent years; the config key
``dwd_start`` (e.g. ``"1995-01"``) sets an earlier first month.

The grid cells are averaged per NUTS region (all levels, the cells are assigned with
the NUTS 3 polygons). Every row of ``dwd.cosmo_rea6`` describes the hour ending at
``time`` (UTC), like the ERA5 tables:

- ``temp_air`` (K), ``wind_zonal``, ``wind_meridional``, ``wind_speed`` (m/s, 10 m),
  ``cloud_cover`` (%): instantaneous values at ``time``
- ``dhi``: diffuse radiation on the horizontal plane (ASWDIFD_S, W/m², hourly mean)
- ``dir_hor``: direct radiation on the horizontal plane (ASWDIR_S, W/m², hourly mean)
- ``ghi`` = ``dir_hor`` + ``dhi`` (W/m², hourly mean). The ``ASOB_S`` field is the net
  shortwave radiation (after the reflection of the surface), not the global radiation.
- ``dni``: direct normal radiation (W/m²), derived from ``dir_hor`` and the solar
  position at the centre of the region, NULL below 3° solar elevation
- ``rain_con``, ``rain_gsp``, ``precipitation`` (convective rain, stratiform rain and
  total precipitation including snow, kg/m² = mm of the hour)

The GRIB files hold one month each, their messages are labelled with the reference time
of the hourly forecast: instantaneous fields and the radiation means are valid at the
reference time (the radiation is the mean of the preceding hour), the precipitation of
the reference time to the next hour. The file of a month holds the reference times
01:00 of its first day until 00:00 of the next month. ``time`` is the end of the hour,
so a month is stored from 01:00 of its first day until 00:00 of the next month, and the
first precipitation hour comes from the last message of the file of the previous month.
"""

import bz2
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pygrib
import requests
from sqlalchemy import text

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    ContinuousCrawler,
    as_utc,
    copy_insert,
    load_config,
)

log = logging.getLogger("openDWD_cosmo")
log.setLevel(logging.INFO)

# open dwd data
base_url = "https://opendata.dwd.de/climate_environment/REA/COSMO_REA6/hourly/2D/"

# column: (parameter directory, accumulated over the hour)
SCALAR_PARAMETERS = {
    "temp_air": ("T_2M", False),
    "dhi": ("ASWDIFD_S", False),
    "dir_hor": ("ASWDIR_S", False),
    "cloud_cover": ("CLCT", False),
    "rain_con": ("RAIN_CON", True),
    "rain_gsp": ("RAIN_GSP", True),
    "precipitation": ("TOT_PRECIP", True),
}
WIND_PARAMETERS = ("U_10M", "V_10M")
VALUE_COLUMNS = [
    "temp_air",
    "ghi",
    "dni",
    "dhi",
    "dir_hor",
    "wind_meridional",
    "wind_zonal",
    "wind_speed",
    "rain_con",
    "rain_gsp",
    "precipitation",
    "cloud_cover",
]

geo_path = Path(__file__).parent.parent / "shapes" / "NUTS_RG_01M_2024_4326.shp"
DOWNLOAD_DIR = Path(__file__).parent.parent / "grb_files"
DATA_PATH = Path(__file__).parent.parent / "data"
NUTS_MATRIX_PATH = DATA_PATH / "nuts_matrix.npy"
# first month of the reanalysis
COSMO_START = datetime(1995, 1, 1, tzinfo=UTC)
# first month crawled by default, the config key dwd_start crawls more (e.g. "1995-01")
DEFAULT_START = datetime(2019, 1, 1, tzinfo=UTC)
# the boundary conditions ERA-Interim were discontinued after August 2019
COSMO_END = datetime(2019, 9, 1, tzinfo=UTC)
DOWNLOAD_TIMEOUT = 300
# the direct normal radiation is not derived near the horizon
MIN_SUN_ELEVATION = np.radians(3)
MAX_DNI = 1400.0


def grid_coordinates() -> tuple[np.ndarray, np.ndarray]:
    """Latitude and longitude of the 824 x 848 rotated grid."""
    return (
        np.load(DATA_PATH / "lat_coordinates.npy"),
        np.load(DATA_PATH / "lon_coordinates.npy"),
    )


def create_nuts_matrix(nuts_matrix_path: Path = NUTS_MATRIX_PATH) -> np.ndarray:
    """Assigns every grid cell to a NUTS 3 region ("x" outside of all regions)."""
    latitude, longitude = grid_coordinates()
    log.info("creating the nuts matrix")
    cells = gpd.GeoDataFrame(
        {"cell": np.arange(latitude.size)},
        geometry=gpd.points_from_xy(longitude.ravel(), latitude.ravel()),
        crs="EPSG:4326",
    )
    regions = gpd.read_file(geo_path)
    regions = regions[regions["LEVL_CODE"] == 3][["NUTS_ID", "geometry"]]
    joined = gpd.sjoin(cells, regions, predicate="within", how="inner")
    # a cell on the border of two regions is assigned to the first one
    joined = joined.drop_duplicates("cell")
    matrix = np.full(latitude.size, "x", dtype="<U5")
    matrix[joined["cell"].to_numpy()] = joined["NUTS_ID"].to_numpy()
    matrix = matrix.reshape(latitude.shape)
    np.save(nuts_matrix_path, matrix)
    log.info("created nuts matrix at %s", nuts_matrix_path)
    return matrix


def load_nuts_matrix() -> np.ndarray:
    if NUTS_MATRIX_PATH.is_file():
        return np.load(NUTS_MATRIX_PATH)
    return create_nuts_matrix()


def solar_elevation_sin(
    times: pd.DatetimeIndex, latitude: np.ndarray, longitude: np.ndarray
) -> np.ndarray:
    """Sine of the solar elevation (n times x n locations), NOAA approximation.

    The accuracy is a few hundredths of a degree, enough for the hourly mean radiation.
    """
    times = pd.DatetimeIndex(times).tz_convert("UTC")
    day = times.dayofyear.to_numpy()[:, None]
    hour = (times.hour + times.minute / 60 + times.second / 3600).to_numpy()[:, None]
    gamma = 2 * np.pi / 365 * (day - 1 + (hour - 12) / 24)
    equation_of_time = 229.18 * (
        0.000075
        + 0.001868 * np.cos(gamma)
        - 0.032077 * np.sin(gamma)
        - 0.014615 * np.cos(2 * gamma)
        - 0.040849 * np.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * np.cos(gamma)
        + 0.070257 * np.sin(gamma)
        - 0.006758 * np.cos(2 * gamma)
        + 0.000907 * np.sin(2 * gamma)
        - 0.002697 * np.cos(3 * gamma)
        + 0.00148 * np.sin(3 * gamma)
    )
    solar_time = hour * 60 + equation_of_time + 4 * longitude[None, :]
    hour_angle = np.radians(solar_time / 4 - 180)
    latitude = np.radians(latitude)[None, :]
    return np.sin(latitude) * np.sin(declination) + np.cos(latitude) * np.cos(
        declination
    ) * np.cos(hour_angle)


def direct_normal_irradiance(
    direct_horizontal: np.ndarray,
    times: pd.DatetimeIndex,
    latitude: np.ndarray,
    longitude: np.ndarray,
) -> np.ndarray:
    """Direct normal radiation from the mean direct radiation on the horizontal plane
    of the hour ending at times (n times x n locations).

    The mean of the (positive) sine of the elevation over the hour is used, so that
    the hourly mean stays consistent; below MIN_SUN_ELEVATION the result is NaN, or 0
    if there is practically no direct radiation (below 1 W/m²).
    """
    offsets = pd.to_timedelta(np.arange(-55, 1, 10), unit="min")
    sine = np.mean(
        [
            np.clip(
                solar_elevation_sin(
                    pd.DatetimeIndex(times) + offset, latitude, longitude
                ),
                0,
                None,
            )
            for offset in offsets
        ],
        axis=0,
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        dni = np.where(
            sine >= np.sin(MIN_SUN_ELEVATION), direct_horizontal / sine, np.nan
        )
    dni = np.where((direct_horizontal < 1) & np.isnan(dni), 0.0, dni)
    return np.clip(dni, 0, MAX_DNI)


class Regions:
    """Averages the grid cells of the NUTS 3 matrix per NUTS region of all levels."""

    def __init__(self, nuts_matrix: np.ndarray):
        latitude, longitude = grid_coordinates()
        self.cells = nuts_matrix != "x"
        nuts3 = nuts_matrix[self.cells]
        cell_latitude = latitude[self.cells]
        cell_longitude = longitude[self.cells]
        ids, latitudes, longitudes = [], [], []
        # per level (NUTS 0 to 3) the index of the region of each cell
        self.indices = []
        for length in (2, 3, 4, 5):
            prefixes, inverse = np.unique(
                np.strings.slice(nuts3, 0, length), return_inverse=True
            )
            inverse = inverse.reshape(-1)
            counts = np.bincount(inverse)
            ids.append(prefixes)
            latitudes.append(np.bincount(inverse, weights=cell_latitude) / counts)
            longitudes.append(np.bincount(inverse, weights=cell_longitude) / counts)
            self.indices.append(inverse)
        self.sizes = [len(i) for i in ids]
        self.ids = np.concatenate(ids)
        # centre of the cells, used for the solar position
        self.latitude = np.concatenate(latitudes)
        self.longitude = np.concatenate(longitudes)
        self.countries = np.strings.slice(self.ids, 0, 2)

    def average(self, grid: np.ndarray) -> np.ndarray:
        """Mean of the grid cells per region (NaN cells are ignored)."""
        values = np.ma.filled(np.ma.asarray(grid, dtype=float), np.nan)[self.cells]
        valid = ~np.isnan(values)
        filled = np.where(valid, values, 0.0)
        result = []
        for inverse, size in zip(self.indices, self.sizes):
            sums = np.bincount(inverse, weights=filled, minlength=size)
            counts = np.bincount(inverse, weights=valid, minlength=size)
            with np.errstate(divide="ignore", invalid="ignore"):
                result.append(sums / counts)
        return np.concatenate(result)


class DWDCrawler(ContinuousCrawler):
    """Hourly COSMO-REA6 per NUTS region, see the module docstring."""

    SCHEMA = "dwd"
    TABLE = "cosmo_rea6"

    def __init__(self, schema_name, config, nuts_matrix=None):
        super().__init__(self.SCHEMA, config)
        if nuts_matrix is None:
            nuts_matrix = load_nuts_matrix()
        self.regions = Regions(nuts_matrix)
        self.create_table()

    def create_table(self):
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {self.TABLE}( "
                    "time timestamptz NOT NULL, "
                    "nuts text, "
                    "country text, "
                    "temp_air double precision, "
                    "ghi double precision, "
                    "dni double precision, "
                    "dhi double precision, "
                    "dir_hor double precision, "
                    "wind_meridional double precision, "
                    "wind_zonal double precision, "
                    "wind_speed double precision, "
                    "rain_con double precision, "
                    "rain_gsp double precision, "
                    "precipitation double precision, "
                    "cloud_cover double precision, "
                    "PRIMARY KEY (time , nuts));"
                )
            )

    def create_hypertable_if_not_exists(self) -> None:
        self.create_single_hypertable_if_not_exists(self.TABLE, "time")

    @staticmethod
    def _path(directory: str, year: int, month: int) -> Path:
        return DOWNLOAD_DIR / f"{directory}.{year}{month:02d}.grb"

    def _download_data(self, directory: str, year: int, month: int) -> Path:
        response = requests.get(
            f"{base_url}{directory}/{directory}.2D.{year}{month:02d}.grb.bz2",
            timeout=DOWNLOAD_TIMEOUT,
        )
        response.raise_for_status()
        log.info(
            "get weather for %s with status code %s", directory, response.status_code
        )

        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        path = self._path(directory, year, month)
        path.write_bytes(bz2.decompress(response.content))
        return path

    def _delete_data(self, directory: str, year: int, month: int) -> None:
        path = self._path(directory, year, month)
        if path.is_file():
            path.unlink()

    @staticmethod
    def _messages(path: Path) -> Iterator[tuple[pd.Timestamp, np.ndarray]]:
        """Time at the end of the hour and grid of each message."""
        grbs = pygrib.open(str(path))
        try:
            for message in grbs:
                yield _message_time(message), message.values
        finally:
            grbs.close()

    def _last_message(self, path: Path) -> tuple[pd.Timestamp, np.ndarray]:
        grbs = pygrib.open(str(path))
        try:
            message = grbs.message(grbs.messages)
            return _message_time(message), message.values
        finally:
            grbs.close()

    def _read_month(self, year: int, month: int) -> pd.DataFrame:
        try:
            return self._read_month_files(year, month)
        finally:
            # a failure leaves downloaded parameters of the month behind
            previous = pd.Timestamp(year=year, month=month, day=1) - pd.DateOffset(
                months=1
            )
            for directory in [d for d, _ in SCALAR_PARAMETERS.values()] + list(
                WIND_PARAMETERS
            ):
                self._delete_data(directory, year, month)
                self._delete_data(directory, previous.year, previous.month)

    def _read_month_files(self, year: int, month: int) -> pd.DataFrame:
        """Region averages of a month: hours (first, last], one row per hour and region.

        Downloads each parameter, reads it and deletes it again.
        """
        first = pd.Timestamp(year=year, month=month, day=1, tz="UTC")
        last = first + pd.DateOffset(months=1)
        hours = pd.date_range(first + pd.Timedelta(hours=1), last, freq="h")
        regions = self.regions
        columns: dict[str, np.ndarray] = {}

        def collect(series: dict[pd.Timestamp, np.ndarray]) -> np.ndarray:
            missing = [h for h in hours if h not in series]
            if missing == [hours[0]] and first == pd.Timestamp(COSMO_START):
                # no previous month: the precipitation of the first hour is unknown
                series[hours[0]] = np.full(len(regions.ids), np.nan)
                missing = []
            if missing:
                raise ValueError(
                    f"{len(missing)} hours missing in {year}-{month:02d}, first {missing[0]}"
                )
            return np.stack([series[h] for h in hours])

        for column, (directory, accumulated) in SCALAR_PARAMETERS.items():
            self._download_data(directory, year, month)
            try:
                series = {
                    time: regions.average(grid)
                    for time, grid in self._messages(self._path(directory, year, month))
                    if first < time <= last
                }
                if accumulated:
                    # the first hour is the last message of the previous month
                    series.update(
                        self._previous_hour(directory, year, month, regions, first)
                    )
            finally:
                self._delete_data(directory, year, month)
            columns[column] = collect(series)

        wind: dict[pd.Timestamp, tuple[np.ndarray, ...]] = {}
        for directory in WIND_PARAMETERS:
            self._download_data(directory, year, month)
        try:
            u_path, v_path = (self._path(d, year, month) for d in WIND_PARAMETERS)
            for (time, u), (time_v, v) in zip(
                self._messages(u_path), self._messages(v_path), strict=True
            ):
                if time != time_v:
                    raise ValueError(f"wind components differ at {time} and {time_v}")
                if first < time <= last:
                    speed = np.hypot(np.ma.filled(u, np.nan), np.ma.filled(v, np.nan))
                    wind[time] = (
                        regions.average(u),
                        regions.average(v),
                        regions.average(speed),
                    )
        finally:
            for directory in WIND_PARAMETERS:
                self._delete_data(directory, year, month)
        for i, column in enumerate(("wind_zonal", "wind_meridional", "wind_speed")):
            columns[column] = collect({t: values[i] for t, values in wind.items()})

        columns["ghi"] = columns["dir_hor"] + columns["dhi"]
        columns["dni"] = direct_normal_irradiance(
            columns["dir_hor"], hours, regions.latitude, regions.longitude
        )

        n_regions = len(regions.ids)
        frame = pd.DataFrame(
            {
                "time": np.repeat(hours.to_numpy(), n_regions),
                "nuts": np.tile(regions.ids, len(hours)),
                "country": np.tile(regions.countries, len(hours)),
            }
        )
        for column in VALUE_COLUMNS:
            frame[column] = columns[column].reshape(-1)
        frame["time"] = pd.to_datetime(frame["time"], utc=True)
        return frame

    def _previous_hour(self, directory, year, month, regions, first):
        """Accumulated value of the hour ending at the first hour of the month."""
        previous = first - pd.DateOffset(months=1)
        if previous < pd.Timestamp(COSMO_START):
            return {}
        try:
            path = self._download_data(directory, previous.year, previous.month)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return {}
            raise
        try:
            time, grid = self._last_message(path)
        finally:
            self._delete_data(directory, previous.year, previous.month)
        if time != first + pd.Timedelta(hours=1):
            raise ValueError(f"{directory} of {previous:%Y-%m} ends at {time}")
        return {time: regions.average(grid)}

    @property
    def start(self) -> pd.Timestamp:
        """First month to crawl: dwd_start of the config, within the reanalysis."""
        start = as_utc(self.config.get("dwd_start") or DEFAULT_START)
        start = start.tz_localize(None).to_period("M").to_timestamp().tz_localize("UTC")
        return min(max(start, as_utc(COSMO_START)), as_utc(COSMO_END))

    def get_latest_data(self) -> datetime:
        with self.engine.begin() as conn:
            latest = conn.execute(text(f"SELECT max(time) FROM {self.TABLE}")).scalar()
        return as_utc(latest) if latest else self.start

    def get_first_data(self) -> datetime:
        with self.engine.begin() as conn:
            first = conn.execute(text(f"SELECT min(time) FROM {self.TABLE}")).scalar()
        return as_utc(first) if first else self.start

    def crawl_from_to(self, begin: datetime, end: datetime):
        """Crawls the months from the month of begin until end (exclusive).

        A month is stored from 01:00 of its first day until 00:00 of the next month, in
        one transaction. Resuming from the latest time therefore starts with the next
        month, and a month which is crawled again is replaced.
        """
        begin = max(as_utc(begin), self.start)
        end = min(as_utc(end), as_utc(COSMO_END))
        months = pd.date_range(
            start=begin.tz_localize(None)
            .to_period("M")
            .to_timestamp()
            .tz_localize("UTC"),
            end=end - pd.Timedelta(seconds=1),
            freq="MS",
        )
        for date in months:
            try:
                df = self._read_month(date.year, date.month)
            except Exception:
                # the months until COSMO_END exist: stop here, resuming from
                # max(time) retries this month instead of leaving a gap
                log.exception("could not read %s", date)
                raise
            log.info("built data for %s and start import to db", date.strftime("%Y-%m"))
            with self.engine.begin() as conn:
                # data is stored per month: replace it if it exists
                conn.execute(
                    text(
                        f"DELETE FROM {self.TABLE} WHERE time > :first AND time <= :last"
                    ),
                    {
                        "first": date.to_pydatetime(),
                        "last": (date + pd.DateOffset(months=1)).to_pydatetime(),
                    },
                )
                df.to_sql(
                    self.TABLE,
                    con=conn,
                    if_exists="append",
                    index=False,
                    chunksize=100000,
                    method=copy_insert,
                )
            log.info("import of %s complete", date.strftime("%Y-%m"))


def _message_time(message) -> pd.Timestamp:
    """Time of a message in UTC: the instant, or the end of the hour of means and sums."""
    return pd.Timestamp(
        f"{message['validityDate']:08d} {message['validityTime']:04d}", tz="UTC"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    config = load_config(DEFAULT_CONFIG_LOCATION)
    crawler = DWDCrawler("dwd", config)
    crawler.crawl_temporal(end=COSMO_END)
    crawler.create_hypertable_if_not_exists()
