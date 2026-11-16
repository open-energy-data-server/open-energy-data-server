# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
import pytest
import requests
from oeds.crawler import dwd
from sqlalchemy import create_engine


def _http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(response=response)


def _crawler():
    # skip __init__, which needs the NUTS matrix
    crawler = object.__new__(dwd.DWDCrawler)
    crawler.engine = create_engine("sqlite://")
    crawler.config = {}
    return crawler


def _crawl(error):
    crawler = _crawler()
    begin = datetime(2019, 3, 1, tzinfo=UTC)
    end = datetime(2019, 6, 1, tzinfo=UTC)
    with mock.patch.object(crawler, "_read_month", side_effect=error) as read:
        crawler.crawl_from_to(begin, end)
    return read


def test_download_checks_status_and_sets_timeout():
    response = mock.Mock()
    response.raise_for_status.side_effect = _http_error(500)
    with mock.patch.object(dwd.requests, "get", return_value=response) as get:
        with pytest.raises(requests.HTTPError):
            _crawler()._download_data("T_2M", 2019, 1)
    assert get.call_args.kwargs["timeout"] == dwd.DOWNLOAD_TIMEOUT


def test_missing_month_inside_the_reanalysis_stops_the_crawl():
    # a (transient) 404 must not leave a gap
    with pytest.raises(requests.HTTPError):
        _crawl(_http_error(404))


def test_crawl_starts_at_the_default_start():
    crawler = _crawler()
    assert crawler.start == dwd.DEFAULT_START
    with mock.patch.object(
        crawler, "_read_month", side_effect=_http_error(503)
    ) as read:
        with pytest.raises(requests.HTTPError):
            crawler.crawl_from_to(datetime(2010, 1, 1, tzinfo=UTC), dwd.COSMO_END)
    read.assert_called_once_with(2019, 1)


def test_configured_start_is_the_first_month_within_the_reanalysis():
    crawler = _crawler()
    crawler.config = {"dwd_start": "2005-03-15"}
    assert crawler.start == pd.Timestamp("2005-03-01", tz="UTC")
    crawler.config = {"dwd_start": "2030-01"}
    assert crawler.start == dwd.COSMO_END


def test_crawl_starts_at_the_first_month_of_the_reanalysis():
    crawler = _crawler()
    crawler.config = {"dwd_start": "1990-01"}
    begin = datetime(1990, 1, 1, tzinfo=UTC)
    end = datetime(1995, 3, 1, tzinfo=UTC)
    with mock.patch.object(
        crawler, "_read_month", side_effect=_http_error(503)
    ) as read:
        with pytest.raises(requests.HTTPError):
            crawler.crawl_from_to(begin, end)
    read.assert_called_once_with(1995, 1)


def test_crawl_stops_after_the_last_month_of_the_reanalysis():
    crawler = _crawler()
    end = datetime(2026, 1, 1, tzinfo=UTC)
    with mock.patch.object(
        crawler, "_read_month", side_effect=_http_error(503)
    ) as read:
        with pytest.raises(requests.HTTPError):
            crawler.crawl_from_to(datetime(2019, 7, 1, tzinfo=UTC), end)
    assert read.call_count == 1
    # nothing is requested after August 2019
    with mock.patch.object(crawler, "_read_month") as read:
        crawler.crawl_from_to(datetime(2019, 9, 1, tzinfo=UTC), end)
    assert read.call_count == 0


def test_failed_month_removes_the_downloaded_files(tmp_path, monkeypatch):
    monkeypatch.setattr(dwd, "DOWNLOAD_DIR", tmp_path)
    crawler = _crawler()
    leftover = tmp_path / "T_2M.201906.grb"
    leftover.write_bytes(b"x")

    def fail(year, month):
        raise ValueError("boom")

    monkeypatch.setattr(crawler, "_read_month_files", fail)
    with pytest.raises(ValueError):
        crawler._read_month(2019, 6)
    assert not leftover.exists()


def test_failing_month_stops_the_crawl():
    with pytest.raises(requests.HTTPError):
        _crawl(_http_error(503))


def test_solar_elevation_of_aachen():
    times = pd.DatetimeIndex(
        ["2019-06-01 11:36", "2019-06-01 20:00", "2019-12-21 11:00"], tz="UTC"
    )
    sine = dwd.solar_elevation_sin(times, np.array([50.78]), np.array([6.08]))
    elevation = np.degrees(np.arcsin(sine.ravel()))
    # solar noon in summer, after sunset, noon in winter
    assert elevation[0] == pytest.approx(61.2, abs=0.5)
    assert elevation[1] < 0
    assert elevation[2] == pytest.approx(15.4, abs=0.5)


def test_direct_normal_irradiance():
    times = pd.DatetimeIndex(["2019-06-01 12:00", "2019-06-01 23:00"], tz="UTC")
    horizontal = np.array([[600.0], [0.0]])
    dni = dwd.direct_normal_irradiance(
        horizontal, times, np.array([50.78]), np.array([6.08])
    )
    # the sun is high at noon: normal radiation exceeds the horizontal one
    assert 600 < dni[0, 0] < 800
    # night, also with the traces of direct radiation after sunset
    assert dni[1, 0] == 0
    traces = dwd.direct_normal_irradiance(
        np.array([[0.1]]), times[1:], np.array([50.78]), np.array([6.08])
    )
    assert traces[0, 0] == 0


def test_regions_average_all_nuts_levels(monkeypatch):
    latitude = np.array([[50.0, 51.0, 52.0]])
    longitude = np.array([[6.0, 7.0, 8.0]])
    monkeypatch.setattr(dwd, "grid_coordinates", lambda: (latitude, longitude))
    matrix = np.array([["DEA2D", "DEA23", "x"]])
    regions = dwd.Regions(matrix)
    mean = dict(zip(regions.ids, regions.average(np.array([[10.0, 20.0, 99.0]]))))
    assert mean["DE"] == 15.0
    assert mean["DEA2"] == 15.0
    assert mean["DEA2D"] == 10.0
    assert mean["DEA23"] == 20.0
    assert regions.countries[0] == "DE"


def test_message_time_is_the_validity_of_the_message():
    message = {"validityDate": 20190601, "validityTime": 100}
    assert dwd._message_time(message) == pd.Timestamp("2019-06-01 01:00", tz="UTC")


def test_previous_month_is_fetched_from_december_2018(monkeypatch):
    # the precipitation of the first hour of the range comes from the month before
    crawler = _crawler()
    first = pd.Timestamp("2018-12-01", tz="UTC")
    calls = []
    monkeypatch.setattr(
        crawler, "_download_data", lambda d, y, m: calls.append((y, m)) or Path("x")
    )
    monkeypatch.setattr(crawler, "_delete_data", lambda *a: None)
    grid = np.ones((1, 1))
    monkeypatch.setattr(
        crawler,
        "_last_message",
        lambda path: (first + pd.Timedelta(hours=1), grid),
    )

    class Regions:
        @staticmethod
        def average(g):
            return g.ravel()

    result = crawler._previous_hour("TOT_PRECIP", 2018, 12, Regions, first)
    assert calls == [(2018, 11)]
    assert list(result) == [first + pd.Timedelta(hours=1)]


def test_crawl_refuses_naive_times_and_keeps_the_stored_month():
    crawler = _crawler()
    stored = pd.DataFrame(
        {"time": pd.to_datetime(["2019-03-01 05:00"], utc=True), "tot_precip": [1.0]}
    )
    stored.to_sql(dwd.DWDCrawler.TABLE, crawler.engine, index=False)
    naive = stored.assign(tot_precip=2.0)
    naive["time"] = naive["time"].dt.tz_localize(None)

    with mock.patch.object(crawler, "_read_month", return_value=naive):
        with pytest.raises(ValueError, match="naive"):
            crawler.crawl_from_to(
                datetime(2019, 3, 1, tzinfo=UTC), datetime(2019, 4, 1, tzinfo=UTC)
            )

    kept = pd.read_sql(f"select * from {dwd.DWDCrawler.TABLE}", crawler.engine)
    assert kept["tot_precip"].tolist() == [1.0]
