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

MAX_CHANGEOVERS_PER_DAY = 10
# Hard cap on CURING PRESS COs scheduled per calendar day (unchanged in new arch).
# 8  → ~594k GT (May 2026 baseline).
# 10 → ~587k (more NRI simultaneously → more building CO overhead → net −7k).

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

MIN_SHIFT_UTILISATION = 0.80
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

MIN_CAMPAIGN_MINS   = 120
# Minimum production run (minutes) per (SKU, machine) before the heuristic
# is allowed to switch to a different SKU.
# Default in building.py = 45 → machine 7001-7004 reach 173 COs/month.
# 120 = quarter-shift minimum, limits daily SKU count and CO overhead.

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

GT_BUFFER_SHIFTS        = 1
# NEW ARCHITECTURE: how many curing shifts of GT to pre-build as a buffer.
# 1 = build exactly what today's presses consume today (default).
# 2 = build today's + 1 shift extra (next-shift safety buffer).
# With GT_BUFFER_SHIFTS = 1–2 (~0.33–0.67 day buffer), the 3-day shelf life is
# never hit under normal operation. Must be <= GT_SHELF_LIFE_DAYS × SHIFTS_PER_DAY.

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

CURING_CO_DURATION_SHIFTS  = 2     # total shifts a press is idle during CO (Shift A + Shift B)
CURING_CO_CHANGEOVER_MINS  = 490   # Shift A: press occupied for changeover (full shift)
CURING_MOULD_CLEAN_MINS    = 120   # Shift B: mould clean window within the idle shift
# Note: press is idle the entire Shift B (480 min); 120 min is the physical
# mould-clean time. The remaining ~360 min of Shift B are not schedulable.
# ─────────────────────────────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════════════
# 6. PHYSICAL CONSTANTS  —  do NOT change (plant layout, not scheduling params)
# ══════════════════════════════════════════════════════════════════════════════

SHIFT_MINS          = 480    # minutes per shift (8 hours × 60)
SHIFTS_PER_DAY      = 3      # A (07:00) / B (15:00) / C (23:00)
CAVITIES_PER_PRESS  = 2      # 2 moulds per press, 1 cavity each = 2 tyres/cycle

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

CONSUMPTION_OUTPUT = os.path.join(_OUT,      "curing_consumption_table.xlsx")
DYNAMIC_CC_OUTPUT  = os.path.join(_OUT,      "curing_consumption_31day.xlsx")
BUILDING_OUTPUT    = os.path.join(_MAIN_OUT, f"bc_building_schedule_{PLAN_START.date()}.xlsx")
CURING_OUTPUT      = os.path.join(_MAIN_OUT, "bc_curing_schedule.xlsx")
CURING_B2C_OUTPUT  = os.path.join(_MAIN_OUT, "bc_curing_b2c.xlsx")
ANALYSIS_OUTPUT    = os.path.join(_MAIN_OUT, "bc_analysis.xlsx")
