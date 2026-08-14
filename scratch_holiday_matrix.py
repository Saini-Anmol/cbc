"""Holiday scenario-matrix driver. One scenario per process, fully isolated output dir.
Usage: python scratch_holiday_matrix.py <tag> <planning_days> <comma-holidays-or-NONE> <out_root>
Prints one parseable RESULT| line.
"""
import sys, os

tag       = sys.argv[1]
days      = int(sys.argv[2])
hols_arg  = sys.argv[3]
out_root  = sys.argv[4]

holidays = [] if hols_arg == "NONE" else [h for h in hols_arg.split(",") if h]

# Isolate ALL outputs to a unique dir BEFORE importing bc_config (which derives
# every output path from cbc_env.OUTPUT_DIR at import time). Zero cross-run collision.
uniq = os.path.join(out_root, f"out_{tag}")
os.makedirs(os.path.join(uniq, "main_output"), exist_ok=True)
import cbc_env
cbc_env.OUTPUT_DIR = uniq

import bc_config as bc
bc.PLANT_HOLIDAYS = holidays                      # read at runtime by the engine
from b2c_pipeline import run_rolling_pipeline

r = run_rolling_pipeline(
    planning_days=days,
    restrict_to_allowable_presses=bc.RESTRICT_PRESSES_TO_ALLOWABLE,
)

# demand total = cured / coverage (coverage is a %)
cov = r["demand_coverage"]
cured = r["total_cured"]
demand = round(cured / (cov / 100.0)) if cov else 0
print("RESULT|%s|%d|%d|%d|%d|%d|%.2f|%d|%d|%d" % (
    tag, days, len(holidays), demand,
    round(r["total_built"]), round(cured), cov,
    r["n_co"], round(r["gt_writeoff"]), r["starvation_events"]))
