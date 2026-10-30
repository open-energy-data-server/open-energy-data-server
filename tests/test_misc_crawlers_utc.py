# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import io

import pandas as pd
from oeds.crawler import londondatastore
from sqlalchemy import create_engine


def test_london_datetime_is_written_as_utc():
    crawler = londondatastore.LondonLoadData.__new__(londondatastore.LondonLoadData)
    crawler.engine = create_engine("sqlite://")
    csv = io.StringIO(
        "LCLid,stdorToU,DateTime,KWH/hh (per half hour) \n"
        "MAC1,Std,2013-10-27 00:00:00.0000000,0.1\n"
        "MAC1,Std,2013-10-27 00:00:00.0000000,0.1\n"
        "MAC1,Std,2013-10-27 00:30:00.0000000,0.2\n"
    )
    crawler.write_member(csv)
    df = pd.read_sql(f"select * from {londondatastore.STAGING_TABLE}", crawler.engine)
    assert len(df) == 2
    assert pd.to_datetime(df["DateTime"], utc=True).iloc[0] == pd.Timestamp(
        "2013-10-27 00:00", tz="UTC"
    )
