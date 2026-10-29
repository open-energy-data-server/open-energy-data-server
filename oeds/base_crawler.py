# SPDX-FileCopyrightText: Florian Maurer
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TypedDict

import yaml
from sqlalchemy import Engine, create_engine, event, text

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
            cursor.close()

    def create_schema(self, schema_name: str) -> None:
        if self.engine.url.drivername.startswith("postgresql"):
            with self.engine.begin() as conn:
                conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_name}"))

    def set_metadata(self, metadata_info: dict[str, str]) -> None:
        set_metadata_only(self.engine, metadata_info)

    def create_hypertable_if_not_exists(self) -> None:
        pass

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
        latest = self.get_latest_data()

        if begin:
            first = self.get_first_data()
            if begin < first:
                self.crawl_from_to(begin, first)
        if not end:
            end = datetime.now()

        if latest < end - self.__class__.get_minimum_offset():
            self.crawl_from_to(latest, end)
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
