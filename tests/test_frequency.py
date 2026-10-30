# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import pandas as pd
from oeds.crawler.frequency import local_to_utc


def test_summer_and_winter_to_utc():
    result = local_to_utc(pd.Series(["2013/07/01 13:00:00", "2013/12/01 13:00:00"]))
    assert list(result) == [
        pd.Timestamp("2013-07-01 11:00", tz="UTC"),
        pd.Timestamp("2013-12-01 12:00", tz="UTC"),
    ]
    assert str(result.tz) == "UTC"


def test_autumn_hour_resolved_by_labels():
    result = local_to_utc(
        pd.Series(
            [
                "2013/10/27 01:59:59",
                "2013/10/27 2A:00:00",
                "2013/10/27 2A:59:59",
                "2013/10/27 2B:00:00",
                "2013/10/27 2B:59:59",
                "2013/10/27 03:00:00",
            ]
        )
    )
    assert list(result) == [
        pd.Timestamp("2013-10-26 23:59:59", tz="UTC"),
        pd.Timestamp("2013-10-27 00:00:00", tz="UTC"),
        pd.Timestamp("2013-10-27 00:59:59", tz="UTC"),
        pd.Timestamp("2013-10-27 01:00:00", tz="UTC"),
        pd.Timestamp("2013-10-27 01:59:59", tz="UTC"),
        pd.Timestamp("2013-10-27 02:00:00", tz="UTC"),
    ]
    assert result.is_unique


def test_spring_gap_and_garbage_become_nat():
    result = local_to_utc(
        pd.Series(["2013/03/31 01:59:59", "2013/03/31 02:30:00", "garbage"])
    )
    assert result[0] == pd.Timestamp("2013-03-31 00:59:59", tz="UTC")
    assert result[1:].isna().all()


def test_all_invalid():
    assert local_to_utc(pd.Series(["x"])).isna().all()


def test_download_fails_on_http_error(monkeypatch):
    import pytest
    import requests
    from oeds.crawler import frequency

    class Response:
        content = b""

        def raise_for_status(self):
            raise requests.HTTPError("503")

    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs)
        return Response()

    monkeypatch.setattr(frequency.requests, "get", fake_get)
    with pytest.raises(requests.HTTPError):
        next(frequency.download_extract_zip("http://example.invalid/x.zip"))
    assert seen["timeout"] == frequency.DOWNLOAD_TIMEOUT
