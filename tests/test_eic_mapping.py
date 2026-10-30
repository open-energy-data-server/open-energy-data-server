# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from oeds.crawler.eic_mapping import build_eic_locations, parse_eic_codes
from oeds.crawler.powerplantmatching import parse_powerplants

PP_CSV = """id,Name,Fueltype,Technology,Set,Country,Capacity,Efficiency,DateIn,DateRetrofit,DateOut,lat,lon,Duration,Volume_Mm3,DamHeight_m,StorageCapacity_MWh,EIC,projectID
0,Plant A,Hydro,Pumped Storage,Store,Austria,380.0,0.75,1943.0,2018.0,,46.9,10.0,,,,,"{'48WSTN00000CRUAC', '12W-0000000018-B'}","{'GEM': {'G1'}}"
1,Plant B,Wind,Onshore,PP,Germany,2.5,,2005.0,,,50.1,8.2,,,,,set(),"{'MASTR': {'M1'}}"
2,Plant C,Solar,PV,PP,Spain,1.0,,,,,40.0,-3.0,,,,,"{'48WSTN00000CRUAC'}",
"""

EIC_CSV = (
    "﻿EicCode;EicDisplayName;EicLongName;EicParent;EicResponsibleParty;EicStatus;"
    "MarketParticipantPostalCode;MarketParticipantIsoCountryCode;"
    "MarketParticipantVatCode;EicTypeFunctionList;type\r\n"
    "26WAI-PONTE10003;0_868;IMAI_PONTE_1;;;Active;;;;Production Unit;W\r\n"
    "10Y1001A1001A016;GB-NI;Northern Ireland;;;Active;BT6 9RT;GB;;Control Area;Y\r\n"
)


def test_parse_eic_codes():
    df = parse_eic_codes(EIC_CSV, "W")
    assert df["eic_code"].tolist() == ["26WAI-PONTE10003", "10Y1001A1001A016"]
    assert (df["code_type"] == "W").all()
    assert df["display_name"].iloc[1] == "GB-NI"
    assert df["country_code"].iloc[1] == "GB"


def test_build_eic_locations():
    loc = build_eic_locations(parse_powerplants(PP_CSV))
    assert sorted(loc["eic_code"]) == ["12W-0000000018-B", "48WSTN00000CRUAC"]
    # first occurrence wins for duplicated codes
    row = loc[loc["eic_code"] == "48WSTN00000CRUAC"].iloc[0]
    assert row["lat"] == 46.9
