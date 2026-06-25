"""
B2C Pipeline — Phase 1: Building Scheduler (B2C mode)
======================================================
Schedules building machines driven by curing CONSUMPTION rather than a curing LP plan.

Key differences from CBC building.py:
  • Input:   curing_consumption_table.xlsx instead of a curing LP schedule
  • Demand:  derived from active press counts × qty_per_press_per_shift
  • GT inv:  opened from DB (not zeroed — real opening inventory used)
  • Start:   1 shift before curing starts (not 1 full day)
  • Cap:     building output ≤ consumption per shift (strict, OVERBUILD_BUFFER_FRAC=0.0)
  • Phasing: Runner-In scheduled first; Non-Runner-In + Runner-Out via joint priority pool

Reuses from building.py (imported):
  Config, ETL, LPMinuteSolver, GeneticOptimiser, CampaignSequencer,
  ScheduleBuilder, StarvationValidator, ExcelExporter, HybridDailyScheduler,
  build_summary, build_util, build_daywise_report, build_daily_sku_counts

Standalone usage:
    python building_b2c.py

Output: data/output/main_output/bc_building_schedule.xlsx
"""

from __future__ import annotations

import math
import os
import sys
import warnings
from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ── venv re-exec ──────────────────────────────────────────────────────────────
_VENV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "myenv")
_VENV_PY  = os.path.join(_VENV_DIR, "bin", "python")
if (os.path.exists(_VENV_PY)
        and os.path.realpath(sys.prefix) != os.path.realpath(_VENV_DIR)
        and not os.environ.get("BC_REEXEC")):
    os.environ["BC_REEXEC"] = "1"
    os.execv(_VENV_PY, [_VENV_PY, os.path.abspath(__file__)] + sys.argv[1:])

# ── Import reusable machinery from existing building.py ───────────────────────
import building as _bld
from building import (
    Config,
    ETL,
    LPMinuteSolver,
    GeneticOptimiser,
    CampaignSequencer,
    ScheduleBuilder,
    StarvationValidator,
    ExcelExporter,
    HybridDailyScheduler,
    build_summary,
    build_util,
    build_daywise_report,
    build_daily_sku_counts,
    _shift_fn,
    _shift_start,
)

import cbc_env

HERE    = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = cbc_env.OUTPUT_DIR
MAIN_OUT = os.path.join(OUT_DIR, "main_output")
os.makedirs(MAIN_OUT, exist_ok=True)

# ── B2C-specific config overrides ────────────────────────────────────────────
# Applied at module level so that all building.py classes that reference
# Config.X pick up the B2C values when called through this module.
Config.MAX_CHANGEOVERS_PER_DAY = 8   # new plant-wide daily cap
Config.OVERBUILD_BUFFER_FRAC   = 0.0 # strict: build exactly what curing consumes
# Remove CURING_PLAN_FILE — not used in B2C (we use consumption table)
Config.CURING_PLAN_FILE = None

# ══════════════════════════════════════════════════════════════════════════════
# B2C ETL  (extends building ETL with consumption table reader)
# ══════════════════════════════════════════════════════════════════════════════

class B2C_ETL(ETL):
    """B2C variant of the building ETL — replaces load_curing_schedule()."""

    def load_consumption_table(self, path: str) -> pd.DataFrame:
        """
        Load the curing consumption table produced by curing_consumption.py.
        Returns DataFrame: [SKUCode, Category, Running_Press_Count,
                            Effective_CT_Min, Qty_Per_Press_Per_Shift,
                            Total_GT_Per_Shift_Day0, Demand_Qty, Priority_Score]
        """
        xl = pd.ExcelFile(path)
        sheet = "Consumption Summary" if "Consumption Summary" in xl.sheet_names else xl.sheet_names[0]
        # Try multiple header rows in case a legend row is present
        for hdr in (0, 1, 2):
            try:
                df = pd.read_excel(path, sheet_name=sheet, header=hdr)
                if "SKUCode" in df.columns and "Category" in df.columns:
                    break
            except Exception:
                continue
        df["SKUCode"] = df["SKUCode"].astype(str).str.strip()
        # Drop legend/summary rows that the Excel reader picks up as data
        df = df[df["SKUCode"].notna() & (df["SKUCode"] != "") & (df["SKUCode"] != "nan")]
        _valid_cats = {"Runner-In", "Runner-Out", "Non-Runner-In"}
        if "Category" in df.columns:
            df = df[df["Category"].isin(_valid_cats)]
        df = df.reset_index(drop=True)
        print(f"  [B2C ETL] Consumption table: {len(df)} rows from {os.path.basename(path)}")
        return df

    def load_gt_inventory_for_b2c(self) -> pd.DataFrame:
        """Load REAL opening GT inventory (not zeroed out as in CBC cold-start)."""
        return self._sql(
            f"SELECT sizeCode AS SKUCode, gtInventory AS GT_Inventory "
            f"FROM {Config.DB_NAME}.gt_inventory_manual"
        )


# ══════════════════════════════════════════════════════════════════════════════
# B2C DEMAND DERIVER
# ══════════════════════════════════════════════════════════════════════════════

class B2C_DemandDeriver:
    """
    Converts the curing consumption table into the per-(SKU, shift) demand matrix
    that HybridDailyScheduler/LPMinuteSolver expects.

    Runner-In:      curing_matrix[s, t] = Total_GT_Per_Shift_Day0[s]  (constant demand)
    Non-Runner-In:  curing_matrix[s, t] = 0 initially (no press yet; demand from file)
    Runner-Out:     excluded (changeover target planning handled separately)
    """

    def derive_from_consumption(
        self,
        df_consumption: pd.DataFrame,
        df_gt_inv: pd.DataFrame,
        plan_start: datetime,
        planning_days: int = 1,
    ) -> tuple:
        """
        Returns the same tuple as building.DemandDeriver.derive():
          (df_sku_demand, curing_matrix, all_skus, df_shift_demand)
        """
        T = planning_days * Config.SHIFTS_PER_DAY
        gt_inv_map = dict(zip(
            df_gt_inv["SKUCode"].astype(str).str.strip(),
            df_gt_inv["GT_Inventory"].astype(float)
        ))

        shift_starts = [
            _shift_start(plan_start, t) for t in range(T)
        ]
        plan_end = plan_start + timedelta(days=planning_days)

        # Build per-(SKU, shift) demand
        sku_shift_demand: dict[tuple, float] = {}

        for _, row in df_consumption.iterrows():
            sku      = str(row["SKUCode"]).strip()
            cat      = str(row.get("Category", "")).strip()
            qty_per  = float(row.get("Total_GT_Per_Shift_Day0", 0))
            dem_qty  = float(row.get("Demand_Qty", 0))

            if cat == "Runner-In":
                for t in range(T):
                    sku_shift_demand[(sku, t)] = qty_per
            elif cat == "Non-Runner-In" and dem_qty > 0:
                # Spread total demand evenly across shifts
                per_shift = dem_qty / (Config.PLANNING_DAYS * Config.SHIFTS_PER_DAY)
                for t in range(T):
                    sku_shift_demand[(sku, t)] = per_shift
            # Runner-Out: excluded from building demand (they'll changeover)

        if not sku_shift_demand:
            # Fallback: empty — shouldn't happen in practice
            return (
                pd.DataFrame(columns=["SKUCode", "GT_Demand", "GT_Inventory",
                                      "Net_GT_Demand", "LP_Demand",
                                      "First_Curing_Start", "Burn_Rate_Per_Shift",
                                      "Active_Shifts"]),
                np.zeros((0, T)), [], pd.DataFrame()
            )

        all_skus = sorted(set(k[0] for k in sku_shift_demand))
        S = len(all_skus)
        sku_idx = {s: i for i, s in enumerate(all_skus)}
        curing_matrix = np.zeros((S, T))
        for (sku, t), qty in sku_shift_demand.items():
            if sku in sku_idx:
                curing_matrix[sku_idx[sku], t] = qty

        total_gt = curing_matrix.sum(axis=1)
        rows = []
        for si, sku in enumerate(all_skus):
            gt_inv   = gt_inv_map.get(sku, 0.0)
            net_dem  = max(0.0, total_gt[si] - gt_inv)
            first_t  = next((t for t in range(T) if curing_matrix[si, t] > 0), T)
            first_start = shift_starts[first_t] if first_t < T else plan_end
            rows.append({
                "SKUCode":             sku,
                "GT_Demand":           int(total_gt[si]),
                "GT_Inventory":        int(gt_inv),
                "Net_GT_Demand":       int(net_dem),
                "LP_Demand":           int(net_dem),
                "First_Curing_Start":  first_start,
                "Burn_Rate_Per_Shift": round(total_gt[si] / max(T, 1), 1),
                "Active_Shifts":       int((curing_matrix[si] > 0).sum()),
            })

        df_sku_demand = pd.DataFrame(rows).sort_values("First_Curing_Start")

        sd_rows = []
        for (sku, t), qty in sku_shift_demand.items():
            if qty > 0:
                s_start = shift_starts[t]
                shift_lbl, _ = _shift_fn(s_start)
                sd_rows.append({
                    "Date": s_start.date(), "Shift": shift_lbl,
                    "SKUCode": sku, "Curing_Qty": int(qty),
                })
        df_shift_demand = pd.DataFrame(sd_rows)

        print(f"  [B2C Demand] SKUs: {S} | Shifts: {T} | "
              f"Total consumption: {total_gt.sum():,.0f}")
        return df_sku_demand, curing_matrix, all_skus, df_shift_demand


# ══════════════════════════════════════════════════════════════════════════════
# SYNTHETIC CURING SCHEDULE BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _make_synthetic_curing(
    df_consumption: pd.DataFrame,
    plan_start: datetime,
    planning_days: int,
) -> pd.DataFrame:
    """
    Convert the consumption table into a df_curing-compatible DataFrame so
    the existing HybridDailyScheduler can consume it without modification.

    Produces one row per (SKU, shift) for the full planning horizon.
    """
    rows = []
    shift_hours = [
        Config.SHIFT_START_HOUR,
        Config.SHIFT_START_HOUR + Config.HOURS_PER_SHIFT,
        Config.SHIFT_START_HOUR + Config.HOURS_PER_SHIFT * 2,
    ]

    for _, row in df_consumption.iterrows():
        sku      = str(row["SKUCode"]).strip()
        cat      = str(row.get("Category", "")).strip()
        qty_ps   = float(row.get("Total_GT_Per_Shift_Day0", 0))
        dem_qty  = float(row.get("Demand_Qty", 0))

        if cat == "Runner-In":
            target_qty = qty_ps
        elif cat == "Non-Runner-In" and dem_qty > 0:
            # Spread total demand evenly so the LP sees steady need
            target_qty = dem_qty / (planning_days * Config.SHIFTS_PER_DAY)
        else:
            continue  # Runner-Out: no building demand

        for day_offset in range(planning_days):
            base_date = plan_start + timedelta(days=day_offset)
            for sh_h in shift_hours:
                sh_start = datetime(
                    base_date.year, base_date.month, base_date.day, sh_h % 24, 0, 0
                )
                if sh_h >= 24:
                    sh_start += timedelta(days=1)
                sh_end = sh_start + timedelta(hours=Config.HOURS_PER_SHIFT)
                rows.append({
                    "SKUCode":   sku,
                    "StartTime": sh_start,
                    "EndTime":   sh_end,
                    "Qty":       target_qty,
                })

    if not rows:
        return pd.DataFrame(columns=["SKUCode", "StartTime", "EndTime", "Qty"])
    return pd.DataFrame(rows)


# ══════════════════════════════════════════════════════════════════════════════
# CHANGEOVER SCHEDULER  (Phase 2b — joint priority pool assignments)
# ══════════════════════════════════════════════════════════════════════════════

class ChangeoverScheduler:
    """
    Assigns changeover days from the joint priority pool:
      1. Combine Non-Runner-In SKUs + Runner-Out press candidates.
      2. Sort by Priority_Score DESC, then MouldLife ASC.
      3. Greedy assign to days respecting MAX_CHANGEOVERS_PER_DAY.

    CO_Day Shift A = CHANGEOVER (300 min)
    CO_Day Shift B = MOULD_CLEAN (120 min)
    CO_Day Shift C = new SKU production begins
    """

    def schedule(
        self,
        df_consumption: pd.DataFrame,
        df_running_moulds_curing,   # from curing_consumption.py ETL
        plan_start: datetime,
        planning_days: int,
        max_co_per_day: int = 8,
    ) -> pd.DataFrame:
        """
        Returns df_co_plan: [Press, Old_SKU, Target_SKU, CO_Day_Index, Status]
        CO_Day_Index is 0-based day offset from plan_start.
        """
        # NRI SKUs: need a curing press assignment
        nri_skus = df_consumption[df_consumption["Category"] == "Non-Runner-In"].copy()
        nri_skus = nri_skus.sort_values("Priority_Score", ascending=False)

        # RO presses: candidate for changeover to a high-priority NRI target
        ro_skus  = df_consumption[df_consumption["Category"] == "Runner-Out"].copy()

        # Build a candidate list: (priority, press_or_sku, old_sku, target_sku, mould_life)
        candidates = []

        # Simple pairing: assign highest priority NRI SKU to each available RO press
        ro_presses = {}
        if df_running_moulds_curing is not None and len(df_running_moulds_curing) > 0:
            ro_press_df = df_running_moulds_curing[
                df_running_moulds_curing["SKUCode"].isin(ro_skus["SKUCode"].tolist())
            ]
            for _, pr in ro_press_df.iterrows():
                ro_presses[str(pr["Machine"])] = {
                    "old_sku": str(pr["SKUCode"]),
                    "mould_life": int(pr["MouldLife_remaining"]),
                }

        nri_queue = list(nri_skus["SKUCode"])
        for press, info in ro_presses.items():
            if not nri_queue:
                break
            target = nri_queue.pop(0)
            pri = float(
                nri_skus.loc[nri_skus["SKUCode"] == target, "Priority_Score"]
                .values[0] if len(nri_skus.loc[nri_skus["SKUCode"] == target]) > 0 else 0
            )
            candidates.append({
                "Press":          press,
                "Old_SKU":        info["old_sku"],
                "Target_SKU":     target,
                "Priority_Score": pri,
                "MouldLife_min":  info["mould_life"],
            })

        if not candidates:
            return pd.DataFrame(columns=["Press", "Old_SKU", "Target_SKU",
                                         "CO_Day_Index", "Status"])

        # Sort by priority DESC, then mould life ASC (near-expiry goes first)
        cand_df = pd.DataFrame(candidates).sort_values(
            ["Priority_Score", "MouldLife_min"],
            ascending=[False, True],
        )

        co_per_day: dict[int, int] = defaultdict(int)
        plan_rows = []
        for _, c in cand_df.iterrows():
            assigned = False
            for day in range(planning_days):
                if co_per_day[day] < max_co_per_day:
                    co_per_day[day] += 1
                    plan_rows.append({
                        "Press":        c["Press"],
                        "Old_SKU":      c["Old_SKU"],
                        "Target_SKU":   c["Target_SKU"],
                        "CO_Day_Index": day,
                        "Status":       "SCHEDULED",
                    })
                    assigned = True
                    break
            if not assigned:
                plan_rows.append({
                    "Press":        c["Press"],
                    "Old_SKU":      c["Old_SKU"],
                    "Target_SKU":   c["Target_SKU"],
                    "CO_Day_Index": -1,
                    "Status":       "DEFERRED",
                })

        df_co_plan = pd.DataFrame(plan_rows)
        sched = (df_co_plan["Status"] == "SCHEDULED").sum()
        deferred = (df_co_plan["Status"] == "DEFERRED").sum()
        print(f"  [CO Scheduler] {sched} changeovers scheduled | {deferred} deferred")
        return df_co_plan


# ══════════════════════════════════════════════════════════════════════════════
# DYNAMIC TARGET LOCK  (Phase 3 — per-shift building cap from CO plan)
# ══════════════════════════════════════════════════════════════════════════════

class DynamicTargetLock:
    """
    Updates active_press_count(SKU, shift_idx) based on the changeover plan.
    Returns a dict {(SKUCode, shift_idx): building_target_units}.
    """

    def lock(
        self,
        df_consumption: pd.DataFrame,
        df_co_plan: pd.DataFrame,
        plan_start: datetime,
        planning_days: int,
    ) -> dict:
        """Returns {(SKUCode, shift_idx): target_units}."""
        T = planning_days * Config.SHIFTS_PER_DAY

        # Initial active press count from consumption table
        press_count: dict[str, list[int]] = {}
        qty_per_press: dict[str, float]   = {}
        for _, row in df_consumption.iterrows():
            sku     = str(row["SKUCode"]).strip()
            pc_val  = row.get("Running_Press_Count", 0)
            pc      = 0 if (pc_val is None or (isinstance(pc_val, float) and math.isnan(pc_val))) else int(pc_val)
            qpp_val = row.get("Qty_Per_Press_Per_Shift", 0)
            qpp     = 0.0 if (qpp_val is None or (isinstance(qpp_val, float) and math.isnan(qpp_val))) else float(qpp_val)
            press_count[sku] = [pc] * T
            qty_per_press[sku] = qpp

        # Apply changeover events
        if df_co_plan is not None and len(df_co_plan) > 0:
            for _, co in df_co_plan.iterrows():
                if co["Status"] != "SCHEDULED":
                    continue
                day_idx     = int(co["CO_Day_Index"])
                target_sku  = str(co["Target_SKU"])
                old_sku     = str(co["Old_SKU"])
                # CO day shift A = 0 (blocked), shift B = 1 (blocked)
                # Production with new SKU starts shift C = day*3+2
                first_prod_shift = day_idx * Config.SHIFTS_PER_DAY + 2

                # Old SKU loses one press from CO shift A onward
                old_first_blocked = day_idx * Config.SHIFTS_PER_DAY
                if old_sku in press_count:
                    for t in range(old_first_blocked, T):
                        press_count[old_sku][t] = max(0, press_count[old_sku][t] - 1)

                # Target SKU gains one press from production shift onward
                if target_sku not in press_count:
                    press_count[target_sku] = [0] * T
                    qty_per_press[target_sku] = 0.0
                    # Try to look up from consumption table
                    row_match = df_consumption[df_consumption["SKUCode"] == target_sku]
                    if len(row_match) > 0:
                        qty_per_press[target_sku] = float(
                            row_match.iloc[0].get("Qty_Per_Press_Per_Shift", 0)
                        )
                for t in range(first_prod_shift, T):
                    press_count[target_sku][t] += 1

        # Build target dict
        targets = {}
        for sku, counts in press_count.items():
            qpp = qty_per_press.get(sku, 0.0)
            for t, cnt in enumerate(counts):
                targets[(sku, t)] = int(cnt * qpp)

        return targets


# ══════════════════════════════════════════════════════════════════════════════
# B2C ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run_from_database_b2c(
    plan_start: datetime | None = None,
    consumption_path: str | None = None,
    output_path: str | None = None,
    engine=None,
    planning_days: int | None = None,
) -> dict:
    """
    Run the B2C building scheduler.

    Args:
        plan_start:       First curing shift (building starts 1 shift earlier).
        consumption_path: Path to curing_consumption_table.xlsx from Phase 0.
        output_path:      Where to write bc_building_schedule.xlsx.
        engine:           SQLAlchemy engine (created from .env if None).
        planning_days:    Override Config.PLANNING_DAYS if provided.

    Returns dict with same keys as building.run_from_database_hybrid().
    """
    from cbc_env import make_engine as _mk

    if plan_start is None:
        plan_start = datetime(2026, 6, 1, 7, 0, 0)
    if output_path is None:
        output_path = os.path.join(
            MAIN_OUT,
            f"bc_building_schedule_{plan_start.date()}.xlsx",
        )
    if consumption_path is None:
        consumption_path = os.path.join(cbc_env.OUTPUT_DIR, "curing_consumption_table.xlsx")
    if engine is None:
        engine = _mk()
    if planning_days is not None:
        Config.PLANNING_DAYS = planning_days

    print("\n" + "=" * 70)
    print("  B2C Phase 1 — Building Scheduler")
    print("=" * 70)

    # ── B2C pre-start: 1 shift before first curing shift ─────────────────────
    # CBC used 1 full day (LEAD_DAYS=1); B2C uses 1 shift (8 hours).
    # plan_start = June 1 07:00 (Shift A) → build_start = May 31 23:00 (Shift C)
    build_start = plan_start - timedelta(hours=Config.HOURS_PER_SHIFT)
    print(f"  [Config] Plan start:  {plan_start}  |  Build start: {build_start}")
    print(f"  [Config] Planning days: {Config.PLANNING_DAYS}")

    # ── ETL ──────────────────────────────────────────────────────────────────
    etl = B2C_ETL(engine)

    print("\n  [ETL] Loading consumption table …")
    df_consumption = etl.load_consumption_table(consumption_path)

    print("  [ETL] Loading real GT inventory (not zeroed) …")
    df_gt_inv = etl.load_gt_inventory_for_b2c()
    print(f"        {len(df_gt_inv)} SKUs with opening GT inventory")

    print("  [ETL] Loading carcass inventory …")
    df_carcass_inv = etl.load_carcass_inventory()

    print("  [ETL] Loading building allowable machines …")
    df_allow = etl.load_machine_allowable()

    print("  [ETL] Loading changeover times …")
    co_map = etl.load_changeover_map()

    print("  [ETL] Loading SKU sizes …")
    sku_to_size = etl.load_sku_sizes()

    print("  [ETL] Loading running building machines (for continuity locks) …")
    df_running = etl.load_running_machines()

    print("  [ETL] Loading history map (GA seed) …")
    history_map = etl.load_history_map()

    print("  [ETL] Loading running curing moulds (for changeover planning) …")
    try:
        from curing_consumption import ConsumptionETL
        cetl = ConsumptionETL(engine)
        df_running_curing = cetl.load_running_moulds()
    except Exception as exc:
        print(f"  ⚠️  Could not load curing running moulds: {exc}")
        df_running_curing = None

    # ── Changeover planning ───────────────────────────────────────────────────
    print("\n  [CO Plan] Scheduling changeovers for Non-Runner-In SKUs …")
    co_scheduler = ChangeoverScheduler()
    df_co_plan = co_scheduler.schedule(
        df_consumption,
        df_running_curing,
        plan_start,
        Config.PLANNING_DAYS,
        max_co_per_day=getattr(Config, "MAX_CHANGEOVERS_PER_DAY", 8),
    )

    # ── Dynamic target lock ───────────────────────────────────────────────────
    print("  [Lock] Computing dynamic per-shift building targets …")
    target_lock = DynamicTargetLock()
    dynamic_targets = target_lock.lock(
        df_consumption, df_co_plan, plan_start, Config.PLANNING_DAYS
    )

    # ── Synthetic curing schedule ─────────────────────────────────────────────
    # Build a df_curing-compatible DataFrame that HybridDailyScheduler can use.
    # Incorporate dynamic targets: update consumption column from dynamic_targets.
    print("  [Synthetic] Building synthetic curing schedule from consumption table …")
    df_consumption_updated = df_consumption.copy()

    df_curing_synthetic = _make_synthetic_curing(
        df_consumption_updated, plan_start, Config.PLANNING_DAYS
    )
    n_skus = df_curing_synthetic["SKUCode"].nunique()
    print(f"  [Synthetic] {len(df_curing_synthetic)} rows | {n_skus} SKUs")

    # Apply the 1-shift offset: the building plan's time window starts at build_start,
    # not plan_start. Shift the synthetic curing times back by 1 shift so day 0
    # of building covers the pre-start shift (May 31 Shift C → June 1 Shift A).
    # The existing HybridDailyScheduler loops from build_start to build_start+N_DAYS.
    df_curing_synthetic["StartTime"] = df_curing_synthetic["StartTime"] - timedelta(
        hours=Config.HOURS_PER_SHIFT
    )
    df_curing_synthetic["EndTime"] = df_curing_synthetic["EndTime"] - timedelta(
        hours=Config.HOURS_PER_SHIFT
    )

    # ── Run the existing HybridDailyScheduler ────────────────────────────────
    # Pass real GT/carcass inventory (NOT zeroed — B2C uses opening inventory).
    # Pass real running machine state (continuity locks active on Day 0).
    print("\n  [Scheduler] Launching HybridDailyScheduler in B2C mode …")
    scheduler = HybridDailyScheduler()
    results = scheduler.run(
        df_curing_synthetic,
        df_gt_inv,          # REAL GT inventory (CBC zeroed this out)
        df_carcass_inv,     # REAL carcass inventory
        df_allow,
        co_map,
        sku_to_size,
        df_running,         # REAL running machine state
        build_start,        # 1 shift before plan_start
        history_map=history_map,
    )

    # ── Attach B2C-specific data to results ───────────────────────────────────
    results["consumption_table"]   = df_consumption
    results["co_plan"]             = df_co_plan
    results["dynamic_targets"]     = pd.DataFrame(
        [(k[0], k[1], v) for k, v in dynamic_targets.items()],
        columns=["SKUCode", "Shift_Idx", "Building_Target"],
    )

    # ── Export ────────────────────────────────────────────────────────────────
    print(f"\n  [Export] Writing → {output_path}")
    exporter = ExcelExporter(output_path)
    exporter.export(results)

    # ── Append B2C-specific sheets ────────────────────────────────────────────
    _append_b2c_sheets(output_path, df_co_plan, results["dynamic_targets"], df_consumption)

    print("=" * 70)
    print("  Phase 1 complete.")
    print("=" * 70 + "\n")

    return results


def _append_b2c_sheets(
    output_path: str,
    df_co_plan: pd.DataFrame,
    df_dynamic_targets: pd.DataFrame,
    df_consumption: pd.DataFrame,
):
    """Append B2C-specific sheets to the building output workbook."""
    try:
        from openpyxl import load_workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        wb = load_workbook(output_path)

        _NAVY  = "1F3864"
        _WHITE = "FFFFFF"

        def _add_sheet(wb, name, df):
            ws = wb.create_sheet(name)
            hdr_fill = PatternFill("solid", fgColor=_NAVY)
            hdr_font = Font(bold=True, color=_WHITE)
            bd = Border(
                left=Side(style="thin"), right=Side(style="thin"),
                top=Side(style="thin"),  bottom=Side(style="thin"),
            )
            for ci, col in enumerate(df.columns, start=1):
                cell = ws.cell(row=1, column=ci, value=col)
                cell.fill = hdr_fill
                cell.font = hdr_font
                cell.border = bd
                cell.alignment = Alignment(horizontal="center")
            for ri, (_, row) in enumerate(df.iterrows(), start=2):
                for ci, val in enumerate(row, start=1):
                    cell = ws.cell(row=ri, column=ci, value=val)
                    cell.border = bd
                    cell.alignment = Alignment(horizontal="center")
            for col in ws.columns:
                w = max((len(str(c.value or "")) for c in col), default=10)
                ws.column_dimensions[get_column_letter(col[0].column)].width = min(w + 2, 35)

        # Sheet: Changeover Plan
        if df_co_plan is not None and len(df_co_plan) > 0:
            _add_sheet(wb, "Changeover Plan", df_co_plan)

        # Sheet: Dynamic Targets (top 1000 rows to keep file manageable)
        if df_dynamic_targets is not None and len(df_dynamic_targets) > 0:
            top_targets = df_dynamic_targets.head(1000)
            _add_sheet(wb, "Dynamic Targets", top_targets)

        # Sheet: SKU Classification Summary
        cat_summary = (
            df_consumption.groupby("Category")
            .agg(
                SKU_Count=("SKUCode", "count"),
                Total_GT_Per_Shift=("Total_GT_Per_Shift_Day0", "sum"),
                Avg_Priority=("Priority_Score", "mean"),
            )
            .reset_index()
        )
        _add_sheet(wb, "SKU Classification", cat_summary)

        wb.save(output_path)
        print(f"  [B2C Sheets] Appended Changeover Plan, Dynamic Targets, SKU Classification")

    except Exception as exc:
        print(f"  ⚠️  Could not append B2C sheets: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# STANDALONE RUN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    _PLAN_START       = datetime(2026, 6, 1, 7, 0, 0)
    _CONSUMPTION_PATH = os.path.join(cbc_env.OUTPUT_DIR, "curing_consumption_table.xlsx")
    _OUTPUT_PATH      = os.path.join(MAIN_OUT, "bc_building_schedule.xlsx")

    results = run_from_database_b2c(
        plan_start       = _PLAN_START,
        consumption_path = _CONSUMPTION_PATH,
        output_path      = _OUTPUT_PATH,
    )

    # Quick summary
    ss = results.get("shift_schedule")
    if ss is not None and len(ss) > 0:
        prod = ss[~ss["SKUCode"].isin(["CHANGEOVER", "MOULD_CLEAN"])]
        total_gt = prod[prod["Machine"].isin(
            [str(m) for m in (Config.STAGE2 | Config.UNISTAGE)]
        )]["Qty"].sum()
        print(f"\n  Total GT produced across schedule: {total_gt:,.0f}")
