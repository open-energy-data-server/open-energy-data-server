# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
from datetime import UTC, datetime

import pandas as pd
from oeds.base_crawler import empty_config
from oeds.crawler import instrat_pl


class Response:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def test_eu_ets_resumes_from_stored_datetime(monkeypatch):
    config = empty_config()
    config["db_uri"] = "sqlite://"
    crawler = instrat_pl.InstratPlCrawler("test", config)
    stored = pd.DataFrame(
        {"eur_per_tco2": [70.0]}, index=pd.DatetimeIndex(["2024-01-02"], name="date")
    )
    with crawler.engine.begin() as conn:
        stored.to_sql("eu_ets", conn)

    rows = [
        {"date": "2024-01-03T00:00:00Z", "price": 71.0},
        {"date": "2024-01-05T00:00:00Z", "price": 73.0},
    ]
    monkeypatch.setattr(
        instrat_pl.requests,
        "get",
        lambda url, **k: Response(json.dumps(rows if "co2" in url else [])),
    )
    crawler.crawl_from_to(
        datetime(2024, 1, 3, tzinfo=UTC), datetime(2024, 1, 6, tzinfo=UTC)
    )
    with crawler.engine.connect() as conn:
        result = pd.read_sql("SELECT * FROM eu_ets ORDER BY date", conn)
    assert len(result) == 4
    assert result["eur_per_tco2"].tolist() == [70.0, 71.0, 73.0, 73.0]
