# ECMWF weather crawlers

Two CDS datasets are crawled by separate crawlers into separate tables of the schema
`ecmwf`, so that they never mix. All times are `timestamptz` (UTC), a row describes the
hour ending at `time`.

| Crawler | CDS dataset | Grid | Wind | Tables (grid / NUTS regions) |
|---|---|---|---|---|
| `ecmwf` | `reanalysis-era5-single-levels` | 0.25°, land and sea | 100 m | `era5` / `era5_eu` |
| `ecmwf_land` | `reanalysis-era5-land` | 0.1°, land only | 10 m | `era5_land` / `era5_land_eu` |

Both start in 2019-01-01 and stop six days before now (publication delay).

| CDS variable | DB column |
|---|---|
| `surface_solar_radiation_downwards` | `ghi` (J/m² of the past hour) |
| `100m_u/v_component_of_wind` (ERA5), `10m_u/v_component_of_wind` (ERA5-Land) | `wind_zonal`, `wind_meridional`, `wind_speed` |
| `2m_temperature` | `temp_air` |
| `total_precipitation` | `precipitation` (m of the past hour) |

ERA5 single levels delivers radiation and precipitation of the past hour. ERA5-Land
accumulates them since 00 UTC (the value at 00 UTC is the sum of the previous day), so
the crawler converts them to hourly values per grid cell.

CDS item limit: 5 variables × 24 h → chunks of **8 days**.

The older tables `ecmwf.ecmwf` and `ecmwf.ecmwf_eu` of earlier versions hold ERA5-Land
without this conversion of the precipitation and are not written any more.
