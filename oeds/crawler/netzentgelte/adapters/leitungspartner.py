# SPDX-FileCopyrightText: Christoph Komanns
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Adapter for Leitungspartner GmbH - distribution system operator for Düren and
Merzenich (subsidiary of Stadtwerke Düren GmbH).

NOTE on city->VNB mapping: the grid operator in Düren is NOT Stadtwerke Düren
GmbH (that is the supplier/default provider), but Leitungspartner GmbH.

Layout: numbered "Preisblätter". pdfplumber wraps some two-line labels in the
annual capacity price system ("... einschl. Umspannung") such that the numbers
are on their own line. Data rows are therefore mapped to voltage levels by
their order (not by the label).

Voltage level order in the annual capacity price system (price sheet 1):
    Hochspannungsnetz einschl. Umspannung  -> Hochspannung
    Mittelspannungsnetz                    -> Mittelspannung
    Mittelspannungsnetz einschl. Umspannung-> Umspannung MS/NS
    Niederspannungsnetz                    -> Niederspannung

Columns:
    Capacity price[<2500h]  Energy price[<2500h]  Capacity price[>=2500h]  Energy price[>=2500h]

Verified against: leitungspartner.de, "Preisblatt Netzentgelte Strom", valid from
01.01.2026.
"""

from __future__ import annotations

import re

from ..schema import NetzentgeltEintrag
from . import _common as c

NETZBETREIBER = "Leitungspartner GmbH"

JLP_EBENEN = [
    "Hochspannung",
    "Mittelspannung",
    "Umspannung_MS_NS",
    "Niederspannung",
]


def parse(
    text: str, stadt: str, jahr: int, quelle_url: str
) -> list[NetzentgeltEintrag]:
    out: list[NetzentgeltEintrag] = []
    m = re.search(r"[Gg]ültig ab:?\s*(\d{1,2}\.\d{1,2}\.\d{4})", text)
    stand = m.group(1) if m else None

    def add(**kw):
        base = dict(
            stadt=stadt,
            netzbetreiber=NETZBETREIBER,
            netzbetreiber_typ="Verteilnetz",
            netzebene=None,
            tarifsystem=None,
            benutzungsdauer_klasse=None,
            grundpreis_eur_jahr=None,
            arbeitspreis_ct_kwh=None,
            leistungspreis_eur_kw_jahr=None,
            leistungspreis_eur_kw_monat=None,
            jahr=jahr,
            stand_dokument=stand,
            quelle_url=quelle_url,
        )
        base.update(kw)
        out.append(NetzentgeltEintrag(**base))

    # Annual capacity price system (price sheet 1)
    jlp = c.slice_between(
        text,
        "Jahresleistungspreissystem für Entnahme mit registrierender Leistungsmessung",
        "Entgelte für den Messstellenbetrieb",
    )
    for ebene, r in zip(JLP_EBENEN, c.numeric_rows(jlp, len(JLP_EBENEN))):
        lp_klein, ap_klein, lp_gross, ap_gross = r[:4]
        add(
            netzebene=ebene,
            tarifsystem="Jahresleistungspreis",
            benutzungsdauer_klasse="<2500h",
            leistungspreis_eur_kw_jahr=lp_klein,
            arbeitspreis_ct_kwh=ap_klein,
        )
        add(
            netzebene=ebene,
            tarifsystem="Jahresleistungspreis",
            benutzungsdauer_klasse=">=2500h",
            leistungspreis_eur_kw_jahr=lp_gross,
            arbeitspreis_ct_kwh=ap_gross,
        )

    # Household tariff low voltage (price sheet 3):
    # "Niederspannungsnetz 76,65 91,21 7,23 8,60" -> base price net/gross, energy price net/gross
    hh = c.slice_between(text, "Haushaltsbedarf")
    for line in hh.splitlines():
        if line.strip().startswith("Niederspannung"):
            nums = c.decimals(line)
            if len(nums) >= 4:
                add(
                    netzebene="Niederspannung",
                    tarifsystem="Grundpreis_Arbeitspreis",
                    grundpreis_eur_jahr=nums[0],
                    arbeitspreis_ct_kwh=nums[2],
                )
                break

    return out
