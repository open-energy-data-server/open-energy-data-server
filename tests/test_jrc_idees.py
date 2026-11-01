# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import io
import zipfile

import pytest
from oeds.base_crawler import empty_config
from oeds.crawler import jrc_idees


def sqlite_config():
    config = empty_config()
    config["db_uri"] = "sqlite://"
    return config


def test_jrc_failed_file_prevents_complete_marker(monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("JRC-IDEES_Industry_DE.xlsx", b"not an excel file")

    class Response:
        content = buf.getvalue()

        def raise_for_status(self):
            pass

    monkeypatch.setattr(jrc_idees.requests, "get", lambda *a, **k: Response())
    crawler = jrc_idees.JrcIdeesCrawler("test", sqlite_config())
    with pytest.raises(RuntimeError):
        crawler.crawl_structural()
    assert not crawler.structure_exists()
