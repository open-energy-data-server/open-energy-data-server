# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from oeds.crawler.powerplantmatching import parse_eic_set, parse_powerplants

PP_CSV = """id,Name,Fueltype,Technology,Set,Country,Capacity,Efficiency,DateIn,DateRetrofit,DateOut,lat,lon,Duration,Volume_Mm3,DamHeight_m,StorageCapacity_MWh,EIC,projectID
0,Plant A,Hydro,Pumped Storage,Store,Austria,380.0,0.75,1943.0,2018.0,,46.9,10.0,,,,,"{'48WSTN00000CRUAC', '12W-0000000018-B'}","{'GEM': {'G1'}}"
1,Plant B,Wind,Onshore,PP,Germany,2.5,,2005.0,,,50.1,8.2,,,,,set(),"{'MASTR': {'M1'}}"
2,Plant C,Solar,PV,PP,Spain,1.0,,,,,40.0,-3.0,,,,,"{'48WSTN00000CRUAC'}",
"""


def test_parse_eic_set():
    assert parse_eic_set("{'B', 'A'}") == ["A", "B"]
    assert parse_eic_set("set()") == []
    assert parse_eic_set(float("nan")) == []


def test_parse_powerplants():
    df = parse_powerplants(PP_CSV)
    assert len(df) == 3
    assert "fuel_type" in df.columns
    assert df["date_in"].iloc[0] == 1943
    assert df["date_out"].isna().all()
