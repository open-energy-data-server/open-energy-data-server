# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import pandas as pd
from oeds.crawler.eview import local_to_utc


def test_local_to_utc_summer_and_winter():
    index = pd.DatetimeIndex(["2023-07-01 13:00", "2023-12-01 13:00"])
    result = local_to_utc(index)
    assert list(result) == [
        pd.Timestamp("2023-07-01 11:00", tz="UTC"),
        pd.Timestamp("2023-12-01 12:00", tz="UTC"),
    ]
    assert str(result.tz) == "UTC"


def test_local_to_utc_spring_gap_becomes_nat():
    index = pd.date_range("2023-03-26 01:45", "2023-03-26 03:00", freq="15min")
    result = local_to_utc(index)
    assert result.isna().sum() == 4  # 02:00 - 02:45 do not exist
    assert result[0] == pd.Timestamp("2023-03-26 00:45", tz="UTC")
    assert result[-1] == pd.Timestamp("2023-03-26 01:00", tz="UTC")


def test_local_to_utc_autumn_repeated_hour_by_order():
    index = pd.DatetimeIndex(
        ["2023-10-29 01:45", "2023-10-29 02:00", "2023-10-29 02:00", "2023-10-29 03:00"]
    )
    result = local_to_utc(index)
    assert list(result) == [
        pd.Timestamp("2023-10-28 23:45", tz="UTC"),
        pd.Timestamp("2023-10-29 00:00", tz="UTC"),
        pd.Timestamp("2023-10-29 01:00", tz="UTC"),
        pd.Timestamp("2023-10-29 02:00", tz="UTC"),
    ]


def test_local_to_utc_autumn_unique_is_standard_time():
    result = local_to_utc(pd.DatetimeIndex(["2023-10-29 02:15"]))
    assert result[0] == pd.Timestamp("2023-10-29 01:15", tz="UTC")
