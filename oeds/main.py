#!/usr/bin/env python3
# SPDX-FileCopyrightText: Florian Maurer
#
# SPDX-License-Identifier: AGPL-3.0-or-later
import argparse
import logging
import os
from pathlib import Path

from oeds.base_crawler import (
    BaseCrawler,
    ContinuousCrawler,
    DownloadOnceCrawler,
    empty_config,
    load_config,
)

log = logging.getLogger("OEDS")
log.setLevel(logging.INFO)


def start_crawler(crawler: BaseCrawler):
    if isinstance(crawler, DownloadOnceCrawler):
        crawler.crawl_structural()
    if isinstance(crawler, ContinuousCrawler):
        crawler.crawl_temporal()


def cli(args=None):
    parser = argparse.ArgumentParser(description="Open-Energy-Data-Server CLI")
    parser.add_argument(
        "--db",
        type=str,
        help="set the DB URI",
    )
    parser.add_argument(
        "--crawler-list", nargs="*", help="List of crawlers to run (default: all)"
    )
    parser.add_argument(
        "-l",
        "--loglevel",
        help="logging level used for file log",
        default="INFO",
        type=str,
        metavar="LOGLEVEL",
        choices=set(logging._nameToLevel.keys()),
    )
    parser.add_argument("--config", type=str, help="Path to config.yaml")
    parsed_args = parser.parse_args(args)

    logging.basicConfig(level=parsed_args.loglevel)
    from oeds.crawler import crawlers

    if parsed_args.crawler_list:
        selected_crawlers = []
        for item in parsed_args.crawler_list:
            selected_crawlers.extend([c.strip() for c in item.split(",") if c.strip()])
    else:
        selected_crawlers = list(crawlers.keys())

    if parsed_args.config:
        config = load_config(parsed_args.config)
    elif os.getenv("OEDS_CONFIG") and Path(os.getenv("OEDS_CONFIG")).exists():
        config = load_config(os.getenv("OEDS_CONFIG"))
    elif Path("config.yml").exists():
        config = load_config("config.yml")
    elif (Path(__file__).parent.parent / "config.yml").exists():
        config = load_config(Path(__file__).parent.parent / "config.yml")
    else:
        config = empty_config()

    if parsed_args.db:
        config["db_uri"] = parsed_args.db
    elif ("db_uri" not in config or not config["db_uri"]) and os.getenv("OEDS_DB_URI"):
        config["db_uri"] = os.getenv("OEDS_DB_URI")

    failed = []
    for crawler_name in selected_crawlers:
        log.info("Starting crawler: %s", crawler_name)
        try:
            crawler_class = crawlers[crawler_name]
        except KeyError:
            raise ValueError(f"crawler {crawler_name} does not exist")

        # one failing crawler must not abort the others
        try:
            crawler = crawler_class(crawler_name, config)
            start_crawler(crawler)
        except Exception:
            log.exception("crawler %s failed", crawler_name)
            failed.append(crawler_name)

    if failed:
        log.error("%d crawler(s) failed: %s", len(failed), ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
