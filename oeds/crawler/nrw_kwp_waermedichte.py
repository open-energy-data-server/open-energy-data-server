# SPDX-FileCopyrightText: Vassily Aliseyko
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import logging
import shutil
import zipfile
from pathlib import Path

import geopandas
import pyogrio
import requests
from sqlalchemy import text

from oeds.base_crawler import DEFAULT_CONFIG_LOCATION, DownloadOnceCrawler, load_config

log = logging.getLogger("iwu")
log.setLevel(logging.INFO)


metadata_info = {
    "schema_name": "nrw_kwp_waermedichte",
    "data_date": "2024-01-23",
    "data_source": "https://www.opengeodata.nrw.de/produkte/umwelt_klima/klima/kwp/",
    "license": "DL-DE-ZERO-2.0",
    "description": "NRW Building stats. Building specific information regarding buildings and heating, modelled",
    "contact": "",
    "temporal_start": None,
    "temporal_end": None,
}

KWP_URL = "https://www.opengeodata.nrw.de/produkte/umwelt_klima/energie/kwp/KWP-NRW-Waermebedarf_EPSG25832_Geodatabase.zip"
DATA_PATH = Path(__file__).parent.parent / "data" / "kwp_nrw"
CHUNK_ROWS = 100_000

# layer of the geodatabase -> table, the last one marks the crawl as complete
LAYERS = {
    "Gebaeude_Fortschreibung_moderat": "waermedichte",
    "Waermelinien": "waermelinien",
}


class KwpCrawler(DownloadOnceCrawler):
    def structure_exists(self) -> bool:
        try:
            query = text(f"SELECT 1 from {list(LAYERS.values())[-1]} limit 1")
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() == 1
        except Exception:
            return False

    def crawl_structural(self, recreate: bool = False, max_rows: int | None = None):
        """
        Args:
            max_rows: only import the first n rows of each layer (for tests)
        """
        if not self.structure_exists() or recreate:
            try:
                gdb = self.download_kwp_data()
                if gdb is None:
                    return
                for layer, table in LAYERS.items():
                    self.save_to_database(gdb, layer, table, max_rows)
            finally:
                self.clean()

    def download_kwp_data(self) -> Path | None:
        """Downloads and extracts the geodatabase (8 GB zip), returns the path of the .gdb."""
        # a previous run may have been aborted
        self.clean()
        DATA_PATH.mkdir(parents=True, exist_ok=True)
        archive = DATA_PATH / "kwp.zip"
        try:
            with requests.get(KWP_URL, stream=True, timeout=60) as response:
                response.raise_for_status()
                with open(archive, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8 * 1024 * 1024):
                        f.write(chunk)
        except requests.RequestException:
            log.exception(
                "Failed to download the KWP NRW data from %s, "
                "check https://www.opengeodata.nrw.de/produkte/umwelt_klima/energie/kwp/",
                KWP_URL,
            )
            return None
        log.info("Downloaded the KWP NRW ZIP file")
        with zipfile.ZipFile(archive) as z:
            z.extractall(DATA_PATH)
        archive.unlink()
        log.info("Extracted KWP NRW GDB")
        return next(DATA_PATH.glob("*.gdb"))

    def save_to_database(
        self, gdb: Path, layer: str, table: str, max_rows: int | None = None
    ):
        """Writes the layer to a staging table which replaces table once it is complete."""
        staging = f"{table}_staging"
        with self.engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {staging}"))
        total = pyogrio.read_info(gdb, layer=layer)["features"]
        if max_rows is not None:
            total = min(total, max_rows)
        for start in range(0, total, CHUNK_ROWS):
            data = geopandas.read_file(
                gdb,
                layer=layer,
                engine="pyogrio",
                skip_features=start,
                max_features=min(CHUNK_ROWS, total - start),
            )
            with self.engine.begin() as conn:
                # the postgis types live in public, which is not in the search_path
                schema = conn.execute(text("SELECT current_schema()")).scalar()
                conn.execute(text(f"SET LOCAL search_path TO {schema}, public"))
                data.to_postgis(
                    staging,
                    conn,
                    schema=schema,
                    if_exists="append",
                    chunksize=10000,
                )
            log.info("%s: %s of %s rows written", layer, start + len(data), total)

        with self.engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
            conn.execute(text(f"ALTER TABLE {staging} RENAME TO {table}"))

    def clean(self):
        shutil.rmtree(DATA_PATH, ignore_errors=True)


if __name__ == "__main__":
    logging.basicConfig()

    config = load_config(DEFAULT_CONFIG_LOCATION)
    crawler = KwpCrawler("nrw_kwp_waermedichte", config)
    crawler.crawl_structural()
    crawler.set_metadata(metadata_info)
