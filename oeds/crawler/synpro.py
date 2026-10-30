# SPDX-FileCopyrightText: Christoph Komanns
#
# SPDX-License-Identifier: AGPL-3.0-or-later
import logging
from io import BytesIO

import pandas as pd
import requests
from sqlalchemy import text

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    DownloadOnceCrawler,
    ensure_utc,
    load_config,
)

metadata_info = {
    "schema_name": "synpro_free_load_profiles",
    "data_date": "2024-01-01",
    "data_source": "https://synpro-lastprofile.de",
    "license": "CDLA-permissive 2.0",
    "description": """15 minütlich aufgelöste Zeitreihen für einen Zeitraum von einem Jahr für eine Auswahl an 4 verschiedenen Haushalten.
    In diesem Beispieldatensatz sind drei verschiedene Ordner zu finden aus denen verschiedene Gebäudekonstellationen erstellt werden können. Es gibt 4 elektrische Lastprofile mit den folgenden sozioökonomischen Faktoren:
        * Eine Familie mit zwei Eltern und zwei Kindern
        * Zwei Vollzeitarbeitende
        * Zwei Personen über 65 (Rentner*innen)
        * Eine Person unter 30
    Dazu gibt es Heizlastprofile sowie Trinkwarmwasserprofile für zwei Gebäude mit jeweils zwei verschiedenen Isolationsstandards:
        * Einfamilienhaus im Passivhausstandard
        * Einfamilienhaus mit Baujahr zwischen 1979 und 2001
        * Mehrfamilienhaus im Passivhausstandard
        * Mehrfamilienhaus mit Baujahr zwischen 1979 und 2001
    Die Profile können entweder einzeln verwendet oder kombiniert werden. Die empfohlenen Kombinationen sind:
        * Das elektrische Lastprofil für die Familie in Kombinationen mit dem Heiz- und Trinkwarmwasserprofil für ein beliebiges Einfamilienhaus
        * Alle elektrischen Lastprofile zusammen mit dem Heiz- und Trinkwarmwasserprofil für ein beliebiges Mehrfamilienhaus""",
    "contact": "komanns@fh-aachen.de",
    "temporal_start": "2020-12-31 23:00:00+00:00",
    "temporal_end": "2021-12-31 22:45:00+00:00",
    "concave_hull_geometry": None,
}


class SynproLoadProfileCrawler(DownloadOnceCrawler):
    def structure_exists(self):
        try:
            query = text(
                "SELECT 1 FROM synpro_free_load_profiles.electric_family LIMIT 1"
            )
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() == 1
        except Exception:
            return False

    def crawl_structural(self, recreate=False):
        if not self.structure_exists() or recreate:
            electric = self.crawl_electric()
            domestic_hot_water = self.crawl_dhw()
            heat = self.crawl_heat()

            self.write_to_database(dfs=electric, name="electric")
            self.write_to_database(dfs=domestic_hot_water, name="domestic_hot_water")
            self.write_to_database(dfs=heat, name="heat")

    def crawl_electric(self) -> dict[str, pd.DataFrame]:
        constellations = {}
        constellations["two_fulltime_employees"] = {
            "url": "https://oc.ise.fraunhofer.de/s/vmwmRdTPCo2Na1w/download?path=%2Felectric&files=synPRO_el_2_fulltime_employees.dat"
        }
        constellations["two_persons_over_65"] = {
            "url": "https://oc.ise.fraunhofer.de/s/vmwmRdTPCo2Na1w/download?path=%2Felectric&files=synPRO_el_2_persons_over65.dat"
        }
        constellations["family"] = {
            "url": "https://oc.ise.fraunhofer.de/s/vmwmRdTPCo2Na1w/download?path=%2Felectric&files=synPRO_el_family.dat"
        }
        constellations["single_person_under_30"] = {
            "url": "https://oc.ise.fraunhofer.de/s/vmwmRdTPCo2Na1w/download?path=%2Felectric&files=synPRO_el_single_person_under30.dat"
        }

        logging.info("Crawling electric loads")
        electric_dfs = {}
        for const, const_values in constellations.items():
            r = requests.get(const_values["url"])

            with BytesIO(r.content) as file:
                df = pd.read_csv(file, delimiter=";", header=8)
                electric_dfs[const] = self.create_datetime_col(df)

        return electric_dfs

    def crawl_dhw(self) -> dict[str, pd.DataFrame]:
        constellations = {}
        constellations["old_multi_party_house"] = {
            "url": "https://oc.ise.fraunhofer.de/s/vmwmRdTPCo2Na1w/download?path=%2Fdomestic_hot_water&files=synPRO_old_building_multi_party_house.dat"
        }
        constellations["old_single_family_house"] = {
            "url": "https://oc.ise.fraunhofer.de/s/vmwmRdTPCo2Na1w/download?path=%2Fdomestic_hot_water&files=synPRO_old_building_single_family_house.dat"
        }
        constellations["passive_multi_party_house"] = {
            "url": "https://oc.ise.fraunhofer.de/s/vmwmRdTPCo2Na1w/download?path=%2Fdomestic_hot_water&files=synPRO_passive_multi_party_house.dat"
        }
        constellations["passive_single_family_house"] = {
            "url": "https://oc.ise.fraunhofer.de/s/vmwmRdTPCo2Na1w/download?path=%2Fdomestic_hot_water&files=synPRO_passive_single_family_house.dat"
        }

        logging.info("Crawling domestic hot water")
        dhw_dfs = {}
        for const, const_values in constellations.items():
            r = requests.get(const_values["url"])

            with BytesIO(r.content) as file:
                df = pd.read_csv(file, delimiter=";", header=16)
                dhw_dfs[const] = self.create_datetime_col(df)

        return dhw_dfs

    def crawl_heat(self) -> dict[str, pd.DataFrame]:
        constellations = {}
        constellations["old_multi_party_house"] = {
            "url": "https://oc.ise.fraunhofer.de/s/vmwmRdTPCo2Na1w/download?path=%2Froom_heating&files=synPRO_old_building_multi_party_house.dat"
        }
        constellations["old_single_family_house"] = {
            "url": "https://oc.ise.fraunhofer.de/s/vmwmRdTPCo2Na1w/download?path=%2Froom_heating&files=synPRO_old_building_single_family_house.dat"
        }
        constellations["passive_multi_party_house"] = {
            "url": "https://oc.ise.fraunhofer.de/s/vmwmRdTPCo2Na1w/download?path=%2Froom_heating&files=synPRO_passive_multi_party_house.dat"
        }
        constellations["passive_single_family_house"] = {
            "url": "https://oc.ise.fraunhofer.de/s/vmwmRdTPCo2Na1w/download?path=%2Froom_heating&files=synPRO_passive_single_family_house.dat"
        }

        logging.info("Crawling heat")
        heat_dfs = {}
        for const, const_values in constellations.items():
            r = requests.get(const_values["url"])

            if "passive_single" in const_values["url"]:
                header = 19
            else:
                header = 23

            with BytesIO(r.content) as file:
                df = pd.read_csv(file, delimiter=";", header=header)
                df.drop(columns="Unnamed: 4", inplace=True)
                heat_dfs[const] = self.create_datetime_col(df)

        return heat_dfs

    def create_datetime_col(self, df: pd.DataFrame) -> pd.DataFrame:
        df["datetime"] = pd.to_datetime(df["unixtimestamp"], unit="s", utc=True)

        df.drop(columns=["YYYYMMDD", "hhmmss", "unixtimestamp"], inplace=True)

        return df

    def write_to_database(self, dfs: dict[str : pd.DataFrame], name: str) -> None:
        logging.info(f"Writing {name} data to database")
        for key, df in dfs.items():
            ensure_utc(df).to_sql(
                name=f"{name}_{key}",
                con=self.engine,
                if_exists="replace",
                index=False,
            )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    config = load_config(DEFAULT_CONFIG_LOCATION)
    synpro = SynproLoadProfileCrawler("synpro_free_load_profiles", config=config)
    synpro.crawl_structural(recreate=False)
    synpro.set_metadata(metadata_info=metadata_info)
