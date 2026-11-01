# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later


import pandas as pd
from oeds.base_crawler import empty_config
from oeds.crawler import ninja


def sqlite_config():
    config = empty_config()
    config["db_uri"] = "sqlite://"
    return config


def test_ninja_structure_requires_last_written_table():
    crawler = ninja.NinjaCrawler("test", sqlite_config())
    with crawler.engine.begin() as conn:
        pd.DataFrame({"a": [1]}).to_sql("capacity_wind_off", conn)
    assert not crawler.structure_exists()
    with crawler.engine.begin() as conn:
        pd.DataFrame({"a": [1]}).to_sql("capacity_solar_merra2", conn)
    assert crawler.structure_exists()
