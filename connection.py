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

from datetime import datetime
try:
    from zoneinfo import ZoneInfo
    _IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - zoneinfo always present on 3.9+
    _IST = None
import pandas as pd
from sqlalchemy import text

from cbc_env import make_engine


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


# ══════════════════════════════════════════════════════════════════════════
# READ — 3 input tables → engine inputs
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


# ══════════════════════════════════════════════════════════════════════════
# WRITE — engine's fresh workbooks → 4 output tables
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
