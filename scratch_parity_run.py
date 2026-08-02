"""scratch_parity_run.py <local|cloud> <may|june|july>
Run ONE path for ONE month in an isolated process; print a PARITY_KPI json line.
Temporary parity-test harness (not part of the deployment)."""
import sys, json, os, tempfile
from datetime import datetime

MONTHS = {
    # RUNNING MOULDS IS ALWAYS Daily_Running_Moulds (the live Day-0 snapshot) for
    # every month — the historical testing_/june_ variants are retired.
    "may":  ("demand_may.xlsx",             (2026, 5, 1), 31, "Daily_Running_Moulds"),
    "june": ("june_demand_tomerji.xlsx",    (2026, 6, 1), 30, "Daily_Running_Moulds"),
    "july": ("july_demand_tomerJi1.xlsx",   (2026, 7, 1), 31, "Daily_Running_Moulds"),
    "august": ("august_demand_tomerji.xlsx", (2026, 8, 1), 31, "Daily_Running_Moulds"),
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
    # PLAN_MONTH / RUNNING_MOULDS_MONTH are imported BY VALUE by the engine modules at
    # bc_config import time (they key the gt_inventory + running-moulds SQL). Set them in
    # the env from THIS month BEFORE importing bc_config, else those queries use the
    # default-August month. setdefault → an explicit env override still wins.
    os.environ.setdefault("PLAN_MONTH", f"{Y:04d}-{M:02d}")
    os.environ.setdefault("RUNNING_MOULDS_MONTH", f"{Y:04d}-{M:02d}")
    import cbc_env
    import bc_config as bc
    # Env overrides let a test point a month at a custom demand file / running-moulds table
    # (e.g. June/July with the user-specified files) without editing the MONTHS table.
    _dovr = os.environ.get("DEMAND_OVR")
    bc.DEMAND_FILE = _dovr if _dovr else os.path.join(cbc_env.INPUT_DIR, fname)
    bc.PLAN_START = datetime(Y, M, D, 7, 0, 0)
    bc.PLANNING_DAYS = days
    bc.RUNNING_MOULDS_TABLE = os.environ.get("RMT_OVR", rmt)
    bc.MAX_CHANGEOVERS_PER_DAY = int(os.environ.get("MAX_CO", "12"))
    import b2c_pipeline
    from b2c_pipeline import run_rolling_pipeline
    wd = tempfile.mkdtemp()
    _cout = os.environ.get("CURE_OUT", os.path.join(wd, "c.xlsx"))
    _bout = os.environ.get("BLD_OUT", os.path.join(wd, "b.xlsx"))
    res = run_rolling_pipeline(
        demand_path=bc.DEMAND_FILE, plan_start=bc.PLAN_START, planning_days=days,
        build_output=_bout, curing_output=_cout,
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
