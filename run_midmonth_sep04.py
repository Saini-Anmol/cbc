"""Mid-month run: 04-Sep-2026 -> 30-Sep-2026 (27 days).

Demand  = BTP_SEPT26_DEMAND (ORIGINAL) minus 3 days of ACTUAL curing production
          (curingPCR_4sep.xlsx, production days 1-3, 07:00-anchored)
          -> BTP_SEPT26_DEMAND_minus_3day_prod.xlsx
State   = the REAL 2026-09-04 DB snapshot (running moulds / GT / carcass)
Carry-in= each building machine's SKU at the END of 3-Sept, derived from
          bc_building_schedule_2026-09-01.xlsx -> day4_seed_end_of_3sep.xlsx
"""
from __future__ import annotations
import os
from datetime import datetime

SNAP_DATE  = "2026-09-04"
PLAN_MONTH = "2026-09"
DAYS       = 27                                  # 4 Sep .. 30 Sep inclusive
SEED       = "data/input/day4_seed_end_of_3sep.xlsx"

# env must be set BEFORE b2c_pipeline import (module-level flags read it)
os.environ["PLANT_2DAY_REPLAY"] = "0"            # its Day col is a plan-day index
os.environ["PLAN_DATE"]  = SNAP_DATE
os.environ["PLAN_MONTH"] = PLAN_MONTH
os.environ["MIDMONTH_SEED_FILE"] = os.path.abspath(SEED)

import bc_config as bc
bc.PLAN_START    = datetime(2026, 9, 4, 7, 0, 0)
bc.PLANNING_DAYS = DAYS
bc.PLAN_MONTH    = PLAN_MONTH
bc.PLAN_DATE     = SNAP_DATE
bc.DEMAND_FILE   = os.path.join(bc.INPUT_DIR, "BTP_SEPT26_DEMAND_minus_3day_prod.xlsx")

import connection as conn
conn.PLAN_MONTH = PLAN_MONTH
conn.PLAN_DATE  = SNAP_DATE
conn._SNAPSHOT_RESOLVED = (SNAP_DATE, SNAP_DATE, PLAN_MONTH)   # pre-warm -> early return

import pandas as pd
_e = bc.make_engine()
for _t in ("Daily_Running_Moulds", "gt_inventory_manual", "carcass_inventory_manual"):
    _n = pd.read_sql(f"SELECT COUNT(*) n FROM {_t} WHERE date='{SNAP_DATE}'", _e).iloc[0]["n"]
    print(f"[pre-check] {_t} @ {SNAP_DATE}: {_n} rows")

import b2c_pipeline
from b2c_pipeline import run_rolling_pipeline
print(f"[pre-check] connection.PLAN_DATE = {conn.PLAN_DATE}")
print(f"[pre-check] PLANT_2DAY_REPLAY    = {b2c_pipeline._PLANT_2DAY_REPLAY}")
print(f"[pre-check] demand file          = {os.path.basename(bc.DEMAND_FILE)}")
print(f"[pre-check] holidays             = {bc.PLANT_HOLIDAYS}")

_out = os.path.join(bc.OUTPUT_DIR, "main_output"); os.makedirs(_out, exist_ok=True)
res = run_rolling_pipeline(
    demand_path=bc.DEMAND_FILE, plan_start=bc.PLAN_START, planning_days=DAYS,
    build_output=os.path.join(_out, f"bc_building_schedule_MIDMONTH_{SNAP_DATE}.xlsx"),
    curing_output=os.path.join(_out, f"bc_curing_b2c_MIDMONTH_{SNAP_DATE}.xlsx"))
print("\n" + "="*62)
print(f"  MID-MONTH RUN COMPLETE  {SNAP_DATE} +{DAYS}d")
print("="*62)
print(f"  GT built   : {res['total_built']:>10,.0f}")
print(f"  GT cured   : {res['total_cured']:>10,.0f}")
print(f"  Coverage   : {res['demand_coverage']:>9.2f}%")
print(f"  Curing COs : {res['n_co']:>10,}")
print(f"  Starvation : {res['starvation_events']:>10,}")
print(f"  Expired GT : {res.get('gt_writeoff',0):>10,.0f}")
print(f"  Building   : {res['build_output']}")
print(f"  Curing     : {res['curing_output']}")
