# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Downloads the power plant list of the PyPSA powerplantmatching project.

The list merges several public power plant databases (MaStR, GEM, JRC, OPSD, ...)
into one csv file. It is a snapshot, so it is only downloaded once
(or again with recreate=True).
"""

import logging
import re
from io import StringIO

import pandas as pd
import requests
from sqlalchemy import text

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    DownloadOnceCrawler,
    load_config,
    utcnow,
)

log = logging.getLogger("powerplantmatching")
log.setLevel(logging.INFO)

metadata_info = {
    "schema_name": "powerplantmatching",
    "data_source": "https://github.com/PyPSA/powerplantmatching",
    "license": "https://github.com/PyPSA/powerplantmatching/blob/master/LICENSE",
    "description": "Matched European power plant list from PyPSA powerplantmatching. "
    "Licenses of the underlying databases may differ.",
    "contact": "",
    "temporal_start": None,
}

DOWNLOAD_URL = (
    "https://raw.githubusercontent.com/PyPSA/powerplantmatching/master/powerplants.csv"
)

COLUMN_MAPPING = {
    "id": "source_id",
    "Name": "name",
    "Fueltype": "fuel_type",
    "Technology": "technology",
    "Set": "set_type",
    "Country": "country",
    "Capacity": "capacity",
    "Efficiency": "efficiency",
    "DateIn": "date_in",
    "DateRetrofit": "date_retrofit",
    "DateOut": "date_out",
    "lat": "lat",
    "lon": "lon",
    "Duration": "duration",
    "Volume_Mm3": "volume_mm3",
    "DamHeight_m": "dam_height_m",
    "StorageCapacity_MWh": "storage_capacity_mwh",
    "EIC": "eic",
    "projectID": "project_id",
}

# values look like "{'48WSTN00000CRUAC', '12W-0000000018-B'}" or "set()"
_QUOTED = re.compile(r"'([^']+)'")


def parse_eic_set(value) -> list[str]:
    """Extracts the codes of a stringified python set, without evaluating it."""
    if not isinstance(value, str):
        return []
    return sorted({c.strip() for c in _QUOTED.findall(value) if c.strip()})


def parse_powerplants(csv_text: str) -> pd.DataFrame:
    """Parses the powerplantmatching csv into a dataframe with snake_case columns."""
    df = pd.read_csv(StringIO(csv_text))
    df = df.rename(columns=COLUMN_MAPPING)
    df = df[[c for c in COLUMN_MAPPING.values() if c in df.columns]]
    for col in ("date_in", "date_retrofit", "date_out"):
        if col in df.columns:
            df[col] = df[col].astype("Int64")
    return df


def download_powerplants_csv() -> str:
    resp = requests.get(DOWNLOAD_URL, timeout=300)
    resp.raise_for_status()
    return resp.text


class PowerplantmatchingCrawler(DownloadOnceCrawler):
    def structure_exists(self) -> bool:
        try:
            with self.engine.connect() as conn:
                return (
                    conn.execute(text("SELECT 1 from powerplants limit 1")).scalar()
                    == 1
                )
        except Exception:
            return False

    def crawl_structural(self, recreate: bool = False):
        if not self.structure_exists() or recreate:
            log.info("Downloading powerplantmatching data")
            df = parse_powerplants(download_powerplants_csv())
            df["downloaded_at"] = utcnow()
            with self.engine.begin() as conn:
                df.to_sql("powerplants", conn, if_exists="replace", index=False)
            log.info("Finished writing %i power plants", len(df))
        if self.engine.url.drivername.startswith("postgresql"):
            self.set_metadata(metadata_info)


if __name__ == "__main__":
    logging.basicConfig()

    config = load_config(DEFAULT_CONFIG_LOCATION)
    crawler = PowerplantmatchingCrawler("powerplantmatching", config=config)
    crawler.crawl_structural(recreate=False)
