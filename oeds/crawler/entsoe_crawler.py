#!/usr/bin/env python3
# SPDX-FileCopyrightText: Florian Maurer
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
This crawler downloads all the data of the ENTSO-E transparency platform.
The resulting data is not available under an open-source license and should not be reshared but is available for crawling yourself.
You can get an API Token here: https://transparencyplatform.zendesk.com/hc/en-us/articles/12845911031188-How-to-get-security-token
More information can be found here: https://transparencyplatform.zendesk.com/hc/en-us/articles/15692855254548-Sitemap-for-Restful-API-Integration

Rate Limit information found here: https://transparencyplatform.zendesk.com/hc/en-us/articles/12783148966036-API-Rate-Limit-Part-1
Primary Limit: 400 requests per minute per user account (API token).

Optional config keys (or environment variables):
- entsoe_countries (ENTSOE_COUNTRIES): list or comma-separated string of area names to crawl, e.g. "DE_LU,DE_AT_LU,DE" (default: all areas)
- entsoe_workers (ENTSOE_WORKERS): number of parallel requests (default: 8)

Each area (and each crossborder pair) is resumed from where its last successful crawl ended,
which is stored in the crawl_state table. So areas which are added later are crawled from the beginning.
"""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from inspect import signature

import pandas as pd
import requests
from entsoe import EntsoePandasClient
from entsoe.exceptions import InvalidBusinessParameterError, NoMatchingDataError
from entsoe.mappings import NEIGHBOURS, PSRTYPE_MAPPINGS, Area
from requests.adapters import HTTPAdapter
from requests.exceptions import HTTPError
from sqlalchemy import inspect, text
from tqdm import tqdm

from oeds.base_crawler import DEFAULT_CONFIG_LOCATION, ContinuousCrawler, load_config

log = logging.getLogger("entsoe")
log.setLevel(logging.INFO)

metadata_info = {
    "schema_name": "entsoe",
    "data_source": "https://data.open-power-system-data.org/conventional_power_plants/latest/conventional_power_plants_EU.csv",
    "license": "CC-BY-4.0.",
    "description": "ENTSOE transparency energy. Country specific load and generation of energy sources.",
    "contact": "",
    "temporal_start": "2014-12-31 22:00:00",
    "temporal_end": "2024-05-07 12:30:00",
}

from_n = []
to_n = []

for n1 in NEIGHBOURS:
    for n2 in NEIGHBOURS[n1]:
        from_n.append(n1)
        to_n.append(n2)
neighbours = pd.DataFrame({"from": from_n, "to": to_n})

all_countries = [e.name for e in Area]

DEFAULT_WORKERS = 8
# seconds to wait for the server, entsoe-py waits forever by default
REQUEST_TIMEOUT = 300
MAX_ATTEMPTS = 5
DEFAULT_START = pd.Timestamp("20150101", tz="UTC")
STATE_TABLE = "crawl_state"


def to_utc(ts):
    """converts a timestamp from the database or the user to a tz-aware UTC pd.Timestamp"""
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def time_chunks(start, end, freq):
    """
    splits [start, end) into consecutive intervals at the boundaries of freq (e.g. "YS", "MS").
    Returns a single interval if freq is None.
    """
    if freq is None:
        return [(start, end)]
    bounds = pd.date_range(start, end, freq=freq, inclusive="neither")
    edges = [start, *bounds, end]
    return list(zip(edges[:-1], edges[1:]))


def sanitize_series(seriesname):
    """
    replaces illegal values from a series name
    for insertion into database

    Parameters
    ----------
    seriesname : str
        name of the series

    Returns
    -------

    st : str

    """

    st = str.replace(str(seriesname), ")", "")
    st = str.replace(st, "(", "")
    st = str.replace(st, ",", "")
    st = str.replace(st, "'", "")
    st = st.strip()
    st = str.replace(st, " ", "_")
    if st == "0":
        st = "value"
    return st


def query_kwargs(proc) -> dict:
    """Generation is condensed into one nett value per production type
    (aggregated minus consumption, e.g. for pumped storage) by entsoe-py."""
    if "nett" in signature(proc).parameters:
        return {"nett": True}
    return {}


class EntsoeCrawler(ContinuousCrawler):
    """
    class to allow easier crawling of ENTSO-E timeseries data
    """

    # data in the last two days might not be available yet, so we wait for it.
    OFFSET_FROM_NOW = timedelta(days=2)

    def __init__(self, schema_name, config):
        super().__init__(schema_name, config)

        countries = self.config.get("entsoe_countries") or os.getenv("ENTSOE_COUNTRIES")
        if isinstance(countries, str):
            countries = [c.strip() for c in countries.split(",") if c.strip()]
        self.countries = countries or all_countries
        unknown = set(self.countries) - set(all_countries)
        if unknown:
            raise ValueError(f"unknown ENTSO-E areas: {sorted(unknown)}")
        self.workers = int(
            self.config.get("entsoe_workers")
            or os.getenv("ENTSOE_WORKERS")
            or DEFAULT_WORKERS
        )
        # serializes DB writes, as parallel ALTER TABLE/CREATE TABLE would conflict
        self._db_lock = threading.Lock()
        session = requests.Session()
        session.mount("https://", HTTPAdapter(pool_maxsize=self.workers))
        self.client = EntsoePandasClient(
            api_key=self.config["entsoe_api_key"],
            session=session,
            timeout=REQUEST_TIMEOUT,
        )
        log.info(f"crawling {len(self.countries)} areas with {self.workers} workers")

        self.init_base_sql()
        self.create_state_table()
        self.save_power_system_data()

    def init_base_sql(self):
        """
        write static data to database once
        """
        psrtype = pd.DataFrame.from_dict(
            PSRTYPE_MAPPINGS, orient="index", columns=["prod_type"]
        )
        areas = pd.DataFrame(
            [[e.name, e.value, e.tz, e.meaning] for e in Area],
            columns=["name", "value", "tz", "meaning"],
        )
        with self.engine.begin() as conn:
            areas.columns = [x.lower() for x in areas.columns]
            psrtype.columns = [x.lower() for x in psrtype.columns]
            areas.to_sql("areas", conn, if_exists="replace")
            psrtype.to_sql("psrtype", conn, if_exists="replace")

    def create_state_table(self):
        """
        creates the table which stores until when each area of each table was crawled successfully
        """
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {STATE_TABLE} "
                    "(table_name TEXT, item TEXT, crawled_until TIMESTAMPTZ, "
                    "PRIMARY KEY (table_name, item))"
                )
            )

    def get_crawled_until(self, tablename):
        """
        returns {item: crawled_until} of the given table from the crawl state
        """
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT item, crawled_until FROM {STATE_TABLE} WHERE table_name = :t"
                ),
                {"t": tablename},
            ).fetchall()
        return {item: to_utc(until) for item, until in rows}

    def set_crawled_until(self, tablename, item, until):
        with self._db_lock, self.engine.begin() as conn:
            params = {"t": tablename, "i": item}
            conn.execute(
                text(f"DELETE FROM {STATE_TABLE} WHERE table_name = :t AND item = :i"),
                params,
            )
            conn.execute(
                text(
                    f"INSERT INTO {STATE_TABLE} (table_name, item, crawled_until) VALUES (:t, :i, :u)"
                ),
                {**params, "u": self._db_ts(until)},
            )

    def _db_ts(self, ts):
        """
        converts a timestamp into a query parameter comparable to the index written by pandas
        """
        ts = to_utc(ts)
        if self.engine.dialect.name == "sqlite":
            # pandas stores timestamps as naive UTC strings in sqlite
            return ts.strftime("%Y-%m-%d %H:%M:%S.%f")
        return ts.to_pydatetime()

    def _latest_index(self, tablename, where, params):
        with self.engine.connect() as conn:
            d = conn.execute(
                text(f'SELECT max("index") FROM "{tablename}" WHERE {where}'), params
            ).scalar()
        return None if d is None else to_utc(d)

    def crawl_ranges(
        self, tablename, items, begin=None, end=None, column_per_item=False
    ):
        """
        Find the range to crawl for each item (area or column) of a table.
        Starts where the last successful crawl of the item ended.
        Items without crawl state (crawled before it existed) start at their latest data,
        items without data at DEFAULT_START.

        Parameters
        ----------
        tablename : str
        items : list[str]
            areas, or column names if column_per_item
        begin, end :
            if both are given, they are used for all items and the crawl state is not updated
        column_per_item : bool
            whether each item is stored in its own column instead of a country column

        Returns
        -------
        ranges : dict[str, tuple[pd.Timestamp, pd.Timestamp]]
        update_state : bool
            whether the crawl state should be updated after crawling the ranges
        """
        if begin is not None and end is not None:
            return {item: (to_utc(begin), to_utc(end)) for item in items}, False

        end = pd.Timestamp.now(tz="UTC") - self.get_minimum_offset()
        state = self.get_crawled_until(tablename)
        insp = inspect(self.engine)
        columns = set()
        if insp.has_table(tablename):
            columns = {c["name"] for c in insp.get_columns(tablename)}

        ranges = {}
        for item in items:
            start = state.get(item)
            if start is None and column_per_item and item in columns:
                start = self._latest_index(tablename, f'"{item}" IS NOT NULL', {})
            elif start is None and not column_per_item and "country" in columns:
                start = self._latest_index(tablename, "country = :c", {"c": item})
            ranges[item] = (start or DEFAULT_START, end)
        return ranges, True

    def _add_missing_columns(self, conn, data, tablename):
        existing = {c["name"] for c in inspect(conn).get_columns(tablename)}
        for col in data.columns:
            if col in existing:
                continue
            if pd.api.types.is_numeric_dtype(data[col]):
                sqltype = "DOUBLE PRECISION"
            else:
                sqltype = "TEXT"
            log.info(f"adding column {col} to {tablename}")
            conn.execute(
                text(f'ALTER TABLE "{tablename}" ADD COLUMN "{col}" {sqltype}')
            )

    def _create_country_index(self, conn, tablename):
        conn.execute(
            text(
                f'CREATE INDEX IF NOT EXISTS "country_idx_{tablename}" ON "{tablename}" ("country", "index")'
            )
        )

    def write_chunk(self, data, tablename, country, start, end):
        """
        Replaces the data of country in [start, end) with data,
        so that crawling a range again does not create duplicates.
        Adds columns which do not exist in the table yet.
        """
        with self._db_lock, self.engine.begin() as conn:
            if inspect(conn).has_table(tablename):
                self._add_missing_columns(conn, data, tablename)
                conn.execute(
                    text(
                        f'DELETE FROM "{tablename}" WHERE country = :c AND "index" >= :s AND "index" < :e'
                    ),
                    {"c": country, "s": self._db_ts(start), "e": self._db_ts(end)},
                )
                data.to_sql(tablename, conn, if_exists="append")
            else:
                data.to_sql(tablename, conn, if_exists="append")
                # needed for fast deletes in the next chunks
                self._create_country_index(conn, tablename)

    def merge_into_wide_table(self, data, tablename):
        """
        Writes data into a table with one column per series.
        Rows with an existing index are updated, so that series can be added
        for time ranges which already exist without creating duplicate rows.
        """
        with self._db_lock, self.engine.begin() as conn:
            if not inspect(conn).has_table(tablename):
                data.to_sql(tablename, conn, if_exists="append")
                return
            self._add_missing_columns(conn, data, tablename)
            tmp = f"{tablename}_tmp"
            data.to_sql(tmp, conn, if_exists="replace")
            cols = [f'"{c}"' for c in data.columns]
            updates = ", ".join(
                f'{c} = COALESCE(tmp.{c}, "{tablename}".{c})' for c in cols
            )
            conn.execute(
                text(
                    f'UPDATE "{tablename}" SET {updates} FROM "{tmp}" AS tmp '
                    f'WHERE "{tablename}"."index" = tmp."index"'
                )
            )
            collist = ", ".join(['"index"', *cols])
            conn.execute(
                text(
                    f'INSERT INTO "{tablename}" ({collist}) SELECT {collist} FROM "{tmp}" AS tmp '
                    f'WHERE NOT EXISTS (SELECT 1 FROM "{tablename}" WHERE "{tablename}"."index" = tmp."index")'
                )
            )
            conn.execute(text(f'DROP TABLE "{tmp}"'))

    def query_with_retries(self, proc, *args, start, end, **kwargs):
        """
        Calls proc and retries on server and connection errors with increasing delay.
        Raises NoMatchingDataError, InvalidBusinessParameterError and HTTP 400 directly.
        """
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return proc(*args, start=start, end=end, **kwargs)
            except (NoMatchingDataError, InvalidBusinessParameterError):
                raise
            except HTTPError as e:
                if e.response is not None and e.response.status_code == 400:
                    raise
                error = e
            except Exception as e:
                error = e
            if attempt == MAX_ATTEMPTS:
                raise error
            log.info(
                f"retrying ({attempt}/{MAX_ATTEMPTS - 1}) {proc.__name__}, {args}, {start}, {end}: {repr(error)}"
            )
            time.sleep(30 * attempt)

    def fetch_and_write_entsoe_df_to_db(self, country, proc, start, end):
        """
        Crawl data from ENTSO-E transparency platform and write it to the database

        Parameters
        ----------
        country : str
            2-letter country code
        proc :
            procedure of entsoe-py client
        start : pd.Timestamp
            start time
        end : pd.Timestamp
            end time

        Returns
        -------
        success : bool
            False if the download or the write failed, True otherwise (also if there is no data)
        """
        try:
            data = pd.DataFrame(
                self.query_with_retries(
                    proc, country, start=start, end=end, **query_kwargs(proc)
                )
            )

            # entsoe-py includes the end timestamp, which is the start of the next chunk
            data = data[(data.index >= start) & (data.index < end)]

            # replace spaces and invalid chars in column names
            data.columns = [sanitize_series(x).lower() for x in data.columns]
            data = data.fillna(0)

            # add country column
            data["country"] = country
            self.write_chunk(data, proc.__name__, country, start, end)
        except (NoMatchingDataError, InvalidBusinessParameterError):
            log.error(f"no data found for {proc.__name__}, {country}, {start}, {end}")
        except Exception as e:
            log.error(
                f"error downloading {proc.__name__}, {country}, {start}, {end}: {e}"
            )
            return False
        return True

    def download_entsoe(
        self,
        countries,
        proc,
        begin=None,
        end=None,
        create_hypertable=True,
        chunk_freq="YS",
        min_days=2,
    ):
        """
        Downloads data with a procedure from a EntsoePandasClient
        and stores it in the configured database.
        Each country is resumed from where its last crawl ended (see crawl_ranges).
        Countries and time chunks are downloaded in parallel.

        Parameters
        ----------
        countries : list[str]
            list of country codes
        proc :
            procedure of entsoe-py
        begin : pd.Timestamp
            if begin and end are given, this range is crawled for all countries
        end : pd.Timestamp

        chunk_freq : str | None
            frequency at which the time range is split into parallel tasks,
            None downloads the whole range at once
        min_days : int
            minimum length of the range to crawl for a country

        Returns
        -------

        """
        tablename = proc.__name__
        log.info(f"****** {tablename} *******")
        ranges, update_state = self.crawl_ranges(tablename, countries, begin, end)
        chunks = {
            country: time_chunks(start, end_, chunk_freq)
            for country, (start, end_) in ranges.items()
            if (end_ - start).days >= min_days
        }
        if not chunks:
            log.info(f"less than {min_days} days - nothing to do")
            return
        if inspect(self.engine).has_table(tablename):
            # needed for fast deletes of already crawled ranges
            with self.engine.begin() as conn:
                self._create_country_index(conn, tablename)

        success = {}
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {
                executor.submit(self.fetch_and_write_entsoe_df_to_db, c, proc, s, e): (
                    c,
                    i,
                )
                for c, country_chunks in chunks.items()
                for i, (s, e) in enumerate(country_chunks)
            }
            for future in tqdm(
                as_completed(futures), total=len(futures), desc=tablename
            ):
                success[futures[future]] = future.result()

        if update_state:
            for country, country_chunks in chunks.items():
                # only the chunks before the first failed one count as crawled,
                # so the next run retries from the failed chunk on
                until = None
                for i, (s, e) in enumerate(country_chunks):
                    if not success[(country, i)]:
                        log.error(f"{tablename} {country} will be retried from {s}")
                        break
                    until = e
                if until is not None:
                    self.set_crawled_until(tablename, country, until)

        if create_hypertable:
            self.create_single_hypertable_if_not_exists(tablename, "index")

    def pull_crossborders(self, begin=None, end=None, allZones=True, countries=None):
        """
        Pulls transmissions across borders from entsoe.
        Each border is stored in its own column and resumed from where its last crawl ended.

        Parameters
        ----------
        begin :
            if begin and end are given, this range is crawled for all borders
        end :

        allZones :
            Default value = True)
        countries : list[str]
            only borders where at least one side is in countries (default: self.countries)

        Returns
        -------

        """
        countries = countries or self.countries
        proc = self.client.query_crossborder_flows
        tablename = proc.__name__
        log.info(f"****** {tablename} *******")

        pairs = {
            f"{n1}-{n2}".lower(): (n1, n2)
            for n1 in NEIGHBOURS
            for n2 in NEIGHBOURS[n1]
            if (n1 in countries or n2 in countries)
            and ((len(n1) == 2 and len(n2) == 2) or allZones)
        }
        ranges, update_state = self.crawl_ranges(
            tablename, list(pairs), begin, end, column_per_item=True
        )
        ranges = {col: r for col, r in ranges.items() if (r[1] - r[0]).days >= 2}
        if not ranges:
            log.info("less than 2 days - nothing to do")
            return

        def fetch_pair(col):
            n1, n2 = pairs[col]
            start, end_ = ranges[col]
            try:
                s = self.query_with_retries(proc, n1, n2, start=start, end=end_)
                return col, s[(s.index >= start) & (s.index < end_)], True
            except (NoMatchingDataError, InvalidBusinessParameterError):
                return col, None, True
            except Exception as e:
                log.error(f"Error crawling Crossboarders {n1}-{n2}: {e}")
                return col, None, False

        batch = {}
        done = []

        def flush():
            # write in batches to limit memory usage
            try:
                if batch:
                    self.merge_into_wide_table(pd.concat(batch, axis=1), tablename)
                if update_state:
                    for col in done:
                        self.set_crawled_until(tablename, col, ranges[col][1])
            except Exception as e:
                log.error(f"error saving crossboarders {e}")
            batch.clear()
            done.clear()

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = [executor.submit(fetch_pair, col) for col in ranges]
            for future in tqdm(as_completed(futures), total=len(futures)):
                col, s, ok = future.result()
                if s is not None and not s.empty:
                    batch[col] = s
                if ok:
                    done.append(col)
                if len(batch) >= self.workers:
                    flush()
        flush()

        self.create_single_hypertable_if_not_exists(tablename, "index")

    def save_power_system_data(self):
        """
        pulls static data from opsd and reads it into the database
        - used for mapping existing power plants from entsoe to a location on a map

        Parameters
        ----------

        Returns
        -------

        """
        df = pd.read_csv(
            "https://data.open-power-system-data.org/conventional_power_plants/latest/conventional_power_plants_EU.csv"
        )
        df = df.dropna(axis=0, subset=["lon", "lat", "eic_code"])
        df = df[
            [
                "eic_code",
                "name",
                "company",
                "country",
                "capacity",
                "energy_source",
                "lon",
                "lat",
            ]
        ]
        # delete those without location or eic_code

        with self.engine.begin() as conn:
            df.to_sql("powersystemdata", conn, if_exists="replace")
        return df

    def download_entsoe_plant_data(self, countries, begin, end):
        """
        Allows to download the generation per power plant from entsoe.
        Uses download_entsoe to write the data into the DB.

        Parameters
        ----------
        countries : list[str]
            list of 2-letter countrycodes
        begin : pd.Timestamp
            timestamp aware pd.Timestamp
        end : pd.Timedelta
            Timedelta to fetch data for per bulk

        Returns
        -------

        """

        # new proxy function
        def query_per_plant(country, start, end):
            """
            wrapper function around query_generation_per_plant to convert multiindex

            Parameters
            ----------
            country : str
                country to fetch
            start : pd.DateTime
                param end:
            end :


            Returns
            -------

            """
            ppp = self.client.query_generation_per_plant(country, start=start, end=end)
            # convert multiindex into second column
            pp = ppp.melt(
                var_name=["name", "type"],
                value_name="value",
                ignore_index=False,
            )
            return pp

        # entsoe-py requests per plant data day by day, so use small chunks for parallelism
        self.download_entsoe(countries, query_per_plant, begin, end, chunk_freq="MS")

        try:
            with self.engine.begin() as conn:
                query = text(
                    'CREATE INDEX IF NOT EXISTS "idx_name_query_per_plant" ON "query_per_plant" ("name", "index", "country");'
                )
                conn.execute(query)
        except Exception as e:
            log.error(f"could not create index: {e}")

        try:
            with self.engine.begin() as conn:
                query = "select distinct name, country,type from query_per_plant"
                names = pd.read_sql_query(query, conn)
                names.to_sql("plant_names", conn, if_exists="replace")
        except Exception as e:
            log.error(f"could not create plant_names: {e}")

    def countries_with_plant_data(
        self,
        countries=all_countries,
        st=pd.Timestamp("20180101", tz="Europe/Berlin"),
    ):
        """
        checks for all countries if the have available data at st or in the last week.
        Checking a recent date as well finds areas which did not exist at st (e.g. DE_LU since 2018-10).
        Returns list of countries with existing generation data per plant at given timestamp

        Parameters
        ----------
        client : entsoe.EntsoePandasClient
        countries : list[str], default all_countries

        Returns
        -------
        plant_countries : list[str]
            list of country_codes with existing data for generation per plant

        """
        plant_countries = []
        log.info("****** find countries with plant_data *******")
        recent = pd.Timestamp.now(tz="Europe/Berlin").normalize() - timedelta(days=7)
        for country in countries:
            for date in (st, recent):
                try:
                    _ = self.client.query_generation_per_plant(
                        country, start=date, end=date + timedelta(days=1)
                    )
                    plant_countries.append(country)
                    log.info(f"found data for {country}")
                    break
                except Exception:
                    continue
        return plant_countries

    def crawl_temporal(
        self,
        begin: datetime | None = None,
        end: datetime | None = None,
        countries=None,
    ):
        """
        Runs everything which is needed to update the database and pull the data since the last successful pull.

        Args:
            begin (datetime): included begin datetime from which to crawl
            end (datetime): exclusive end datetime until which to crawl
            countries (list[str]): areas to crawl (default: self.countries)

        Returns
        -------

        """
        countries = countries or self.countries
        # yearly values, so download the whole range at once and only once a year
        self.download_entsoe(
            countries,
            self.client.query_installed_generation_capacity,
            begin,
            end,
            create_hypertable=False,
            chunk_freq=None,
            min_days=366,
        )

        # timeseries
        ts_procs = [
            # self.client.query_installed_generation_capacity_per_unit,
            self.client.query_day_ahead_prices,
            self.client.query_load,
            self.client.query_load_forecast,
            self.client.query_generation_forecast,
            self.client.query_wind_and_solar_forecast,
            self.client.query_generation,
        ]

        # Download load and generation
        for proc in ts_procs:
            self.download_entsoe(countries, proc, begin, end)

        self.pull_crossborders(begin, end, countries=countries)

        plant_countries = self.countries_with_plant_data(countries)

        self.download_entsoe_plant_data(plant_countries, begin, end)

        log.info("****** finished updating ENTSO-E *******")

    def create_database(self, start: datetime, end: datetime, countries=all_countries):
        """

        Parameters
        ----------
        start :
            datetime
        end :
            datetime
        countries :
             (Default value = all_countries)

        Returns
        -------

        """
        self.init_base_sql()
        self.save_power_system_data()
        self.download_entsoe(
            countries,
            self.client.query_installed_generation_capacity_per_unit,
            start,
            end,
        )


if __name__ == "__main__":
    logging.basicConfig()
    log.info("ENTSOE")
    """
    First register at ENTSO-E transparency portal by clicking login or this link:
    https://transparency.entsoe.eu/protected-url
    Generate Token as documented here:
    https://iop-transparency.entsoe.eu/content/static_content/download?path=/Static%20content/API-Token-Management.pdf
    """
    config = load_config(DEFAULT_CONFIG_LOCATION)
    env_api_key = os.getenv("ENTSOE_API_KEY")
    if env_api_key:
        config["entsoe_api_key"] = env_api_key
    crawler = EntsoeCrawler("entsoe", config)

    start = pd.Timestamp("20150101", tz="Europe/Berlin")
    crawler.create_database(start, pd.Timestamp.now(tz="Europe/Berlin"))
    crawler.crawl_temporal(start, pd.Timestamp.now(tz="Europe/Berlin"))
    crawler.set_metadata(metadata_info)

    # start = pd.Timestamp("20150101", tz="Europe/Berlin")
    # delta = timedelta(days=30)
    # end = start + delta

    # # db = 'sqlite:///data/entsoe.db'
    # schema_name = "entsoe"

    # procs = [
    #     crawler.client.query_day_ahead_prices,
    #     crawler.client.query_net_position,
    #     crawler.client.query_load,
    #     crawler.client.query_load_forecast,
    #     crawler.client.query_generation_forecast,
    #     crawler.client.query_wind_and_solar_forecast,
    #     crawler.client.query_generation,
    # ]
    # # Download load and generation
    # for proc in procs:
    #     # hier könnte man parallelisieren
    #     crawler.download_entsoe(all_countries, proc, start)

    # # Capacities
    # procs = [
    #     crawler.client.query_installed_generation_capacity,
    #     crawler.client.query_installed_generation_capacity_per_unit,
    # ]

    # # per plant generation
    # plant_countries = crawler.countries_with_plant_data(all_countries)
