# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import pandas as pd
from oeds.crawler.opsd import prepare_when2heat


def test_prepare_when2heat_is_aware_utc():
    data = pd.DataFrame(
        {
            "utc_timestamp": ["2019-07-01T00:00:00Z", "2019-12-01T00:00:00Z"],
            "cet_cest_timestamp": [
                "2019-07-01T02:00:00+0200",
                "2019-12-01T01:00:00+0100",
            ],
            "DE_COP": [3.1, 2.9],
        }
    )
    result = prepare_when2heat(data)
    assert result.index.name == "utc_timestamp"
    assert str(result.index.tz) == "UTC"
    assert list(result.index) == [
        pd.Timestamp("2019-07-01 00:00", tz="UTC"),
        pd.Timestamp("2019-12-01 00:00", tz="UTC"),
    ]
    assert str(result["cet_cest_timestamp"].dt.tz) == "UTC"
    assert (result["cet_cest_timestamp"].to_numpy() == result.index.to_numpy()).all()
    assert "utc_timestamp" not in result.columns
    assert list(result["DE_COP"]) == [3.1, 2.9]
