# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import date

import pandas as pd
import pytest

pytest.importorskip("cdsapi")
pytest.importorskip("geopandas")
pytest.importorskip("roiti")


def test_ecmwf_process_makes_times_aware_utc():
    from oeds.crawler.ecmwf_crawler import process_era5_dataframe

    df = pd.DataFrame(
        {
            "time": pd.to_datetime(["2024-01-01 01:00", "2024-01-01 02:00"]),
            "latitude": [50.0, 50.0],
            "longitude": [6.0, 6.0],
            "ghi": [1.0, 2.0],
            "wind_zonal": [1.0, 1.0],
            "wind_meridional": [1.0, 1.0],
            "temp_air": [280.0, 280.0],
        }
    )
    result = process_era5_dataframe(df)
    assert str(result["time"].dt.tz) == "UTC"
    assert result["time"].iloc[0] == pd.Timestamp("2024-01-01 01:00", tz="UTC")


def test_gie_gas_days_are_dates_and_updates_aware_utc():
    from oeds.crawler.gie_crawler import to_dataframe

    df = to_dataframe(
        [{"gasDayStart": "2024-01-01", "updatedAt": "2024-01-02 17:00:00"}], "EU"
    )
    assert df["gasdaystart"].iloc[0] == date(2024, 1, 1)
    assert str(df["updatedat"].dt.tz) == "UTC"
