# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later


import pytest
from oeds.base_crawler import empty_config
from oeds.crawler import oep


def sqlite_config():
    config = empty_config()
    config["db_uri"] = "sqlite://"
    return config


class StreamResponse:
    def __init__(self, chunks, fail=False):
        self.chunks = chunks
        self.fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        if self.fail:
            raise oep.requests.HTTPError("500")

    def iter_content(self, chunk_size):
        yield from self.chunks
        if self.fail:
            raise oep.requests.ConnectionError("broken")


def test_oep_download_is_atomic(monkeypatch, tmp_path):
    crawler = oep.OepCrawler("test", sqlite_config())
    target = tmp_path / "oep.csv"
    monkeypatch.setattr(
        oep.requests, "get", lambda *a, **k: StreamResponse([b"a"], fail=True)
    )
    with pytest.raises(oep.requests.RequestException):
        crawler.crawl_oep(target)
    assert not target.exists()
    assert not target.with_name("oep.csv.part").exists()

    csv = b"geom,geom_centre,geom_surfacepoint,geom_centroid,v\n1,1,1,1,5\n"
    monkeypatch.setattr(
        oep.requests, "get", lambda *a, **k: StreamResponse([csv[:10], csv[10:]])
    )
    crawler.crawl_oep(target)
    assert target.read_bytes() == csv
    assert not target.with_name("oep.csv.part").exists()
