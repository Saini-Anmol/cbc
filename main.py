"""
main.py — CLOUD DB orchestrator (deployment Phase 3).

One run, driven entirely by a plan_id:

    read_db(plan_id)  →  inject run config  →  run_rolling_pipeline
                      →  write_db(plan_id, ...)

Everything the engine needs beyond demand+params (masters, running-moulds) is
read by the engine's own ETL from the DB. On cloud the running-moulds table is
fixed = jkplanningV1.Daily_Running_Moulds (bc_config.RUNNING_MOULDS_TABLE).

    python main.py <plan_id>
"""
from __future__ import annotations

import os
import shutil
import tempfile
import traceback

# ══════════════════════════════════════════════════════════════════════════
# ★ FEATURE TOGGLE (CLOUD) — DELIVERY-DATE / PRIORITY-FLAG COMMITTED-DELIVERY ★
#   Flip this ONE line to turn the committed-delivery feature ON/OFF for the CLOUD
#   path (it is pinned into CLOUD_CONFIG below). ON is INERT until jkt_demand carries
#   `priorityFlag` / `deliveryDate` columns AND connection.read_db selects them, so
#   today it is safely ON with no effect on cloud output. The LOCAL toggle is the
#   matching ★ block at the top of bc_config.py.
# ══════════════════════════════════════════════════════════════════════════
DELIVERY_PRIORITY = True

# ══════════════════════════════════════════════════════════════════════════
# CLOUD CONFIG PIN — production values frozen for the cloud path.
#
# These are written onto bc_config BEFORE the engine imports them, so editing
# bc_config.py locally (e.g. for a local_main.py experiment) does NOT change a
# cloud run. To change a CLOUD value, edit it HERE — this dict is the cloud
# source of truth for these knobs.
#
# NOT pinned here (they come per-run from the DB via read_db / _apply_run_cfg):
#   PLAN_START, PLANNING_DAYS  ← planStartDate / planEndDate
#   MAX_CHANGEOVERS_PER_DAY    ← noOfChangeOver
#   PRESS_EFFICIENCY           ← efficiency
#   DEMAND_FILE                ← jkt_demand
# ══════════════════════════════════════════════════════════════════════════
import bc_config as _bc

CLOUD_CONFIG: dict = {
    # ── Day-0 curing press state — MUST stay the live snapshot on cloud ──
    "RUNNING_MOULDS_TABLE":                   "Daily_Running_Moulds",
    # ── GT storage / shelf life ─────────────────────────────────────────
    # MUST equal bc_config.MAX_ENDOFDAY_GT_INVENTORY (the dynamic GT buffer fills to this
    # cap, so any drift changes the plan) → keep in sync for local↔cloud parity. Committed
    # local value is 8000; if the local cap changes, update this pin too.
    "MAX_ENDOFDAY_GT_INVENTORY":              8000,
    "GT_SHELF_LIFE_DAYS":                     3,
    "TOPUP_LOOKAHEAD_DAYS_GT":                3,
    "CARCASS_SHELF_LIFE_DAYS":                1,
    # ── Stage-1 carcass CO + gate + overnight carcass cap (ADOPTED this cycle) ──
    # Stage-2 GT is clamped to feasible Stage-1 carcass (invariant #3); Stage-1 machines
    # charge real building CO (no production during CO); carcass shown = exactly Stage-2
    # consumption; the overnight carcass buffer is hard-capped at 2000 (a plant storage
    # limit). Verified July/Aug/June (mould-audit PASS, carcass=GT, EOD ≤2000 / 0 over).
    "STAGE2_CARCASS_GATE_ENABLED":            True,
    "STAGE1_CO_ENABLED":                      True,
    "MAX_ENDOFDAY_CARCASS_INVENTORY":         1200,   # MUST equal bc_config (hard limit)
    "GT_BUFFER_SHIFTS":                       2,
    "GT_BUFFER_SHIFTS_VMI":                   2,      # split buffer (VMI banks 2 shifts)
    "GT_BUFFER_SHIFTS_OTHER":                 1,      # BJ/UNI/STAGE bank 1
    # ── Mould clean (curing press) ──────────────────────────────────────
    "MOULD_CLEAN_CYCLES":                     3000,
    "MOULD_CLEAN_MINS":                       480,
    # ── Building campaign / CO controls ─────────────────────────────────
    "MIN_CAMPAIGN_MINS":                      60,
    "MIN_CAMPAIGN_UNITS":                     40,
    "MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT": 2,
    "OVERBUILD_BUFFER_FRAC":                  0.2,
    "POOL_SIZE":                              3,
    "STARVATION_BUFFER_MINS":                 30,
    "STAGE2_CO_TIME_MULTIPLIER":              2.0,
    "BUILD_LEAD_SHIFTS":                      3,
    # ── Building inch rules + SKU cap (this cycle) ──────────────────────
    "MIN_INCH_DWELL_DAYS":                    5,      # 5-day min inch dwell (INCH_RULES)
    "MAX_BUILDING_SKUS_PER_DAY":              4,      # 4 distinct SKUs/machine/day (BLD_SKU_CAP)
    "INCH_PLUS3_CO_MINS":                     480,    # +3/−3 escape (toggle OFF by default)
    "INCH_PLUS3_MIN_DAYS_LEFT":               5,
    # ── Historical inch-LOCK (ADOPTED this cycle) — supersedes the ±2 anchor band ──
    # Per-machine allowed-inch SETS from the 4-month plant report: 27 machines FIXED to
    # one inch (0 diff-CO), 12 FLEXIBLE to their ranked historical inches. Applied on
    # cloud too (the Apr–Jul plant history is a fixed reference for every month).
    "INCH_HIST_LOCK_ENABLED":                 True,
    "INCH_HIST_LOCK_MIN_SHARE":               0.02,   # reproduces the 27-fixed/12-flexible split
    "INCH_HIST_LOCK_MAX_INCHES":              3,
    "INCH_HIST_LOCK_STAGE1":                  False,  # S1 stays demand-optimal (carcass-FEASIBLE)
    "FIXED_ESCAPE_ENABLED":                   False,  # Lever B REJECTED (−2,572 net, 3 mo) — kept off
    # ── Delivery-date / priority-flag committed-delivery SKUs (top ★ toggle) ──
    # INERT on cloud until jkt_demand carries priorityFlag/deliveryDate + read_db selects
    # them. Sub-levers (DP_ACQUIRE/DP_RESERVE/DP_MOULDCAP/DP_PACE_MARGIN/DP_BLD) are
    # module-level env defaults in the engine (adopted "full mould-cap") — see the NOTE below.
    "DELIVERY_PRIORITY_ENABLED":              DELIVERY_PRIORITY,
    "DELIVERY_PRIORITY_UNDATED_TO_MONTHEND":  True,
    # ── Curing CO controls ──────────────────────────────────────────────
    "CO_CLASS_B_THRESHOLD":                   0.8,
    "CURING_CO_CHANGEOVER_MINS":              480,    # match bc_config (parity)
    "CURING_CO_DURATION_SHIFTS":              1,
    # ── Curing capacity-utilisation KPI denominator (fixed plant roster) ──
    "CURING_PRESS_COUNT":                     170,    # daily+monthly curing util denominator
}
# NOTE: the b2c_pipeline behaviour toggles (IUkeep `_IDLE_UNMET_ENABLED`/`_IDLE_UNMET_KEEP_GATE`,
# mould gate, CO scorer, inch rules, mould-clean, mould-life-v2 — all pinned ON in the engine
# module; +3/−3 escape and the global mould optimiser — env-gated default OFF) are MODULE-LEVEL
# in b2c_pipeline.py, so the cloud path inherits them automatically by importing the engine.
# They are NOT bc_config attributes and cannot be pinned here; keep them in sync in the engine.

for _k, _v in CLOUD_CONFIG.items():
    setattr(_bc, _k, _v)

# Import the engine ONLY AFTER the pin above, so every `from bc_config import X`
# inside the engine binds to the pinned cloud value (not whatever is in the file).
import b2c_pipeline
import curing_consumption
import cbc_env
from b2c_pipeline import run_rolling_pipeline
import connection as conn

# Where the per-run building/curing workbooks are written and KEPT so a user can
# download them. Identical format to the local run (same Excel writers).
# In Docker set PLAN_OUTPUT_DIR=/app/output and mount a volume there.
PLAN_OUTPUT_DIR = os.environ.get(
    "PLAN_OUTPUT_DIR", os.path.join(cbc_env.OUTPUT_DIR, "main_output")
)


def _apply_run_cfg(run_cfg: dict) -> None:
    """Override the per-run engine knobs from DB params (v1: max_co + efficiency).

    plan_start / planning_days / demand are passed as call arguments, not
    globals. mouldAvailability and the priority weightages are v1-dormant.
    """
    b2c_pipeline.MAX_CHANGEOVERS_PER_DAY = int(run_cfg["max_co_per_day"])
    curing_consumption.ConsumptionConfig.PRESS_EFFICIENCY = float(run_cfg["press_efficiency"])
    # Plant holidays (jkt_holiday_calendar) → the engine reads bc_config.PLANT_HOLIDAYS at
    # runtime; empty list = holiday-free run (bit-for-bit identical to no-holiday).
    _hols = list(run_cfg.get("holidays", []) or [])
    setattr(_bc, "PLANT_HOLIDAYS", _hols)
    os.environ["PLANT_HOLIDAYS"] = ",".join(_hols)


def _set_running_moulds_table(name: str) -> None:
    """Point the Day-0 running-moulds table for this run.

    RUNNING_MOULDS_TABLE is imported by value into several engine modules, so it
    must be set on each. Production leaves this at the pinned Daily_Running_Moulds;
    the override exists for month-specific snapshots (e.g. parity testing).
    """
    setattr(_bc, "RUNNING_MOULDS_TABLE", name)
    for _m in (b2c_pipeline, curing_consumption):
        if hasattr(_m, "RUNNING_MOULDS_TABLE"):
            setattr(_m, "RUNNING_MOULDS_TABLE", name)
    try:
        import curing_b2c as _cb2c
        setattr(_cb2c, "RUNNING_MOULDS_TABLE", name)
    except Exception:  # pragma: no cover
        pass
    try:
        import curing_consumption_dynamic as _ccd
        if hasattr(_ccd, "RUNNING_MOULDS_TABLE"):
            setattr(_ccd, "RUNNING_MOULDS_TABLE", name)
    except Exception:  # pragma: no cover
        pass


def _set_plan_month(plan_start) -> None:
    """Point plan_month (running-moulds + opening GT/carcass SQL filters) at THIS run's
    month, derived from the run's plan_start.

    PLAN_MONTH / RUNNING_MOULDS_MONTH are imported BY VALUE into several engine modules
    at import time (they key the `WHERE plan_month = …` queries), and on cloud they are
    otherwise fixed to bc_config's file-default month. Without this, a cloud run for any
    month != that default would read the WRONG month's Day-0 snapshot + opening GT.
    Mirrors _set_running_moulds_table: set on bc_config and on every engine module that
    binds the value (both already-imported and lazily-imported later).
    """
    pm = plan_start.strftime("%Y-%m")
    os.environ["PLAN_MONTH"] = pm
    os.environ["RUNNING_MOULDS_MONTH"] = pm
    setattr(_bc, "PLAN_MONTH", pm)
    setattr(_bc, "RUNNING_MOULDS_MONTH", pm)
    # bc_config.PLAN_START is read directly by the holiday helpers (day-index anchor), so it
    # must reflect THIS run's start, not bc_config's file default.
    setattr(_bc, "PLAN_START", plan_start)
    for _modname in ("curing_consumption", "curing_b2c", "curing_consumption_dynamic",
                     "building", "building_b2c", "b2c_pipeline"):
        try:
            _m = __import__(_modname)
        except Exception:  # pragma: no cover
            continue
        for _attr in ("PLAN_MONTH", "RUNNING_MOULDS_MONTH"):
            if hasattr(_m, _attr):
                setattr(_m, _attr, pm)


def run_plan(plan_id: str, created_by: str = "scheduler",
             keep_files: bool = False,
             running_moulds_table: str | None = None) -> dict:
    """Execute one scheduler run for plan_id and populate the output tables.

    running_moulds_table overrides the Day-0 snapshot for this run (default =
    the pinned cloud table). Used for month-specific parity testing.
    """
    engine = conn.get_engine()
    _set_running_moulds_table(running_moulds_table or _bc.RUNNING_MOULDS_TABLE)

    # ── read inputs from the 3 input tables ───────────────────────────────
    demand_df, run_cfg, sku_desc = conn.read_db(engine, plan_id)
    # Align plan_month to the run's actual month BEFORE the engine ETL reads Day-0 data.
    _set_plan_month(run_cfg["plan_start"])
    print(f"[main] plan_id={plan_id}  SKUs={len(demand_df)}  "
          f"start={run_cfg['plan_start'].date()}  days={run_cfg['planning_days']}  "
          f"max_co={run_cfg['max_co_per_day']}  eff={run_cfg['press_efficiency']}")
    print(f"[main] cloud config pinned ({len(CLOUD_CONFIG)}): "
          + ", ".join(f"{k}={v}" for k, v in CLOUD_CONFIG.items()))

    # ── stage demand to a temp workbook (engine reads Excel) ──────────────
    workdir = tempfile.mkdtemp(prefix=f"plan_{plan_id}_")
    demand_path = os.path.join(workdir, "demand.xlsx")
    demand_df.to_excel(demand_path, index=False)

    # ── output workbooks are KEPT (downloadable), stamped with plan_id ────
    ds = run_cfg["plan_start"].date()
    os.makedirs(PLAN_OUTPUT_DIR, exist_ok=True)
    build_out = os.path.join(PLAN_OUTPUT_DIR, f"bc_building_schedule_{plan_id}_{ds}.xlsx")
    curing_out = os.path.join(PLAN_OUTPUT_DIR, f"bc_curing_b2c_{plan_id}_{ds}.xlsx")

    # ── inject per-run config + run the engine ────────────────────────────
    _apply_run_cfg(run_cfg)
    result = run_rolling_pipeline(
        demand_path=demand_path,
        plan_start=run_cfg["plan_start"],
        planning_days=run_cfg["planning_days"],
        build_output=build_out,
        curing_output=curing_out,
        sku_desc_map=sku_desc,          # DB master descriptions → output sheets
        # CLOUD↔LOCAL PARITY + feasibility: restrict curing to the 170 allowable presses,
        # exactly like local_main.py. Without this, cloud would cure on running-moulds
        # presses that are NOT in the allowable matrix (e.g. 85214/85215) — a feasibility
        # violation and a source of local↔cloud divergence.
        restrict_to_allowable_presses=getattr(_bc, "RESTRICT_PRESSES_TO_ALLOWABLE", True),
    )

    # ── write the 4 output tables (rules 4 & 5 applied inside write_db) ────
    counts = conn.write_db(
        engine, plan_id, result,
        result["build_output"], result["curing_output"],
        sku_desc=sku_desc,
        plant_name=run_cfg.get("plant_name"),
        product_name=run_cfg.get("product_name"),
        created_by=created_by,
    )

    # only the demand-staging temp dir is discarded; the workbooks are kept
    if not keep_files:
        shutil.rmtree(workdir, ignore_errors=True)

    summary = {
        "plan_id": plan_id,
        "status": "done",
        "rows_written": counts,
        "outputs": {
            "building": result["build_output"],
            "curing":   result["curing_output"],
        },
        "kpi": {
            "gt_built":     round(result["total_built"]),
            "gt_cured":     round(result["total_cured"]),
            "coverage":     round(result["demand_coverage"], 2),
            "curing_cos":   result["n_co"],
            "mould_cleans": result["n_mould_cleans"],
            "gt_writeoff":  round(result["gt_writeoff"]),
            "starvation":   result["starvation_events"],
        },
    }
    return summary


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python main.py <plan_id>")
        sys.exit(1)
    pid = sys.argv[1]
    try:
        out = run_plan(pid)
        print("\n[main] DONE:", out)
    except Exception as e:  # noqa: BLE001 — surface run failure for the caller
        print(f"\n[main] FAILED for plan_id={pid}: {e}")
        traceback.print_exc()
        sys.exit(2)
