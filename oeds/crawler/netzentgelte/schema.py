# SPDX-FileCopyrightText: Christoph Komanns
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unified data model for electricity grid fees (Germany).

Each row in the final output is a NetzentgeltEintrag: one city, one grid
operator, one voltage level, one price component.

Key terminology (see README):
- "Niederspannung / Grundpreis-Arbeitspreissystem" -> the tariff for normal
  household customers without interval metering.
- "Niederspannung / Jahres-/Monatsleistungspreissystem" -> for commercial RLM
  customers with interval metering, NOT for normal households.
- Distribution voltage levels above low voltage (MS/NS transformation, medium
  voltage, HS/MS transformation, high voltage) are included in the same
  price sheet of the respective distribution operator (VNB).
- The transmission grid (extra-high voltage + HÖS/HS transformation) has been
  nationwide uniform since 2023 and does NOT depend on the individual city/VNB.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# Canonical voltage level names, in order from generation/transmission to the
# household connection.
NETZEBENEN = [
    "Hoechstspannung",  # HÖS - transmission grid
    "Umspannung_HOES_HS",  # transmission/distribution boundary
    "Hochspannung",  # HS - distribution grid
    "Umspannung_HS_MS",  # distribution grid
    "Mittelspannung",  # MS - distribution grid
    "Umspannung_MS_NS",  # distribution grid
    "Niederspannung",  # NS - distribution grid (household connection)
]

TARIFSYSTEME = [
    "Grundpreis_Arbeitspreis",  # household customers / SLP without interval metering
    "Jahresleistungspreis",  # RLM customers, annual capacity price system
    "Monatsleistungspreis",  # RLM customers, monthly capacity price system
]

# Customer type per tariff system:
# - SLP (Standardlastprofil): household/small customers without interval metering,
#   billed via base price + energy price.
# - RLM (registrierende Leistungsmessung): commercial/large customers with interval
#   metering, billed via capacity and energy price.
KUNDENTYP_JE_TARIFSYSTEM = {
    "Grundpreis_Arbeitspreis": "SLP",
    "Jahresleistungspreis": "RLM",
    "Monatsleistungspreis": "RLM",
}


def _brutto(netto: float | None, faktor: float) -> float | None:
    """Compute gross value from net value (None stays None)."""
    if netto is None:
        return None
    return round(netto * faktor, 5)


@dataclass
class NetzentgeltEintrag:
    stadt: str  # city requested by the user
    netzbetreiber: str  # name of the grid operator (VNB or ÜNB)
    netzbetreiber_typ: str  # "Verteilnetz" or "Uebertragungsnetz"
    netzebene: str  # one of NETZEBENEN
    tarifsystem: str  # one of TARIFSYSTEME
    benutzungsdauer_klasse: str | None  # "<2500h" / ">=2500h" / None
    grundpreis_eur_jahr: float | None
    arbeitspreis_ct_kwh: float | None
    leistungspreis_eur_kw_jahr: float | None
    leistungspreis_eur_kw_monat: float | None
    jahr: int  # validity year of the price sheet
    stand_dokument: str | None  # version/effective date per PDF
    quelle_url: str
    # Basis in which the source publishes the prices above.
    netto_oder_brutto: str = "netto"
    # VAT rate in percent for net<->gross conversion. Germany 19;
    # foreign adapters differ (NL 21, BE 6).
    mwst_prozent: float = 19.0
    # Derived automatically from the tariff system in __post_init__.
    kundentyp: str | None = None
    # Gross counterparts of the price components, computed in __post_init__.
    grundpreis_eur_jahr_brutto: float | None = None
    arbeitspreis_ct_kwh_brutto: float | None = None
    leistungspreis_eur_kw_jahr_brutto: float | None = None
    leistungspreis_eur_kw_monat_brutto: float | None = None

    def __post_init__(self) -> None:
        if self.kundentyp is None:
            self.kundentyp = KUNDENTYP_JE_TARIFSYSTEM.get(self.tarifsystem)
        # Compute gross values. If the source already publishes gross amounts
        # (e.g. NL/BE), the gross columns match the source values.
        faktor = (
            1.0 if self.netto_oder_brutto == "brutto" else 1 + self.mwst_prozent / 100
        )
        if self.grundpreis_eur_jahr_brutto is None:
            self.grundpreis_eur_jahr_brutto = _brutto(self.grundpreis_eur_jahr, faktor)
        if self.arbeitspreis_ct_kwh_brutto is None:
            self.arbeitspreis_ct_kwh_brutto = _brutto(self.arbeitspreis_ct_kwh, faktor)
        if self.leistungspreis_eur_kw_jahr_brutto is None:
            self.leistungspreis_eur_kw_jahr_brutto = _brutto(
                self.leistungspreis_eur_kw_jahr, faktor
            )
        if self.leistungspreis_eur_kw_monat_brutto is None:
            self.leistungspreis_eur_kw_monat_brutto = _brutto(
                self.leistungspreis_eur_kw_monat, faktor
            )

    def as_dict(self) -> dict:
        return asdict(self)


CSV_FIELDS = [
    "stadt",
    "netzbetreiber",
    "netzbetreiber_typ",
    "netzebene",
    "tarifsystem",
    "kundentyp",
    "benutzungsdauer_klasse",
    "grundpreis_eur_jahr",
    "grundpreis_eur_jahr_brutto",
    "arbeitspreis_ct_kwh",
    "arbeitspreis_ct_kwh_brutto",
    "leistungspreis_eur_kw_jahr",
    "leistungspreis_eur_kw_jahr_brutto",
    "leistungspreis_eur_kw_monat",
    "leistungspreis_eur_kw_monat_brutto",
    "jahr",
    "stand_dokument",
    "quelle_url",
    "netto_oder_brutto",
    "mwst_prozent",
]
