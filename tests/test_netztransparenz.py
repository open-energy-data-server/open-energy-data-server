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
    NetztransparenzCrawler,
    is_missing_credential,
    to_utc,
)
from sqlalchemy import text


def make_crawler(**credentials):
    config = empty_config()
    config["db_uri"] = "sqlite://"
    config.update(credentials)
    return NetztransparenzCrawler("netztransparenz", config)


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
            def fetch(begin, end, transform):
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
    crawler.redispatch()
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
            def fetch(begin, end, transform):
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
            return lambda begin, end, transform: pd.DataFrame()

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
