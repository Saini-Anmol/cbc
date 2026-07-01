"""
B2C Pipeline — Phase 0 Extended: 31-Day Dynamic Curing Consumption
===================================================================
Pre-computes a 31-sheet Excel (one sheet per day) showing how curing
consumption evolves across the May planning horizon as changeovers execute.

Approach: two-pass pre-computation (fully independent of the building scheduler)
  Pass 1 — Build CO schedule from Day 0 data:
    - Runner-Out presses: CO to highest-urgency NRI target, Day 1+
    - Runner-In presses : CO fires on the day demand is fulfilled (instantly)
    - NRI SKUs          : receive a press via CO, ranked by urgency score
    - Max 8 COs per day (plant-wide hard limit)
    - CO target urgency  = f(Priority_Score, production_days vs horizon remaining)
      Class A (CRITICAL): current_production_days > horizon_left  → can't meet demand without CO
      Class B (HELPFUL) : current_production_days ≤ horizon_left  → already fulfillable
      Sort: Class A first, then −Priority_Score, then after_CO_days ASC

  Pass 2 — Simulate 31 days using that CO schedule:
    - Running_Press_Count updated per CO event (new SKU from Shift C of CO day)
    - Updated_Demand_Qty decremented daily by Total_GT_Per_Shift × 3 shifts
    - production_days recomputed each day from remaining demand and press count
    - NRI SKUs before their CO fires: Running_Press_Count=0, Total_GT=0, production_days=blank

Outputs
  data/output/curing_consumption_31day.xlsx
    - Sheets Day_01 … Day_31  : per-day consumption table
    - Sheet  CO_Schedule       : full changeover plan (press, day, old_sku → new_sku)
    - Sheet  Day0_Summary      : same as existing curing_consumption_table.xlsx (for reference)

Standalone usage:
    python curing_consumption_dynamic.py
"""

from __future__ import annotations

import math
import os
import sys
import warnings
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

warnings.filterwarnings("ignore")

# ── venv re-exec ──────────────────────────────────────────────────────────────
_VENV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "myenv")
_VENV_PY  = os.path.join(_VENV_DIR, "bin", "python")
if (os.path.exists(_VENV_PY)
        and os.path.realpath(sys.prefix) != os.path.realpath(_VENV_DIR)
        and not os.environ.get("BC_REEXEC")):
    os.environ["BC_REEXEC"] = "1"
    os.execv(_VENV_PY, [_VENV_PY, os.path.abspath(__file__)] + sys.argv[1:])

import cbc_env
from curing_consumption import (
    ConsumptionConfig,
    ConsumptionETL,
    SKUClassifier,
    CycleTimeResolver,
    SKUEligibilityFilter,
)

HERE    = os.path.dirname(os.path.abspath(__file__))
IN_DIR  = cbc_env.INPUT_DIR
OUT_DIR = cbc_env.OUTPUT_DIR


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

# ── All scheduling params imported from bc_config (single source of truth) ───
from bc_config import (
    PLAN_START,
    PLANNING_DAYS,
    MAX_CHANGEOVERS_PER_DAY as MAX_CO_PER_DAY,
    SHIFTS_PER_DAY,
)

_NAVY  = "1F3864"
_WHITE = "FFFFFF"
_BLUE  = "D6E4F0"
_YELL  = "FFF2CC"
_GREEN = "E2EFDA"
_ORNG  = "FCE4D6"


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _qty_per_press_per_shift(ct_min: float) -> int:
    return math.floor(ConsumptionConfig.SHIFT_MINS / ct_min) \
           * ConsumptionConfig.CAVITIES_PER_MOULD


def _qty_per_press_per_day(ct_min: float) -> float:
    return _qty_per_press_per_shift(ct_min) * SHIFTS_PER_DAY


def _production_days(remaining_demand: float, press_count: int, rate_per_day: float) -> Optional[float]:
    """Days needed to fulfill remaining demand at current press rate. None if no press."""
    if press_count <= 0 or rate_per_day <= 0:
        return None
    return remaining_demand / (press_count * rate_per_day)


def _urgency_sort_key(
    priority_score: float,
    current_press_count: int,
    updated_demand: float,
    rate_per_day: float,
    horizon_left: int,
) -> tuple:
    """
    Two-level urgency sort key (sort ascending = highest urgency first).

    Class A (CRITICAL, key=0): current_production_days > horizon_left
        Demand CANNOT be met without this CO.
    Class B (HELPFUL, key=1) : current_production_days <= horizon_left
        Demand can be met with existing presses.

    Within class: highest Priority_Score first, then fewest after-CO days.
    """
    if current_press_count <= 0 or rate_per_day <= 0:
        current_days = float("inf")
    else:
        current_days = updated_demand / (current_press_count * rate_per_day)

    if rate_per_day > 0:
        after_days = updated_demand / ((current_press_count + 1) * rate_per_day)
    else:
        after_days = float("inf")

    cls = 0 if current_days > horizon_left else 1
    return (cls, -priority_score, after_days)


# ══════════════════════════════════════════════════════════════════════════════
# CO SCHEDULER  (Pass 1)
# ══════════════════════════════════════════════════════════════════════════════

class COScheduler:
    """
    Compute the full 31-day changeover schedule from Day 0 data alone.

    Returns a list of CO events:
        [{"day": int, "press": str, "old_sku": str, "new_sku": str}, ...]

    Rules:
    - Runner-Out presses: CO on earliest available day (Day 1+)
    - Runner-In presses : CO on the day Updated_Demand_Qty first reaches 0
    - NRI target        : highest urgency_sort_key among eligible allowable presses
    - Max MAX_CO_PER_DAY COs per day; excess deferred to next day
    - CO timing: CO fires on Day D; new SKU production from Day D (Shift C = same day)
      For daily-level modelling, press count update takes effect Day D+1 onwards
    """

    def schedule(
        self,
        df_day0: pd.DataFrame,
        df_demand: pd.DataFrame,
        df_allowable: pd.DataFrame,
        df_running_moulds: pd.DataFrame,
        ct_map: dict[str, float],
        max_co_per_day: int = MAX_CO_PER_DAY,
    ) -> list[dict]:
        """Returns sorted list of CO events."""

        # ── press → current SKU map ───────────────────────────────────────────
        press_to_sku: dict[str, str] = {}
        for _, r in df_running_moulds.iterrows():
            press_to_sku[str(r["Machine"])] = str(r["SKUCode"])

        ro_skus  = set(df_day0.loc[df_day0["Category"] == "Runner-Out",     "SKUCode"])
        ri_skus  = set(df_day0.loc[df_day0["Category"] == "Runner-In",      "SKUCode"])
        nri_skus = set(df_day0.loc[df_day0["Category"] == "Non-Runner-In",  "SKUCode"])

        all_demand_skus = set(df_demand["SKUCode"].str.strip())
        demand_map   = dict(zip(df_demand["SKUCode"].str.strip(), df_demand["Quantity"]))
        priority_map = dict(zip(df_demand["SKUCode"].str.strip(), df_demand["Priority"]))

        # ── Build press → ALL compatible demand SKUs (NRI + RI) ─────────────
        # Fixes bug where RO presses were only matched against NRI targets.
        sku_to_presses: dict[str, set] = {}
        for _, r in df_allowable.iterrows():
            sku = str(r["SKUCode"]).strip()
            if sku in all_demand_skus:
                machines = r.get("Machines", [])
                if machines:
                    sku_to_presses[sku] = {str(p) for p in machines}

        press_to_demand_targets: dict[str, list] = {}
        for sku, presses in sku_to_presses.items():
            for p in presses:
                press_to_demand_targets.setdefault(p, []).append(sku)

        # ── Running state ─────────────────────────────────────────────────────
        press_count: dict[str, int] = {}
        for _, r in df_day0.iterrows():
            sku = str(r["SKUCode"])
            press_count[sku] = int(r.get("Running_Press_Count", 0))

        updated_demand: dict[str, float] = {
            sku: float(demand_map.get(sku, 0)) for sku in all_demand_skus
        }

        # ── Track eligible presses ────────────────────────────────────────────
        # pending_ro_presses: RO presses carried forward every day until they CO.
        # Fixes bug where RO presses were only offered on Day 1; any that didn't
        # fit in the 8/day cap were silently dropped.
        runner_out_presses: set = {p for p, s in press_to_sku.items() if s in ro_skus}
        pending_ro_presses: set = runner_out_presses.copy()

        # demand_running_presses: presses running a demand SKU (RI + CO'd NRI/RO).
        # When their SKU's demand = 0, the press is freed for CO.
        demand_running_presses: set = {p for p, s in press_to_sku.items() if s in ri_skus}

        co_events: list[dict] = []
        daily_co_used: dict[int, int] = {}

        # ── Day-by-day simulation ─────────────────────────────────────────────
        for day in range(1, PLANNING_DAYS + 1):
            horizon_left = PLANNING_DAYS - day + 1
            co_used = daily_co_used.get(day, 0)

            # Drain demand by previous day's production
            if day > 1:
                for sku in all_demand_skus:
                    n = press_count.get(sku, 0)
                    if n <= 0:
                        continue
                    rate = _qty_per_press_per_day(
                        ct_map.get(sku, ConsumptionConfig.DEFAULT_CYCLE_TIME_MIN))
                    updated_demand[sku] = max(0.0, updated_demand[sku] - n * rate)

            if co_used >= max_co_per_day:
                continue

            # Identify free presses this day:
            # 1. All pending RO presses (carried forward until they CO)
            # 2. Demand-running presses whose SKU demand just hit 0
            newly_free: list[str] = list(pending_ro_presses)
            for p in demand_running_presses:
                current_sku = press_to_sku.get(p)
                if current_sku and updated_demand.get(current_sku, 0) <= 0:
                    newly_free.append(p)

            if not newly_free:
                continue

            # Score candidates: target = NRI (any) OR under-supplied RI
            # Under-supplied RI: current press count cannot meet demand in time
            candidates: list[tuple] = []
            for p in set(newly_free):          # deduplicate
                old_sku = press_to_sku.get(p, "")
                for target in press_to_demand_targets.get(p, []):
                    if target == old_sku:
                        continue               # don't CO to the same SKU
                    rem = updated_demand.get(target, 0)
                    if rem <= 0:
                        continue               # demand already fulfilled

                    n_t  = press_count.get(target, 0)
                    ct_t = ct_map.get(target, ConsumptionConfig.DEFAULT_CYCLE_TIME_MIN)
                    rate_t = _qty_per_press_per_day(ct_t)

                    is_nri = target in nri_skus
                    is_ri  = target in ri_skus

                    if is_nri:
                        pass   # always eligible
                    elif is_ri and n_t > 0:
                        # Only eligible if under-supplied (can't meet demand in time)
                        current_days = rem / (n_t * rate_t) if rate_t > 0 else float("inf")
                        if current_days <= horizon_left:
                            continue           # RI is already on track — skip
                    else:
                        continue

                    key = _urgency_sort_key(
                        priority_score=float(priority_map.get(target, 0)),
                        current_press_count=n_t,
                        updated_demand=rem,
                        rate_per_day=rate_t,
                        horizon_left=horizon_left,
                    )
                    candidates.append((key, p, old_sku, target))

            _dct = ConsumptionConfig.DEFAULT_CYCLE_TIME_MIN
            candidates.sort(key=lambda x: (
                x[0][0],                                           # Class A (0) before Class B (1)
                ct_map.get(x[3], _dct),                            # min CT target first (max throughput)
                x[0][1], x[0][2],                                  # −priority, after_days
                len(press_to_demand_targets.get(x[1], [])),         # exclusive press first (fewer targets)
            ))

            assigned: set = set()
            for key, p, old_sku, new_sku in candidates:
                if co_used >= max_co_per_day:
                    break
                if p in assigned:
                    continue

                # Plant limit = 8 COs/day (hard).  Within that budget, only fire
                # Class A COs — those where demand CANNOT be met in the remaining
                # horizon without this additional press.  Class B COs ("helpful"
                # but not critical) are deferred: demand is already reachable with
                # existing presses and using the CO slot for them just spreads
                # building capacity thinner, causing timing gaps and starvation.
                urgency_class = key[0]   # 0 = Class A (critical), 1 = Class B
                if urgency_class != 0:
                    continue  # Class B — skip; existing presses can meet demand

                co_events.append(
                    {"day": day, "press": p, "old_sku": old_sku, "new_sku": new_sku}
                )
                press_to_sku[p]  = new_sku
                press_count[old_sku] = max(0, press_count.get(old_sku, 0) - 1)
                press_count[new_sku] = press_count.get(new_sku, 0) + 1

                pending_ro_presses.discard(p)       # no longer stranded RO
                demand_running_presses.add(p)       # now running a demand SKU

                assigned.add(p)
                co_used += 1
                daily_co_used[day] = co_used

        # ── Rescue pass: NRI SKUs still without any CO ────────────────────────────
        # Main loop only frees presses when (a) RO presses are stranded or
        # (b) demand-running presses fulfil their SKU's demand completely.
        # Some NRI SKUs never match either condition because their compatible
        # curing presses are busy with RI SKUs that never fully drain demand.
        # Solution: donate one press from any RI SKU that has n_presses > 1
        # AND can still meet its own demand with n−1 presses.
        scheduled_nri = {ev["new_sku"] for ev in co_events if ev["new_sku"] in nri_skus}
        rescue_nri = sorted(
            nri_skus - scheduled_nri,
            key=lambda s: _urgency_sort_key(
                float(priority_map.get(s, 0)),
                0,
                float(updated_demand.get(s, 0)),
                _qty_per_press_per_day(ct_map.get(s, ConsumptionConfig.DEFAULT_CYCLE_TIME_MIN)),
                0,
            ),
        )
        n_rescued = 0
        if rescue_nri:
            print(f"  [CO Rescue] {len(rescue_nri)} NRI SKUs without CO — attempting rescue …")
            # Inverse map: nri_sku → set of presses that CAN produce it
            sku_to_compat: dict[str, set] = {}
            for press, targets in press_to_demand_targets.items():
                for t in targets:
                    sku_to_compat.setdefault(t, set()).add(press)

            for nri_sku in rescue_nri:
                rem = updated_demand.get(nri_sku, 0)
                if rem <= 0:
                    continue  # demand already zero — no point scheduling CO
                compatible = sorted(sku_to_compat.get(nri_sku, set()))
                scheduled = False
                for press in compatible:
                    current_sku = press_to_sku.get(press, "")
                    if current_sku == nri_sku:
                        continue
                    # Only donate from RI SKUs that have a spare press (n > 1)
                    if current_sku not in ri_skus:
                        continue
                    n_ri = press_count.get(current_sku, 0)
                    if n_ri <= 1:
                        continue  # can't spare — only press for that RI SKU
                    # Verify that RI SKU can still meet its FULL demand with n−1 presses.
                    # Previous bug: used updated_demand (= 0 after simulation with all
                    # n presses) which always passed the check — allowing COs even when
                    # n−1 presses cannot cover full demand across the horizon.
                    # Fix: use original demand_map value and compute actual capacity
                    # accounting for when the CO fires (earliest budget-available day).
                    ri_ct          = ct_map.get(current_sku, ConsumptionConfig.DEFAULT_CYCLE_TIME_MIN)
                    ri_rate        = _qty_per_press_per_day(ri_ct)
                    ri_full_demand = float(demand_map.get(current_sku, 0))

                    # Find earliest day with CO budget (needed for capacity check)
                    _co_day = next(
                        (d for d in range(1, PLANNING_DAYS + 1)
                         if daily_co_used.get(d, 0) < max_co_per_day),
                        None,
                    )
                    if _co_day is None:
                        continue  # no CO budget anywhere in horizon

                    # Capacity: n_ri presses run days 1.._co_day, then n_ri−1 for rest
                    cap_before = n_ri * ri_rate * _co_day
                    cap_after  = max(0, n_ri - 1) * ri_rate * (PLANNING_DAYS - _co_day)
                    if (cap_before + cap_after) < ri_full_demand:
                        continue  # CO would leave RI SKU demand unmet

                    # Schedule CO on the earliest available day found above
                    co_events.append({
                        "day":     _co_day,
                        "press":   press,
                        "old_sku": current_sku,
                        "new_sku": nri_sku,
                    })
                    press_to_sku[press] = nri_sku
                    press_count[current_sku] = max(0, n_ri - 1)
                    press_count[nri_sku] = press_count.get(nri_sku, 0) + 1
                    daily_co_used[_co_day] = daily_co_used.get(_co_day, 0) + 1
                    demand_running_presses.add(press)
                    pending_ro_presses.discard(press)
                    scheduled = True
                    n_rescued += 1
                    break

            still_missing = len(rescue_nri) - n_rescued
            print(f"  [CO Rescue] Rescued {n_rescued} NRI SKUs via spare-press donation"
                  + (f" | {still_missing} still without CO (no compatible spare press)"
                     if still_missing else ""))

        # ── Summary ───────────────────────────────────────────────────────────
        total_slots = max_co_per_day * PLANNING_DAYS
        used_slots  = len(co_events)
        co_by_day   = {}
        for ev in co_events:
            co_by_day[ev["day"]] = co_by_day.get(ev["day"], 0) + 1
        peak_day = max(co_by_day, key=co_by_day.get) if co_by_day else 0

        print(f"  [CO Scheduler] {used_slots} COs used / {total_slots} available "
              f"({used_slots/total_slots*100:.1f}%)")
        print(f"  [CO Scheduler] Peak: Day {peak_day} "
              f"({co_by_day.get(peak_day,0)} COs)  |  "
              f"Zero-CO days: {sum(1 for d in range(1,PLANNING_DAYS+1) if d not in co_by_day)}")

        if pending_ro_presses:
            print(f"  [WARN] {len(pending_ro_presses)} RO presses still stranded at Day 31 "
                  f"(no compatible demand SKU found after filter):")
            for p in sorted(pending_ro_presses):
                n_compat = len(press_to_demand_targets.get(p, []))
                print(f"    Press {p} ({press_to_sku.get(p,'?')}): "
                      f"{n_compat} compatible targets in allowable")

        return co_events


# ══════════════════════════════════════════════════════════════════════════════
# DAY SIMULATOR  (Pass 2)
# ══════════════════════════════════════════════════════════════════════════════

class DaySimulator:
    """
    Simulate 31 days of curing consumption using the pre-computed CO schedule.

    For each day D, the sheet contains:
      SKUCode, Category, Running_Press_Count, Total_Available_Moulds,
      Effective_CT_Min, Qty_Per_Press_Per_Shift, Total_GT_Per_Shift_DayN,
      Updated_Demand_Qty, Production_Days, Priority_Score
    """

    def simulate(
        self,
        df_day0: pd.DataFrame,
        df_demand: pd.DataFrame,
        df_allowable: pd.DataFrame,
        ct_map: dict[str, float],
        co_events: list[dict],
    ) -> list[pd.DataFrame]:
        """Returns list of 31 DataFrames, one per day (index 0 = Day 1)."""

        demand_map   = dict(zip(df_demand["SKUCode"].str.strip(), df_demand["Quantity"]))
        priority_map = dict(zip(df_demand["SKUCode"].str.strip(), df_demand["Priority"]))

        # Allowable moulds count per SKU (total eligible presses in master)
        allowable_count: dict[str, int] = {}
        for _, r in df_allowable.iterrows():
            sku = str(r["SKUCode"]).strip()
            machines = r.get("Machines", [])
            allowable_count[sku] = len(machines) if machines else 0

        # Build CO lookup: day → list of (press, old_sku, new_sku)
        co_by_day: dict[int, list] = {}
        for ev in co_events:
            co_by_day.setdefault(ev["day"], []).append(ev)

        # Universe of SKUs (demand ∪ running presses)
        all_skus = sorted(set(df_day0["SKUCode"].tolist()) | set(demand_map.keys()))

        # Initial state from Day 0 table
        press_count: dict[str, int] = {}
        category_map: dict[str, str] = {}
        for _, r in df_day0.iterrows():
            sku = str(r["SKUCode"])
            press_count[sku] = int(r.get("Running_Press_Count", 0))
            category_map[sku] = str(r.get("Category", "Non-Runner-In"))

        # Running demand
        updated_demand: dict[str, float] = {
            sku: float(demand_map.get(sku, 0)) for sku in all_skus
        }

        daily_sheets: list[pd.DataFrame] = []

        for day in range(1, PLANNING_DAYS + 1):
            horizon_left = PLANNING_DAYS - day + 1

            # Apply COs for this day (press count update effective from Shift C same day)
            for ev in co_by_day.get(day, []):
                old = ev["old_sku"]
                new = ev["new_sku"]
                press_count[old] = max(0, press_count.get(old, 0) - 1)
                press_count[new] = press_count.get(new, 0) + 1
                if category_map.get(new) == "Non-Runner-In":
                    category_map[new] = "Runner-In"

            # Snapshot today's GT output BEFORE draining (used in the sheet)
            gt_today: dict[str, int] = {}
            for sku in all_skus:
                n = press_count.get(sku, 0)
                if n <= 0:
                    continue
                ct  = ct_map.get(sku, ConsumptionConfig.DEFAULT_CYCLE_TIME_MIN)
                qps = _qty_per_press_per_shift(ct)
                gt_today[sku] = n * qps   # per-shift; × SHIFTS_PER_DAY = day total

            # Drain demand by today's full production FIRST so the day sheet
            # shows "Updated_Demand_Qty" = remaining demand AFTER today runs.
            # This ensures Day 31's row correctly reflects the closing balance.
            for sku, qps in gt_today.items():
                ct   = ct_map.get(sku, ConsumptionConfig.DEFAULT_CYCLE_TIME_MIN)
                rate = _qty_per_press_per_day(ct)
                updated_demand[sku] = max(0.0, updated_demand.get(sku, 0) - rate * press_count.get(sku, 0))

            # Build day sheet (demand figures are closing balances for the day)
            rows = []
            for sku in all_skus:
                dem = demand_map.get(sku, 0)
                if dem <= 0 and press_count.get(sku, 0) == 0:
                    continue  # not in demand and not running — skip

                cat = category_map.get(sku, "Non-Runner-In")
                n   = press_count.get(sku, 0)
                ct  = ct_map.get(sku, ConsumptionConfig.DEFAULT_CYCLE_TIME_MIN)
                qps = _qty_per_press_per_shift(ct)

                rem_demand = max(0.0, updated_demand.get(sku, 0))

                # production_days from closing balance: how many more days at current rate
                if n > 0 and rem_demand > 0:
                    rate_day = _qty_per_press_per_day(ct)
                    prod_days = round(_production_days(rem_demand, n, rate_day), 1)
                else:
                    prod_days = None   # blank in Excel

                rows.append({
                    "SKUCode":                  sku,
                    "Category":                 cat,
                    "Running_Press_Count":       n,
                    "Total_Available_Moulds":    allowable_count.get(sku, 0),
                    "Effective_CT_Min":          round(ct, 2),
                    "Qty_Per_Press_Per_Shift":   qps,
                    "Total_GT_Per_Shift_DayN":   gt_today.get(sku, 0),
                    "Updated_Demand_Qty":        int(rem_demand),
                    "Production_Days":           prod_days,
                    "Priority_Score":            priority_map.get(sku, 0),
                })

            df_day = pd.DataFrame(rows)
            _ord = {"Runner-In": 0, "Runner-Out": 1, "Non-Runner-In": 2}
            df_day["_o"] = df_day["Category"].map(_ord).fillna(3)
            df_day = (df_day
                      .sort_values(["_o", "Priority_Score"], ascending=[True, False])
                      .drop(columns=["_o"])
                      .reset_index(drop=True))

            daily_sheets.append(df_day)

        return daily_sheets


# ══════════════════════════════════════════════════════════════════════════════
# EXCEL EXPORTER
# ══════════════════════════════════════════════════════════════════════════════

class DynamicExporter:
    """Write the 31-sheet Excel file."""

    _COLS = [
        "SKUCode", "Category", "Running_Press_Count", "Total_Available_Moulds",
        "Effective_CT_Min", "Qty_Per_Press_Per_Shift", "Total_GT_Per_Shift_DayN",
        "Updated_Demand_Qty", "Production_Days", "Priority_Score",
    ]
    # Human-readable column headers for day sheets
    _COL_HEADERS = {
        "Updated_Demand_Qty":    "Remaining Demand (after day)",
        "Total_GT_Per_Shift_DayN": "GT / Shift (this day)",
    }

    _CAT_FILL = {
        "Runner-In":     PatternFill("solid", fgColor="E2EFDA"),
        "Runner-Out":    PatternFill("solid", fgColor="FCE4D6"),
        "Non-Runner-In": PatternFill("solid", fgColor="FFF2CC"),
    }

    def _hdr_style(self):
        return {
            "fill": PatternFill("solid", fgColor=_NAVY),
            "font": Font(bold=True, color=_WHITE, size=10),
            "alignment": Alignment(horizontal="center", vertical="center", wrap_text=True),
            "border": Border(
                bottom=Side(style="thin", color=_WHITE),
                right=Side(style="thin", color=_WHITE),
            ),
        }

    def _apply_hdr(self, ws, row=1):
        hdr = self._hdr_style()
        for col_idx, col_name in enumerate(self._COLS, start=1):
            label = self._COL_HEADERS.get(col_name, col_name.replace("_", " "))
            cell = ws.cell(row=row, column=col_idx, value=label)
            for k, v in hdr.items():
                setattr(cell, k, v)

    def _write_day_sheet(self, ws, df: pd.DataFrame, day: int):
        plan_date = PLAN_START + timedelta(days=day - 1)
        ws.title = f"Day_{day:02d}"

        # Title row
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(self._COLS))
        title_cell = ws.cell(row=1, column=1,
                             value=f"Curing Consumption — Day {day:02d} "
                                   f"({plan_date.strftime('%d-%b-%Y')})")
        title_cell.font = Font(bold=True, size=11, color=_WHITE)
        title_cell.fill = PatternFill("solid", fgColor=_NAVY)
        title_cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 22

        # Header row
        self._apply_hdr(ws, row=2)
        ws.row_dimensions[2].height = 30

        # Data rows
        for r_idx, (_, row) in enumerate(df.iterrows(), start=3):
            cat = str(row.get("Category", ""))
            fill = self._CAT_FILL.get(cat)
            for c_idx, col in enumerate(self._COLS, start=1):
                val = row.get(col)
                # Production_Days: leave blank (None) when no press
                if col == "Production_Days" and val is None:
                    val = ""
                cell = ws.cell(row=r_idx, column=c_idx, value=val)
                cell.alignment = Alignment(horizontal="center", vertical="center")
                if fill:
                    cell.fill = fill
                if col in ("Updated_Demand_Qty", "Total_GT_Per_Shift_DayN",
                           "Running_Press_Count", "Qty_Per_Press_Per_Shift"):
                    cell.number_format = "#,##0"
                elif col in ("Effective_CT_Min", "Production_Days"):
                    cell.number_format = "0.0"
                elif col == "Priority_Score":
                    cell.number_format = "0.00"

        # Column widths
        _widths = [16, 16, 18, 20, 15, 20, 22, 20, 14, 14]
        for i, w in enumerate(_widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

        # Legend below data
        legend_row = len(df) + 4
        ws.cell(row=legend_row, column=1, value="Legend:").font = Font(bold=True)
        for cat, fill in self._CAT_FILL.items():
            legend_row += 1
            c = ws.cell(row=legend_row, column=1, value=cat)
            c.fill = fill
            c.alignment = Alignment(horizontal="left")

    def _write_co_sheet(self, ws, co_events: list[dict]):
        ws.title = "CO_Schedule"
        headers = ["Day", "Press", "Old_SKU", "New_SKU", "Plan_Date", "CO_Type"]
        hdr = self._hdr_style()
        for c_idx, h in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=c_idx, value=h.replace("_", " "))
            for k, v in hdr.items():
                setattr(cell, k, v)

        for r_idx, ev in enumerate(co_events, start=2):
            plan_date = (PLAN_START + timedelta(days=ev["day"] - 1)).strftime("%d-%b-%Y")
            ws.cell(row=r_idx, column=1, value=ev["day"])
            ws.cell(row=r_idx, column=2, value=ev["press"])
            ws.cell(row=r_idx, column=3, value=ev["old_sku"])
            ws.cell(row=r_idx, column=4, value=ev["new_sku"])
            ws.cell(row=r_idx, column=5, value=plan_date)
            ws.cell(row=r_idx, column=6, value="curing_CO")
            for c in range(1, 7):
                ws.cell(row=r_idx, column=c).alignment = Alignment(horizontal="center")

        for c_idx, w in enumerate([8, 12, 16, 16, 14, 12], start=1):
            ws.column_dimensions[get_column_letter(c_idx)].width = w

        # Day-level summary below
        from collections import Counter
        co_by_day = Counter(ev["day"] for ev in co_events)
        summary_row = len(co_events) + 3
        ws.cell(row=summary_row, column=1, value="Day-level CO count:").font = Font(bold=True)
        for day in sorted(co_by_day):
            summary_row += 1
            ws.cell(row=summary_row, column=1, value=f"Day {day:02d}").alignment = Alignment(horizontal="center")
            ws.cell(row=summary_row, column=2, value=co_by_day[day]).alignment = Alignment(horizontal="center")

    def _cell(self, ws, row, col, value="", bold=False, fill=None,
              align="center", num_fmt=None, font_size=10, color=None):
        c = ws.cell(row=row, column=col, value=value)
        c.alignment = Alignment(horizontal=align, vertical="center")
        f = Font(bold=bold, size=font_size)
        if color:
            f = Font(bold=bold, size=font_size, color=color)
        c.font = f
        if fill:
            c.fill = fill
        if num_fmt:
            c.number_format = num_fmt
        return c

    def _section_header(self, ws, row, col, text, n_cols=6):
        ws.merge_cells(start_row=row, start_column=col,
                       end_row=row, end_column=col + n_cols - 1)
        c = ws.cell(row=row, column=col, value=text)
        c.font = Font(bold=True, color=_WHITE, size=10)
        c.fill = PatternFill("solid", fgColor="2E4057")
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws.row_dimensions[row].height = 18

    def _write_summary_sheet(
        self,
        ws,
        df_day0: pd.DataFrame,
        df_excluded: pd.DataFrame,
        daily_sheets: list,
        co_events: list,
        n_demand_raw: int,
        demand_path: str,
        planning_days: int,
    ):
        ws.title = "Summary"
        ws.column_dimensions["A"].width = 36
        ws.column_dimensions["B"].width = 18
        ws.column_dimensions["C"].width = 18
        ws.column_dimensions["D"].width = 18
        ws.column_dimensions["E"].width = 18
        ws.column_dimensions["F"].width = 22

        _LABEL_FILL = PatternFill("solid", fgColor="EBF3FA")
        _VAL_FILL   = PatternFill("solid", fgColor="FFFFFF")
        _WARN_FILL  = PatternFill("solid", fgColor="FFE0CC")
        _GOOD_FILL  = PatternFill("solid", fgColor="E2EFDA")
        _TH_FILL    = PatternFill("solid", fgColor="D6E4F0")

        hdr = self._hdr_style()
        r = 1

        # ── Title ──────────────────────────────────────────────────────────────
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        c = ws.cell(row=r, column=1,
                    value="B2C Curing Consumption — 31-Day Plan Summary (May 2026)")
        c.font = Font(bold=True, size=13, color=_WHITE)
        c.fill = PatternFill("solid", fgColor=_NAVY)
        c.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[r].height = 26
        r += 1

        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        c = ws.cell(row=r, column=1,
                    value=f"Demand file: {os.path.basename(demand_path)}   |   "
                          f"Generated: {datetime.now().strftime('%d-%b-%Y %H:%M')}")
        c.font = Font(italic=True, size=9, color="555555")
        c.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[r].height = 16
        r += 2

        # ── Section A: Demand File Overview ───────────────────────────────────
        self._section_header(ws, r, 1, "A.  Demand File Overview", 6)
        r += 1

        demand_rows = (df_day0[df_day0["Category"] != "Runner-Out"])
        ri_rows  = df_day0[df_day0["Category"] == "Runner-In"]
        ro_rows  = df_day0[df_day0["Category"] == "Runner-Out"]
        nri_rows = df_day0[df_day0["Category"] == "Non-Runner-In"]

        # Exclude Runner-Out (non-demand) AND excluded SKUs (no master data) from
        # total_demand so that demand_left_day31 (which also excludes them) stays
        # on the same basis — avoids phantom fulfillment of 62,802 unproducible tyres.
        _excl_codes = set(df_excluded["SKUCode"].astype(str).str.strip()) if len(df_excluded) else set()
        total_demand = float(
            df_day0[
                (df_day0["Category"] != "Runner-Out") &
                (~df_day0["SKUCode"].astype(str).str.strip().isin(_excl_codes))
            ]["Demand_Qty"].sum()
        )
        n_eligible   = len(demand_rows)
        n_excluded   = len(df_excluded)

        overview = [
            ("Total SKUs in demand file",    n_demand_raw,            "#,##0",  None),
            ("Eligible SKUs (pass filter)",  n_eligible,              "#,##0",  _GOOD_FILL),
            ("Excluded SKUs (no data)",      n_excluded,              "#,##0",  _WARN_FILL if n_excluded else None),
            ("Total demand quantity (tyres)", int(total_demand),      "#,##0",  None),
        ]
        for label, val, fmt, fill in overview:
            self._cell(ws, r, 1, label, align="left",  fill=_LABEL_FILL)
            self._cell(ws, r, 2, val,   num_fmt=fmt,   fill=fill or _VAL_FILL)
            ws.row_dimensions[r].height = 16
            r += 1
        r += 1

        # ── Section B: Day 0 Category Breakdown ───────────────────────────────
        self._section_header(ws, r, 1, "B.  Day 0 Category Breakdown", 6)
        r += 1

        # sub-header
        for ci, htext in enumerate(
            ["Category", "SKU Count", "Press Count", "Demand Qty", "GT / Shift (Day 0)", ""],
            start=1
        ):
            c = ws.cell(row=r, column=ci, value=htext)
            for k, v in hdr.items():
                setattr(c, k, v)
        ws.row_dimensions[r].height = 20
        r += 1

        cat_data = [
            ("Runner-In",
             len(ri_rows),
             int(ri_rows["Running_Press_Count"].sum()),
             int(ri_rows["Demand_Qty"].sum()),
             int(ri_rows["Total_GT_Per_Shift_Day0"].sum()),
             ""),
            ("Runner-Out  ⚠ non-demand SKUs — CO candidates only",
             len(ro_rows),
             int(ro_rows["Running_Press_Count"].sum()),
             "—",
             int(ro_rows["Total_GT_Per_Shift_Day0"].sum()),
             "These presses will CO to demand SKUs"),
            ("Non-Runner-In",
             len(nri_rows),
             0,
             int(nri_rows["Demand_Qty"].sum()),
             0,
             "Awaiting curing press via CO"),
        ]
        for cat, sku_cnt, press_cnt, dem_qty, gt_shift, note in cat_data:
            fill = self._CAT_FILL.get(cat.split("  ")[0])
            self._cell(ws, r, 1, cat,       align="left", fill=fill)
            self._cell(ws, r, 2, sku_cnt,   num_fmt="#,##0", fill=fill)
            self._cell(ws, r, 3, press_cnt, num_fmt="#,##0", fill=fill)
            self._cell(ws, r, 4, dem_qty,   num_fmt="#,##0" if dem_qty != "—" else "@", fill=fill)
            self._cell(ws, r, 5, gt_shift,  num_fmt="#,##0", fill=fill)
            self._cell(ws, r, 6, note,      align="left",    fill=fill)
            ws.row_dimensions[r].height = 16
            r += 1

        # Total row
        total_fill = PatternFill("solid", fgColor="D6E4F0")
        self._cell(ws, r, 1, "TOTAL (demand SKUs)", bold=True, align="left", fill=total_fill)
        self._cell(ws, r, 2, n_eligible,                        num_fmt="#,##0", fill=total_fill, bold=True)
        self._cell(ws, r, 3, int(ri_rows["Running_Press_Count"].sum()),
                              num_fmt="#,##0", fill=total_fill, bold=True)
        self._cell(ws, r, 4, int(total_demand),                 num_fmt="#,##0", fill=total_fill, bold=True)
        self._cell(ws, r, 5, int(ri_rows["Total_GT_Per_Shift_Day0"].sum()),
                              num_fmt="#,##0", fill=total_fill, bold=True)
        ws.row_dimensions[r].height = 16
        r += 1

        # Note about Runner-Out
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        note_cell = ws.cell(
            row=r, column=1,
            value="ℹ  Runner-Out rows are NOT from the demand file. "
                  "They are presses currently curing a non-demanded SKU. "
                  "They appear in Day0 only for CO planning; their rows disappear "
                  "from each day sheet once the CO executes."
        )
        note_cell.font = Font(italic=True, size=9, color="555555")
        note_cell.alignment = Alignment(horizontal="left", wrap_text=True)
        note_cell.fill = PatternFill("solid", fgColor="F5F5F5")
        ws.row_dimensions[r].height = 28
        r += 2

        # ── Section C: Demand Coverage over 31 days ───────────────────────────
        self._section_header(ws, r, 1, "C.  Demand Coverage — 31-Day Horizon", 6)
        r += 1

        total_gt_capacity = sum(
            df_d["Total_GT_Per_Shift_DayN"].sum() * SHIFTS_PER_DAY
            for df_d in daily_sheets
        )
        demand_left_day31 = int(daily_sheets[-1]["Updated_Demand_Qty"].sum())
        gt_produced       = int(total_demand) - demand_left_day31
        coverage_pct      = (gt_produced / total_demand * 100) if total_demand else 0

        cov_rows = [
            ("Total demand quantity (tyres)",          int(total_demand),     "#,##0"),
            ("Total GT capacity across 31 days",       int(total_gt_capacity),"#,##0"),
            ("Demand fulfilled by Day 31 (tyres)",     gt_produced,           "#,##0"),
            ("Demand remaining after Day 31 (tyres)",  demand_left_day31,     "#,##0"),
            ("Demand coverage %",                      round(coverage_pct, 1),"0.0\"%\""),
        ]
        for label, val, fmt in cov_rows:
            self._cell(ws, r, 1, label, align="left", fill=_LABEL_FILL)
            fill = _GOOD_FILL if label == "Demand coverage %" and coverage_pct >= 90 else _VAL_FILL
            self._cell(ws, r, 2, val, num_fmt=fmt, fill=fill, bold=(label == "Demand coverage %"))
            ws.row_dimensions[r].height = 16
            r += 1
        r += 1

        # ── Section D: CO Schedule Summary ────────────────────────────────────
        self._section_header(ws, r, 1, "D.  Changeover Schedule Summary", 6)
        r += 1

        from collections import Counter
        co_by_day = Counter(ev["day"] for ev in co_events)
        total_cos = len(co_events)

        self._cell(ws, r, 1, "Total changeover events",    align="left", fill=_LABEL_FILL)
        self._cell(ws, r, 2, total_cos, num_fmt="#,##0",   fill=_VAL_FILL, bold=True)
        ws.row_dimensions[r].height = 16
        r += 1
        self._cell(ws, r, 1, "Peak COs in a single day",  align="left", fill=_LABEL_FILL)
        self._cell(ws, r, 2, max(co_by_day.values()) if co_by_day else 0,
                   num_fmt="#,##0", fill=_VAL_FILL)
        ws.row_dimensions[r].height = 16
        r += 1

        # mini day table
        for ci, htext in enumerate(["Day", "COs", ""], start=1):
            c = ws.cell(row=r, column=ci, value=htext)
            for k, v in hdr.items():
                setattr(c, k, v)
        ws.row_dimensions[r].height = 18
        r += 1
        for day in sorted(co_by_day):
            self._cell(ws, r, 1, f"Day {day:02d}", fill=_TH_FILL)
            self._cell(ws, r, 2, co_by_day[day], num_fmt="#,##0", fill=_VAL_FILL)
            ws.row_dimensions[r].height = 15
            r += 1
        r += 1

        # ── Section E: Excluded SKUs ───────────────────────────────────────────
        self._section_header(ws, r, 1, f"E.  Excluded SKUs ({n_excluded}) — Reason for Skip", 6)
        r += 1

        if df_excluded.empty:
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
            ws.cell(row=r, column=1, value="None — all demand SKUs are eligible.").font = \
                Font(italic=True, color="555555")
            ws.row_dimensions[r].height = 16
        else:
            excl_cols = ["SKUCode", "Demand_Qty", "Priority_Score", "Remark"]
            for ci, htext in enumerate(excl_cols, start=1):
                c = ws.cell(row=r, column=ci, value=htext.replace("_", " "))
                for k, v in hdr.items():
                    setattr(c, k, v)
            ws.row_dimensions[r].height = 20
            r += 1
            for _, row in df_excluded.iterrows():
                self._cell(ws, r, 1, str(row.get("SKUCode", "")), align="left",
                           fill=_WARN_FILL)
                self._cell(ws, r, 2, int(row.get("Demand_Qty", 0)),
                           num_fmt="#,##0", fill=_WARN_FILL)
                self._cell(ws, r, 3, float(row.get("Priority_Score", 0)),
                           num_fmt="0.0000", fill=_WARN_FILL)
                self._cell(ws, r, 4, str(row.get("Remark", "")), align="left",
                           fill=_WARN_FILL)
                ws.row_dimensions[r].height = 16
                r += 1

            # total excluded demand
            r += 1
            excl_dem = int(df_excluded["Demand_Qty"].sum())
            self._cell(ws, r, 1, "Total demand in excluded SKUs", align="left",
                       fill=_WARN_FILL, bold=True)
            self._cell(ws, r, 2, excl_dem, num_fmt="#,##0", fill=_WARN_FILL, bold=True)
            excl_pct = excl_dem / (total_demand + excl_dem) * 100 if (total_demand + excl_dem) else 0
            self._cell(ws, r, 3, f"{excl_pct:.1f}% of gross demand",
                       align="left", fill=_WARN_FILL)

    def export(
        self,
        daily_sheets: list[pd.DataFrame],
        co_events: list[dict],
        df_day0: pd.DataFrame,
        df_excluded: pd.DataFrame,
        n_demand_raw: int,
        demand_path: str,
        planning_days: int,
        output_path: str,
    ):
        wb = Workbook()
        wb.remove(wb.active)  # remove default sheet

        # Sheet 1: Summary (new)
        ws_sum = wb.create_sheet("Summary")
        self._write_summary_sheet(
            ws_sum, df_day0, df_excluded, daily_sheets, co_events,
            n_demand_raw, demand_path, planning_days,
        )

        # Sheet 2: Day0 snapshot
        ws0 = wb.create_sheet("Day0_Summary")
        day0_cols = [
            "SKUCode", "Category", "Running_Press_Count", "MouldLife_min",
            "Effective_CT_Min", "Qty_Per_Press_Per_Shift", "Total_GT_Per_Shift_Day0",
            "Demand_Qty", "Priority_Score", "Skip_Reason",
        ]
        day0_cols_present = [c for c in day0_cols if c in df_day0.columns]

        # Title row for Day0 that explains the Runner-Out rows
        n_ri  = (df_day0["Category"] == "Runner-In").sum()
        n_ro  = (df_day0["Category"] == "Runner-Out").sum()
        n_nri = (df_day0["Category"] == "Non-Runner-In").sum()
        ws0.merge_cells(start_row=1, start_column=1,
                        end_row=1, end_column=len(day0_cols_present))
        title = ws0.cell(
            row=1, column=1,
            value=(f"Day 0 Snapshot — {n_ri} Runner-In + {n_nri} Non-Runner-In "
                   f"(demand SKUs)  |  {n_ro} Runner-Out (non-demand, CO candidates only)")
        )
        title.font  = Font(bold=True, size=10, color=_WHITE)
        title.fill  = PatternFill("solid", fgColor=_NAVY)
        title.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws0.row_dimensions[1].height = 20

        hdr = self._hdr_style()
        for c_idx, h in enumerate(day0_cols_present, start=1):
            cell = ws0.cell(row=2, column=c_idx, value=h.replace("_", " "))
            for k, v in hdr.items():
                setattr(cell, k, v)
        ws0.row_dimensions[2].height = 20

        for r_idx, (_, row) in enumerate(df_day0.iterrows(), start=3):
            cat = str(row.get("Category", ""))
            fill = self._CAT_FILL.get(cat)
            for c_idx, col in enumerate(day0_cols_present, start=1):
                cell = ws0.cell(row=r_idx, column=c_idx, value=row.get(col))
                cell.alignment = Alignment(horizontal="center")
                if fill:
                    cell.fill = fill
        for i, w in enumerate([16, 16, 18, 14, 15, 20, 22, 16, 14, 40], start=1):
            ws0.column_dimensions[get_column_letter(i)].width = w

        # CO Schedule sheet
        ws_co = wb.create_sheet("CO_Schedule")
        self._write_co_sheet(ws_co, co_events)

        # Day sheets (Day_01 … Day_31)
        for day_idx, df_day in enumerate(daily_sheets, start=1):
            ws = wb.create_sheet()
            self._write_day_sheet(ws, df_day, day_idx)

        # demand_drawdown — demand remaining after each day's curing (RI + NRI only)
        # Day 0 = opening available demand (excludes excluded NRI + Runner-Out)
        # Daily_Consumed = prev_remaining - curr_remaining
        ws_dd = wb.create_sheet("demand_drawdown")
        hdr_dd = self._hdr_style()
        for c_idx, label in enumerate(["Day", "Remaining_Demand", "Daily_Consumed"], start=1):
            cell = ws_dd.cell(row=1, column=c_idx, value=label)
            for k, v in hdr_dd.items():
                setattr(cell, k, v)
        ws_dd.row_dimensions[1].height = 20

        demand_cats = {"Runner-In", "Non-Runner-In"}
        # Day 0 opening: sum Demand_Qty for RI + NRI, exclude rows with Skip_Reason
        d0_mask = df_day0["Category"].isin(demand_cats)
        if "Skip_Reason" in df_day0.columns:
            d0_mask &= df_day0["Skip_Reason"].isna() | (df_day0["Skip_Reason"].astype(str).str.strip() == "")
        day0_remaining = int(df_day0.loc[d0_mask, "Demand_Qty"].sum())

        ws_dd.cell(row=2, column=1, value="Opening (Day 0)").alignment = Alignment(horizontal="center")
        ws_dd.cell(row=2, column=2, value=day0_remaining).alignment = Alignment(horizontal="center")
        ws_dd.cell(row=2, column=3, value="—").alignment = Alignment(horizontal="center")

        prev_remaining = day0_remaining
        total_consumed = 0
        for day_idx, df_day in enumerate(daily_sheets, start=1):
            dmask = df_day["Category"].isin(demand_cats)
            curr_remaining = int(df_day.loc[dmask, "Updated_Demand_Qty"].sum())
            consumed = prev_remaining - curr_remaining
            total_consumed += consumed
            r = day_idx + 2
            ws_dd.cell(row=r, column=1, value=f"Day {day_idx:02d}").alignment = Alignment(horizontal="center")
            ws_dd.cell(row=r, column=2, value=curr_remaining).alignment = Alignment(horizontal="center")
            ws_dd.cell(row=r, column=3, value=consumed).alignment = Alignment(horizontal="center")
            prev_remaining = curr_remaining

        total_row = len(daily_sheets) + 3
        for col, val in [(1, "Total Consumed"), (2, ""), (3, total_consumed)]:
            cell = ws_dd.cell(row=total_row, column=col, value=val)
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center")
        ws_dd.column_dimensions["A"].width = 18
        ws_dd.column_dimensions["B"].width = 20
        ws_dd.column_dimensions["C"].width = 18

        # curing_daily_cons — total curing production per day across all SKUs
        ws_dc = wb.create_sheet("curing_daily_cons")
        hdr = self._hdr_style()
        for c_idx, label in enumerate(["Day", "Total_Curing_Production"], start=1):
            cell = ws_dc.cell(row=1, column=c_idx, value=label)
            for k, v in hdr.items():
                setattr(cell, k, v)
        ws_dc.row_dimensions[1].height = 20
        grand_total = 0
        for day_idx, df_day in enumerate(daily_sheets, start=1):
            demand_mask = df_day["Category"].isin({"Runner-In", "Non-Runner-In"})
            daily_total = int(df_day.loc[demand_mask, "Total_GT_Per_Shift_DayN"].sum() * SHIFTS_PER_DAY)
            grand_total += daily_total
            ws_dc.cell(row=day_idx + 1, column=1, value=f"Day {day_idx:02d}").alignment = Alignment(horizontal="center")
            ws_dc.cell(row=day_idx + 1, column=2, value=daily_total).alignment = Alignment(horizontal="center")
        total_row = len(daily_sheets) + 2
        total_label = ws_dc.cell(row=total_row, column=1, value="Total")
        total_label.font = Font(bold=True)
        total_label.alignment = Alignment(horizontal="center")
        total_val = ws_dc.cell(row=total_row, column=2, value=grand_total)
        total_val.font = Font(bold=True)
        total_val.alignment = Alignment(horizontal="center")
        ws_dc.column_dimensions["A"].width = 12
        ws_dc.column_dimensions["B"].width = 24

        wb.save(output_path)
        print(f"  [Export] Saved → {output_path}")
        print(f"  [Export] Sheets: Summary + Day0_Summary + CO_Schedule + "
              f"{len(daily_sheets)} day sheets + demand_drawdown + curing_daily_cons")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run_dynamic_consumption(
    demand_path: str | None = None,
    output_path: str | None = None,
    plan_start: datetime = PLAN_START,
    planning_days: int = PLANNING_DAYS,
    max_co_per_day: int = MAX_CO_PER_DAY,
) -> dict:
    """
    Build the 31-day dynamic curing consumption file.

    Returns dict with keys:
        daily_sheets, co_events, ct_map, df_day0
    """
    if demand_path is None:
        # Auto-detect demand file in input dir
        for fname in sorted(os.listdir(IN_DIR), reverse=True):
            if fname.lower().endswith((".xlsx", ".csv")) and "demand" in fname.lower():
                demand_path = os.path.join(IN_DIR, fname)
                break
        if demand_path is None:
            candidates = [f for f in os.listdir(IN_DIR)
                          if f.lower().endswith((".xlsx", ".csv"))]
            if candidates:
                demand_path = os.path.join(IN_DIR, candidates[0])
            else:
                raise FileNotFoundError(f"No demand file found in {IN_DIR}")

    if output_path is None:
        output_path = os.path.join(OUT_DIR, "curing_consumption_31day.xlsx")

    print("\n" + "=" * 70)
    print("  B2C Phase 0 Extended — 31-Day Dynamic Curing Consumption (May)")
    print("=" * 70)
    print(f"  Demand file : {os.path.basename(demand_path)}")
    print(f"  Plan start  : {plan_start.strftime('%d-%b-%Y')} | Days: {planning_days}")

    engine = cbc_env.make_engine()
    etl = ConsumptionETL(engine)

    print("\n  [ETL] Loading demand …")
    df_demand = etl.load_demand(demand_path)
    n_demand_raw = len(df_demand)          # total before eligibility filter
    print(f"        {n_demand_raw} demanded SKUs")

    print("  [ETL] Loading cycle times …")
    df_ct = etl.load_cycle_times()

    print("  [ETL] Loading running moulds …")
    df_running = etl.load_running_moulds()
    print(f"        {len(df_running)} active press rows")

    print("  [ETL] Loading curing allowable machines …")
    df_allowable = etl.load_curing_allowable()
    print(f"        {len(df_allowable)} SKUs with allowable presses")

    # Eligibility filter
    print("  [ETL] Loading eligibility sources …")
    bld_master  = etl.load_building_allowable_skus()
    bld_history = etl.load_building_history_skus()
    cur_master  = etl.load_curing_allowable_skus()
    cur_history = etl.load_curing_history_skus()
    filt = SKUEligibilityFilter()
    df_demand, df_excluded = filt.filter(
        df_demand, bld_master, bld_history, cur_master, cur_history
    )
    print(f"  [Eligible] {len(df_demand)} SKUs pass | {len(df_excluded)} excluded")

    # Classify & resolve CT
    classifier = SKUClassifier()
    df_classify = classifier.classify(df_demand, df_running)
    ri  = (df_classify["Category"] == "Runner-In").sum()
    nri = (df_classify["Category"] == "Non-Runner-In").sum()
    print(f"  [Classify] Runner-In: {ri} | Non-Runner-In: {nri}")

    ct_resolver = CycleTimeResolver()
    ct_map = ct_resolver.resolve(df_classify["SKUCode"].tolist(), df_ct)
    n_default = sum(1 for v in ct_map.values()
                    if v == ConsumptionConfig.DEFAULT_CYCLE_TIME_MIN)
    print(f"  [CT] {len(ct_map)} SKUs | {n_default} using default {ConsumptionConfig.DEFAULT_CYCLE_TIME_MIN} min")

    # Build Day 0 consumption table (same as curing_consumption.py output)
    from curing_consumption import ConsumptionCalculator
    calc = ConsumptionCalculator()
    df_day0 = calc.compute(df_classify, ct_map, df_demand, plan_start, planning_days)
    df_day0["Skip_Reason"] = ""   # eligible demand SKUs — no skip

    # Append excluded demand SKUs back into Day0 for display (with Skip_Reason).
    # They are kept at zero press count; scheduling still uses only eligible SKUs.
    if not df_excluded.empty:
        excl_rows = []
        for _, row in df_excluded.iterrows():
            sku = str(row["SKUCode"])
            ct  = ct_map.get(sku, ConsumptionConfig.DEFAULT_CYCLE_TIME_MIN)
            qps = _qty_per_press_per_shift(ct)
            excl_rows.append({
                "SKUCode":                  sku,
                "Category":                 "Non-Runner-In",
                "Running_Press_Count":       0,
                "MouldLife_min":             0,
                "Effective_CT_Min":          ct,
                "Qty_Per_Press_Per_Shift":   qps,
                "Total_GT_Per_Shift_Day0":   0,
                "Demand_Qty":                int(row.get("Demand_Qty", 0)),
                "Priority_Score":            float(row.get("Priority_Score", 0)),
                "Skip_Reason":               str(row.get("Remark", "Not eligible")),
            })
        df_day0 = pd.concat([df_day0, pd.DataFrame(excl_rows)], ignore_index=True)
        print(f"  [Day0] Re-added {len(excl_rows)} excluded SKUs with Skip_Reason")

    # Include Runner-Out in Day 0 table for the dynamic file.
    # Runner-Out = presses currently running a NON-demand SKU.
    # Group df_running by SKU to get press count (one row per machine).
    demand_sku_set = set(df_classify["SKUCode"])
    df_ro_running = df_running[~df_running["SKUCode"].isin(demand_sku_set)].copy()

    if not df_ro_running.empty:
        ro_grouped = (
            df_ro_running.groupby("SKUCode")
            .agg(
                RunningPressCount=("Machine", "count"),
                MouldLife_min=("MouldLife_remaining", "min"),
            )
            .reset_index()
        )
        ct_map_ro = ct_resolver.resolve(ro_grouped["SKUCode"].tolist(), df_ct)
        ct_map.update(ct_map_ro)
        demand_lookup_all = dict(zip(df_demand["SKUCode"].str.strip(), df_demand["Quantity"]))
        priority_lookup_all = dict(zip(df_demand["SKUCode"].str.strip(), df_demand["Priority"]))
        ro_rows = []
        for _, r in ro_grouped.iterrows():
            sku = str(r["SKUCode"])
            ct  = ct_map.get(sku, ConsumptionConfig.DEFAULT_CYCLE_TIME_MIN)
            qps = _qty_per_press_per_shift(ct)
            n   = int(r["RunningPressCount"])
            ro_rows.append({
                "SKUCode":                  sku,
                "Category":                 "Runner-Out",
                "Running_Press_Count":       n,
                "MouldLife_min":             int(r["MouldLife_min"]),
                "Effective_CT_Min":          ct,
                "Qty_Per_Press_Per_Shift":   qps,
                "Total_GT_Per_Shift_Day0":   n * qps,
                "Demand_Qty":                demand_lookup_all.get(sku, 0),
                "Priority_Score":            priority_lookup_all.get(sku, 0),
                "Skip_Reason":               "Non-demand SKU — CO candidate",
            })
        df_day0 = pd.concat([df_day0, pd.DataFrame(ro_rows)], ignore_index=True)
        print(f"  [Day0] Added {len(ro_rows)} Runner-Out SKUs | "
              f"Total rows: {len(df_day0)} "
              f"({n_demand_raw} demand + {len(ro_rows)} non-demand CO candidates)")

    # Pass 1: CO schedule
    print("\n  [Pass 1] Computing CO schedule …")
    scheduler = COScheduler()
    co_events = scheduler.schedule(
        df_day0, df_demand, df_allowable, df_running, ct_map, max_co_per_day
    )

    # Pass 2: 31-day simulation
    print(f"\n  [Pass 2] Simulating {planning_days} days …")
    simulator = DaySimulator()
    daily_sheets = simulator.simulate(
        df_day0, df_demand, df_allowable, ct_map, co_events
    )

    # Print day-by-day summary
    print(f"\n  {'Day':<6} {'RI Presses':>11} {'NRI Presses':>12} "
          f"{'Total GT/Shift':>15} {'Demand Left':>12}")
    print("  " + "-" * 60)
    for d_idx, df_d in enumerate(daily_sheets, start=1):
        ri_presses  = df_d.loc[df_d["Category"] == "Runner-In",  "Running_Press_Count"].sum()
        nri_presses = df_d.loc[df_d["Category"] == "Non-Runner-In", "Running_Press_Count"].sum()
        total_gt    = df_d["Total_GT_Per_Shift_DayN"].sum()
        dem_left    = df_d["Updated_Demand_Qty"].sum()
        print(f"  Day {d_idx:02d} {int(ri_presses):>11,} {int(nri_presses):>12,} "
              f"{int(total_gt):>15,} {int(dem_left):>12,}")

    # Export
    print(f"\n  [Export] Writing Excel …")
    exporter = DynamicExporter()
    exporter.export(
        daily_sheets=daily_sheets,
        co_events=co_events,
        df_day0=df_day0,
        df_excluded=df_excluded,
        n_demand_raw=n_demand_raw,
        demand_path=demand_path,
        planning_days=planning_days,
        output_path=output_path,
    )

    return {
        "daily_sheets": daily_sheets,
        "co_events":    co_events,
        "ct_map":       ct_map,
        "df_day0":      df_day0,
        "df_excluded":  df_excluded,
    }


if __name__ == "__main__":
    import glob

    # Usage: python curing_consumption_dynamic.py [demand_file_path]
    # If no argument given, auto-detect from data/input/.
    if len(sys.argv) > 1:
        demand_path = sys.argv[1]
        if not os.path.exists(demand_path):
            raise SystemExit(f"Demand file not found: {demand_path}")
    else:
        # Auto-pick demand file.
        # Preference order: normalized XLSX with "demand" in name > other demand XLSX
        # > demand CSV > any XLSX > any CSV.  Within each tier, sort descending by
        # name so date-stamped files resolve to the most recent.
        # Exclude files whose name contains "BACKUP" or "backup".
        def _is_backup(p: str) -> bool:
            return "backup" in os.path.basename(p).lower()

        tiers = [
            [p for p in glob.glob(os.path.join(IN_DIR, "*may*.xlsx"))
             if not _is_backup(p)],
            [p for p in glob.glob(os.path.join(IN_DIR, "*demand*normalized*.xlsx"))
             if not _is_backup(p)],
            [p for p in glob.glob(os.path.join(IN_DIR, "*demand*.xlsx"))
             if not _is_backup(p)],
            [p for p in glob.glob(os.path.join(IN_DIR, "*demand*.csv"))
             if not _is_backup(p)],
            [p for p in glob.glob(os.path.join(IN_DIR, "*.xlsx"))
             if not _is_backup(p)],
            [p for p in glob.glob(os.path.join(IN_DIR, "*.csv"))
             if not _is_backup(p)],
        ]
        demand_path = None
        for tier in tiers:
            if tier:
                demand_path = sorted(tier, reverse=True)[0]
                break
        if demand_path is None:
            raise SystemExit(f"No demand file found in {IN_DIR}. "
                             "Place a demand .xlsx/.csv there and re-run.")

    run_dynamic_consumption(
        demand_path=demand_path,
        output_path=os.path.join(OUT_DIR, "curing_consumption_31day.xlsx"),
    )
