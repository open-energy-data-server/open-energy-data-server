# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import UTC, datetime

import pandas as pd
import pytest
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
