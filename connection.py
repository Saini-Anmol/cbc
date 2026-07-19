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

# Stage-2 building machines — for the building_s2 occupancy KPI.
_STAGE2_MACHINES = {"8201", "8301", "8302", "8501", "8502", "7301"}

_OUTPUT_TABLES = [
    "jkt_plan_building",
    "jkt_plan_curing",
    "jkt_plan_Infeasibility",
    "jkt_plan_kpis",
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
    """Return (demand_df[SKUCode,Requirement], run_cfg dict, sku_desc dict)."""
    dem = pd.read_sql(
        text("SELECT skuCode, requirement, skuDescription "
             "FROM jkt_demand WHERE plan_id = :p"),
        engine, params={"p": plan_id},
    )
    if dem.empty:
        raise ValueError(f"jkt_demand has no rows for plan_id={plan_id!r}")
    dem["skuCode"] = dem["skuCode"].astype(str).str.strip()
    demand_df = (dem.groupby("skuCode", as_index=False)["requirement"].sum()
                    .rename(columns={"skuCode": "SKUCode",
                                     "requirement": "Requirement"}))
    sku_desc = dict(zip(dem["skuCode"], dem["skuDescription"]))

    prm = pd.read_sql(
        text("SELECT * FROM jkt_plan_params WHERE plan_id = :p"),
        engine, params={"p": plan_id},
    )
    if prm.empty:
        raise ValueError(f"jkt_plan_params has no row for plan_id={plan_id!r}")
    row = prm.iloc[0].to_dict()

    preset: dict = {}
    pname = row.get("optimisationPreset")
    if pname is not None and not (isinstance(pname, float) and pd.isna(pname)):
        pdf = pd.read_sql(
            text("SELECT * FROM jkt_plan_presets WHERE presetName = :n"),
            engine, params={"n": pname},
        )
        if not pdf.empty:
            preset = pdf.iloc[0].to_dict()

    start = pd.to_datetime(_val(row, preset, "planStartDate")).replace(
        hour=7, minute=0, second=0, microsecond=0)
    end = pd.to_datetime(_val(row, preset, "planEndDate"))
    planning_days = int((end.normalize() - start.normalize()).days) + 1

    # efficiency is stored as a PERCENTAGE in the DB (e.g. 94.0); the engine
    # wants a fraction (PRESS_EFFICIENCY = 0.94). Normalise > 1 → /100.
    eff = float(_val(row, preset, "efficiency", 0.94))
    if eff > 1.0:
        eff = eff / 100.0

    run_cfg = {
        "plan_id":          plan_id,
        "plan_start":       start.to_pydatetime(),
        "planning_days":    planning_days,
        "max_co_per_day":   int(_val(row, preset, "noOfChangeOver", 12)),
        "press_efficiency": eff,
        "plant_name":       _val(row, preset, "plantName"),
        "product_name":     _val(row, preset, "productName"),
        # v2 / dormant, carried for completeness
        "mould_availability": _val(row, preset, "mouldAvailability"),
    }
    return demand_df, run_cfg, sku_desc


# ══════════════════════════════════════════════════════════════════════════
# WRITE — engine's fresh workbooks → 4 output tables
# ══════════════════════════════════════════════════════════════════════════
def _desc(series: pd.Series, sku_desc: dict) -> pd.Series:
    return series.astype(str).map(lambda s: sku_desc.get(s))


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
    # missing-master = explicitly 0 eligible machines (NaN = not a real 0).
    missing_master = elig.notna() & (elig == 0)
    mask = (status == "UNMET") | missing_master
    sub = df[mask].copy()
    skip = sub["Skip_Reason"] if "Skip_Reason" in sub.columns else pd.Series([None] * len(sub))
    skip_reason = [
        "NO_ELIGIBLE_MACHINE" if m else (str(s) if pd.notna(s) and str(s).strip() else None)
        for m, s in zip(missing_master[mask].tolist(), skip.tolist())
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

    is_s2 = (bu["Machine"].astype(str).isin(_STAGE2_MACHINES)
             | bu["Machine_Group"].astype(str).str.contains("STAGE2", case=False, na=False))
    demand_sku = int(len(df))
    plan_sku = int((pd.to_numeric(df["Planned_Units"], errors="coerce").fillna(0) > 0).sum())

    kpis = pd.DataFrame([{
        "plan_id":                          plan_id,
        "demandFulfillment":                round(float(result.get("demand_coverage", 0.0)), 2),
        "demandSKU":                        demand_sku,
        "planSKU":                          plan_sku,
        "capacityUtilisation":              _occ(cu, ["Used_Mins", "CO_Mins", "Mould_Clean_Mins"]),
        "building_capacityUtilisation":     _occ(bu, ["Prod_Mins", "CO_Mins"]),
        "building_s2_capacityUtilisation":  _occ(bu[is_s2], ["Prod_Mins", "CO_Mins"]),
        "curingChangeovers":                int(result.get("n_co", 0)),   # rule 4: planned + dynamic
        "createdAt":                        now,
        "createdBy":                        created_by,
    }])

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

    return {
        "jkt_plan_building":      len(bld),
        "jkt_plan_curing":        len(cur),
        "jkt_plan_Infeasibility": len(infeas),
        "jkt_plan_kpis":          len(kpis),
    }
