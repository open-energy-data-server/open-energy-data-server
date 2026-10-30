# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import gzip
from datetime import date

import pandas as pd
import pytest
from oeds.base_crawler import empty_config
from oeds.crawler import crawlers, eurostat_crawler
from oeds.crawler.eurostat_crawler import EurostatCrawler, parse_tsv, period_start

SAMPLE = (
    "freq,siec,unit,geo\\TIME_PERIOD\t2023-S1 \t2023-S2 \t2024-S1 \n"
    "S,E7000,KWH,AT\t0.2411 \t: \t0.2511 e\n"
    "S,E7000,KWH,DE\t0.3 p\t0.31 \t: \n"
    "S,E7000,KWH,FR\t: \t: \t: \n"
)


def test_period_start():
    assert period_start("2023") == pd.Timestamp("2023-01-01")
    assert period_start("2023-S2") == pd.Timestamp("2023-07-01")
    assert period_start("2023-Q3") == pd.Timestamp("2023-07-01")
    assert period_start("2023-05") == pd.Timestamp("2023-05-01")
    assert period_start("2023-05-17") == pd.Timestamp("2023-05-17")
    with pytest.raises(ValueError):
        period_start("foo")


def test_parse_tsv():
    df = parse_tsv(SAMPLE)
    assert list(df.columns) == [
        "freq",
        "siec",
        "unit",
        "geo",
        "time_period",
        "period_start",
        "value",
        "flag",
    ]
    assert len(df) == 4
    row = df[(df.geo == "AT") & (df.time_period == "2024-S1")].iloc[0]
    assert row.value == pytest.approx(0.2511)
    assert row.flag == "e"
    assert row.period_start == date(2024, 1, 1)
    de = df[(df.geo == "DE") & (df.time_period == "2023-S1")].iloc[0]
    assert de.flag == "p"
    assert df[df.time_period == "2023-S2"].period_start.iloc[0] == date(2023, 7, 1)
    assert (df[(df.geo == "AT") & (df.time_period == "2023-S1")].flag == "").all()


def test_parse_tsv_geo_filter():
    df = parse_tsv(SAMPLE, geo={"DE"})
    assert set(df.geo) == {"DE"}
    assert len(df) == 2


def test_registered():
    assert crawlers["eurostat"] is EurostatCrawler


def test_crawl_structural_writes_tables(monkeypatch, tmp_path):
    config = empty_config()
    config["db_uri"] = f"sqlite:///{tmp_path}/eurostat.db"
    config["eurostat_datasets"] = "nrg_pc_204,nrg_pc_202"
    config["eurostat_geo"] = ["AT"]

    class Response:
        content = gzip.compress(SAMPLE.encode())

        def raise_for_status(self):
            pass

    urls = []

    def fake_get(url, timeout):
        urls.append(url)
        return Response()

    monkeypatch.setattr(eurostat_crawler.requests, "get", fake_get)
    crawler = EurostatCrawler("eurostat", config)
    assert crawler.datasets == ["nrg_pc_204", "nrg_pc_202"]
    assert not crawler.structure_exists()
    crawler.crawl_structural()
    assert len(urls) == 2 and "nrg_pc_204" in urls[0]
    df = pd.read_sql("select * from nrg_pc_202", crawler.engine)
    assert set(df.geo) == {"AT"} and len(df) == 2
    assert crawler.structure_exists()
    crawler.crawl_structural()
    assert len(urls) == 2
    crawler.crawl_structural(recreate=True)
    assert len(urls) == 4
