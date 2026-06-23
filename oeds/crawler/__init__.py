# SPDX-FileCopyrightText: OEDS Contributors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Registry of all crawlers.

The modules are imported lazily by name (issue #45): `oeds --crawler-list dwd`
must not import the heavy dependencies of the other crawlers, and the CLI
should stay fast. `tests/test_crawlers.py` imports every mapped module and
checks that the class exists, since the class names here are plain strings
that no linter can check.
"""

from importlib import import_module
from typing import Iterator

from oeds.base_crawler import BaseCrawler

_CRAWLER_MODULES: dict[str, tuple[str, str]] = {
    "public": ("oeds.crawler.nuts_mapper", "NutsCrawler"),
    "chargepoint": ("oeds.crawler.chargepoint", "ChargepointDownloader"),
    "dwd": ("oeds.crawler.dwd", "DWDCrawler"),
    "e2watch": ("oeds.crawler.e2watch", "E2WatchCrawler"),
    "ecmwf": ("oeds.crawler.ecmwf_crawler", "EcmwfCrawler"),
    "ecmwf_land": ("oeds.crawler.ecmwf_crawler", "EcmwfLandCrawler"),
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
    "iwugebaeudetypen": ("oeds.crawler.iwugebaeudetypen", "IwuCrawler"),
    "jao": ("oeds.crawler.jao_crawler", "JaoCrawler"),
    "jrc_idees": ("oeds.crawler.jrc_idees", "JrcIdeesCrawler"),
    "kelmarsh": ("oeds.crawler.kelmarsh", "KelmarshCrawler"),
    "ladesaeulenregister": (
        "oeds.crawler.ladesaeulenregister",
        "LadesaeulenregisterCrawler",
    ),
    "londondatastore": ("oeds.crawler.londondatastore", "LondonLoadData"),
    "mastr": ("oeds.crawler.mastr", "MastrDownloader"),
    "netztransparenz": ("oeds.crawler.netztransparenz", "NetztransparenzCrawler"),
    "ninja": ("oeds.crawler.ninja", "NinjaCrawler"),
    "nrw_kwp_waermedichte": ("oeds.crawler.nrw_kwp_waermedichte", "KwpCrawler"),
    "oep": ("oeds.crawler.oep", "OepCrawler"),
    "opec": ("oeds.crawler.opec", "OpecDownloader"),
    "opsd": ("oeds.crawler.opsd", "OpsdCrawler"),
    "refit": ("oeds.crawler.refit", "RefitCrawler"),
    "regelleistung": ("oeds.crawler.regelleistung", "RegelleistungCrawler"),
    "smard": ("oeds.crawler.smard", "SmardCrawler"),
    "synpro": ("oeds.crawler.synpro", "SynproLoadProfileCrawler"),
    "vea_industrial_load_profiles": (
        "oeds.crawler.vea_industrial_load_profiles",
        "IndustrialLoadProfileCrawler",
    ),
    "windmodel": ("oeds.crawler.windmodel", "WindTurbineCrawler"),
}


class _CrawlerRegistry:
    def __init__(self, modules: dict[str, tuple[str, str]]):
        self._modules = modules
        self._loaded: dict[str, type[BaseCrawler]] = {}

    def keys(self):
        return self._modules.keys()

    def values(self):
        return [self[name] for name in self._modules]

    def get(self, name: str, default=None):
        if name not in self._modules:
            return default
        return self[name]

    def __contains__(self, name: object) -> bool:
        return name in self._modules

    def __len__(self) -> int:
        return len(self._modules)

    def __iter__(self) -> Iterator[str]:
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
