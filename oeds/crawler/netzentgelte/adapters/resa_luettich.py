# SPDX-FileCopyrightText: Christoph Komanns
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Adapter for RESA SA - distribution system operator for Liège (BE, Wallonia).

IMPORTANT CONTEXT (please read): Liège is in Belgium (Wallonia). Walloon
distribution grid fees are set by the regulator CWaPE (tariff period 2026-2029)
and follow a completely different logic from Germany:

- There is a "terme fixe" (fixed annual amount, €/an) and a "terme
  proportionnel" (energy price, c€/kWh), the latter depending on meter type
  (simple/24h, bihoraire off-peak/peak hours, Impact ECO/MEDIUM/PIC tariff).
- NO household fees tiered by voltage level.
- Published amounts are TVAC, i.e. INCLUDING 6% Belgian VAT (for households),
  not net.

This adapter maps the standard household case (simple/24h meter):
- netzebene = Niederspannung, tarifsystem = Grundpreis_Arbeitspreis
- grundpreis_eur_jahr = terme fixe (€/an, TVAC)
- arbeitspreis_ct_kwh = terme proportionnel "simple" (c€/kWh, TVAC)
- netto_oder_brutto = "brutto"

The Belgian "terme proportionnel" bundles more than the German distribution
grid fee; a direct numerical comparison with German cities is therefore only
of limited use.

The source is the official CWaPE synthesis of all Walloon VNBs; this adapter
reads the row of operator RESA specifically.

Verified against: cwape.be, "Synthèse des tarifs de distribution élec 2026 (FR)",
valid 01.01.2026-31.12.2026.
"""

from __future__ import annotations

import re

from ..schema import NetzentgeltEintrag
from . import _common as c

NETZBETREIBER = "RESA SA"


def parse(
    text: str, stadt: str, jahr: int, quelle_url: str
) -> list[NetzentgeltEintrag]:
    out: list[NetzentgeltEintrag] = []
    m = re.search(r"Mise à jour du\s+([\d.]+)", text)
    stand = m.group(1) if m else None

    # Synthesis table row starting with "RESA "
    # (not "RÉSEAU DES ÉNERGIES DE WAVRE"):
    #   RESA  <terme fixe €/an>  <simple c€/kWh>  <bihoraire HC>  <bihoraire HP> ...
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("RESA ") and not s.startswith("RÉSEAU"):
            nums = c.decimals(line)
            if len(nums) >= 2:
                out.append(
                    NetzentgeltEintrag(
                        stadt=stadt,
                        netzbetreiber=NETZBETREIBER,
                        netzbetreiber_typ="Verteilnetz",
                        netzebene="Niederspannung",
                        tarifsystem="Grundpreis_Arbeitspreis",
                        benutzungsdauer_klasse=None,
                        grundpreis_eur_jahr=nums[0],
                        arbeitspreis_ct_kwh=nums[1],
                        leistungspreis_eur_kw_jahr=None,
                        leistungspreis_eur_kw_monat=None,
                        jahr=jahr,
                        stand_dokument=stand,
                        quelle_url=quelle_url,
                        netto_oder_brutto="brutto",
                        mwst_prozent=6.0,
                    )
                )
            break

    return out
