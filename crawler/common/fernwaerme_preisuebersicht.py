# SPDX-FileCopyrightText: Bing Zhe Puah
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import io
import logging
import zipfile
import pandas as pd
import requests
from bs4 import BeautifulSoup
import numpy as np


from common.base_crawler import BaseCrawler

log = logging.getLogger("iwu")
log.setLevel(logging.INFO)


metadata_info = {
    "schema_name": "fernwaerme_preisuebersicht",
    "data_date": "2024-10-16",
    "data_source": "https://waermepreise.info/preisuebersicht/",
    "license": "third party usage allowed",
    "description": "Fernwärme Preisübersicht.",
    "contact": "aliseyko@fh-aachen.de",
    "temporal_start": "2022-01-01",
    "temporal_end": "2024-01-04",
    "concave_hull_geometry": None,
}


class FWCrawler(BaseCrawler):
    def __init__(self, schema_name):
        super().__init__(schema_name)

    def pull_data(self):
        url = "https://waermepreise.info/preisuebersicht/"
        response = requests.get(url)
        soup = BeautifulSoup(response.content, 'html.parser')

        table = soup.find('table')
        headers = [header.text.strip() for header in table.find_all('th')]

        rows = []
        for row in table.find_all('tr')[1:]:
            rows.append([cell.text.strip() for cell in row.find_all('td')])

        df = pd.DataFrame(rows, columns=headers)

        column_name1 = 'EFH in ct/kWh\nEinfamilienhaus \nAbnahmefall 15 kW \n(27.000 kWh)'
        df[column_name1] = df[column_name1].str.replace(',', '.').str.strip()
        df[column_name1] = pd.to_numeric(df[column_name1], errors='coerce')

        column_name2 = 'MFH in ct/kWh\nMehrfamilienhaus\nAbnahmefall 160 kW \n(288.000 kWh)'
        df[column_name2] = df[column_name2].str.replace(',', '.').str.strip()
        df[column_name2] = pd.to_numeric(df[column_name2], errors='coerce')

        column_name3 = 'Industrie in ct/kWh\nIndustrie / Gewerbe\nAbnahmefall 600 kW \n(1.080.000 kWh)'
        df[column_name3] = df[column_name3].str.replace(',', '.').str.strip()
        df[column_name3] = pd.to_numeric(df[column_name3], errors='coerce')

        column_name5 = 'Verluste in MWh\nNetzverluste werden bestimmt durch\n- Netzlänge\n- Abnahmedichte\n- Netztemperatur'
        df[column_name5] = df[column_name5].str.replace(',', '.').str.strip()
        df[column_name5] = pd.to_numeric(df[column_name5], errors='coerce')

        column_name6 = 'Verluste in %/a\nNetzverluste werden bestimmt durch\n- Netzlänge\n- Abnahmedichte\n- Netztemperatur'
        df[column_name6] = df[column_name6].str.replace('%', '.').str.replace(',', '').str.strip()
        df[column_name6] = pd.to_numeric(df[column_name6], errors='coerce')

        column_name8 = 'KWK Anteil\nKWK (Kraft-Wärme-Kopplung) \ngleichzeitige Strom- und Wärmeerzeugung\nbesonders effiziente Ausnutzung des eingesetzten Brennstoffs'
        df[column_name8] = df[column_name8].str.replace('%', '.').str.replace(',', '').str.strip()
        df[column_name8] = pd.to_numeric(df[column_name8], errors='coerce')

        column_name9 = 'PEF\n Glossar'
        df[column_name9] = df[column_name9].str.replace(',', '.').str.strip()
        df[column_name9] = pd.to_numeric(df[column_name9], errors='coerce')

        column_name4 = 'Netzgröße\nNetzgröße nach Höhe der angeschlossenen Wärme­erzeugungs­leistung'
        df[['Min Netzgröße', 'Max Netzgröße']] = df[column_name4].str.split('-', expand=True)

        df['Min Netzgröße'] = df.apply(lambda row: 0 if str(row['Min Netzgröße']).startswith('b') else row['Min Netzgröße'], axis=1)
        df['Max Netzgröße'] = df.apply(lambda row: row['Max Netzgröße'] if str(row['Max Netzgröße']).startswith('b') else row['Max Netzgröße'], axis=1)

        df['Min Netzgröße'] = df[column_name4].apply(lambda x: None if 'größer' in x else x.split('-')[0].strip())
        df['Max Netzgröße'] = df[column_name4].apply(lambda x:  x.replace('bis', '').strip()if 'bis' in x else x.split('-')[-1].strip())
        df['Max Netzgröße'] = df['Max Netzgröße'].apply(lambda x: 'unlimited' if str().startswith('g') else x)

        df['Min Netzgröße'] = df['Min Netzgröße'].str.replace(',', '.').str.replace('MW', '').str.strip()
        df['Min Netzgröße'] = pd.to_numeric(df['Min Netzgröße'], errors='coerce')
        df['Max Netzgröße'] = pd.to_numeric(df['Max Netzgröße'], errors='coerce')

        df['Min Netzgröße'] = df[column_name4].apply(
            lambda x: 0 if 'bis' in x else (x if 'größer' in x else x.split('-')[0].strip())
            .replace('größer', '').replace('MW', '').strip()
        )                                           
        df['Max Netzgröße'] = df[column_name4].apply(
            lambda x: x if 'bis' in x else ('unlimited' if 'größer' in x else x.split('-')[-1].strip())
        )

        df['Max Netzgröße'] = df['Max Netzgröße'].str.replace('MW', '').str.replace(',', '.').str.replace('bis', '').str.strip()

        column_name7 = 'EE & KN\nAnteil Erneuerbarer und klimaneutraler Energieträger'
        df[['Min EE & KN', 'Max EE & KN']] = df[column_name7].str.split('-', expand=True)
        df['Min EE & KN'] = df['Min EE & KN'].str.replace(',', '.').str.strip()

        df['Min EE & KN'] = df[column_name7].apply(lambda x: None if 'bis' in x else x.split('-')[0].strip())
        df['Max EE & KN'] = df[column_name7].apply(lambda x: x.replace('bis', '').strip() if 'bis' in x else x.split('-')[-1].strip())
        df['Max EE & KN'] = df['Max EE & KN'].str.replace('%', '').str.replace(',', '.').str.strip()
        df['Min EE & KN'] = pd.to_numeric(df['Min EE & KN'], errors='coerce')
        df['Max EE & KN'] = pd.to_numeric(df['Max EE & KN'], errors='coerce')
        df['Min EE & KN'].fillna('-', inplace=True)
        df['Max EE & KN'].fillna('-', inplace=True)

        df['Preisstand\nDatum der letzten Preisanpassung'] = pd.to_datetime(df['Preisstand\nDatum der letzten Preisanpassung'], errors='coerce')

        df.drop(columns=[column_name4, column_name7], inplace=True)

        return df

    def write_to_sql(self, data):
        with self.engine.begin() as conn:
            tbl_name = "fernwaerme_preisuebersicht"
            data.to_sql(tbl_name, conn, if_exists="replace")

def main(schema_name):
    iwu = FWCrawler(schema_name)
    data = iwu.pull_data()
    iwu.write_to_sql(data)


if __name__ == "__main__":
    main("fernwaerme_preisuebersicht")
