# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Offline tests for the netzentgelte parsers: the parsers are pure text ->
dataclass, so realistic text excerpts of the price sheets cover them without
network access."""

import logging

import pandas as pd
from oeds.base_crawler import empty_config
from oeds.crawler.netzentgelte import crawler as nz
from oeds.crawler.netzentgelte.adapters._common import numeric_rows_warn
from oeds.crawler.netzentgelte.adapters.rheinnetz import parse as parse_rheinnetz
from oeds.crawler.netzentgelte.fetch_uebertragungsnetz import parse_uenb_text
from oeds.crawler.netzentgelte.schema import NetzentgeltEintrag

# Excerpt of 251205_preisblatt_ben_2026.pdf (netztransparenz.de). The JLP
# columns are Leistungspreis then Arbeitspreis for each use-duration class.
UENB_TEXT = """Bundeseinheitliche Netzentgelte Strom 2026
Gültig ab 01.01.2026
Jahresleistungspreissystem
Abnahmefall 1: Entnahme ohne Load-Following, < 2.500 h/a und Abnahmefall 2:
Load-Following, >= 2.500 h/a, jeweils Leistungspreis / Arbeitspreis
Höchstspannung (HÖS) 11,39 2,36 53,06 0,69
Umspannung (HÖS/HS) 4,53 0,74 28,63 0,20
Monatsleistungspreissystem
Höchstspannung (HÖS) 8,84 0,69
Umspannung (HÖS/HS) 3,53 0,20
"""


def _entry(entries, netzebene, tarifsystem, dauer=None):
    hits = [
        e
        for e in entries
        if e.netzebene == netzebene
        and e.tarifsystem == tarifsystem
        and e.benutzungsdauer_klasse == dauer
    ]
    assert len(hits) == 1, (netzebene, tarifsystem, dauer)
    return hits[0]


def test_parse_uenb_jlp_column_order():
    """Capacity price (Leistungspreis) comes before energy price (Arbeitspreis)
    in the ÜNB sheet: HÖS is 11,39 EUR/kW*a and 2,36 ct/kWh for <2500h."""
    entries = parse_uenb_text(UENB_TEXT, jahr=2026, quelle_url="http://unb/2026.pdf")
    hos_klein = _entry(entries, "Hoechstspannung", "Jahresleistungspreis", "<2500h")
    assert hos_klein.leistungspreis_eur_kw_jahr == 11.39
    assert hos_klein.arbeitspreis_ct_kwh == 2.36
    hos_gross = _entry(entries, "Hoechstspannung", "Jahresleistungspreis", ">=2500h")
    assert hos_gross.leistungspreis_eur_kw_jahr == 53.06
    assert hos_gross.arbeitspreis_ct_kwh == 0.69
    um_klein = _entry(entries, "Umspannung_HOES_HS", "Jahresleistungspreis", "<2500h")
    assert um_klein.leistungspreis_eur_kw_jahr == 4.53
    assert um_klein.arbeitspreis_ct_kwh == 0.74
    um_gross = _entry(entries, "Umspannung_HOES_HS", "Jahresleistungspreis", ">=2500h")
    assert um_gross.leistungspreis_eur_kw_jahr == 28.63
    assert um_gross.arbeitspreis_ct_kwh == 0.20
    assert all(e.jahr == 2026 for e in entries)
    assert all(e.stand_dokument == "01.01.2026" for e in entries)


def test_parse_uenb_mlp_rows():
    entries = parse_uenb_text(UENB_TEXT, jahr=2026, quelle_url="http://unb/2026.pdf")
    hos = _entry(entries, "Hoechstspannung", "Monatsleistungspreis")
    assert hos.leistungspreis_eur_kw_monat == 8.84
    assert hos.arbeitspreis_ct_kwh == 0.69
    assert hos.leistungspreis_eur_kw_jahr is None
    um = _entry(entries, "Umspannung_HOES_HS", "Monatsleistungspreis")
    assert um.leistungspreis_eur_kw_monat == 3.53
    # 4 JLP rows (2 levels x 2 classes) + 2 MLP rows
    assert len(entries) == 6


RHEINNETZ_TEXT = """Netznutzungsentgelte Strom 2026, gültig ab 01.01.2026
Preisblatt 1
Niederspannung 150,00 178,50 3,78 4,50
Preisblatt 2
Weitere Preisblätter
Preisblatt 4
Jahrespreissystem
Hochspannung 22,11 1,82 103,06 0,41
Hochspannung mit Umspannung auf Mittelspannung 26,47 1,50 129,64 0,32
Mittelspannung 24,18 1,24 121,97 0,25
Mittelspannung mit Umspannung auf Niederspannung 31,32 1,23 153,68 0,21
Niederspannung 51,93 2,27 229,16 0,37
Monatspreissystem
"""


def test_parse_rheinnetz():
    entries = parse_rheinnetz(
        RHEINNETZ_TEXT, stadt="Köln", jahr=2026, quelle_url="http://rheinnetz/pb"
    )
    slp = _entry(entries, "Niederspannung", "Grundpreis_Arbeitspreis")
    assert slp.grundpreis_eur_jahr == 150.00
    assert slp.arbeitspreis_ct_kwh == 3.78
    hs = _entry(entries, "Hochspannung", "Jahresleistungspreis", "<2500h")
    assert hs.leistungspreis_eur_kw_jahr == 22.11
    assert hs.arbeitspreis_ct_kwh == 1.82
    ns = _entry(entries, "Niederspannung", "Jahresleistungspreis", ">=2500h")
    assert ns.leistungspreis_eur_kw_jahr == 229.16
    assert ns.arbeitspreis_ct_kwh == 0.37
    # 1 household row + 5 voltage levels x 2 duration classes
    assert len(entries) == 11


def test_numeric_rows_warns_on_truncation(caplog):
    section = "Hochspannung 1,00 2,00 3,00 4,00\n"
    with caplog.at_level(logging.WARNING, logger="netzentgelte"):
        rows = numeric_rows_warn(section, 3, "Test JLP")
    assert len(rows) == 1
    assert "only 1 of 3" in caplog.text


def _eintrag(jahr, arbeitspreis=1.0):
    return NetzentgeltEintrag(
        stadt="Teststadt",
        netzbetreiber="Testnetz",
        netzbetreiber_typ="Verteilnetz",
        netzebene="Niederspannung",
        tarifsystem="Grundpreis_Arbeitspreis",
        benutzungsdauer_klasse=None,
        grundpreis_eur_jahr=100.0,
        arbeitspreis_ct_kwh=arbeitspreis,
        leistungspreis_eur_kw_jahr=None,
        leistungspreis_eur_kw_monat=None,
        jahr=jahr,
        stand_dokument=None,
        quelle_url="http://test/pb.pdf",
    )


def test_crawl_structural_keeps_other_years(monkeypatch, tmp_path):
    """A new year must not delete the price sheets of the previous year, and a
    re-run replaces only the rows of the crawled year."""
    config = empty_config()
    config["db_uri"] = f"sqlite:///{tmp_path}/netzentgelte.db"
    crawler = nz.NetzentgeltCrawler("netzentgelte", config)
    monkeypatch.setattr(
        nz, "collect_entries", lambda **kw: ([_eintrag(kw["jahr"])], [])
    )

    crawler.crawl_structural()
    crawler.jahr = 2027
    crawler.crawl_structural()
    df = pd.read_sql(
        "SELECT jahr, arbeitspreis_ct_kwh FROM netzentgelte", crawler.engine
    )
    assert sorted(df["jahr"]) == [2026, 2027]

    monkeypatch.setattr(
        nz, "collect_entries", lambda **kw: ([_eintrag(kw["jahr"], 9.9)], [])
    )
    crawler.jahr = 2026
    crawler.crawl_structural(recreate=True)
    df = pd.read_sql(
        "SELECT jahr, arbeitspreis_ct_kwh FROM netzentgelte", crawler.engine
    )
    assert sorted(df["jahr"]) == [2026, 2027]
    assert df.loc[df.jahr == 2026, "arbeitspreis_ct_kwh"].tolist() == [9.9]
    assert df.loc[df.jahr == 2027, "arbeitspreis_ct_kwh"].tolist() == [1.0]
