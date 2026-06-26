"""
JK Tyre BTP — B2C ORCHESTRATOR  (Building → Curing)
=====================================================
Wires the four B2C pipeline stages together:

    Phase 0  CONSUMPTION TABLE   curing_consumption.py
                                  Reads active curing press state + cycle times.
                                  Computes GT consumption per shift per SKU.
                                  Classifies SKUs: Runner-In / Runner-Out / Non-Runner-In.
                                  Output: curing_consumption_table.xlsx

    Phase 1  BUILDING SCHEDULE   building_b2c.py
                                  Schedules 39 building machines driven by consumption.
                                  Runner-In first; joint priority pool for NRI + RO.
                                  Real opening GT inventory (not cold-start).
                                  Build pre-start: 1 shift before first curing shift.
                                  Output: bc_building_schedule.xlsx

    Phase 2  CURING SCHEDULE     curing_deriver.py
                                  Derives curing output deterministically.
                                  Rolling GT balance per SKU: building output
                                  feeds each shift's curing consumption.
                                  Output: bc_curing_schedule.xlsx

    Phase 3  ANALYSIS & KPIs     bc_analyser.py
                                  Monthly KPIs, starvation report, utilisation.
                                  Output: bc_analysis.xlsx

Data layout:  inputs in data/input/, outputs in data/output/.
DB creds:     data from .env via cbc_env.py (unchanged from CBC pipeline).

Run:
    python bc.py
"""

from __future__ import annotations

import os
import sys

# ── Run under the project venv even if launched as `python3 bc.py` ───────────
_VENV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "myenv")
_VENV_PY  = os.path.join(_VENV_DIR, "bin", "python")
if (os.path.exists(_VENV_PY)
        and os.path.realpath(sys.prefix) != os.path.realpath(_VENV_DIR)
        and not os.environ.get("BC_REEXEC")):
    os.environ["BC_REEXEC"] = "1"
    os.execv(_VENV_PY, [_VENV_PY, os.path.abspath(__file__)] + sys.argv[1:])

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

import cbc_env

from curing_consumption import build_consumption_table
from building_b2c       import run_from_database_b2c
from curing_deriver     import derive_curing_schedule
from bc_analyser        import run_analysis

HERE     = os.path.dirname(os.path.abspath(__file__))
IN       = cbc_env.INPUT_DIR
OUT      = cbc_env.OUTPUT_DIR
MAIN_OUT = os.path.join(OUT, "main_output")
os.makedirs(MAIN_OUT, exist_ok=True)

# ══════════════════════════════════════════════════════════════════════════════
# USER SETTINGS  —  edit these for each planning run, then `python3 bc.py`
# ══════════════════════════════════════════════════════════════════════════════
# 1) Demand file. Drop the new demand workbook in data/input/ and point here.
#    Required columns: SKUCode, Requirement (or Updated_Requirement),
#    ConsolidatedPriorityScore. Everything else comes from the DB.
DEMAND_FILE = os.path.join(IN, "demand_may.xlsx")

# 2) First shift of the plan (07:00 = shift-A start) and number of days.
#    Building pre-start = 1 shift before this (Apr 30 Shift C for May plan).
PLAN_START_DT = datetime(2026, 5, 1, 7, 0, 0)
PLANNING_DAYS = 31


# ══════════════════════════════════════════════════════════════════════════════
# BC CONFIG DATACLASS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BCConfig:
    # Demand
    DEMAND_FILE:   str      = DEMAND_FILE

    # Plan horizon
    PLAN_START:    datetime = field(default_factory=lambda: PLAN_START_DT)
    PLANNING_DAYS: int      = PLANNING_DAYS

    # Phase 0 output
    CONSUMPTION_OUTPUT: str = os.path.join(OUT, "curing_consumption_table.xlsx")

    # Phase 1 output
    BUILDING_OUTPUT: str = os.path.join(MAIN_OUT, "bc_building_schedule.xlsx")

    # Phase 2 output
    CURING_OUTPUT: str = os.path.join(MAIN_OUT, "bc_curing_schedule.xlsx")

    # Phase 3 output
    ANALYSIS_OUTPUT: str = os.path.join(MAIN_OUT, "bc_analysis.xlsx")

    # Pipeline control
    RUN_BUILDING:  bool = True
    RUN_CURING:    bool = True
    RUN_ANALYSIS:  bool = True


# ══════════════════════════════════════════════════════════════════════════════
# PHASE FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def phase0_consumption(cfg: BCConfig, engine) -> dict:
    """
    Build the curing GT consumption table.

    Returns the dict from curing_consumption.build_consumption_table().
    Output: curing_consumption_table.xlsx
    """
    print("\n" + "▓" * 72)
    print("▓  PHASE 0 — Curing Consumption Table")
    print("▓" * 72)

    result = build_consumption_table(
        demand_path   = cfg.DEMAND_FILE,
        engine        = engine,
        output_path   = cfg.CONSUMPTION_OUTPUT,
        plan_start    = cfg.PLAN_START,
        planning_days = cfg.PLANNING_DAYS,
    )

    df = result["consumption_df"]
    ri  = (df["Category"] == "Runner-In").sum()
    ro  = (df["Category"] == "Runner-Out").sum()
    nri = (df["Category"] == "Non-Runner-In").sum()
    total_gt = df.loc[df["Category"] == "Runner-In", "Total_GT_Per_Shift_Day0"].sum()

    print(f"\n  ✓ Phase 0 complete")
    print(f"    Runner-In: {ri}  |  Runner-Out: {ro}  |  Non-Runner-In: {nri}")
    print(f"    Total GT consumed/shift (Runner-In presses): {total_gt:,.0f}")

    return result


def phase1_building(cfg: BCConfig, consumption: dict, engine) -> dict:
    """
    Run the B2C building scheduler.

    Returns the results dict from building_b2c.run_from_database_b2c().
    Output: bc_building_schedule.xlsx
    """
    if not cfg.RUN_BUILDING:
        print("\n  [bc.py] RUN_BUILDING=False — skipping Phase 1.")
        return {}

    print("\n" + "▓" * 72)
    print("▓  PHASE 1 — Building Schedule (B2C)")
    print("▓" * 72)

    results = run_from_database_b2c(
        plan_start       = cfg.PLAN_START,
        consumption_path = cfg.CONSUMPTION_OUTPUT,
        output_path      = cfg.BUILDING_OUTPUT,
        engine           = engine,
        planning_days    = cfg.PLANNING_DAYS,
    )

    ss = results.get("shift_schedule")
    if ss is not None and not ss.empty:
        prod = ss[~ss["SKUCode"].astype(str).isin(
            {"CHANGEOVER", "MOULD_CLEAN", "C/O", "CLEANING"}
        )]
        total_gt = prod[prod["Machine"].astype(str).apply(
            lambda m: m in {
                "8201","8301","8302","8501","8502","7301",
                "7001","7002","7003","7004","6001","6002","6003","6004",
                "7101","7102","7103","7104","7105","7106",
                "7201","7501","7502","7503",
            }
        )]["Qty"].sum()
        print(f"\n  ✓ Phase 1 complete — Total GT built: {total_gt:,.0f}")
    else:
        print("\n  ✓ Phase 1 complete")

    return results


def phase2_curing(
    cfg: BCConfig,
    building_results: dict,
    engine,
) -> dict:
    """
    Derive the curing schedule from building output.

    Returns the results dict from curing_deriver.derive_curing_schedule().
    Output: bc_curing_schedule.xlsx
    """
    if not cfg.RUN_CURING:
        print("\n  [bc.py] RUN_CURING=False — skipping Phase 2.")
        return {}

    print("\n" + "▓" * 72)
    print("▓  PHASE 2 — Curing Schedule (Derived)")
    print("▓" * 72)

    # Pass changeover plan from building if available
    co_plan = building_results.get("co_plan")

    results = derive_curing_schedule(
        building_path    = cfg.BUILDING_OUTPUT,
        consumption_path = cfg.CONSUMPTION_OUTPUT,
        output_path      = cfg.CURING_OUTPUT,
        engine           = engine,
        co_plan          = co_plan,
    )

    df_day = results.get("daily_summary")
    if df_day is not None and not df_day.empty:
        total   = df_day["Total_Cured"].sum()
        starv   = df_day["Starvation_Events"].sum()
        avg     = total / max(len(df_day), 1)
        print(f"\n  ✓ Phase 2 complete")
        print(f"    Total Cured Tyres (Month): {total:,.0f}")
        print(f"    Avg Daily Cured:           {avg:,.0f}")
        print(f"    Starvation Events:         {starv}")
    else:
        print("\n  ✓ Phase 2 complete")

    return results


def phase3_analysis(cfg: BCConfig) -> "bc_analyser.BCAnalyser | None":
    """
    Run KPI analysis on all three output files.

    Returns the BCAnalyser instance.
    Output: bc_analysis.xlsx
    """
    if not cfg.RUN_ANALYSIS:
        print("\n  [bc.py] RUN_ANALYSIS=False — skipping Phase 3.")
        return None

    print("\n" + "▓" * 72)
    print("▓  PHASE 3 — Analysis & KPIs")
    print("▓" * 72)

    analyser = run_analysis(
        building_path    = cfg.BUILDING_OUTPUT,
        curing_path      = cfg.CURING_OUTPUT,
        consumption_path = cfg.CONSUMPTION_OUTPUT,
        output_path      = cfg.ANALYSIS_OUTPUT,
    )

    print(f"\n  ✓ Phase 3 complete")
    return analyser


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run_bc(cfg: BCConfig | None = None) -> dict:
    """
    Run the full B2C pipeline.

    Returns:
        {
          "consumption":  dict from Phase 0,
          "building":     dict from Phase 1,
          "curing":       dict from Phase 2,
          "analysis":     BCAnalyser from Phase 3,
        }
    """
    cfg = cfg or BCConfig()
    engine = cbc_env.make_engine()

    print("\n" + "█" * 72)
    print("█  JK Tyre BTP — B2C PIPELINE")
    print(f"█  Plan start: {cfg.PLAN_START}  |  Days: {cfg.PLANNING_DAYS}")
    print(f"█  Demand:     {os.path.basename(cfg.DEMAND_FILE)}")
    print("█" * 72)

    consumption = phase0_consumption(cfg, engine)
    building    = phase1_building(cfg, consumption, engine)
    curing      = phase2_curing(cfg, building, engine)
    analysis    = phase3_analysis(cfg)

    print("\n" + "█" * 72)
    print("█  B2C PIPELINE COMPLETE")
    print(f"█  Phase 0 output : {cfg.CONSUMPTION_OUTPUT}")
    print(f"█  Phase 1 output : {cfg.BUILDING_OUTPUT}")
    if cfg.RUN_CURING:
        print(f"█  Phase 2 output : {cfg.CURING_OUTPUT}")
    else:
        print("█  Phase 2 (curing) : SKIPPED — review building schedule first")
    if cfg.RUN_ANALYSIS:
        print(f"█  Phase 3 output : {cfg.ANALYSIS_OUTPUT}")
    else:
        print("█  Phase 3 (analysis): SKIPPED")
    print("█" * 72 + "\n")

    return {
        "consumption": consumption,
        "building":    building,
        "curing":      curing,
        "analysis":    analysis,
    }


if __name__ == "__main__":
    run_bc()
