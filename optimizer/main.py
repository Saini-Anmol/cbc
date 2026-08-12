"""optimizer/main.py — SINGLE ENTRY POINT for the CP-SAT optimizer model.

Reads optimizer/opt_config.py (edit ALL parameters there), runs the full rolling-window month
plan, and writes the multi-sheet Excel schedules to main_output/.

Run:                 myenv/bin/python -m optimizer.main
Quick smoke (1 window, 60s):   OPT_DET_TIME=60 MONTH_DAYS=10 myenv/bin/python -m optimizer.main
Then validate:       myenv/bin/python validate_schedule.py --building main_output/optimizer_building_schedule_full_<YYYY-MM>-01.xlsx --curing main_output/optimizer_curing_schedule_full_<YYYY-MM>-01.xlsx --demand data/input/<demand> --plan-month <YYYY-MM>
"""
import os
from datetime import datetime

# 1) Load the consolidated config FIRST — it populates every env var the engine reads.
from optimizer import opt_config as cfg

# 2) Now import the engine (its module-level constants read the env opt_config just set).
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEMAND = os.path.join(_REPO, "data", "input", cfg.DEMAND_FILE)

import bc_config as _bc
_bc.DEMAND_FILE = _DEMAND                 # keep the greedy warm-start on this month's demand
from optimizer.data import load_model_inputs
from optimizer.driver import run_rolling
from optimizer.output_full import write_full_schedules
from curing_consumption import ConsumptionETL
from cbc_env import make_engine


def main():
    plan_start = datetime(int(cfg.MONTH_YEAR), int(cfg.MONTH_NUM), 1, 7, 0, 0)
    print("=" * 84)
    print("OPTIMIZER  |  " + cfg.summary())
    print("=" * 84)

    mi = load_model_inputs(demand_path=_DEMAND, plan_start=plan_start,
                           planning_days=int(cfg.PLANNING_DAYS))
    print("ModelInputs:", mi.summary())

    cetl = ConsumptionETL(make_engine())
    ct_df = cetl.load_cycle_times()
    ct_map = {str(r.SKUCode): float(r.CycleTime_min) for r in ct_df.itertuples()}

    res = run_rolling(mi, det_time_s=float(cfg.DET_TIME_PER_WINDOW),
                      workers=int(cfg.WORKERS), seed=1, verbose=True)

    print("-" * 84)
    print(f"FULL MONTH ({cfg.PLAN_MONTH}): cured={res['total_cured']:,} / "
          f"demand={res['total_demand']:,} = {res['coverage']*100:.2f}%  "
          f"| curing COs={res['total_co']:,} | {res['total_wall_s']/60:.1f} min")

    _out_dir = os.environ.get("OPT_OUT_DIR", "main_output")   # override for A/B smoke runs
    paths = write_full_schedules(res, mi, ct_map=ct_map, out_dir=_out_dir, demand_path=_DEMAND)
    print(f"Building (full): {paths['building']}")
    print(f"Curing   (full): {paths['curing']}")
    # NOTE: the greedy sheets (greedy_building_{date}.xlsx / greedy_curing_{date}.xlsx) are saved
    # EARLIER, inside greedy_warmstart._greedy_sheets — right after the greedy runs, before the
    # optimizer windows — so they persist even if the optimizer is interrupted.


if __name__ == "__main__":
    main()
