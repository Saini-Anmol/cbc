"""
b2c_pipeline.py — End-to-end B2C scheduling pipeline.

Orchestrates three stages:
  1. Curing Consumption (dynamic) — 31-day CO schedule + press state
  2. Building Scheduler (B2C)     — shift-wise GT production plan
  3. Curing Schedule (B2C)        — shift-wise curing plan derived from GT output

All parameters are read from bc_config.py — edit there, not here.

Usage:
    python b2c_pipeline.py                           # uses bc_config.py defaults
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
from curing_b2c import run_curing_b2c

# ── All params from bc_config (single source of truth) ────────────────────────
from bc_config import (
    PLAN_START,
    PLANNING_DAYS,
    DEMAND_FILE,
    MAX_CHANGEOVERS_PER_DAY,
    MIN_CAMPAIGN_MINS,
    BUILD_LEAD_SHIFTS,
    DYNAMIC_CC_OUTPUT  as CC_OUTPUT,
    BUILDING_OUTPUT    as BUILD_OUTPUT,
    CURING_B2C_OUTPUT  as CURING_OUTPUT,
)


def run_pipeline(
    demand_path:   str | None = None,
    cc_output:     str | None = None,
    build_output:  str | None = None,
    curing_output: str | None = None,
    plan_start:    datetime | None = None,
    planning_days: int | None = None,
) -> dict:
    """
    Run the full B2C pipeline:
      Step 1 — Curing Consumption (dynamic)
      Step 2 — Building Schedule (B2C)
      Step 3 — Curing Schedule (B2C, derived from GT output)

    All paths default to bc_config.py values.

    Returns a dict with:
        co_events      : list of CO event dicts from the curing consumption step
        n_co           : total changeovers scheduled
        cc_output      : path to curing consumption Excel
        build_output   : path to building schedule Excel
        curing_output  : path to curing schedule Excel
        build_result   : return value of run_from_database_b2c()
        curing_result  : return value of run_curing_b2c()
    """
    demand_path   = demand_path   or DEMAND_FILE
    cc_output     = cc_output     or CC_OUTPUT
    build_output  = build_output  or BUILD_OUTPUT
    curing_output = curing_output or CURING_OUTPUT
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
        max_co_per_day=MAX_CHANGEOVERS_PER_DAY,
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
            max_changeovers_per_day=MAX_CHANGEOVERS_PER_DAY,
            min_campaign_mins=MIN_CAMPAIGN_MINS,
            build_lead_shifts=BUILD_LEAD_SHIFTS,
        )
    finally:
        os.unlink(tmp.name)

    print(f"\n  [Pipeline] Step 2 complete — output: {os.path.basename(build_output)}")

    # ── Step 3: Curing Schedule (B2C) ─────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  PIPELINE — Step 3: Curing Schedule (B2C)")
    print("=" * 70)
    curing_result = run_curing_b2c(
        building_path = build_output,
        output_path   = curing_output,
        demand_path   = demand_path,
        plan_start    = plan_start,
        planning_days = planning_days,
    )
    total_cured = sum(curing_result["daily_cured"].values())
    print(f"\n  [Pipeline] Step 3 complete — Total cured: {total_cured:,.0f}  "
          f"output: {os.path.basename(curing_output)}")

    return {
        "co_events":     co_events,
        "n_co":          len(co_events),
        "cc_output":     cc_output,
        "build_output":  build_output,
        "curing_output": curing_output,
        "build_result":  build_result,
        "curing_result": curing_result,
    }


if __name__ == "__main__":
    _demand = sys.argv[1] if len(sys.argv) > 1 else None
    result = run_pipeline(demand_path=_demand)
    print("\n" + "█" * 70)
    print("  PIPELINE COMPLETE")
    print("█" * 70)
    print(f"  Changeovers scheduled : {result['n_co']}")
    print(f"  1. Curing consumption : {result['cc_output']}")
    print(f"  2. Building schedule  : {result['build_output']}")
    print(f"  3. Curing schedule    : {result['curing_output']}")
    total = sum(result["curing_result"]["daily_cured"].values())
    print(f"  Total cured (month)   : {total:,.0f} tyres")
    print("█" * 70)
