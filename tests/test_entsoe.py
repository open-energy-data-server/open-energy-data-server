# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import pandas as pd
from entsoe import EntsoePandasClient
from oeds.crawler.entsoe_crawler import (
    EntsoeClient,
    name_unnamed_plants,
    query_kwargs,
    to_utc,
)


def test_generation_is_queried_nett():
    client = EntsoePandasClient(api_key="test")
    assert query_kwargs(client.query_generation) == {"nett": True}
    assert query_kwargs(client.query_generation_forecast) == {"nett": True}
    assert query_kwargs(client.query_load) == {}


def test_to_utc_reads_naive_as_utc_and_converts_aware():
    assert to_utc(pd.Timestamp("2024-01-01")) == pd.Timestamp("2024-01-01", tz="UTC")
    berlin = pd.Timestamp("2024-07-01 02:00", tz="Europe/Berlin")
    assert str(to_utc(berlin).tz) == "UTC"
    assert to_utc(berlin) == pd.Timestamp("2024-07-01 00:00", tz="UTC")


PLANT_XML = """<GL_MarketDocument>
<TimeSeries>
<mRID>1</mRID>
<quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name><curveType>A01</curveType>
<MktPSRType><psrType>B05</psrType><PowerSystemResources>
<mRID codingScheme="A01">11WD7ELVE2S-E4-9</mRID>
</PowerSystemResources></MktPSRType>
<Period><timeInterval><start>2015-01-27T00:00Z</start><end>2015-01-27T01:00Z</end></timeInterval>
<resolution>PT60M</resolution><Point><position>1</position><quantity>100</quantity></Point></Period>
</TimeSeries>
<TimeSeries>
<mRID>2</mRID>
<quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name><curveType>A01</curveType>
<MktPSRType><psrType>B05</psrType><PowerSystemResources>
<mRID codingScheme="A01">11WD2NIED4KW1--Y</mRID><name>Niederaussem K</name>
</PowerSystemResources></MktPSRType>
<Period><timeInterval><start>2015-01-27T00:00Z</start><end>2015-01-27T01:00Z</end></timeInterval>
<resolution>PT60M</resolution><Point><position>1</position><quantity>200</quantity></Point></Period>
</TimeSeries>
</GL_MarketDocument>"""


def test_unnamed_plants_get_their_eic_as_name():
    xml = name_unnamed_plants(PLANT_XML)
    assert "<name>11WD7ELVE2S-E4-9</name>" in xml
    assert xml.count("<name>") == 2


def test_client_parses_per_plant_data_without_name(monkeypatch):
    client = EntsoeClient(api_key="test")
    monkeypatch.setattr(
        EntsoeClient,
        "_base_request",
        lambda self, params, start, end: type("R", (), {"text": PLANT_XML})(),
    )
    df = client.query_generation_per_plant(
        "DE_AMPRION",
        start=pd.Timestamp("2015-01-27", tz="UTC"),
        end=pd.Timestamp("2015-01-27 01:00", tz="UTC"),
    )
    names = set(df.columns.get_level_values(0))
    assert names == {"11WD7ELVE2S-E4-9", "Niederaussem K"}
