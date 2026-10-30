# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest
import requests
from oeds.crawler import chargepoint


class Response:
    text = "Preis (Festgelegt von X)\n0,39 €/kWh"

    def __init__(self, summaries):
        self.summaries = summaries

    def raise_for_status(self):
        pass

    def json(self):
        return {"station_list": {"summaries": self.summaries}}


def test_stations_without_device_id_are_skipped(monkeypatch):
    summaries = [{"lat": 1, "lon": 2}, {"device_id": 7, "lat": 1, "lon": 2}]
    monkeypatch.setattr(
        chargepoint.requests, "get", lambda *a, **k: Response(summaries)
    )
    monkeypatch.setattr(chargepoint.requests, "post", lambda *a, **k: Response([]))
    stations = chargepoint.read_stations(max_locations=1)
    assert list(stations) == [7]
    assert stations[7]["price_e_kwh"] == 0.39


def test_too_many_failed_requests_abort_the_crawl(monkeypatch):
    def fail(*args, **kwargs):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(chargepoint.requests, "get", fail)
    with pytest.raises(RuntimeError):
        chargepoint.read_stations(max_locations=2)
