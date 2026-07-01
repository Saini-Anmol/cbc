"""
b2c_pipeline.py — End-to-end B2C scheduling pipeline.

Two modes:
  LEGACY (run_pipeline):
    Step 1: Curing Consumption (dynamic) — 31-day CO schedule
    Step 2: Building Scheduler (B2C)     — 31-day LP building plan
    Step 3: Curing Schedule (B2C)        — shift-wise curing from GT output

  ROLLING (run_rolling_pipeline):   <-- NEW DEFAULT
    Pre-compute: CO schedule + master data (once)
    For each Day D (1..31):
      1. Compute curing demand from actual press_state
      2. Greedy building assignment (projected GT — Option B)
      3. Per-shift: add building to GT inventory FIRST, then cure min(capacity, gt_available)
      4. GT shelf-life writeoff at end of day
      5. Apply CO transitions

    Output: SAME Excel files and sheet names as the legacy pipeline:
      Building → bc_building_schedule_{date}.xlsx
        Sheets: Shift Schedule | Changeover Plan | SKU Classification |
                Shift Schedule (Clean) | Daily GT & Carcass | Demand Fulfillment (B2C)
      Curing  → bc_curing_b2c.xlsx
        Sheets: Demand Fulfillment | Machine Utilization | Shift Schedule |
                Mould Tracker | Machine Schedule | Daily Cured tyres | GT Gap Diagnostic

All parameters are read from bc_config.py — edit there, not here.

Usage:
    python b2c_pipeline.py                           # rolling pipeline (new default)
    python b2c_pipeline.py data/input/demand_may.xlsx
    python b2c_pipeline.py --legacy                  # run old 31-day LP pipeline
"""

import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd

import cbc_env
from curing_consumption_dynamic import run_dynamic_consumption, ConsumptionConfig
from building_b2c import run_from_database_b2c
from curing_b2c import run_curing_b2c
from curing_consumption import ConsumptionETL

# ── All params from bc_config (single source of truth) ────────────────────────
from bc_config import (
    PLAN_START,
    PLANNING_DAYS,
    DEMAND_FILE,
    GT_SHELF_LIFE_DAYS,
    MAX_CHANGEOVERS_PER_DAY,
    MIN_CAMPAIGN_MINS,
    BUILD_LEAD_SHIFTS,
    GT_BUFFER_SHIFTS,
    MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT,
    BUILDING_CO_SAME_SIZE,
    BUILDING_CO_DIFF_SIZE,
    SHIFT_MINS,
    SHIFT_STARTS,
    SHIFT_ENDS,
    DYNAMIC_CC_OUTPUT  as CC_OUTPUT,
    BUILDING_OUTPUT    as BUILD_OUTPUT,
    CURING_B2C_OUTPUT  as CURING_OUTPUT,
)

# ── Machine group map ─────────────────────────────────────────────────────────
_MACHINE_GROUP: dict[str, str] = {}
for _m in ("6001","6002","6003","6004","7001","7002","7003","7004"):
    _MACHINE_GROUP[_m] = "VMI"
for _m in ("7101","7102","7103","7104","7105","7106","7201"):
    _MACHINE_GROUP[_m] = "BJ"
for _m in ("7501","7502","7503"):
    _MACHINE_GROUP[_m] = "UNISTAGE"
for _m in ("8201","8301","8302","8501","8502","7301"):
    _MACHINE_GROUP[_m] = "STAGE2"
for _m in ("6801","6802","6803","6909","6911","7601","7701",
           "7801","7802","7803","7804","8001","8002","8003","8101"):
    _MACHINE_GROUP[_m] = "STAGE1"

_S1_MACHINES = frozenset(m for m, g in _MACHINE_GROUP.items() if g == "STAGE1")

# ── Building machine CT (seconds/unit) ────────────────────────────────────────
_BLD_CT_SEC: dict[str, float] = {
    "7001":57.6,  "7002":57.6,  "7003":57.6,  "7004":57.6,
    "6001":60.0,  "6002":60.0,  "6003":60.0,  "6004":60.0,
    "7101":102.0, "7102":102.0, "7103":78.0,  "7104":108.0,
    "7105":108.0, "7106":57.6,  "7201":66.0,
    "7501":108.0, "7502":108.0, "7503":108.0,
    "8201":72.0,  "8301":78.0,  "8302":78.0,
    "8501":108.0, "8502":120.0, "7301":90.0,
    "6801":150,   "6802":218,   "6803":262,
    "6909":187,   "6911":150,   "7601":253,
    "7701":267,   "7801":163,   "7802":182,
    "7803":261,   "7804":257,   "8001":114,
    "8002":169,   "8003":113,   "8101":300,
}

DEFAULT_CURING_CT = ConsumptionConfig.DEFAULT_CYCLE_TIME_MIN
CURING_CAVITIES   = 2


# ══════════════════════════════════════════════════════════════════════════════
# ROLLING PIPELINE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _bld_qty_per_shift(machine: str) -> int:
    ct_min = _BLD_CT_SEC.get(str(machine), 120.0) / 60.0
    return int(SHIFT_MINS / ct_min)


def _cure_qty_per_shift(ct_min: float) -> int:
    return int(SHIFT_MINS / ct_min) * CURING_CAVITIES


def _co_cost(machine: str, from_inch: str, to_inch: str) -> int:
    mg = _MACHINE_GROUP.get(str(machine), "VMI")
    if from_inch == to_inch:
        return BUILDING_CO_SAME_SIZE.get(mg, 60)
    return BUILDING_CO_DIFF_SIZE.get(mg, 120)


def _assign_building_day(
    curing_demand:       dict,
    machine_skus:        dict,
    machine_current_sku: dict,
    sku_inch:            dict,
    demand_remaining:    dict,
    gt_inventory:        dict,
) -> dict:
    """
    Greedy per-day building assignment.

    Option B — projected GT:
      projected_gt accumulates as each machine commits. Later machines skip
      SKUs where projected supply already covers curing demand.

    Same-inch preference:
      same_size_CO candidates always tried before diff_size_CO.

    Returns: {machine: [(sku, qty_int, co_type_str)]}
      co_type: "start" | "same_size_CO" | "diff_size_CO"
    """
    MAX_COS = MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT * 3
    DAY_MINS = SHIFT_MINS * 3

    projected_gt: dict[str, float] = dict(gt_inventory)

    def _deficit(sku: str) -> float:
        need = curing_demand.get(sku, 0.0) * GT_BUFFER_SHIFTS
        gap  = need - projected_gt.get(sku, 0.0)
        cap  = max(0.0, demand_remaining.get(sku, 0.0))
        return min(max(0.0, gap), cap)

    _pri = {"VMI": 0, "BJ": 1, "UNISTAGE": 2, "STAGE2": 3, "STAGE1": 9}
    sorted_machines = sorted(
        machine_skus.keys(),
        key=lambda m: (_pri.get(_MACHINE_GROUP.get(m, ""), 9), m),
    )

    plan: dict[str, list] = {}

    for machine in sorted_machines:
        eligible = machine_skus.get(machine, set())
        if not any(_deficit(s) > 0 for s in eligible):
            continue

        remaining = DAY_MINS
        co_count  = 0
        cur_sku   = machine_current_sku.get(machine, "")
        cur_inch  = sku_inch.get(cur_sku, "")
        rate      = _bld_qty_per_shift(machine) / SHIFT_MINS
        campaigns: list[tuple] = []

        # Pass 1: serve current SKU (no CO)
        if cur_sku in eligible and _deficit(cur_sku) > 0:
            mins = min(remaining, _deficit(cur_sku) / rate if rate > 0 else remaining)
            qty  = int(mins * rate)
            if mins >= MIN_CAMPAIGN_MINS and qty > 0:
                campaigns.append((cur_sku, qty, "start"))
                projected_gt[cur_sku] = projected_gt.get(cur_sku, 0.0) + qty
                remaining -= mins

        # Pass 2: CO to other deficit SKUs, same-inch preferred
        while remaining >= MIN_CAMPAIGN_MINS and co_count < MAX_COS:
            same_cands: list = []
            diff_cands: list = []
            for sku in eligible:
                if sku == cur_sku or _deficit(sku) <= 0:
                    continue
                to_inch = sku_inch.get(sku, "")
                cost    = _co_cost(machine, cur_inch, to_inch)
                if cost > 0.20 * remaining or remaining - cost < MIN_CAMPAIGN_MINS:
                    continue
                bucket = same_cands if to_inch == cur_inch else diff_cands
                bucket.append((-_deficit(sku), cost, sku))

            if same_cands:
                same_cands.sort()
                _, best_cost, best_sku = same_cands[0]
                co_type = "same_size_CO"
            elif diff_cands:
                diff_cands.sort()
                _, best_cost, best_sku = diff_cands[0]
                co_type = "diff_size_CO"
            else:
                break

            avail = remaining - best_cost
            mins  = min(avail, _deficit(best_sku) / rate if rate > 0 else avail)
            qty   = int(mins * rate)
            if mins < MIN_CAMPAIGN_MINS or qty <= 0:
                break

            campaigns.append((best_sku, qty, co_type))
            projected_gt[best_sku] = projected_gt.get(best_sku, 0.0) + qty
            remaining -= (best_cost + mins)
            co_count  += 1
            cur_sku    = best_sku
            cur_inch   = sku_inch.get(cur_sku, "")

        if campaigns:
            plan[machine] = campaigns

    return plan


def _writeoff_stale_gt(gt_inventory, last_build_day, current_day, shelf_days=GT_SHELF_LIFE_DAYS):
    total = 0.0
    for sku in list(gt_inventory.keys()):
        qty = gt_inventory[sku]
        if qty > 0 and (current_day - last_build_day.get(sku, 0)) > shelf_days:
            total += qty
            gt_inventory[sku] = 0.0
    return total


# ══════════════════════════════════════════════════════════════════════════════
# OUTPUT WRITERS — same sheet names as legacy pipeline
# ══════════════════════════════════════════════════════════════════════════════

def _xl_header(ws, row: int, cols: list, bg="1F3864", fg="FFFFFF"):
    from openpyxl.styles import PatternFill, Font, Alignment
    fill = PatternFill("solid", fgColor=bg)
    font = Font(bold=True, size=10, color=fg)
    aln  = Alignment(horizontal="center", vertical="center")
    for ci, h in enumerate(cols, 1):
        c = ws.cell(row=row, column=ci, value=h)
        c.fill, c.font, c.alignment = fill, font, aln


def _xl_fill(ws, row_num: int, n_cols: int, hex_color: str):
    from openpyxl.styles import PatternFill
    fill = PatternFill("solid", fgColor=hex_color)
    for ci in range(1, n_cols + 1):
        ws.cell(row=row_num, column=ci).fill = fill


def _write_rolling_building_excel(
    output_path: str,
    bld_shift_rows: list,          # per-shift rows (includes CO sentinels)
    bld_co_events: list,           # building machine CO events
    df_day0: "pd.DataFrame",       # Day 0 curing consumption (SKU classification)
    sku_machine_map: dict,         # {sku: set(machines)} for eligibility
    opening_gt: dict,              # opening GT inventory
    demand_dict: dict,             # {sku: demand_qty} from demand file
    planning_days: int,
) -> None:
    """
    Write building Excel matching the legacy bc_building_schedule output.

    Sheets:
      1. Shift Schedule         — per-shift rows (title at row 1, blank at row 2, header at row 3)
      2. Changeover Plan        — building machine CO events
      3. SKU Classification     — category summary from Day 0 consumption
      4. Shift Schedule (Clean) — production-only rows (no CO sentinels)
      5. Daily GT & Carcass     — daily GT and carcass totals
      6. Demand Fulfillment (B2C) — per-SKU demand vs planned GT
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    _NAVY  = "1F3864"; _WHITE = "FFFFFF"; _GREEN = "E2EFDA"
    _AMBER = "FFF2CC"; _RED   = "FFE0E0"; _GREY  = "D3D3D3"
    _CO    = "FFC000"

    def _fill(h):   return PatternFill("solid", fgColor=h)
    def _bold(s=10): return Font(bold=True, size=s)
    def _ctr():      return Alignment(horizontal="center", vertical="center")

    _SENTINEL = {"CHANGEOVER", "MOULD_CLEAN", "C/O", "CO"}

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    # ── Sheet 1: Shift Schedule (header at row 3 — matches legacy header=2 read) ─
    ws = wb.create_sheet("Shift Schedule")
    ws.cell(row=1, column=1, value="BC Building Schedule (Rolling Pipeline)").font = _bold(12)
    # Row 2: blank
    # Row 3: headers
    bld_cols = ["Machine", "Date", "Shift", "SKUCode", "Qty", "Machine_Group", "CO_Type"]
    _xl_header(ws, 3, bld_cols)
    for ri, row in enumerate(bld_shift_rows, 4):
        is_co = str(row.get("SKUCode", "")).upper() in _SENTINEL
        for ci, col in enumerate(bld_cols, 1):
            cell = ws.cell(row=ri, column=ci, value=row.get(col, ""))
            cell.alignment = _ctr()
            if is_co:
                cell.fill = _fill(_CO)
                cell.font = Font(bold=True)
    for col in ws.columns:
        w = max((len(str(c.value or "")) for c in col), default=8)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(w + 2, 38)

    # ── Sheet 2: Changeover Plan ───────────────────────────────────────────────
    ws_co = wb.create_sheet("Changeover Plan")
    co_cols = ["Machine", "Date", "Day", "From_SKU", "Target_SKU",
               "CO_Type", "CO_Cost_Mins", "CO_Day_Index", "Status"]
    _xl_header(ws_co, 1, co_cols)
    for ri, row in enumerate(bld_co_events, 2):
        for ci, col in enumerate(co_cols, 1):
            ws_co.cell(row=ri, column=ci, value=row.get(col, "")).alignment = _ctr()
    for col in ws_co.columns:
        w = max((len(str(c.value or "")) for c in col), default=8)
        ws_co.column_dimensions[get_column_letter(col[0].column)].width = min(w + 2, 38)

    # ── Sheet 3: SKU Classification ────────────────────────────────────────────
    ws_cat = wb.create_sheet("SKU Classification")
    cat_counts: dict[str, dict] = defaultdict(lambda: {"SKU_Count": 0, "Total_Demand": 0, "Avg_Priority": []})
    if df_day0 is not None and not df_day0.empty:
        for _, r in df_day0.iterrows():
            cat = str(r.get("Category", "Unknown"))
            sku = str(r.get("SKUCode", ""))
            dem = float(r.get("Demand_Qty", 0) or 0)
            pri = float(r.get("Priority_Score", 0) or 0)
            cat_counts[cat]["SKU_Count"]    += 1
            cat_counts[cat]["Total_Demand"] += dem
            cat_counts[cat]["Avg_Priority"].append(pri)
    cat_data = [
        {"Category": cat, "SKU_Count": v["SKU_Count"],
         "Total_Demand": int(v["Total_Demand"]),
         "Avg_Priority": round(sum(v["Avg_Priority"]) / max(len(v["Avg_Priority"]), 1), 4)}
        for cat, v in sorted(cat_counts.items())
    ]
    cat_cols = ["Category", "SKU_Count", "Total_Demand", "Avg_Priority"]
    _xl_header(ws_cat, 1, cat_cols)
    for ri, row in enumerate(cat_data, 2):
        for ci, col in enumerate(cat_cols, 1):
            ws_cat.cell(row=ri, column=ci, value=row.get(col, "")).alignment = _ctr()
    # KPI footer
    n_bld_co = sum(1 for r in bld_shift_rows if str(r.get("SKUCode","")).upper() in _SENTINEL)
    ws_cat.cell(row=len(cat_data) + 3, column=1, value="Building COs scheduled").font = _bold()
    ws_cat.cell(row=len(cat_data) + 3, column=2, value=n_bld_co)
    ws_cat.column_dimensions["A"].width = 22
    for ltr in "BCD":
        ws_cat.column_dimensions[ltr].width = 16

    # ── Sheet 4: Shift Schedule (Clean) — production rows only ────────────────
    ws_clean = wb.create_sheet("Shift Schedule (Clean)")
    _xl_header(ws_clean, 1, bld_cols)
    prod_rows = [r for r in bld_shift_rows if str(r.get("SKUCode","")).upper() not in _SENTINEL]
    for ri, row in enumerate(prod_rows, 2):
        for ci, col in enumerate(bld_cols, 1):
            ws_clean.cell(row=ri, column=ci, value=row.get(col, "")).alignment = _ctr()
    for col in ws_clean.columns:
        w = max((len(str(c.value or "")) for c in col), default=8)
        ws_clean.column_dimensions[get_column_letter(col[0].column)].width = min(w + 2, 38)

    # ── Sheet 5: Daily GT & Carcass ────────────────────────────────────────────
    ws_daily = wb.create_sheet("Daily GT & Carcass")
    daily_agg: dict[str, dict] = defaultdict(lambda: {"GT_Produced": 0, "Carcass_Produced": 0,
                                                       "Total_Units": 0, "Active_SKUs": set()})
    for row in prod_rows:
        d    = str(row.get("Date", ""))
        mach = str(row.get("Machine", ""))
        qty  = int(row.get("Qty", 0) or 0)
        sku  = str(row.get("SKUCode", ""))
        if mach in _S1_MACHINES:
            daily_agg[d]["Carcass_Produced"] += qty
        else:
            daily_agg[d]["GT_Produced"] += qty
        daily_agg[d]["Total_Units"]  += qty
        daily_agg[d]["Active_SKUs"].add(sku)
    daily_cols = ["Date", "GT_Produced", "Carcass_Produced", "Total_Units",
                  "Active_SKUs", "Cumulative_GT"]
    _xl_header(ws_daily, 1, daily_cols)
    cum_gt = 0
    for ri, (date, v) in enumerate(sorted(daily_agg.items()), 2):
        cum_gt += v["GT_Produced"]
        vals = [date, v["GT_Produced"], v["Carcass_Produced"],
                v["Total_Units"], len(v["Active_SKUs"]), cum_gt]
        for ci, val in enumerate(vals, 1):
            ws_daily.cell(row=ri, column=ci, value=val).alignment = _ctr()
    ws_daily.column_dimensions["A"].width = 14
    for ltr in "BCDEF":
        ws_daily.column_dimensions[ltr].width = 16

    # ── Sheet 6: Demand Fulfillment (B2C) ─────────────────────────────────────
    ws_dem = wb.create_sheet("Demand Fulfillment (B2C)")
    prod_by_sku: dict[str, int] = defaultdict(int)
    for row in prod_rows:
        sku = str(row.get("SKUCode", ""))
        if sku and sku.upper() not in _SENTINEL and str(row.get("Machine","")) not in _S1_MACHINES:
            prod_by_sku[sku] += int(row.get("Qty", 0) or 0)

    dem_cols = ["SKUCode", "Category", "Priority", "Demand", "GT_Inventory",
                "Planned_Units", "Gap", "Fulfillment_Pct", "Status",
                "Eligible_Machines", "Skip_Reason"]
    _xl_header(ws_dem, 1, dem_cols)

    cat_map_d0: dict[str, str]   = {}
    pri_map_d0: dict[str, float] = {}
    if df_day0 is not None and not df_day0.empty:
        for _, r in df_day0.iterrows():
            s = str(r.get("SKUCode","")).strip()
            cat_map_d0[s] = str(r.get("Category",""))
            pri_map_d0[s] = float(r.get("Priority_Score", 0) or 0)

    dem_rows_out = []
    for sku, dem in sorted(demand_dict.items(), key=lambda x: -x[1]):
        planned  = float(prod_by_sku.get(sku, 0))
        gap      = max(0, int(dem) - int(planned))
        fill_pct = round(100 * planned / dem, 1) if dem > 0 else 0.0
        status   = ("FULLY MET" if planned >= dem * 0.95
                    else "PARTIAL" if planned > 0 else "UNMET")
        dem_rows_out.append({
            "SKUCode": sku, "Category": cat_map_d0.get(sku, ""),
            "Priority": round(pri_map_d0.get(sku, 0), 7),
            "Demand": int(dem),
            "GT_Inventory": int(opening_gt.get(sku, 0)),
            "Planned_Units": int(planned), "Gap": gap,
            "Fulfillment_Pct": f"{fill_pct}%", "Status": status,
            "Eligible_Machines": len(sku_machine_map.get(sku, set())),
            "Skip_Reason": "" if planned > 0 else (
                "No eligible building machine" if not sku_machine_map.get(sku) else ""),
        })
    status_colors = {"FULLY MET": _GREEN, "PARTIAL": _AMBER, "UNMET": _RED}
    for ri, row in enumerate(dem_rows_out, 2):
        color = status_colors.get(row["Status"], _GREY)
        for ci, col in enumerate(dem_cols, 1):
            cell = ws_dem.cell(row=ri, column=ci, value=row.get(col, ""))
            cell.fill = _fill(color)
            cell.alignment = _ctr()
    ws_dem.column_dimensions["A"].width = 34
    for ltr in "BCDEFGHIJK":
        ws_dem.column_dimensions[ltr].width = 15

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    wb.save(output_path)
    print(f"  [Rolling] Building output → {output_path}")


def _write_rolling_curing_excel(
    output_path: str,
    cure_shift_rows: list,         # per-shift press events
    press_stats: dict,             # {press: {running_mins, co_mins, clean_mins, skus, cycles, units}}
    press_sku_stats: dict,         # {(press,sku): {cycles, units, mins_used}}
    daily_cured: dict,             # {date_str: qty}
    sku_cured: dict,               # {sku: qty}
    closing_gt_bal: dict,          # {sku: gt_remaining}
    build_by_shift_sku: dict,      # {(date,shift): {sku: qty}} — for GT diagnostic
    opening_gt: dict,
    demand_dict: dict,             # {sku: demand_qty}
    cure_ct_map: dict,             # {sku: ct_min}
    curing_allowable: dict,        # {sku: [press_ids]}
    planning_days: int,
    plan_start: datetime,
) -> None:
    """
    Write curing Excel matching the legacy bc_curing_b2c output.

    Sheets:
      1. Demand Fulfillment  — per-SKU demand vs cured + fulfillment %
      2. Machine Utilization — per-press running / idle / CO minutes + utilization %
      3. Shift Schedule      — per-shift press events (RUNNING / CO / MOULD_CLEAN)
      4. Mould Tracker       — placeholder (mould cycle not tracked in rolling)
      5. Machine Schedule    — per (press, SKU) summary
      6. Daily Cured tyres   — daily cured totals
      7. GT Gap Diagnostic   — closing GT balance by SKU
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    _GREEN = "C6EFCE"; _AMBER = "FFEB9C"; _RED   = "FFC7CE"; _LGREY = "D9D9D9"
    _NAVY  = "1F3864"; _WHITE = "FFFFFF"; _BLUE  = "DCE6F1"; _LYELL = "FFF2CC"
    _DGREY = "F2F2F2"; _ORANGE= "FFC000"

    def _fill(h): return PatternFill("solid", fgColor=h)
    def _bold(s=10, color="000000"): return Font(bold=True, size=s, color=color)
    def _ctr(): return Alignment(horizontal="center", vertical="center", wrap_text=True)

    def _hdr(ws, row, cols, bg=_NAVY, fg=_WHITE):
        for ci, h in enumerate(cols, 1):
            c = ws.cell(row=row, column=ci, value=h)
            c.fill = _fill(bg); c.font = _bold(10, fg); c.alignment = _ctr()

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    avail_mins = planning_days * 3 * SHIFT_MINS

    # ── Sheet 1: Demand Fulfillment ───────────────────────────────────────────
    ws = wb.create_sheet("Demand Fulfillment")
    cols = ["SKUCode", "Priority", "Demand", "GT_Inventory", "Planned_Units",
            "Gap", "Fulfillment_Pct", "Status", "CycleTime_min",
            "Eligible_Machines", "Presses_Needed", "Skip_Reason"]
    _hdr(ws, 1, cols)
    status_fill = {"FULLY MET": _GREEN, "PARTIAL": _AMBER, "UNMET": _RED, "NO DATA": _LGREY}
    rows_out = []
    for sku, dem in sorted(demand_dict.items(), key=lambda x: -x[1]):
        planned = float(sku_cured.get(sku, 0))
        gap     = max(0, dem - planned)
        pct     = planned / dem if dem > 0 else 0.0
        ct      = cure_ct_map.get(sku, DEFAULT_CURING_CT)
        cap_day = _cure_qty_per_shift(ct) * 3 * planning_days
        p_needed= max(1, round(dem / cap_day)) if cap_day > 0 else "-"
        status  = ("FULLY MET" if planned >= dem * 0.999
                   else "PARTIAL" if planned > 0
                   else ("NO DATA" if dem <= 0 else "UNMET"))
        rows_out.append({
            "SKUCode": sku, "Priority": 1.0, "Demand": int(dem),
            "GT_Inventory": int(opening_gt.get(sku, 0)),
            "Planned_Units": int(planned), "Gap": int(gap),
            "Fulfillment_Pct": pct, "Status": status,
            "CycleTime_min": round(ct, 2),
            "Eligible_Machines": len(curing_allowable.get(sku, [])),
            "Presses_Needed": p_needed, "Skip_Reason": "",
        })
    for ri, r in enumerate(rows_out, 2):
        f = _fill(status_fill.get(r["Status"], _WHITE))
        for ci, h in enumerate(cols, 1):
            cell = ws.cell(row=ri, column=ci, value=r[h])
            cell.fill = f; cell.alignment = _ctr()
            if h == "Fulfillment_Pct":
                cell.number_format = "0.0%"
    tr = len(rows_out) + 3
    ws.cell(row=tr, column=1, value="TOTAL").font = _bold(11)
    ws.cell(row=tr, column=3, value=sum(r["Demand"]        for r in rows_out)).font = _bold(11)
    ws.cell(row=tr, column=5, value=sum(r["Planned_Units"] for r in rows_out)).font = _bold(11)
    ws.cell(row=tr, column=6, value=sum(r["Gap"]           for r in rows_out)).font = _bold(11)
    tot_d = sum(r["Demand"] for r in rows_out); tot_p = sum(r["Planned_Units"] for r in rows_out)
    tc = ws.cell(row=tr, column=7, value=tot_p / tot_d if tot_d else 0)
    tc.font = _bold(11); tc.number_format = "0.0%"
    ws.column_dimensions["A"].width = 34
    for ltr in "BCDEFGHIJKL": ws.column_dimensions[ltr].width = 15
    ws.freeze_panes = "A2"

    # ── Sheet 2: Machine Utilization ──────────────────────────────────────────
    ws = wb.create_sheet("Machine Utilization")
    all_presses = sorted(press_stats)
    if all_presses:
        avg_u = sum(press_stats[p]["running_mins"] / avail_mins for p in all_presses) / len(all_presses)
        high  = sum(1 for p in all_presses if press_stats[p]["running_mins"] / avail_mins >= 0.90)
        low   = sum(1 for p in all_presses if press_stats[p]["running_mins"] / avail_mins < 0.05)
        ws.cell(row=1, column=1,
                value=f"Avg util: {avg_u:.1%}  |  High(≥90%): {high}  |  Idle(<5%): {low}  |  Presses: {len(all_presses)}"
                ).font = _bold(10)
    u_cols = ["Machine", "Available_Mins", "Used_Mins", "Idle_Mins",
              "Utilization_Pct", "SKUs_Count", "Total_Cycles", "Total_Units"]
    _hdr(ws, 2, u_cols)
    for ri, press in enumerate(all_presses, 3):
        s    = press_stats[press]
        used = s["running_mins"]
        idle = max(0, avail_mins - used - s["co_mins"] - s["clean_mins"])
        pct  = used / avail_mins if avail_mins else 0.0
        color = _GREEN if pct >= 0.90 else (_AMBER if pct >= 0.60 else _RED)
        vals  = [press, avail_mins, round(used), round(idle), pct,
                 len(s["skus"]), s["cycles"], s["units"]]
        for ci, v in enumerate(vals, 1):
            cell = ws.cell(row=ri, column=ci, value=v)
            cell.fill = _fill(color); cell.alignment = _ctr()
            if ci == 5: cell.number_format = "0.0%"
    for ltr in "ABCDEFGH": ws.column_dimensions[ltr].width = 17
    ws.freeze_panes = "A3"

    # ── Sheet 3: Shift Schedule ────────────────────────────────────────────────
    ws = wb.create_sheet("Shift Schedule")
    ss_cols = ["Date", "Shift", "Machine", "SKUCode", "StartTime", "EndTime",
               "Qty", "CycleTime_min", "GT_Inventory", "Remarks"]
    _hdr(ws, 1, ss_cols)
    s_fill = {"A": _fill(_BLUE), "B": _fill(_LYELL), "C": _fill(_DGREY)}
    for ri, r in enumerate(cure_shift_rows, 2):
        st = r.get("_status", "RUNNING")
        if st == "CHANGEOVER":
            f = _fill(_ORANGE)
        elif st == "MOULD_CLEAN":
            f = _fill(_AMBER)
        else:
            f = s_fill.get(r.get("Shift", ""), _fill(_WHITE))
        for ci, h in enumerate(ss_cols, 1):
            cell = ws.cell(row=ri, column=ci, value=r.get(h, ""))
            cell.fill = f; cell.alignment = _ctr()
            if st in ("CHANGEOVER", "MOULD_CLEAN"):
                cell.font = Font(bold=True)
    ws.column_dimensions["A"].width = 14; ws.column_dimensions["D"].width = 32
    ws.column_dimensions["I"].width = 16; ws.freeze_panes = "A2"

    # ── Sheet 4: Mould Tracker (placeholder) ─────────────────────────────────
    ws = wb.create_sheet("Mould Tracker")
    mt_cols = ["MouldNo", "Compatible_SKUs", "Life_Remaining", "Assigned_Machine"]
    _hdr(ws, 1, mt_cols)
    ws.cell(row=2, column=1,
            value="Mould tracking not available in rolling pipeline — check curing_b2c output"
            ).font = Font(italic=True, color="888888")
    ws.column_dimensions["A"].width = 20; ws.column_dimensions["B"].width = 44

    # ── Sheet 5: Machine Schedule ─────────────────────────────────────────────
    ws = wb.create_sheet("Machine Schedule")
    ms_rows = []
    for (press, sku), s in sorted(press_sku_stats.items()):
        if s["units"] == 0:
            continue
        ct       = cure_ct_map.get(sku, DEFAULT_CURING_CT)
        days_used = s["mins_used"] / (3 * SHIFT_MINS) if s["mins_used"] else 0
        ms_rows.append({
            "Machine": press, "SKUCode": sku,
            "CycleTime_min": round(ct, 2),
            "Cycles": s["cycles"], "Units_Planned": s["units"],
            "Mins_Used": round(s["mins_used"]), "Days_Used": round(days_used, 2),
        })
    ms_rows.sort(key=lambda r: (r["Machine"], -r["Units_Planned"]))
    tot_u = sum(r["Units_Planned"] for r in ms_rows)
    tot_c = sum(r["Cycles"] for r in ms_rows)
    ws.cell(row=1, column=1,
            value=f"Press-SKU pairs: {len(ms_rows)}  |  Total Units: {tot_u:,}  |  Total Cycles: {tot_c:,}"
            ).font = _bold(10)
    ms_cols = ["Machine", "SKUCode", "CycleTime_min", "Cycles",
               "Units_Planned", "Mins_Used", "Days_Used"]
    _hdr(ws, 2, ms_cols)
    for ri, r in enumerate(ms_rows, 3):
        for ci, h in enumerate(ms_cols, 1):
            ws.cell(row=ri, column=ci, value=r.get(h, "")).alignment = _ctr()
    ws.column_dimensions["A"].width = 12; ws.column_dimensions["B"].width = 34
    for ltr in "CDEFG": ws.column_dimensions[ltr].width = 14
    ws.freeze_panes = "A3"

    # ── Sheet 6: Daily Cured tyres ────────────────────────────────────────────
    ws = wb.create_sheet("Daily Cured tyres")
    _hdr(ws, 1, ["Date", "Cured_Qty"])
    total_c = 0
    for d in range(planning_days):
        date_str = (plan_start + timedelta(days=d)).strftime("%Y-%m-%d")
        qty      = int(daily_cured.get(date_str, 0))
        ws.cell(row=d + 2, column=1, value=date_str).alignment = _ctr()
        c = ws.cell(row=d + 2, column=2, value=qty)
        c.alignment = _ctr()
        c.fill = _fill(_BLUE) if qty > 0 else _fill(_RED)
        total_c += qty
    tr = planning_days + 3
    ws.cell(row=tr, column=1, value="TOTAL").font = _bold(11)
    t = ws.cell(row=tr, column=2, value=total_c)
    t.font = _bold(11); t.fill = _fill(_GREEN)
    ws.column_dimensions["A"].width = 14; ws.column_dimensions["B"].width = 14

    # ── Sheet 7: GT Gap Diagnostic ────────────────────────────────────────────
    ws = wb.create_sheet("GT Gap Diagnostic")
    _hdr(ws, 1, ["SKUCode", "GT_Built", "GT_Cured", "Closing_Balance", "Reason"])
    built_per_sku: dict[str, float] = defaultdict(float)
    for sku_qty in build_by_shift_sku.values():
        for sku, qty in sku_qty.items():
            built_per_sku[str(sku)] += float(qty)
    for sku, qty in opening_gt.items():
        built_per_sku[str(sku)] += float(qty)
    press_skus = set(sku_cured.keys())
    ri = 2
    for sku in sorted(closing_gt_bal, key=lambda s: -closing_gt_bal[s]):
        bal = closing_gt_bal[sku]
        if bal < 0.5:
            continue
        built  = built_per_sku.get(sku, 0.0)
        cured  = float(sku_cured.get(sku, 0))
        if sku not in press_skus:
            reason = "NO_PRESS"; fill = _fill(_RED)
        elif cured > 0:
            reason = "DEMAND_MET"; fill = _fill(_AMBER)
        else:
            reason = "RESIDUAL"; fill = _fill(_LGREY)
        for ci, val in enumerate([sku, round(built), round(cured), round(bal), reason], 1):
            c = ws.cell(row=ri, column=ci, value=val)
            if ci == 4: c.fill = fill
            c.alignment = _ctr()
        ri += 1
    ws.column_dimensions["A"].width = 34
    for ltr in "BCDE": ws.column_dimensions[ltr].width = 16

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    wb.save(output_path)
    print(f"  [Rolling] Curing output  → {output_path}")


# ══════════════════════════════════════════════════════════════════════════════
# ROLLING PIPELINE — main function
# ══════════════════════════════════════════════════════════════════════════════

def run_rolling_pipeline(
    demand_path:    str | None = None,
    plan_start:     datetime | None = None,
    planning_days:  int | None = None,
    build_output:   str | None = None,
    curing_output:  str | None = None,
) -> dict:
    """
    Rolling day-by-day B2C pipeline.

    Generates building and curing schedules simultaneously:
      - Building machines are assigned based on actual GT deficit each day
      - Curing presses cure only what GT is available (GT-limited)
      - Both schedules written to the same Excel format as the legacy pipeline
    """
    demand_path   = demand_path   or DEMAND_FILE
    plan_start    = plan_start    or PLAN_START
    planning_days = planning_days or PLANNING_DAYS
    build_output  = build_output  or BUILD_OUTPUT
    curing_output = curing_output or CURING_OUTPUT

    print("\n" + "=" * 70)
    print("  ROLLING PIPELINE — Pre-computation")
    print("=" * 70)

    # ── A: CO schedule ────────────────────────────────────────────────────────
    print("  [Rolling] Computing CO schedule …")
    cc_result = run_dynamic_consumption(
        demand_path=demand_path, output_path=CC_OUTPUT,
        plan_start=plan_start, planning_days=planning_days,
        max_co_per_day=MAX_CHANGEOVERS_PER_DAY,
    )
    co_events = cc_result["co_events"]
    df_day0   = cc_result["df_day0"]
    print(f"  [Rolling] {len(co_events)} curing CO events pre-computed")

    co_by_day: dict[int, list] = defaultdict(list)
    for ev in co_events:
        co_by_day[int(ev["day"])].append((ev["press"], ev["old_sku"], ev["new_sku"]))

    # ── B: Master data ────────────────────────────────────────────────────────
    from cbc_env import make_engine
    engine = make_engine()

    cetl = ConsumptionETL(engine)
    df_ct_raw = cetl.load_cycle_times()
    cure_ct_map: dict[str, float] = {
        str(r["SKUCode"]): float(r["CT_Min"])
        for _, r in df_ct_raw.iterrows() if r.get("CT_Min")
    }

    # Building CTs are hardcoded in _BLD_CT_SEC above (sourced from plant data)

    machine_skus: dict[str, set]  = defaultdict(set)
    sku_machine_map: dict[str, set] = defaultdict(set)
    sku_inch: dict[str, str] = {}
    try:
        from building_b2c import B2C_ETL as _BETL
        _etl = _BETL(engine)
        df_allow    = _etl.load_machine_allowable()
        sku_to_size = _etl.load_sku_sizes()
        sku_inch = {str(k): str(v).strip().replace('"', "") for k, v in sku_to_size.items()}

        _HARD = {
            "7001":{"16"},       "6001":{"14"},        "7002":{"14"},   "7004":{"14"},
            "6002":{"15"},       "7003":{"15"},        "6003":{"17","18"}, "6004":{"16"},
            "7101":{"15"},       "7102":{"14","15"},   "7103":{"13"},   "7104":{"14","15"},
            "7105":{"13"},       "7106":{"13"},        "7201":{"16"},
            "7501":{"12"},       "7502":{"13"},        "7503":{"13"},
        }
        for idx, row in df_allow.iterrows():
            sku = str(row["SKUCode"]); si = sku_inch.get(sku, "")
            ml  = list(row.get("Machines", []) or [])
            df_allow.at[idx, "Machines"] = [
                m for m in ml if str(m) not in _HARD or si in _HARD[str(m)]
            ]
        for _, row in df_allow.iterrows():
            sku = str(row["SKUCode"])
            for m in (row.get("Machines") or []):
                machine_skus[str(m)].add(sku)
                sku_machine_map[sku].add(str(m))
        print(f"  [Rolling] Allowable map: {len(machine_skus)} machines")
    except Exception as _e:
        print(f"  [Rolling] Allowable map: failed ({_e})")

    # ── C: Press state ────────────────────────────────────────────────────────
    df_moulds = cetl.load_running_moulds()
    press_state: dict[str, dict] = {}
    for _, r in df_moulds.iterrows():
        press_state[str(r["Machine"])] = {"sku": str(r["SKUCode"]), "status": "RUNNING"}

    press_count: dict[str, int] = defaultdict(int)
    for st in press_state.values():
        press_count[st["sku"]] += 1

    # Curing allowable: {sku: [press_ids]} for demand fulfillment sheet
    curing_allowable: dict[str, list] = defaultdict(list)
    for press, st in press_state.items():
        curing_allowable[st["sku"]].append(press)

    # ── D: Opening GT inventory ───────────────────────────────────────────────
    try:
        from curing_b2c import _load_opening_gt
        opening_gt = _load_opening_gt(engine)
    except Exception:
        opening_gt = {}
    gt_inventory: dict[str, float] = defaultdict(float, opening_gt)

    # ── E: Demand ─────────────────────────────────────────────────────────────
    demand_df = pd.read_excel(demand_path)
    sku_col = next((c for c in demand_df.columns if "SKU"  in str(c)), demand_df.columns[0])
    qty_col = next(
        (c for c in demand_df.columns
         if any(x in str(c) for x in ("Requirement","Demand","Qty","Quantity"))),
        demand_df.columns[1],
    )
    demand_dict: dict[str, float] = {
        str(r[sku_col]): float(r[qty_col] or 0)
        for _, r in demand_df.iterrows() if pd.notna(r.get(qty_col))
    }
    demand_remaining: dict[str, float] = dict(demand_dict)
    total_demand = sum(demand_dict.values())
    print(f"  [Rolling] Demand: {len(demand_dict)} SKUs, {total_demand:,.0f} units")

    # ── F: Machine current SKU ────────────────────────────────────────────────
    machine_current_sku: dict[str, str] = {}
    try:
        df_running_bld = _etl.load_running_machines()
        machine_current_sku = {str(r["Machine"]): str(r["SKUCode"]) for _, r in df_running_bld.iterrows()}
    except Exception:
        pass

    # ══════════════════════════════════════════════════════════════════════════
    # Data accumulators (matching output sheet formats)
    # ══════════════════════════════════════════════════════════════════════════
    bld_shift_rows:  list[dict] = []   # building Shift Schedule rows (+ CO sentinels)
    bld_co_events:   list[dict] = []   # building machine CO events
    cure_shift_rows: list[dict] = []   # curing Shift Schedule rows
    press_stats:     dict = defaultdict(lambda: {
        "running_mins": 0.0, "co_mins": 0.0, "clean_mins": 0.0,
        "skus": set(), "cycles": 0, "units": 0,
    })
    press_sku_stats: dict = defaultdict(lambda: {"cycles": 0, "units": 0, "mins_used": 0.0})
    daily_cured:     dict[str, int] = defaultdict(int)
    sku_cured:       dict[str, int] = defaultdict(int)
    build_by_shift_sku: dict = {}      # {(date,shift): {sku: qty}} for GT diagnostic
    last_build_day:  dict[str, int] = {}
    daily_summary:   list[dict] = []
    writeoff_total   = 0.0
    SHIFTS           = ["A", "B", "C"]

    print("\n" + "=" * 70)
    print("  ROLLING PIPELINE — Day-by-day simulation")
    print("=" * 70)

    for day in range(1, planning_days + 1):
        date     = plan_start + timedelta(days=day - 1)
        date_str = date.strftime("%Y-%m-%d")
        today_cos = co_by_day.get(day, [])
        co_press_map: dict[str, str] = {p: ns for p, _, ns in today_cos}

        # ── 1. Curing demand ─────────────────────────────────────────────
        curing_demand: dict[str, float] = defaultdict(float)
        for press, st in press_state.items():
            if st["status"] != "RUNNING":
                continue
            sku = st["sku"]
            ct  = cure_ct_map.get(sku, DEFAULT_CURING_CT)
            if press in co_press_map:
                new_sku = co_press_map[press]
                new_ct  = cure_ct_map.get(new_sku, DEFAULT_CURING_CT)
                curing_demand[new_sku] += _cure_qty_per_shift(new_ct) * 1  # Shift C only
            else:
                curing_demand[sku] += _cure_qty_per_shift(ct) * 3

        # ── 2. Building assignment (Option B: projected GT) ───────────────
        daily_plan = _assign_building_day(
            curing_demand=dict(curing_demand),
            machine_skus=machine_skus,
            machine_current_sku=machine_current_sku,
            sku_inch=sku_inch,
            demand_remaining=demand_remaining,
            gt_inventory=gt_inventory,
        )

        # ── 3. Distribute build across shifts + record CO events ──────────
        bld_plan_by_shift: dict[tuple, list] = {(date_str, s): [] for s in SHIFTS}
        for machine, campaigns in daily_plan.items():
            prev_sku = machine_current_sku.get(machine, "")
            prev_inch = sku_inch.get(prev_sku, "")
            for sku, qty, co_type in campaigns:
                # Insert building CO sentinel row (once, in Shift A)
                if co_type != "start":
                    co_mins = _co_cost(machine, prev_inch, sku_inch.get(sku, ""))
                    bld_shift_rows.append({
                        "Machine": machine, "Date": date_str, "Shift": "A",
                        "SKUCode": "CHANGEOVER", "Qty": co_mins,
                        "Machine_Group": _MACHINE_GROUP.get(machine, ""),
                        "CO_Type": co_type,
                    })
                    bld_co_events.append({
                        "Machine": machine, "Date": date_str,
                        "Day": day, "CO_Day_Index": day,
                        "From_SKU": prev_sku, "Target_SKU": sku,
                        "CO_Type": co_type, "CO_Cost_Mins": co_mins,
                        "Status": f"Rolling CO ({co_type})",
                    })
                # Distribute production across 3 shifts
                qps = qty // 3; rem = qty - qps * 3
                for s_idx, shift in enumerate(SHIFTS):
                    q = qps + (1 if s_idx < rem else 0)
                    if q > 0:
                        bld_plan_by_shift[(date_str, shift)].append((machine, sku, q, co_type))
                        bld_shift_rows.append({
                            "Machine": machine, "Date": date_str, "Shift": shift,
                            "SKUCode": sku, "Qty": q,
                            "Machine_Group": _MACHINE_GROUP.get(machine, ""),
                            "CO_Type": "production",
                        })
                prev_sku = sku; prev_inch = sku_inch.get(sku, "")
            if campaigns:
                machine_current_sku[machine] = campaigns[-1][0]
                for sku, qty, _ in campaigns:
                    if qty > 0:
                        last_build_day[sku] = day

        # ── 4. Per-shift simulation ───────────────────────────────────────
        day_built: dict[str, float] = defaultdict(float)
        day_cured_d: dict[str, float] = defaultdict(float)

        for shift in SHIFTS:
            key = (date_str, shift)
            shift_bld: dict[str, int] = defaultdict(int)
            build_by_shift_sku[key] = shift_bld

            # 4a. Building: add to GT inventory FIRST
            for machine, sku, qty, co_type in bld_plan_by_shift.get(key, []):
                gt_inventory[sku] += qty
                day_built[sku]    += qty
                shift_bld[sku]    += qty

            # 4b. Curing simulation
            for press in sorted(press_state):
                st  = press_state[press]
                sku = st["sku"]

                if press in co_press_map:
                    if shift == "A":
                        status = "CHANGEOVER"
                    elif shift == "B":
                        status = "MOULD_CLEAN"
                    else:
                        sku    = co_press_map[press]
                        status = "RUNNING"
                else:
                    status = st["status"]

                ct      = cure_ct_map.get(sku, DEFAULT_CURING_CT)
                cap     = _cure_qty_per_shift(ct)
                gt_avail = max(0.0, gt_inventory.get(sku, 0.0))

                if status == "RUNNING":
                    cured = min(cap, int(gt_avail))
                    gt_inventory[sku] = gt_avail - cured
                    day_cured_d[sku] += cured
                    sku_cured[sku]   += cured
                    daily_cured[date_str] += cured
                    demand_remaining[sku]  = max(0.0, demand_remaining.get(sku, 0.0) - cured)
                    press_stats[press]["running_mins"] += SHIFT_MINS
                    press_stats[press]["skus"].add(sku)
                    press_stats[press]["cycles"] += cured // CURING_CAVITIES
                    press_stats[press]["units"]  += cured
                    press_sku_stats[(press, sku)]["cycles"]   += cured // CURING_CAVITIES
                    press_sku_stats[(press, sku)]["units"]    += cured
                    press_sku_stats[(press, sku)]["mins_used"] += SHIFT_MINS
                else:
                    cured = 0
                    if status == "CHANGEOVER":
                        press_stats[press]["co_mins"] += SHIFT_MINS
                    elif status == "MOULD_CLEAN":
                        press_stats[press]["clean_mins"] += SHIFT_MINS

                cure_shift_rows.append({
                    "Date":         date_str,
                    "Shift":        shift,
                    "Machine":      press,
                    "SKUCode":      sku,
                    "StartTime":    SHIFT_STARTS.get(shift, ""),
                    "EndTime":      SHIFT_ENDS.get(shift, ""),
                    "Qty":          cured,
                    "CycleTime_min": round(ct, 1),
                    "GT_Inventory": int(round(gt_avail)),
                    "Remarks":      status if status != "RUNNING" else "",
                    "_status":      status,
                })

        # ── 5. GT shelf-life writeoff ─────────────────────────────────────
        day_writeoff = _writeoff_stale_gt(gt_inventory, last_build_day, day)
        writeoff_total += day_writeoff

        # ── 6. Apply CO transitions ───────────────────────────────────────
        for press, old_sku, new_sku in today_cos:
            press_count[old_sku] = max(0, press_count.get(old_sku, 0) - 1)
            press_count[new_sku] = press_count.get(new_sku, 0) + 1
            press_state[press]   = {"sku": new_sku, "status": "RUNNING"}
            curing_allowable[new_sku].append(press)

        # Daily summary
        d_built = sum(day_built.values()); d_cured = sum(day_cured_d.values())
        n_active = sum(1 for st in press_state.values() if st["status"] == "RUNNING")
        dem_met  = total_demand - sum(max(0, v) for v in demand_remaining.values())
        cov      = dem_met / total_demand * 100 if total_demand > 0 else 0
        if day % 5 == 0 or day == 1 or day == planning_days:
            print(f"  Day {day:2d} | built {d_built:6,.0f} | cured {d_cured:6,.0f} | "
                  f"presses {n_active} | COs {len(today_cos)} | "
                  f"writeoff {day_writeoff:,.0f} | coverage {cov:.1f}%")
        daily_summary.append({
            "Day": day, "Date": date_str,
            "GT_Built": int(round(d_built)), "GT_Cured": int(round(d_cured)),
            "GT_Writeoff": int(round(day_writeoff)),
            "Active_Presses": n_active, "COs_Today": len(today_cos),
            "Demand_Coverage": round(cov, 2),
        })

    # ── Final KPIs ────────────────────────────────────────────────────────────
    total_built  = sum(r["GT_Built"]  for r in daily_summary)
    total_cured  = sum(r["GT_Cured"]  for r in daily_summary)
    dem_met      = total_demand - sum(max(0, v) for v in demand_remaining.values())
    final_cov    = dem_met / total_demand * 100 if total_demand > 0 else 0
    starvation_n = sum(
        1 for r in cure_shift_rows
        if r.get("_status") == "RUNNING" and r.get("GT_Inventory", 1) == 0 and r.get("Qty", 0) == 0
    )

    print("\n" + "=" * 70)
    print("  ROLLING PIPELINE — Results")
    print("=" * 70)
    print(f"  Total GT built       : {total_built:>10,.0f}")
    print(f"  Total cured          : {total_cured:>10,.0f}")
    print(f"  GT written off       : {writeoff_total:>10,.0f}")
    print(f"  Starvation events    : {starvation_n:>10,}")
    print(f"  Demand coverage      : {final_cov:>9.1f}%  ({dem_met:,.0f} / {total_demand:,.0f})")

    # ── Write Excel outputs (same format as legacy pipeline) ─────────────────
    closing_gt_bal = {sku: v for sku, v in gt_inventory.items() if v > 0}

    _write_rolling_building_excel(
        output_path    = build_output,
        bld_shift_rows = bld_shift_rows,
        bld_co_events  = bld_co_events,
        df_day0        = df_day0,
        sku_machine_map = sku_machine_map,
        opening_gt     = opening_gt,
        demand_dict    = demand_dict,
        planning_days  = planning_days,
    )
    _write_rolling_curing_excel(
        output_path       = curing_output,
        cure_shift_rows   = cure_shift_rows,
        press_stats       = dict(press_stats),
        press_sku_stats   = dict(press_sku_stats),
        daily_cured       = dict(daily_cured),
        sku_cured         = dict(sku_cured),
        closing_gt_bal    = closing_gt_bal,
        build_by_shift_sku= build_by_shift_sku,
        opening_gt        = opening_gt,
        demand_dict       = demand_dict,
        cure_ct_map       = cure_ct_map,
        curing_allowable  = dict(curing_allowable),
        planning_days     = planning_days,
        plan_start        = plan_start,
    )

    return {
        "total_built":       total_built,
        "total_cured":       total_cured,
        "gt_writeoff":       writeoff_total,
        "starvation_events": starvation_n,
        "demand_coverage":   final_cov,
        "demand_remaining":  demand_remaining,
        "gt_inventory":      dict(gt_inventory),
        "daily_summary":     daily_summary,
        "co_events":         co_events,
        "n_co":              len(co_events),
        "build_output":      build_output,
        "curing_output":     curing_output,
    }


# ══════════════════════════════════════════════════════════════════════════════
# LEGACY PIPELINE (31-day LP — use --legacy flag)
# ══════════════════════════════════════════════════════════════════════════════

def run_pipeline(
    demand_path:   str | None = None,
    cc_output:     str | None = None,
    build_output:  str | None = None,
    curing_output: str | None = None,
    plan_start:    datetime | None = None,
    planning_days: int | None = None,
) -> dict:
    demand_path   = demand_path   or DEMAND_FILE
    cc_output     = cc_output     or CC_OUTPUT
    build_output  = build_output  or BUILD_OUTPUT
    curing_output = curing_output or CURING_OUTPUT
    plan_start    = plan_start    or PLAN_START
    planning_days = planning_days or PLANNING_DAYS

    print("\n" + "=" * 70)
    print("  PIPELINE — Step 1: Curing Consumption (Dynamic)")
    print("=" * 70)
    cc_result = run_dynamic_consumption(
        demand_path=demand_path, output_path=cc_output,
        plan_start=plan_start, planning_days=planning_days,
        max_co_per_day=MAX_CHANGEOVERS_PER_DAY,
    )
    co_events = cc_result["co_events"]
    df_day0   = cc_result["df_day0"]
    print(f"\n  [Pipeline] Step 1 complete — {len(co_events)} CO events → {os.path.basename(cc_output)}")

    df_cons = df_day0[df_day0["Category"].isin({"Runner-In", "Non-Runner-In"})].copy()
    if "Skip_Reason" in df_cons.columns:
        df_cons = df_cons[
            df_cons["Skip_Reason"].isna() | (df_cons["Skip_Reason"].astype(str).str.strip() == "")
        ].copy()
        df_cons = df_cons.drop(columns=["Skip_Reason"], errors="ignore")

    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False, dir=tempfile.gettempdir())
    tmp.close()
    with pd.ExcelWriter(tmp.name, engine="openpyxl") as writer:
        df_cons.to_excel(writer, sheet_name="Consumption Summary", index=False)

    print("\n" + "=" * 70)
    print("  PIPELINE — Step 2: Building Scheduler (B2C)")
    print("=" * 70)
    try:
        build_result = run_from_database_b2c(
            plan_start=plan_start, consumption_path=tmp.name,
            output_path=build_output, planning_days=planning_days,
            external_co_schedule=co_events,
            max_changeovers_per_day=MAX_CHANGEOVERS_PER_DAY,
            min_campaign_mins=MIN_CAMPAIGN_MINS,
            build_lead_shifts=BUILD_LEAD_SHIFTS,
        )
    finally:
        os.unlink(tmp.name)
    print(f"\n  [Pipeline] Step 2 complete → {os.path.basename(build_output)}")

    print("\n" + "=" * 70)
    print("  PIPELINE — Step 3: Curing Schedule (B2C)")
    print("=" * 70)
    curing_result = run_curing_b2c(
        building_path=build_output, output_path=curing_output,
        demand_path=demand_path, plan_start=plan_start, planning_days=planning_days,
    )
    total_cured = sum(curing_result["daily_cured"].values())
    print(f"\n  [Pipeline] Step 3 complete — Total cured: {total_cured:,.0f} → {os.path.basename(curing_output)}")

    return {
        "co_events": co_events, "n_co": len(co_events),
        "cc_output": cc_output, "build_output": build_output, "curing_output": curing_output,
        "build_result": build_result, "curing_result": curing_result,
    }


if __name__ == "__main__":
    _args   = sys.argv[1:]
    _legacy = "--legacy" in _args
    _demand = next((a for a in _args if not a.startswith("--")), None)

    if _legacy:
        print("[Pipeline] Legacy 31-day LP mode (--legacy flag)")
        result = run_pipeline(demand_path=_demand)
        print("\n" + "█" * 70)
        print("  LEGACY PIPELINE COMPLETE")
        print("█" * 70)
        print(f"  Changeovers scheduled : {result['n_co']}")
        print(f"  1. Curing consumption : {result['cc_output']}")
        print(f"  2. Building schedule  : {result['build_output']}")
        print(f"  3. Curing schedule    : {result['curing_output']}")
        total = sum(result["curing_result"]["daily_cured"].values())
        print(f"  Total cured (month)   : {total:,.0f} tyres")
        print("█" * 70)
    else:
        print("[Pipeline] Rolling day-by-day mode (new architecture)")
        result = run_rolling_pipeline(demand_path=_demand)
        print("\n" + "█" * 70)
        print("  ROLLING PIPELINE COMPLETE")
        print("█" * 70)
        print(f"  Curing COs scheduled  : {result['n_co']}")
        print(f"  GT built (month)      : {result['total_built']:>10,.0f}")
        print(f"  GT cured (month)      : {result['total_cured']:>10,.0f}")
        print(f"  GT written off        : {result['gt_writeoff']:>10,.0f}")
        print(f"  Starvation events     : {result['starvation_events']:>10,}")
        print(f"  Demand coverage       : {result['demand_coverage']:>9.1f}%")
        print(f"  Building output       : {result['build_output']}")
        print(f"  Curing  output        : {result['curing_output']}")
        print("█" * 70)
        print("\n  Worst 10 SKUs by remaining demand:")
        rem = sorted(result["demand_remaining"].items(), key=lambda x: -x[1])[:10]
        for sku, qty in rem:
            print(f"    {sku}: {qty:,.0f} units remaining")
