# SPDX-FileCopyrightText: Christoph Komanns
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Adapter for Regionetz GmbH - distribution system operator for Aachen and the
StädteRegion Aachen (joint venture of STAWAG and EWV).

Layout: numbered "Preisblätter", label always BEFORE the numbers.
Relevant:
- Price sheet 1: annual capacity price system per grid/transformation level.
- Price sheet 4: household customers without interval metering (low voltage).

Columns in the annual capacity price system:
    Capacity price[<2500h]  Energy price[<2500h]  Capacity price[>=2500h]  Energy price[>=2500h]

Verified against: regionetz.de, "Preisblätter Strom Regionetz 2026", valid from
01.01.2026.
"""

from __future__ import annotations

import re

from ..schema import NetzentgeltEintrag
from . import _common as c

NETZBETREIBER = "Regionetz GmbH"

# Order of voltage level rows in the annual capacity price system (price sheet 1):
# High voltage (HS) / HS/MS transformation / Medium voltage (MS) / MS/NS transformation / Low voltage (NS)
JLP_EBENEN = [
    "Hochspannung",
    "Umspannung_HS_MS",
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
        "Jahresleistungspreissystem für Entnahme mit registrierender Lastgangmessung",
        "Monatsleistungspreissystem",
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

    # Household tariff low voltage (price sheet 4):
    # "Niederspannung 41,65 49,56 9,32 11,09" -> base price net/gross, energy price net/gross
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
