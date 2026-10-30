# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from oeds.base_crawler import (
    BaseCrawler,
    ContinuousCrawler,
    as_utc,
    copy_insert,
    empty_config,
    ensure_utc,
    utcnow,
)
from sqlalchemy import text


def make_crawler(cls=BaseCrawler):
    config = empty_config()
    config["db_uri"] = "sqlite://"
    return cls("test", config)


def test_utcnow_is_aware_utc(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    import time

    time.tzset()
    try:
        now = utcnow()
        assert now.utcoffset() == timedelta(0)
        reference = pd.Timestamp.now(tz="UTC")
        assert abs(reference - pd.Timestamp(now)) < pd.Timedelta(seconds=5)
    finally:
        monkeypatch.undo()
        time.tzset()


def test_crawl_temporal_uses_revision_window():
    calls = []
    latest = utcnow() - timedelta(days=10)

    class Revised(ContinuousCrawler):
        REVISION_WINDOW = timedelta(days=3)

        def get_latest_data(self):
            return latest

        def crawl_from_to(self, begin, end):
            calls.append((begin, end))

    make_crawler(Revised).crawl_temporal()
    assert len(calls) == 1
    assert calls[0][0] == latest - timedelta(days=3)
    assert calls[0][1] - utcnow() < timedelta(seconds=5)


def test_crawl_temporal_without_revision_window_starts_at_latest():
    calls = []
    latest = datetime(2024, 1, 1)

    class Plain(ContinuousCrawler):
        def get_latest_data(self):
            return latest

        def crawl_from_to(self, begin, end):
            calls.append(begin)

    make_crawler(Plain).crawl_temporal(end=datetime(2024, 1, 5, tzinfo=UTC))
    assert calls == [pd.Timestamp(latest, tz="UTC")]


def test_replace_overlap_creates_table_and_replaces_range():
    crawler = make_crawler()

    def frame(days, plant, value):
        return pd.DataFrame(
            {
                "time": pd.to_datetime(days, utc=True),
                "plant": plant,
                "value": value,
            }
        )

    with crawler.engine.begin() as conn:
        # table does not exist yet
        first = pd.concat(
            [
                frame(["2024-01-01", "2024-01-02", "2024-01-03"], "a", 1.0),
                frame(["2024-01-01", "2024-01-02", "2024-01-03"], "b", 1.0),
            ]
        )
        crawler.replace_overlap(conn, "t", first, "time", ["plant"])
    with crawler.engine.begin() as conn:
        # only plant a is fetched again: plant b must stay untouched
        crawler.replace_overlap(
            conn,
            "t",
            frame(["2024-01-02", "2024-01-03", "2024-01-04"], "a", 2.0),
            "time",
            ["plant"],
        )
    with crawler.engine.begin() as conn:
        rows = conn.execute(
            text("select plant, substr(time, 1, 10), value from t order by 1, 2")
        ).all()
    assert rows == [
        ("a", "2024-01-01", 1.0),
        ("a", "2024-01-02", 2.0),
        ("a", "2024-01-03", 2.0),
        ("a", "2024-01-04", 2.0),
        ("b", "2024-01-01", 1.0),
        ("b", "2024-01-02", 1.0),
        ("b", "2024-01-03", 1.0),
    ]


def test_replace_overlap_without_entities_and_index_time():
    crawler = make_crawler()
    df = pd.DataFrame(
        {"value": [1.0, 2.0]},
        index=pd.Index(
            pd.to_datetime(["2024-01-01", "2024-01-02"], utc=True), name="time"
        ),
    )
    with crawler.engine.begin() as conn:
        crawler.replace_overlap(conn, "t", df, "time")
        crawler.replace_overlap(conn, "t", df * 10, "time")
        crawler.replace_overlap(conn, "t", df.iloc[0:0], "time")
        assert conn.execute(text("select sum(value), count(*) from t")).one() == (
            30.0,
            2,
        )


def test_replace_overlap_is_atomic():
    crawler = make_crawler()
    df = pd.DataFrame(
        {"time": pd.to_datetime(["2024-01-01"], utc=True), "value": [1.0]}
    )
    with crawler.engine.begin() as conn:
        crawler.replace_overlap(conn, "t", df, "time")
    with pytest.raises(RuntimeError):
        with crawler.engine.begin() as conn:
            crawler.replace_overlap(conn, "t", df.assign(value=2.0), "time")
            raise RuntimeError("fail after replace")
    with crawler.engine.begin() as conn:
        assert conn.execute(text("select value from t")).scalar() == 1.0


def test_replace_overlap_rejects_naive_datetimes():
    crawler = make_crawler()
    df = pd.DataFrame({"time": pd.to_datetime(["2024-01-01"]), "value": [1.0]})
    with pytest.raises(ValueError, match="naive"):
        with crawler.engine.begin() as conn:
            crawler.replace_overlap(conn, "t", df, "time")


def test_as_utc_reads_naive_as_utc_and_converts_aware():
    assert as_utc(datetime(2024, 1, 1)) == pd.Timestamp("2024-01-01", tz="UTC")
    berlin = pd.Timestamp("2024-07-01 02:00", tz="Europe/Berlin")
    assert as_utc(berlin) == pd.Timestamp("2024-07-01 00:00", tz="UTC")
    assert str(as_utc(berlin).tz) == "UTC"


def test_replace_overlap_with_calendar_days():
    crawler = make_crawler()

    def frame(days, value):
        return pd.DataFrame(
            {"day": [pd.Timestamp(d).date() for d in days], "value": value}
        )

    with crawler.engine.begin() as conn:
        crawler.replace_overlap(
            conn, "t", frame(["2024-01-01", "2024-01-02"], 1.0), "day"
        )
        crawler.replace_overlap(
            conn, "t", frame(["2024-01-02", "2024-01-03"], 2.0), "day"
        )
        assert conn.execute(text("select sum(value), count(*) from t")).one() == (
            5.0,
            3,
        )


def test_ensure_utc_converts_and_rejects_multiindex_levels():
    times = pd.to_datetime(["2024-07-01 02:00"]).tz_localize("Europe/Berlin")
    index = pd.MultiIndex.from_arrays([times, ["a"]], names=["time", "plant"])
    df = ensure_utc(pd.DataFrame({"value": [1.0]}, index=index))
    level = df.index.get_level_values("time")
    assert str(level.tz) == "UTC"
    assert level[0] == pd.Timestamp("2024-07-01 00:00", tz="UTC")

    naive = pd.MultiIndex.from_arrays(
        [pd.to_datetime(["2024-07-01"]), ["a"]], names=["time", "plant"]
    )
    with pytest.raises(ValueError, match="naive"):
        ensure_utc(pd.DataFrame({"value": [1.0]}, index=naive))


def test_copy_insert_quotes_identifiers():
    from types import SimpleNamespace

    from sqlalchemy.dialects import postgresql

    statements = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def copy_expert(self, sql, buf):
            statements.append(sql)

    conn = SimpleNamespace(
        connection=SimpleNamespace(cursor=Cursor),
        dialect=postgresql.dialect(),
    )
    table = SimpleNamespace(schema='my"schema', name='t"; DROP TABLE x; --')
    copy_insert(table, conn, ['col"1', "value"], [(1, 2.0)])
    assert statements == [
        'COPY "my""schema"."t""; DROP TABLE x; --" ("col""1", "value") '
        "FROM STDIN WITH (FORMAT text)"
    ]


def test_copy_text_keeps_empty_strings_apart_from_null():
    from oeds.base_crawler import _copy_text

    assert _copy_text(None) == r"\N"
    assert _copy_text("") == ""
    assert _copy_text("a\tb\nc\\d") == r"a\tb\nc\\d"
    assert _copy_text(1.5) == "1.5"


def test_ensure_utc_checks_python_datetimes_in_object_columns():
    from datetime import date, timezone

    aware = pd.DataFrame(
        {
            "t": pd.Series(
                [datetime(2024, 7, 1, 2, tzinfo=timezone(timedelta(hours=2)))],
                dtype=object,
            )
        }
    )
    converted = ensure_utc(aware)
    assert converted["t"].iloc[0] == pd.Timestamp("2024-07-01 00:00", tz="UTC")

    naive = pd.DataFrame({"t": pd.Series([datetime(2024, 7, 1)], dtype=object)})
    with pytest.raises(ValueError, match="naive"):
        ensure_utc(naive)

    # calendar days stay dates
    days = pd.DataFrame({"d": [date(2024, 7, 1)]})
    assert ensure_utc(days)["d"].iloc[0] == date(2024, 7, 1)
