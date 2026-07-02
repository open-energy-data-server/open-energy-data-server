# SPDX-FileCopyrightText: Christoph Komanns
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Adapter for Rheinische NETZGesellschaft mbH (RheinNetz) - distribution system
operator for Cologne.

RheinNetz does NOT publish its price sheet in EDI code list format (unlike
Stromnetz Berlin), but as numbered "Preisblätter 1..14". Relevant for this
tool:
- Price sheet 1: household customers without interval metering (low voltage,
  base price + energy price).
- Price sheet 4: annual (capacity) price system per grid/transformation level
  (RLM customers).

Columns in the annual price system (price sheet 4):
    Capacity price[<2500h]  Energy price[<2500h]  Capacity price[>=2500h]  Energy price[>=2500h]

Verified against: rheinnetz.de, "Netznutzungsentgelte Strom 2026", valid from
01.01.2026 (provisional, as of 10.10.2025).
"""

from __future__ import annotations

import re

from ..schema import NetzentgeltEintrag
from . import _common as c

NETZBETREIBER = "Rheinische NETZGesellschaft mbH (RheinNetz)"

# Order of voltage level rows in the annual price system (price sheet 4).
JLP_EBENEN = [
    "Hochspannung",
    "Umspannung_HS_MS",  # "Hochspannung mit Umspannung auf MS"
    "Mittelspannung",
    "Umspannung_MS_NS",  # "Mittelspannung mit Umspannung auf NS"
    "Niederspannung",
]


def parse(
    text: str, stadt: str, jahr: int, quelle_url: str
) -> list[NetzentgeltEintrag]:
    out: list[NetzentgeltEintrag] = []
    m = re.search(r"Stand[:\s]*?(\d{2}\.\d{2}\.\d{4})", text) or re.search(
        r"[Gg]ültig ab\s*(\d{2}\.\d{2}\.\d{4})", text
    )
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

    # Household tariff low voltage (price sheet 1):
    # "Niederspannung 150,00 178,50 3,78 4,50"  -> base price net/gross, energy price net/gross
    pb1 = c.slice_between(text, "Preisblatt 1", "Preisblatt 2")
    for line in pb1.splitlines():
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

    # Annual capacity price system (price sheet 4)
    jlp = c.slice_between(text, "Jahrespreissystem", "Monatspreissystem")
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

    return out
