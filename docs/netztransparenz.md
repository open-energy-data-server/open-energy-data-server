# Netztransparenz crawler

The crawler `netztransparenz` downloads the data of the German TSOs from the
[netztransparenz.de Web API](https://api-portal.netztransparenz.de/) into the schema
`netztransparenz`, one table per time series. The data is not under an open licence: crawl
it yourself and do not reshare it.

## Setup

Register in the API portal, create an API client and put its credentials into `config.yml`:

```yaml
ipnt_client_id: "cm_app_ntp_id_..."
ipnt_client_secret: "ntp_..."
```

Without credentials the crawler logs an error and skips. It uses the Python package
[netztransparenz](https://github.com/ma-lor/NetztransparenzClient). Versions up to 0.2.4 lack the
remuneration, per-minute NRV and mFRR endpoints and uses an old path for the online
extrapolation (HTTP 404). The crawler skips these tables with a warning until a newer version
is installed ([PR #5](https://github.com/ma-lor/NetztransparenzClient/pull/5)).

## Times and resume

- All times are `timestamptz` in UTC. `von` is the begin of the period and `bis` its end; for
  measures (`redispatch`, `kapazitaetsreserve`, `vorhaltung_krd`) they are `beginn` and `ende`.
- Every table is a hypertable on its time column.
- Each run continues every table from its latest time *with a value*. The API also returns rows
  without values for periods that are not published yet, so a fixed "latest row" would skip them.
  Rows of the fetched range are replaced, so a run can repeat a range without duplicates.
- Measures are fetched again for the last 7 days, because they can be published after their begin.
- `prognose_solar` and `prognose_wind` are downloaded completely on every run, because the
  forecasts change with every publication.
- A request before the first data of an endpoint returns HTTP 400 with the first data in the
  message ("Es können keine Daten vor dem 30.09.2025 22:00 abgerufen werden."). The crawler
  continues from that date and logs a warning.
- Requests are made per year (31 days for per-minute data) and stored per request. After a
  server error or timeout a request is tried up to 3 times. If it still fails, the range is
  requested in halves, down to one day: the API answers some long ranges with HTTP 500 that
  work in shorter requests (e.g. operational `NRVSaldo` for 2024-02-23 to 2025-02-22). A failing
  table does not stop the others, and the run exits with an error naming the failed tables.

To download a range again, for example after a source correction, call the crawler with begin
and end; it replaces the rows of that range in every table:

```python
from datetime import UTC, datetime
from oeds.base_crawler import load_config
from oeds.crawler.netztransparenz import NetztransparenzCrawler

crawler = NetztransparenzCrawler("netztransparenz", load_config("config.yml"))
crawler.crawl_temporal(datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 2, 1, tzinfo=UTC))
```

## Tables

Columns are the CSV columns of the API in lower case, without brackets, with `_` for spaces and
`-`, `_per_` for `/` and `ae`/`oe`/`ue`/`ss` for umlauts (`ID AEP in EUR/MWh` →
`id_aep_in_eur_per_mwh`). The TSO columns are `50hertz`, `amprion`, `tennet_tso` and
`transnetbw`, partly with a unit or direction suffix (`50hertz_mw`, `amprion_positiv`).

"Quality-assured" (`qualitaetsgesichert`) data is published about monthly, "operational"
(`betrieblich`) data in near real time. The column `datentyp` says which one a row is.

### Wind and solar

| Table | Endpoint | First data (UTC) | Resolution |
|---|---|---|---|
| `prognose_solar`, `prognose_wind` | `prognose/{Solar,Wind}` | 2011-03-31 22:00 | 15 min, day-ahead forecast in MW |
| `hochrechnung_solar`, `hochrechnung_wind` | `hochrechnung/{Solar,Wind}` | 2011-03-31 22:00 | 15 min, extrapolated feed-in in MW |
| `online_hochrechnung_solar`, `_windonshore`, `_windoffshore` | `onlinehochrechnung/{type}/qualitaetsgesichert` | 2011-12-31 23:00 (offshore 2012-12-13 23:00) | hourly, 15 min since 2025-09-30 22:00, MW |

### Marketing of the EEG energy by the TSOs

| Table | Endpoint | First data (UTC) |
|---|---|---|
| `vermarktung_inanspruchnahme_ausgleichsenergie` | `vermarktung/InanspruchnahmeAusgleichsenergie` (kWh) | 2011-03-31 22:00 |
| `vermarktung_untertaegige_strommengen` | `vermarktung/UntertaegigeStrommengen` (MW) | 2011-03-31 22:00 |
| `vermarktung_differenz_einspeiseprognose` | `vermarktung/DifferenzEinspeiseprognose` (MW) | 2011-03-31 22:00 |
| `vermarktung_exaa`, `vermarktung_epex` | `vermarktung/Vermarktung{Exaa,Epex}` (MW) | 2011-12-31 23:00 / 22:00 |
| `vermarktung_solar`, `vermarktung_wind`, `vermarktung_sonstige` | `vermarktung/Vermarktungs{Solar,Wind,Sonstige}` (MW) | 2013-12-31 23:00 |

All in 15 min resolution.

### Prices and remuneration

| Table | Endpoint | First data (UTC) | Notes |
|---|---|---|---|
| `spotmarktpreise` | `Spotmarktpreise` | 2020-12-31 23:00 | ct/kWh, hourly, 15 min since 2025-09-30 22:00 |
| `id_aep` | `IdAep` | 2020-06-30 22:00 | index of the imbalance price, EUR/MWh, 15 min |
| `marktpraemie` | `marktpraemie` | 2012-01 | monthly market values and premiums in ct/kWh; `von` is the begin of the month in German time; `negative_preise_*` as boolean |
| `negative_preise` | `NegativePreise` | 2015-12-31 23:00 | hourly; `stunde1` … `stunde6` as sent by the API (1, or NULL for "N.A.") for the rules of 1 to 6 hours |
| `negative_preise_15m` | `NegativePreise/15` | 2025-09-30 22:00 | 15 min; `negativ` as boolean |
| `anspruch_verguetung_eeg_<logic>` | `anspruchverguetungeeg/<logic>` | see below | `anspruchverguetung`: entitlement to remuneration under the EEG rule, boolean |
| `anspruch_verguetung_kwkg_<logic>` | `anspruchverguetungkwkg/<logic>` | see below | the same under the KWKG |

The remuneration tables follow the rule of the plant (`<logic>`), each with its own first data:
EEG `1hour` 2020-08-31 22:00, `2hour` 2025-12-31 23:00, `3hour` 2023-12-31 23:00, `4hour`
2020-12-31 23:00, `6hour` 2015-12-31 23:00, `15minutes` and `2cent` 2025-09-30 22:00; KWKG
`1hour` 2015-12-31 23:00, `15minutes` 2025-09-30 22:00. The hourly logics are hourly, the others
15 min. The API documents `NegativePreise` and `anspruchverguetung*` with the same sentence
("states if an entitlement to remuneration … is granted"); `anspruchverguetung*` is the newer one.

The yearly market premium (`Jahresmarktpraemie`, four values per year) is not crawled.

### Balancing (NrvSaldo)

Quality-assured, 15 min:

| Table | Datatype | First data (UTC) |
|---|---|---|
| `nrv_saldo` | `NRVSaldo`, net balance of Germany in MW | 2013-12-31 23:00 |
| `rz_saldo` | `RZSaldo`, balance of each control area in MW | 2014-04-30 22:00 |
| `aktivierte_srl` | `AktivierteSRL`, activated aFRR per TSO, positive/negative | 2014-04-30 22:00 |
| `aktivierte_mrl` | `AktivierteMRL`, activated mFRR per TSO, positive/negative | 2013-12-31 23:00 |
| `srl_optimierung`, `mrl_optimierung` | `SRLOptimierung`, `MRLOptimierung` | 2022-06-21 22:00 / 2014-05-31 22:00 |
| `prl` | `PRL`, FCR | 2022-06-21 22:00 |
| `difference` | `Difference` | 2022-06-21 22:00 |
| `zusatzmassnahmen`, `nothilfe` | `Zusatzmassnahmen`, `Nothilfe` (emergency assistance) | 2013-12-31 23:00 |
| `abschaltbare_lasten` | `AbschaltbareLasten`, interruptible loads | 2023-11-30 23:00 |
| `rebap` | `reBAP`, imbalance price `rebap_unterdeckt`/`rebap_ueberdeckt` in EUR/MWh | 2013-12-31 23:00 |
| `aep_module`, `finanzielle_wirkung_aep_module` | `AEPModule` (EUR/MWh) and their financial effect (EUR) | 2022-06-21 22:00 |
| `value_of_avoided_activation` | `VoAA`, EUR/MWh | 2023-11-01 00:00 |

Operational:

| Table | Datatype | First data (UTC) | Resolution |
|---|---|---|---|
| `nrv_saldo_betrieblich` | `NRVSaldo`, with the AEP scarcity component and MOL deviation flags | 2014-02-25 23:00 | 15 min |
| `nrv_saldo_minute` | `nrvSaldoMinute`, with a `status` | 2026-01-27 23:00 | 1 min |
| `mfrr_satisfied_demand` | `mFRRSatisfiedDemand`, Germany positive/negative in MW | 2026-01-13 23:00 | 15 min |
| `aep_schaetzer` | `AepSchaetzer`, estimate of the imbalance price in EUR/MWh, with a `status` | 2023-03-13 23:00 | 15 min |
| `srl_mol_abweichungen` | `SrlMolAbweichungen`, deviations from the aFRR merit order (events) | 2023-12-31 23:00 | per event |
| `mrl_mol_abweichungen` | `MrlMolAbweichungen`, reasons for deviations from the mFRR merit order (0/1) | 2023-12-31 23:00 | 15 min |
| `traffic_light` | `TrafficLight`, NRV traffic light in `ampel` (e.g. `GREEN`, `GREEN_NEG`, `BLUE`) | 2021-09-22 22:00 | 1 min |

The other combinations of datatype and product either have no data ("Für die Kombination …
gibt es keine Daten") or duplicate a quality-assured table.

### System services

| Table | Endpoint | First data (UTC) |
|---|---|---|
| `redispatch` | `redispatch`, measures with plant, direction, power and energy | 2020-12-31 23:00 |
| `kapazitaetsreserve` | `Kapazitaetsreserve`, use of the capacity reserve, same columns | 2017-06-02 04:00 |
| `vorhaltung_krd` | `VorhaltungkRD`, capacity held for curative redispatch, same columns | 2025-09-30 22:00 |
| `ausgewiesene_absm`, `zugeteilte_absm` | `AusgewieseneABSM`, `ZugeteilteABSM`: designated and allocated curtailment quantity in MW per area (`h1`, `h2`, `t1` … `t6`), 15 min | 2024-09-30 22:00 |
| `erzeugungsverbot` | `Erzeugungsverbot`: generation bans per area (0/1), 15 min | 2024-09-30 22:00 |

## Known API behaviour

- The complete download of `vermarktung/VermarktungEpex` (without a range) returns HTTP 500;
  requests with a range work. The crawler always requests ranges.
- `TrafficLight` sometimes returns HTTP 500 and works on the next attempt.
- The public OpenAPI document of the API is served by the portal as
  `https://api-portal.netztransparenz.de/api/PublicDocumentationAndHelp/public-open-api-documentation`.
