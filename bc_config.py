"""
bc_config.py — Single Source of Truth for ALL B2C pipeline parameters.
=======================================================================
Every pipeline file (bc.py, curing_consumption_dynamic.py, building_b2c.py,
curing_b2c.py, b2c_pipeline.py) MUST import parameters from HERE.
Never hardcode scheduling parameters in any other file.

To change any parameter for a planning run: edit only this file.

Sections:
    1. Plan horizon
    2. Input files
    3. Curing press changeover (curing_consumption_dynamic.py)
    4. Building scheduler  (building_b2c.py + building.py Config)
    5. Curing simulation   (curing_b2c.py)
    6. Physical constants  (do NOT change — plant constraints)
    7. Output paths
"""

from __future__ import annotations

import os
from datetime import datetime

import cbc_env

# ══════════════════════════════════════════════════════════════════════════════
# 1. PLAN HORIZON
#    Change PLAN_START and PLANNING_DAYS each month before running.
# ══════════════════════════════════════════════════════════════════════════════

PLAN_START    = datetime(2026, 5, 1, 7, 0, 0)   # first shift of plan (Shift A, 07:00)
PLANNING_DAYS = 31                                # number of days in plan horizon

# ══════════════════════════════════════════════════════════════════════════════
# 2. INPUT FILES
#    Drop the demand workbook in data/input/ and update DEMAND_FILE.
#    Required columns: SKUCode, Requirement (or Updated_Requirement),
#                      ConsolidatedPriorityScore
# ══════════════════════════════════════════════════════════════════════════════

DEMAND_FILE = os.path.join(cbc_env.INPUT_DIR, "demand_may.xlsx")

# ── Daily running-moulds ETL table (Day-0 curing press state) ────────────────
# SINGLE SOURCE OF TRUTH for which running-moulds snapshot the plan starts from.
# Every consumer (curing_consumption.py Phase 0, curing_b2c.py press state +
# mould tracker) imports this — never hardcode the table name anywhere else.
# Change ONLY this line each planning cycle:
#   july plan  → "june_Daily_Running_Moulds"      (26-Jun snapshot, 169 presses)
#   june plan  → "testing_Daily_Running_Moulds"   (27-May snapshot, 165 presses)
#   (live/rolling)                → "Daily_Running_Moulds"
# The table lives in the DB given by JKT_DB_DATABASE (default jkplanningV1) and
# must have columns: WCNAME, Sapcode, Mould life, Target life, Mould Fix_dt.
RUNNING_MOULDS_TABLE = "Daily_Running_Moulds"

# ══════════════════════════════════════════════════════════════════════════════
# 3. CURING PRESS CHANGEOVER  →  curing_consumption_dynamic.py
# ══════════════════════════════════════════════════════════════════════════════
BUILD_LEAD_SHIFTS   = 3
# LEGACY (31-day upfront LP): building targets curing demand 1 full day ahead.
# NEW ARCHITECTURE (rolling loop): 0 for steady-state (simultaneous start);
# still 2 for curing CO days (building pre-starts Shift A of CO day so 2 shifts
# of GT accumulate before the press fires up in Shift C).

TOPUP_LOOKAHEAD_DAYS_GT = 3
# LEGACY: TopUp pre-builds GT at most this many days ahead.
# NEW ARCHITECTURE: changes to 1 SHIFT (not 3 days) — building and curing start
# simultaneously; GT is produced and consumed within the same shift.
# Physical reason: building CT ≈ 2 min → 1 machine produces 240 GT/shift,
# enough to feed ≈ 4.3 curing presses in real time. Pre-build buffer not needed.

MAX_CHANGEOVERS_PER_DAY = 12   # was 18 — user set 12 for the surplus-release test
# Hard cap on CURING PRESS COs scheduled per calendar day (unchanged in new arch).
# 8  → ~594k GT (May 2026 baseline).
# 10 → ~615k (balanced NRI activation).
# 14 → ~650k target: activates more NRI COs (TTMX0/MSXT0/TUHL0/HURL0 gain ~20k units).
#      Also gives FXPC0 more presses via Runner-Out → FXPC0 CO conversion.

CO_CLASS_B_THRESHOLD = 0.8
# Class A threshold for curing CO urgency scoring.
# A CO candidate is Class A (critical) when: current_days > horizon_left × threshold
# 1.0 (default) = strict Class A: demand truly cannot be met without this CO.
# 0.8 = allow COs where demand would take >80% of remaining horizon with current presses.
# Lower = more COs scheduled = more NRI SKUs activated (higher output but more CO overhead).
# Effect: activates high-demand NRI SKUs that sit just below the strict Class A cutoff.


# ── NEW ARCHITECTURE: Building machine CO cap ────────────────────────────────
MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT = 2
# Maximum changeovers a single building machine may perform in ONE SHIFT.
# Plant currently averages 0.57 CO/shift/machine (1 CO per shift is typical).
# Upper bound = 2 CO/shift; actual value depends on curing press consumption
# (how many SKUs need simultaneous GT feed from the same machine).
# Allows one machine to serve up to 3 curing press groups in one shift.
# Confirmed from plant data (7001: 195/55 R16 → CO → 215/60 R16; both 16").
# Must be same_size_CO (same inch) to satisfy the 80% utilisation floor:
#   1 × same_size_CO (VMI 20 min) =  20 min overhead → 95.8% production  ✓ (typical)
#   2 × same_size_CO (VMI 20 min) =  40 min overhead → 91.7% production  ✓ (max allowed)
#   2 × diff_size_CO (VMI 120 min) = 240 min overhead → 50% production   ✗ BLOCKED

MIN_SHIFT_UTILISATION = 0.77
# Each building machine must achieve ≥ 80% production time per shift.
# Expressed as fraction of SHIFT_MINS (480 min): floor = 384 production minutes.
# Used to: (a) block a CO if remaining time after it < 384 min, (b) trigger
# idle-fill assignment if a machine drops below this floor after demand cap.
# ─────────────────────────────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════════════
# 4. BUILDING SCHEDULER  →  building_b2c.py  +  building.py Config
# ══════════════════════════════════════════════════════════════════════════════

# ── Building machine changeover times (minutes) ──────────────────────────────
# Two CO types: same_size_CO (inch unchanged, only recipe/compound changes)
#               diff_size_CO (inch changes — mould size must change)
# Source of truth for actual per-machine values: Master_Building_CO_Times sheet.
# These dicts are the canonical reference and are used by the LP penalty map
# in building.py. Keys match machine group labels used in the codebase.

BUILDING_CO_SAME_SIZE = {
    # machine group  →  same_size_CO duration (min)
    "VMI":      20,   # 6001–6004, 7001–7004  — cheapest CO (4.2% of shift)
    "BJ":       45,   # 7101–7106, 7201
    "STAGE2":   59,   # 8201, 8301, 8302, 8501, 8502, 7301
    "STAGE1":   60,   # 6801–6803, 6909, 6911, 7601, 7701, 7801–7804, 8001–8003, 8101
    "MID":      60,   # same as Stage-1 (shared group in CO master)
    "UNISTAGE": 110,  # 7501–7503
}

BUILDING_CO_DIFF_SIZE = {
    # machine group  →  diff_size_CO duration (min)
    "STAGE2":   88,   # acceptable if no VMI alternative (88 min)
    "BJ":       90,   # 7101–7106, 7201
    "VMI":      120,  # 6001–6004, 7001–7004
    "STAGE1":   180,  # 37.5% of one shift — avoid unless critical demand
    "MID":      180,
    "UNISTAGE": 180,  # 7501–7503 — same as Stage-1
}

STAGE2_CO_TIME_MULTIPLIER = 2.0
# LP penalty multiplier applied to Stage-2 diff_size_CO time in building.py.
# 88 min × 2.0 = 176 min effective — discourages LP from overloading Stage-2
# with SKU switches. Raise to further penalise; lower to relax.
# Used at: building.py co_time_map construction (line ~2021).

# ─────────────────────────────────────────────────────────────────────────────

MIN_CAMPAIGN_MINS   = 60
# Minimum production run (minutes) per (SKU, machine) before the heuristic
# is allowed to switch to a different SKU.
# Default in building.py = 45 → machine 7001-7004 reach 173 COs/month.
# Was 120 (for LP pipeline to prevent CO explosion). Rolling pipeline uses
# MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT=2 to cap COs, so 60 is safe.
# 120 blocked any SKU with ≤2 presses (112 units/shift < 120 min threshold)
# on fast VMI machines (1 unit/min CT), causing permanent zero production.
# 60 allows 1–2 press SKUs (64–128 min/shift demand) while still preventing
# micro-campaigns that generate unnecessary CO overhead.

MIN_CAMPAIGN_UNITS  = 40
# Secondary guard: minimum units per campaign (after MIN_CAMPAIGN_MINS passes).


OVERBUILD_BUFFER_FRAC = 0.2
# LP headroom above net daily demand (fraction).
# 0.2 = 20% buffer prevents LP ceiling from collapsing to 0 when partial WIP
# already covers some demand. Does NOT violate the hard "total build ≤ demand"
# ceiling — the total horizon cap is enforced by gt_topup_target separately.

PRE_START_SHIFTS    = 2
# Building starts this many shifts BEFORE PLAN_START so RI SKUs have a GT
# buffer when curing fires on Day 1.
# 2 → build starts Apr 30 15:00 (Shift B) for a May 1 07:00 plan.
# 1 → Apr 30 23:00 (Shift C) — caused Day-1 starvation for zero-inventory RI SKUs.


TOPUP_LOOKAHEAD_DAYS_CARCASS = 1
# Same as above but for Stage-1 carcass.

GT_SHELF_LIFE_DAYS      = 3
# GT cannot sit more than 3 days before curing (plant rule).
# TopUp will not pre-build GT beyond this window.

MAX_ENDOFDAY_GT_INVENTORY = 8000
# Plant capacity constraint: total GT held in inventory at the END of any day
# (summed over all SKUs, after curing + stale writeoff) cannot exceed this many
# units. Enforced PROACTIVELY during building (never build past the ceiling) so
# it is a hard cap, not a reactive writeoff. Bounds the forward-buffer level-load.

GT_BUFFER_SHIFTS        = 2
# VMI sibling machines (e.g. 6004+7001, both on 16") both need non-zero deficit
# to stay active. With _buf=2, target = 2× cure rate → each sibling fills ~1 shift
# worth → total 2 shifts of buffer maintained.
# BJ/UNISTAGE/STAGE use 1× (see _assign_building_shift _buf logic).

POOL_SIZE = 3
# Max SKUs per building machine pool.
# Each machine oscillates among POOL_SIZE same-inch eligible SKUs.
# Pool is fixed at Day 1; replaced only when a SKU's demand finishes.
# Determines Campaign 2+ candidate list inside _assign_building_shift.

STARVATION_BUFFER_MINS = 30
# If a pool SKU's GT inventory covers < 30 min of curing press consumption,
# it is treated as starving and gets Campaign-1 priority over normal urgency.
# 30 min = ~3–4 curing cycles on a typical press (CT ~8–12 min).

CARCASS_SHELF_LIFE_DAYS = 1
# Stage-1 carcass shelf life: 1 day (must enter Stage-2 same or next shift).

# ══════════════════════════════════════════════════════════════════════════════
# 5. CURING SIMULATION  →  curing_b2c.py
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_CURING_CT = 17.0
# Fallback cure cycle time (minutes) used when a SKU's CT is absent from
# Master_Curing_Design_CycleTime. Typical PCR press CT is 15–20 min.

# ── Curing press changeover times ────────────────────────────────────────────
# A curing press CO occupies 2 consecutive shifts:
#   Shift A (CO day)  → CHANGEOVER   (press idle, mould swap)
#   Shift B (CO day)  → MOULD_CLEAN  (press idle, mould clean)
#   Shift C (CO day)  → PRODUCTION begins on new SKU
# Building for the new SKU must start simultaneously with Shift A (see CLAUDE.md).

CURING_CO_DURATION_SHIFTS  = 1     # shifts idle during CO: Shift A only (CHANGEOVER).
#                                    Mould clean removed from scheduler model.
CURING_CO_CHANGEOVER_MINS  = 480   # Shift A: press occupied for changeover (full shift)
# Shift B: PRODUCTION begins for new SKU immediately (no mould-clean idle shift).
# ─────────────────────────────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════════════
# 6. PHYSICAL CONSTANTS  —  do NOT change (plant layout, not scheduling params)
# ══════════════════════════════════════════════════════════════════════════════

SHIFT_MINS          = 480    # minutes per shift (8 hours × 60)
SHIFTS_PER_DAY      = 3      # A (07:00) / B (15:00) / C (23:00)
CAVITIES_PER_PRESS  = 2      # 2 moulds per press, 1 cavity each = 2 tyres/cycle

# ── Mould clean (curing press) ───────────────────────────────────────────────
MOULD_CLEAN_CYCLES  = 3000   # cycles a press runs before a mandatory mould clean
#                              (= 6,000 tyres at CURING_CAVITIES = 2). A curing CO
#                              also resets mould life (CO includes a clean).
MOULD_CLEAN_MINS    = 480    # 8-hour mould clean = one full shift of press downtime.

SHIFT_NAMES  = ["A", "B", "C"]
SHIFT_STARTS = {"A": "07:00", "B": "15:00", "C": "23:00"}
SHIFT_ENDS   = {"A": "15:00", "B": "23:00", "C": "07:00"}

# GT-producing machine IDs (Unistage + Stage-2; excludes Stage-1 carcass)
GT_MACHINES = frozenset({
    "8201", "8301", "8302", "8501", "8502", "7301",   # Stage-2
    "7001", "7002", "7003", "7004",                    # VMIMAXX
    "6001", "6002", "6003", "6004",                    # VMIMAXX
    "7101", "7102", "7103", "7104", "7105", "7106",   # BJ
    "7201",                                            # BJ
    "7501", "7502", "7503",                            # UNI_NARROW
})

# ══════════════════════════════════════════════════════════════════════════════
# 7. OUTPUT PATHS  —  derived automatically from PLAN_START
# ══════════════════════════════════════════════════════════════════════════════

_OUT      = cbc_env.OUTPUT_DIR
_MAIN_OUT = os.path.join(_OUT, "main_output")
os.makedirs(_MAIN_OUT, exist_ok=True)

# All per-run outputs are stamped with PLAN_START (and the horizon length) so a
# new planning cycle never silently overwrites the previous month's results.
CONSUMPTION_OUTPUT = os.path.join(_OUT,      f"curing_consumption_table_{PLAN_START.date()}.xlsx")
DYNAMIC_CC_OUTPUT  = os.path.join(_OUT,      f"curing_consumption_{PLANNING_DAYS}day_{PLAN_START.date()}.xlsx")
BUILDING_OUTPUT    = os.path.join(_MAIN_OUT, f"bc_building_schedule_{PLAN_START.date()}.xlsx")
CURING_OUTPUT      = os.path.join(_MAIN_OUT, f"bc_curing_schedule_{PLAN_START.date()}.xlsx")
CURING_B2C_OUTPUT  = os.path.join(_MAIN_OUT, f"bc_curing_b2c_{PLAN_START.date()}.xlsx")
ANALYSIS_OUTPUT    = os.path.join(_MAIN_OUT, "bc_analysis.xlsx")
ROLLING_OUTPUT     = os.path.join(_MAIN_OUT, "bc_rolling_schedule.xlsx")
