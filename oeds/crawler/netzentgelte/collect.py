# SPDX-FileCopyrightText: Christoph Komanns
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Collect grid fee entries for all cities configured in ``cities.yaml`` and add
the nationwide transmission grid fees (ÜNB).

This is the OEDS crawler variant of the original ``pipeline.run()`` from the
netzentgelte tool: instead of writing a CSV, ``collect_entries`` returns the
entries as a list so the crawler can write them to the database. The domain
logic (which city -> which VNB -> which adapter, plus the nationwide
transmission part) is unchanged.

IMPORTANT: ``collect_entries`` requires real internet access - the price sheets
are PDFs on the websites of the individual grid operators or on
netztransparenz.de. Without network access, only the parsers themselves can be
tested against saved text excerpts (see tests/).
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import pdfplumber
import requests
import yaml

from .adapters import (
    enexis_heerlen,
    leitungspartner,
    regionetz,
    resa_luettich,
    rheinnetz,
    stadtwerke_juelich,
    stromnetz_berlin,
)
from .fetch_uebertragungsnetz import DEFAULT_UENB_PDF_URL, fetch_uenb_entries
from .schema import NetzentgeltEintrag

log = logging.getLogger("netzentgelte")

# Vendored city -> VNB -> price sheet URL -> adapter mapping. Must be maintained
# manually per year or when adding new cities (see file comment there).
DEFAULT_CITIES_YAML = Path(__file__).parent / "cities.yaml"

# Static mapping from the ``adapter`` name in cities.yaml to its parse function.
# One small, verified parser per distribution operator - there is deliberately
# no universal parser (every operator uses its own price sheet layout). New
# operators are added here together with their adapter module.
ADAPTERS = {
    "enexis_heerlen": enexis_heerlen.parse,
    "leitungspartner": leitungspartner.parse,
    "regionetz": regionetz.parse,
    "resa_luettich": resa_luettich.parse,
    "rheinnetz": rheinnetz.parse,
    "stadtwerke_juelich": stadtwerke_juelich.parse,
    "stromnetz_berlin": stromnetz_berlin.parse,
}


def _download_pdf_text(url: str) -> str:
    resp = requests.get(
        url, timeout=30, headers={"User-Agent": "open-energy-data-server/netzentgelte"}
    )
    resp.raise_for_status()
    parts = []
    with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(parts)


def collect_entries(
    cities_yaml: str | Path = DEFAULT_CITIES_YAML,
    jahr: int = 2026,
    uenb_url: str = DEFAULT_UENB_PDF_URL,
) -> tuple[list[NetzentgeltEintrag], list[str]]:
    """Fetch and parse all price sheets and return ``(entries, warnings)``.

    Never silently produces wrong or guessed numbers: if a city has no adapter
    or download/parsing fails, that is recorded as plain text in ``warnings``
    and the city is simply missing from the result.
    """
    with open(cities_yaml, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    all_entries: list[NetzentgeltEintrag] = []
    warnings: list[str] = []

    # 1) Transmission grid: once per run, applies nationwide to all German cities.
    try:
        uenb_entries = fetch_uenb_entries(jahr=jahr, pdf_url=uenb_url)
        all_entries.extend(uenb_entries)
        log.info("Transmission grid (nationwide): %d entries", len(uenb_entries))
    except Exception as exc:
        warnings.append(f"Transmission grid could not be loaded: {exc}")
        log.warning(warnings[-1])

    # 2) Distribution grid: per city, depending on the responsible VNB.
    for city in config.get("cities", []):
        name = city["name"]
        vnb = city["vnb"]
        url = city["preisblatt_url"]
        adapter_name = city.get("adapter")

        if not adapter_name:
            warnings.append(
                f"{name}: no adapter configured for VNB '{vnb}'. "
                f"Price sheet URL is on file ({url}), but must be parsed manually "
                f"or automated via a new adapter."
            )
            log.warning(warnings[-1])
            continue

        parse = ADAPTERS.get(adapter_name)
        if parse is None:
            warnings.append(
                f"{name} ({vnb}): unknown adapter '{adapter_name}' - "
                f"not registered in ADAPTERS."
            )
            log.warning(warnings[-1])
            continue

        try:
            text = _download_pdf_text(url)
        except Exception as exc:
            warnings.append(f"{name} ({vnb}): download failed: {exc}")
            log.warning(warnings[-1])
            continue

        try:
            entries = parse(text, stadt=name, jahr=jahr, quelle_url=url)
            all_entries.extend(entries)
            log.info("%s (%s): %d entries", name, vnb, len(entries))
        except Exception as exc:
            warnings.append(f"{name} ({vnb}): parser error: {exc}")
            log.warning(warnings[-1])

    return all_entries, warnings
