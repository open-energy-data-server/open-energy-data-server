# SPDX-FileCopyrightText: Florian Maurer
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import logging

import pandas as pd
import requests
from bs4 import BeautifulSoup
from sqlalchemy import text

from oeds.base_crawler import DEFAULT_CONFIG_LOCATION, DownloadOnceCrawler, load_config

log = logging.getLogger(__name__)

CHARGEPOINT_MAP_URL = "https://mc-eu.chargepoint.com/map-prod/get"
REQUEST_TIMEOUT = 30
MAX_FAILED_SHARE = 0.1
PRICE_URL = "https://de.chargepoint.com/dashboard/getStationPricingDetails"


def parse_number(text: str) -> float:
    """Parses a German formatted number like 0,39 or 1.234,5."""
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    return float(text)


def read_stations(max_locations: int | None = None):
    stations = {}
    # for key in stations.keys():
    #    stations[key]['price'] = stations[key]['price'].strip()

    # does not give more than 50 stations
    # no matter which are or page is given (page does not work at all)
    location = '"ne_lat":55.10666691630692,"ne_lon":22.321837533755254,"sw_lat":43.833901738393294,"sw_lon":-5.3856819974947445,'

    locations = []
    for lat in range(44, 54):
        for lon in range(3, 22):
            loc = f'"ne_lat":{lat + 1},"ne_lon":{lon + 1},"sw_lat":{lat},"sw_lon":{lon}'
            locations.append(loc)

    if max_locations is not None:
        locations = locations[:max_locations]

    requests_total = 0
    requests_failed = 0
    for location in locations:
        for sort in ["installation_date", "distance"]:
            # f-string would be unreadable here
            url = (
                CHARGEPOINT_MAP_URL
                + '?{"station_list":{'
                + location
                + '"page_size":100,"page_offset":"", "sort_by":"'
                + sort
                + '","filter":{"network_chargepoint":true},"include_map_bound":true,"estimated_fee_input":{"arrival_time":"12:45","battery_size":30}}}'
            )

            requests_total += 1
            try:
                resp = requests.get(url, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                j = resp.json()
                summaries = j["station_list"]["summaries"]
            except (requests.RequestException, ValueError, KeyError, TypeError):
                log.exception("could not read stations for %s", location)
                requests_failed += 1
                continue

            log.info(
                "Summary Count for %s: %s",
                location,
                len(summaries),
            )

            new_stations = 0
            for station in summaries:
                device_id = station.get("device_id")
                if device_id is None:
                    log.warning("skipping station without device_id in %s", location)
                    continue
                try:
                    response = requests.post(
                        PRICE_URL,
                        data={"deviceId": device_id},
                        timeout=REQUEST_TIMEOUT,
                    )
                    response.raise_for_status()
                except requests.RequestException:
                    log.exception("could not read price of %s", device_id)
                    continue
                soup = BeautifulSoup(response.text, "html.parser")
                price_text = (
                    soup.get_text()
                    .strip()
                    .replace("\n\n", "\n")
                    .replace("\n\n", "\n")
                    .replace("\xa0", " ")
                    .splitlines()
                )

                if device_id not in stations:
                    new_stations += 1

                stations[device_id] = {
                    "lat": station["lat"],
                    "lon": station["lon"],
                    "price": price_text,
                }
            log.info("added %s new stations", new_stations)

    if requests_failed > MAX_FAILED_SHARE * requests_total:
        # the caller replaces the stored table, which must not happen with a partial crawl
        raise RuntimeError(
            f"{requests_failed} of {requests_total} station requests failed"
        )

    for s_id, station in stations.items():
        for line in station["price"]:
            try:
                if line.endswith("€/kWh"):
                    station["price_e_kwh"] = parse_number(line.split(" ")[0])
                if line.startswith("Preis (Festgelegt von "):
                    station["kunde"] = (
                        line.replace("Preis (Festgelegt von ", "")
                        .replace(")", "")
                        .strip()
                    )
                if line.endswith("€/Std."):
                    station["parking"] = parse_number(line.split(" ")[0])
                elif line.endswith("€/Min."):
                    station["parking"] = parse_number(line.split(" ")[0]) * 60
            except ValueError:
                log.warning("could not parse price line %r of %s", line, s_id)

    log.info("Found %s stations", len(stations.keys()))
    return stations


class ChargepointDownloader(DownloadOnceCrawler):
    def structure_exists(self) -> bool:
        try:
            with self.engine.connect() as conn:
                return (
                    conn.execute(
                        text("SELECT 1 from chargingstations limit 1")
                    ).scalar()
                    == 1
                )
        except Exception:
            return False

    def crawl_structural(
        self, recreate: bool = False, max_locations: int | None = None
    ):
        if not self.structure_exists() or recreate:
            stations = read_stations(max_locations=max_locations)
            df = pd.DataFrame(stations).T
            df.index.name = "cs_id"
            for col, default_val in [
                ("price_e_kwh", 0.0),
                ("parking", 0.0),
                ("kunde", "unknown customer"),
            ]:
                if col not in df.columns:
                    df[col] = default_val
                else:
                    df[col] = df[col].fillna(default_val)
            df["price_strings"] = df.price.apply("</br> ".join)
            with self.engine.begin() as conn:
                df.to_sql("chargingstations", conn, if_exists="replace")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    config = load_config(DEFAULT_CONFIG_LOCATION)
    chargepoint = ChargepointDownloader("chargepoint", config=config)
    chargepoint.crawl_structural(recreate=False)
