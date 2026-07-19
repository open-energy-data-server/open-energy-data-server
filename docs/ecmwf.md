# ECMWF weather crawler

CDS dataset: `reanalysis-era5-single-levels` (~0.25°).

| CDS variable | DB column |
|---|---|
| `surface_solar_radiation_downwards` | `ghi` (hourly deaccumulated J/m²) |
| `100m_u/v_component_of_wind` | `wind_zonal`, `wind_meridional`, `wind_speed` |
| `2m_temperature` | `temp_air` |
| `total_precipitation` | `precipitation` |

Tables: `ecmwf` (grid points), `ecmwf_eu` (NUTS-averaged).

CDS item limit: 5 variables × 24 h → chunks of **8 days**.
