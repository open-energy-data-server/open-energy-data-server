# SPDX-FileCopyrightText: Bing Zhe Puah
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import logging

import pandas as pd
import requests
from bs4 import BeautifulSoup
from sqlalchemy import text

from oeds.base_crawler import DEFAULT_CONFIG_LOCATION, DownloadOnceCrawler, load_config

log = logging.getLogger("iwu")
log.setLevel(logging.INFO)

metadata_info = {
    "schema_name": "fernwaerme_preisuebersicht",
    "data_date": "2024-10-16",
    "data_source": "https://waermepreise.info",
    "license": "https://www.waermepreise.info/impressum/",
    "license_short": "nur für private Nutzung, nicht für kommerzielle Zwecke",
    "description": "Fernwärme Preisübersicht.",
    "contact": "aliseyko@fh-aachen.de",
    "temporal_start": "2022-01-01",
    "temporal_end": "2024-01-04",
    "concave_hull_geometry": None,
}


def parse_network_size(value) -> tuple[str | None, str | None]:
    """Splits the network size in MW into (min, max) as strings.

    Handles "bis 5", "5 - 10" and "größer als 50"; max is None for the last one.
    """
    if not isinstance(value, str):
        return None, None
    text = value.replace("MW", "").replace(",", ".").replace("\xad", "").strip()
    lowered = text.lower()
    if lowered.startswith("bis"):
        return "0", text[3:].strip()
    if "größer" in lowered:
        return lowered.replace("größer", "").replace("als", "").strip(), None
    if "-" in text:
        low, high = text.split("-", 1)
        return low.strip(), high.strip()
    return text, text


class FWCrawler(DownloadOnceCrawler):
    def structure_exists(self) -> bool:
        try:
            query = text("SELECT 1 from fernwaerme_preisuebersicht limit 1")
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() == 1
        except Exception:
            return False

    def crawl_structural(self, recreate: bool = False):
        if not self.structure_exists() or recreate:
            log.info("downloading fernwaerme data")
            data = self.download_waerme_preise()
            with self.engine.begin() as conn:
                tbl_name = "fernwaerme_preisuebersicht"
                data.to_sql(tbl_name, conn, if_exists="replace")
            log.info("finished writing fernwaerme data")

    def download_waerme_preise(self):
        url = "https://waermepreise.info"
        response = requests.get(url)
        soup = BeautifulSoup(response.content, "html.parser")

        table = soup.find("table")

        if not table:
            raise Exception("no html table found")
        headers = [header.text.strip() for header in table.find_all("th")]

        rows = []
        for row in table.find_all("tr")[1:]:
            rows.append([cell.text.strip() for cell in row.find_all("td")])

        df = pd.DataFrame(rows, columns=headers)

        def normalize_col(col):
            return " ".join(col.split()).replace("\xa0", " ")

        df.columns = [normalize_col(col) for col in df.columns]
        # print("Normalized columns:", df.columns.tolist())

        for col in [
            "EFH in ct/kWh (brutto) EFH in ct/kWh = Einfamilienhaus Abnahmefall 15 kW (27.000 kWh))",
            "MFH in ct/kWh (brutto) MFH in ct/kWh = Mehrfamilienhaus Abnahmefall 160 kW (288.000 kWh)",
            "Industrie in ct/kWh (brutto) Industrie in ct/kWh = Industrie bzw. Gewerbe/Handel/Dienstleistungen Abnahmefall 600 kW (1.080.000 kWh)",
        ]:
            df[col] = df[col].str.replace(",", ".").str.strip()
            df[col] = pd.to_numeric(df[col], errors="coerce")

        column_name5 = "Netzverluste in MWh/a Netzverluste werden bestimmt durch - Netzlänge - Abnahmedichte - Netztemperatur"
        df[column_name5] = df[column_name5].str.replace(",", ".").str.strip()
        df[column_name5] = pd.to_numeric(df[column_name5], errors="coerce")

        column_name6 = "Netzverluste Netzverluste werden bestimmt durch - Netzlänge - Abnahmedichte - Netztemperatur"
        df[column_name6] = (
            df[column_name6].str.replace("%", "").str.replace(",", ".").str.strip()
        )
        df[column_name6] = pd.to_numeric(df[column_name6], errors="coerce")

        column_name8 = "Anteil KWK KWK (Kraft-Wärme-Kopplung) gleichzeitige Strom- und Wärmeerzeugung besonders effiziente Ausnutzung des eingesetzten Brennstoffs"
        df[column_name8] = (
            df[column_name8].str.replace("%", "").str.replace(",", ".").str.strip()
        )
        df[column_name8] = pd.to_numeric(df[column_name8], errors="coerce")

        column_name9 = "PEF Glossar"
        df[column_name9] = df[column_name9].str.replace(",", ".").str.strip()
        df[column_name9] = pd.to_numeric(df[column_name9], errors="coerce")

        column_name4 = "Netzgröße in MW Netzgröße nach Höhe der angeschlossenen Wärme­erzeugungs­leistung"
        sizes = df[column_name4].apply(parse_network_size)
        df["Min Netzgröße"] = pd.to_numeric(sizes.str[0], errors="coerce")
        # no upper limit ("größer als x") is stored as NULL
        df["Max Netzgröße"] = pd.to_numeric(sizes.str[1], errors="coerce")

        column_name7 = "Anteil EE & KE erneuerbare Energieträger und Abwärme gemäß § 3 WPG (Wärmeplanungsgesetz)"
        df[["Min EE & KN", "Max EE & KN"]] = (
            df[column_name7].str.split("-", n=1, expand=True).reindex(columns=[0, 1])
        )
        df["Min EE & KN"] = df["Min EE & KN"].str.replace(",", ".").str.strip()
        df["Min EE & KN"] = df[column_name7].apply(
            lambda x: (
                None
                if (isinstance(x, str) and ("bis" in x or x.strip().startswith("<")))
                else x.split("-")[0].strip()
            )
        )
        df["Max EE & KN"] = df[column_name7].apply(
            lambda x: (
                x.replace("bis", "").strip()
                if "bis" in x
                else x.strip()[1:].strip()
                if (isinstance(x, str) and x.strip().startswith("<"))
                else x.split("-")[-1].strip()
            )
        )
        df["Max EE & KN"] = (
            df["Max EE & KN"].str.replace("%", "").str.replace(",", ".").str.strip()
        )
        df["Min EE & KN"] = pd.to_numeric(df["Min EE & KN"], errors="coerce")
        df["Max EE & KN"] = pd.to_numeric(df["Max EE & KN"], errors="coerce")
        # df['Min EE & KN'].fillna('-', inplace=True)
        # df['Max EE & KN'].fillna('-', inplace=True)
        df["Preisstand Datum der letzten Preisanpassung"] = pd.to_datetime(
            df["Preisstand Datum der letzten Preisanpassung"], errors="coerce"
        )

        df.drop(columns=[column_name4, column_name7], inplace=True)

        return df


if __name__ == "__main__":
    logging.basicConfig()

    config = load_config(DEFAULT_CONFIG_LOCATION)
    craw = FWCrawler("fernwaerme_preisuebersicht", config)
    craw.crawl_structural(recreate=False)
