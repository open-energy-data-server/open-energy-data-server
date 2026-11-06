# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
EIC (Energy Identification Code) lists.

eic_codes:     official ENTSO-E lists of allocated EIC codes, one list per code type
               (A, T, V, W, X, Y, Z).
eic_locations: EIC code -> lat/lon, derived from the EIC codes which are part of the
               PyPSA powerplantmatching power plant list.
"""

import logging
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
from oeds.crawler.powerplantmatching import (
    download_powerplants_csv,
    parse_eic_set,
    parse_powerplants,
)

log = logging.getLogger("eic_mapping")
log.setLevel(logging.INFO)

metadata_info = {
    "schema_name": "eic_mapping",
    "data_source": "https://www.entsoe.eu/data/energy-identification-codes-eic/eic-approved-codes/",
    "license": "https://www.entsoe.eu/data/energy-identification-codes-eic/",
    "description": "ENTSO-E allocated EIC codes and a mapping of EIC codes to "
    "coordinates derived from PyPSA powerplantmatching",
    "contact": "",
    "temporal_start": "",
}

# A: Substation, T: Tie-line, V: Location, W: Resource object,
# X: Party, Y: Area/Domain, Z: Measurement point
EIC_TYPES = ["A", "T", "V", "W", "X", "Y", "Z"]
BASE_URL = "https://eepublicdownloads.blob.core.windows.net/cio-lio/csv/{}_eicCodes.csv"

COLUMN_MAPPING = {
    "EicCode": "eic_code",
    "EicDisplayName": "display_name",
    "EicLongName": "long_name",
    "EicParent": "parent",
    "EicResponsibleParty": "responsible_party",
    "EicStatus": "status",
    "MarketParticipantPostalCode": "postal_code",
    "MarketParticipantIsoCountryCode": "country_code",
    "MarketParticipantVatCode": "vat_code",
    "EicTypeFunctionList": "function_list",
}


def parse_eic_codes(csv_text: str, code_type: str) -> pd.DataFrame:
    """Parses one ENTSO-E EIC csv (semicolon separated)."""
    df = pd.read_csv(StringIO(csv_text.lstrip("﻿")), sep=";", dtype=str)
    df = df.rename(columns=COLUMN_MAPPING)
    df = df[[c for c in COLUMN_MAPPING.values() if c in df.columns]]
    df = df.dropna(subset=["eic_code"])
    df["eic_code"] = df["eic_code"].str.strip()
    df["code_type"] = code_type
    return df


def build_eic_locations(powerplants: pd.DataFrame) -> pd.DataFrame:
    """One row per EIC code with the coordinates of the power plant it belongs to."""
    df = powerplants[["eic", "lat", "lon"]].dropna(subset=["lat", "lon"]).copy()
    df["eic_code"] = df["eic"].map(parse_eic_set)
    df = df.explode("eic_code").dropna(subset=["eic_code"])
    df = df[["eic_code", "lat", "lon"]].drop_duplicates(subset=["eic_code"])
    return df.reset_index(drop=True)


def download_eic_codes() -> pd.DataFrame:
    frames = []
    for code_type in EIC_TYPES:
        resp = requests.get(BASE_URL.format(code_type), timeout=120)
        resp.raise_for_status()
        frames.append(parse_eic_codes(resp.content.decode("utf-8-sig"), code_type))
    return pd.concat(frames, ignore_index=True)


class EicMappingCrawler(DownloadOnceCrawler):
    def structure_exists(self) -> bool:
        try:
            with self.engine.connect() as conn:
                has_codes = conn.execute(text("SELECT 1 from eic_codes limit 1"))
                has_locations = conn.execute(
                    text("SELECT 1 from eic_locations limit 1")
                )
                return has_codes.scalar() == 1 and has_locations.scalar() == 1
        except Exception:
            return False

    def crawl_structural(self, recreate: bool = False):
        if not self.structure_exists() or recreate:
            log.info("Downloading EIC code lists")
            codes = download_eic_codes()
            codes["downloaded_at"] = utcnow()
            powerplants = parse_powerplants(download_powerplants_csv())
            locations = build_eic_locations(powerplants)
            locations["downloaded_at"] = utcnow()
            with self.engine.begin() as conn:
                codes.to_sql("eic_codes", conn, if_exists="replace", index=False)
                locations.to_sql(
                    "eic_locations", conn, if_exists="replace", index=False
                )
                for table in ("eic_codes", "eic_locations"):
                    conn.execute(
                        text(
                            f"CREATE INDEX IF NOT EXISTS idx_{table}_eic_code "
                            f"ON {table} (eic_code)"
                        )
                    )
            log.info("Wrote %i EIC codes and %i locations", len(codes), len(locations))


if __name__ == "__main__":
    logging.basicConfig()

    config = load_config(DEFAULT_CONFIG_LOCATION)
    crawler = EicMappingCrawler("eic_mapping", config=config)
    crawler.crawl_structural(recreate=False)
