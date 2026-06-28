"""
b2c_pipeline.py — End-to-end B2C scheduling pipeline.

Orchestrates two stages:
  1. Curing Consumption (dynamic) — generates 31-day CO schedule
  2. Building Scheduler (B2C)     — uses CO schedule to assign building machines

Usage:
    python b2c_pipeline.py                          # auto-detect demand file
    python b2c_pipeline.py data/input/demand_may.xlsx
"""

import os
import sys
import tempfile
from datetime import datetime

import pandas as pd

import cbc_env
from curing_consumption_dynamic import run_dynamic_consumption
from building_b2c import run_from_database_b2c

# ── Constants ──────────────────────────────────────────────────────────────────
PLAN_START    = datetime(2026, 5, 1, 7, 0, 0)
PLANNING_DAYS = 31
OUT_DIR       = cbc_env.OUTPUT_DIR
IN_DIR        = cbc_env.INPUT_DIR

CC_OUTPUT     = os.path.join(OUT_DIR, "curing_consumption_31day.xlsx")
BUILD_OUTPUT  = os.path.join(OUT_DIR, "main_output", "bc_building_schedule_2026-05-01.xlsx")
DEMAND_FILE   = os.path.join(IN_DIR, "demand_may.xlsx")


def run_pipeline(
    demand_path: str | None = None,
    cc_output: str | None = None,
    build_output: str | None = None,
    plan_start: datetime | None = None,
    planning_days: int | None = None,
) -> dict:
    """
    Run the full B2C pipeline: curing consumption → building schedule.

    Returns a dict with:
        co_events    : list of CO event dicts from the curing step
        n_co         : total changeovers scheduled
        cc_output    : path to curing consumption Excel
        build_output : path to building schedule Excel
        build_result : return value of run_from_database_b2c()
    """
    demand_path   = demand_path   or DEMAND_FILE
    cc_output     = cc_output     or CC_OUTPUT
    build_output  = build_output  or BUILD_OUTPUT
    plan_start    = plan_start    or PLAN_START
    planning_days = planning_days or PLANNING_DAYS

    # ── Step 1: Curing Consumption (dynamic) ──────────────────────────────────
    print("\n" + "=" * 70)
    print("  PIPELINE — Step 1: Curing Consumption (Dynamic)")
    print("=" * 70)
    cc_result = run_dynamic_consumption(
        demand_path=demand_path,
        output_path=cc_output,
        plan_start=plan_start,
        planning_days=planning_days,
    )
    co_events = cc_result["co_events"]
    df_day0   = cc_result["df_day0"]
    print(f"\n  [Pipeline] Step 1 complete — {len(co_events)} CO events, "
          f"output: {os.path.basename(cc_output)}")

    # ── Step 2: Write Day 0 as temp consumption table ─────────────────────────
    # building_b2c reads consumption from a file ("Consumption Summary" sheet).
    # We derive this from df_day0 (same data source), filtering to RI + eligible NRI.
    df_cons = df_day0[df_day0["Category"].isin({"Runner-In", "Non-Runner-In"})].copy()
    # Drop excluded NRI SKUs (those with a non-empty Skip_Reason)
    if "Skip_Reason" in df_cons.columns:
        df_cons = df_cons[
            df_cons["Skip_Reason"].isna() | (df_cons["Skip_Reason"].astype(str).str.strip() == "")
        ].copy()
        df_cons = df_cons.drop(columns=["Skip_Reason"], errors="ignore")

    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False, dir=tempfile.gettempdir())
    tmp.close()
    with pd.ExcelWriter(tmp.name, engine="openpyxl") as writer:
        df_cons.to_excel(writer, sheet_name="Consumption Summary", index=False)
    print(f"  [Pipeline] Temp consumption table: {len(df_cons)} SKUs "
          f"({df_cons[df_cons['Category']=='Runner-In'].shape[0]} RI, "
          f"{df_cons[df_cons['Category']=='Non-Runner-In'].shape[0]} NRI)")

    # ── Step 3: Building Scheduler (B2C) ──────────────────────────────────────
    print("\n" + "=" * 70)
    print("  PIPELINE — Step 2: Building Scheduler (B2C)")
    print("=" * 70)
    try:
        build_result = run_from_database_b2c(
            plan_start=plan_start,
            consumption_path=tmp.name,
            output_path=build_output,
            planning_days=planning_days,
            external_co_schedule=co_events,
        )
    finally:
        os.unlink(tmp.name)

    print(f"\n  [Pipeline] Step 2 complete — output: {os.path.basename(build_output)}")

    return {
        "co_events":    co_events,
        "n_co":         len(co_events),
        "cc_output":    cc_output,
        "build_output": build_output,
        "build_result": build_result,
    }


if __name__ == "__main__":
    _demand = sys.argv[1] if len(sys.argv) > 1 else None
    result = run_pipeline(demand_path=_demand)
    print("\n" + "=" * 70)
    print("  PIPELINE COMPLETE")
    print("=" * 70)
    print(f"  Changeovers scheduled : {result['n_co']}")
    print(f"  Curing consumption    : {result['cc_output']}")
    print(f"  Building schedule     : {result['build_output']}")
