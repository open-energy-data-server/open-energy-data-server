# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import pandas as pd
from oeds.crawler.opsd import prepare_when2heat


def test_prepare_when2heat_is_aware_utc():
    data = pd.DataFrame(
        {
            "utc_timestamp": ["2019-07-01T00:00:00Z", "2019-12-01T00:00:00Z"],
            "cet_cest_timestamp": [
                "2019-07-01T02:00:00+0200",
                "2019-12-01T01:00:00+0100",
            ],
            "DE_COP": [3.1, 2.9],
        }
    )
    result = prepare_when2heat(data)
    assert result.index.name == "utc_timestamp"
    assert str(result.index.tz) == "UTC"
    assert list(result.index) == [
        pd.Timestamp("2019-07-01 00:00", tz="UTC"),
        pd.Timestamp("2019-12-01 00:00", tz="UTC"),
    ]
    assert str(result["cet_cest_timestamp"].dt.tz) == "UTC"
    assert (result["cet_cest_timestamp"].to_numpy() == result.index.to_numpy()).all()
    assert "utc_timestamp" not in result.columns
    assert list(result["DE_COP"]) == [3.1, 2.9]


def test_when2heat_error_page_is_not_saved(tmp_path):
    from unittest import mock

    import pytest
    import requests
    from oeds.crawler import opsd

    response = mock.Mock(content=b"<html>error</html>")
    response.raise_for_status.side_effect = requests.HTTPError("503")
    crawler = object.__new__(opsd.OpsdCrawler)
    db_path = tmp_path / "when2heat.db"
    with mock.patch.object(opsd.requests, "get", return_value=response) as get:
        with pytest.raises(requests.HTTPError):
            crawler.write_when2_heat(db_path)
    assert get.call_args.kwargs["timeout"] == opsd.DOWNLOAD_TIMEOUT
    assert list(tmp_path.iterdir()) == []


def test_when2heat_download_without_database_is_not_saved(tmp_path):
    from unittest import mock

    import pytest
    from oeds.crawler import opsd

    # an error page served with HTTP 200
    response = mock.Mock(content=b"<html>maintenance</html>")
    crawler = object.__new__(opsd.OpsdCrawler)
    db_path = tmp_path / "when2heat.db"
    with mock.patch.object(opsd.requests, "get", return_value=response):
        with pytest.raises(opsd.sqlite3.DatabaseError):
            crawler.write_when2_heat(db_path)
    assert list(tmp_path.iterdir()) == []
