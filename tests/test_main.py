# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import oeds.crawler
import pytest
from oeds import main
from oeds.base_crawler import ContinuousCrawler


def test_failing_crawler_does_not_abort_run(monkeypatch):
    started = []

    class Broken(ContinuousCrawler):
        def __init__(self, schema_name, config):
            raise ValueError("no credentials")

    class Failing:
        def __init__(self, schema_name, config):
            pass

    class Good(Failing):
        pass

    monkeypatch.setattr(
        oeds.crawler, "crawlers", {"broken": Broken, "failing": Failing, "good": Good}
    )
    monkeypatch.setattr(
        main,
        "start_crawler",
        lambda crawler: (
            (_ for _ in ()).throw(RuntimeError("boom"))
            if type(crawler) is Failing
            else started.append(type(crawler))
        ),
    )
    args = ["--db", "sqlite://", "--config", "/nonexistent.yml"]
    monkeypatch.setattr(main, "load_config", lambda path: {})
    result = main.cli(args + ["--crawler-list", "broken", "failing", "good"])
    assert started == [Good]
    assert result == 1


def test_all_crawlers_succeed_returns_zero(monkeypatch):
    class Good:
        def __init__(self, schema_name, config):
            pass

    monkeypatch.setattr(oeds.crawler, "crawlers", {"good": Good})
    monkeypatch.setattr(main, "start_crawler", lambda crawler: None)
    assert main.cli(["--db", "sqlite://", "--crawler-list", "good"]) == 0


def test_unknown_crawler_raises(monkeypatch):
    monkeypatch.setattr(oeds.crawler, "crawlers", {})
    with pytest.raises(ValueError):
        main.cli(["--db", "sqlite://", "--crawler-list", "nope"])
