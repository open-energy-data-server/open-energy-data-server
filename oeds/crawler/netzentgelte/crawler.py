# SPDX-FileCopyrightText: Christoph Komanns
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
OEDS crawler for German electricity grid fees (Netzentgelte).

Unlike ``eon_grid_fees`` (which obtains grid fees indirectly via the E.ON
tariff calculator per postal code), this crawler parses the official price
sheets of distribution system operators (VNB) and the nationwide transmission
grid price sheet directly and normalises them into a unified schema (one row =
city x grid operator x voltage level x price component).

The domain logic comes from the ``netzentgelte`` tool and is vendored here as
the ``oeds.crawler.netzentgelte`` subpackage so that OEDS remains independently
installable and runnable.

This is structural, annually published data (price sheets apply per calendar
year), hence a ``DownloadOnceCrawler``. Re-running with ``recreate=True`` or for
a new ``jahr`` overwrites the table.

IMPORTANT: The crawler requires real internet access. For a new year, update
the price sheet URLs in ``cities.yaml`` and the transmission grid URL in
``fetch_uebertragungsnetz.py`` (same maintenance as in the upstream tool).
"""

import logging

import pandas as pd
from sqlalchemy import text

from oeds.base_crawler import DEFAULT_CONFIG_LOCATION, DownloadOnceCrawler, load_config
from oeds.crawler.netzentgelte.collect import DEFAULT_CITIES_YAML, collect_entries
from oeds.crawler.netzentgelte.fetch_uebertragungsnetz import DEFAULT_UENB_PDF_URL
from oeds.crawler.netzentgelte.schema import CSV_FIELDS

log = logging.getLogger("netzentgelte")
log.setLevel(logging.INFO)

#: Validity year of the vendored price sheets (cities.yaml / transmission URL are 2026).
DEFAULT_JAHR = 2026

TABLE_NAME = "netzentgelte"

metadata_info = {
    "schema_name": "netzentgelte",
    "data_date": f"{DEFAULT_JAHR}-01-01",
    "data_source": (
        "https://www.netztransparenz.de (transmission grid, nationwide) "
        "and the price sheets of the respective distribution system operators "
        "(see cities.yaml)"
    ),
    "license": (
        "Price sheets of the respective grid operators are freely available "
        "under § 28 StromNEV; redistribution rules vary by operator."
    ),
    "description": (
        "Electricity grid fees per city/distribution operator: household customers "
        "(SLP, base price/energy price) and higher voltage levels "
        "(RLM, annual/monthly capacity price), plus nationwide transmission "
        "grid fees. Net and gross values per price component."
    ),
    "contact": "komanns@fh-aachen.de",
    "temporal_start": f"{DEFAULT_JAHR}-01-01",
    "temporal_end": f"{DEFAULT_JAHR}-12-31",
    "concave_hull_geometry": None,
}


class NetzentgeltCrawler(DownloadOnceCrawler):
    """Downloads electricity grid fees and writes them to the ``netzentgelte``
    table in the schema of the same name."""

    #: Override before the run via ``crawler.jahr = ...``.
    jahr: int = DEFAULT_JAHR

    def structure_exists(self) -> bool:
        try:
            query = text(f"SELECT 1 FROM {TABLE_NAME} LIMIT 1")
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() == 1
        except Exception:
            return False

    def crawl_structural(self, recreate: bool = False):
        if not self.structure_exists() or recreate:
            log.info("start download netzentgelte (year %s)", self.jahr)
            entries, warnings = collect_entries(
                cities_yaml=DEFAULT_CITIES_YAML,
                jahr=self.jahr,
                uenb_url=DEFAULT_UENB_PDF_URL,
            )
            for warning in warnings:
                log.warning(warning)

            if not entries:
                raise RuntimeError(
                    "No grid fee entries collected - likely no network access. "
                    "Warnings: " + "; ".join(warnings)
                )

            df = pd.DataFrame([e.as_dict() for e in entries], columns=CSV_FIELDS)
            with self.engine.begin() as conn:
                df.to_sql(TABLE_NAME, conn, if_exists="replace", index=False)
            log.info(
                "finished netzentgelte: %d rows written (%d warning(s))",
                len(df),
                len(warnings),
            )


if __name__ == "__main__":
    logging.basicConfig()

    config = load_config(DEFAULT_CONFIG_LOCATION)
    crawler = NetzentgeltCrawler("netzentgelte", config)
    crawler.crawl_structural()
    crawler.set_metadata(metadata_info)
