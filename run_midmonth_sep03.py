"""Mid-month run: 03-Sep-2026 → 30-Sep-2026 (28 days).

Demand  = BTP_SEPT26_DEMAND minus the 2-day actual curing production
          (already deducted into BTP_SEPT26_DEMAND_minus_2day_prod.xlsx).
State   = the REAL 2026-09-03 snapshot (running moulds / GT / carcass).

Two things must be forced, both BEFORE the engine imports:
  1. PLANT_2DAY_REPLAY=0 — the plant 2-day replay file is indexed by PLAN-DAY,
     so leaving it on would replay the 1st-2nd's schedule over 3rd-4th Sept.
  2. connection.PLAN_DATE must stay 2026-09-03. connection._resolve_snapshot
     recomputes f"{PLAN_MONTH}-01" and REBINDS the global that all 9 snapshot
     queries read — unless its cache is already warm for this month, in which
     case it early-returns without touching PLAN_DATE. So we pre-warm the cache.
No pipeline source is modified.
"""
from __future__ import annotations
import os
from datetime import datetime

SNAP_DATE = "2026-09-03"
PLAN_MONTH = "2026-09"
DAYS = 28                                   # 3 Sep .. 30 Sep inclusive

# ── env must be set before b2c_pipeline import (module-level flags read it) ──
os.environ["PLANT_2DAY_REPLAY"] = "0"
os.environ["PLAN_DATE"] = SNAP_DATE
os.environ["PLAN_MONTH"] = PLAN_MONTH

import bc_config as bc
bc.PLAN_START = datetime(2026, 9, 3, 7, 0, 0)
bc.PLANNING_DAYS = DAYS
bc.PLAN_MONTH = PLAN_MONTH
bc.PLAN_DATE = SNAP_DATE
bc.DEMAND_FILE = os.path.join(bc.INPUT_DIR, "BTP_SEPT26_DEMAND_minus_2day_prod.xlsx")

# ── pin the snapshot date and pre-warm the resolver cache ──
import connection as conn
conn.PLAN_MONTH = PLAN_MONTH
conn.PLAN_DATE = SNAP_DATE
conn._SNAPSHOT_RESOLVED = (PLAN_MONTH, SNAP_DATE, PLAN_MONTH)   # → resolver early-returns

# ── verify the snapshot really is the 3rd, before running ──
import pandas as pd
_e = bc.make_engine()
for _t in ("Daily_Running_Moulds", "gt_inventory_manual", "carcass_inventory_manual"):
    _n = pd.read_sql(f"SELECT COUNT(*) AS n FROM {_t} WHERE date = '{SNAP_DATE}'", _e).iloc[0]["n"]
    print(f"[pre-check] {_t} @ {SNAP_DATE}: {_n} rows")

import b2c_pipeline
from b2c_pipeline import run_rolling_pipeline
print(f"[pre-check] connection.PLAN_DATE = {conn.PLAN_DATE}  (must be {SNAP_DATE})")
print(f"[pre-check] PLANT_2DAY_REPLAY    = {b2c_pipeline._PLANT_2DAY_REPLAY} (must be False)")
print(f"[pre-check] demand file          = {os.path.basename(bc.DEMAND_FILE)}")

_out = os.path.join(bc.OUTPUT_DIR, "main_output")
os.makedirs(_out, exist_ok=True)
res = run_rolling_pipeline(
    demand_path=bc.DEMAND_FILE,
    plan_start=bc.PLAN_START,
    planning_days=DAYS,
    build_output=os.path.join(_out, f"bc_building_schedule_MIDMONTH_{SNAP_DATE}.xlsx"),
    curing_output=os.path.join(_out, f"bc_curing_b2c_MIDMONTH_{SNAP_DATE}.xlsx"),
)
print("\n" + "=" * 62)
print(f"  MID-MONTH RUN COMPLETE  {SNAP_DATE} +{DAYS}d")
print("=" * 62)
print(f"  GT built   : {res['total_built']:>10,.0f}")
print(f"  GT cured   : {res['total_cured']:>10,.0f}")
print(f"  Coverage   : {res['demand_coverage']:>9.1f}%")
print(f"  Curing COs : {res['n_co']:>10,}")
print(f"  Starvation : {res['starvation_events']:>10,}")
print(f"  Building   : {res['build_output']}")
print(f"  Curing     : {res['curing_output']}")
print(f"[snapshot used] connection.PLAN_DATE = {conn.PLAN_DATE}")
