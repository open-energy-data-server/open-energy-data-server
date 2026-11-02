# SPDX-FileCopyrightText: Marvin Lorber
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Crawler for data from https://www.netztransparenz.de.
The resulting data is not available under an open-source license and should not be reshared but is available for crawling yourself.

Using this crawler requires an account and an API client in the Netztransparenz API portal
(see: https://api-portal.netztransparenz.de/). Put the credentials of the client into
config.yml as 'ipnt_client_id' and 'ipnt_client_secret'.

Each time series of the API is stored in its own table, see ENDPOINTS and
docs/netztransparenz.md. All times are timestamptz in UTC.
"""

import inspect
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.metadata import version

import netztransparenz as nt
import pandas as pd
import requests
import sqlalchemy as sql

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    ContinuousCrawler,
    CrawlerConfig,
    as_utc,
    ensure_utc,
    load_config,
    utcnow,
)

log = logging.getLogger("netztransparenz")
log.setLevel(logging.INFO)

metadata_info = {
    "schema_name": "netztransparenz",
    "data_source": "https://ds.netztransparenz.de/api/",
    "license": "https://www.netztransparenz.de/en/About-us/Information-platforms/Disclosure-obligations-in-accordance-with-the-EU-Transparency-Regulation",
    "description": "Data of the German TSOs from netztransparenz.de: forecasts and extrapolations of wind and solar, marketing of EEG energy, spot and negative prices, entitlement to remuneration, balancing (NRV and RZ saldo, activated aFRR/mFRR, reBAP, AEP, VoAA), redispatch, capacity reserve and curtailment.",
    "contact": "",
    "temporal_start": "2011-03-31 22:00:00",
    "temporal_end": None,
    "concave_hull_geometry": None,
}

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def database_friendly(string):
    """Column name for the database, e.g. "ID AEP in EUR/MWh" -> "id_aep_in_eur_per_mwh"."""
    string = string.lower().translate(_UMLAUTS)
    string = string.replace("(", "").replace(")", "").replace("/", "_per_")
    return re.sub(r"[\s\-.,]+", "_", string.strip())


# the client splits longer ranges into requests of a year and concatenates them
CHUNK = timedelta(days=365)
RETRIES = 3
RETRY_WAIT = 30  # seconds, times the attempt
# the API answers some long ranges with HTTP 500 that work in shorter requests
MIN_CHUNK = timedelta(days=1)
# e.g. "Es können keine Daten vor dem 30.09.2025 22:00 abgerufen werden."
NO_DATA_BEFORE = re.compile(r"keine Daten vor dem (\d{2}\.\d{2}\.\d{4} \d{2}:\d{2})")


def to_utc(value) -> pd.Timestamp:
    """Aware UTC; naive datetimes are interpreted as UTC."""
    return as_utc(value)


def is_missing_credential(value) -> bool:
    return not value or str(value).strip().upper().startswith("YOUR_")


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


class NoDataBefore(Exception):
    """The API has no data before first_data (HTTP 400)."""

    def __init__(self, first_data: pd.Timestamp):
        super().__init__(f"no data before {first_data}")
        self.first_data = first_data


def localize(*columns: str) -> Callable[[pd.DataFrame], pd.DataFrame]:
    """The client returns some times as naive UTC, the API sends all times in UTC."""

    def prepare(df: pd.DataFrame) -> pd.DataFrame:
        for column in columns:
            if column in df.columns and df[column].dt.tz is None:
                df[column] = df[column].dt.tz_localize("UTC")
        return df

    return prepare


def yes_no(*columns: str) -> Callable[[pd.DataFrame], pd.DataFrame]:
    """Stores "Ja"/"Nein" as boolean."""

    def prepare(df: pd.DataFrame) -> pd.DataFrame:
        for column in columns:
            if column in df.columns:
                df[column] = (
                    df[column].map({"Ja": True, "Nein": False}).astype("boolean")
                )
        return df

    return prepare


def negative_prices(df: pd.DataFrame) -> pd.DataFrame:
    return localize("von")(df.rename(columns={"Datum": "von"}))


def traffic_light(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns={"From": "von", "To": "bis", "Value": "ampel"})
    for column in ("von", "bis"):
        df[column] = pd.to_datetime(df[column], utc=True)
    return df


def id_aep(df: pd.DataFrame) -> pd.DataFrame:
    # the times of day are already part of von and bis
    return df.drop(columns=["(Uhrzeit) von", "(Uhrzeit) bis"], errors="ignore")


def market_premium(df: pd.DataFrame) -> pd.DataFrame:
    # "1/2025": the month in German time
    month = pd.to_datetime(df.pop("Monat"), format="%m/%Y")
    df.insert(0, "von", month.dt.tz_localize("Europe/Berlin").dt.tz_convert("UTC"))
    flags = [c for c in df.columns if c.startswith("Negative Preise")]
    return yes_no(*flags)(df)


@dataclass(frozen=True)
class Endpoint:
    """A time series of the API, stored in its own table."""

    table: str
    # method of netztransparenz.NetztransparenzClient
    method: str
    # the API names it in the HTTP 400 of earlier requests
    first_data: datetime
    time_column: str = "von"
    # keyword arguments of the method, e.g. the variant of the endpoint
    kwargs: tuple[tuple[str, str], ...] = ()
    # False for methods without the parameter transform_dates
    transform: bool = True
    chunk: timedelta = CHUNK
    # measures can be published after their begin, so the last days are fetched again
    revision: timedelta = timedelta(0)
    prepare: Callable[[pd.DataFrame], pd.DataFrame] | None = None


def nrv_saldo(table, datatype, product, first_data, **kwargs) -> Endpoint:
    return Endpoint(table, f"nrvsaldo_{datatype}_{product}", first_data, **kwargs)


MEASURES = dict(time_column="beginn", revision=timedelta(days=7))

ENDPOINTS = [
    # wind and solar
    Endpoint("hochrechnung_solar", "hochrechnung_solar", utc(2011, 3, 31, 22)),
    Endpoint("hochrechnung_wind", "hochrechnung_wind", utc(2011, 3, 31, 22)),
    *[
        Endpoint(
            f"online_hochrechnung_{kind}",
            f"online_hochrechnung_{kind}",
            first_data,
            kwargs=(("product", "qualitaetsgesichert"),),
        )
        for kind, first_data in (
            ("solar", utc(2011, 12, 31, 23)),
            ("windonshore", utc(2011, 12, 31, 23)),
            ("windoffshore", utc(2012, 12, 13, 23)),
        )
    ],
    # marketing of the EEG energy by the TSOs
    *[
        Endpoint(f"vermarktung_{name}", f"vermarktung_{name}", first_data)
        for name, first_data in (
            ("inanspruchnahme_ausgleichsenergie", utc(2011, 3, 31, 22)),
            ("untertaegige_strommengen", utc(2011, 3, 31, 22)),
            ("differenz_einspeiseprognose", utc(2011, 3, 31, 22)),
            ("exaa", utc(2011, 12, 31, 23)),
            ("epex", utc(2011, 12, 31, 22)),
            ("sonstige", utc(2013, 12, 31, 23)),
            ("solar", utc(2013, 12, 31, 23)),
            ("wind", utc(2013, 12, 31, 23)),
        )
    ],
    # prices
    Endpoint("spotmarktpreise", "spotmarktpreise", utc(2020, 12, 31, 23)),
    Endpoint("id_aep", "id_aep", utc(2020, 6, 30, 22), prepare=id_aep),
    Endpoint(
        "marktpraemie",
        "marktpraemie",
        # the client requests the months of begin and end in UTC: 2012-01 is the first
        utc(2012, 1, 1),
        prepare=market_premium,
    ),
    Endpoint(
        "negative_preise",
        "negative_preise",
        utc(2015, 12, 31, 23),
        prepare=negative_prices,
    ),
    Endpoint(
        "negative_preise_15m",
        "negative_preise_15m",
        utc(2025, 9, 30, 22),
        prepare=lambda df: yes_no("Negativ")(negative_prices(df)),
    ),
    *[
        Endpoint(
            f"anspruch_verguetung_{law}_{logic}",
            f"anspruch_verguetung_{law}",
            first_data,
            kwargs=(("logic", logic),),
            prepare=yes_no("AnspruchVerguetung"),
        )
        for law, logic, first_data in (
            ("eeg", "1hour", utc(2020, 8, 31, 22)),
            ("eeg", "2hour", utc(2025, 12, 31, 23)),
            ("eeg", "3hour", utc(2023, 12, 31, 23)),
            ("eeg", "4hour", utc(2020, 12, 31, 23)),
            ("eeg", "6hour", utc(2015, 12, 31, 23)),
            ("eeg", "15minutes", utc(2025, 9, 30, 22)),
            ("eeg", "2cent", utc(2025, 9, 30, 22)),
            ("kwkg", "1hour", utc(2015, 12, 31, 23)),
            ("kwkg", "15minutes", utc(2025, 9, 30, 22)),
        )
    ],
    # balancing, quality-assured ("qualitaetsgesichert"), published about monthly
    nrv_saldo("nrv_saldo", "nrvsaldo", "qualitaetsgesichert", utc(2013, 12, 31, 23)),
    nrv_saldo("rz_saldo", "rzsaldo", "qualitaetsgesichert", utc(2014, 4, 30, 22)),
    nrv_saldo(
        "aktivierte_srl", "aktivierte_srl", "qualitaetsgesichert", utc(2014, 4, 30, 22)
    ),
    nrv_saldo(
        "aktivierte_mrl", "aktivierte_mrl", "qualitaetsgesichert", utc(2013, 12, 31, 23)
    ),
    nrv_saldo(
        "srl_optimierung",
        "srl_optimierung",
        "qualitaetsgesichert",
        utc(2022, 6, 21, 22),
    ),
    nrv_saldo(
        "mrl_optimierung",
        "mrl_optimierung",
        "qualitaetsgesichert",
        utc(2014, 5, 31, 22),
    ),
    nrv_saldo("prl", "prl", "qualitaetsgesichert", utc(2022, 6, 21, 22)),
    nrv_saldo("difference", "difference", "qualitaetsgesichert", utc(2022, 6, 21, 22)),
    nrv_saldo(
        "zusatzmassnahmen",
        "zusatzmassnahmen",
        "qualitaetsgesichert",
        utc(2013, 12, 31, 23),
    ),
    nrv_saldo("nothilfe", "nothilfe", "qualitaetsgesichert", utc(2013, 12, 31, 23)),
    nrv_saldo(
        "abschaltbare_lasten",
        "abschaltbare_lasten",
        "qualitaetsgesichert",
        utc(2023, 11, 30, 23),
    ),
    nrv_saldo("rebap", "rebap", "qualitaetsgesichert", utc(2013, 12, 31, 23)),
    nrv_saldo("aep_module", "aep_module", "qualitaetsgesichert", utc(2022, 6, 21, 22)),
    nrv_saldo(
        "finanzielle_wirkung_aep_module",
        "finanzielle_wirkung_aep_module",
        "qualitaetsgesichert",
        utc(2022, 6, 21, 22),
    ),
    nrv_saldo(
        "value_of_avoided_activation", "voaa", "qualitaetsgesichert", utc(2023, 11, 1)
    ),
    # balancing, operational ("betrieblich"), published in near real time
    nrv_saldo("nrv_saldo_betrieblich", "nrvsaldo", "betrieblich", utc(2014, 2, 25, 23)),
    nrv_saldo(
        "nrv_saldo_minute",
        "nrvsaldo_minute",
        "betrieblich",
        utc(2026, 1, 27, 23),
        chunk=timedelta(days=31),
    ),
    nrv_saldo(
        "mfrr_satisfied_demand",
        "mfrr_satisfied_demand",
        "betrieblich",
        utc(2026, 1, 13, 23),
    ),
    nrv_saldo("aep_schaetzer", "aep_schaetzer", "betrieblich", utc(2023, 3, 13, 23)),
    nrv_saldo(
        "srl_mol_abweichungen",
        "srl_mol_abweichungen",
        "betrieblich",
        utc(2023, 12, 31, 23),
        revision=timedelta(days=7),
    ),
    nrv_saldo(
        "mrl_mol_abweichungen",
        "mrl_mol_abweichungen",
        "betrieblich",
        utc(2023, 12, 31, 23),
    ),
    Endpoint(
        "traffic_light",
        "traffic_light",
        utc(2021, 9, 22, 22),
        transform=False,
        chunk=timedelta(days=31),
        prepare=traffic_light,
    ),
    # system services
    Endpoint(
        "redispatch",
        "redispatch",
        utc(2020, 12, 31, 23),
        prepare=localize("BEGINN", "ENDE"),
        **MEASURES,
    ),
    Endpoint(
        "kapazitaetsreserve",
        "kapazitaetsreserve",
        utc(2017, 6, 2, 4),
        prepare=localize("BEGINN", "ENDE"),
        **MEASURES,
    ),
    Endpoint(
        "vorhaltung_krd",
        "vorhaltung_krd",
        utc(2025, 9, 30, 22),
        prepare=localize("BEGINN", "ENDE"),
        **MEASURES,
    ),
    *[
        Endpoint(name, name, utc(2024, 9, 30, 22), prepare=localize("von", "bis"))
        for name in ("ausgewiesene_absm", "zugeteilte_absm", "erzeugungsverbot")
    ],
]


class NetztransparenzCrawler(ContinuousCrawler):
    TIMEDELTA = timedelta(days=2)
    # table name -> time column
    TIME_COLUMNS = {
        "prognose_solar": "von",
        "prognose_wind": "von",
        **{e.table: e.time_column for e in ENDPOINTS},
    }

    def __init__(self, schema_name: str, config: CrawlerConfig):
        super().__init__(schema_name, config)
        self.client = None
        self.initialize_token()

    def get_latest_data(self) -> datetime:
        return ENDPOINTS[0].first_data

    def get_first_data(self) -> datetime:
        return ENDPOINTS[0].first_data

    def initialize_token(self):
        ipnt_client_id = self.config.get("ipnt_client_id")
        ipnt_client_secret = self.config.get("ipnt_client_secret")
        if is_missing_credential(ipnt_client_id) or is_missing_credential(
            ipnt_client_secret
        ):
            log.error(
                "Missing client credentials for netztransparenz. Please provide ipnt_client_id and ipnt_client_secret. Skipping crawler."
            )
            return

        try:
            self.client = nt.NetztransparenzClient(
                ipnt_client_id, ipnt_client_secret, strict=False
            )
        except Exception:
            log.exception("netztransparenz login failed - skipping crawler")

    def check_health(self):
        log.info(self.client.check_health())

    def _store(self, table: str, df: pd.DataFrame):
        """Writes df as aware UTC and replaces rows of the same time range, so that
        fetching an overlapping range again does not create duplicates."""
        time_column = self.TIME_COLUMNS[table]
        if (
            time_column in df.columns
            and df[time_column].isna().all()
            and isinstance(df.index, pd.DatetimeIndex)
        ):
            # the client returns a chunk without data as a frame with a time column;
            # concatenated with the chunks indexed by time, the times are in the index
            df = df.drop(columns=time_column).infer_objects()
            df.index.name = time_column
        df = ensure_utc(df)
        df = df.rename(mapper=database_friendly, axis="columns")
        in_index = time_column not in df.columns
        times = df.index if in_index else df[time_column]
        if df.empty:
            return
        with self.engine.begin() as conn:
            if sql.inspect(conn).has_table(table):
                conn.execute(
                    sql.text(
                        f"DELETE FROM {table} WHERE {time_column} BETWEEN :lo AND :hi"
                    ).bindparams(
                        sql.bindparam("lo", type_=sql.DateTime(timezone=True)),
                        sql.bindparam("hi", type_=sql.DateTime(timezone=True)),
                    ),
                    {
                        "lo": times.min().to_pydatetime(),
                        "hi": times.max().to_pydatetime(),
                    },
                )
            df.to_sql(table, conn, if_exists="append", index=in_index)

    def forecast_solar(self):
        df = self.client.prognose_solar(transform_dates=True)
        self._store("prognose_solar", df)

    def forecast_wind(self):
        df = self.client.prognose_wind(transform_dates=True)
        self._store("prognose_wind", df)

    def supports(self, endpoint: Endpoint) -> bool:
        """Older versions of the netztransparenz package lack newer endpoints, or
        use paths the API does not serve any more."""
        func = getattr(self.client, endpoint.method, None)
        if func is None:
            return False
        parameters = inspect.signature(func).parameters
        if any(p.kind is p.VAR_KEYWORD for p in parameters.values()):
            return True
        return all(name in parameters for name, _ in endpoint.kwargs)

    def fetch(self, endpoint: Endpoint, begin: pd.Timestamp, end: pd.Timestamp):
        """Stores the data of endpoint between begin and end."""
        func = getattr(self.client, endpoint.method)
        # the client interprets naive datetimes as local time, pass aware ones
        args = [begin.to_pydatetime(), end.to_pydatetime()]
        if endpoint.transform:
            args.append(True)
        df = func(*args, **dict(endpoint.kwargs))
        if df.empty:
            # e.g. no measures in the range
            return
        if endpoint.prepare is not None:
            df = endpoint.prepare(df)
        self._store(endpoint.table, df)

    def _fetch(self, endpoint: Endpoint, begin: pd.Timestamp, end: pd.Timestamp):
        """fetch again after server errors and timeouts."""
        for attempt in range(1, RETRIES + 1):
            try:
                return self.fetch(endpoint, begin, end)
            except requests.RequestException as e:
                response = getattr(e, "response", None)
                if response is not None and response.status_code < 500:
                    match = NO_DATA_BEFORE.search(response.text or "")
                    if response.status_code == 400 and match:
                        first = pd.Timestamp(
                            datetime.strptime(match.group(1), "%d.%m.%Y %H:%M"),
                            tz="UTC",
                        )
                        raise NoDataBefore(first) from e
                    raise
                if attempt == RETRIES:
                    raise
                log.warning(f"{endpoint.table} {begin} - {end} failed ({e}), retrying")
                time.sleep(RETRY_WAIT * attempt)

    def find_latest(self, tablename: str, column_name: str, default):
        """The latest time with a value: the API also returns rows without values
        for times that are not published yet."""
        try:
            with self.engine.begin() as conn:
                columns = sql.inspect(conn).get_columns(tablename)
                quote = conn.dialect.identifier_preparer.quote
                values = [
                    quote(c["name"])
                    for c in columns
                    if c["name"] not in (column_name, "bis", "ende")
                    and isinstance(
                        c["type"], (sql.Float, sql.Integer, sql.Numeric, sql.Boolean)
                    )
                ]
                query = f"SELECT max({quote(column_name)}) FROM {quote(tablename)}"
                if values:
                    query += " WHERE " + " OR ".join(f"{v} IS NOT NULL" for v in values)
                return conn.execute(sql.text(query)).scalar() or default
        except Exception:
            return default

    def create_hypertable_if_not_exists(self) -> None:
        for tablename, time_column in self.TIME_COLUMNS.items():
            self.create_single_hypertable_if_not_exists(tablename, time_column)

    def check_table_exists(self, tablename):
        return sql.inspect(self.engine).has_table(tablename)

    def crawl_endpoint(
        self, endpoint: Endpoint, begin: datetime | None, end: datetime | None
    ):
        latest = to_utc(
            self.find_latest(endpoint.table, endpoint.time_column, endpoint.first_data)
        )
        current_begin = to_utc(begin) if begin else None
        # overlapping rows are replaced when written, so begin may equal latest
        if current_begin is None or current_begin < latest:
            current_begin = max(latest - endpoint.revision, to_utc(endpoint.first_data))
        current_end = to_utc(end) if end else as_utc(utcnow())
        log.info(f"Crawling {endpoint.table} from {current_begin} to {current_end}")
        # stored per chunk, so a failure keeps the chunks before and the next run
        # resumes there
        chunk = endpoint.chunk
        while current_begin < current_end:
            chunk_end = min(current_begin + chunk, current_end)
            try:
                self._fetch(endpoint, current_begin, chunk_end)
            except requests.RequestException as e:
                response = getattr(e, "response", None)
                if (response is not None and response.status_code < 500) or (
                    chunk_end - current_begin <= MIN_CHUNK
                ):
                    raise
                chunk = max((chunk_end - current_begin) / 2, MIN_CHUNK)
                log.warning(
                    f"{endpoint.table} {current_begin} - {chunk_end} failed, "
                    f"requesting {chunk} at a time"
                )
                continue
            except NoDataBefore as e:
                if e.first_data <= current_begin:
                    raise
                log.warning(
                    f"{endpoint.table} has no data before {e.first_data}, "
                    f"not {endpoint.first_data}"
                )
                current_begin = e.first_data
                continue
            current_begin = chunk_end

    def crawl_temporal(
        self, begin: datetime | None = None, end: datetime | None = None
    ):
        if self.client is None:
            log.error("no netztransparenz client available - skipping crawl")
            return
        # forecasts change with every publication, so they are refreshed on each run
        failed = []
        for forecast in (self.forecast_solar, self.forecast_wind):
            try:
                forecast()
            except Exception:
                log.exception(f"{forecast.__name__} failed")
                failed.append(forecast.__name__)

        for endpoint in ENDPOINTS:
            if not self.supports(endpoint):
                log.warning(
                    f"skipping {endpoint.table}: netztransparenz {version('netztransparenz')} "
                    f"has no {endpoint.method} with {dict(endpoint.kwargs)}, "
                    "update the netztransparenz package"
                )
                continue
            try:
                self.crawl_endpoint(endpoint, begin, end)
            except Exception:
                log.exception(f"Crawling {endpoint.table} failed")
                failed.append(endpoint.table)
        self.create_hypertable_if_not_exists()
        if failed:
            raise RuntimeError(f"netztransparenz tables failed: {failed}")


if __name__ == "__main__":
    logging.basicConfig()

    config = load_config(DEFAULT_CONFIG_LOCATION)
    crawl = NetztransparenzCrawler("netztransparenz", config=config)
    crawl.crawl_temporal()
    crawl.set_metadata(metadata_info)
