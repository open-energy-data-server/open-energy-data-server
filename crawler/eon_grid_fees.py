# SPDX-FileCopyrightText: Florian Maurer, Christian Rieke
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import logging

import pandas as pd
import requests
from geopy.geocoders import Nominatim
from sqlalchemy import create_engine

from common.base_crawler import create_schema_only, set_metadata_only
from common.config import db_uri

logging.basicConfig()
log = logging.getLogger("MaStR")
log.setLevel(logging.INFO)

metadata_info = {
    "schema_name": "eon_fees",
    "data_source": "https://www.eon.de/de/gk/strom/tarifrechner.html",
    "license": "EON restricted",
    "description": "Contract data of EON",
    "contact": "",
    "temporal_start": "2025-01-17",
}

DB_URI = "postgresql://readonly:readonly@timescale.nowum.fh-aachen.de:5432/opendata"

engine = create_engine(DB_URI, pool_pre_ping=True)

EON_URI = "https://occ.eon.de/b2b-pricing/1.0/api/v2/offers"
GRID_FEES_URI = "https://occ.eon.de/b2b-pricing/1.0/api/v2/thirdPartyCosts/rlm/power"


def get_contract_data(address: dict):
    data = {
        "city": address.get("city"),
        "clientId": "eonde",
        "consumption": "100000",
        "division": "Strom",
        # "housenumber": address.get("house_number"),
        "post_code": address.get("postcode"),
        "profile": "NHO",
        "start_date": "2025-03-01",
        # "street": address.get('road'),
    }
    response = requests.post(EON_URI, json=data)
    assert response.status_code == 200, f"{response.status_code}: {response.text}"
    contract = response.json()
    return contract["price_details"]


def get_grid_data(address: dict):
    params = {
        "type": "Strom",
        "city": address.get("city", address.get("town")),
        "consumption": 100000,
        "zipCode": address.get("postcode"),
        "street": address.get("road"),
        # "houseNumber": address.get("house_number")
    }

    grid_response = requests.get(GRID_FEES_URI, params=params)
    assert grid_response.status_code == 200, f"{grid_response.status_code}: {grid_response.text}"
    return grid_response.json()


with engine.begin() as conn:
    plz_nuts = pd.read_sql_query(
        "select code, nuts3, longitude, latitude from plz",
        conn,
        index_col="code",
    )


# Initialize Nominatim API
geolocator = Nominatim(user_agent="Open-Energy-Data-Server")
grid_fee_results = {}
contracts_results = {}

for code, row in plz_nuts.iterrows():
    latitude = row["latitude"]
    longitude = row["longitude"]
    print(f"currently working at {code}")

    # Perform reverse geocoding
    location = geolocator.reverse(f"{latitude}, {longitude}")
    address = location.raw["address"]
    try:
        contracts_results[code] = get_contract_data(address)
    except Exception:
        log.exception(f"error in contract fees eon for {code}")
    try:
        grid_fee_results[code] = get_grid_data(address)
    except Exception:
        log.exception(f"error in grid fees eon for {code}")


with open("grid_fees.json", "w") as f:
    json.dump(grid_fee_results, f, indent=2)

with open("contracts_results.json", "w") as f:
    json.dump(contracts_results, f, indent=2)

def main(schema_name):
    engine = create_engine(db_uri(schema_name), pool_pre_ping=True)
    create_schema_only(engine, schema_name)
    try:
        pass
        # TODO crawl to database (connection=engine)
    except Exception:
        log.exception("error in mastr")
    set_metadata_only(engine, metadata_info)


if __name__ == "__main__":
    main("eon_fees")
