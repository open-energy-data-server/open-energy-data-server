# SPDX-FileCopyrightText: Christoph Komanns
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Small shared helper functions for the VNB adapters.

Deliberately minimal: there is NO universal parser for all grid operators (see
README, section "Why no single parser for all"). Each adapter remains
responsible for its own PDF layout and only uses what is truly common from
here:

- German/Dutch number format (thousands separator, decimal comma),
- extracting a section between two text markers,
- collecting table data rows by the number of numbers per line (robust against
  pdfplumber placing row labels before or after the number columns).

This module starts with "_" and is therefore NOT loaded as an adapter by the
pipeline.
"""

from __future__ import annotations

import re

# e.g. "1.971,0398", "150,00", "-137,13", "11,06315"
_DECIMAL_RE = re.compile(r"-?\d{1,3}(?:\.\d{3})*,\d+")


def de_float(s: str) -> float:
    """German number format -> float ("1.234,56" -> 1234.56)."""
    return float(s.replace(".", "").replace(",", "."))


def decimals(line: str) -> list[float]:
    """All decimal numbers in a line as floats, in reading order.

    Integers without a decimal comma (e.g. the "2.500" in "< 2.500 h/a") are
    deliberately ignored - only real price values with fractional digits count.
    """
    return [de_float(m) for m in _DECIMAL_RE.findall(line)]


def slice_between(text: str, start: str, end: str | None = None) -> str:
    """Section between the first occurrence of `start` and the first subsequent
    occurrence of `end` (or until end of text if end=None)."""
    i = text.find(start)
    if i < 0:
        return ""
    i += len(start)
    if end is None:
        return text[i:]
    j = text.find(end, i)
    return text[i:] if j < 0 else text[i:j]


def numeric_rows(section: str, count: int, min_numbers: int = 4) -> list[list[float]]:
    """The first `count` lines from `section` that contain at least `min_numbers`
    decimal numbers - returned as a list of their numbers per line.

    This reliably extracts tables such as the annual capacity price system even
    when the voltage level label is on its own line before or after the numbers:
    the order of data rows is preserved.
    """
    rows: list[list[float]] = []
    for line in section.splitlines():
        nums = decimals(line)
        if len(nums) >= min_numbers:
            rows.append(nums)
            if len(rows) >= count:
                break
    return rows
