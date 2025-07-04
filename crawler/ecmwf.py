# SPDX-FileCopyrightText: Jonathan Sejdija, Florian Maurer
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import csv
import glob
import logging
import os
from datetime import date, datetime, timedelta
from io import StringIO

import cdsapi
import geopandas as gpd
import pandas as pd
import swifter  # noqa: F401
import xarray as xr
from shapely.geometry import Point
from sqlalchemy import create_engine, text

from common.config import db_uri

"""
    Note that only requests with no more that 1000 items at a time are valid.
    See the following link for further information:
    https://confluence.ecmwf.int/pages/viewpage.action?pageId=308052947

    Also "Surface net solar radiation" got renamed to "Surface net short-wave (solar) radiation"
"""

log = logging.getLogger("ecmwf")

# path of nuts file
# downloaded from
# https://ec.europa.eu/eurostat/de/web/gisco/geodata/reference-data/administrative-units-statistical-units/nuts#nuts21
nuts_path = os.path.realpath(
    os.path.join(os.path.dirname(__file__), "shapes", "NUTS_RG_01M_2021_4326.shp")
)

# coords for europe according to:
# https://cds.climate.copernicus.eu/toolbox/doc/how-to/1_how_to_retrieve_data/1_how_to_retrieve_data.html#retrieve-a-geographical-subset-and-change-the-default-resolution
coords = [75, -15, 30, 42.5]

# requested weather variable
var_ = [
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "2m_temperature",
    "total_precipitation ",
    "surface_net_solar_radiation",
]


def create_table(engine):
    try:
        query_create_hypertable = "SELECT public.create_hypertable('ecmwf', 'time', if_not_exists => TRUE, migrate_data => TRUE);"
        query_create_hypertable_eu = "SELECT public.create_hypertable('ecmwf_eu', 'time', if_not_exists => TRUE, migrate_data => TRUE);"
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS ecmwf( "
                "time timestamp without time zone NOT NULL, "
                "temp_air double precision, "
                "ghi double precision, "
                "wind_meridional double precision, "
                "wind_zonal double precision, "
                "wind_speed double precision, "
                "precipitation double precision, "
                "latitude double precision, "
                "longitude double precision, "
                "PRIMARY KEY (time , latitude, longitude));"
            )
            conn.exec_driver_sql(query_create_hypertable)
        log.info("created hypertable ecmwf")
    except Exception as e:
        log.error(f"could not create hypertable: {e}")

    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS ecmwf_eu( "
                "time timestamp without time zone NOT NULL, "
                "temp_air double precision, "
                "ghi double precision, "
                "wind_meridional double precision, "
                "wind_zonal double precision, "
                "wind_speed double precision, "
                "precipitation double precision, "
                "latitude double precision, "
                "longitude double precision, "
                "nuts_id text, "
                "PRIMARY KEY (time , latitude, longitude, nuts_id));"
            )
            conn.exec_driver_sql(query_create_hypertable_eu)
        log.info("created hypertable ecmwf_eu")
    except Exception as e:
        log.error(f"could not create hypertable: {e}")


def save_data(request, ecmwf_client: cdsapi.Client):
    # path for downloaded files from copernicus
    save_downloaded_files_path = os.path.realpath(
        os.path.join(
            os.path.dirname(__file__),
            f"{request.get('year')}_{request.get('month')}_{request.get('day')[0]}-{request.get('month')}_{request.get('day')[len(request.get('day')) - 1]}_ecmwf.grb",
        )
    )
    ecmwf_client.retrieve("reanalysis-era5-land", request, save_downloaded_files_path)


def psql_insert_copy(table, conn, keys: list[str], data_iter):
    # gets a DBAPI connection that can provide a cursor
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


def build_dataframe(engine, request: dict, write_lat_lon: bool = True):
    file_path = os.path.realpath(
        os.path.join(
            os.path.dirname(__file__),
            f"{request.get('year')}_{request.get('month')}_{request.get('day')[0]}-{request.get('month')}_{request.get('day')[len(request.get('day')) - 1]}_ecmwf.grb",
        )
    )
    weather_data = xr.open_dataset(file_path, engine="cfgrib")
    log.info(f"successfully read file {file_path}")
    weather_data = weather_data.to_dataframe()
    weather_data = weather_data.dropna(axis=0)
    weather_data = weather_data.reset_index()
    weather_data = weather_data.drop(
        ["time", "step", "number", "surface"], axis="columns"
    )
    weather_data = weather_data.rename(
        columns={
            "valid_time": "time",
            "u10": "wind_zonal",
            "v10": "wind_meridional",
            "t2m": "temp_air",
            "ssr": "ghi",
            "tp": "precipitation",
        }
    )
    # calculate wind speed from zonal and meridional wind
    weather_data["wind_speed"] = (
        weather_data["wind_zonal"] ** 2 + weather_data["wind_meridional"] ** 2
    ) ** 0.5
    weather_data = weather_data.round({"latitude": 2, "longitude": 2})
    # columns ghi ist accumulated over 24 hours, so use difference to get hourly values
    # first we need to order by time and location and then calculate the difference
    weather_data = weather_data.sort_values(by=["latitude", "longitude", "time"])
    weather_data["ghi"] = weather_data["ghi"].diff()
    # set negatives to 0
    weather_data["ghi"] = weather_data["ghi"].clip(lower=0)
    # nan to 0
    weather_data["ghi"] = weather_data["ghi"].fillna(0)
    # set ghi at 00:00 to 0
    weather_data.loc[weather_data["time"].dt.hour == 0, "ghi"] = 0
    weather_data = weather_data.set_index(["time", "latitude", "longitude"])

    log.info("preparing to write dataframe into ecmwf database")
    # write to database
    if write_lat_lon:
        try:
            weather_data.to_sql(
                "ecmwf",
                con=engine,
                if_exists="append",
                chunksize=10000,
                method=psql_insert_copy,
            )
        except Exception:
            log.error(
                "no postgresql? - could not write using psql_insert_copy - using multi method"
            )
            weather_data.to_sql(
                "ecmwf", con=engine, if_exists="append", chunksize=10000
            )

    nuts3 = gpd.GeoDataFrame.from_file(nuts_path)
    # use only nuts_id and coordinates from nuts file so fewer columns have to be joined
    nuts3 = nuts3.loc[:, ["NUTS_ID", "geometry"]]
    nuts3 = nuts3.set_index("NUTS_ID")
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
    weather_data = weather_data.rename(columns={"index_right": "nuts_id"})
    weather_data = weather_data.dropna(axis=0)
    # calculate average for all locations inside the current nuts area
    weather_data = weather_data.groupby(["time", "nuts_id"]).mean(numeric_only=True)
    weather_data = weather_data.reset_index()
    weather_data = weather_data.set_index(["time", "latitude", "longitude", "nuts_id"])
    log.info("preparing to write nuts dataframe into ecmwf_eu database")
    try:
        weather_data.to_sql(
            "ecmwf_eu",
            con=engine,
            if_exists="append",
            chunksize=10000,
            method=psql_insert_copy,
        )
    except Exception:
        log.error(
            "no postgresql? - could not write using psql_insert_copy - using multi method"
        )
        weather_data.to_sql("ecmwf_eu", con=engine, if_exists="append", chunksize=10000)

    # Delete files locally to save space
    file_list = glob.glob(file_path + "*", recursive=True)
    for file in file_list:
        try:
            os.remove(file)
            log.info(f"removed file {file}")
        except OSError as e:
            log.info(f"Error: {e.filename} - {e.strerror}")


def get_latest_date_in_database(
    engine, start_date: datetime, end_date: datetime = None
):
    if not end_date:
        end_date = datetime.combine(date.today(), datetime.min.time())
    sql = text(
        f"select time from ecmwf where time > '{start_date}' and time < '{end_date}' order by time desc limit 1"
    )
    try:
        with engine.begin() as conn:
            last_date = pd.read_sql(sql, con=conn, parse_dates=["time"]).values[0][0]
        last_date = pd.to_datetime(str(last_date))
        last_date = pd.to_datetime(last_date.strftime("%Y-%m-%d %H:%M:%S"))
        log.info(f"Last date in database is: {last_date}")
        next_date = last_date + timedelta(hours=1)
        log.info(f"Next date to crawl: {next_date}")
        return next_date
    except Exception as e:
        log.error(e)
        return start_date


def daterange(start_date: datetime, end_date: datetime = None):
    if not end_date:
        end_date = date.today()
    for n in range(
        int((datetime.combine(end_date, datetime.min.time()) - start_date).days)
    ):
        yield start_date + timedelta(n)


def request_builder(dates: list[datetime]):
    dates_dataframe = pd.DataFrame(dates, columns=["Date"])
    g = dates_dataframe.groupby(pd.Grouper(key="Date", freq="M"))
    dfs = [group for _, group in g]
    for month in dfs:
        days = []
        for i in range(month.index.start, month.index.stop):
            days.append(f"{month['Date'].dt.day[i]:02d}")
        day_chunks = divide_month_in_chunks(days, 8)
        for chunk in day_chunks:
            request = dict(
                format="grib",
                variable=var_,
                year=f"{month['Date'].dt.year[month.index.start]}",
                month=f"{month['Date'].dt.month[month.index.start]:02d}",
                day=chunk,
                time=[f"{i:02d}:00" for i in range(24)],
            )
            request["area"] = coords

            yield request


def single_day_request(last_date: datetime):
    request = dict(
        format="grib",
        variable=var_,
        year=f"{last_date.year}",
        month=f"{last_date.month:02d}",
        day=f"{last_date.day:02d}",
        time=[f"{i:02d}:00" for i in range(last_date.hour, 24)],
    )
    request["area"] = coords
    return request


def divide_month_in_chunks(li, n):
    ch = []
    for i in range(0, len(li), n):
        ch.append(li[i : i + n])
    return ch


def main(db_uri: str):
    # initializing the client for ecmwf service
    START_DATE = datetime(2019, 1, 1)
    END_DATE = None  # use today as end
    ecmwf_client = cdsapi.Client()
    engine = create_engine(db_uri)
    create_table(engine)
    last_date = get_latest_date_in_database(
        engine, start_date=START_DATE, end_date=END_DATE
    )

    # the requests are build from 00:00 - 23:00 for each day
    # however, for recent dates the cds API delivers data up until the latest hour of the day it can deliver
    # that is why a check is necessary to first make sure that the database has dates up until 23:00
    if last_date.hour != 23:
        log.info("Creating request for single day")
        request = single_day_request(last_date)
        log.info(f"The current request running: {request}")
        save_data(request, ecmwf_client)
        build_dataframe(engine, request)
        last_date = get_latest_date_in_database(
            engine, start_date=START_DATE, end_date=END_DATE
        )

    dates = []
    for single_date in daterange(last_date):
        dates.append(single_date)

    for request in request_builder(dates):
        log.info(f"The current request running: {request}")
        save_data(request, ecmwf_client)
        build_dataframe(engine, request)


if __name__ == "__main__":
    logging.basicConfig(filename="ecmwf.log", encoding="utf-8", level=logging.INFO)
    # db_uri = 'sqlite:///./data/weather.db'
    main(db_uri("weather"))
