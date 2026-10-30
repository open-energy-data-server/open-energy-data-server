# SPDX-FileCopyrightText: Florian Maurer
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import io
import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TypedDict

import pandas as pd
import yaml
from sqlalchemy import (
    Date,
    DateTime,
    Engine,
    bindparam,
    create_engine,
    event,
    inspect,
    text,
)

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_LOCATION = Path(__file__).parent.parent / "config.yml"


class CrawlerConfig(TypedDict):
    db_uri: str
    entsoe_api_key: str
    gie_api_key: str
    ipnt_client_id: str
    ipnt_client_secret: str
    jao_api_key: str


def load_config(config_path: Path | str = "config.yml") -> CrawlerConfig:
    with Path(config_path).open("r") as f:
        config = yaml.safe_load(f)
    return config


def empty_config() -> CrawlerConfig:
    return {
        "db_uri": "",
        "entsoe_api_key": "",
        "gie_api_key": "",
        "ipnt_client_id": "",
        "ipnt_client_secret": "",
        "jao_api_key": "",
    }


def utcnow() -> datetime:
    """Current time in UTC (timezone aware), independent of the machine timezone."""
    return datetime.now(UTC)


def as_utc(timestamp) -> pd.Timestamp:
    """Converts a timestamp read from the database to an aware UTC timestamp.

    All instants are stored as timestamptz in UTC. Naive values only come from
    databases without timezone support (sqlite) and are read as UTC.
    """
    timestamp = pd.Timestamp(timestamp)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def ensure_utc(df: pd.DataFrame) -> pd.DataFrame:
    """Converts all datetime columns and a datetime index of df to UTC.

    Naive datetimes are rejected: their meaning (UTC or local time) is unknown,
    so the crawler has to localize them explicitly before writing.
    """
    df = df.copy()
    for name in df.columns:
        series = df[name]
        if pd.api.types.is_datetime64_any_dtype(series):
            if series.dt.tz is None:
                raise ValueError(f"column {name} holds naive datetimes")
            df[name] = series.dt.tz_convert("UTC")
        elif pd.api.types.is_object_dtype(series):
            # python datetimes (not dates) in an object column
            values = series.dropna()
            instants = values[values.map(lambda v: isinstance(v, datetime))]
            if instants.empty:
                continue
            if instants.map(lambda v: v.utcoffset() is None).any():
                raise ValueError(f"column {name} holds naive datetimes")
            if len(instants) == len(values):
                df[name] = pd.to_datetime(series, utc=True)
    if isinstance(df.index, pd.MultiIndex):
        for i, level in enumerate(df.index.levels):
            if isinstance(level, pd.DatetimeIndex):
                if level.tz is None:
                    raise ValueError(f"index level {level.name} holds naive datetimes")
                df.index = df.index.set_levels(level.tz_convert("UTC"), level=i)
    elif isinstance(df.index, pd.DatetimeIndex):
        if df.index.tz is None:
            raise ValueError(f"index {df.index.name} holds naive datetimes")
        df.index = df.index.tz_convert("UTC")
    return df


def _copy_text(value) -> str:
    """Formats a value for COPY in text format, where only None becomes NULL."""
    if value is None:
        return r"\N"
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\t", r"\t")
        .replace("\n", r"\n")
        .replace("\r", r"\r")
    )


def copy_insert(table, conn, keys, data_iter):
    """pandas to_sql insertion method using COPY, which is much faster for postgres.

    The text format keeps empty strings apart from NULL, unlike CSV.
    """
    dbapi_conn = conn.connection
    with dbapi_conn.cursor() as cur:
        buf = io.StringIO()
        for row in data_iter:
            buf.write("\t".join(_copy_text(value) for value in row))
            buf.write("\n")
        buf.seek(0)
        quote = conn.dialect.identifier_preparer.quote_identifier
        columns = ", ".join(quote(k) for k in keys)
        name = ".".join(quote(part) for part in (table.schema, table.name) if part)
        sql = f"COPY {name} ({columns}) FROM STDIN WITH (FORMAT text)"
        if hasattr(cur, "copy_expert"):  # psycopg2
            cur.copy_expert(sql, buf)
        else:  # psycopg 3
            with cur.copy(sql) as copy:
                copy.write(buf.read())


class BaseCrawler:
    def __init__(self, schema_name: str, config: CrawlerConfig):
        if "db_uri" not in config.keys():
            raise ValueError("Please provide a 'db_uri' in the config")
        self.config = config.copy()
        db_uri = config["db_uri"].format(DBNAME=schema_name)
        if db_uri.startswith("postgresql+psycopg2://"):
            db_uri = "postgresql+psycopg://" + db_uri[len("postgresql+psycopg2://") :]
        elif db_uri.startswith("postgresql://"):
            db_uri = "postgresql+psycopg://" + db_uri[len("postgresql://") :]
        self.config["db_uri"] = db_uri
        self.engine = create_engine(self.config["db_uri"], pool_pre_ping=True)
        if self.engine.url.drivername.startswith("postgresql"):
            self._register_search_path(schema_name)
        self.create_schema(schema_name)

    def _register_search_path(self, schema_name: str) -> None:
        """Ensures search_path is set on every connection from the pool,
        so unqualified table names resolve to the correct schema."""

        @event.listens_for(self.engine, "connect")
        def set_search_path(dbapi_conn, conn_record) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute(f"SET search_path TO {schema_name}")
            # timestamptz values are read in UTC, independent of the server
            cursor.execute("SET TIME ZONE 'UTC'")
            cursor.close()

    def create_schema(self, schema_name: str) -> None:
        if self.engine.url.drivername.startswith("postgresql"):
            with self.engine.begin() as conn:
                conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_name}"))

    def set_metadata(self, metadata_info: dict[str, str]) -> None:
        set_metadata_only(self.engine, metadata_info)

    def create_hypertable_if_not_exists(self) -> None:
        pass

    def replace_overlap(
        self,
        conn,
        table: str,
        df: pd.DataFrame,
        time_col: str,
        entity_cols: Sequence[str] = (),
        chunksize: int | None = None,
        method=None,
    ) -> None:
        """Deletes the rows of table which overlap df and appends df, so that
        data can be fetched again (e.g. because of revisions) without duplicates.

        Rows are deleted if time_col lies between the min and max of df
        and, for every entity column, the value occurs in df.
        Call this inside the transaction of conn, to keep delete and append atomic.
        time_col may be a column or the (named) index of df.
        """
        if df.empty:
            return
        df = ensure_utc(df)
        in_columns = time_col in df.columns
        times = df[time_col] if in_columns else df.index.get_level_values(time_col)
        if inspect(conn).has_table(table):
            quote = conn.dialect.identifier_preparer.quote
            sql = f"DELETE FROM {quote(table)} WHERE {quote(time_col)} BETWEEN :lo AND :hi"
            lo, hi = times.min(), times.max()
            # calendar days (e.g. gas days) are stored as date, instants as timestamptz
            if isinstance(lo, date) and not isinstance(lo, datetime):
                time_type = Date
            else:
                time_type = DateTime(timezone=True)
                lo = pd.Timestamp(lo).to_pydatetime()
                hi = pd.Timestamp(hi).to_pydatetime()
            params = {"lo": lo, "hi": hi}
            for i, col in enumerate(entity_cols):
                sql += f" AND {quote(col)} IN :e{i}"
                params[f"e{i}"] = df[col].unique().tolist()
            stmt = text(sql).bindparams(
                bindparam("lo", type_=time_type),
                bindparam("hi", type_=time_type),
                *[bindparam(f"e{i}", expanding=True) for i in range(len(entity_cols))],
            )
            conn.execute(stmt, params)
        df.to_sql(
            table,
            conn,
            if_exists="append",
            index=not in_columns,
            chunksize=chunksize,
            method=method,
        )

    def create_single_hypertable_if_not_exists(
        self, table_name: str, time_column: str
    ) -> None:
        if not self.engine.url.drivername.startswith("postgresql"):
            return

        try:
            with self.engine.connect() as conn:
                tbl_exists = conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :t"
                    ),
                    {"t": table_name},
                ).scalar()
            if not tbl_exists:
                return

            query_create_hypertable = text(
                f"SELECT public.create_hypertable('{table_name}', '{time_column}', if_not_exists => TRUE, migrate_data => TRUE);"
            )
            with self.engine.begin() as conn:
                conn.execute(query_create_hypertable)
            logger.info(f"created hypertable {table_name} if not exists")
        except Exception as e:
            logger.error(f"could not create hypertable: {e}")


class DownloadOnceCrawler(BaseCrawler):
    def structure_exists(self) -> bool:
        return False

    def crawl_structural(self, recreate: bool = False):
        if not self.structure_exists() or recreate:
            raise NotImplementedError()
        self.create_hypertable_if_not_exists()


class ContinuousCrawler(BaseCrawler):
    """Ideomatic Crawler for temporal data, served for continuous execution of the crawler.

    The idea is to take care of conditional constraints (like end date must be at hour 0) in the crawler.
    All crawlers should fit to this interface, to handle the start and end date well, and also handle download of data prior to the existing data, if exists.

    All temporal data should be in UTC.


    Args:
        BaseCrawler (_type_): _description_
    """

    OFFSET_FROM_NOW = timedelta(hours=1)
    # data younger than this is revised by the source: crawl_temporal fetches it
    # again, so crawl_from_to must be idempotent for the overlap (e.g. replace_overlap)
    REVISION_WINDOW: timedelta = timedelta(0)

    @classmethod
    def get_minimum_offset(cls):
        return cls.OFFSET_FROM_NOW

    def get_latest_data(self) -> datetime:
        raise NotImplementedError()

    def get_first_data(self) -> datetime:
        raise NotImplementedError()

    def crawl_from_to(self, begin: datetime, end: datetime):
        """Crawls data from begin (inclusive) until end (exclusive)

        Args:
            begin (datetime): included begin datetime from which to crawl
            end (datetime): exclusive end datetime until which to crawl
        """

    def crawl_temporal(
        self, begin: datetime | None = None, end: datetime | None = None
    ):
        latest = as_utc(self.get_latest_data())

        if begin:
            begin = as_utc(begin)
            first = as_utc(self.get_first_data())
            if begin < first:
                self.crawl_from_to(begin, first)
        end = as_utc(end) if end else utcnow()

        if latest < end - self.__class__.get_minimum_offset():
            self.crawl_from_to(latest - self.REVISION_WINDOW, end)
        self.create_hypertable_if_not_exists()


def set_metadata_only(engine: Engine, metadata_info: dict[str, str]):
    for key in ["temporal_start", "temporal_end", "contact"]:
        if key not in metadata_info.keys():
            metadata_info[key] = None
    if "data_date" not in metadata_info.keys():
        metadata_info["data_date"] = date.today()
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO public.metadata
            (schema_name, data_date, data_source, license, description, contact, temporal_start, temporal_end)
            VALUES
            (:schema_name, :data_date, :data_source, :license, :description, :contact, :temporal_start, :temporal_end)
            ON CONFLICT (schema_name) DO UPDATE SET
                data_date = EXCLUDED.data_date,
                data_source = EXCLUDED.data_source,
                license = EXCLUDED.license,
                description = EXCLUDED.description,
                contact = EXCLUDED.contact,
                temporal_start = EXCLUDED.temporal_start,
                temporal_end = EXCLUDED.temporal_end
            """),
            metadata_info,
        )
        conn.execute(
            text("""
            UPDATE public.metadata
            SET tables = (SELECT COUNT(*) FROM pg_class JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace WHERE nspname = :schema_name AND pg_class.relkind = 'r'),
                size = (SELECT SUM(pg_total_relation_size(pg_class.oid)) FROM pg_class JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace WHERE nspname = :schema_name AND pg_class.relkind = 'r'),
                crawl_date = NOW()
            WHERE schema_name = :schema_name
            """),
            {"schema_name": metadata_info["schema_name"]},
        )
        conn.execute(
            text("""
            NOTIFY pgrst, 'reload schema';
            """)
        )
