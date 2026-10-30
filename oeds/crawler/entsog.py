#!/usr/bin/env python3
# SPDX-FileCopyrightText: Florian Maurer
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
This crawler downloads all the data of the ENTSO-G transparency platform.
The resulting data is not available under an open-source license and should not be reshared but is available for crawling yourself.
"""

import logging
import re
import tempfile
import time
import urllib.error
import urllib.parse
from datetime import date, datetime, timedelta

import pandas as pd
import requests
from sqlalchemy import Boolean, Date, DateTime, Float, bindparam, inspect, text
from tqdm import tqdm

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    ContinuousCrawler,
    as_utc,
    copy_insert,
    ensure_utc,
    load_config,
)

log = logging.getLogger("entsog")
log.setLevel(logging.INFO)

metadata_info = {
    "schema_name": "entsog",
    "data_date": "2024-06-12",
    "data_source": "https://transparency.entsog.eu/api/v1/",
    "license": "https://www.entsog.eu/privacy-policy-and-terms-use",
    "description": "ENTSOG transparency energy. Country specific flows of energy sources.",
    "contact": "",
    "temporal_start": "2017-07-10 02:00:00",
    "temporal_end": "2024-06-10 09:00:00",
    "concave_hull_geometry": None,
}

api_endpoint = "https://transparency.entsog.eu/api/v1/"
REQUEST_TIMEOUT = 600
# a month of hourly data per request takes ~10s, a day ~5s, so large ranges are much faster
CHUNK_DAYS = 31
FIRST_DAY = date(2017, 7, 10)
# the API only serves the last five years, older data is in the TP archives
API_HISTORY_YEARS = 5
INDICATORS = ["Physical Flow", "Allocation", "Firm Technical"]

fr = date(2020, 5, 18)
to = date.today()

"""
data = pd.read_csv(
    f'{api_endpoint}operationaldata.csv?limit=1000&indicator=Allocation&from={fr}&to={to}')
response = requests.get(api_endpoint+'operationaldatas')
data = pd.read_csv(api_endpoint+'AggregatedData.csv?limit=1000')
response = requests.get(api_endpoint+'AggregatedData?limit=1000')
data = pd.DataFrame(response.json()['AggregatedData'])
"""


def getDataFrame(name, params=["limit=10000"], useJson=False, allow_empty=False):
    params_str = ""
    if len(params) > 0:
        params_str = "?"
    for param in params[:-1]:
        params_str = params_str + param + "&"
    params_str += params[-1]

    i = 0
    data = pd.DataFrame()
    success = False
    while i < 5 and not success:
        try:
            i += 1
            if useJson:
                url = f"{api_endpoint}{name}.json{params_str}"
                response = requests.get(url, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                data = pd.DataFrame(response.json()[name])
                # replace empty string with None
                data = data.replace([""], [None])
            else:
                url = f"{api_endpoint}{name}.csv{params_str}"
                data = pd.read_csv(url, index_col=False)
            success = True
        except requests.exceptions.InvalidURL:
            raise
        except (
            requests.exceptions.RequestException,
            urllib.error.URLError,
            ValueError,
            KeyError,
        ) as e:
            # gateway timeouts, rate limiting and truncated responses are retried
            log.error(f"Error getting Dataframe ({repr(e)}) - attempt {i}")
            if i < 5:
                wait = 30 * i
                log.info(f"waiting {wait} seconds..")
                time.sleep(wait)

    if not success:
        raise Exception("could not get any data for params:", params_str)
    if data.empty and not allow_empty:
        raise Exception("could not get any data for params:", params_str)
    return merge_lowercase_columns(data)


def merge_lowercase_columns(data: pd.DataFrame) -> pd.DataFrame:
    """Lowercases the column names and merges columns which only differ in case.

    The JSON of some entities uses different spellings like dataSet and dataset.
    """
    merged = {}
    for col in data.columns:
        name = col.lower()
        if name in merged:
            merged[name] = merged[name].combine_first(data[col])
        else:
            merged[name] = data[col]
    return pd.DataFrame(merged, index=data.index)


# the JSON endpoint carries UTC offsets, the CSV endpoint only has CET/CEST wall time
# which is ambiguous in the repeated hour at the end of daylight saving time
TIME_COLUMNS = ["periodfrom", "periodto", "lastupdatedatetime", "originalperiodfrom"]
# columns which are empty in some responses, so their type must not be inferred
BOOL_COLUMNS = ["isunlimited", "iscamrelevant", "isna", "iscmprelevant", "isarchived"]
FLOAT_COLUMNS = ["value", "idpointtype"]
NATURAL_KEY = ["operatorkey", "pointkey", "directionkey", "periodfrom"]
SQL_TYPES = {"f": "double precision", "i": "bigint", "M": "timestamptz", "b": "boolean"}


def to_utc(series: pd.Series) -> pd.Series:
    """Converts timestamps with UTC offsets to timezone aware UTC."""
    return pd.to_datetime(series, utc=True)


def prepare_operational_data(df: pd.DataFrame) -> pd.DataFrame:
    for col in TIME_COLUMNS:
        if col in df.columns:
            df[col] = to_utc(df[col])
    for col in BOOL_COLUMNS:
        if col in df.columns:
            df[col] = df[col].map(lambda v: None if pd.isna(v) else bool(v))
    for col in FLOAT_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col])
    # the id is not unique for rows without data (isna), so use the natural key
    key = [c for c in NATURAL_KEY if c in df.columns]
    return df.drop_duplicates(subset=key, keep="last")


ARCHIVE_TABLE = "physical_flow_archive_daily"
ARCHIVE_TYPE = 16  # daily physical flow of all TSOs per calendar year
ARCHIVE_LIST_URL = f"{api_endpoint}archives?archive_type={ARCHIVE_TYPE}"
ARCHIVE_DOWNLOAD_URL = "https://transparency.entsog.eu/api/archiveDirectories"
ARCHIVE_CHUNKSIZE = 200_000
# The layout differs between the years: 2016-2019 have 15/16 columns and no
# offsets, 2020 has 13 columns with offsets and a stray ",," at the end of the
# header and of every line. Columns are therefore read by (normalised) name.
ARCHIVE_HEADER_ALIASES = {
    "operatorname": "operatorlabel",
    "operatoreic": "operatorkey",
    "pointname": "pointlabel",
    "pointeic": "pointkey",
    "direction": "directionkey",
}
ARCHIVE_SOURCE_COLUMNS = [
    "operatorlabel",
    "operatorkey",
    "pointlabel",
    "pointkey",
    "directionkey",
    "indicator",
    "periodfrom",
    "value",
    "unit",
    "rmkmessage",
    "rmkgeneral",
    "rmkspecific",
]
ARCHIVE_COLUMNS = [
    "operatorlabel",
    "operatorkey",
    "pointlabel",
    "pointkey",
    "directionkey",
    "indicator",
    "gasday",
    "value",
    "unit",
    "rmkmessage",
    "rmkgeneral",
    "rmkspecific",
]

ARCHIVE_KEY = ["gasday", "operatorkey", "pointkey", "pointlabel", "directionkey"]


def extract_gasday(starts: pd.Series) -> pd.Series:
    """The gas day is the date of "Period From" as written in the file. The
    local time and offset behind the date are ignored on purpose."""
    return pd.to_datetime(starts.str[:10], format="%Y-%m-%d", errors="coerce").dt.date


def deduplicate_archive(df: pd.DataFrame) -> pd.DataFrame:
    """One row per gas day, operator, point and direction. Identical rows are
    merged, for differing values the last one in file order wins."""
    df = df.drop_duplicates(keep="last")
    dup = df.duplicated(subset=ARCHIVE_KEY, keep=False)
    if dup.any():
        groups = df[dup].groupby(ARCHIVE_KEY, dropna=False).ngroups
        log.warning(
            f"{groups} archive gas days with conflicting rows, keeping the last"
        )
    return df.drop_duplicates(subset=ARCHIVE_KEY, keep="last")


def parse_archive_values(values: pd.Series) -> pd.Series:
    """Values up to 2019 are grouped by dots including six decimal places
    (93.698.000.000.000 is 93698000.0), later ones are plain (88063395.00)."""
    grouped = values.str.count(r"\.") > 1
    result = pd.to_numeric(values.where(~grouped), errors="coerce")
    result[grouped] = (
        pd.to_numeric(values[grouped].str.replace(".", "", regex=False)) / 1e6
    )
    return result


def prepare_archive(df: pd.DataFrame) -> pd.DataFrame:
    """Brings an archive CSV (read with normalised column names) into the
    layout of the API tables. The daily data is stored per gas day (date)."""
    df = df.copy()
    df["gasday"] = extract_gasday(df["periodfrom"])
    bad = df["gasday"].isna()
    if bad.any():
        log.warning(f"dropping {bad.sum()} archive rows with invalid gas day")
        df = df[~bad]
    df["value"] = parse_archive_values(df["value"])
    df["indicator"] = "Physical Flow"
    # the trailing ",," ends up in the last column
    df["rmkspecific"] = df["rmkspecific"].str.removesuffix(",,")
    for col in ("rmkgeneral", "rmkspecific"):
        df[col] = df[col].replace({"": None})
    df["rmkmessage"] = df["rmkmessage"].replace({"NULL": None, "": None})
    df["periodtype"] = "day"
    return deduplicate_archive(df[ARCHIVE_COLUMNS + ["periodtype"]]).reset_index(
        drop=True
    )


def _normalise_archive_header(name: str) -> str:
    key = re.sub(r"[\s,]", "", name.lstrip("﻿")).lower()
    return ARCHIVE_HEADER_ALIASES.get(key, key)


def read_archive(source, **kwargs) -> pd.DataFrame:
    """Reads a whole archive CSV (path, URL or file object) by column names."""

    def read(encoding):
        return pd.read_csv(
            source,
            sep=";",
            encoding=encoding,
            header=0,
            index_col=False,
            usecols=lambda c: _normalise_archive_header(c) in ARCHIVE_SOURCE_COLUMNS,
            dtype=str,
            keep_default_na=False,
            **kwargs,
        )

    try:
        df = read("utf-8-sig")
    except UnicodeDecodeError:
        # the 2019 archive is not UTF-8
        if hasattr(source, "seek"):
            source.seek(0)
        df = read("cp1252")
    df.columns = [_normalise_archive_header(c) for c in df.columns]
    return prepare_archive(df)


def read_archive_chunks(source, chunksize: int = ARCHIVE_CHUNKSIZE, **kwargs):
    """Yields prepared chunks of an archive CSV. The file is read as a whole as
    duplicates are removed across the file."""
    df = read_archive(source, **kwargs)
    for start in range(0, len(df), chunksize):
        yield df.iloc[start : start + chunksize]


def archive_year(filename: str) -> int | None:
    match = re.search(r"CalendarYear(\d{4})", filename)
    return int(match.group(1)) if match else None


class EntsogCrawler(ContinuousCrawler):
    def pullData(self, names):
        pbar = tqdm(names)
        for name in pbar:
            try:
                pbar.set_description(name)
                # use Json as connectionpoints have weird csv
                # TODO Json somehow has different data
                # connectionpoints count differ
                # and tpTSO column are named tSO in connpointdirections
                data = getDataFrame(name, useJson=True)

                with self.engine.begin() as conn:
                    tbl_name = name.lower().replace(" ", "_")
                    data.to_sql(tbl_name, conn, if_exists="replace")

            except Exception:
                log.exception("error pulling data")

        if "operatorpointdirections" in names:
            with self.engine.begin() as conn:
                query = text(
                    'CREATE INDEX IF NOT EXISTS "idx_opd" ON operatorpointdirections (operatorKey, pointKey,directionkey);'
                )
                conn.execute(query)

    def findNewBegin(self, table_name):
        """Returns the day before the latest stored value. This range is fetched again,
        existing rows of the fetched range are replaced, so that no duplicates occur."""
        try:
            with self.engine.begin() as conn:
                query = text(f"select max(periodfrom) from {table_name}")
                d = conn.execute(query).fetchone()[0]
            if d is None:
                raise ValueError("table is empty")
            begin = as_utc(d).date() - timedelta(days=1)
        except Exception as e:
            begin = FIRST_DAY
            log.error(f"table does not exist yet - using default start {begin} ({e})")
        return begin

    def _fetch_operational(self, indicator, beg, end):
        """Fetches a date range, which is split if the API fails to deliver it."""
        params = [
            "limit=-1",
            "indicator=" + urllib.parse.quote(indicator),
            "from=" + str(beg),
            "to=" + str(end),
            "periodType=hour",
        ]
        try:
            return getDataFrame(
                "operationaldata", params, useJson=True, allow_empty=True
            )
        except Exception:
            if (end - beg).days <= 1:
                log.exception(f"could not get {indicator} from {beg} to {end}")
                return None
            mid = beg + (end - beg) / 2
            log.warning(f"splitting {beg} to {end} at {mid}")
            parts = [
                self._fetch_operational(indicator, beg, mid),
                self._fetch_operational(indicator, mid, end),
            ]
            if any(p is None for p in parts):
                # a partly fetched range must count as failed
                return None
            return pd.concat(parts)

    def _write_operational(self, tbl_name, df):
        df = prepare_operational_data(df)
        dtype = {c: DateTime(timezone=True) for c in TIME_COLUMNS if c in df.columns}
        dtype.update({c: Boolean() for c in BOOL_COLUMNS if c in df.columns})
        dtype.update({c: Float(53) for c in FLOAT_COLUMNS if c in df.columns})
        with self.engine.begin() as conn:
            if inspect(conn).has_table(tbl_name):
                known = {c["name"] for c in inspect(conn).get_columns(tbl_name)}
                for col in df.columns:
                    if col not in known:
                        sql_type = SQL_TYPES.get(df[col].dtype.kind, "text")
                        log.info(f"adding column {col} to {tbl_name}")
                        conn.execute(
                            text(
                                f'ALTER TABLE {tbl_name} ADD COLUMN "{col}" {sql_type}'
                            )
                        )
                # replace the fetched range instead of appending it again.
                # A few rows span months and are returned by every request
                # within their period, they must not widen the deleted range.
                long = df["periodto"] - df["periodfrom"] > timedelta(days=1)
                short = df[~long]
                if not short.empty:
                    conn.execute(
                        text(
                            f"DELETE FROM {tbl_name} WHERE periodfrom BETWEEN :lo AND :hi"
                        ).bindparams(
                            bindparam("lo", type_=DateTime(timezone=True)),
                            bindparam("hi", type_=DateTime(timezone=True)),
                        ),
                        {
                            "lo": short["periodfrom"].min().to_pydatetime(),
                            "hi": short["periodfrom"].max().to_pydatetime(),
                        },
                    )
                periods = df.loc[long, ["periodfrom", "periodto"]].drop_duplicates()
                for period in periods.itertuples(index=False):
                    conn.execute(
                        text(
                            f"DELETE FROM {tbl_name} WHERE periodfrom = :lo AND periodto = :hi"
                        ).bindparams(
                            bindparam("lo", type_=DateTime(timezone=True)),
                            bindparam("hi", type_=DateTime(timezone=True)),
                        ),
                        {
                            "lo": period.periodfrom.to_pydatetime(),
                            "hi": period.periodto.to_pydatetime(),
                        },
                    )
            df = ensure_utc(df)
            df.to_sql(tbl_name, conn, if_exists="append", index=False, dtype=dtype)

    def pullOperationalData(self, indicators, initial_begin=None, end=None):
        log.info("getting values from operationaldata")
        if not end:
            end = date.today()

        for indicator in indicators:
            tbl_name = indicator.lower().replace(" ", "_")
            if initial_begin:
                begin = initial_begin
            else:
                begin = self.findNewBegin(tbl_name)
            # calendar years keep leap days out of the way, plus a safety margin
            first_available = (
                pd.Timestamp(date.today()) - pd.DateOffset(years=API_HISTORY_YEARS)
            ).date() + timedelta(days=7)
            if begin < first_available:
                log.warning(
                    f"{indicator} before {first_available} is only in the TP archives"
                )
                begin = first_available

            days = (end - begin).days
            log.info(
                f"start: {begin}, end: {end}, days: {days}, indicator: {indicator}"
            )

            if days < 1:
                continue
            delta = timedelta(days=CHUNK_DAYS)

            pbar = tqdm(range(0, days, CHUNK_DAYS))
            for i in pbar:
                beg1 = begin + timedelta(days=i)
                end1 = min(beg1 + delta, end)
                pbar.set_description(f"op {indicator} {beg1} to {end1}")

                time.sleep(1)
                df = self._fetch_operational(indicator, beg1, end1)
                if df is None:
                    # resuming from max(periodfrom) must not skip the failed window
                    log.error(
                        f"giving up {indicator} at {beg1}, resumes there next run"
                    )
                    break
                if df.empty:
                    log.warning(f"no data for {indicator} from {beg1} to {end1}")
                    continue
                try:
                    self._write_operational(tbl_name, df)
                except Exception:
                    log.exception(f"could not write {indicator} {beg1} to {end1}")
                    break

        # sqlite will only use one index. EXPLAIN QUERY PLAIN shows if index is used
        # ref: https://www.sqlite.org/optoverview.html#or_optimizations
        # reference https://stackoverflow.com/questions/31031561/sqlite-query-to-get-the-closest-datetime
        if "Allocation" in indicators:
            with self.engine.begin() as conn:
                query = text(
                    'CREATE INDEX IF NOT EXISTS "idx_opdata" ON Allocation (operatorKey,periodfrom);'
                )
                conn.execute(query)

                query = text(
                    'CREATE INDEX IF NOT EXISTS "idx_pointKey" ON Allocation (pointKey,periodfrom);'
                )
                conn.execute(query)
        if "Physical Flow" in indicators:
            with self.engine.begin() as conn:
                query = text(
                    'CREATE INDEX IF NOT EXISTS "idx_phys_operator" ON Physical_Flow (operatorKey,periodfrom);'
                )
                conn.execute(query)

                query = text(
                    'CREATE INDEX IF NOT EXISTS "idx_phys_point" ON Physical_Flow (pointKey,periodfrom);'
                )
                conn.execute(query)

        if "Firm Technical" in indicators:
            with self.engine.begin() as conn:
                query = text(
                    'CREATE INDEX IF NOT EXISTS "idx_ft_opdata" ON Firm_Technical (operatorKey,periodfrom);'
                )
                conn.execute(query)

                query = text(
                    'CREATE INDEX IF NOT EXISTS "idx_ft_pointKey" ON Firm_Technical (pointKey,periodfrom);'
                )
                conn.execute(query)

    def _archive_year_present(self, year: int) -> bool:
        # the import of a year is atomic, so a row inside the year is enough. Each
        # file also holds the last gas day of the previous year, so check mid-year.
        try:
            with self.engine.begin() as conn:
                if not inspect(conn).has_table(ARCHIVE_TABLE):
                    return False
                row = conn.execute(
                    text(
                        f"SELECT 1 FROM {ARCHIVE_TABLE} "
                        "WHERE gasday >= :lo AND gasday <= :hi LIMIT 1"
                    ),
                    {"lo": date(year, 6, 1), "hi": date(year, 6, 30)},
                ).fetchone()
            return row is not None
        except Exception:
            log.exception("could not check the archive table")
            return False

    def import_archive_file(self, path: str) -> int:
        """Replaces the rows covered by one archive file, in a single transaction."""
        df = read_archive(path)
        lo, hi = df["gasday"].min(), df["gasday"].max()
        rows = 0
        dtype = {
            "gasday": Date(),
            "value": Float(53),
        }
        with self.engine.begin() as conn:
            if inspect(conn).has_table(ARCHIVE_TABLE):
                conn.execute(
                    text(
                        f"DELETE FROM {ARCHIVE_TABLE} WHERE gasday BETWEEN :lo AND :hi"
                    ),
                    {"lo": lo, "hi": hi},
                )
            for start in range(0, len(df), ARCHIVE_CHUNKSIZE):
                chunk = df.iloc[start : start + ARCHIVE_CHUNKSIZE]
                chunk.to_sql(
                    ARCHIVE_TABLE,
                    conn,
                    if_exists="append",
                    index=False,
                    dtype=dtype,
                    method=copy_insert,
                )
                rows += len(chunk)
        return rows

    def import_archive(self, force: bool = False) -> None:
        """Imports the yearly daily physical flow archives which are not stored yet.
        The API only serves the last five years, the archive reaches back further."""
        response = requests.get(ARCHIVE_LIST_URL, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        files = [f for a in response.json()["archives"] for f in a["links"]]
        for filename in sorted(files):
            year = archive_year(filename)
            if year is None:
                continue
            if not force and self._archive_year_present(year):
                continue
            url = (
                f"{ARCHIVE_DOWNLOAD_URL}/{ARCHIVE_TYPE}/{urllib.parse.quote(filename)}"
            )
            log.info(f"importing archive {filename}")
            with tempfile.NamedTemporaryFile(suffix=".csv") as tmp:
                with requests.get(url, stream=True, timeout=REQUEST_TIMEOUT) as r:
                    r.raise_for_status()
                    for block in r.iter_content(1024 * 1024):
                        tmp.write(block)
                    tmp.flush()
                rows = self.import_archive_file(tmp.name)
            log.info(f"imported {rows} rows of {year}")
        self.create_single_hypertable_if_not_exists(ARCHIVE_TABLE, "gasday")

    def crawl_temporal(
        self, begin: datetime | None = None, end: datetime | None = None
    ):
        names = [
            "cmpUnsuccessfulRequests",
            "connectionpoints",
            "operators",
            "balancingzones",
            "operatorpointdirections",
            "Interconnections",
            "aggregateInterconnections",
            # 'operationaldata',
            # 'cmpUnavailables',
            # 'cmpAuctions',
            # 'AggregatedData', # operationaldata aggregated for each zone
            # 'tariffssimulations',
            # 'tariffsfulls',
            # 'urgentmarketmessages',
        ]

        self.pullData(names)

        self.pullOperationalData(
            INDICATORS,
            initial_begin=begin.date() if begin else None,
            end=end.date() if end else None,
        )

        try:
            self.import_archive()
        except Exception:
            log.exception("could not import the ENTSOG archive")

        self.create_hypertable_if_not_exists()

    def create_hypertable_if_not_exists(self) -> None:
        for indicator in INDICATORS:
            table_name = indicator.lower().replace(" ", "_")
            self.create_single_hypertable_if_not_exists(table_name, "periodfrom")


if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    config = load_config(DEFAULT_CONFIG_LOCATION)

    craw = EntsogCrawler("entsog", config)
    craw.crawl_temporal()
    craw.set_metadata(metadata_info)
