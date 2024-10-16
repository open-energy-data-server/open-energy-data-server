# SPDX-FileCopyrightText: Vassily Aliseyko
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import io
import logging
import zipfile
import pandas as pd
import requests
from bs4 import BeautifulSoup

from common.base_crawler import BaseCrawler

log = logging.getLogger("iwu")
log.setLevel(logging.INFO)


metadata_info = {
    "schema_name": "fernwaerme_preisuebersicht",
    "data_date": "2015-02-10",
    "data_source": "https://waermepreise.info/preisuebersicht/",
    "license": "third party usage allowed",
    "description": "Fernwärme Preisübersicht.",
    "contact": "aliseyko@fh-aachen.de",
    "temporal_start": None,
    "temporal_end": None,
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
