# SPDX-FileCopyrightText: Christoph Komanns
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Transmission grid fees (ÜNB) - nationwide, applies to ALL of Germany.

Since 2023 (NEMoG), the four German transmission system operators (50Hertz,
Amprion, TenneT, TransnetBW) publish a uniform price sheet for extra-high
voltage and the HÖS/HS transformation level. This part is fully automatable
and city-independent: download once per year, parse, done.

Source: https://www.netztransparenz.de (section "Netzentgelte")
The PDF is typically republished once per year (provisional around early
October, final in early December for the following year).
"""

from __future__ import annotations

import io
import re

import pdfplumber
import requests

from .schema import NetzentgeltEintrag

# This URL changes every year (filename contains the publication date). If in
# doubt, look up the current link on https://www.netztransparenz.de under
# "Netzentgelte" -> "Strom" -> "Bundeseinheitliche Netzentgelte" and enter it
# here, or override NETZTRANSPARENZ_URL at call time.
DEFAULT_UENB_PDF_URL = (
    "https://www.netztransparenz.de/xspproxy/api/staticfiles/"
    "ntp-relaunch/dokumente/netzentgelte/251205_preisblatt_ben_2026.pdf"
)

NUM = r"([\d.]+,\d+)"


def _de_float(s: str) -> float:
    return float(s.replace(".", "").replace(",", "."))


def parse_uenb_text(text: str, jahr: int, quelle_url: str) -> list[NetzentgeltEintrag]:
    """
    Parse the full text of the transmission price sheet obtained via
    pdfplumber/PDF text extraction. Works for the layout used by
    50Hertz/Amprion/TenneT/TransnetBW since 2023 (verified against 2024-2026 PDFs).
    """
    out: list[NetzentgeltEintrag] = []

    stand_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
    stand = stand_match.group(1) if stand_match else None

    # Annual capacity price system row, e.g.:
    # "Höchstspannung (HÖS) 11,39 2,36 53,06 0,69"
    # Source column order: EP(<2500h) CP(<2500h) EP(>=2500h) CP(>=2500h)
    jlp_pattern = re.compile(
        r"(Höchstspannung \(HÖS\)|Umspannung \(HÖS/HS\))\s+"
        rf"{NUM}\s+{NUM}\s+{NUM}\s+{NUM}"
    )
    netzebene_map = {
        "Höchstspannung (HÖS)": "Hoechstspannung",
        "Umspannung (HÖS/HS)": "Umspannung_HOES_HS",
    }

    seen_jlp = set()
    for m in jlp_pattern.finditer(text):
        bezeichnung = m.group(1)
        if bezeichnung in seen_jlp:
            continue
        seen_jlp.add(bezeichnung)
        ap_klein, lp_klein, ap_gross, lp_gross = (
            _de_float(m.group(2)),
            _de_float(m.group(3)),
            _de_float(m.group(4)),
            _de_float(m.group(5)),
        )
        netzebene = netzebene_map[bezeichnung]
        out.append(
            NetzentgeltEintrag(
                stadt="(bundeseinheitlich)",
                netzbetreiber="ÜNB (50Hertz/Amprion/TenneT/TransnetBW)",
                netzbetreiber_typ="Uebertragungsnetz",
                netzebene=netzebene,
                tarifsystem="Jahresleistungspreis",
                benutzungsdauer_klasse="<2500h",
                grundpreis_eur_jahr=None,
                arbeitspreis_ct_kwh=ap_klein,
                leistungspreis_eur_kw_jahr=lp_klein,
                leistungspreis_eur_kw_monat=None,
                jahr=jahr,
                stand_dokument=stand,
                quelle_url=quelle_url,
            )
        )
        out.append(
            NetzentgeltEintrag(
                stadt="(bundeseinheitlich)",
                netzbetreiber="ÜNB (50Hertz/Amprion/TenneT/TransnetBW)",
                netzbetreiber_typ="Uebertragungsnetz",
                netzebene=netzebene,
                tarifsystem="Jahresleistungspreis",
                benutzungsdauer_klasse=">=2500h",
                grundpreis_eur_jahr=None,
                arbeitspreis_ct_kwh=ap_gross,
                leistungspreis_eur_kw_jahr=lp_gross,
                leistungspreis_eur_kw_monat=None,
                jahr=jahr,
                stand_dokument=stand,
                quelle_url=quelle_url,
            )
        )

    # Monthly capacity price system row, e.g.: "Höchstspannung (HÖS) 8,84 0,69"
    mlp_pattern = re.compile(
        r"(Höchstspannung \(HÖS\)|Umspannung \(HÖS/HS\))\s+" + NUM + r"\s+" + NUM
    )
    seen_mlp = set()
    for m in mlp_pattern.finditer(text):
        bezeichnung = m.group(1)
        key = (bezeichnung, "mlp")
        if key in seen_mlp:
            continue
        # This regex also matches parts of the JLP row; only accept when
        # NOT followed directly by 4 numbers (otherwise it is the JLP row).
        tail = text[m.end() : m.end() + 20]
        if re.match(r"\s*" + NUM, tail):
            continue
        seen_mlp.add(key)
        lp, ap = _de_float(m.group(2)), _de_float(m.group(3))
        netzebene = netzebene_map[bezeichnung]
        out.append(
            NetzentgeltEintrag(
                stadt="(bundeseinheitlich)",
                netzbetreiber="ÜNB (50Hertz/Amprion/TenneT/TransnetBW)",
                netzbetreiber_typ="Uebertragungsnetz",
                netzebene=netzebene,
                tarifsystem="Monatsleistungspreis",
                benutzungsdauer_klasse=None,
                grundpreis_eur_jahr=None,
                arbeitspreis_ct_kwh=ap,
                leistungspreis_eur_kw_jahr=None,
                leistungspreis_eur_kw_monat=lp,
                jahr=jahr,
                stand_dokument=stand,
                quelle_url=quelle_url,
            )
        )

    return out


def fetch_uenb_entries(
    jahr: int, pdf_url: str = DEFAULT_UENB_PDF_URL
) -> list[NetzentgeltEintrag]:
    """Download the current transmission price sheet and parse it. Requires real
    internet access (does not work in this repo's sandbox, but on a normal
    machine/CI runner with internet access)."""
    resp = requests.get(pdf_url, timeout=30)
    resp.raise_for_status()
    text_parts = []
    with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            text_parts.append(t)
    full_text = "\n".join(text_parts)
    return parse_uenb_text(full_text, jahr=jahr, quelle_url=pdf_url)
