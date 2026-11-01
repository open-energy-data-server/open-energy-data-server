# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import zipfile
from unittest import mock

import pytest
from oeds.crawler import kelmarsh
from sqlalchemy import create_engine, text


def _row(timestamp, power="1.5"):
    row = [""] * 63
    row[0], row[1], row[27], row[62] = timestamp, "5.0", "10.0", power
    return ",".join(row)


def _crawler(tmp_path, rows):
    zip_path = tmp_path / "scada.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        for name in kelmarsh.TURBINES.values():
            z.writestr(name, "# header\n" + "\n".join(rows))
    crawler = object.__new__(kelmarsh.KelmarshCrawler)
    crawler.engine = create_engine("sqlite://")
    crawler.config = {}
    crawler.download_scada_archive = lambda: zip_path
    return crawler


def test_missing_turbines_are_reloaded(tmp_path):
    rows = [_row("2023-01-01 00:00:00"), _row("2023-01-01 00:10:00")]
    crawler = _crawler(tmp_path, rows)
    assert not crawler.structure_exists()
    crawler.load_scada_data()
    assert crawler.structure_exists()

    with crawler.engine.begin() as conn:
        conn.execute(text("DELETE FROM scada WHERE turbine = 'KWF3'"))
    assert not crawler.structure_exists()
    crawler.load_scada_data()
    assert crawler.structure_exists()
    with crawler.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM scada")).scalar() == 12


def test_unparsable_file_is_rolled_back(tmp_path):
    crawler = _crawler(tmp_path, [_row("garbage"), _row("garbage")])
    with pytest.raises(ValueError):
        crawler.load_scada_data()
    assert crawler.loaded_turbines() == set()


def test_archive_is_downloaded_atomically(tmp_path):
    crawler = object.__new__(kelmarsh.KelmarshCrawler)
    crawler.config = {}
    target = tmp_path / "archive.zip"
    response = mock.Mock()
    response.iter_content.return_value = [b"abc", b"def"]
    with mock.patch.object(kelmarsh.requests, "get", return_value=response) as get:
        crawler.download_scada_archive(target)
    assert get.call_args.kwargs["timeout"] == kelmarsh.DOWNLOAD_TIMEOUT
    assert target.read_bytes() == b"abcdef"
    assert not target.with_name("archive.zip.part").exists()

    target.unlink()
    response.iter_content.side_effect = OSError("connection lost")
    with mock.patch.object(kelmarsh.requests, "get", return_value=response):
        with pytest.raises(OSError):
            crawler.download_scada_archive(target)
    assert not target.exists()
