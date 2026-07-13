# SPDX-FileCopyrightText: Florian Maurer, Jonathan Sejdija, OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import csv
import logging
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from sqlalchemy import text

from oeds.base_crawler import DEFAULT_CONFIG_LOCATION, DownloadOnceCrawler, load_config

log = logging.getLogger("kelmarsh")
log.setLevel(logging.INFO)

metadata_info = {
    "schema_name": "kelmarsh",
    "data_date": "2025-08-11",
    "data_source": "https://zenodo.org/records/16807551",
    "license": "CC-BY-4.0",
    "description": "Kelmarsh wind farm SCADA data. High-resolution time-indexed turbine performance metrics.",
    "contact": "",
    "temporal_start": "2023-01-01 00:00:00",
    "temporal_end": "2023-12-31 23:50:00",
    "concave_hull_geometry": None,
}

SCADA_ZIP_FILENAME = "Kelmarsh_SCADA_2023_5961.zip"
SCADA_ZIP_URL = (
    "https://zenodo.org/records/16807551/files/"
    f"{SCADA_ZIP_FILENAME}?download=1"
)
scada_zip_path = Path(__file__).parent.parent / SCADA_ZIP_FILENAME


class KelmarshCrawler(DownloadOnceCrawler):
    def structure_exists(self) -> bool:
        try:
            query = text("SELECT 1 from scada limit 1")
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() == 1
        except Exception:
            return False

    def create_hypertable_if_not_exists(self):
        self.create_single_hypertable_if_not_exists("scada", "datetime")

    def crawl_structural(self, recreate: bool = False):
        if not self.structure_exists() or recreate:
            # Drop table if recreate
            if recreate:
                try:
                    with self.engine.begin() as conn:
                        conn.execute(text("DROP TABLE IF EXISTS scada"))
                except Exception as e:
                    log.warning(f"Could not drop table: {e}")

            self.load_scada_data()

        self.create_hypertable_if_not_exists()
        if self.engine.url.drivername.startswith("postgresql"):
            self.set_metadata(metadata_info)

    def download_scada_archive(self, zip_path: Path = scada_zip_path) -> Path:
        if zip_path.is_file():
            log.info("%s already exists", zip_path)
            return zip_path

        log.info("Downloading Kelmarsh SCADA archive from %s", SCADA_ZIP_URL)
        response = requests.get(SCADA_ZIP_URL, stream=True)
        response.raise_for_status()

        with zip_path.open("wb") as archive:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    archive.write(chunk)

        log.info("Downloaded Kelmarsh SCADA archive to %s", zip_path)
        return zip_path

    def load_scada_data(self):
        zip_path = self.download_scada_archive()

        turbines = {
            'KWF1': 'Turbine_Data_Kelmarsh_1_2023-01-01_-_2024-01-01_228.csv',
            'KWF2': 'Turbine_Data_Kelmarsh_2_2023-01-01_-_2024-01-01_229.csv',
            'KWF3': 'Turbine_Data_Kelmarsh_3_2023-01-01_-_2024-01-01_230.csv',
            'KWF4': 'Turbine_Data_Kelmarsh_4_2023-01-01_-_2024-01-01_231.csv',
            'KWF5': 'Turbine_Data_Kelmarsh_5_2023-01-01_-_2024-01-01_232.csv',
            'KWF6': 'Turbine_Data_Kelmarsh_6_2023-01-01_-_2024-01-01_233.csv'
        }

        log.info(f"Loading Kelmarsh SCADA data from {zip_path}...")

        chunk_size = 50000

        with zipfile.ZipFile(zip_path) as z:
            for t_name, csv_filename in turbines.items():
                log.info(f"Processing {t_name} SCADA file: {csv_filename}...")
                
                rows_accumulator = []
                
                with z.open(csv_filename) as f:
                    # Stream CSV line-by-line to avoid unzipping large files to disk
                    csv_reader = csv.reader((line.decode('utf-8') for line in f))
                    
                    for row in csv_reader:
                        if not row or row[0].startswith('#'):
                            continue
                        
                        try:
                            # Parse columns:
                            # 0: Date and time
                            # 1: Wind speed (m/s)
                            # 27: Energy Export (kWh)
                            # 62: Power (kW)
                            timestamp_str = row[0]
                            wind_speed_str = row[1]
                            energy_export_str = row[27]
                            power_kw_str = row[62]
                            
                            # Filter out duplicate rows where active power is NaN
                            if not power_kw_str or power_kw_str == 'NaN':
                                continue
                            
                            dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                            wind_speed = float(wind_speed_str) if wind_speed_str and wind_speed_str != 'NaN' else None
                            energy_export = float(energy_export_str) if energy_export_str and energy_export_str != 'NaN' else 0.0
                            power_kw = float(power_kw_str)
                            
                            rows_accumulator.append({
                                'datetime': dt,
                                'turbine': t_name,
                                'power': power_kw,
                                'wind_speed': wind_speed,
                                'energy_export': energy_export
                            })
                            
                            # Write in chunks to the database
                            if len(rows_accumulator) >= chunk_size:
                                df = pd.DataFrame(rows_accumulator)
                                with self.engine.begin() as conn:
                                    df.to_sql("scada", con=conn, if_exists="append", index=False)
                                log.info(f"  Inserted chunk of {len(df)} records for {t_name}")
                                rows_accumulator = []
                                
                        except Exception as e:
                            continue
                
                # Write remaining rows
                if rows_accumulator:
                    df = pd.DataFrame(rows_accumulator)
                    with self.engine.begin() as conn:
                        df.to_sql("scada", con=conn, if_exists="append", index=False)
                    log.info(f"  Inserted final chunk of {len(df)} records for {t_name}")

        # Create indexes
        log.info("Creating index on scada (datetime, turbine)...")
        with self.engine.begin() as conn:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_scada_datetime_turbine ON scada (datetime, turbine)"))
        
        log.info("Kelmarsh SCADA data loaded successfully.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    config = load_config(DEFAULT_CONFIG_LOCATION)
    crawler = KelmarshCrawler("kelmarsh", config)
    crawler.crawl_structural(recreate=True)
