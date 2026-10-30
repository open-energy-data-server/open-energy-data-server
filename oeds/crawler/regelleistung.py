# SPDX-FileCopyrightText: Steffen Carstensen
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
https://regelleistung.net/
"""

import functools as ft
import logging
import warnings
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import inspect, text

from oeds.base_crawler import (
    DEFAULT_CONFIG_LOCATION,
    ContinuousCrawler,
    as_utc,
    ensure_utc,
    load_config,
)

log = logging.getLogger("regelleistung")
log.setLevel(logging.INFO)

# Regelleistungsmarkt
TABLE_DATA = {
    # FCR
    "fcr_bedarfe": "https://www.regelleistung.net/apps/cpp-publisher/api/v1/download/tenders/demands?date={date_str}&exportFormat=xlsx&market=CAPACITY&productTypes=FCR",
    "fcr_ergebnisse": "https://www.regelleistung.net/apps/cpp-publisher/api/v1/download/tenders/resultsoverview?date={date_str}&exportFormat=xlsx&market=CAPACITY&productTypes=FCR",
    "fcr_anonyme_ergebnisse": "https://www.regelleistung.net/apps/cpp-publisher/api/v1/download/tenders/anonymousresults?date={date_str}&exportFormat=xlsx&market=CAPACITY&productTypes=FCR",
    # aFRR Capacity
    "afrr_bedarfe_regelleistung": "https://www.regelleistung.net/apps/cpp-publisher/api/v1/download/tenders/demands?date={date_str}&exportFormat=xlsx&market=CAPACITY&productTypes=aFRR",
    "afrr_ergebnisse_regelleistung": "https://www.regelleistung.net/apps/cpp-publisher/api/v1/download/tenders/resultsoverview?date={date_str}&exportFormat=xlsx&market=CAPACITY&productTypes=aFRR",
    "afrr_anonyme_ergebnisse_regelleistung": "https://www.regelleistung.net/apps/cpp-publisher/api/v1/download/tenders/anonymousresults?date={date_str}&exportFormat=xlsx&market=CAPACITY&productTypes=aFRR",
    # mFRR Capacity
    "mfrr_bedarfe_regelleistung": "https://www.regelleistung.net/apps/cpp-publisher/api/v1/download/tenders/demands?date={date_str}&exportFormat=xlsx&market=CAPACITY&productTypes=mFRR",
    "mfrr_ergebnisse_regelleistung": "https://www.regelleistung.net/apps/cpp-publisher/api/v1/download/tenders/resultsoverview?date={date_str}&exportFormat=xlsx&market=CAPACITY&productTypes=mFRR",
    "mfrr_anonyme_ergebnisse_regelleistung": "https://www.regelleistung.net/apps/cpp-publisher/api/v1/download/tenders/anonymousresults?date={date_str}&exportFormat=xlsx&market=CAPACITY&productTypes=mFRR",
    # aFRR Energy
    "afrr_bedarfe_regelarbeit": "https://www.regelleistung.net/apps/cpp-publisher/api/v1/download/tenders/demands?date={date_str}&exportFormat=xlsx&market=ENERGY&productTypes=aFRR",
    "afrr_ergebnisse_regelarbeit": "https://www.regelleistung.net/apps/cpp-publisher/api/v1/download/tenders/resultsoverview?date={date_str}&exportFormat=xlsx&market=ENERGY&productTypes=aFRR",
    "afrr_anonyme_ergebnisse_regelarbeit": "https://www.regelleistung.net/apps/cpp-publisher/api/v1/download/tenders/anonymousresults?date={date_str}&exportFormat=xlsx&market=ENERGY&productTypes=aFRR",
    # mFRR Energy
    "mfrr_bedarfe_regelarbeit": "https://www.regelleistung.net/apps/cpp-publisher/api/v1/download/tenders/demands?date={date_str}&exportFormat=xlsx&market=ENERGY&productTypes=mFRR",
    "mfrr_ergebnisse_regelarbeit": "https://www.regelleistung.net/apps/cpp-publisher/api/v1/download/tenders/resultsoverview?date={date_str}&exportFormat=xlsx&market=ENERGY&productTypes=mFRR",
    "mfrr_anonyme_ergebnisse_regelarbeit": "https://www.regelleistung.net/apps/cpp-publisher/api/v1/download/tenders/anonymousresults?date={date_str}&exportFormat=xlsx&market=ENERGY&productTypes=mFRR",
    # ABLA
    "abla_bedarfe": "https://www.regelleistung.net/apps/cpp-publisher/api/v1/download/tenders/demands?date={date_str}&exportFormat=xlsx&market=CAPACITY&productTypes=ABLA",
    "abla_ergebnisse": "https://www.regelleistung.net/apps/cpp-publisher/api/v1/download/tenders/resultsoverview?date={date_str}&exportFormat=xlsx&market=CAPACITY&productTypes=ABLA",
    "abla_anonyme_ergebnisse": "https://www.regelleistung.net/apps/cpp-publisher/api/v1/download/tenders/anonymousresults?date={date_str}&exportFormat=xlsx&market=CAPACITY&productTypes=ABLA",
}


def database_friendly(string):
    return (
        string.lower()
        .replace("(eur/mw)/h", "eur_mwh")
        .replace("productname", "product")
        .replace("[", "")
        .replace("]", "")
        .replace("/", "_")
        .replace("(", "")
        .replace(")", "")
        .replace("-", "")
        .replace("+", "")
        .replace(" ", "_")
        # Table fcr_ergebnisse
        .replace("fr_demand_mw", "france_demand_mw")
        .replace("dk_demand_mw", "denmark_demand_mw")
        .replace("nl_demand_mw", "netherlands_demand_mw")
        .replace("at_demand_mw", "austria_demand_mw")
        .replace("be_demand_mw", "belgium_demand_mw")
        .replace("de_demand_mw", "germany_demand_mw")
        .replace("ch_demand_mw", "switzerland_demand_mw")
        .replace("si_demand_mw", "slovenia_demand_mw")
        .replace("at_import_export_mw", "austria_deficit_surplus_mw")
        .replace("fr_import_export_mw", "france_deficit_surplus_mw")
        .replace("dk_import_export_mw", "denmark_deficit_surplus_mw")
        .replace("ch_import_export_mw", "switzerland_deficit_surplus_mw")
        .replace("si_import_export_mw", "slovenia_deficit_surplus_mw")
        .replace("be_import_export_mw", "belgium_deficit_surplus_mw")
        .replace("de_import_export_mw", "germany_deficit_surplus_mw")
        .replace("nl_import_export_mw", "netherlands_deficit_surplus_mw")
        .replace(
            "at_settlementcapacity_price_eur_mw",
            "austria_settlementcapacity_price_eur_mw",
        )
        .replace(
            "ch_settlementcapacity_price_eur_mw",
            "switzerland_settlementcapacity_price_eur_mw",
        )
        .replace(
            "de_settlementcapacity_price_eur_mw",
            "germany_settlementcapacity_price_eur_mw",
        )
        .replace(
            "si_settlementcapacity_price_eur_mw",
            "slovenia_settlementcapacity_price_eur_mw",
        )
        .replace(
            "be_settlementcapacity_price_eur_mw",
            "belgium_settlementcapacity_price_eur_mw",
        )
        .replace(
            "dk_settlementcapacity_price_eur_mw",
            "denmark_settlementcapacity_price_eur_mw",
        )
        .replace(
            "nl_settlementcapacity_price_eur_mw",
            "netherlands_settlementcapacity_price_eur_mw",
        )
        .replace(
            "fr_settlementcapacity_price_eur_mw",
            "france_settlementcapacity_price_eur_mw",
        )
    )


def col_rename_fcr_demand(string):
    return (
        string.lower()
        # fcr_bedarfe (am 07.09.2022 wurde West Dänemark in den LFC Block aufgenommen) -> Spalten können zusammengefügt werden
        .replace("germany_block_demand_mw", "germany_country_demand_mw")
        .replace("germany_block_export_limit_mw", "germany_country_export_limit_mw")
        .replace("germany_block_core_portion_mw", "germany_country_core_portion_mw")
        .replace("denmark_block_demand_mw", "denmark_country_demand_mw")
        .replace("denmark_block_export_limit_mw", "denmark_country_export_limit_mw")
        .replace("denmark_block_core_portion_mw", "denmark_country_core_portion_mw")
    )


def prepare_demands_df(df):
    df.rename(mapper=lambda x: col_rename_fcr_demand(x), axis="columns", inplace=True)
    col_mapping = {}
    id_vars = []
    demand_cols = []
    export_cols = []
    nuclear_portion_cols = []
    for col_name in df.columns:
        if col_name.endswith("_demand_mw"):
            area_name = (
                col_name.rsplit("_", 2)[0]
                if col_name == "total_demand_mw"
                else col_name.rsplit("_", 3)[0]
            )
            col_mapping.update({col_name: area_name})
            demand_cols.append(col_name)
        elif col_name.endswith("_export_limit_mw"):
            area_name = (
                col_name.rsplit("_", 3)[0]
                if col_name == "total_export_limit_mw"
                else col_name.rsplit("_", 4)[0]
            )
            col_mapping.update({col_name: area_name})
            export_cols.append(col_name)
        elif col_name.endswith("_core_portion_mw"):
            area_name = (
                col_name.rsplit("_", 3)[0]
                if col_name == "total_core_portion_mw"
                else col_name.rsplit("_", 4)[0]
            )
            col_mapping.update({col_name: area_name})
            nuclear_portion_cols.append(col_name)
        else:
            id_vars.append(col_name)
    var_col_name = "area"
    df_melted_demand = df.melt(
        id_vars=id_vars,
        value_vars=demand_cols,
        var_name=var_col_name,
        value_name="demand_mw",
    )
    df_melted_demand[var_col_name] = df_melted_demand[var_col_name].replace(col_mapping)
    df_melted_export = df.melt(
        id_vars=id_vars,
        value_vars=export_cols,
        var_name=var_col_name,
        value_name="export_limit_mw",
    )
    df_melted_export[var_col_name] = df_melted_export[var_col_name].replace(col_mapping)
    df_melted_nuclear = df.melt(
        id_vars=id_vars,
        value_vars=nuclear_portion_cols,
        var_name=var_col_name,
        value_name="nuclear_portion_mw",
    )
    df_melted_nuclear[var_col_name] = df_melted_nuclear[var_col_name].replace(
        col_mapping
    )

    dfs = [df_melted_demand, df_melted_export, df_melted_nuclear]
    dfs = [df.set_index([*id_vars, var_col_name]) for df in dfs]
    df_final = ft.reduce(lambda left, right: left.join(right, how="outer"), dfs)

    df_final = df_final.dropna(
        subset=["demand_mw", "export_limit_mw", "nuclear_portion_mw"], how="all"
    )

    return df_final.reset_index()


def prepare_fcr_results_df(df):
    col_mapping = {}
    id_vars = []
    demand_cols = []
    settlementcapacity_cols = []
    deficit_surplus_cols = []
    for col_name in df.columns:
        if col_name.endswith("_demand_mw"):
            area_name = col_name.rsplit("_", 2)[0]
            col_mapping.update({col_name: area_name})
            demand_cols.append(col_name)
        elif col_name.endswith("_settlementcapacity_price_eur_mw"):
            area_name = col_name.rsplit("_", 4)[0]
            col_mapping.update({col_name: area_name})
            settlementcapacity_cols.append(col_name)
        elif col_name.endswith("_deficit_surplus_mw"):
            area_name = col_name.rsplit("_", 3)[0]
            col_mapping.update({col_name: area_name})
            deficit_surplus_cols.append(col_name)
        else:
            id_vars.append(col_name)
    df_melted_demand = df.melt(
        id_vars=id_vars,
        value_vars=demand_cols,
        var_name="area",
        value_name="demand_mw",
    )
    df_melted_demand["area"] = df_melted_demand["area"].replace(col_mapping)
    df_melted_settlementcapacity = df.melt(
        id_vars=id_vars,
        value_vars=settlementcapacity_cols,
        var_name="area",
        value_name="settlementcapacity_price_eur_mw",
    )
    df_melted_settlementcapacity["area"] = df_melted_settlementcapacity["area"].replace(
        col_mapping
    )
    df_melted_deficit_surplus = df.melt(
        id_vars=id_vars,
        value_vars=deficit_surplus_cols,
        var_name="area",
        value_name="deficit_surplus_mw",
    )
    df_melted_deficit_surplus["area"] = df_melted_deficit_surplus["area"].replace(
        col_mapping
    )

    dfs = [
        df_melted_demand,
        df_melted_settlementcapacity,
        df_melted_deficit_surplus,
    ]
    dfs = [df.set_index([*id_vars, "area"]) for df in dfs]
    df_final = ft.reduce(lambda left, right: left.join(right, how="outer"), dfs)

    df_final = df_final.dropna(
        subset=[
            "demand_mw",
            "settlementcapacity_price_eur_mw",
            "deficit_surplus_mw",
        ],
        how="all",
    )
    return df_final.reset_index()


def prepare_afrr_mfrr_results_df(df):
    # suffix of the wide column -> (long column name, number of suffix parts)
    suffixes = {
        "_min_capacity_price_eur_mwh": ("min_capacity_price_eur_mwh", 5),
        "_average_capacity_price_eur_mwh": ("average_capacity_price_eur_mwh", 5),
        "_marginal_capacity_price_eur_mwh": ("marginal_capacity_price_eur_mwh", 5),
        "_import_export_mw": ("import_export_mw", 3),
        "_sum_of_offered_capacity_mw": ("sum_of_offered_capacity_mw", 5),
        "_min_energy_price_eur_mwh": ("min_energy_price_eur_mwh", 5),
        "_average_energy_price_eur_mwh": ("average_energy_price_eur_mwh", 5),
        "_marginal_energy_price_eur_mwh": ("marginal_energy_price_eur_mwh", 5),
    }
    col_mapping = {}
    id_vars = []
    value_cols = {value_name: [] for value_name, _ in suffixes.values()}
    for col_name in df.columns:
        for suffix, (value_name, n_parts) in suffixes.items():
            if col_name.endswith(suffix):
                col_mapping[col_name] = col_name.rsplit("_", n_parts)[0]
                value_cols[value_name].append(col_name)
                break
        else:
            id_vars.append(col_name)

    dfs = []
    for value_name, cols in value_cols.items():
        # products differ in the published columns (e.g. ABLA has no energy prices)
        if not cols:
            continue
        melted = df.melt(
            id_vars=id_vars, value_vars=cols, var_name="area", value_name=value_name
        )
        melted["area"] = melted["area"].replace(col_mapping)
        dfs.append(melted.set_index([*id_vars, "area"]))
    if not dfs:
        return df
    df_final = ft.reduce(lambda left, right: left.join(right, how="outer"), dfs)

    df_final = df_final.dropna(subset=list(df_final.columns), how="all")
    return df_final.reset_index()


BERLIN = "Europe/Berlin"
# columns published as Europe/Berlin local time, stored as aware UTC
LOCAL_TIME_COLUMNS = [
    "date_from",
    "date_to",
    "gate_open_time",
    "gate_closure_time",
    "allocation_deadline",
]


def berlin_to_utc(series: pd.Series) -> pd.Series:
    """Convert naive Europe/Berlin wall-clock times to aware UTC."""
    local = series.dt.tz_localize(
        BERLIN,
        # ambiguous (DST end) -> first occurrence, nonexistent (DST start) -> shift
        ambiguous=np.ones(len(series), dtype=bool),
        nonexistent="shift_forward",
    )
    return local.dt.tz_convert("UTC")


def utc_to_berlin_date(value: datetime) -> date:
    """Delivery day (Europe/Berlin) of a stored UTC timestamp (aware or naive UTC)."""
    local = as_utc(value).tz_convert(BERLIN)
    return date(local.year, local.month, local.day)


def get_date_column_from_table_name(table_name):
    if "regelarbeit" in table_name:
        return "delivery_date"
    else:
        return "date_from"


def get_df_for_date(url, date_to_get: date, table_name: str):
    date_str = date_to_get.strftime("%Y-%m-%d")
    url_with_date = url.format(date_str=date_str)
    warnings.filterwarnings(
        action="ignore",
        category=UserWarning,
        message="Workbook contains no default style, apply openpyxl's default",
    )
    try:
        df = pd.read_excel(
            url_with_date, sheet_name="001", na_values=["-", "n.a.", "n.e."]
        )
    except ValueError as e:
        if "Worksheet named" in str(e):
            # no tender data published for this day
            return pd.DataFrame()
        raise Exception(f"Could not read Datasheet from URL: {url_with_date}") from e
    except Exception as e:
        raise Exception(f"Could not read Datasheet from URL: {url_with_date}") from e
    df.rename(mapper=lambda x: database_friendly(x), axis="columns", inplace=True)

    # adapt date_from and date_to column if from regelleistungsmarkt
    if get_date_column_from_table_name(table_name) == "date_from" and df.shape[0] > 0:
        product_split_array = (df["product"].str.split("_")).to_numpy()
        hours_from = np.array([product_list[1] for product_list in product_split_array])
        hours_to = np.array([product_list[2] for product_list in product_split_array])
        timedelta_from = np.array([timedelta(hours=int(hour)) for hour in hours_from])
        timedelta_to = np.array([timedelta(hours=int(hour)) for hour in hours_to])
        df["date_from"] = df["date_from"] + pd.to_timedelta(timedelta_from, "d")
        df["date_to"] = df["date_to"] + pd.to_timedelta(timedelta_to, "d")

    # adapt mw column to mwh column
    if get_date_column_from_table_name(table_name) == "date_from" and df.shape[0] > 0:
        hours_from = np.array([product_list[1] for product_list in product_split_array])
        hours_to = np.array([product_list[2] for product_list in product_split_array])
        hours_from_int = (hours_from).astype(np.int16)
        hours_to_int = (hours_to).astype(np.int16)
        hours_diff = hours_to_int - hours_from_int
        cols_to_adapt = [
            "total_min_capacity_price_eur_mw",
            "total_average_capacity_price_eur_mw",
            "total_marginal_capacity_price_eur_mw",
            "germany_min_capacity_price_eur_mw",
            "germany_average_capacity_price_eur_mw",
            "germany_marginal_capacity_price_eur_mw",
            "austria_min_capacity_price_eur_mw",
            "austria_average_capacity_price_eur_mw",
            "austria_marginal_capacity_price_eur_mw",
            "capacity_price_eur_mw",
        ]
        for col_to_adapt in cols_to_adapt:
            if col_to_adapt in df.columns:
                final_col_name = col_to_adapt + "h"
                df[final_col_name] = pd.to_numeric(df[col_to_adapt]) / hours_diff

        cols_to_drop = [*cols_to_adapt]
        for col_to_drop in cols_to_drop:
            if col_to_drop in df.columns:
                df = df.drop(col_to_drop, axis=1)

    if "bedarfe" in table_name and df.shape[0] > 0:
        df = prepare_demands_df(df)
    elif table_name == "fcr_ergebnisse" and df.shape[0] > 0:
        df = prepare_fcr_results_df(df)
    elif "ergebnis" in table_name and "anonyme" not in table_name and df.shape[0] > 0:
        df = prepare_afrr_mfrr_results_df(df)

    df = df.dropna(axis="columns", how="all")

    # unify country representation to NUTS standard
    if "area" in df.columns or "country" in df.columns:
        df.rename(columns={"country": "area"}, inplace=True)
        df["area"] = (
            df["area"]
            .replace(
                {
                    "germany": "DE",
                    "netherlands": "NL",
                    "belgium": "BE",
                    "austria": "AT",
                    "slovenia": "SI",
                    "czech_republic": "CZ",
                    "denmark": "DK",
                    "france": "FR",
                    "switzerland": "CH",
                }
            )
            .str.upper()
        )

    # the source publishes Europe/Berlin local times, store aware UTC
    for col in LOCAL_TIME_COLUMNS:
        if col in df.columns and pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = berlin_to_utc(df[col])
    # the delivery day is a calendar date
    if "delivery_date" in df.columns and pd.api.types.is_datetime64_any_dtype(
        df["delivery_date"]
    ):
        df["delivery_date"] = df["delivery_date"].dt.date

    return df


TEMPORAL_START = date(2020, 2, 1)


def sql_type(series: pd.Series) -> str:
    if pd.api.types.is_datetime64_any_dtype(series):
        return "TIMESTAMPTZ"
    if pd.api.types.is_bool_dtype(series):
        return "BOOLEAN"
    if pd.api.types.is_numeric_dtype(series):
        return "DOUBLE PRECISION"
    return "TEXT"


def to_delivery_date(date_col: str, value: datetime) -> date:
    if date_col == "delivery_date":
        return date(value.year, value.month, value.day)
    return utc_to_berlin_date(value)


class RegelleistungCrawler(ContinuousCrawler):
    TIMEDELTA = timedelta(days=2)

    def get_latest_data(self, table_name: str) -> date:
        date_col = get_date_column_from_table_name(table_name)
        query = text(f'SELECT max("{date_col}") as "{date_col}" FROM "{table_name}"')
        try:
            with self.engine.connect() as conn:
                latest_datetime = conn.execute(query).scalar()
                if not latest_datetime:
                    # empty table: the day before the start, as callers add one day
                    return TEMPORAL_START - timedelta(days=1)
                return to_delivery_date(date_col, latest_datetime)
        except Exception:
            log.error("No data found for %s", table_name)
            return TEMPORAL_START - timedelta(days=1)

    def get_first_data(self, table_name: str) -> date:
        date_col = get_date_column_from_table_name(table_name)
        query = text(f'SELECT min("{date_col}") as "{date_col}" FROM "{table_name}"')
        try:
            with self.engine.connect() as conn:
                earliest_datetime = conn.execute(query).scalar()
                if not earliest_datetime:
                    return TEMPORAL_START
                return to_delivery_date(date_col, earliest_datetime)
        except Exception:
            log.error("No data found for %s", table_name)
            return TEMPORAL_START

    def append_df(self, table_name: str, df: pd.DataFrame) -> None:
        """Append df, adding columns the source introduced since the table was created."""
        if df.empty or len(df.columns) == 0:
            return
        # integers would fix the column type to bigint, later values may be fractional
        for col in df.columns:
            if pd.api.types.is_integer_dtype(df[col]) and col != "area":
                df[col] = df[col].astype("float64")
        with self.engine.begin() as conn:
            inspector = inspect(conn)
            if inspector.has_table(table_name):
                existing = {c["name"] for c in inspector.get_columns(table_name)}
                for col in df.columns:
                    if col in existing:
                        continue
                    log.info("Adding new column %s to %s", col, table_name)
                    conn.execute(
                        text(
                            f'ALTER TABLE "{table_name}" ADD COLUMN "{col}" {sql_type(df[col])}'
                        )
                    )
            df = ensure_utc(df)
            df.to_sql(table_name, conn, if_exists="append", index=False)

    def write_past_entries(
        self,
        table_name,
        url,
        earliest_date,
        earliest_date_to_write=TEMPORAL_START,
    ):
        data_for_date_exists = True
        wrote_data = False

        while data_for_date_exists and (earliest_date_to_write < earliest_date):
            try:
                earliest_date -= timedelta(days=1)
                df = get_df_for_date(url, earliest_date, table_name)
                self.append_df(table_name, df)
                wrote_data = True
            except Exception as e:
                log.info(
                    f"The earliest date for {table_name} is the date {earliest_date}. {e}"
                )
                data_for_date_exists = False

        if wrote_data:
            log.info(
                f"Finished writing {table_name} to Database with earliest date {earliest_date}"
            )
        elif not wrote_data and data_for_date_exists:
            log.info(
                f"The defined date for the earliest entry was already reached in {table_name}. If you want to have more data, simply adjust the earliest date to write parameter."
            )
        else:
            log.info(f"No past data was written for {table_name}")

    def create_table_and_write_past_data(
        self, url, table_name, earliest_date_to_write=TEMPORAL_START
    ):
        log.info(f"Start creating table {table_name} and adding new data")
        earliest_date = date.today()
        self.write_past_entries(table_name, url, earliest_date, earliest_date_to_write)

    def add_additional_past_entries(
        self, table_name, url, earliest_date_to_write=TEMPORAL_START
    ):
        log.info(f"Start writing missing past entries in table {table_name} if any")
        earliest_date = self.get_first_data(table_name)
        self.write_past_entries(table_name, url, earliest_date, earliest_date_to_write)

    def write_new_data_from_latest_date_to_today(
        self, url, table_name, latest_data_date
    ):
        log.info(f"Start writing new data to {table_name}")

        today_date = date.today()

        if latest_data_date == (today_date - timedelta(days=1)):
            log.info(f"Table {table_name} has already the newest data.")
        else:
            latest_data_date = latest_data_date + timedelta(days=1)
            while latest_data_date < today_date:
                df = get_df_for_date(url, latest_data_date, table_name)
                self.append_df(table_name, df)
                latest_data_date += timedelta(days=1)

            log.info(
                f"Finished writing new data to {table_name} with newest date being yesterday {(latest_data_date - timedelta(days=1))}"
            )

    def write_data_in_table(
        self,
        table_name,
        url,
        earliest_date_to_write=TEMPORAL_START,
        write_additional_past_entries_if_any=True,
    ):
        latest_date = self.get_latest_data(table_name)
        if latest_date is not None:
            self.write_new_data_from_latest_date_to_today(url, table_name, latest_date)
            if (
                write_additional_past_entries_if_any
                and earliest_date_to_write < TEMPORAL_START
            ):
                self.add_additional_past_entries(
                    table_name, url, earliest_date_to_write
                )
        else:
            self.create_table_and_write_past_data(
                url, table_name, earliest_date_to_write
            )

    def crawl_temporal(self, begin: date | None = None, end: date | None = None):
        # TODO refactoring, begin and end is not respected
        for table_name, url in TABLE_DATA.items():
            self.write_data_in_table(table_name, url)

        self.create_hypertable_if_not_exists()

    def create_hypertable_if_not_exists(self) -> None:
        for table_name in TABLE_DATA.keys():
            date_col = get_date_column_from_table_name(table_name)
            self.create_single_hypertable_if_not_exists(table_name, date_col)


if __name__ == "__main__":
    logging.basicConfig()
    config = load_config(DEFAULT_CONFIG_LOCATION)
    crawler = RegelleistungCrawler("regelleistung", config)
    crawler.crawl_temporal()
