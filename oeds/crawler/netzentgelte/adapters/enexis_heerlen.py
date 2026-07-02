# SPDX-FileCopyrightText: Christoph Komanns
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Adapter for Enexis Netbeheer B.V. - distribution system operator for Heerlen (NL,
province of Limburg).

IMPORTANT CONTEXT (please read): Heerlen is in the Netherlands. The Dutch grid
fee system is NOT comparable to the German one and only approximately fits the
(Germany-centric) schema of this tool:

- For households ("kleinverbruik", connection up to 3x80 A) there is NO energy
  price in ct/kWh. Grid costs are a pure CAPACITY CHARGE: a fixed annual amount
  per connection size (aansluitwaarde, in amperes), regulated by the ACM.
- There are NO household fees tiered by voltage level.
- Published amounts INCLUDE 21% btw (VAT), not net as with German VNBs.

This adapter therefore maps the standard household connection
(">1 x 10 t/m 3 x 25 A", the typical house connection) as ONE entry:
- netzebene = Niederspannung, tarifsystem = Grundpreis_Arbeitspreis
- grundpreis_eur_jahr = annual grid costs for this connection (incl. btw)
- arbeitspreis_ct_kwh = None (does not exist for kleinverbruik)
- netto_oder_brutto = "brutto"

German columns (capacity price, utilisation hours, voltage level tiers) remain
empty because they simply do not exist here. A direct numerical comparison with
German cities is therefore only of limited use.

Verified against: enexis.nl, tariff sheet "Elektriciteit | Kleinverbruik",
Netbeheerkosten 1 januari 2026, Versie 1.0.
"""

from __future__ import annotations

import re

from ..schema import NetzentgeltEintrag
from . import _common as c

NETZBETREIBER = "Enexis Netbeheer B.V."


def parse(
    text: str, stadt: str, jahr: int, quelle_url: str
) -> list[NetzentgeltEintrag]:
    out: list[NetzentgeltEintrag] = []
    m = re.search(r"Versie\s+([\d.]+)", text)
    stand = f"Versie {m.group(1)}" if m else None

    # Table "Aansluiting met elektriciteitsmeter": per connection size
    # Per dag / Per maand / Per jaar (incl. btw). We use the standard household
    # connection 3 x 25 A and take the annual amount (last number).
    sec = c.slice_between(
        text, "Aansluiting met elektriciteitsmeter", "Aansluiting zonder"
    )
    for line in sec.splitlines():
        if re.search(r"3\s*[\u00d7x]\s*25", line):  # "3 x 25" or "3 × 25"
            nums = c.decimals(line)
            if nums:
                out.append(
                    NetzentgeltEintrag(
                        stadt=stadt,
                        netzbetreiber=NETZBETREIBER,
                        netzbetreiber_typ="Verteilnetz",
                        netzebene="Niederspannung",
                        tarifsystem="Grundpreis_Arbeitspreis",
                        benutzungsdauer_klasse=None,
                        grundpreis_eur_jahr=nums[-1],
                        arbeitspreis_ct_kwh=None,
                        leistungspreis_eur_kw_jahr=None,
                        leistungspreis_eur_kw_monat=None,
                        jahr=jahr,
                        stand_dokument=stand,
                        quelle_url=quelle_url,
                        netto_oder_brutto="brutto",
                        mwst_prozent=21.0,
                    )
                )
            break

    return out
