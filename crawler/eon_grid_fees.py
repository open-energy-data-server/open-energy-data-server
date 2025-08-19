# SPDX-FileCopyrightText: Florian Maurer, Christian Rieke
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import logging
from datetime import datetime

import pandas as pd
import requests
from dateutil.relativedelta import relativedelta
from geopy.geocoders import Nominatim
from sqlalchemy import text

from common.base_crawler import DownloadOnceCrawler, load_config

log = logging.getLogger(__name__)

metadata_info = {
    "schema_name": "eon_fees",
    "data_source": "https://www.eon.de/de/gk/strom/tarifrechner.html",
    "license": "EON restricted",
    "description": "Contract data of EON",
    "contact": "",
    "temporal_start": "2025-01-17",
}

EON_URI = "https://occ.eon.de/b2b-pricing/1.0/api/v2/offers"
GRID_FEES_URI = "https://occ.eon.de/b2b-pricing/1.0/api/v2/thirdPartyCosts/rlm/power"


def get_contract_data(address: dict):
    # we need to create an offer starting for the next month
    start = datetime.now() + relativedelta(day=1)
    start_str = start.strftime("%Y-%m-01")
    data = {
        "city": address.get("city"),
        "clientId": "eonde",
        "consumption": "100000",
        "division": "Strom",
        # "housenumber": address.get("house_number"),
        "post_code": address.get("postcode"),
        "profile": "NHO",
        "start_date": start_str,
        # "street": address.get('road'),
    }
    response = requests.post(EON_URI, json=data)
    assert response.status_code == 200, f"{response.status_code}: {response.text}"
    contract = response.json()
    return contract["price_details"]


def get_grid_data(address: dict):
    postcode = address.get(
        "postcode",
    )
    city = address.get("city", address.get("town"))
    if not city:
        city = requests.get(
            f"https://occ.eon.de/zipcodes/1.3/api?clientId=eonde&query={postcode}"
        ).json()
        city = city["zipCodes"][0]["cities"][0]["city"]

    street = requests.get(
        f"https://occ.eon.de/streets/1.3/api?clientId=eonde&zipCode={postcode}&streetName=a"
    ).json()[0]

    if not street:
        street = address.get("road")
    params = {
        "type": "Strom",
        "city": city,
        "consumption": 100000,
        "zipCode": address.get("postcode"),
        "street": street,
        # "houseNumber": address.get("house_number")
    }

    grid_response = requests.get(GRID_FEES_URI, params=params)
    assert grid_response.status_code == 200, (
        f"{grid_response.status_code}: {grid_response.text}"
    )
    return grid_response.json()


class EonGridFeeCrawler(DownloadOnceCrawler):
    def structure_exists(self) -> bool:
        try:
            query = text("SELECT 1 from eon_grid_fees limit 1")
            with self.engine.connect() as conn:
                return conn.execute(query).scalar() == 1
        except Exception:
            return False

    def crawl_structural(self, recreate: bool = False):
        if not self.structure_exists() or recreate:
            log.info("start download grid fees")
            self.download_grid_fees()
            log.info("finished download grid fees")

    def download_grid_fees(self):
        with self.engine.connect() as conn:
            plz_nuts = pd.read_sql_query(
                "select code, nuts3, longitude, latitude from public.plz",
                conn,
                index_col="code",
            )
        # Initialize Nominatim API
        geolocator = Nominatim(user_agent="Open-Energy-Data-Server")
        grid_fee_results = {}
        contracts_results = {}

        # code = 72516
        # row = plz_nuts.loc[code]
        # location is only based on nuts3, so it is hardly usefule to add so many
        plz_nuts = plz_nuts.drop_duplicates("nuts3")

        for code, row in plz_nuts.iterrows():
            latitude = row["latitude"]
            longitude = row["longitude"]
            print(f"currently working at {code}")

            # Perform reverse geocoding
            location = geolocator.reverse(f"{latitude}, {longitude}")
            address = location.raw["address"]
            # some location middles do not have a postcode set like
            # 57642
            address["postcode"] = address.get("postcode", str(code))
            try:
                contracts_results[code] = get_contract_data(address)
            except Exception:
                log.exception(f"error in contract fees eon for {code}")
            try:
                grid_fee_results[code] = get_grid_data(address)
            except Exception:
                log.exception(f"error in grid fees eon for {code}")

        df = pd.DataFrame()
        df["zip_code"] = pd.Series(grid_fee_results.keys()).values
        df["working_price_grid_ct_per_kwh"] = list(
            map(
                lambda i: i.get("prices").get("working_price_grid").get("value_vat"),
                map(grid_fee_results.get, grid_fee_results.keys()),
            )
        )
        df["power_price_grid_eur_per_kw"] = list(
            map(
                lambda i: i.get("prices").get("power_price_grid").get("value_vat"),
                map(grid_fee_results.get, grid_fee_results.keys()),
            )
        )
        df["fee_measurement_eur_per_year"] = list(
            map(
                lambda i: i.get("prices").get("fee_measurement").get("value_vat"),
                map(grid_fee_results.get, grid_fee_results.keys()),
            )
        )

        with self.engine.begin() as conn:
            df.to_sql("eon_grid_fees", conn)


if __name__ == "__main__":
    logging.basicConfig()
    from pathlib import Path

    config = load_config(Path(__file__).parent.parent / "config.yml")
    crawler = EonGridFeeCrawler("grid_fees", config)
    crawler.crawl_structural()
    crawler.set_metadata(metadata_info)

    # with open("grid_fees.json", "w") as f:
    #     json.dump(grid_fee_results, f, indent=2)

    # with open("contracts_results.json", "w") as f:
    #     json.dump(contracts_results, f, indent=2)
