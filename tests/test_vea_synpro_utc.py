# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import pandas as pd
from oeds.crawler.synpro import SynproLoadProfileCrawler


def test_synpro_datetime_is_aware_utc():
    df = pd.DataFrame(
        {
            "unixtimestamp": [1451602800],
            "YYYYMMDD": [20160101],
            "hhmmss": [0],
        }
    )
    result = SynproLoadProfileCrawler.create_datetime_col(None, df)
    assert str(result["datetime"].dt.tz) == "UTC"
    assert result["datetime"][0] == pd.Timestamp("2015-12-31 23:00", tz="UTC")
