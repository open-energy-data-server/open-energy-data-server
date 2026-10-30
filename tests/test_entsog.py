# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later


import pandas as pd
from oeds.crawler.entsog import (
    merge_lowercase_columns,
    prepare_operational_data,
    to_utc,
)


def test_to_utc_uses_offsets_on_dst_change():
    # the CSV endpoint has "2024-10-27 02:00" twice, the JSON endpoint carries the offset
    series = pd.Series(
        [
            "2024-10-27T02:00:00+02:00",
            "2024-10-27T02:00:00+01:00",
            "2024-10-27T03:00:00+01:00",
        ]
    )
    result = to_utc(series)
    assert str(result.dt.tz) == "UTC"
    assert list(result) == [
        pd.Timestamp("2024-10-27 00:00", tz="UTC"),
        pd.Timestamp("2024-10-27 01:00", tz="UTC"),
        pd.Timestamp("2024-10-27 02:00", tz="UTC"),
    ]


def test_prepare_operational_data_converts_and_drops_duplicates():
    row = {
        "id": "1NA0",
        "operatorkey": "A",
        "pointkey": "P",
        "directionkey": "entry",
        "periodfrom": "2024-10-27T02:00:00+01:00",
        "periodto": "2024-10-27T03:00:00+01:00",
        "lastupdatedatetime": "2024-10-28T05:00:00+01:00",
        "value": "1.5",
        "iscamrelevant": 1.0,
        "isna": None,
    }
    df = pd.DataFrame([row, row])
    result = prepare_operational_data(df)
    assert len(result) == 1
    assert result["periodfrom"].iloc[0] == pd.Timestamp("2024-10-27 01:00", tz="UTC")
    assert result["lastupdatedatetime"].iloc[0] == pd.Timestamp(
        "2024-10-28 04:00", tz="UTC"
    )
    assert result["value"].iloc[0] == 1.5
    assert bool(result["iscamrelevant"].iloc[0])


def test_merge_lowercase_columns_combines_spellings():
    df = pd.DataFrame(
        {"dataSet": [1, None], "dataset": [None, 2], "periodFrom": ["a", "b"]}
    )
    merged = merge_lowercase_columns(df)
    assert list(merged.columns) == ["dataset", "periodfrom"]
    assert merged["dataset"].tolist() == [1, 2]


def _hours(day_hours):
    return pd.Series([f"{d} {h}:00:00.000" for d, h in day_hours])


def test_write_operational_keeps_rows_before_long_periods():
    from oeds.base_crawler import empty_config
    from oeds.crawler.entsog import EntsogCrawler
    from sqlalchemy import text

    config = empty_config()
    config["db_uri"] = "sqlite://"
    crawler = EntsogCrawler("test", config)

    def frame(starts, hours=1):
        starts = pd.to_datetime(starts)
        return pd.DataFrame(
            {
                "periodfrom": starts,
                "periodto": starts + pd.Timedelta(hours=hours),
                "pointkey": "p",
                "directionkey": "entry",
                "operatorkey": "o",
                "value": 1.0,
            }
        )

    long_row = frame(["2025-12-31"], hours=24 * 91)
    crawler._write_operational(
        "physical_flow", pd.concat([frame(["2026-01-01", "2026-01-02"]), long_row])
    )
    # a later request returns the long row again
    crawler._write_operational(
        "physical_flow", pd.concat([frame(["2026-03-06", "2026-03-07"]), long_row])
    )
    with crawler.engine.connect() as conn:
        rows = conn.execute(text("SELECT periodfrom FROM physical_flow")).fetchall()
    assert len(rows) == 5


def test_write_operational_long_rows_only_keep_hourly_rows():
    from oeds.base_crawler import empty_config
    from oeds.crawler.entsog import EntsogCrawler
    from sqlalchemy import text

    config = empty_config()
    config["db_uri"] = "sqlite://"
    crawler = EntsogCrawler("test", config)

    def frame(starts, hours=1):
        starts = pd.to_datetime(starts)
        return pd.DataFrame(
            {
                "periodfrom": starts,
                "periodto": starts + pd.Timedelta(hours=hours),
                "pointkey": "p",
                "directionkey": "entry",
                "operatorkey": "o",
                "value": 1.0,
            }
        )

    long_row = frame(["2025-12-31"], hours=24 * 91)
    crawler._write_operational("physical_flow", frame(["2026-01-01", "2026-01-02"]))
    crawler._write_operational("physical_flow", long_row)
    crawler._write_operational("physical_flow", long_row)
    with crawler.engine.connect() as conn:
        rows = conn.execute(text("SELECT periodfrom FROM physical_flow")).fetchall()
    assert len(rows) == 3
