# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import io
from datetime import date

import pandas as pd
from oeds.crawler.entsog import (
    ARCHIVE_COLUMNS,
    archive_year,
    deduplicate_archive,
    extract_gasday,
    merge_lowercase_columns,
    parse_archive_values,
    prepare_operational_data,
    read_archive,
    read_archive_chunks,
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


ARCHIVE_CSV = (
    "﻿Operator Name;Operator EIC;Point name;Point EIC;Direction;Indicator;"
    "Period From;Period To;value;Unit;RmkMessage;RmkGeneral;RmkSpecific,,\r\n"
    "AB Amber Grid;21X1;Kiemenai;21Z1;entry;PhysicalFlow;"
    "2020-01-01 07:00:00.0000000 +02:00;2020-01-02 07:00:00.0000000 +02:00;"
    "1.50;kWh/d;NULL;;,,\r\n"
    "Gasunie;21X2;Emden;21Z2;exit;PhysicalFlow;"
    "2020-01-01 06:00:00.0000000 +01:00;2020-01-02 06:00:00.0000000 +01:00;"
    "0.00;kWh/d;Estimated;general;specific,,\r\n"
)


def test_read_archive_chunks_parses_offsets_and_trailing_commas():
    chunks = list(read_archive_chunks(io.StringIO(ARCHIVE_CSV), chunksize=1))
    assert len(chunks) == 2
    df = pd.concat(chunks, ignore_index=True)
    assert list(df.columns) == ARCHIVE_COLUMNS + ["periodtype"]
    # the date as written, whatever the local time and offset
    assert list(df["gasday"]) == [date(2020, 1, 1)] * 2
    assert list(df["value"]) == [1.5, 0.0]
    assert list(df["indicator"]) == ["Physical Flow"] * 2
    assert list(df["periodtype"]) == ["day"] * 2
    assert df["rmkmessage"].tolist() == [None, "Estimated"]
    assert df["rmkgeneral"].tolist() == [None, "general"]
    assert df["rmkspecific"].tolist() == [None, "specific"]
    assert df["operatorkey"].tolist() == ["21X1", "21X2"]


def test_archive_year():
    assert archive_year("AllTSOs_CalendarYear2019_ PhysicalFlow.csv") == 2019
    assert archive_year("other.csv") is None


ARCHIVE_CSV_OLD = (
    "Operator Name;Operator EIC;Point name;Point EIC;Direction;Indicator;"
    "Period From;Period To;Value;Unit;RmkMessage;RmkGeneral;RmkSpecific;"
    "Common Remarks;NmIndicatorType;IdTpInterruptionType\n"
    # UTC operator: start hour alternates with DST
    "Gasunie;21X-NL-A-A0A0A-Z;Emden;21Z2;exit;Physical Flow;"
    "2016-01-01 05:00:00.000;2016-01-02 05:00:00.000;93.698.000.000.000;kWh/d;"
    "NULL;;;;PhysicalFlow;-1\n"
    "Gasunie;21X-NL-A-A0A0A-Z;Emden;21Z2;exit;Physical Flow;"
    "2016-07-01 04:00:00.000;2016-07-02 04:00:00.000;9.999999;kWh/d;"
    "NULL;;;;PhysicalFlow;-1\n"
    # local operator: constant hour, ';' inside a quoted remark
    "National Grid;21X-GB-A-A0A0A-7;Bacton;21Z3;entry;Physical Flow;"
    "2016-01-01 05:00:00.000;2016-01-02 05:00:00.000;NULL;kWh/d;"
    'NULL;;;"a; b";PhysicalFlow;\n'
    "National Grid;21X-GB-A-A0A0A-7;Bacton;21Z3;entry;Physical Flow;"
    "2016-07-01 05:00:00.000;2016-07-02 05:00:00.000;1.000.000.000.000;kWh/d;"
    "NULL;;;;PhysicalFlow;\n"
)


def test_read_archive_old_layout_without_offsets():
    df = read_archive(io.StringIO(ARCHIVE_CSV_OLD))
    assert list(df.columns) == ARCHIVE_COLUMNS + ["periodtype"]
    assert len(df) == 4
    assert list(df["gasday"]) == [
        date(2016, 1, 1),
        date(2016, 7, 1),
        date(2016, 1, 1),
        date(2016, 7, 1),
    ]
    assert df["value"].isna().tolist() == [False, False, True, False]
    assert df["value"].dropna().tolist() == [93698000.0, 9.999999, 1000000.0]


def test_read_archive_new_layout_with_lowercase_value():
    df = read_archive(io.StringIO(ARCHIVE_CSV))
    assert list(df["gasday"]) == [date(2020, 1, 1)] * 2
    assert list(df["value"]) == [1.5, 0.0]


def test_parse_archive_values():
    values = pd.Series(["93.698.000.000.000", "9.999999", "88063395.00", "NULL", "0"])
    result = parse_archive_values(values)
    assert result.iloc[:3].tolist() == [93698000.0, 9.999999, 88063395.0]
    assert result.iloc[3:].isna().tolist() == [True, False]


def test_extract_gasday_uses_the_written_date():
    starts = pd.Series(
        [
            "2020-01-01 07:00:00.0000000 +02:00",
            "2015-12-31 05:00:00.000",
            "2016-03-27 00:00:00.000",
            "garbage",
        ]
    )
    result = extract_gasday(starts)
    assert list(result[:3]) == [date(2020, 1, 1), date(2015, 12, 31), date(2016, 3, 27)]
    assert pd.isna(result.iloc[3])


def _row(day, value, op="O", point="P", label="L", direction="entry"):
    return {
        "gasday": day,
        "operatorkey": op,
        "pointkey": point,
        "pointlabel": label,
        "directionkey": direction,
        "value": value,
    }


def test_deduplicate_archive_merges_identical_rows(caplog):
    df = pd.DataFrame([_row(date(2016, 1, 1), 1.0)] * 2)
    with caplog.at_level("WARNING"):
        result = deduplicate_archive(df)
    assert len(result) == 1
    assert "conflicting" not in caplog.text


def test_deduplicate_archive_keeps_last_of_conflicts(caplog):
    df = pd.DataFrame(
        [
            _row(date(2016, 1, 1), 1.0),
            _row(date(2016, 1, 1), float("nan"), direction="exit"),
            _row(date(2016, 1, 1), 2.0),
            _row(date(2016, 1, 2), 3.0),
        ]
    )
    with caplog.at_level("WARNING"):
        result = deduplicate_archive(df)
    assert len(result) == 3
    assert result["value"].dropna().tolist() == [2.0, 3.0]
    assert "1 archive gas days with conflicting" in caplog.text


def test_read_archive_deduplicates_file():
    line = (
        "Gasunie;21X-NL;Emden;21Z2;exit;Physical Flow;"
        "2016-01-01 05:00:00.000;2016-01-02 05:00:00.000;{};kWh/d;"
        "NULL;;;;PhysicalFlow;-1\n"
    )
    header = ARCHIVE_CSV_OLD.split("\n")[0] + "\n"
    df = read_archive(io.StringIO(header + line.format("1.0") * 2 + line.format("2.0")))
    assert df["value"].tolist() == [2.0]


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


def test_archive_year_ignores_the_first_gas_day_of_the_next_file():
    from datetime import date

    from oeds.base_crawler import empty_config
    from oeds.crawler.entsog import ARCHIVE_TABLE, EntsogCrawler

    config = empty_config()
    config["db_uri"] = "sqlite://"
    crawler = EntsogCrawler("test", config)
    with crawler.engine.begin() as conn:
        # the 2018 file starts with the gas day 2017-12-31
        pd.DataFrame({"gasday": [date(2017, 12, 31)], "value": [1.0]}).to_sql(
            ARCHIVE_TABLE, conn, index=False
        )
    assert not crawler._archive_year_present(2017)
    with crawler.engine.begin() as conn:
        pd.DataFrame({"gasday": [date(2017, 6, 15)], "value": [1.0]}).to_sql(
            ARCHIVE_TABLE, conn, index=False, if_exists="append"
        )
    assert crawler._archive_year_present(2017)
