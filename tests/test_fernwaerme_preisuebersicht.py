# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import date

import pandas as pd
from oeds.crawler import fernwaerme_preisuebersicht


def test_fernwaerme_price_date_is_day_first():
    result = fernwaerme_preisuebersicht.parse_price_date(
        pd.Series(["01.10.2024", "", "13.01.2025"])
    )
    assert result.iloc[0] == date(2024, 10, 1)
    assert pd.isna(result.iloc[1])
    assert result.iloc[2] == date(2025, 1, 13)
