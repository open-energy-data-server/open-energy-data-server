# SPDX-FileCopyrightText: Florian Maurer, Christian Rieke
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import logging
import time

import pandas as pd
import requests
from dateutil.relativedelta import relativedelta
from geopy.geocoders import Nominatim
from sqlalchemy import text
from tqdm import tqdm

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    DownloadOnceCrawler,
    load_config,
    utcnow,
)

log = logging.getLogger(__name__)

metadata_info = {
    "schema_name": "eon_grid_fees",
    "data_source": "https://www.eon.de/de/gk/strom/tarifrechner.html",
    "license": "EON restricted",
    "description": "Contract data of EON",
    "contact": "",
    "temporal_start": "2025-01-17",
}

EON_URI = "https://occ.eon.de/b2b-pricing/1.0/api/v2/offers"
GRID_FEES_URI = "https://occ.eon.de/b2b-pricing/1.0/api/v2/thirdPartyCosts/rlm/power"
REQUEST_TIMEOUT = 30
STAGING_TABLE = "eon_grid_fees_staging"
FLUSH_SIZE = 100
MAX_FAILED_SHARE = 0.1


def get_contract_data(address: dict):
    # we need to create an offer starting for the next month
    start = utcnow() + relativedelta(months=1)
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
    response = requests.post(EON_URI, json=data, timeout=REQUEST_TIMEOUT)
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
            f"https://occ.eon.de/zipcodes/1.3/api?clientId=eonde&query={postcode}",
            timeout=REQUEST_TIMEOUT,
        ).json()
        city = city["zipCodes"][0]["cities"][0]["city"]

    street = requests.get(
        f"https://occ.eon.de/streets/1.3/api?clientId=eonde&zipCode={postcode}&streetName=a",
        timeout=REQUEST_TIMEOUT,
    ).json()

    # extract first element if available
    if street:
        street = street[0]

    if not street:
        street = address.get("road")
    params = {
        "type": "Strom",
        "city": city,
        "consumption": 100000,
        "zipCode": address.get("postcode"),
        "street": street,
        # "houseNumber" is not used anyway and can be left out
    }

    grid_response = requests.get(GRID_FEES_URI, params=params, timeout=REQUEST_TIMEOUT)
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
        geolocator = Nominatim(user_agent="OEDS FH-Aachen", timeout=REQUEST_TIMEOUT)
        rows = []
        failed = 0
        flushed = 0

        with self.engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {STAGING_TABLE}"))

        # location is only based on nuts3, but we crawl all to get all DSO data
        for nuts3, plzs in tqdm(plz_nuts.groupby("nuts3")):
            for code, row in plzs.iterrows():
                try:
                    result = self.get_plz_fees(geolocator, code)
                except Exception:
                    log.exception(f"error in grid fees eon for {code}")
                    result = None
                if result is None:
                    failed += 1
                else:
                    rows.append(result)
                if len(rows) - flushed >= FLUSH_SIZE:
                    self.write_staging(rows[flushed:])
                    flushed = len(rows)

        self.write_staging(rows[flushed:])
        total = len(rows) + failed
        if not rows or failed > MAX_FAILED_SHARE * total:
            raise RuntimeError(
                f"{failed} of {total} postcodes failed, keeping the existing grid fees"
            )
        log.info(f"{failed} of {total} postcodes failed")

        # replace the snapshot only after a finished run
        with self.engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS eon_grid_fees"))
            conn.execute(text(f"ALTER TABLE {STAGING_TABLE} RENAME TO eon_grid_fees"))

        return pd.DataFrame(rows)

    def write_staging(self, rows: list[dict]):
        if rows:
            with self.engine.begin() as conn:
                pd.DataFrame(rows).to_sql(
                    STAGING_TABLE, conn, if_exists="append", index=False
                )

    def get_plz_fees(self, geolocator, code) -> dict | None:
        """Returns the grid fees of a postcode or None if they could not be found."""
        # sleep to reduce load a nominatim API
        # https://operations.osmfoundation.org/policies/nominatim/
        time.sleep(2)
        log.info(f"currently working at {code}")
        loc = geolocator.geocode(f"{code}")
        if loc is None:
            log.warning(f"no location found for {code}, skipping")
            return None
        time.sleep(1)

        location = geolocator.reverse(f"{loc.raw['lat']}, {loc.raw['lon']}")
        if location is None:
            log.warning(f"no address found for {code}, skipping")
            return None
        address = location.raw["address"]
        # some location middles do not have a postcode set like
        # 57642
        # fill with leading zeros
        address["postcode"] = str(address.get("postcode", code)).zfill(5)
        try:
            get_contract_data(address)
        except Exception:
            log.exception(f"error in contract fees eon for {code}")

        prices = get_grid_data(address)["prices"]
        working_price = prices["working_price_grid"]
        return {
            "zip_code": code,
            "working_price_grid_ct_per_kwh": working_price["value_vat"],
            "power_price_grid_eur_per_kw": prices["power_price_grid"]["value_vat"],
            "fee_measurement_eur_per_year": prices["fee_measurement"]["value_vat"],
            "market_partner_name": working_price.get("market_partner_name"),
            "market_partner_code": working_price.get("market_partner_code"),
        }


if __name__ == "__main__":
    logging.basicConfig()

    config = load_config(DEFAULT_CONFIG_LOCATION)
    crawler = EonGridFeeCrawler("eon_grid_fees", config)
    grid_fees = crawler.download_grid_fees()
    crawler.set_metadata(metadata_info)
