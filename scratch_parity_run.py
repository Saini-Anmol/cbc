"""scratch_parity_run.py <local|cloud> <may|june|july>
Run ONE path for ONE month in an isolated process; print a PARITY_KPI json line.
Temporary parity-test harness (not part of the deployment)."""
import sys, json, os, tempfile
from datetime import datetime

MONTHS = {
    "may":  ("demand_may.xlsx",                      (2026, 5, 1), 31, "Daily_Running_Moulds"),
    "june": ("demand_tomerji_june_normalized.xlsx",  (2026, 6, 1), 30, "testing_Daily_Running_Moulds"),
    "july": ("july_demand_tomerJi1.xlsx",            (2026, 7, 1), 31, "june_Daily_Running_Moulds"),
}
mode, month = sys.argv[1], sys.argv[2]
fname, (Y, M, D), days, rmt = MONTHS[month]


def emit(res):
    print("PARITY_KPI " + json.dumps({
        "built":    round(res["total_built"]),
        "cured":    round(res["total_cured"]),
        "coverage": round(res["demand_coverage"], 2),
        "n_co":     res["n_co"],
        "cleans":   res["n_mould_cleans"],
        "writeoff": round(res["gt_writeoff"]),
        "starv":    res["starvation_events"],
    }))


if mode == "local":
    import cbc_env
    import bc_config as bc
    bc.DEMAND_FILE = os.path.join(cbc_env.INPUT_DIR, fname)
    bc.PLAN_START = datetime(Y, M, D, 7, 0, 0)
    bc.PLANNING_DAYS = days
    bc.RUNNING_MOULDS_TABLE = rmt
    bc.MAX_CHANGEOVERS_PER_DAY = 12
    import b2c_pipeline
    from b2c_pipeline import run_rolling_pipeline
    wd = tempfile.mkdtemp()
    res = run_rolling_pipeline(
        demand_path=bc.DEMAND_FILE, plan_start=bc.PLAN_START, planning_days=days,
        build_output=os.path.join(wd, "b.xlsx"), curing_output=os.path.join(wd, "c.xlsx"),
    )
    emit(res)

else:  # cloud
    import main
    summ = main.run_plan(f"PARITY_{month}", created_by="parity", running_moulds_table=rmt)
    k = summ["kpi"]
    print("PARITY_KPI " + json.dumps({
        "built": k["gt_built"], "cured": k["gt_cured"], "coverage": k["coverage"],
        "n_co": k["curing_cos"], "cleans": k["mould_cleans"],
        "writeoff": k["gt_writeoff"], "starv": k["starvation"],
    }))
