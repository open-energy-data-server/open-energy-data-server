# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from importlib import import_module

from oeds.base_crawler import BaseCrawler

_CRAWLER_MODULES: dict[str, tuple[str, str]] = {
    "public": ("oeds.crawler.nuts_mapper", "NutsCrawler"),
    "chargepoint": ("oeds.crawler.chargepoint", "ChargepointDownloader"),
    "e2watch": ("oeds.crawler.e2watch", "E2WatchCrawler"),
    "ecmwf": ("oeds.crawler.ecmwf_crawler", "EcmwfCrawler"),
    "eon_grid_fees": ("oeds.crawler.eon_grid_fees", "EonGridFeeCrawler"),
    "entsoe": ("oeds.crawler.entsoe_crawler", "EntsoeCrawler"),
    "entsog": ("oeds.crawler.entsog", "EntsogCrawler"),
    "eview": ("oeds.crawler.eview", "EViewCrawler"),
    "fernwaerme_preisuebersicht": (
        "oeds.crawler.fernwaerme_preisuebersicht",
        "FWCrawler",
    ),
    "frequency": ("oeds.crawler.frequency", "FrequencyCrawler"),
    "gie": ("oeds.crawler.gie_crawler", "GieCrawler"),
    "instrat_pl": ("oeds.crawler.instrat_pl", "InstratPlCrawler"),
    "jao": ("oeds.crawler.jao_crawler", "JaoCrawler"),
    "jrc_idees": ("oeds.crawler.jrc_idees", "JrcIdeesCrawler"),
    "ladesaeulenregister": (
        "oeds.crawler.ladesaeulenregister",
        "LadesaeulenregisterCrawler",
    ),
    "londondatastore": ("oeds.crawler.londondatastore", "LondonLoadData"),
    "mastr": ("oeds.crawler.mastr", "MastrDownloader"),
    "netzentgelte": ("oeds.crawler.netzentgelte", "NetzentgeltCrawler"),
    "netztransparenz": ("oeds.crawler.netztransparenz", "NetztransparenzCrawler"),
    "ninja": ("oeds.crawler.ninja", "NinjaCrawler"),
    "oep": ("oeds.crawler.oep", "OepCrawler"),
    "opec": ("oeds.crawler.opec", "OpecDownloader"),
    "opsd": ("oeds.crawler.opsd", "OpsdCrawler"),
    "smard": ("oeds.crawler.smard", "SmardCrawler"),
    "synpro": ("oeds.crawler.synpro", "SynproLoadProfileCrawler"),
    "vea_industrial_load_profiles": (
        "oeds.crawler.vea_industrial_load_profiles",
        "IndustrialLoadProfileCrawler",
    ),
    "weather": ("oeds.crawler.ecmwf_crawler", "EcmwfCrawler"),
}


class _CrawlerRegistry:
    def __init__(self, modules: dict[str, tuple[str, str]]):
        self._modules = modules
        self._loaded: dict[str, type[BaseCrawler]] = {}

    def keys(self):
        return self._modules.keys()

    def __iter__(self):
        return iter(self._modules)

    def items(self):
        for name in self._modules:
            yield name, self[name]

    def __getitem__(self, name: str) -> type[BaseCrawler]:
        if name not in self._modules:
            raise KeyError(name)
        if name not in self._loaded:
            module_name, class_name = self._modules[name]
            module = import_module(module_name)
            self._loaded[name] = getattr(module, class_name)
        return self._loaded[name]


crawlers: _CrawlerRegistry = _CrawlerRegistry(_CRAWLER_MODULES)
