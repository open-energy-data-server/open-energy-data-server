# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import UTC, datetime, timedelta

import pandas as pd
from oeds.base_crawler import empty_config
from oeds.crawler.jao_crawler import JaoCrawler
from sqlalchemy import text


def auction(identification, corridor, provisional):
    return {
        "identification": identification,
        "horizonName": "Monthly",
        "corridorCode": corridor,
        "cancelled": False,
        "marketPeriodStart": "2019-02-28T23:00:00.000+00:00",
        "marketPeriodStop": "2019-03-31T22:00:00.000+00:00",
        "bidGateClosure": "2019-02-20T08:30:00.000+00:00",
        "disputeProcessGateClosure": "2019-02-20T10:05:00.000+00:00",
        "provisionalAuctionresult": provisional,
        "results": [],
        "products": [],
        "winningParties": [],
    }


class FakeClient:
    def __init__(self, auctions):
        self.auctions = auctions
        self.bid_requests = []

    def get_auctions(self, corridor, from_date, to_date, horizon):
        return self.auctions

    def get_bids(self, auction_id):
        self.bid_requests.append(auction_id)
        return pd.DataFrame({"price": [1.0], "quantity": [2.0]})


def make_crawler():
    config = empty_config()
    config["db_uri"] = "sqlite://"
    return JaoCrawler("test", config)


def test_old_auctions_without_provisional_result_are_crawled():
    crawler = make_crawler()
    client = FakeClient([auction("CH-DE-M-BASE-190301-01", "CH-DE", None)])
    crawler.crawl_single_horizon(
        client, datetime(2019, 1, 1), datetime(2019, 4, 1), "CH-DE", "Monthly"
    )
    assert client.bid_requests == ["CH-DE-M-BASE-190301-01"]


def test_auctions_of_other_corridors_are_stored_once():
    crawler = make_crawler()
    # CEE auctions are listed for several corridors
    shared = auction("CEEXD-M-BASE-------190301-01", "CEEXD", None)
    for corridor in ("AT-CZ", "CZ-AT"):
        crawler.crawl_single_horizon(
            FakeClient([shared]),
            datetime(2019, 1, 1),
            datetime(2019, 4, 1),
            corridor,
            "Monthly",
        )
    with crawler.engine.connect() as conn:
        auctions = conn.execute(text("SELECT COUNT(*) FROM auctions")).scalar()
        bids = conn.execute(text("SELECT COUNT(*) FROM bids_monthly")).scalar()
    assert auctions == 1
    assert bids == 1


def test_auction_times_are_aware_utc():
    from oeds.crawler.jao_crawler import auctions_to_frames

    auctions_df, _ = auctions_to_frames([auction("A", "CH-DE", None)], "Monthly")
    assert str(auctions_df["date"].dt.tz) == "UTC"
    assert auctions_df["date"].iloc[0] == pd.Timestamp("2019-02-28T23:00", tz="UTC")


def test_crawl_temporal_looks_back_for_late_published_auctions():
    crawler = make_crawler()
    latest = pd.Timestamp("2024-03-01", tz="UTC")
    crawler.get_latest_data = lambda: latest
    calls = []
    crawler.crawl_from_to = lambda begin, end: calls.append(begin)
    crawler.create_hypertable_if_not_exists = lambda: None
    crawler.crawl_temporal(end=datetime(2024, 4, 1, tzinfo=UTC))
    assert calls == [latest - timedelta(days=45)]
