# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest
from oeds.base_crawler import empty_config
from oeds.crawler.refit import RefitCrawler
from sqlalchemy import text


def make_crawler():
    config = empty_config()
    config["db_uri"] = "sqlite://"
    return RefitCrawler("test", config)


def test_refit_is_only_visible_when_all_houses_are_written(tmp_path):
    (tmp_path / "House1.csv").write_text("Unix,Aggregate\n1380000000,10\n")
    crawler = make_crawler()
    with pytest.raises(FileNotFoundError):
        crawler.write_houses(tmp_path, ["House1.csv", "House2.csv"])
    assert not crawler.structure_exists()

    (tmp_path / "House2.csv").write_text("Unix,Aggregate\n1380000060,20\n")
    crawler.write_houses(tmp_path, ["House1.csv", "House2.csv"])
    assert crawler.structure_exists()
    with crawler.engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM refit")).scalar() == 2
