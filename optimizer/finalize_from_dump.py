"""optimizer/finalize_from_dump.py — replay ALL post-solve processing (same-CO trim, mould
clean, GT aging write-off, JIT carcass reschedule) on a DUMPED raw CP-SAT plan and re-write the
full Excel schedules — with NO re-solve. Lets us iterate + re-validate any post-process fix in
seconds instead of a 16-min solve.

Usage (produce the dump with OPT_DUMP_RAW=<path> on a normal run, then):
  MONTH_NUM=7 MONTH_DAYS=31 MONTH_DEMAND=july_demand_tomerJi1.xlsx DUMP_PATH=<path> \
  OPT_SAME_CO_TRIM=1 OPT_BUILT_CAP=1 OPT_AGE_TRIM=1 OPT_CARC_RESCHED=1 \
  myenv/bin/python -m optimizer.finalize_from_dump
Then run validate_schedule.py on the rewritten main_output/*.
"""
import os
import pickle
from datetime import datetime

_Y = int(os.environ.get("MONTH_YEAR", "2026"))
_M = int(os.environ.get("MONTH_NUM", "7"))
_DAYS = int(os.environ.get("MONTH_DAYS", "31"))
_MONTH = f"{_Y:04d}-{_M:02d}"
os.environ["RUNNING_MOULDS_MONTH"] = _MONTH
os.environ["PLAN_MONTH"] = _MONTH
# post-process toggles (default ON; override via env). These are read at driver import time.
for _k, _v in {"OPT_SAME_CO_TRIM": "1", "OPT_MOULD_CLEAN": "1", "OPT_AGE_TRIM": "1",
               "OPT_CARC_RESCHED": "1", "OPT_ENDDAY_GT": "1", "OPT_MOULD_INFO": "1",
               "OPT_CURE_CLIP": "1"}.items():
    os.environ.setdefault(_k, _v)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DUMP = os.environ.get("DUMP_PATH") or os.environ["OPT_DUMP_RAW"]
DEMAND = os.path.join(REPO, "data", "input",
                      os.environ.get("MONTH_DEMAND", "july_demand_tomerJi1.xlsx"))

from optimizer.data import load_model_inputs
from optimizer import driver as D
from optimizer.output_full import write_full_schedules
from curing_consumption import ConsumptionETL
from cbc_env import make_engine
import bc_config as _bc
_bc.DEMAND_FILE = DEMAND


def main():
    mi = load_model_inputs(demand_path=DEMAND, plan_start=datetime(_Y, _M, 1, 7, 0, 0),
                           planning_days=_DAYS)
    with open(DUMP, "rb") as f:
        raw = pickle.load(f)
    print(f"MONTH={_MONTH} days={_DAYS}  raw cured={raw['total_cured_raw']:,}  "
          f"bld_rows={len(raw['building_rows']):,} cure_rows={len(raw['curing_rows']):,}")
    res = D._finalize_plan(mi, raw["building_rows"], raw["curing_rows"], raw["mould_rows"],
                           raw["endday_gt_by_date"], raw["total_cured_raw"], raw["total_co"],
                           raw["total_demand"], raw["windows"], 0.0, True)
    print(f"FINAL cured={res['total_cured']:,} / demand={res['total_demand']:,} = "
          f"{res['coverage']*100:.2f}%")
    cetl = ConsumptionETL(make_engine())
    ct_df = cetl.load_cycle_times()
    ct_map = {str(r.SKUCode): float(r.CycleTime_min) for r in ct_df.itertuples()}
    paths = write_full_schedules(res, mi, ct_map=ct_map, out_dir="main_output", demand_path=DEMAND)
    print(f"written: {paths['building']}  |  {paths['curing']}")


if __name__ == "__main__":
    main()
