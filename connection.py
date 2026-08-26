"""
connection.py — DB read/write adapter for the B2C scheduler cloud deployment.

Phase 2 of the deployment plan (see approach/deployment.md). Two entry points:

  read_db(engine, plan_id)  -> (demand_df, run_cfg, sku_desc)
      Reads the 3 input tables (jkt_demand + jkt_plan_params, with
      jkt_plan_presets as fallback) and maps them to what the engine needs.

  write_db(engine, plan_id, result, building_xlsx, curing_xlsx, ...)
      Ingests the two workbooks the engine just wrote and populates the 4
      live output tables (jkt_plan_building / jkt_plan_curing /
      jkt_plan_Infeasibility / jkt_plan_kpis). The DB is populated from the
      SAME Excel the local path uses, so DB and local outputs are identical
      by construction. jkt_plan_capacityUtilisation is deferred (Phase 2 spec).

Design notes
------------
* Masters + running-moulds are read by the engine's own ETL, not here (on
  cloud the running-moulds table is fixed = jkplanningV1.Daily_Running_Moulds).
* curingChangeovers (KPI rule 4) comes from result["n_co"] (planned + dynamic),
  NOT from counting Excel rows — one dynamic CO can span two segment rows.
* jkt_plan_Infeasibility (rule 5) stores only UNMET + missing-master SKUs.
* Occupancy KPIs = busy time (prod + CO [+ mould-clean]) / available.
"""
from __future__ import annotations

import os
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
    _IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - zoneinfo always present on 3.9+
    _IST = None
import numpy as np
import pandas as pd
from sqlalchemy import text

from bc_config import make_engine, ENV
from bc_config import (RUNNING_MOULDS_TABLE, RUNNING_MOULDS_MONTH, PLAN_MONTH, PLAN_DATE,
                       SNAPSHOT_FALLBACK_MONTH)
from curing_consumption_dynamic import ConsumptionConfig  # config class (stays with the scheduling logic)
from building import Config, _ps_dedicated_skus  # building config + PS-dedication helper (building.py stays the leaf)


def now_ist() -> datetime:
    """Current time in IST as a naive datetime (for MySQL DATETIME columns)."""
    if _IST is not None:
        return datetime.now(_IST).replace(tzinfo=None)
    return datetime.now()

# Fixed cloud running-moulds table (documented; the engine ETL uses
# bc_config.RUNNING_MOULDS_TABLE — keep that = "Daily_Running_Moulds" on cloud).
RUNNING_MOULDS_CLOUD = "Daily_Running_Moulds"

# Building machine groups — used for the per-group occupancy KPIs.
# Classified by machine ID (stable) rather than the sheet's Machine_Group label,
# which is display text and has bitten us before (the label is "Stage-2", so a
# substring test for "STAGE2" silently never matched).
# NOTE on terminology: "US machines" = UNI_NARROW (7501-7503) only.
# "Unistage" colloquially means VMI + BJ + UNI_NARROW — not this group.
_GROUP_MACHINES = {
    "VMI":        {"6001", "6002", "6003", "6004", "7001", "7002", "7003", "7004"},
    "BJ":         {"7101", "7102", "7103", "7104", "7105", "7106", "7201"},
    "UNI_NARROW": {"7501", "7502", "7503"},
    "STAGE2":     {"8201", "8301", "8302", "8501", "8502", "7301"},
    "STAGE1":     {"6802", "6803", "6909", "6911", "7601", "7701",
                   "7801", "7802", "7803", "7804", "8001", "8002", "8003", "8101"},  # 6801 retired → 14
}

_OUTPUT_TABLES = [
    "jkt_plan_building",
    "jkt_plan_curing",
    "jkt_plan_Infeasibility",
    "jkt_plan_kpis",
    "jkt_plan_capacityUtilisation",
    "jkt_plan_moulds",
]


def get_engine():
    """Hardened SQLAlchemy engine (pool_pre_ping / recycle)."""
    return make_engine()

DB = ENV.get("JKT_DB_DATABASE", "jkplanningV1")  # DB name for the raw-SQL ETL helpers below


# ══════════════════════════════════════════════════════════════════════════
# INPUT TABLES (planning-pipeline reads) — everything the engine READS from the DB
#   1. per-run inputs: read_db → jkt_plan_params, jkt_demand, jkt_holiday_calendar
#   2. master + running-moulds ETL (below read_db): ConsumptionETL, building ETL/B2C_ETL,
#      and the curing opening-inventory / press-state / mould raw-SQL helpers
# ══════════════════════════════════════════════════════════════════════════
def _val(row: dict, preset: dict, key, default=None):
    """Prefer the run row; fall back to the preset; else default."""
    v = row.get(key)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        v = preset.get(key)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return default
    return v


def read_db(engine, plan_id: str):
    """Return (demand_df[SKUCode,Requirement(,Priority Flag,Delivery Date)], run_cfg, sku_desc).

    ALL run configuration is read from jkt_plan_params ONLY — the backend does NOT read the
    preset table. The frontend copies the selected preset's values (impPriorityFlag,
    mouldAvailability, noOfChangeOver, efficiency, dates, …) into the params row on plan
    create/edit, so jkt_plan_params is the single source of truth for a run.

    The committed-delivery feature (jkt_demand.priorityFlag + deliveryDate) is GATED by
    jkt_plan_params.impPriorityFlag:
      • impPriorityFlag = 1 → the two columns ARE read + staged, so the engine honours the
        delivery dates / priority flags.
      • impPriorityFlag = 0 → they are NOT read; the plan is the plain baseline even if the
        columns are populated in jkt_demand.
    """
    # ── plan params — the single source of truth (no preset read) ──
    prm = pd.read_sql(
        text("SELECT * FROM jkt_plan_params WHERE plan_id = :p"),
        engine, params={"p": plan_id},
    )
    if prm.empty:
        raise ValueError(f"jkt_plan_params has no row for plan_id={plan_id!r}")
    row = prm.iloc[0].to_dict()

    def _pv(key, default=None):
        v = row.get(key)
        return default if (v is None or (isinstance(v, float) and pd.isna(v))) else v
    def _truthy(v) -> bool:
        return (v is not None) and (str(v).strip().lower() in {"1", "1.0", "yes", "y", "true"})

    # ── config knobs — jkt_plan_params ONLY ──
    imp_priority       = _truthy(_pv("impPriorityFlag"))
    mould_availability = int(_pv("mouldAvailability", 0))
    max_co_per_day     = int(_pv("noOfChangeOver", 12))
    _eff_pct           = float(_pv("efficiency", 94.0))      # stored as a PERCENTAGE in the DB (94.0)
    press_efficiency   = _eff_pct / 100.0 if _eff_pct > 1.0 else _eff_pct   # engine wants a fraction

    # ── demand — read the committed-delivery columns ONLY when impPriorityFlag is ON ──
    _read_prio = imp_priority
    if _read_prio:
        try:
            dem = pd.read_sql(
                text("SELECT skuCode, requirement, skuDescription, priorityFlag, deliveryDate "
                     "FROM jkt_demand WHERE plan_id = :p"),
                engine, params={"p": plan_id},
            )
        except Exception:            # columns absent on this DB → degrade to base, feature inert
            _read_prio = False
    if not _read_prio:
        dem = pd.read_sql(
            text("SELECT skuCode, requirement, skuDescription "
                 "FROM jkt_demand WHERE plan_id = :p"),
            engine, params={"p": plan_id},
        )
    if dem.empty:
        raise ValueError(f"jkt_demand has no rows for plan_id={plan_id!r}")
    dem["skuCode"] = dem["skuCode"].astype(str).str.strip()
    if _read_prio:
        # Consolidate per SKU: requirement summed; committed when ANY row's priorityFlag
        # is set OR a deliveryDate is present (a date implies commitment); deadline =
        # EARLIEST deliveryDate. Staged under the exact header names the engine parser
        # reads ("Priority Flag" / "Delivery Date") — identical to a local Excel run.
        def _flag_any(s):
            for v in s:
                if v is not None and str(v).strip().lower() in {"1", "1.0", "yes", "y", "true"}:
                    return "1"
            return ""

        def _earliest_date(s):
            ds = [d for d in (pd.to_datetime(v, errors="coerce") for v in s) if pd.notna(d)]
            return min(ds) if ds else pd.NaT

        demand_df = (dem.groupby("skuCode", as_index=False)
                        .agg(Requirement=("requirement", "sum"),
                             **{"Priority Flag": ("priorityFlag", _flag_any),
                                "Delivery Date": ("deliveryDate", _earliest_date)})
                        .rename(columns={"skuCode": "SKUCode"}))
        _n_committed = int(((demand_df["Priority Flag"] == "1")
                            | demand_df["Delivery Date"].notna()).sum())
        print(f"[read_db] DELIVERY_PRIORITY ON (jkt_plan_params.impPriorityFlag=1): "
              f"{_n_committed} committed SKU(s) from jkt_demand")
    else:
        demand_df = (dem.groupby("skuCode", as_index=False)["requirement"].sum()
                        .rename(columns={"skuCode": "SKUCode",
                                         "requirement": "Requirement"}))
        print(f"[read_db] DELIVERY_PRIORITY OFF (jkt_plan_params.impPriorityFlag=0) — "
              f"priority flag / delivery date in jkt_demand are ignored")
    # Description lookup — SOLE source is the uploaded jkt_demand.skuDescription (the
    # jkt_sku_description master table is retired / not used). SKUs absent from jkt_demand
    # (e.g. Runner-Out SKUs on a press with zero demand) have no description → "NA"
    # downstream (see _desc). The frontend must upload skuDescription with the demand.
    sku_desc: dict = {}
    for k, v in zip(dem["skuCode"], dem["skuDescription"]):
        if v is not None and str(v).strip() and str(v).lower() != "nan":
            sku_desc[str(k).strip()] = v

    # ── plan dates: jkt_plan_params ONLY (never the preset) ──
    _sd, _ed = row.get("planStartDate"), row.get("planEndDate")
    if _sd is None or pd.isna(_sd) or _ed is None or pd.isna(_ed):
        raise ValueError(f"jkt_plan_params.planStartDate/planEndDate is NULL for plan_id={plan_id!r}")
    start = pd.to_datetime(_sd).replace(hour=7, minute=0, second=0, microsecond=0)
    end = pd.to_datetime(_ed)
    planning_days = int((end.normalize() - start.normalize()).days) + 1

    # ── plant holidays (jkt_holiday_calendar) — FIXED, month-keyed, NOT per plan_id ──────
    # Holidays are a fixed plant calendar with their OWN id (independent of plan_id). Rows are
    # selected by plan_month = the plan's START month ("YYYY-MM", char(15)); the plan_start
    # month drives the lookup. Full-day rows only (is_full_day = 1). Each row's
    # [start_date, end_date] span is expanded to individual YYYY-MM-DD dates; multiple rows are
    # unioned. Absent table / no rows → [] (holiday-free run, bit-for-bit no-holiday plan).
    _pm = start.strftime("%Y-%m")
    def _read_holidays():
        try:
            hdf = pd.read_sql(text(
                "SELECT start_date, end_date FROM jkt_holiday_calendar "
                "WHERE plan_month = :pm AND is_full_day = 1"), engine, params={"pm": _pm})
        except Exception as _he:
            print(f"[read_db] holiday-calendar read skipped ({type(_he).__name__}: {_he})")
            return []
        days: set = set()
        for _, _hr in hdf.iterrows():
            _s, _e = pd.to_datetime(_hr["start_date"]), pd.to_datetime(_hr["end_date"])
            if pd.isna(_s) or pd.isna(_e):
                continue
            for _d in pd.date_range(_s.normalize(), _e.normalize(), freq="D"):
                days.add(_d.strftime("%Y-%m-%d"))
        return sorted(days)
    _holidays = _read_holidays()
    if _holidays:
        print(f"[read_db] plant holidays for plan_month={_pm}: {_holidays}")

    run_cfg = {
        "plan_id":          plan_id,
        "plan_start":       start.to_pydatetime(),
        "planning_days":    planning_days,
        "holidays":         _holidays,
        "max_co_per_day":   max_co_per_day,     # PRESET-authoritative (resolved above)
        "press_efficiency": press_efficiency,   # PRESET-authoritative (resolved above)
        "plant_name":       _pv("plantName"),
        "product_name":     _pv("productName"),
        # v2 / dormant, carried for completeness
        "mould_availability": mould_availability,
    }
    return demand_df, run_cfg, sku_desc


# ── master + running-moulds ETL (curing) — the engine's Day-0 read layer ──
class ConsumptionETL:
    """Load data from DB needed to build the curing consumption table."""

    def __init__(self, engine):
        self.engine = engine
        self.db = ConsumptionConfig.DB_NAME

    def _sql(self, q: str) -> pd.DataFrame:
        return pd.read_sql(q, self.engine)

    # -- adapted from curing_lp.ETL.load_demand (lines 316-330) ---------------
    def load_demand(self, path: str) -> pd.DataFrame:
        """Load demand file. Returns [SKUCode, Quantity, Priority].

        Only SKUCode + a quantity column are required. Priority
        (ConsolidatedPriorityScore) is COMPUTED from the quantity via min-max
        normalisation (v1: requirement only) — any priority column already in
        the source is ignored, so Excel and DB inputs behave identically.

        Accepted quantity columns (first match): Quantity / Updated_Requirement /
        Requirement. SKU column: SKUCode / skuCode / sku_code / Sapcode.
        """
        if str(path).lower().endswith(".csv"):
            df = pd.read_csv(path)
        else:
            df = pd.read_excel(path)

        # Normalise column names (handle camelCase / lowercase variations)
        col_map = {
            "skuCode":  "SKUCode",
            "sku_code": "SKUCode",
            "sapcode":  "SKUCode",
            "Sapcode":  "SKUCode",
            "requirement":         "Requirement",
            "updated_requirement":  "Updated_Requirement",
        }
        df = df.rename(columns={c: col_map[c] for c in df.columns if c in col_map})

        if "SKUCode" not in df.columns:
            raise KeyError(
                f"Demand file {path!r} has no SKU column. "
                f"Columns found: {df.columns.tolist()}"
            )

        df["SKUCode"] = df["SKUCode"].astype(str).str.strip()

        # Resolve quantity column
        if "Quantity" in df.columns:
            qty_col = "Quantity"
        elif "Updated_Requirement" in df.columns:
            qty_col = "Updated_Requirement"
        elif "Requirement" in df.columns:
            qty_col = "Requirement"
        else:
            raise KeyError(
                f"Demand file {path!r} has no quantity column. "
                f"Columns found: {df.columns.tolist()}"
            )

        # Aggregate demand per SKU (a SKU may span several demand line-items).
        df = (df.groupby("SKUCode")
                .agg(Quantity=(qty_col, "sum"))
                .reset_index())
        if os.environ.get("DEMAND_INT_NORMALIZE", "1") != "0":
            # Demand is physically integer; strip xlsx float dust (e.g. 13750.000000000002)
            # so the curing-side Updated_Demand matches the DB-int path. Cloud reads int → no-op.
            df["Quantity"] = df["Quantity"].round()
        df = df[df["Quantity"] > 0].copy()

        # ConsolidatedPriorityScore (v1) — computed HERE, the single source, so
        # local Excel and cloud DB (`jkt_demand`, which has no priority column)
        # behave identically. v1 uses REQUIREMENT ONLY: min-max normalise the
        # per-SKU quantity over the whole demand.
        #   score = (q - q_min) / (q_max - q_min)   (1.0 for all if q_max == q_min)
        # Any priority column already present in the file is intentionally ignored.
        q = df["Quantity"].astype(float)
        q_min, q_max = q.min(), q.max()
        df["Priority"] = ((q - q_min) / (q_max - q_min)) if q_max > q_min else 1.0
        return df

    # -- adapted from curing_lp.ETL.load_cycle_times (lines 332-343) ----------
    def load_cycle_times(self) -> pd.DataFrame:
        """Load effective cycle times from DB. Returns [SKUCode, CycleTime_min]."""
        df = self._sql(
            f"SELECT Sapcode AS SKUCode, `Cure Time` AS Raw "
            f"FROM {self.db}.Master_Curing_Design_CycleTime"
        )
        df["CycleTime_min"] = np.round(
            (df["Raw"] + ConsumptionConfig.LOAD_UNLOAD_BUFFER_MIN)
            / ConsumptionConfig.PRESS_EFFICIENCY
        )
        df = df[["SKUCode", "CycleTime_min"]].drop_duplicates("SKUCode")
        df["SKUCode"] = df["SKUCode"].str.strip()
        return df

    # -- adapted from curing_lp.ETL.load_gt_inventory (lines 360-366) ---------
    def load_gt_inventory(self) -> pd.DataFrame:
        """Load opening GT inventory from DB. Returns [SKUCode, GT_Inventory]."""
        _resolve_snapshot(self.engine)
        return self._sql(
            f"SELECT sizeCode AS SKUCode, gtInventory AS GT_Inventory"
            f" FROM {self.db}.gt_inventory_manual WHERE date = '{PLAN_DATE}'"
        )

    # -- adapted from curing_lp.ETL.load_running_moulds (lines 368-400) -------
    def load_running_moulds(self) -> pd.DataFrame:
        """
        Load currently running moulds per curing press.
        Returns [Machine, SKUCode, MouldNos, MouldLife_remaining, Num_Moulds].
        Excludes presses where SKUCode is blank/NULL (in changeover or idle).
        """
        _resolve_snapshot(self.engine)
        wc_master = self._sql(f"SELECT * FROM {self.db}.Master_WC_Master")
        wc_master = wc_master[["wcID", "WCNAME"]]

        df = self._sql(f"SELECT * FROM {self.db}.{RUNNING_MOULDS_TABLE} WHERE date = '{PLAN_DATE}'")
        if "updatedAt" in df.columns:
            df = df.drop(columns=["updatedAt"])

        dff = df[["WCNAME", "Side", "Sapcode", "Current MouldNo", "Mould life"]].copy()
        dff["Mould life"] = 3000 - dff["Mould life"]
        dff["Mould life"] = np.where(dff["Mould life"] < 0, 0, dff["Mould life"])

        dff = dff.merge(wc_master, on=["WCNAME"], how="left")
        dff["WCNAME"] = dff["WCNAME"].str.replace(r"(LH|RH)$", "", regex=True).str.strip()
        dff["curing_machine"] = dff["WCNAME"] + dff["Side"]

        running = dff[["curing_machine", "Current MouldNo", "Sapcode", "Mould life"]].copy()
        running.columns = ["WCNAME", "Current MouldNo", "Sapcode", "Mould life"]
        running["WCNAME"] = running["WCNAME"].str.strip("LH|RH")
        running["No"] = 1

        # Exclude presses that are in CO or mould clean (sentinel SKU codes)
        _co_sentinels = {"CHANGEOVER", "MOULD_CLEAN", "MOULDCLEAN", "CO", "CLEAN", ""}
        running = running[
            running["Sapcode"].notna()
            & (~running["Sapcode"].str.strip().str.upper().isin(_co_sentinels))
        ]

        grouped = (
            running.groupby("WCNAME")
                .agg(
                    SKUCode=("Sapcode", "first"),
                    MouldNos=("Current MouldNo", list),
                    MouldLife_remaining=("Mould life", "min"),
                    Num_Moulds=("No", "count"),
                )
                .reset_index()
        )
        grouped.columns = ["Machine", "SKUCode", "MouldNos", "MouldLife_remaining", "Num_Moulds"]
        # Defensive determinism (see load_curing_allowable): groupby sorts keys by
        # default, but sort explicitly so this holds even if that default ever changes.
        return (grouped[["Machine", "SKUCode", "MouldNos", "MouldLife_remaining", "Num_Moulds"]]
                .sort_values("Machine").reset_index(drop=True))

    def load_mould_eligibility(self) -> dict:
        """Mould→SKU mapping from Master_Mapping_Mould_SKU.

        Returns {"sku_moulds": {SKUCode: set(mould_id)},
                 "mould_skus": {mould_id: set(SKUCode)}}.
        A press needs 2 eligible moulds to run an SKU; a mould can serve several
        SKUs (sharing).

        Table schema is SCHEMA-ADAPTIVE (the table has flipped naming across cycles):
          • Current/original DB: `Mould` (mould id) / `Matl.Code` (SKU) / `Active Flag`=1.
          • A prior cycle used `Mold_Name` / `Item_Code` (no active flag, all rows count).
        We introspect the live columns and build the right query, so a silent
        mould-blind run (gate OFF) can never happen again on a schema flip.
        """
        _cols = set(self._sql(
            f"SHOW COLUMNS FROM {self.db}.Master_Mapping_Mould_SKU")["Field"].astype(str))
        if {"Mould", "Matl.Code"} <= _cols:
            _where = " WHERE `Active Flag` = 1" if "Active Flag" in _cols else ""
            df = self._sql(
                f"SELECT `Mould` AS mould, `Matl.Code` AS sku "
                f"FROM {self.db}.Master_Mapping_Mould_SKU{_where}")
        elif {"Mold_Name", "Item_Code"} <= _cols:
            df = self._sql(
                f"SELECT `Mold_Name` AS mould, `Item_Code` AS sku "
                f"FROM {self.db}.Master_Mapping_Mould_SKU")
        else:
            raise RuntimeError(
                f"Master_Mapping_Mould_SKU: unrecognized columns {sorted(_cols)}")
        df["mould"] = df["mould"].astype(str).str.strip()
        df["sku"]   = df["sku"].astype(str).str.strip()
        sku_moulds: dict[str, set] = {}
        mould_skus: dict[str, set] = {}
        for m, s in zip(df["mould"], df["sku"]):
            if not m or not s or m.lower() == "nan" or s.lower() == "nan":
                continue
            sku_moulds.setdefault(s, set()).add(m)
            mould_skus.setdefault(m, set()).add(s)
        return {"sku_moulds": sku_moulds, "mould_skus": mould_skus}

    def load_curing_allowable(self) -> pd.DataFrame:
        """Load SKU → eligible curing presses. Returns [SKUCode, Machines (list of str)]."""
        df = self._sql(
            f"SELECT * FROM {self.db}.Master_Curing_Allowable_Machines_source"
        )
        df = df.rename(columns={"SKU Code": "SKUCode"})
        mcols = [c for c in df.columns if str(c).isdigit()]
        df["Machines"] = df.apply(
            lambda r: [str(c) for c in mcols if str(r[c]).strip().lower() == "yes"], axis=1
        )
        # Deterministic row order: SELECT * has no ORDER BY, so MySQL row order is not
        # guaranteed stable across connections/queries. Downstream CO-scheduling code
        # iterates these rows to build press->SKU candidate lists; unstable input order
        # here was a real source of run-to-run non-determinism in the CO schedule.
        return df[["SKUCode", "Machines"]].sort_values("SKUCode").reset_index(drop=True)

    def load_allowable_press_ids(self) -> set:
        """The plant curing-press roster = the numeric press-ID columns of the
        allowable matrix (Master_Curing_Allowable_Machines_source) — exactly 170
        presses. Used to restrict the running-moulds snapshot to real presses
        (the snapshot occasionally carries a few presses not in this roster)."""
        df = self._sql(
            f"SELECT * FROM {self.db}.Master_Curing_Allowable_Machines_source"
        )
        return {str(c) for c in df.columns if str(c).isdigit()}

    def load_building_allowable_skus(self) -> set:
        """SKUs that appear in building allowable master with at least one machine."""
        try:
            df = self._sql(
                f"SELECT SKUCode FROM {self.db}.Master_Building_Allowable_Machines"
            )
            return set(df["SKUCode"].astype(str).str.strip())
        except Exception as exc:
            print(f"  ⚠  Building allowable master unavailable: {exc}")
            return set()

    def load_building_history_skus(self) -> set:
        """SKUs appearing in Stage-1 or Stage-2 building history tables."""
        skus: set = set()
        for tbl in ("Building_Stage1_Best_Machines", "Building_Stage2_Best_Machines"):
            try:
                df = self._sql(
                    f"SELECT DISTINCT sizeCode AS SKUCode FROM {self.db}.{tbl}"
                )
                skus |= set(df["SKUCode"].astype(str).str.strip())
            except Exception as exc:
                print(f"  ⚠  {tbl} unavailable: {exc}")
        return skus

    def load_curing_allowable_skus(self) -> set:
        """SKUs that have at least one curing press in master data."""
        try:
            df = self._sql(
                f"SELECT `SKU Code` AS SKUCode "
                f"FROM {self.db}.Master_Curing_Allowable_Machines_source"
            )
            return set(df["SKUCode"].astype(str).str.strip())
        except Exception as exc:
            print(f"  ⚠  Curing allowable master unavailable: {exc}")
            return set()

    def load_curing_history_skus(self) -> set:
        """SKUs seen in RUNNING_MOULDS_TABLE (current/historical curing press state)."""
        _sentinels = {"CHANGEOVER", "MOULD_CLEAN", "MOULDCLEAN", "CO", "CLEAN", "NAN", ""}
        try:
            _resolve_snapshot(self.engine)
            df = self._sql(
                f"SELECT DISTINCT Sapcode AS SKUCode "
                f"FROM {self.db}.{RUNNING_MOULDS_TABLE} "
                f"WHERE Sapcode IS NOT NULL AND Sapcode != '' "
                f"AND date = '{PLAN_DATE}'"
            )
            raw = set(df["SKUCode"].astype(str).str.strip())
            return {s for s in raw if s.upper() not in _sentinels}
        except Exception as exc:
            print(f"  ⚠  Curing history ({RUNNING_MOULDS_TABLE}) unavailable: {exc}")
            return set()


# ── building ETL (base) + B2C subclass — the engine's building read layer ──
class ETL:
    S1_NAME_MAP = {
        "midland4stage1":"7804","midland2stage1":"7802","bj2stage1":"6802",
        "bj8stage1":"7201","sai3stage1":"8003","bj7stage1":"7104",
        "bj9stage1":"7105","bj3stage1":"6803","bj4stage1":"7101",
        "bj5stage1":"7102","88d1stage1":"8101","ltmstage1":"7601",
        "midland5stage1":"7701","midland3stage1":"7803","bj10stage1":"7106",
        "sai1stage1":"8001","sai2stage1":"8002","midland1stage1":"7801",
        "nrm11stage1":"6911","nrm9stage1":"6909",
        "bj6stage1":"7103",
    }
    S2_NAME_MAP = {
        "bj8":"7201","bj7":"7104","bj9":"7105","vmi1":"8501","vmi2":"8502",
        "bj4":"7101","bj5":"7102","newirm":"7301","bj6":"7103","oldirm":"8201",
        "vmi2Maxx":"7002","gtic1":"8301","vmi3Maxx":"7003","gtic2":"8302",
        "us1":"7501","us2":"7502","bj10":"7106","us3":"7503",
        "vmi4Maxx":"7004","vmi1Maxx":"7001",
        "VMIExxium01":"6001","VMIExxium02":"6002",
        "VMIExxium03":"6003","VMIExxium04":"6004",
    }

    def __init__(self, engine=None):
        self.engine = engine

    def _sql(self, q):
        return pd.read_sql(q, self.engine)

    def load_curing_schedule(self):
        """Read the curing plan building schedules against.

        Source = Config.CURING_PLAN_FILE (set by cbc.py to the Phase-C feed-aware
        bridge). Handles both shapes:
          • a flat file (csv/xlsx) with SKUCode/StartTime/EndTime/Qty columns, and
          • a curing-scheduler workbook with a 'Shift Schedule' sheet (title rows
            above the header, e.g. the provided jkt_plan.xlsx).
        Falls back to the legacy jkt_plan.csv path if nothing is configured.
        """
        src = getattr(Config, "CURING_PLAN_FILE", None) or \
            r'/Users/ajaygour/Downloads/jkt_plan.csv'

        def _has_cols(d):
            cols = {str(c).strip().lower() for c in d.columns}
            return {"skucode", "starttime", "endtime"}.issubset(cols)

        df = None
        if str(src).lower().endswith((".xlsx", ".xls", ".xlsm")):
            xl = pd.ExcelFile(src)
            # Prefer a 'Shift Schedule' sheet; else the first sheet that parses.
            sheets = (["Shift Schedule"] if "Shift Schedule" in xl.sheet_names
                      else []) + list(xl.sheet_names)
            for sh in sheets:
                for hdr in (0, 1, 2, 3):
                    try:
                        cand = pd.read_excel(src, sheet_name=sh, header=hdr)
                    except Exception:
                        continue
                    if _has_cols(cand):
                        df = cand
                        break
                if df is not None:
                    break
            if df is None:
                raise ValueError(
                    f"No sheet with SKUCode/StartTime/EndTime found in {src}")
        else:
            df = pd.read_csv(src)

        # Normalise lowercase/variant column names to the canonical ones.
        canon = {"skucode": "SKUCode", "starttime": "StartTime",
                 "endtime": "EndTime", "qty": "Qty"}
        df = df.rename(columns={c: canon[str(c).strip().lower()]
                                for c in df.columns
                                if str(c).strip().lower() in canon})
        df["StartTime"] = pd.to_datetime(df["StartTime"])
        df["EndTime"]   = pd.to_datetime(df["EndTime"])
        df["SKUCode"]   = df["SKUCode"].astype(str)
        df["Qty"]       = pd.to_numeric(df.get("Qty", 0), errors="coerce").fillna(0)
        # Drop changeover/cleaning placeholder rows and zero-demand rows.
        df = df[(df["SKUCode"] != "CHANGEOVER") & (df["Qty"] > 0)].copy()
        print(f"  [Curing plan] {len(df)} rows from {os.path.basename(str(src))}")
        return df

    def load_gt_inventory(self):
        # df = pd.read_csv(r"/Users/ajaygour/Downloads/BTP_3April_LP.csv")
        # df["StartTime"] = pd.to_datetime(df["StartTime"])
        # df["EndTime"]   = pd.to_datetime(df["EndTime"])
        # return df
        _resolve_snapshot(self.engine)
        return self._sql(
            f"SELECT sizeCode AS SKUCode, gtInventory AS GT_Inventory "
            f"FROM {Config.DB_NAME}.gt_inventory_manual WHERE date = '{PLAN_DATE}'"
        )

    def load_carcass_inventory(self):
        try:
            _resolve_snapshot(self.engine)
            return self._sql(
                f"SELECT sizeCode AS SKUCode, CarcassInv AS Carcass_Inventory "
                f"FROM {Config.DB_NAME}.carcass_inventory_manual WHERE date = '{PLAN_DATE}'"
            )
        except Exception:
            return pd.DataFrame(columns=["SKUCode","Carcass_Inventory"])

    def load_machine_allowable(self):
        df = self._sql(
            f"SELECT * FROM {Config.DB_NAME}.Master_Building_Allowable_Machines"
        )
        def _parse(s):
            if pd.isna(s) or not str(s).strip(): return []
            # keep numeric IDs (normalised) AND alphanumeric machine codes like ps3/ps4
            out = []
            for p in str(s).split(','):
                p = p.strip()
                if not p:
                    continue
                out.append(str(int(p)) if p.isdigit() else p)
            return out
        df = df.rename(columns={"Machines": "_machines_raw"})
        df["Machines"] = df["_machines_raw"].apply(_parse)
        # ── ps3/ps4 MASTER ON/OFF (bc_config.PS_MACHINES_ENABLED, default OFF) ──────────────
        # OFF strips ps3/ps4 from every allowable list -> the plant's ORIGINAL line without the
        # new machines (measures max production without them). Env PS_MACHINES=1 forces ON.
        try:
            from bc_config import PS_MACHINES_ENABLED as _PS_ON
        except Exception:
            _PS_ON = False
        if os.environ.get("PS_MACHINES") is not None:
            _PS_ON = (os.environ.get("PS_MACHINES") != "0")
        if not _PS_ON:
            df["Machines"] = df["Machines"].apply(lambda ms: [m for m in ms if m not in ("ps3", "ps4")])
        # ps3/ps4 SKU-EXCLUSIVITY (env PS_EXCLUSIVE, default ON): any SKU allowable on a NEW
        # ps machine is built ONLY on the ps machine(s) — the plant dedicated ps3/ps4 to these
        # SKUs, which also frees the shared VMI pool for other inches. Removes all non-ps
        # machines from those SKUs' allowable list.
        import os as _os
        # Default OFF: measured -33k (dedicating ps3/ps4's VMI-type SKUs relieves VMI, not the
        # BJ bottleneck). ps3/ps4 add the most value SHARED in the pool. Set PS_EXCLUSIVE=1 to
        # re-enable dedication (dynamic set via bc_config.PS_DEDICATION / _ps_dedicated_skus).
        if _os.environ.get("PS_EXCLUSIVE", "0") == "1":
            _PS = {"ps3", "ps4"}
            _env = _os.environ.get("PS_EXCL_SKUS", "").strip()
            if _env:                                     # fixed override (A/B / pinning a set)
                _sel = {s.strip() for s in _env.split(",") if s.strip()}
            else:                                        # DYNAMIC: choose from THIS month's demand
                _sel = _ps_dedicated_skus(df)
            def _ps_excl(sku, ms):
                has_ps = [m for m in ms if m in _PS]
                if not has_ps:
                    return ms
                return has_ps if sku in _sel else [m for m in ms if m not in _PS]
            df["Machines"] = [_ps_excl(str(s).strip(), ms)
                              for s, ms in zip(df["SKUCode"], df["Machines"])]
        return df[["SKUCode", "Machines"]]

    def load_changeover_map(self):
        df = self._sql(f"SELECT * FROM {Config.DB_NAME}.Master_Building_ChangeoverTime")
        co_map = {}
        for _, r in df.iterrows():
            co_map[str(r["MachineCode"])] = {
                "same": float(r["Same Size(Minutes)"]) + 10,
                "diff": float(r["Different Size(Minutes)"]) + 10,
            }
        return co_map

    def load_sku_sizes(self):
        df = self._sql(
            f"SELECT SKUCode, Size "
            f"FROM {Config.DB_NAME}.Master_Curing_Allowable_Machines"
        )
        return dict(zip(df["SKUCode"].astype(str), df["Size"].astype(str)))

    def load_history_map(self):
        """3-month run counts → {(machine, sku): count}.

        Source: DB tables Building_Stage1_Best_Machines + Building_Stage2_Best_Machines
        (cols: sizeCode, MachineName, count, MachineNo). Falls back to the legacy
        master_building_stage1/2_best_machine.csv files if the DB is unavailable.
        """
        frames = []
        if self.engine is not None:
            for table in ("Building_Stage1_Best_Machines",
                          "Building_Stage2_Best_Machines"):
                try:
                    frames.append(self._sql(
                        f"SELECT MachineNo, sizeCode, count "
                        f"FROM {Config.DB_NAME}.{table}"))
                except Exception as e:  # noqa: BLE001
                    print(f"  ⚠️  history table {table}: {e}")
        if not frames:
            here = os.path.dirname(os.path.abspath(__file__))
            for fp in (os.path.join(here, "master_building_stage1_best_machine.csv"),
                       os.path.join(here, "master_building_stage2_best_machine.csv")):
                if os.path.exists(fp):
                    try:
                        frames.append(pd.read_csv(fp))
                    except Exception as e:  # noqa: BLE001
                        print(f"  ⚠️  {fp}: {e}")
                else:
                    print(f"  ⚠️  history source missing (DB + file): {fp}")

        hist = {}
        for df in frames:
            for _, r in df.iterrows():
                try:
                    m = str(int(r["MachineNo"]))
                    s = str(r["sizeCode"])
                    c = float(r["count"])
                except (ValueError, TypeError, KeyError):
                    continue
                if not m or not s or c <= 0:
                    continue
                hist[(m, s)] = hist.get((m, s), 0.0) + c
        print(f"  [History] Loaded {len(hist)} (machine, SKU) pairs.")
        return hist

    def load_running_machines(self):
        rows = []
        for table, name_map in [
            ("TBMStage1_ProductionEventData", self.S1_NAME_MAP),
            ("TBMStage2_ProductionEventData", self.S2_NAME_MAP),
        ]:
            try:
                df = self._sql(
                    f"SELECT WorkCenter, RecipeCode "
                    f"FROM {Config.DB_NAME}.{table} "
                    f"ORDER BY DtAndTime DESC"
                )
                df = df.drop_duplicates(subset=["WorkCenter"])
                for _, r in df.iterrows():
                    mid = name_map.get(str(r["WorkCenter"]))
                    if mid:
                        rows.append({"Machine":str(mid),"SKUCode":str(r["RecipeCode"])})
            except Exception as e:
                print(f"  ⚠️  {table}: {e}")
        return (
            pd.DataFrame(rows) if rows
            else pd.DataFrame(columns=["Machine","SKUCode"])
        )


class B2C_ETL(ETL):
    """B2C variant of the building ETL — replaces load_curing_schedule()."""

    def load_consumption_table(self, path: str) -> pd.DataFrame:
        """
        Load the curing consumption table produced by curing_consumption_dynamic.py.
        Returns DataFrame: [SKUCode, Category, Running_Press_Count,
                            Effective_CT_Min, Qty_Per_Press_Per_Shift,
                            Total_GT_Per_Shift_Day0, Demand_Qty, Priority_Score]
        """
        xl = pd.ExcelFile(path)
        sheet = "Consumption Summary" if "Consumption Summary" in xl.sheet_names else xl.sheet_names[0]
        # Try multiple header rows in case a legend row is present
        for hdr in (0, 1, 2):
            try:
                df = pd.read_excel(path, sheet_name=sheet, header=hdr)
                if "SKUCode" in df.columns and "Category" in df.columns:
                    break
            except Exception:
                continue
        df["SKUCode"] = df["SKUCode"].astype(str).str.strip()
        # Drop legend/summary rows that the Excel reader picks up as data
        df = df[df["SKUCode"].notna() & (df["SKUCode"] != "") & (df["SKUCode"] != "nan")]
        _valid_cats = {"Runner-In", "Non-Runner-In"}
        if "Category" in df.columns:
            df = df[df["Category"].isin(_valid_cats)]
        df = df.reset_index(drop=True)
        print(f"  [B2C ETL] Consumption table: {len(df)} rows from {os.path.basename(path)}")
        return df

    def load_gt_inventory_for_b2c(self) -> pd.DataFrame:
        """Load REAL opening GT inventory (not zeroed out as in CBC cold-start)."""
        _resolve_snapshot(self.engine)
        return self._sql(
            f"SELECT sizeCode AS SKUCode, gtInventory AS GT_Inventory "
            f"FROM {Config.DB_NAME}.gt_inventory_manual WHERE date = '{PLAN_DATE}'"
        )


# ── curing opening-inventory + press-state + mould ETL (raw-SQL helpers) ──
def _sql(engine, q: str) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(q, conn)


# ── opening-snapshot date resolver (start-of-month + fallback) ─────────────────────────
# Decides ONE effective snapshot date used by ALL opening-state queries (running-moulds +
# opening GT + opening carcass) so they seed from the SAME date and fall back TOGETHER.
#   • Start-of-month: the snapshot is always taken from f"{PLAN_MONTH}-01", regardless of the
#     actual plan-start day (the real plan dates PLAN_START/PLANNING_DAYS stay as entered; only
#     the snapshot source is start-of-month).
#   • Fallback: if Daily_Running_Moulds (RUNNING_MOULDS_TABLE) has NO rows for that "-01" date,
#     running-moulds + GT + carcass ALL fall back to SNAPSHOT_FALLBACK_MONTH's "-01" snapshot.
# It rebinds the module-global PLAN_DATE; every snapshot query below reads PLAN_DATE, so setting
# it here steers all of them at once. The decision is cached (keyed on the UNMUTATED requested
# PLAN_MONTH) so the DB COUNT runs once and re-entrant calls are free; a new run for a different
# month (main._set_plan_month resets PLAN_MONTH) re-resolves.
_SNAPSHOT_RESOLVED = None   # (requested_month, effective_date, effective_month) | None

def _resolve_snapshot(engine):
    """Resolve + cache the effective opening-snapshot date; rebind module-global PLAN_DATE.
    Returns the effective 'YYYY-MM-01' date string."""
    global _SNAPSHOT_RESOLVED, PLAN_DATE, RUNNING_MOULDS_MONTH
    requested_month = PLAN_MONTH
    if _SNAPSHOT_RESOLVED is not None and _SNAPSHOT_RESOLVED[0] == requested_month:
        return _SNAPSHOT_RESOLVED[1]
    req_date = f"{requested_month}-01"
    eff_month, eff_date = requested_month, req_date
    try:
        n = int(_sql(engine,
            f"SELECT COUNT(*) AS n FROM {DB}.{RUNNING_MOULDS_TABLE} "
            f"WHERE date = '{req_date}'")["n"].iloc[0])
    except Exception as exc:                                     # DB hiccup → don't fall back blindly
        print(f"[snapshot] resolver COUNT failed ({exc}); using requested {req_date}")
        n = 1
    if n == 0:
        eff_month = SNAPSHOT_FALLBACK_MONTH
        eff_date  = f"{SNAPSHOT_FALLBACK_MONTH}-01"
        print(f"[snapshot] plan_month {requested_month} has no running-moulds "
              f"→ FALLBACK to {SNAPSHOT_FALLBACK_MONTH} snapshot ({eff_date})")
    else:
        print(f"[snapshot] plan_month {requested_month} → snapshot {eff_date} "
              f"({n} running-moulds rows)")
    PLAN_DATE = eff_date                    # steers all 9 snapshot queries (they read this global)
    RUNNING_MOULDS_MONTH = eff_month        # effective plan_month (cosmetic; unused downstream)
    _SNAPSHOT_RESOLVED = (requested_month, eff_date, eff_month)
    return eff_date

def _load_cycle_times(engine) -> dict:
    try:
        df = _sql(engine,
            f"SELECT Sapcode AS sku, `Cure Time` AS raw_ct "
            f"FROM {DB}.Master_Curing_Design_CycleTime")
        df["sku"] = df["sku"].astype(str).str.strip()
        df["ct"]  = (pd.to_numeric(df["raw_ct"], errors="coerce") + 0) / 0.94
        return {r["sku"]: float(r["ct"]) for _, r in df.iterrows() if pd.notna(r["ct"])}
    except Exception as e:
        print(f"  ⚠  Cycle times: {e}")
        return {}


def _load_opening_gt(engine) -> dict:
    try:
        _resolve_snapshot(engine)
        df = _sql(engine,
            f"SELECT sizeCode AS sku, gtInventory AS qty "
            f"FROM {DB}.gt_inventory_manual WHERE date = '{PLAN_DATE}'")
        df["sku"] = df["sku"].astype(str).str.strip()
        df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0)
        return {r["sku"]: float(r["qty"]) for _, r in df.iterrows()}
    except Exception as e:
        print(f"  ⚠  Opening GT inventory: {e}")
        return {}


def _load_opening_carcass(engine) -> dict:
    """Opening Stage-1 CARCASS inventory per SKU (sizeCode → CarcassInv), plan_month-filtered —
    the exact analog of _load_opening_gt for carcass_inventory_manual. Consumed FIRST in the
    Stage-1 carcass schedule so the plant's on-hand carcass is not wasted."""
    try:
        _resolve_snapshot(engine)
        df = _sql(engine,
            f"SELECT sizeCode AS sku, CarcassInv AS qty "
            f"FROM {DB}.carcass_inventory_manual WHERE date = '{PLAN_DATE}'")
        df["sku"] = df["sku"].astype(str).str.strip()
        df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0)
        return {r["sku"]: float(r["qty"]) for _, r in df.iterrows() if float(r["qty"]) > 0}
    except Exception as e:
        print(f"  ⚠  Opening carcass inventory: {e}")
        return {}


def _load_press_state(engine) -> pd.DataFrame:
    """
    Returns DataFrame with columns: press, sku, mould_life
    press = WCNAME_clean (e.g. "75206") — same format as CO events from
    curing_consumption_dynamic.py. MUST NOT use wcID — that format never
    matches CO event press IDs and silently breaks all CO transitions.
    """
    try:
        _resolve_snapshot(engine)
        rm = _sql(engine, f"SELECT * FROM {DB}.{RUNNING_MOULDS_TABLE} WHERE date = '{PLAN_DATE}'")
        if "updatedAt" in rm.columns:
            rm = rm.drop(columns=["updatedAt"])

        rm["press"]      = rm["WCNAME"].str.replace(r"(LH|RH)$", "", regex=True).str.strip()
        rm["sku"]        = rm["Sapcode"].astype(str).str.strip()
        rm["mould_life"] = pd.to_numeric(
            rm["Mould life"] if "Mould life" in rm.columns else 6000,
            errors="coerce",
        ).fillna(6000)

        valid = rm[
            rm["press"].notna() & (rm["press"] != "") &
            rm["sku"].notna()   & (rm["sku"]   != "") & (rm["sku"] != "nan")
        ].copy()

        # One row per press (LH+RH both strip to same WCNAME_clean with same SKU)
        return valid[["press", "sku", "mould_life"]].drop_duplicates("press").reset_index(drop=True)
    except Exception as e:
        print(f"  ⚠  Press state: {e}")
        return pd.DataFrame(columns=["press", "sku", "mould_life"])


def _load_mould_tracker(engine) -> pd.DataFrame:
    try:
        _resolve_snapshot(engine)
        rm  = _sql(engine, f"SELECT * FROM {DB}.{RUNNING_MOULDS_TABLE} WHERE date = '{PLAN_DATE}'")
        # Schema-adaptive (the mapping table has flipped naming across cycles):
        # current/original `Mould`/`Matl.Code`/`Active Flag`=1, or prior `Mold_Name`/`Item_Code`.
        _mcols = set(_sql(engine, f"SHOW COLUMNS FROM {DB}.Master_Mapping_Mould_SKU")["Field"].astype(str))
        if {"Mould", "Matl.Code"} <= _mcols:
            _mwhere = " WHERE `Active Flag` = 1" if "Active Flag" in _mcols else ""
            mms = _sql(engine,
                f"SELECT `Mould` AS MouldNo, `Matl.Code` AS sku "
                f"FROM {DB}.Master_Mapping_Mould_SKU{_mwhere}")
        else:
            mms = _sql(engine,
                f"SELECT `Mold_Name` AS MouldNo, `Item_Code` AS sku "
                f"FROM {DB}.Master_Mapping_Mould_SKU")

        active = mms   # active-flag already applied above when present
        compat = (active.groupby("MouldNo")["sku"]
                        .apply(lambda x: ", ".join(x.astype(str).str.strip()))
                        .reset_index()
                        .rename(columns={"sku": "Compatible_SKUs"}))

        assigned: dict[str, str] = {}
        life_map: dict[str, int] = {}
        if "Current MouldNo" in rm.columns:
            for _, r in rm.iterrows():
                life = int(pd.to_numeric(r.get("Mould life", 6000), errors="coerce") or 6000)
                for mn in str(r.get("Current MouldNo", "")).split(","):
                    mn = mn.strip()
                    if mn:
                        assigned[mn] = str(r.get("WCNAME", "FREE")).strip()
                        life_map[mn] = life

        rows = []
        for _, row in compat.iterrows():
            mn = str(row["MouldNo"]).strip()
            rows.append({
                "MouldNo":          mn,
                "Compatible_SKUs":  row["Compatible_SKUs"],
                "Life_Remaining":   life_map.get(mn, 6000),
                "Assigned_Machine": assigned.get(mn, "FREE"),
            })
        return pd.DataFrame(rows)
    except Exception as e:
        print(f"  ⚠  Mould tracker: {e}")
        return pd.DataFrame(columns=["MouldNo", "Compatible_SKUs", "Life_Remaining", "Assigned_Machine"])


def _load_curing_allowable(engine) -> dict:
    """SKUCode -> list of eligible press IDs (strings)."""
    try:
        df = _sql(engine, f"SELECT * FROM {DB}.Master_Curing_Allowable_Machines_source")
        df.columns = [str(c).strip() for c in df.columns]
        # First column is SKUCode; remaining columns are press IDs (as column names or values)
        sku_col = df.columns[0]
        result: dict[str, list] = {}
        for _, row in df.iterrows():
            sku = str(row[sku_col]).strip()
            presses = []
            for c in df.columns[1:]:
                val = str(row[c]).strip()
                if val not in ("", "nan", "None", "0"):
                    try:
                        presses.append(str(int(float(val))))
                    except (ValueError, TypeError):
                        pass
            if presses:
                result[sku] = presses
        return result
    except Exception as e:
        print(f"  ⚠  Curing allowable: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════════════
# OUTPUT TABLES (cloud deployment) — everything the engine WRITES to the DB
#   write_db → the 6 jkt_plan_* output tables (_OUTPUT_TABLES); no other code writes.
# ══════════════════════════════════════════════════════════════════════════
def _desc(series: pd.Series, sku_desc: dict) -> pd.Series:
    """SKU description, with sentinel rows labelled instead of left NULL.

    Building CHANGEOVER (and curing MOULD_CLEAN) rows carry a sentinel in the
    SKUCode column rather than a real SKU, so there is no description to look
    up — label them with the sentinel itself so the column is never NULL.
    """
    _SENTINELS = {"CHANGEOVER", "MOULD_CLEAN"}

    def _one(s: str):
        d = sku_desc.get(s)
        if d is not None and str(d).strip():
            return d
        return s if s in _SENTINELS else None

    return series.astype(str).map(_one)


def _machine_name(series: pd.Series) -> pd.Series:
    """Building machine code → plant name (bc_config.BUILDING_MACHINE_NAMES); "NA"
    for anything not in the dict. Curing presses are not mapped (press-id column)."""
    try:
        from bc_config import BUILDING_MACHINE_NAMES as _names
    except Exception:
        _names = {}
    return series.astype(str).str.strip().map(lambda x: _names.get(x, "NA"))


def write_db(engine, plan_id: str, result: dict,
             building_xlsx: str, curing_xlsx: str,
             sku_desc: dict | None = None,
             plant_name=None, product_name=None,
             created_by: str = "scheduler",
             overwrite: bool = True) -> dict:
    """Populate the 4 output tables from the freshly written workbooks."""
    sku_desc = sku_desc or {}
    now = now_ist()

    # ── building schedule (header row 2) ──────────────────────────────────
    bs = pd.read_excel(building_xlsx, sheet_name="Shift Schedule", header=2)
    bld = pd.DataFrame({
        "plan_id":        plan_id,
        "Date":           pd.to_datetime(bs["Date"]).dt.date,
        "Shift":          bs["Shift"].astype(str),
        "Machine":        bs["Machine"].astype(str),
        "machineName":    _machine_name(bs["Machine"]),
        "SKUCode":        bs["SKUCode"].astype(str),
        "skuDescription": _desc(bs["SKUCode"], sku_desc),
        "StartTime":      pd.to_datetime(bs["StartTime"]),
        "EndTime":        pd.to_datetime(bs["EndTime"]),
        "Qty":            pd.to_numeric(bs["Qty"], errors="coerce").fillna(0).astype(int),
        "Machine_Group":  bs["Machine_Group"].astype(str),
        "CO_Type":        bs["CO_Type"].astype(str),
        "createdAt":      now,
        "createdBy":      created_by,
    })

    # ── curing schedule (header row 0) ────────────────────────────────────
    cs = pd.read_excel(curing_xlsx, sheet_name="Shift Schedule", header=0)
    cur = pd.DataFrame({
        "plan_id":        plan_id,
        "Date":           pd.to_datetime(cs["Date"]).dt.date,
        "Shift":          cs["Shift"].astype(str),
        "Machine":        cs["Machine"].astype(str),
        "SKUCode":        cs["SKUCode"].astype(str),
        "skuDescription": _desc(cs["SKUCode"], sku_desc),
        "StartTime":      pd.to_datetime(cs["StartTime"]),
        "EndTime":        pd.to_datetime(cs["EndTime"]),
        "Qty":            pd.to_numeric(cs["Qty"], errors="coerce").fillna(0).astype(int),
        "CycleTime_min":  pd.to_numeric(cs["CycleTime_min"], errors="coerce"),
        "GT_Inventory":   pd.to_numeric(cs["GT_Inventory"], errors="coerce").fillna(0).astype(int),
        "Remarks":        cs["Remarks"].fillna("").astype(str),
        "createdAt":      now,
        "createdBy":      created_by,
    })

    # ── infeasibility — rule 5: UNMET + missing-master only ───────────────
    df = pd.read_excel(building_xlsx, sheet_name="Demand Fulfillment (B2C)", header=0)
    # Keep only real SKU rows — the sheet appends a KPI-summary footer block
    # (blank Status/Category) that must NOT be treated as SKUs.
    df = df[df["Status"].notna()].copy()
    elig = pd.to_numeric(df["Eligible_Machines"], errors="coerce")
    status = df["Status"].astype(str).str.upper()
    planned = pd.to_numeric(df["Planned_Units"], errors="coerce").fillna(0)
    # missing-master = explicitly 0 eligible machines (NaN = not a real 0).
    missing_master = elig.notna() & (elig == 0)
    # Zero production is infeasible regardless of the sheet's label. The engine
    # calls a SKU UNMET only when (built + opening GT) == 0, so a SKU that built
    # NOTHING but happens to hold a few units of opening GT is labelled PARTIAL
    # and would silently escape this report (seen: demand 4,473, opening GT 62,
    # built 0, cured 0 -> "PARTIAL" at 1.4%).
    zero_production = planned <= 0
    mask = (status == "UNMET") | missing_master | zero_production
    sub = df[mask].copy()
    skip = sub["Skip_Reason"] if "Skip_Reason" in sub.columns else pd.Series([None] * len(sub))
    # Always give a reason: missing master data > the sheet's own reason >
    # "UNMET_CAPACITY" (has machines, but the horizon/throughput could not cover it).
    skip_reason = [
        "NO_ELIGIBLE_MACHINE" if m
        else (str(s) if pd.notna(s) and str(s).strip()
              else ("ZERO_PRODUCTION" if z else "UNMET_CAPACITY"))
        for m, z, s in zip(missing_master[mask].tolist(),
                           zero_production[mask].tolist(), skip.tolist())
    ]
    infeas = pd.DataFrame({
        "plan_id":        plan_id,
        "plantName":      plant_name,
        "productName":    product_name,
        "skuCode":        sub["SKUCode"].astype(str),
        "skuDescription": _desc(sub["SKUCode"], sku_desc),
        "priority":       pd.to_numeric(sub["Priority"], errors="coerce"),
        "demand":         pd.to_numeric(sub["Demand"], errors="coerce").fillna(0).astype(int),
        "plannedUnits":   pd.to_numeric(sub["Planned_Units"], errors="coerce").fillna(0).astype(int),
        "status":         sub["Status"].astype(str),
        "skipReason":     skip_reason,
        "createdAt":      now,
        "createdBy":      created_by,
    })

    # ── KPIs — rule 4 (total COs) + occupancy metrics ─────────────────────
    bu = pd.read_excel(building_xlsx, sheet_name="Machine Utilization", header=1)
    cu = pd.read_excel(curing_xlsx,   sheet_name="Machine Utilization", header=1)

    def _occ(frame, busy_cols):
        avail = pd.to_numeric(frame["Available_Mins"], errors="coerce").fillna(0).sum()
        busy = sum(pd.to_numeric(frame[c], errors="coerce").fillna(0).sum() for c in busy_cols)
        return round(100.0 * busy / avail, 2) if avail else 0.0

    # Per-group building occupancy = (production + CO) / available, by machine ID.
    _bld_busy = ["Prod_Mins", "CO_Mins"]     # building has no mould clean
    _mach = bu["Machine"].astype(str)

    def _group_occ(group: str) -> float:
        return _occ(bu[_mach.isin(_GROUP_MACHINES[group])], _bld_busy)

    # Building changeover EVENT counts from the shift schedule (carcass and
    # production rows are not changeovers). Cross-checks against COs_Done.
    _co_type = bs["CO_Type"].astype(str)
    n_co_same = int((_co_type == "same_size_CO").sum())
    n_co_diff = int((_co_type == "diff_size_CO").sum())

    demand_sku = int(len(df))
    plan_sku = int((pd.to_numeric(df["Planned_Units"], errors="coerce").fillna(0) > 0).sum())

    # Compute each utilisation ONCE and reuse for both output tables, so
    # jkt_plan_kpis and jkt_plan_capacityUtilisation can never disagree.
    u_curing = _occ(cu, ["Used_Mins", "CO_Mins", "Mould_Clean_Mins"])
    u_build  = _occ(bu, _bld_busy)          # ALL 39 machines (incl. Stage-1)
    # building_s2_capacityUtilisation = ALL GT-making machines (VMI + BJ +
    # UNI_NARROW + Stage-2 = 24), NOT Stage-2 alone. Stage-1 (carcass) is
    # excluded. Column name kept for API/DB compatibility (per user decision).
    _gt_machines = (_GROUP_MACHINES["VMI"] | _GROUP_MACHINES["BJ"]
                    | _GROUP_MACHINES["UNI_NARROW"] | _GROUP_MACHINES["STAGE2"])
    u_s2     = _occ(bu[_mach.isin(_gt_machines)], _bld_busy)   # all GT machines
    u_s1     = _group_occ("STAGE1")
    u_vmi    = _group_occ("VMI")
    u_bj     = _group_occ("BJ")
    u_uni    = _group_occ("UNI_NARROW")     # "US machines"

    # Keep the monthly curing KPI consistent with the daily rows: both use the
    # fixed CURING_PRESS_COUNT denominator (bc_config), and every day has the same
    # available press-minutes, so the monthly curing figure == mean of the daily
    # curing utils. Falls back to the Machine-Utilization occupancy if the engine
    # did not emit the daily series.
    _daily_series = result.get("daily_capacity_util") or []
    if _daily_series:
        u_curing = round(sum(float(r["capacityUtilisation"]) for r in _daily_series)
                         / len(_daily_series), 2)

    kpis = pd.DataFrame([{
        "plan_id":                          plan_id,
        "demandFulfillment":                round(float(result.get("demand_coverage", 0.0)), 2),
        "demandSKU":                        demand_sku,
        "planSKU":                          plan_sku,
        "capacityUtilisation":              u_curing,
        "building_capacityUtilisation":     u_build,
        "building_s2_capacityUtilisation":  u_s2,
        "stage1_capacityUtilisation":       u_s1,
        "vmi_capacityUtilisation":          u_vmi,
        "bj_capacityUtilisation":           u_bj,
        "uniNarrow_capacityUtilisation":    u_uni,
        "curingChangeovers":                int(result.get("n_co", 0)),  # rule 4: planned + dynamic
        "buildingChangeovers_sameSize":     n_co_same,
        "buildingChangeovers_diffSize":     n_co_diff,
        "buildingChangeovers":              n_co_same + n_co_diff,
        "createdAt":                        now,
        "createdBy":                        created_by,
    }])

    # ── capacity utilisation — DAILY curing only: 30-31 rows per plan (composite
    # PK plan_id+date). One value per day = average curing-press utilisation =
    # (production + mould-clean + CO) / (CURING_PRESS_COUNT × 1440) that day.
    # Building-group utils live monthly in jkt_plan_kpis (not repeated per day).
    # From the engine (result["daily_capacity_util"]) so the daily rows aggregate
    # to the monthly curing figure in jkt_plan_kpis. Falls back to the single
    # monthly row if the engine did not emit the daily series (back-compat).
    if _daily_series:
        cap = pd.DataFrame([{
            "plan_id":             plan_id,
            "date":                pd.to_datetime(row["date"]).date(),
            "capacityUtilisation": row.get("capacityUtilisation"),
            "createdAt":           now,
            "createdBy":           created_by,
        } for row in _daily_series])
    else:
        cap = pd.DataFrame([{
            "plan_id":             plan_id,
            "date":                pd.to_datetime(bs["Date"]).min().date(),
            "capacityUtilisation": u_curing,
            "createdAt":           now,
            "createdBy":           created_by,
        }])

    # ── mould-in-use — DAILY grid straight from the curing MouldInUse sheet:
    # PLANNING_DAYS × #demand-SKUs rows (one per day×SKU). mouldsInUse = the day's
    # MAX across its 3 shifts of the SKU's moulds mounted on running presses;
    # totalEligibleMoulds = the SKU's eligible-mould pool (constant, 0 if none).
    # Composite PK (plan_id, Date, SKUCode). Sheet header is on Excel row 2.
    ms = pd.read_excel(curing_xlsx, sheet_name="MouldInUse", header=1)
    moulds = pd.DataFrame({
        "plan_id":             plan_id,
        "Date":                pd.to_datetime(ms["Date"]).dt.date,
        "SKUCode":             ms["SKU Code"].astype(str),
        "skuDescription":      _desc(ms["SKU Code"], sku_desc),
        "mouldsInUse":         pd.to_numeric(ms["Mould in USE"], errors="coerce").fillna(0).astype(int),
        "totalEligibleMoulds": pd.to_numeric(ms["Total Eligible Moulds"], errors="coerce").fillna(0).astype(int),
        "createdAt":           now,
        "createdBy":           created_by,
    }) if len(ms) else pd.DataFrame()

    # ── write (unique plan_id per run; overwrite makes re-runs idempotent) ─
    if overwrite:
        with engine.begin() as c:
            for t in _OUTPUT_TABLES:
                c.execute(text(f"DELETE FROM {t} WHERE plan_id = :p"), {"p": plan_id})

    bld.to_sql("jkt_plan_building", engine, if_exists="append", index=False)
    cur.to_sql("jkt_plan_curing", engine, if_exists="append", index=False)
    if len(infeas):
        infeas.to_sql("jkt_plan_Infeasibility", engine, if_exists="append", index=False)
    kpis.to_sql("jkt_plan_kpis", engine, if_exists="append", index=False)
    cap.to_sql("jkt_plan_capacityUtilisation", engine, if_exists="append", index=False)
    if len(moulds):
        moulds.to_sql("jkt_plan_moulds", engine, if_exists="append", index=False)

    return {
        "jkt_plan_building":            len(bld),
        "jkt_plan_curing":              len(cur),
        "jkt_plan_Infeasibility":       len(infeas),
        "jkt_plan_kpis":                len(kpis),
        "jkt_plan_capacityUtilisation": len(cap),
        "jkt_plan_moulds":              len(moulds),
    }
