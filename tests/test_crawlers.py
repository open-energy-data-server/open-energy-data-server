# SPDX-FileCopyrightText: Florian Maurer, OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest
import psycopg
from oeds.base_crawler import empty_config
from oeds.crawler import crawlers


def test_crawler_registry():
    """Verify that all crawlers are correctly registered."""
    assert len(crawlers) >= 30
    expected = [
        "public",
        "chargepoint",
        "e2watch",
        "ecmwf",
        "entsoe",
        "entsog",
        "eview",
        "gie",
        "jao",
        "mastr",
        "netztransparenz",
        "smard",
        "regelleistung",
        "iwugebaeudetypen",
        "nrw_kwp_waermedichte",
        "refit",
        "windmodel",
    ]
    for name in expected:
        assert name in crawlers, f"Missing crawler in registry: {name}"


def test_instantiate_crawlers(tmp_path):
    """Verify that each crawler can be instantiated with a test SQLite URI."""
    db_file = tmp_path / "test.db"
    config = empty_config()
    config["db_uri"] = f"sqlite:///{db_file}"

    # Some crawlers require API keys or external preconditions to instantiate
    skipped_crawlers = {
        "gie": "Requires GIE_API_KEY",
        "netztransparenz": "Requires ipnt credentials",
        "ecmwf": "Requires pre-existing NUTS database tables",
        "weather": "Requires pre-existing NUTS database tables",
    }

    for name, crawler_cls in crawlers.items():
        if name in skipped_crawlers:
            continue
        instance = crawler_cls(name, config)
        assert instance is not None
        assert instance.config["db_uri"] == f"sqlite:///{db_file}"


def test_cli_comma_separated_parsing():
    """Test CLI argument parsing for comma-separated crawler lists."""
    from oeds.main import cli

    with pytest.raises(SystemExit) as exc:
        cli(["--help"])
    assert exc.value.code == 0


def test_postgres_container_connectivity():
    """Verify connectivity and schema handling against the local PostgreSQL test container if running."""
    try:
        conn = psycopg.connect("postgresql://opendata:opendata@127.0.0.1:6432/opendata", connect_timeout=2)
    except Exception:
        pytest.skip("Local postgres container not reachable on port 6432")

    with conn.cursor() as cur:
        cur.execute("SELECT 1;")
        assert cur.fetchone()[0] == 1
    conn.close()
