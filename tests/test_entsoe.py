# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import pandas as pd
from entsoe import EntsoePandasClient
from oeds.crawler.entsoe_crawler import query_kwargs, to_utc


def test_generation_is_queried_nett():
    client = EntsoePandasClient(api_key="test")
    assert query_kwargs(client.query_generation) == {"nett": True}
    assert query_kwargs(client.query_generation_forecast) == {"nett": True}
    assert query_kwargs(client.query_load) == {}


def test_to_utc_reads_naive_as_utc_and_converts_aware():
    assert to_utc(pd.Timestamp("2024-01-01")) == pd.Timestamp("2024-01-01", tz="UTC")
    berlin = pd.Timestamp("2024-07-01 02:00", tz="Europe/Berlin")
    assert str(to_utc(berlin).tz) == "UTC"
    assert to_utc(berlin) == pd.Timestamp("2024-07-01 00:00", tz="UTC")
