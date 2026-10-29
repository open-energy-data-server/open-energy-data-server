# SPDX-FileCopyrightText: Florian Maurer
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# Pre-load psycopg to prevent C-runtime / dynamic linker conflicts with GDAL/pyogrio/eccodes
try:
    import psycopg  # noqa: F401
except ImportError:
    pass
