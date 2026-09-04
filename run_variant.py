"""Parameterized 4-Sep..30-Sep runner for A/B experiments.

Usage:  VARIANT=<tag> [any engine env vars] python run_variant.py
Writes  data/output/variants/bc_{building,curing}_<tag>.xlsx  (never clobbers other runs)
Prints  RESULT <tag> built=<n> cured=<n> cov=<pct> cos=<n> starv=<n>
"""
from __future__ import annotations
import os
from datetime import datetime

TAG = os.environ.get("VARIANT", "base")
SNAP, PM, DAYS = "2026-09-04", "2026-09", 27

os.environ.setdefault("PLANT_2DAY_REPLAY", "0")
os.environ["PLAN_DATE"] = SNAP
os.environ["PLAN_MONTH"] = PM

import bc_config as bc
bc.PLAN_START, bc.PLANNING_DAYS = datetime(2026, 9, 4, 7, 0, 0), DAYS
bc.PLAN_MONTH, bc.PLAN_DATE = PM, SNAP
if os.environ.get("DEMAND_OVERRIDE"):
    bc.DEMAND_FILE = os.environ["DEMAND_OVERRIDE"]
else:
    bc.DEMAND_FILE = os.path.join(bc.INPUT_DIR, "BTP_SEPT26_DEMAND_minus_3day_prod.xlsx")
if os.environ.get("PLANNING_DAYS_OVERRIDE"):
    DAYS = bc.PLANNING_DAYS = int(os.environ["PLANNING_DAYS_OVERRIDE"])
if os.environ.get("PLAN_START_OVERRIDE"):          # "YYYY-MM-DD"
    _d = datetime.strptime(os.environ["PLAN_START_OVERRIDE"], "%Y-%m-%d")
    bc.PLAN_START = datetime(_d.year, _d.month, _d.day, 7, 0, 0)
    SNAP = bc.PLAN_DATE = os.environ["PLAN_START_OVERRIDE"]
    os.environ["PLAN_DATE"] = SNAP

SNAP_EFF = os.environ.get("SNAP_DATE_OVERRIDE") or SNAP    # snapshot date, independent of plan start
bc.PLAN_DATE = SNAP_EFF
os.environ["PLAN_DATE"] = SNAP_EFF

if os.environ.get("SEED_MODE") == "derive":      # plant-2day derived seed (plant-set consistent)
    bc.MIDMONTH_BUILDING_SEED_FILE = ""
    os.environ.pop("MIDMONTH_SEED_FILE", None)

import connection as conn
conn.PLAN_MONTH, conn.PLAN_DATE = PM, SNAP_EFF
conn._SNAPSHOT_RESOLVED = None

from b2c_pipeline import run_rolling_pipeline
out = os.path.join(bc.OUTPUT_DIR, "variants"); os.makedirs(out, exist_ok=True)
res = run_rolling_pipeline(
    demand_path=bc.DEMAND_FILE, plan_start=bc.PLAN_START, planning_days=bc.PLANNING_DAYS,
    build_output=os.path.join(out, f"bc_building_{TAG}.xlsx"),
    curing_output=os.path.join(out, f"bc_curing_{TAG}.xlsx"))
print(f"RESULT {TAG} built={res['total_built']:.0f} cured={res['total_cured']:.0f} "
      f"cov={res['demand_coverage']:.2f} cos={res['n_co']} starv={res['starvation_events']}")
