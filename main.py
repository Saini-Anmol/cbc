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

import b2c_pipeline
import curing_consumption
from b2c_pipeline import run_rolling_pipeline
import connection as conn


def _apply_run_cfg(run_cfg: dict) -> None:
    """Override the per-run engine knobs from DB params (v1: max_co + efficiency).

    plan_start / planning_days / demand are passed as call arguments, not
    globals. mouldAvailability and the priority weightages are v1-dormant.
    """
    b2c_pipeline.MAX_CHANGEOVERS_PER_DAY = int(run_cfg["max_co_per_day"])
    curing_consumption.ConsumptionConfig.PRESS_EFFICIENCY = float(run_cfg["press_efficiency"])


def run_plan(plan_id: str, created_by: str = "scheduler",
             keep_files: bool = False) -> dict:
    """Execute one scheduler run for plan_id and populate the output tables."""
    engine = conn.get_engine()

    # ── read inputs from the 3 input tables ───────────────────────────────
    demand_df, run_cfg, sku_desc = conn.read_db(engine, plan_id)
    print(f"[main] plan_id={plan_id}  SKUs={len(demand_df)}  "
          f"start={run_cfg['plan_start'].date()}  days={run_cfg['planning_days']}  "
          f"max_co={run_cfg['max_co_per_day']}  eff={run_cfg['press_efficiency']}")

    # ── stage demand to a temp workbook (engine reads Excel) ──────────────
    workdir = tempfile.mkdtemp(prefix=f"plan_{plan_id}_")
    demand_path = os.path.join(workdir, "demand.xlsx")
    demand_df.to_excel(demand_path, index=False)
    ds = run_cfg["plan_start"].date()
    build_out = os.path.join(workdir, f"bc_building_schedule_{ds}.xlsx")
    curing_out = os.path.join(workdir, f"bc_curing_b2c_{ds}.xlsx")

    # ── inject per-run config + run the engine ────────────────────────────
    _apply_run_cfg(run_cfg)
    result = run_rolling_pipeline(
        demand_path=demand_path,
        plan_start=run_cfg["plan_start"],
        planning_days=run_cfg["planning_days"],
        build_output=build_out,
        curing_output=curing_out,
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

    if not keep_files:
        shutil.rmtree(workdir, ignore_errors=True)

    summary = {
        "plan_id": plan_id,
        "status": "done",
        "rows_written": counts,
        "kpi": {
            "gt_built":   round(result["total_built"]),
            "gt_cured":   round(result["total_cured"]),
            "coverage":   round(result["demand_coverage"], 1),
            "curing_cos": result["n_co"],
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
