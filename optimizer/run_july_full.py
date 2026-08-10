"""optimizer/run_july_full.py — run the pure CP-SAT optimizer for JULY 2026 and write
its committed month plan in the FULL greedy multi-sheet Excel format to main_output/.

Config: pure CP-SAT v1 (WARMSTART=none, INCH_RELAX=0), det_time_s=120, workers=8, seed=1.
Expect ~8 min (3-4 rolling windows x 120s). Env month is set BEFORE importing the engine
so all month-keyed ETLs (running moulds, opening GT) read 2026-07.
"""
import os
from datetime import datetime

os.environ.setdefault("RUNNING_MOULDS_MONTH", "2026-07")
os.environ.setdefault("PLAN_MONTH", "2026-07")
os.environ.setdefault("WARMSTART", "none")  # respect env WARMSTART (none|greedy|simple)
os.environ.pop("INCH_RELAX", None)     # -> 0

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMAND = os.path.join(REPO, "data", "input", "july_demand_tomerJi1.xlsx")

from optimizer.data import load_model_inputs
from optimizer.driver import run_rolling
from optimizer.output_full import write_full_schedules
from curing_consumption import ConsumptionETL
from cbc_env import make_engine


def main():
    plan_start = datetime(2026, 7, 1, 7, 0, 0)
    # Keep bc_config.DEMAND_FILE consistent with the month we're solving so the greedy
    # warm-start (which reads bc.DEMAND_FILE) builds its hint on the SAME (July) demand,
    # not whatever month bc_config was last pinned to (was August -> mismatched hint).
    import bc_config as _bc
    _bc.DEMAND_FILE = DEMAND
    mi = load_model_inputs(demand_path=DEMAND, plan_start=plan_start, planning_days=31)
    print("ModelInputs:", mi.summary())

    # cure cycle-time map (minutes) for the writers' CycleTime columns
    cetl = ConsumptionETL(make_engine())
    ct_df = cetl.load_cycle_times()
    ct_map = {str(r.SKUCode): float(r.CycleTime_min) for r in ct_df.itertuples()}

    _det = float(os.environ.get("OPT_DET_TIME", "240"))
    _ws = os.environ.get("WARMSTART", "none")
    print(f"Rolling CP-SAT: det_time={_det:.0f}s workers=8 seed=1 warmstart={_ws}  demand={sum(mi.demand.values()):,}")
    print("-" * 78)
    res = run_rolling(mi, det_time_s=_det, workers=8, seed=1, verbose=True)
    print("-" * 78)
    cov = res["coverage"] * 100
    print(f"FULL MONTH: cured={res['total_cured']:,} / demand={res['total_demand']:,} = {cov:.2f}%")
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
