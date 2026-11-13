#!/usr/bin/env python3
# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
This crawler downloads energy datasets from Eurostat (https://ec.europa.eu/eurostat).

It uses the TSV bulk download of the Eurostat SDMX 2.1 dissemination API, which has
no limit on the number of observations (unlike the JSON-stat API).
Every dataset is stored as one long-format table named like the dataset code
(e.g. nrg_bal_c), with its dimensions as columns, and the columns

- time_period: the original period string of Eurostat, e.g. 2023, 2023-S1, 2023-Q2 or 2023-05
- period_start: first day of the period as date (periods are calendar periods, not instants)
- value: the observation as float
- flag: the Eurostat observation flag (e.g. "e" estimated, "p" provisional), empty if there is none

Missing observations (":") are not stored.
The data is a snapshot, which is refreshed by crawling with --recreate.

Optional config keys (or environment variables):
- eurostat_datasets (EUROSTAT_DATASETS): list or comma-separated string of dataset codes (default: DEFAULT_DATASETS)
- eurostat_geo (EUROSTAT_GEO): list or comma-separated string of geo codes (e.g. "DE,EU27_2020") to keep (default: all)
"""

import gzip
import io
import logging
import os
import re

import pandas as pd
import requests

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    DownloadOnceCrawler,
    load_config,
)

log = logging.getLogger("eurostat")
log.setLevel(logging.INFO)

metadata_info = {
    "schema_name": "eurostat",
    "data_source": "https://ec.europa.eu/eurostat/web/energy/database",
    "license": "CC-BY-4.0",
    "description": "Energy datasets of Eurostat (balances, capacities, electricity and gas prices, electricity supply)",
    "contact": "",
    "temporal_start": "1985-01-01",
}

EUROSTAT_URL = (
    "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{code}"
    "?format=TSV&compressed=true"
)

DEFAULT_DATASETS = [
    "nrg_bal_c",  # complete energy balances
    "nrg_inf_epcrw",  # electricity production capacities for renewables and wastes
    "nrg_pc_204",  # electricity prices for household consumers (semiannual)
    "nrg_pc_205",  # electricity prices for non-household consumers (semiannual)
    "nrg_pc_202",  # gas prices for household consumers (semiannual)
    "nrg_pc_203",  # gas prices for non-household consumers (semiannual)
    "nrg_cb_e",  # supply, transformation and consumption of electricity
]

_PERIOD = re.compile(r"^(\d{4})(?:-(?:([SQ])(\d)|(\d{2})(?:-(\d{2}))?))?$")


def period_start(period: str) -> pd.Timestamp:
    """First day of an Eurostat period: 2023, 2023-S1, 2023-Q3, 2023-05 or 2023-05-17."""
    m = _PERIOD.match(period.strip())
    if not m:
        raise ValueError(f"unknown Eurostat period: {period!r}")
    year, kind, num, month, day = m.groups()
    if kind == "S":
        month = 1 + 6 * (int(num) - 1)
    elif kind == "Q":
        month = 1 + 3 * (int(num) - 1)
    return pd.Timestamp(int(year), int(month or 1), int(day or 1))


def parse_tsv(content: str, geo: set[str] | None = None) -> pd.DataFrame:
    """Converts the Eurostat TSV (dimensions in the first column, one column per period)
    to a long table with the dimensions, time_period, period_start, value and flag."""
    wide = pd.read_csv(io.StringIO(content), sep="\t", dtype=str)
    # header of first column: "freq,nrg_bal,siec,unit,geo\TIME_PERIOD"
    dims = wide.columns[0].split("\\")[0].split(",")
    dims = [d.strip() for d in dims]
    wide.columns = ["dims"] + [c.strip() for c in wide.columns[1:]]
    keys = wide.pop("dims").str.split(",", expand=True)
    keys.columns = dims
    if geo is not None and "geo" in keys.columns:
        mask = keys["geo"].isin(geo)
        keys, wide = keys[mask], wide[mask]
    df = pd.concat([keys, wide], axis=1).melt(
        id_vars=dims, var_name="time_period", value_name="raw"
    )
    df["raw"] = df["raw"].str.strip()
    df = df[df["raw"].notna() & (df["raw"] != ":") & (df["raw"] != "")]
    parts = df["raw"].str.extract(r"^(-?[\d.]+(?:[eE][-+]?\d+)?)\s*([a-zA-Z]*)$")
    df["value"] = pd.to_numeric(parts[0], errors="coerce")
    df["flag"] = parts[1].fillna("")
    df = df[df["value"].notna()]
    starts = {p: period_start(p).date() for p in df["time_period"].unique()}
    df["period_start"] = df["time_period"].map(starts)
    columns = dims + ["time_period", "period_start", "value", "flag"]
    return df[columns].reset_index(drop=True)


def _as_list(value) -> list[str] | None:
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]
    return list(value) if value else None


class EurostatCrawler(DownloadOnceCrawler):
    def __init__(self, schema_name, config):
        super().__init__(schema_name, config)
        self.datasets = (
            _as_list(
                self.config.get("eurostat_datasets") or os.getenv("EUROSTAT_DATASETS")
            )
            or DEFAULT_DATASETS
        )
        geo = _as_list(self.config.get("eurostat_geo") or os.getenv("EUROSTAT_GEO"))
        self.geo = set(geo) if geo else None

    def structure_exists(self) -> bool:
        try:
            done = pd.read_sql("select * from crawl_complete", self.engine)
        except Exception:
            return False
        return set(self.datasets) <= set(done["dataset"])

    def download_dataset(self, code: str) -> pd.DataFrame:
        response = requests.get(EUROSTAT_URL.format(code=code), timeout=600)
        response.raise_for_status()
        text = gzip.decompress(response.content).decode("utf-8")
        return parse_tsv(text, self.geo)

    def crawl_structural(self, recreate: bool = False):
        if self.structure_exists() and not recreate:
            return
        done = []
        for code in self.datasets:
            log.info(f"Download Eurostat dataset {code}")
            df = self.download_dataset(code)
            with self.engine.begin() as conn:
                df.to_sql(code, conn, if_exists="replace", index=False, chunksize=50000)
            done.append(code)
            log.info(f"Wrote {len(df)} rows to {code}")
        # marker table is written last, so partial runs are repeated
        pd.DataFrame({"dataset": done}).to_sql(
            "crawl_complete", self.engine, if_exists="replace", index=False
        )


if __name__ == "__main__":
    logging.basicConfig()

    config = load_config(DEFAULT_CONFIG_LOCATION)
    crawler = EurostatCrawler("eurostat", config)
    crawler.crawl_structural()
    crawler.set_metadata(metadata_info)
