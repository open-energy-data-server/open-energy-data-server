# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import UTC, datetime

import pandas as pd
import pytest
import requests
from oeds.base_crawler import empty_config
from oeds.crawler import netztransparenz
from oeds.crawler.netztransparenz import (
    ENDPOINTS,
    NetztransparenzCrawler,
    database_friendly,
    is_missing_credential,
    to_utc,
)
from sqlalchemy import text


def make_crawler(**credentials):
    config = empty_config()
    config["db_uri"] = "sqlite://"
    config.update(credentials)
    return NetztransparenzCrawler("netztransparenz", config)


def endpoint(table):
    return next(e for e in ENDPOINTS if e.table == table)


def utc_frame(start, periods, tz="UTC"):
    index = pd.date_range(start, periods=periods, freq="h", tz=tz, name="von")
    return pd.DataFrame(
        {"Wert": range(periods), "bis": index + pd.Timedelta("1h")}, index=index
    )


@pytest.mark.parametrize("value", ["", None, "YOUR_IPNT_CLIENT_ID", "your_secret"])
def test_placeholder_credentials_are_missing(value):
    assert is_missing_credential(value)
    assert not is_missing_credential("cm_app_ntp_id_123")


def test_missing_credentials_skip_crawl(caplog):
    crawler = make_crawler(ipnt_client_id="YOUR_ID", ipnt_client_secret="YOUR_SECRET")
    assert crawler.client is None
    crawler.crawl_temporal()
    assert "Missing client credentials" in caplog.text


def test_login_failure_is_caught(monkeypatch):
    def fail(*args, **kwargs):
        raise Exception("Login failed.")

    monkeypatch.setattr(netztransparenz.nt, "NetztransparenzClient", fail)
    crawler = make_crawler(ipnt_client_id="id", ipnt_client_secret="secret")
    assert crawler.client is None


def test_to_utc():
    assert to_utc(datetime(2024, 1, 1, 12)) == pd.Timestamp(
        "2024-01-01 12:00", tz="UTC"
    )
    aware = pd.Timestamp("2024-07-01 12:00", tz="Europe/Berlin").to_pydatetime()
    assert to_utc(aware) == pd.Timestamp("2024-07-01 10:00", tz="UTC")


def test_store_writes_utc_without_duplicates():
    crawler = make_crawler()
    crawler._store(
        "hochrechnung_wind", utc_frame("2024-10-26 22:00", 4, tz="Europe/Berlin")
    )
    # an overlapping, revised range replaces rows instead of appending them again
    revised = utc_frame("2024-10-26 22:00", 4, tz="UTC")
    revised.index = revised.index + pd.Timedelta("1h")
    crawler._store("hochrechnung_wind", revised)
    with crawler.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT von, bis FROM hochrechnung_wind ORDER BY von")
        ).all()
    times = [r[0] for r in rows]
    assert len(times) == len(set(times))
    assert times[0].startswith("2024-10-26 20:00")
    assert all("+" not in t for t in times)


def test_redispatch_uses_beginn_and_no_index():
    crawler = make_crawler()
    df = pd.DataFrame(
        {
            "BEGINN": pd.to_datetime(["2024-01-01 10:00"], utc=True),
            "ENDE": pd.to_datetime(["2024-01-01 11:00"], utc=True),
        }
    )
    crawler._store("redispatch", df)
    crawler._store("redispatch", df)
    with crawler.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM redispatch")).scalar() == 1
        columns = [r[1] for r in conn.execute(text("PRAGMA table_info(redispatch)"))]
    assert "beginn" in columns
    assert "index" not in columns


def test_crawl_temporal_passes_aware_utc_and_refreshes_forecasts():
    crawler = make_crawler(ipnt_client_id="id", ipnt_client_secret="secret")
    calls = {"forecast": 0, "ranges": []}

    class Client:
        def prognose_solar(self, transform_dates):
            calls["forecast"] += 1
            return utc_frame("2024-01-01", 2)

        prognose_wind = prognose_solar

        def __getattr__(self, name):
            def fetch(begin, end, *args, **kwargs):
                calls["ranges"].append((begin, end))
                return pd.DataFrame()

            return fetch

    crawler.client = Client()
    crawler.crawl_temporal()
    crawler.crawl_temporal()
    assert calls["forecast"] == 4
    assert calls["ranges"]
    assert all(b.tzinfo is UTC and e.tzinfo is UTC for b, e in calls["ranges"])


def test_store_rejects_naive_datetimes():
    import pytest

    crawler = make_crawler()
    df = pd.DataFrame({"BEGINN": pd.to_datetime(["2024-01-01 10:00"])})
    with pytest.raises(ValueError):
        crawler._store("redispatch", df)


def test_redispatch_localizes_naive_utc_like_the_client():
    crawler = make_crawler()

    class Client:
        def redispatch(self, begin, end, transform):
            return pd.DataFrame(
                {
                    "BEGINN": pd.to_datetime(["2024-01-01 10:00"]),
                    "ENDE": pd.to_datetime(["2024-01-01 11:00"]),
                }
            )

    crawler.client = Client()
    crawler.fetch(
        endpoint("redispatch"),
        pd.Timestamp("2024-01-01", tz="UTC"),
        pd.Timestamp("2024-01-02", tz="UTC"),
    )
    with crawler.engine.connect() as conn:
        beginn = conn.execute(text("SELECT beginn FROM redispatch")).scalar()
    assert beginn.startswith("2024-01-01 10:00")


def test_failing_table_does_not_abort_others():
    crawler = make_crawler(ipnt_client_id="id", ipnt_client_secret="secret")
    fetched = []
    hypertables = []

    class Client:
        def prognose_solar(self, transform_dates):
            return utc_frame("2024-01-01", 2)

        prognose_wind = prognose_solar

        def redispatch(self, begin, end, transform):
            raise RuntimeError("boom")

        def __getattr__(self, name):
            def fetch(begin, end, *args, **kwargs):
                fetched.append(name)
                return pd.DataFrame()

            return fetch

    crawler.client = Client()
    crawler.create_hypertable_if_not_exists = lambda: hypertables.append(True)
    with pytest.raises(RuntimeError, match="redispatch"):
        crawler.crawl_temporal()
    assert "nrvsaldo_rzsaldo_qualitaetsgesichert" in fetched
    assert hypertables


def test_store_keeps_the_times_of_chunks_concatenated_with_an_empty_chunk():
    import io

    from netztransparenz.constants import endpoints

    crawler = make_crawler()
    header = endpoints["/NrvSaldo/AktivierteMRL/Qualitaetsgesichert"]
    empty = pd.read_csv(io.StringIO(header["transformed_header"]), sep=";")
    data = utc_frame("2014-05-01", 2).rename(columns={"Wert": "Deutschland (Positiv)"})
    crawler._store("aktivierte_mrl", pd.concat([empty, data]))
    with crawler.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT von, deutschland_positiv FROM aktivierte_mrl ORDER BY von")
        ).all()
    assert [r[0][:16] for r in rows] == ["2014-05-01 00:00", "2014-05-01 01:00"]
    assert [r[1] for r in rows] == [0.0, 1.0]


def _http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status}", response=response)


def _chunk_crawler(fetch):
    crawler = make_crawler(ipnt_client_id="id", ipnt_client_secret="secret")

    class Client:
        def prognose_solar(self, transform_dates):
            return pd.DataFrame()

        prognose_wind = prognose_solar

        def __getattr__(self, name):
            if name == "hochrechnung_wind":
                return fetch
            return lambda begin, end, *args, **kwargs: pd.DataFrame()

    crawler.client = Client()
    crawler.create_hypertable_if_not_exists = lambda: None
    return crawler


def test_chunks_are_stored_so_a_failure_resumes_after_them(monkeypatch):
    monkeypatch.setattr(netztransparenz, "RETRY_WAIT", 0)
    ranges = []

    def fetch(begin, end, transform):
        ranges.append((begin, end))
        if len(ranges) == 2:
            raise _http_error(400)
        return utc_frame(begin.replace(tzinfo=None), 2)

    crawler = _chunk_crawler(fetch)
    end = datetime(2013, 6, 1, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="hochrechnung_wind"):
        crawler.crawl_temporal(end=end)
    assert all(e - b <= netztransparenz.CHUNK for b, e in ranges)
    assert ranges[1][0] == ranges[0][1]
    assert crawler.find_latest("hochrechnung_wind", "von", None).startswith(
        "2011-03-31 23:00"
    )


def test_server_errors_are_retried(monkeypatch):
    monkeypatch.setattr(netztransparenz, "RETRY_WAIT", 0)
    failures = [_http_error(500), requests.Timeout()]

    def fetch(begin, end, transform):
        if failures:
            raise failures.pop(0)
        return pd.DataFrame()

    crawler = _chunk_crawler(fetch)
    crawler.crawl_temporal(end=datetime(2011, 6, 1, tzinfo=UTC))
    assert failures == []


def test_database_friendly_column_names():
    assert database_friendly("TenneT TSO (MW)") == "tennet_tso_mw"
    assert database_friendly("ID AEP in EUR/MWh") == "id_aep_in_eur_per_mwh"
    assert database_friendly("AEP-Schätzer") == "aep_schaetzer"
    assert database_friendly("Abruf-ÜNB") == "abruf_uenb"


def test_every_table_has_one_endpoint():
    tables = [e.table for e in ENDPOINTS]
    assert len(tables) == len(set(tables))
    assert all(e.first_data.tzinfo is UTC for e in ENDPOINTS)


def test_resume_at_the_last_time_with_values():
    crawler = make_crawler()
    df = utc_frame("2024-01-01", 4).astype({"Wert": float})
    # the API returns rows without values for times not published yet
    df.loc[df.index[-2:], "Wert"] = None
    crawler._store("rebap", df)
    latest = crawler.find_latest("rebap", "von", None)
    assert latest.startswith("2024-01-01 01:00")


def test_no_data_before_moves_the_begin(monkeypatch):
    monkeypatch.setattr(netztransparenz, "RETRY_WAIT", 0)
    ranges = []

    def fetch(begin, end, transform):
        ranges.append(begin)
        if begin < datetime(2012, 1, 1, tzinfo=UTC):
            response = requests.Response()
            response.status_code = 400
            response._content = "Es können keine Daten vor dem 01.01.2012 00:00 abgerufen werden.".encode()
            raise requests.HTTPError("400", response=response)
        return utc_frame(begin.replace(tzinfo=None), 2)

    crawler = _chunk_crawler(fetch)
    crawler.crawl_temporal(end=datetime(2012, 2, 1, tzinfo=UTC))
    assert ranges[1] == datetime(2012, 1, 1, tzinfo=UTC)
    assert crawler.find_latest("hochrechnung_wind", "von", None).startswith(
        "2012-01-01 01:00"
    )


def test_endpoints_missing_in_the_client_are_skipped(caplog):
    crawler = make_crawler(ipnt_client_id="id", ipnt_client_secret="secret")

    class Client:
        def prognose_solar(self, transform_dates):
            return pd.DataFrame()

        prognose_wind = prognose_solar

        def online_hochrechnung_solar(self, begin, end, transform_dates=False):
            # the path of old client versions returns HTTP 404
            raise AssertionError("must not be called")

        def __getattr__(self, name):
            if name.startswith(("anspruch_", "online_")):
                raise AttributeError(name)
            return lambda begin, end, *args, **kwargs: pd.DataFrame()

    crawler.client = Client()
    crawler.create_hypertable_if_not_exists = lambda: None
    crawler.crawl_temporal(end=datetime(2011, 6, 1, tzinfo=UTC))
    assert "skipping online_hochrechnung_solar" in caplog.text
    assert "skipping anspruch_verguetung_eeg_1hour" in caplog.text


def test_prepare_converts_flags_and_times():
    negative = endpoint("negative_preise_15m").prepare(
        pd.DataFrame(
            {
                "Datum": pd.to_datetime(["2025-10-01 00:00", "2025-10-01 00:15"]),
                "Negativ": ["Ja", "Nein"],
            }
        )
    )
    assert negative["von"].dt.tz is not None
    assert list(negative["Negativ"]) == [True, False]

    premium = endpoint("marktpraemie").prepare(
        pd.DataFrame({"Monat": ["1/2025"], "Negative Preise (1H)": ["Ja"]})
    )
    # the month in German time
    assert premium["von"].iloc[0] == pd.Timestamp("2024-12-31 23:00", tz="UTC")
    assert premium["Negative Preise (1H)"].iloc[0]

    light = endpoint("traffic_light").prepare(
        pd.DataFrame(
            {
                "From": ["2025-06-01T00:00:00Z"],
                "To": ["2025-06-01T00:01:00Z"],
                "Value": ["GREEN"],
            }
        )
    )
    assert list(light.columns) == ["von", "bis", "ampel"]
    assert light["von"].iloc[0] == pd.Timestamp("2025-06-01", tz="UTC")


def test_empty_frames_with_naive_columns_are_skipped():
    crawler = make_crawler()

    class Client:
        def kapazitaetsreserve(self, begin, end, transform):
            return pd.DataFrame(
                {"BEGINN": pd.to_datetime([]), "ENDE": pd.to_datetime([])}
            )

    crawler.client = Client()
    crawler.fetch(
        endpoint("kapazitaetsreserve"),
        pd.Timestamp("2024-01-01", tz="UTC"),
        pd.Timestamp("2024-01-02", tz="UTC"),
    )
    assert not crawler.check_table_exists("kapazitaetsreserve")


def test_failing_ranges_are_split(monkeypatch):
    monkeypatch.setattr(netztransparenz, "RETRY_WAIT", 0)
    ranges = []

    def fetch(begin, end, transform):
        ranges.append((begin, end))
        # like NrvSaldo/NRVSaldo/Betrieblich for 2024-02..2025-02
        if end - begin > pd.Timedelta(days=100):
            raise _http_error(500)
        return utc_frame(begin.replace(tzinfo=None), 2)

    crawler = _chunk_crawler(fetch)
    end = datetime(2012, 3, 31, tzinfo=UTC)
    crawler.crawl_temporal(end=end)
    stored = [(b, e) for b, e in ranges if e - b <= pd.Timedelta(days=100)]
    assert stored[0][0] == datetime(2011, 3, 31, 22, tzinfo=UTC)
    assert stored[-1][1] == end
    assert all(a[1] == b[0] for a, b in zip(stored, stored[1:]))


def test_ranges_failing_down_to_a_day_fail_the_table(monkeypatch):
    monkeypatch.setattr(netztransparenz, "RETRY_WAIT", 0)

    def fetch(begin, end, transform):
        raise _http_error(500)

    crawler = _chunk_crawler(fetch)
    with pytest.raises(RuntimeError, match="hochrechnung_wind"):
        crawler.crawl_temporal(end=datetime(2011, 6, 1, tzinfo=UTC))
