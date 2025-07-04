# SPDX-FileCopyrightText: Florian Maurer
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
The Joint Research Centre's Integrated Database of the European Energy System (JRC-IDEES) compiles a rich set of information allowing for highly granular analyses of the dynamics of the European energy system, so as to better understand the past and create a robust basis for future policy assessments.
https://data.jrc.ec.europa.eu/dataset/82322924-506a-4c9a-8532-2bdd30d69bf5

This cralwer should be run once - the schema needs to be removed if run again.
"""

import io
import logging
import zipfile

import pandas as pd
import requests
from sqlalchemy import create_engine, text

from common.base_crawler import create_schema_only, set_metadata_only
from common.config import db_uri

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

metadata_info = {
    "schema_name": "jrc_idees",
    "data_source": "https://data.jrc.ec.europa.eu/dataset/82322924-506a-4c9a-8532-2bdd30d69bf5",
    "license": "CC-BY-4.0",
    "description": "Joint Research Centre's Integrated Database of the European Energy System (JRC-IDEES) compiles a rich set of information allowing for highly granular analyses of the dynamics of the European energy system",
    "contact": "",
    "temporal_start": "2000-01-01",
}

JRC_IDEES_URL = "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/JRC-IDEES/JRC-IDEES-2021_v1/JRC-IDEES-2021.zip"


def main(schema_name):
    schema_name = "jrc_idees"
    engine = create_engine(db_uri(schema_name), pool_pre_ping=True)
    create_schema_only(engine, schema_name)
    log.info("Download JRC-IDEES dataset")
    response = requests.get(JRC_IDEES_URL)
    log.info("Write JRC-IDEES dataset to database")
    table_names = []
    with zipfile.ZipFile(io.BytesIO(response.content)) as thezip:
        for zipinfo in thezip.infolist():
            try:
                with thezip.open(zipinfo) as thefile:
                    # zipinfo = name?
                    zone = thefile.name.split("/")[0]
                    log.info(thefile.name)
                    if "xls" not in thefile.name:
                        continue
                    xl = pd.ExcelFile(thefile)
                    for sheet_name in xl.sheet_names:
                        if sheet_name in ["index", "cover", "RES_hh_eff"]:
                            continue
                        index_col = [0]
                        if (
                            "EmissionBalance" in thefile.name
                            or "EnergyBalance" in thefile.name
                        ):
                            index_col = [0, 1]

                        table_names.append(sheet_name)
                        df = xl.parse(sheet_name, index_col=index_col)
                        df.dropna(how="all", axis=1, inplace=True)
                        df.columns = [
                            col.strip() if isinstance(col, str) else col
                            for col in df.columns
                        ]
                        df = df[~df.index.duplicated(keep="first")]
                        df = df.T
                        df.index = pd.to_datetime(
                            df.index, format="%Y", errors="coerce"
                        )
                        df.index.name = "year"
                        if len(index_col) > 1:
                            df.columns = df.columns.map("_".join).map(
                                lambda x: x.strip("_")
                            )
                        splitted = thefile.name.split(".")[0].split("_")
                        zone = splitted[-1]
                        # the middle part is the section - might be more than one word
                        section = "_".join(splitted[1:-1])
                        # insert as to leftmost columns
                        df.insert(0, "zone", zone)

                        if df.empty:
                            continue
                        table_name = f"{section}_{sheet_name}".lower()
                        with engine.begin() as conn:
                            df.to_sql(table_name, conn, if_exists="append")
            except Exception as e:
                log.error(f"Error: {e} - {zipinfo}")
    log.info("Finished writing JRC-IDEES dataset to Database")

    try:
        for table in table_names:
            query = text(
                f"select public.create_hypertable('{schema_name}.{table}', 'year', if_not_exists => TRUE, migrate_data => TRUE)"
            )
            with engine.begin() as conn:
                conn.execute(query)
        log.error("successfully created hypertable for jrc_idees")
    except Exception:
        log.error("could not create hypertable for jrc_idees")

    set_metadata_only(engine, metadata_info)


if __name__ == "__main__":
    logging.basicConfig()
    main("jrc_idees")
