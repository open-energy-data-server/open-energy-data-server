# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import UTC, datetime
from unittest import mock

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
    return crawler


def _crawl(error):
    crawler = _crawler()
    begin = datetime(2020, 1, 1, tzinfo=UTC)
    end = datetime(2020, 3, 1, tzinfo=UTC)
    with mock.patch.object(crawler, "_download_data", side_effect=error) as download:
        crawler.crawl_from_to(begin, end)
    return download


def test_download_checks_status_and_sets_timeout():
    response = mock.Mock()
    response.raise_for_status.side_effect = _http_error(500)
    with mock.patch.object(dwd.requests, "get", return_value=response) as get:
        with pytest.raises(requests.HTTPError):
            _crawler()._download_data("temp_air", "2020", "01")
    assert get.call_args.kwargs["timeout"] == dwd.DOWNLOAD_TIMEOUT


def test_missing_month_is_skipped():
    # every month is missing, the crawl still walks through all of them
    download = _crawl(_http_error(404))
    assert download.call_count == 3


def test_failing_month_stops_the_crawl():
    with pytest.raises(requests.HTTPError):
        _crawl(_http_error(503))
