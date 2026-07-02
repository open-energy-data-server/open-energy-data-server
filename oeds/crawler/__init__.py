# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from oeds.base_crawler import BaseCrawler
from oeds.crawler.chargepoint import ChargepointDownloader
from oeds.crawler.e2watch import E2WatchCrawler
from oeds.crawler.ecmwf_crawler import EcmwfCrawler
from oeds.crawler.entsoe_crawler import EntsoeCrawler
from oeds.crawler.entsog import EntsogCrawler
from oeds.crawler.eon_grid_fees import EonGridFeeCrawler
from oeds.crawler.eview import EViewCrawler
from oeds.crawler.fernwaerme_preisuebersicht import FWCrawler
from oeds.crawler.frequency import FrequencyCrawler
from oeds.crawler.gie_crawler import GieCrawler
from oeds.crawler.instrat_pl import InstratPlCrawler
from oeds.crawler.jao_crawler import JaoCrawler
from oeds.crawler.jrc_idees import JrcIdeesCrawler
from oeds.crawler.ladesaeulenregister import LadesaeulenregisterCrawler
from oeds.crawler.londondatastore import LondonLoadData
from oeds.crawler.mastr import MastrDownloader
from oeds.crawler.netzentgelte import NetzentgeltCrawler
from oeds.crawler.netztransparenz import NetztransparenzCrawler
from oeds.crawler.ninja import NinjaCrawler
from oeds.crawler.nuts_mapper import NutsCrawler
from oeds.crawler.oep import OepCrawler
from oeds.crawler.opec import OpecDownloader
from oeds.crawler.opsd import OpsdCrawler
from oeds.crawler.smard import SmardCrawler
from oeds.crawler.synpro import SynproLoadProfileCrawler
from oeds.crawler.vea_industrial_load_profiles import IndustrialLoadProfileCrawler

crawlers: dict[str, type[BaseCrawler]] = {
    "public": NutsCrawler,
    "chargepoint": ChargepointDownloader,
    "e2watch": E2WatchCrawler,
    "ecmwf": EcmwfCrawler,
    "eon_grid_fees": EonGridFeeCrawler,
    "entsoe": EntsoeCrawler,
    "entsog": EntsogCrawler,
    "eview": EViewCrawler,
    "fernwaerme_preisuebersicht": FWCrawler,
    "frequency": FrequencyCrawler,
    "gie": GieCrawler,
    "instrat_pl": InstratPlCrawler,
    "jao": JaoCrawler,
    "jrc_idees": JrcIdeesCrawler,
    "ladesaeulenregister": LadesaeulenregisterCrawler,
    "londondatastore": LondonLoadData,
    "mastr": MastrDownloader,
    "netzentgelte": NetzentgeltCrawler,
    "netztransparenz": NetztransparenzCrawler,
    "ninja": NinjaCrawler,
    "oep": OepCrawler,
    "opec": OpecDownloader,
    "opsd": OpsdCrawler,
    "smard": SmardCrawler,
    "synpro": SynproLoadProfileCrawler,
    "vea_industrial_load_profiles": IndustrialLoadProfileCrawler,
    "weather": EcmwfCrawler,
}
