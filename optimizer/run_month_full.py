"""optimizer/run_month_full.py — run the CP-SAT optimizer for ANY month and write its
committed plan in the full multi-sheet Excel format to main_output/. Parameterized by env
so June / July / August all use the IDENTICAL validated pipeline + fixes.

Env (set BEFORE running):
  MONTH_YEAR   (default 2026)
  MONTH_NUM    (6 | 7 | 8 ...)          -> plan_start = YYYY-MM-01 07:00
  MONTH_DAYS   (30 | 31 | 29 ...)       -> planning horizon length
  MONTH_DEMAND (filename under data/input, e.g. june_demand_tomerji.xlsx)
The month string YYYY-MM is derived and exported as RUNNING_MOULDS_MONTH + PLAN_MONTH
BEFORE the engine imports, so all month-keyed ETLs (running moulds, opening GT, opening
carcass) read the correct month. bc_config.DEMAND_FILE is pinned to the same file so the
greedy warm-start builds its hint on the SAME month's demand.

Solve knobs via env: WARMSTART (greedy), OPT_DET_TIME (240), plus the fix toggles.
"""
import os
from datetime import datetime

_Y = int(os.environ.get("MONTH_YEAR", "2026"))
_M = int(os.environ.get("MONTH_NUM", "7"))
_DAYS = int(os.environ.get("MONTH_DAYS", "31"))
_MONTH = f"{_Y:04d}-{_M:02d}"
os.environ.setdefault("RUNNING_MOULDS_MONTH", _MONTH)
os.environ.setdefault("PLAN_MONTH", _MONTH)
os.environ["RUNNING_MOULDS_MONTH"] = _MONTH        # force (override any stale value)
os.environ["PLAN_MONTH"] = _MONTH
os.environ.setdefault("WARMSTART", "none")
os.environ.pop("INCH_RELAX", None)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMAND = os.path.join(REPO, "data", "input",
                      os.environ.get("MONTH_DEMAND", "july_demand_tomerJi1.xlsx"))

from optimizer.data import load_model_inputs
from optimizer.driver import run_rolling
from optimizer.output_full import write_full_schedules
from curing_consumption import ConsumptionETL
from cbc_env import make_engine


def main():
    plan_start = datetime(_Y, _M, 1, 7, 0, 0)
    import bc_config as _bc
    _bc.DEMAND_FILE = DEMAND                        # keep greedy warm-start on this month's demand
    mi = load_model_inputs(demand_path=DEMAND, plan_start=plan_start, planning_days=_DAYS)
    print(f"MONTH={_MONTH} days={_DAYS} demand_file={os.path.basename(DEMAND)}")
    print("ModelInputs:", mi.summary())

    cetl = ConsumptionETL(make_engine())
    ct_df = cetl.load_cycle_times()
    ct_map = {str(r.SKUCode): float(r.CycleTime_min) for r in ct_df.itertuples()}

    _det = float(os.environ.get("OPT_DET_TIME", "240"))
    _ws = os.environ.get("WARMSTART", "none")
    print(f"Rolling CP-SAT: det_time={_det:.0f}s workers=8 seed=1 warmstart={_ws}  "
          f"demand={sum(mi.demand.values()):,}")
    print("-" * 78)
    res = run_rolling(mi, det_time_s=_det, workers=8, seed=1, verbose=True)
    print("-" * 78)
    cov = res["coverage"] * 100
    print(f"FULL MONTH ({_MONTH}): cured={res['total_cured']:,} / demand={res['total_demand']:,} = {cov:.2f}%")
    print(f"           curing COs={res['total_co']:,}  wall={res['total_wall_s']:,}s "
          f"({res['total_wall_s']/60:.1f} min)  windows={len(res['windows'])}")

    paths = write_full_schedules(res, mi, ct_map=ct_map, out_dir="main_output",
                                 demand_path=DEMAND)
    print("-" * 78)
    print(f"Building (full): {paths['building']}  "
          f"({paths['n_building_rows']:,} rows, {paths['n_building_cos']:,} COs)")
    print(f"Curing   (full): {paths['curing']}  "
          f"({paths['n_curing_rows']:,} rows, {paths['n_curing_cos']:,} COs)")


if __name__ == "__main__":
    main()
