# SPDX-FileCopyrightText: Florian Maurer, Christian Rieke
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
The [ene't](https://download.enet.eu/) is a data provider for energy data.
If an institut has bought the data, it receives it as XLSX and CSV.
The latter can be used to create a database from it.

It contains aggregated usage data of so-called Bilanzkreise in the past.

The resulting data is not available under an open-source license and should not be reshared but is available for crawling yourself.
"""

import glob
import logging
import os.path as osp

import pandas as pd
from sqlalchemy import create_engine, text

from common.config import db_uri

log = logging.getLogger("MaStR")
log.setLevel(logging.INFO)


def get_keys_from_export(connection):
    keys = {i: {"nsp": [], "msp": [], "hsp": []} for i in range(1, 100)}
    grids = {"nsp": [], "msp": [], "hsp": []}
    vals = {"nsp": "netz_nsp", "msp": "netz_nr_msp", "hsp": "netz_nr_hsp"}

    for i in range(1, 100):
        start = i * 1000
        end = start + 1000

        for voltage in ["nsp", "msp", "hsp"]:
            query = (
                f"SELECT distinct (no.ortsteil, no.ort, {vals[voltage]}) "
                f"FROM netze_ortsteile no where plz >= {start} and plz < {end} "
                f"and no.gueltig_bis = '2100-01-01 00:00:00'"
            )

            df = pd.read_sql(query, connection)
            if not df.empty:
                for _, series in df.iterrows():
                    x = tuple(map(str, series.values[0][1:-1].split(",")))
                    grid_id = int(x[-1])
                    if grid_id not in grids[voltage]:
                        keys[i][voltage].append(grid_id)
                        grids[voltage].append(grid_id)
    return keys


def init_database(connection, database):
    query = text(f"DROP DATABASE IF EXISTS {database}")
    connection.execution_options(isolation_level="AUTOCOMMIT").execute(query)

    query = text(f"CREATE DATABASE {database}")
    connection.execution_options(isolation_level="AUTOCOMMIT").execute(query)

    log.info("initialize database")


enet_path = osp.join(osp.dirname(__file__), "enet")


def create_db_from_export(connection, enet_path):
    for table in glob.glob(enet_path + "/*.csv"):
        df = pd.read_csv(table, sep=";", encoding="cp1252", decimal=",")
        df.columns = [x.lower() for x in df.columns]
        date_fields = [
            "stand",
            "von",
            "bis",
            "gueltig_seit",
            "gueltig_bis",
            "datum_erfassung",
            "datum_aenderung",
            "letzte_pruefung",
            "letzte_aenderung",
            "ersterfassung",
            "aenderungsdatum",
        ]
        for field in date_fields:
            if field in df.columns:
                # df[field] = df[field].replace('2999-12-31 00:00:00', '2100-01-01 00:00:00')
                df[field] = pd.to_datetime(df[field], cache=True, errors="coerce")
                df[field] = df[field].fillna(pd.to_datetime("2100-01-01"))
        table_name = table.split("1252_")[-1][:-4]
        log.info(table_name)
        df.to_sql(table_name.lower(), connection, if_exists="append", index=False)


def main(db_uri):
    engine = create_engine(db_uri)
    create_db_from_export(engine)


if __name__ == "__main__":
    logging.basicConfig()
    main(db_uri("enet"))
