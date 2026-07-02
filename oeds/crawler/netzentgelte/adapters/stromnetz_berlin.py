# SPDX-FileCopyrightText: Christoph Komanns
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Adapter for Stromnetz Berlin GmbH (distribution system operator for Berlin).

Stromnetz Berlin (like many German VNBs) publishes its price sheet in EDI code
list format: each line starts with an ID like "1-01-7-002", followed by a
label, net price, gross price, unit.

This format is NOT a nationwide standard, but very common because many VNBs
follow the BDEW market rules format. Other VNBs (e.g. SWM Infrastruktur,
Bayernwerk Netz) use different layouts - each needs its own thin adapter (see
README, section "Adding a new grid operator").

Verified against: stromnetz.berlin, price sheet valid from 01.01.2026,
version 15.10.2025 (provisional).
"""

from __future__ import annotations

import re

from ..schema import NetzentgeltEintrag

NETZBETREIBER = "Stromnetz Berlin GmbH"

NUM = r"(-?[\d.]+,\d+)"


def _de_float(s: str) -> float:
    return float(s.replace(".", "").replace(",", "."))


# Annual capacity price system rows, e.g.:
# "1-01-7-001 Jahresleistungspreissystem Niederspannung Jahresbenutzungsdauerstunden < 2.500 h/a Leistungspreis 8,39 9,98 €/kW/a"
JLP_LP = re.compile(
    r"Jahresleistungspreissystem (Hochspannung|Mittelspannung|Niederspannung|"
    r"Umspannung Hoch-/Mittelspannung|Umspannung Mittel-/Niederspannung) "
    r"Jahresbenutzungsdauerstunden (< 2\.500 h/a|>= 2\.500 h/a) Leistungspreis "
    + NUM
    + r" "
    + NUM
)
JLP_AP = re.compile(
    r"Jahresleistungspreissystem (Hochspannung|Mittelspannung|Niederspannung|"
    r"Umspannung Hoch-/Mittelspannung|Umspannung Mittel-/Niederspannung) "
    r"Jahresbenutzungsdauerstunden (< 2\.500 h/a|>= 2\.500 h/a) Arbeitspreis "
    + NUM
    + r" "
    + NUM
)

# Base price / energy price system (= household customer tariff, low voltage)
GP = re.compile(
    r"Grundpreis-/ Arbeitspreissystem Marktlokation Grundpreis für "
    r"Arbeitspreissystem Grundpreis " + NUM + r" " + NUM
)
AP_HAUSHALT = re.compile(
    r"Arbeitspreis Standardlasttarifzeit \(sonst\. Verbrauch.*?\) "
    r".*?abgerechnet werden\s+" + NUM + r" " + NUM,
    re.DOTALL,
)

NETZEBENE_MAP = {
    "Hochspannung": "Hochspannung",
    "Mittelspannung": "Mittelspannung",
    "Niederspannung": "Niederspannung",
    "Umspannung Hoch-/Mittelspannung": "Umspannung_HS_MS",
    "Umspannung Mittel-/Niederspannung": "Umspannung_MS_NS",
}
BENUTZUNGSDAUER_MAP = {
    "< 2.500 h/a": "<2500h",
    ">= 2.500 h/a": ">=2500h",
}


def parse(
    text: str, stadt: str, jahr: int, quelle_url: str
) -> list[NetzentgeltEintrag]:
    out: list[NetzentgeltEintrag] = []

    stand_match = re.search(r"Version (\d{2}\.\d{2}\.\d{4})", text)
    stand = stand_match.group(1) if stand_match else None

    def make(
        netzebene,
        tarifsystem,
        benutzungsdauer,
        grundpreis,
        arbeitspreis,
        leistungspreis_jahr,
    ):
        out.append(
            NetzentgeltEintrag(
                stadt=stadt,
                netzbetreiber=NETZBETREIBER,
                netzbetreiber_typ="Verteilnetz",
                netzebene=netzebene,
                tarifsystem=tarifsystem,
                benutzungsdauer_klasse=benutzungsdauer,
                grundpreis_eur_jahr=grundpreis,
                arbeitspreis_ct_kwh=arbeitspreis,
                leistungspreis_eur_kw_jahr=leistungspreis_jahr,
                leistungspreis_eur_kw_monat=None,
                jahr=jahr,
                stand_dokument=stand,
                quelle_url=quelle_url,
            )
        )

    # Collect capacity price per voltage level/utilisation hours, then merge with energy price
    lp_values: dict[tuple[str, str], float] = {}
    for m in JLP_LP.finditer(text):
        ebene, dauer, lp, _ = m.group(1), m.group(2), m.group(3), m.group(4)
        lp_values[(ebene, dauer)] = _de_float(lp)

    for m in JLP_AP.finditer(text):
        ebene, dauer, ap, _ = m.group(1), m.group(2), m.group(3), m.group(4)
        netzebene = NETZEBENE_MAP[ebene]
        benutzungsdauer = BENUTZUNGSDAUER_MAP[dauer]
        lp = lp_values.get((ebene, dauer))
        make(
            netzebene, "Jahresleistungspreis", benutzungsdauer, None, _de_float(ap), lp
        )

    # Household customer tariff (base price / energy price system, low voltage)
    gp_match = GP.search(text)
    grundpreis = _de_float(gp_match.group(1)) if gp_match else None
    ap_match = AP_HAUSHALT.search(text)
    arbeitspreis_haushalt = _de_float(ap_match.group(1)) if ap_match else None

    if grundpreis is not None or arbeitspreis_haushalt is not None:
        make(
            "Niederspannung",
            "Grundpreis_Arbeitspreis",
            None,
            grundpreis,
            arbeitspreis_haushalt,
            None,
        )

    return out
