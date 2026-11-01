# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from unittest import mock

import pandas as pd
import pytest
from oeds.crawler import eon_grid_fees
from sqlalchemy import create_engine, text


def _crawler(codes):
    crawler = object.__new__(eon_grid_fees.EonGridFeeCrawler)
    crawler.engine = create_engine("sqlite://")
    plz = pd.DataFrame({"code": codes, "nuts3": "DE111"}).set_index("code")
    return crawler, plz


def _run(crawler, plz, fees):
    def read_sql_query(*args, **kwargs):
        return plz

    with (
        mock.patch.object(eon_grid_fees.pd, "read_sql_query", read_sql_query),
        mock.patch.object(eon_grid_fees, "Nominatim"),
        mock.patch.object(crawler, "get_plz_fees", side_effect=fees),
    ):
        return crawler.download_grid_fees()


def _row(code):
    return {"zip_code": code, "working_price_grid_ct_per_kwh": 1.0}


def test_failed_postcodes_are_skipped_and_table_replaced():
    codes = [f"{i:05d}" for i in range(20)]
    crawler, plz = _crawler(codes)
    with crawler.engine.begin() as conn:
        conn.execute(text("CREATE TABLE eon_grid_fees (old int)"))
    fees = [RuntimeError("429"), None] + [_row(c) for c in codes[2:]]
    df = _run(crawler, plz, fees)
    assert len(df) == 18
    with crawler.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM eon_grid_fees")).scalar() == 18


def test_too_many_failures_keep_existing_table():
    codes = [f"{i:05d}" for i in range(10)]
    crawler, plz = _crawler(codes)
    with crawler.engine.begin() as conn:
        conn.execute(text("CREATE TABLE eon_grid_fees (old int)"))
        conn.execute(text("INSERT INTO eon_grid_fees VALUES (1)"))
    fees = [None, None] + [_row(c) for c in codes[2:]]
    with pytest.raises(RuntimeError):
        _run(crawler, plz, fees)
    with crawler.engine.connect() as conn:
        assert conn.execute(text("SELECT old FROM eon_grid_fees")).scalar() == 1
