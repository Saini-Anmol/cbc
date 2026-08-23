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

# ── env / project paths / DB engine (absorbed from the former cbc_env.py) ──
HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")

# Project data layout (inputs the user drops in, outputs we write).
INPUT_DIR = os.path.join(HERE, "data", "input")
OUTPUT_DIR = os.path.join(HERE, "data", "output")

# ══════════════════════════════════════════════════════════════════════════════
# ★ RUN PARAMETERS — EDIT THESE EACH MONTH BEFORE RUNNING ★
#   The only lines you normally change for a new month. DEMAND_FILE uses INPUT_DIR
#   (defined just above); RUNNING_MOULDS_MONTH / PLAN_MONTH auto-derive from PLAN_START.
#   Detailed notes for each param remain in their original sections further below.
# ══════════════════════════════════════════════════════════════════════════════
PLAN_START    = datetime(2026, 8, 1, 7, 0, 0)   # first shift of plan (Shift A, 07:00)
PLANNING_DAYS = 31                              # days in the plan horizon (30 June / 31 Jul/Aug)
DEMAND_FILE   = os.path.join(INPUT_DIR, "august_demand_tomerji.xlsx")  # per-month demand workbook
RUNNING_MOULDS_TABLE = "Daily_Running_Moulds"   # Day-0 curing press-state snapshot (live table)
PLANT_HOLIDAYS = False                           # list of "YYYY-MM-DD" or False (INERT); cloud reads jkt_holiday_calendar
# auto-derived from PLAN_START (env overrides) — month keys for running-moulds + opening GT/carcass
RUNNING_MOULDS_MONTH = os.environ.get("RUNNING_MOULDS_MONTH") or PLAN_START.strftime("%Y-%m")
PLAN_MONTH           = os.environ.get("PLAN_MONTH")           or PLAN_START.strftime("%Y-%m")
# EXACT-DATE key ("YYYY-MM-DD") — the running-moulds / gt_inventory_manual / carcass_inventory_manual
# tables now carry a `date` column, so state is read for the plan's START DATE (a month can hold
# several daily snapshots, e.g. 2026-08-01 AND 2026-08-21 → plan_month alone is ambiguous). Env
# overridable; harnesses/main set it alongside PLAN_MONTH. For a mid-month "generate from today" run,
# PLAN_START = today, so PLAN_DATE = today and the ETL seeds from today's real running-moulds/GT/carcass.
PLAN_DATE            = os.environ.get("PLAN_DATE")            or PLAN_START.strftime("%Y-%m-%d")


# Non-secret defaults only. Host/user/password/database must come from .env or
# the process environment — there are intentionally no credential fallbacks.
_DEFAULTS = {
    "JKT_DB_PORT": "3306",
    "JKT_DB_DATABASE": "jkplanningV1",
}

# Keys that must be present (no default) — accessing them when unset raises.
_REQUIRED = ("JKT_DB_HOST", "JKT_DB_USER", "JKT_DB_PASSWORD")


def _load_env_file(path: str = ENV_PATH) -> dict:
    vals = dict(_DEFAULTS)
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    # Process env overrides the file; the file overrides the defaults.
    #
    # IMPORTANT: we must also pick up keys that exist ONLY in the environment.
    # In Docker there is no .env inside the image (secrets are injected with
    # -e / --env-file), so iterating just the already-known keys would silently
    # drop JKT_DB_HOST/USER/PASSWORD and the app would fail to boot with
    # "Missing required config".
    env_keys = {k for k in os.environ if k.startswith("JKT_") or k == "MES_API_KEY"}
    for k in set(vals) | set(_REQUIRED) | env_keys:
        v = os.environ.get(k)
        if v:
            vals[k] = v
    return vals


ENV = _load_env_file()


def require(key: str) -> str:
    """Return a required env value, raising a clear error if it is unset."""
    val = ENV.get(key)
    if not val:
        raise RuntimeError(
            f"Missing required config '{key}'. Set it in {ENV_PATH} "
            f"(key=value) or as an environment variable.")
    return val


def db_config() -> dict:
    """Return {host, port, user, password, database} for the planning DB."""
    for k in _REQUIRED:
        require(k)
    return {
        "host": ENV["JKT_DB_HOST"],
        "port": int(ENV.get("JKT_DB_PORT", 3306)),
        "user": ENV["JKT_DB_USER"],
        "password": ENV["JKT_DB_PASSWORD"],
        "database": ENV["JKT_DB_DATABASE"],
    }


def mes_api_key() -> str:
    """MES export API key for data_fetch.py — required, no fallback."""
    return require("MES_API_KEY")


def db_url() -> str:
    """SQLAlchemy URL for mysql+pymysql."""
    c = db_config()
    return (f"mysql+pymysql://{c['user']}:{c['password']}"
            f"@{c['host']}:{c['port']}/{c['database']}")


def make_engine(connect_timeout: int = 15):
    from sqlalchemy import create_engine
    # pool_pre_ping  → test (and transparently replace) a connection before use,
    #                  so a dropped/stale connection reconnects instead of raising
    #                  "Can't reconnect until invalid transaction is rolled back".
    # pool_recycle   → recycle connections older than this (sec) before the
    #                  remote MySQL's idle wait_timeout can kill them — important
    #                  because the engine sits idle through the long build phase.
    return create_engine(
        db_url(),
        connect_args={"connect_timeout": connect_timeout},
        pool_pre_ping=True,
        pool_recycle=280,
    )


def in_path(name: str) -> str:
    return os.path.join(INPUT_DIR, name)


def out_path(name: str) -> str:
    return os.path.join(OUTPUT_DIR, name)



# ══════════════════════════════════════════════════════════════════════════════
# ★ FEATURE TOGGLE — DELIVERY-DATE / PRIORITY-FLAG COMMITTED-DELIVERY SKUs ★
#   Flip this ONE line to turn the committed-delivery feature ON/OFF for LOCAL runs.
#   ON (default) is INERT unless the demand file carries a "Priority Flag" / "Delivery
#   Date" column WITH data — so June and any non-priority file run bit-for-bit either
#   way; only July/August (which carry the columns) are affected. Full detail + the
#   sub-levers (DP_ACQUIRE / DP_RESERVE / DP_MOULDCAP / DP_PACE_MARGIN / DP_BLD, all env)
#   live in §4 below and in b2c_pipeline.py / curing_consumption_dynamic.py.
#   Env DELIVERY_PRIORITY=0 also forces it off. For the CLOUD path, flip the matching
#   top toggle in main.py (CLOUD_CONFIG) instead — editing this file does not affect cloud.
# ══════════════════════════════════════════════════════════════════════════════
DELIVERY_PRIORITY_ENABLED             = False
DELIVERY_PRIORITY_UNDATED_TO_MONTHEND = False

# ══════════════════════════════════════════════════════════════════════════════
# ★ FEATURE TOGGLE — RUNNER-OUT DAY-1 CHANGEOVER ★
#   A curing press running a NO-DEMAND SKU at Day-0 (Runner-Out) is forced to change
#   over on Day-1 Shift A to its neediest allowable demand SKU (2 free moulds) and produce
#   from Shift B — instead of sitting idle on the dead SKU. Measured July +3,196
#   (666,496 → 669,692). Env RUNNER_OUT_DAY1_CO=0 also forces it off.
# ══════════════════════════════════════════════════════════════════════════════
RUNNER_OUT_DAY1_CO_ENABLED = False   # default ON

# ══════════════════════════════════════════════════════════════════════════════
# 1. PLAN HORIZON
#    Change PLAN_START and PLANNING_DAYS each month before running.
# ══════════════════════════════════════════════════════════════════════════════

# PLAN_START — moved to the RUN PARAMETERS block at the top of this file
# PLANNING_DAYS — moved to the RUN PARAMETERS block at the top of this file

# ── Plant holidays — NON-working days (no building, no curing) ─────────────────
# List of holiday dates as "YYYY-MM-DD" strings inside the plan horizon. Empty =
# feature INERT (output bit-for-bit identical to a no-holiday run). Holidays are IDLE
# days inside the FIXED calendar span (same demand, fewer working days → lower coverage).
# Aging stays CALENDAR-based (GT 3-day / carcass 1-day still age across a holiday);
# in-flight changeovers/cleans complete during the idle day (setup crew). Env
# PLANT_HOLIDAYS="2026-07-15,2026-07-16" overrides; cloud reads jkt_plan_params.
# PLANT_HOLIDAYS — moved to the RUN PARAMETERS block at the top of this file
#   e.g. PLANT_HOLIDAYS = ["2026-07-15", "2026-07-16"]   (edit here or set the env var)

# ══════════════════════════════════════════════════════════════════════════════
# 2. INPUT FILES
#    Drop the demand workbook in data/input/ and update DEMAND_FILE.
#    Required columns: SKUCode, Requirement (or Updated_Requirement),
#                      ConsolidatedPriorityScore
# ══════════════════════════════════════════════════════════════════════════════

# DEMAND_FILE — moved to the RUN PARAMETERS block at the top of this file

# ── Per-(SKU × machine) building cycle-time file ─────────────────────────────
# When BLD_CT_FILE_ENABLED, building CT (sec/unit) is looked up per (SKU, machine)
# from this CSV — a machine can build different SKUs at different speeds (e.g. VMI
# builds small tyres faster than large). The file is authoritative for CT ONLY:
#   • allowability still comes from Master_Building_Allowable_Machines (DB);
#   • any (SKU, machine) pair missing from the file falls back to the per-machine
#     fixed CT in b2c_pipeline._BLD_CT_SEC (never a blind default).
# Toggle OFF (or env BLD_CT_FILE=0) reproduces the fixed-per-machine-CT plan
# bit-for-bit. See b2c_pipeline._bld_ct_sec / _load_bld_ct_file.
BLD_CT_FILE_ENABLED = True
# CORRECT building-CT source (per-(SKU,machine) sec/unit). Loader reads .xlsx or .csv by
# extension (b2c_pipeline._load_bld_ct_file). The old *_Cycle_time_Building.csv variants are
# retired — this xlsx is the single authoritative CT file.
BLD_CT_FILE = os.path.join(INPUT_DIR, "Cycle_time_Building.xlsx")

# ── Per-machine dominant-inch ranking file (inch-locking source) ─────────────
# When DOMINANT_INCH_FILE_ENABLED, each building machine's dominant inch AND its
# ordered multi-inch band come from this xlsx (sheet "Dominant_Inch", cols
# Machine, Ranked_Inches) — built from the plant's last-N-day running data
# (see data/analysis_aug/machine_inch_dominant_aug.xlsx). It overrides the scalar
# b2c_pipeline._MACHINE_DOMINANT_INCH (= top inch) and exposes the ranked band as
# _MACHINE_DOMINANT_INCH_RANKED (Phase-5 inch-locking source for the planner).
# OFF (default) or a missing file keeps the hardcoded dominant-inch map →
# bit-for-bit baseline. Env DOMINANT_INCH_FILE=0 also disables.
DOMINANT_INCH_FILE_ENABLED = True   # ADOPTED — 39-machine dominant-inch band + start-free anchor
DOMINANT_INCH_FILE = os.path.join(HERE, "data", "analysis_aug",
                                  "machine_inch_dominant_aug.xlsx")

# ── Historical inch-LOCK (INCH_HIST_LOCK) — replaces the anchor±2 band ────────
# Per-machine ALLOWED-INCH SETS come from the 4-month plant building report
# (sheet "Inch_Counts_Matrix"): an inch is kept for a machine when it is >=
# INCH_HIST_LOCK_MIN_SHARE of that machine's records, ranked by count, capped at
# INCH_HIST_LOCK_MAX_INCHES. A machine that ran essentially one inch (>=2% only on
# its dominant) becomes FIXED — locked to that single inch, ZERO different-size CO
# ever. A machine that historically ran multiple inches becomes FLEXIBLE — it may
# only build/CO among its ranked historical inches (the +/-2 anchor band is
# DISCONTINUED, so historically-evidenced +/-3 jumps like 7001 15<->18 are allowed
# while a jump to an inch it never ran is not). Enforced via the allowable-machine
# strip + machine_locked_inches gate + Stage-1 _s1_inch_ok. This 2% threshold
# reproduces the plant's 27-fixed / 12-flexible split exactly.
# OFF (env INCH_HIST_LOCK=0) or a missing file → current anchor±2 behaviour,
# bit-for-bit. Overrides DOMINANT_INCH_FILE's dominant map when both are on.
INCH_HIST_LOCK_ENABLED   = (os.environ.get("INCH_HIST_LOCK", "1") != "0")
INCH_HIST_LOCK_FILE      = os.path.join(HERE, "data", "analysis_aug",
                                        "machine_inch_dominant_4months_Apr-Jul.xlsx")
INCH_HIST_LOCK_MIN_SHARE = float(os.environ.get("INCH_HIST_MIN_SHARE", "0.02"))
INCH_HIST_LOCK_MAX_INCHES = int(os.environ.get("INCH_HIST_MAX_INCHES", "3"))
# Apply the historical lock to Stage-1 carcass machines too? Default OFF: Stage-1 is
# post-hoc (doesn't gate GT/cured) and _STAGE1_SINGLE_INCH already fixes each S1 machine
# to ONE (demand-optimal, carcass-FEASIBLE) inch — that already satisfies "fixed = one
# inch". Forcing S1 onto historical inches only broke carcass feasibility (715 units) for
# zero cured benefit, so it is off. ON = also lock S1 to its historical inch set.
INCH_HIST_LOCK_STAGE1 = (os.environ.get("INCH_HIST_LOCK_STAGE1", "0") != "0")
# Fixed-machine escape (Lever B): a FIXED machine (single historical inch) may take at
# most FIXED_ESCAPE_MAX_COS different-size CO(s) — and ONLY after its own inch's demand
# is fully complete — to a scarce inch it is DB-certified for, then it stays there. This
# recovers idle capacity stranded on fixed 14"/17" machines once their inch is done,
# without letting them abandon their inch while it still has work. Default OFF (measure).
FIXED_ESCAPE_ENABLED  = (os.environ.get("FIXED_ESCAPE", "0") != "0")
FIXED_ESCAPE_MAX_COS  = int(os.environ.get("FIXED_ESCAPE_MAX_COS", "1"))

# ── Start all building machines FREE (no Day-0 running SKU) ───────────────────
# The plant provides no building-running-machine snapshot for this cycle, so every
# building machine starts free → the first shift seeds each as a "start" (0 CO),
# giving NO initial same/diff-size building COs; each machine anchors to its
# DOMINANT inch (from the file above). Verified: 0 forced initial COs. Env
# BLD_START_FREE overrides. See b2c_pipeline._BLD_START_FREE / _anchor_seed_inch.
BLD_START_FREE_ENABLED = True

# ── Daily running-moulds ETL table (Day-0 curing press state) ────────────────
# SINGLE SOURCE OF TRUTH for which running-moulds snapshot the plan starts from.
# Every consumer (curing_consumption_dynamic.py Phase 0, curing_b2c.py press state +
# mould tracker) imports this — never hardcode the table name anywhere else.
# Change ONLY this line each planning cycle:
#   august plan → "june_Daily_Running_Moulds"     (no July snapshot exists; user chose June)
#   july plan  → "june_Daily_Running_Moulds"      (26-Jun snapshot, 169 presses)
#   june plan  → "testing_Daily_Running_Moulds"   (27-May snapshot, 165 presses)
#   (live/rolling)                → "Daily_Running_Moulds"
# The table lives in the DB given by JKT_DB_DATABASE (default jkplanningV1) and
# must have columns: WCNAME, Sapcode, Mould life, Target life, Mould Fix_dt.
# RUNNING_MOULDS_TABLE — moved to the RUN PARAMETERS block at the top of this file

# ── Which month's snapshot to read from the consolidated Daily_Running_Moulds ──
# All months now live in ONE table (Daily_Running_Moulds), discriminated by the
# plan_month column ('YYYY-MM'). The 4 curing SQL sites filter WHERE plan_month =
# RUNNING_MOULDS_MONTH. Auto-derived from PLAN_START so it can never disagree with
# the plan month; env RUNNING_MOULDS_MONTH overrides (e.g. to reuse a prior month's
# snapshot). If a month ever has >1 snapshot, read the latest snapshot_date.
# RUNNING_MOULDS_MONTH — moved to the RUN PARAMETERS block at the top of this file

# ── Which month's opening inventory to read (gt_inventory_manual / carcass_inventory_manual) ──
# Both inventory tables now hold all months in ONE table each, discriminated by the
# plan_month column ('YYYY-MM'). The 4 GT + 1 carcass read sites filter WHERE plan_month =
# PLAN_MONTH. Auto-derived from PLAN_START (env PLAN_MONTH overrides); same value as
# RUNNING_MOULDS_MONTH by default. Each month's snapshot is loaded from data/gt_carcass/
# (aggregated per SKU by plcbomname). May has no data — no 2026-05 rows exist.
# PLAN_MONTH — moved to the RUN PARAMETERS block at the top of this file

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

MAX_CHANGEOVERS_PER_DAY = 17   # validated CO cap (lowest starvation; cap=16 hurt July ~11pp).
# Only caps 12 and 14 have been measured on correct data (Daily_Running_Moulds):
#   cap 12 -> May 644,570 | June 611,593 | July 641,262   (best of the two)
#   cap 14 -> May 665,244 | June 603,933 | July 627,916   (net -332, rejected)
# Caps 10/11/13/15/16 are UNTESTED — the earlier 10-16 sweep used the retired
# june_Daily_Running_Moulds table and is void. Run the 7x3 sweep before trusting 16.
# Cloud is unaffected by this line: the cap comes from jkt_plan_params.noOfChangeOver.
# Hard cap on CURING PRESS COs scheduled per calendar day (unchanged in new arch).
# 8  → ~594k GT (May 2026 baseline).
# 10 → ~615k (balanced NRI activation).
# 14 → ~650k target: activates more NRI COs (TTMX0/MSXT0/TUHL0/HURL0 gain ~20k units).
#      Also gives FXPC0 more presses via Runner-Out → FXPC0 CO conversion.

# VMI/BJ different-size CO tightness under GROUP_INCH_POLICY (the adopted co-plan config).
# VMI_JIT_MARGIN: a VMI/BJ machine changes to a DIFFERENT inch only when the off-inch target is
#   this many units MORE starving than staying — higher = fewer diff-size COs (plant runs ~8 VMI/mo).
# VMI_MAX_DIFF_CO_PER_DAY: per-machine per-day different-size CO budget for VMI/BJ.
# SINGLE cross-month optimum = 250 (swept {250,275,300,325} on May/June/July with SIZE_BAL on):
#   highest total cured (1,987,725) AND lowest total starvation (4,478). July cliffs above 250
#   (683,560 → 668,073 at 275, no diff-CO benefit), which dominates the aggregate; June also prefers
#   250 (638,216) under SIZE_BAL. One value beats the old per-month 300/300/250 split by +2.4k.
#   Env VMI_JIT_MARGIN / VMI_MAX_DIFF_CO_PER_DAY override for A/B.
VMI_JIT_MARGIN = 250
VMI_MAX_DIFF_CO_PER_DAY = 1

# Round-trip buffer partner = SAME inch size (prefer a same-size CO for the rotation). PER-MONTH knob:
#   True on low-demand months (May: +14,964 cured, VMI diff-CO 11→8 = plant, starvation down, mould-PASS)
#   — balanced demand rotates within-inch, so a same-inch partner sizes the buffer correctly.
#   False on high-demand July (−14,409): its imbalanced 15"-heavy demand genuinely needs cross-inch
#   rotations, so restricting the round-trip partner to same-inch undersizes the buffer and starves it.
#   The current committed month is July → False. Set True when running a May-like month.
#   Env RT_SAME_INCH overrides. (RT_SAME_INCH_FRAC>0 relaxes the restriction — measured worse, keep 0.)
RT_SAME_INCH = False

# ── Adaptive curing CO on sustained starvation (unified delay + switch lever) ──
# One threshold governs both directions the client wants:
#   • while a running press keeps getting GT (fed), it stays on its SKU and its
#     planned CO defers naturally — "if building can supply, CO can be planned later";
#   • when a press receives 0 GT for CURING_STARV_SWITCH_SHIFTS CONSECUTIVE shifts,
#     its SKU is building-limited → CO it to a SKU that building CAN supply (a
#     feedable in-demand SKU with GT + 2 free moulds).
# N=8 shifts ≈ 2.7 days ≈ the 3-day GT shelf life. Swept to find the optimum.
# OFF (default) or env CURING_ADAPT_CO=0 → today's schedule, bit-for-bit.
CURING_ADAPT_CO_ENABLED    = False   # OFF: conflicts with the adopted dynamic buffer (substitutes)
CURING_STARV_SWITCH_SHIFTS = 8
# Feed guard: only switch a GENUINELY building-limited SKU (buildable < curing draw),
# never one transiently starved because the buffer is busy elsewhere (which the buffer
# will feed). Fixes the buffer↔CO conflict (−12.4k→0) AND improves CO-alone (+563).
# ON by default so the 7k fallback (CURING_ADAPT_CO_ENABLED=True) uses the better path.
CURING_ADAPT_FEED_GUARD_ENABLED = True

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

INCH_PLUS3_CO_MINS = 480
# Building +3/-3 one-time inch escape (experiment). A machine may make ONE inch jump of
# exactly 3 (beyond the ±2 band) for the whole month; that jump is a major retooling
# costing this many minutes (8h) of building CO — vs the normal 88-180 min diff CO.
INCH_PLUS3_MIN_DAYS_LEFT = 5
# Only allow the +3/-3 escape when at least this many plan days remain, so the 8h CO is
# amortised by enough remaining production. Enforced when _INCH_PLUS3_ENABLED.

# ── Stepwise inch-DRIFT (INCH_STEP_DRIFT) — bounded relaxation of the historical inch-lock ──
# A STRANDED building machine (its historical inch's servable demand is done and it would
# otherwise sit IDLE this shift) may migrate its inch by ONE step (±1 adjacent inch) via a
# normal diff-size CO, one-way (never revert), to a DB-CERTIFIED adjacent inch that has real
# deficit + demand. Cumulative reach is capped at INCH_STEP_MAX (2) from the machine's
# historical inch, and a DIRECT ±3 jump is never allowed (must step 14→15→16, not 14→17).
# This re-enables DB-allowable capability the 4-month hist-lock stripped (invents no pairs),
# attacking the lock's SHIFT-LEVEL stranding (the temporal gap the monthly upper bound exposed)
# while KEEPING the lock as the base policy for machines that have their own work.
# Default OFF (env INCH_STEP_DRIFT=1) → today's plan bit-for-bit. Replaces the +3/-3 escape.
#
# MEASURED as a standalone GREEDY lever (July): the safe whole-month gate fires 0x (== OFF,
# 664,345) — a genuinely-done inch only frees late-month, too late to redirect profitably; the
# looser this-shift gate fires (6002/7003 15->14, 7106 13->14, 7501 12->13) but regresses -6,049
# (pulls capacity OFF scarce 15"/13" onto 14", one-way, can't return). Same trap as FIXED_ESCAPE.
# CONCLUSION: inch relaxation cannot pay off as a greedy per-shift reaction — the drift decision
# is myopic. The mechanism (DB-certified adjacent-inch, one-way, capped) is RETAINED OFF as the
# inch-relaxation CONSTRAINT MODEL for the Phase-2 global time-indexed optimizer, which can decide
# ahead of time to reserve a machine's drift for when the scarce inch actually needs it.
INCH_STEP_DRIFT_ENABLED = (os.environ.get("INCH_STEP_DRIFT", "0") != "0")
INCH_STEP_MAX = int(os.environ.get("INCH_STEP_MAX", "2"))   # max cumulative ±inch drift from base

# ── Lookahead buffer (LOOKAHEAD_BUF) — Phase-1a time-indexed pre-build sizing ──
# The dynamic buffer (_dyn_H) and the forward-buffer starvation-risk gate size a SKU's pre-build
# to the CURRENT shift's curing draw only — blind to a KNOWN incoming draw SPIKE (N presses
# scheduled to change over ONTO the SKU today). The spike is fully deterministic from the CO plan
# (co_press_map). When ON, both size to the ANTICIPATED peak draw = (presses running it + presses
# CO'ing to it today) × cure-rate, so a SKU about to get more presses is pre-built AHEAD instead of
# starving on the spike shift. Still bounded by demand cap + 3-day shelf + the 8k EOD GT cap → no
# waste GT (unlike IDLE_GAP_FILL, which clogged the cap −56k). Default OFF → today bit-for-bit.
#
# MEASURED (June/July/Aug, mould-audit PASS, deterministic): REJECTED — June −694 / July −3,552 /
# Aug −8,001 (= −12,247), and starvation UP on all three. Sizing to the anticipated PEAK over-buffers:
# it front-loads GT for spike-SKUs and diverts building from SKUs that need it NOW → they starve.
# Same front-loading failure mode as IDLE_GAP_FILL. Confirms the temporal gap is protected by the
# 3-day GT shelf (scarce GT can't be pre-built and banked) — a greedy pre-build lever can't beat it.
LOOKAHEAD_BUF_ENABLED = (os.environ.get("LOOKAHEAD_BUF", "0") != "0")

MAX_BUILDING_SKUS_PER_DAY = 4
# Plant rule: a single building machine may produce at most this many DISTINCT SKUs in
# one calendar day — the overnight carryover SKU counts as #1, so ≤3 changeovers after it.
# Both same-size and diff-size COs count. Per-machine (not plant-wide). Enforced when
# _BLD_SKU_CAP_ENABLED (env BLD_SKU_CAP) in b2c_pipeline.py; stacks on the per-shift CO cap.

MIN_INCH_DWELL_DAYS = 5
# Plant diff-size (inch-change) building-CO rule: once a machine builds an inch size
# it must stay on that size ≥ this many days before changing to a DIFFERENT inch —
# UNLESS the current size's demand it can serve is already completed on that machine
# (deficit-done override, then it may change early). A machine may run one size all
# month. Only enforced when _INCH_RULES_ENABLED (env INCH_RULES=1) in b2c_pipeline.py.

# Diff-size-CO amortization gate (env DIFF_CO_GATE in b2c_pipeline.py). Kills wasteful
# inch-hopping churn: a machine may do a DIFFERENT-inch CO only if (a) ≥ this many days
# since its last diff-size CO, and (b) the target inch offers ≥ DIFF_CO_MIN_TARGET_UNITS
# of sustained servable demand for it (amortizes the 88-180 min cost). Same-inch COs stay
# free. Tuned to drive diff-size COs from ~293 toward <60 while holding/raising KPI.
DIFF_CO_MIN_DWELL_DAYS  = 5       # min days between diff-size COs per machine (adopted: 5)
DIFF_CO_MIN_TARGET_UNITS = 300    # min sustained target-inch demand to justify a diff CO

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
    "STAGE1":   60,   # 6802–6803, 6909, 6911, 7601, 7701, 7801–7804, 8001–8003, 8101 (6801 retired)
    "MID":      60,   # same as Stage-1 (shared group in CO master)
    "UNISTAGE": 110,  # 7501–7503
    "PS":       30,   # ps3, ps4 (NEW 2026-08 GT machines) — plant same_size_CO = 30 min
}

BUILDING_CO_DIFF_SIZE = {
    # machine group  →  diff_size_CO duration (min)
    "STAGE2":   88,   # acceptable if no VMI alternative (88 min)
    "BJ":       90,   # 7101–7106, 7201
    "VMI":      40,   # 6001–6004, 7001–7004 — plant-confirmed (was wrongly 120); cheap inch change
    "STAGE1":   180,  # 37.5% of one shift — avoid unless critical demand
    "MID":      180,
    "UNISTAGE": 180,  # 7501–7503 — same as Stage-1
    "PS":       60,   # ps3, ps4 (NEW 2026-08 GT machines) — plant diff_size_CO = 60 min
}

# ── ps3 / ps4 NEW machines — MASTER ON/OFF toggle ────────────────────────────────────
# OFF (default) = the plant's ORIGINAL line WITHOUT ps3/ps4 (they are stripped from the
# building allowable entirely, as if not installed) → measures max production without the
# new machines. ON = ps3/ps4 active (their DB-allowable SKUs, CT 48, inch ps3=15"/ps4=16",
# CO 30/60). Env PS_MACHINES=1 forces ON, PS_MACHINES=0 forces OFF.
PS_MACHINES_ENABLED = False   # default OFF
# When ON, each ps machine may build at most this many units for the whole month (hard cap,
# applies in shared or dedicated mode). Plant's real monthly capacity for the new machines.
PS_MAX_BUILD = {"ps3": 10000, "ps4": 7000}

# ── ps3 / ps4 NEW-machine dedication (DYNAMIC per-month) ──────────────────────────────
# Each new GT machine is dedicated to a set of SKUs chosen FRESH from every month's demand:
# among its dominant-inch SKUs it is DB-allowable for, take the highest-demand ones cumulatively
# up to `max_build` (the machine's monthly capacity ceiling; "keep it low" = never exceed). Those
# SKUs become EXCLUSIVE to the ps machines (removed from the shared VMI pool). Selection is computed
# in building.load_machine_allowable(); env PS_EXCL_SKUS overrides with a fixed list, PS_EXCLUSIVE=0
# disables. Edit inch / cap here.
PS_DEDICATION = {
    "ps3": {"inch": "15", "max_build": 10000},   # us3 — 15", max 10k (aligned to PS_MAX_BUILD)
    "ps4": {"inch": "16", "max_build": 7000},    # us4 — 16", max 7k  (aligned to PS_MAX_BUILD)
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

MAX_ENDOFDAY_GT_INVENTORY = int(os.environ.get("GT_CAP_MAX", "8000"))
# Raised 7k→10k to let the dynamic GT buffer (below) fill the storage it needs for
# the +20.6k coverage gain (buffer natural depth ~9.3k ≤ 10k, verified days-over=0).
# This assumes the plant can hold ~10k GT overnight — a PLANT STORAGE decision.
# Plant capacity constraint: total GT held in inventory at the END of any day
# (summed over all SKUs, after curing + stale writeoff) cannot exceed this many
# units. Enforced PROACTIVELY during building (never build past the ceiling) so
# it is a hard cap, not a reactive writeoff. Bounds the forward-buffer level-load.

# End-of-day CARCASS inventory cap (analogous to the 8k GT cap, but for Stage-1
# carcass held overnight). Carcass has a 1-day shelf, so a limited buffer of
# pre-built carcass may be carried between shifts/overnight to back a Stage-2 burst;
# this bounds that buffer so the plant never hoards carcass. Applied only when the
# STAGE1_CO carcass-realism model is ON (bounds the gate's pre-build). HARD plant limit
# 1200 (was 2000). Env CARCASS_EOD_CAP.
MAX_ENDOFDAY_CARCASS_INVENTORY = int(os.environ.get("CARCASS_EOD_CAP", "1200"))

# ── Carcass build-to-consumption (CARCASS_NO_OVERBUILD) — ADOPTED, default ON ──
# The Stage-1 carcass GATE used to size its per-shift pre-build to max(Stage-2 build rate,
# CURING DRAW). Curing draw is the TOTAL GT a SKU's presses pull, which includes GT built by
# BJ/Unistage groups that need NO carcass — so for a split-group SKU (e.g. 1225170015012LSTL0:
# curing draw 899/shift vs Stage-2 carcass consumption 470/shift) the gate targeted a carcass
# buffer ~1.9x what Stage-2 actually consumes. The surplus had no consumer, aged out on the
# 1-day shelf, and was rebuilt next shift — ~21k units of phantom Stage-1 work for that one SKU
# (64,880 built vs 43,670 consumed), none of it KPI-visible (the output rows were already capped
# at consumption) but wasteful and it broke the carcass-row timeline (rows front-loaded to early
# days, then dropped the real late carcass as "aged-out tail" — the display bug). When ON, the
# gate pre-builds to the Stage-2 BUILD rate only (the true carcass consumer), so carcass built
# ~= carcass consumed with no aging-out; PASS 1 (same-shift Stage-2 shortfall) is UNTOUCHED so
# Stage-2 never starves for carcass (invariant #3). Also drives the carcass-row builder to a
# time-windowed FIFO match (rows attributed to the day/shift their GT is consumed, within the
# 1-day aging window; any still-aged-out carcass is NOT shown). Env CARCASS_NO_OVERBUILD=0
# reverts to the over-build + front-loaded-rows behaviour bit-for-bit.
CARCASS_NO_OVERBUILD_ENABLED = (os.environ.get("CARCASS_NO_OVERBUILD", "1") != "0")

GT_BUFFER_SHIFTS        = 2
# VMI sibling machines (e.g. 6004+7001, both on 16") both need non-zero deficit
# to stay active. With _buf=2, target = 2× cure rate → each sibling fills ~1 shift
# worth → total 2 shifts of buffer maintained.
# BJ/UNISTAGE/STAGE use 1× (see _assign_building_shift _buf logic).

# Flat GT pre-build buffer depth per group (shifts of cure-draw to bank ahead). Lever D
# for idle-machine recovery under the inch rules: raising these lets an idle/pinned
# machine bank more current-inch GT (bounded by the demand cap + MAX_ENDOFDAY_GT_INVENTORY,
# so no overbuild/excess carry). Defaults = today's values (VMI 2 / others 1) so the
# baseline is unchanged; test the recovery config with GT_BUF_VMI=3 GT_BUF_OTHER=2.
GT_BUFFER_SHIFTS_VMI    = int(os.environ.get("GT_BUF_VMI",   "2"))
GT_BUFFER_SHIFTS_OTHER  = int(os.environ.get("GT_BUF_OTHER", "1"))

# ── Dynamic GT buffer (Phase 1, DYN_BUFFER) ──────────────────────────────────
# When DYN_BUFFER_ENABLED, the flat GT_BUFFER_SHIFTS_* is replaced by a per-SKU,
# per-shift buffer horizon H_s (shifts) computed from live curing draw, feeder
# contention (away-time proxy) and starvation risk:
#   H_s = clip( round( floor_g · (1 + ALPHA·Contention_s + BETA·RiskShort_s) ),
#               floor_g, GT_SHELF_LIFE_SHIFTS )
# floor_g = FLOOR_VMI for VMI-fed SKUs else FLOOR_OTHER (the old flat values act as
# FLOORS). Buffer target B_s = draw_s · H_s. OFF (default) or env DYN_BUFFER=0 →
# the flat per-group buffer, bit-for-bit. ALPHA/BETA tuned by the KPI sweep.
DYN_BUFFER_ENABLED = True    # ADOPTED — the +20.6k lever (needs the 10k GT cap above)
DYN_BUF_FLOOR_VMI   = 2
DYN_BUF_FLOOR_OTHER = 1
DYN_BUF_ALPHA       = 0.0    # contention term OFF — it over-buffered the press-tight month
DYN_BUF_BETA        = 2.0    # risk-driven depth; natural GT peak ~9.3k, fits the 10k cap
# GT-cap fairness: the dynamic buffer's overnight-carry excess is bounded by the 7k
# end-of-day cap, but curing DRAINS GT during the day, so reserving the full entry
# carry under-fills the legal storage. Credit this many shifts of total curing draw
# back into the headroom so the buffer can fill the 7k (0 = fully conservative, the
# old too-tight bound). The retest verifies end-of-day GT still never exceeds the cap.
DYN_BUF_CURE_CREDIT = 1.0

# ── Global scored building assignment (Phase 2, GLOBAL_SCORE_V2) ──────────────
# Replaces the Phase-A(continuation)+Phase-B(_key ranking) two-pass ordering in
# _assign_building_shift with ONE unified scored pass: every (machine,SKU) candidate
# — INCLUDING each machine's current SKU folded in as the CO=0 continuation — is
# scored by a single utility U and assigned best-first (no primary/secondary). U =
# w_def·ñDeficit + w_starv·ñStarv + w_gap·ñGap + w_scarce·(1/feeders)·[starving]
#   − w_co·ñCO_min − w_inch·ñInchPenalty − w_over·[gt≥draw·H]. Pull terms
# (deficit/starv/gap) and push terms (CO/inch) are min-max normalized over the
# candidate set; scarcity + over-buffer are structural indicators. Graded inch
# penalty uses the machine's ranked dominant band. OFF (default) or env
# GLOBAL_SCORE_V2=0 → the committed _key path, bit-for-bit. Weights tuned by sweep.
GLOBAL_SCORE_V2 = False
GS_W_DEF     = 1.0    # ñ this-shift (dynamic-buffer) deficit          (pull)
GS_W_STARV   = 1.0    # ñ near-dry starvation 1/(gt/draw+eps)          (pull)
GS_W_GAP     = 0.5    # ñ cumulative monthly unmet-demand gap          (pull)
GS_W_SCARCE  = 1.0    # (1/|feeders_s|)·[s starving] sole-feeder bonus (pull)
GS_W_CO      = 0.75   # ñ changeover minutes CO_min(m,s)               (push)
GS_W_INCH    = 0.5    # ñ inch-band position InchPenalty(m,s)          (push)
GS_W_OVER    = 1.0    # [gt ≥ draw·H_s] over-buffer indicator          (push)
GS_INCH_OFFBAND = 5   # penalty added on top of band length for an off-band inch

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

# ── Opening carcass inventory-first (CARCASS_INV) ─────────────────────────────
# Consume the plant's opening Stage-1 carcass (jkplanningV1.carcass_inventory_manual,
# SKU-keyed = sizeCode, col CarcassInv, plan_month-filtered) BEFORE the Stage-1 carcass
# scheduler builds new carcass — the exact analog of opening GT (gt_inventory_manual).
# Consumed only within the carcass SHELF window from Day-0 (CARCASS_SHELF_LIFE_DAYS) and
# only against the SAME SKU code. NOTE: carcass is POST-HOC in the rolling pipeline (it
# does NOT gate GT/cured), so this is KPI-NEUTRAL — it only makes Stage-1 utilization/output
# realistic and LOWERS the carcass INFEASIBLE count. Default OFF (env CARCASS_INV=1) = A/B;
# OFF reproduces the current carcass schedule bit-for-bit.
CARCASS_INV_ENABLED = (os.environ.get("CARCASS_INV", "1") != "0")   # ADOPTED (KPI-neutral realism)

# ── Stage-2 carcass GATE (hard constraint) ───────────────────────────────────
# When ON, Stage-2 GT each shift is CAPPED by feasible same-shift Stage-1 carcass
# supply: Stage-2 can NEVER build GT the carcass can't back — it WAITS for carcass.
# Enforces invariant #3 as a scheduling constraint (not just the post-hoc report),
# so the plan is physically realizable (Stage-2 GT total == Stage-1 carcass total).
# Only ever REDUCES Stage-2 GT (demand cap / no-waste-GT safe); can lower cured.
# Committed default: ON (env STAGE2_CARCASS_GATE=0 forces OFF for A/B). Enforces
# invariant #3 — Stage-2 never builds GT without carcass. Engine reads this via getattr.
STAGE2_CARCASS_GATE_ENABLED = (os.environ.get("STAGE2_CARCASS_GATE", "1") != "0")

# ── Stage-1 building CHANGEOVER time (STAGE1_CO) ──────────────────────────────
# When ON, the 15 Stage-1 (carcass) machines are charged real building CO time —
# same_size_CO = 60 min, diff_size_CO = 180 min (flat, all 15) — exactly like the
# 24 GT machines: NO production during the CO block. It is BINDING: a Stage-1
# machine that changes carcass SKU makes fewer units that shift, so the Stage-2
# carcass GATE clamps Stage-2 GT where carcass+CO cannot keep up (correctness over
# KPI; only ever REDUCES cured, demand-cap / no-waste-GT safe). Previously Stage-1
# changeovers were FREE (0 min) in both the gate feasibility and the post-plan
# carcass rows — a real-plant modelling gap. Requires the Stage-2 carcass gate ON.
# ADOPTED default ON (verified July/Aug/June: mould-audit PASS, carcass=Stage-2 GT
# exactly, EOD carcass ≤ MAX_ENDOFDAY_CARCASS_INVENTORY=1200 with 0 days over,
# deterministic). Carcass is shown = exactly what Stage-2 consumes (see CARCASS_NO_OVERBUILD:
# build-to-consumption + FIFO row match, aged-out carcass never built/shown). KPI cost
# is the honest price of the hard 1200 carcass-storage limit + real Stage-1 CO — proven
# unrecoverable by Stage-1 reallocation (Stage-1 is ~48% idle, 0 inch shortages) or by
# cutting Stage-2 COs (that STARVES the 6 bottleneck presses — Aug −22k). Env STAGE1_CO=0
# reverts to the free-Stage-1-CO baseline bit-for-bit. Pinned in main.CLOUD_CONFIG.
STAGE1_CO_ENABLED = (os.environ.get("STAGE1_CO", "1") != "0")

# ── Stage-2 campaign consolidation (S2_CAMPAIGN) ──────────────────────────────
# Cuts the churn of the 6 Stage-2 GT machines {8201,8301,8302,8501,8502,7301} so
# per-shift CARCASS demand is smoother and the 1200-unit overnight carcass
# buffer (MAX_ENDOFDAY_CARCASS_INVENTORY) can absorb it — recovering
# the KPI that spiky carcass demand costs WITHOUT raising the cap. STAGE2-only knobs,
# all no-ops when OFF (bit-for-bit). The ADOPTED lever is S2_MIN_CAMPAIGN_MINS: a
# Stage-2 CO to a NEW sku must yield a campaign >= this long (default 185 min), else
# the machine idles rather than doing a short churn switch — this frees CO time that
# becomes longer productive campaigns (Stage-2 GT actually rises). 185 is the middle
# of a measured stable [180,190] step (>=200 regresses sharply, so keep it here). The
# other two knobs were measured WORSE on July and default to no-ops: S2_SKU_CAP (tighter
# distinct-SKU/day cap; 4 = the plant-wide cap = no-op, tighter HURT) and S2_MAX_CO_PER_DAY
# (blunt per-day CO budget; 0 = disabled, budget<=2 collapses production). July, STAGE1_CO=1:
# OFF 663,700 / 217 Stage-2 COs -> ON 665,599 (+1,899) / 142 COs, carcass=GT, EOD<=2000,
# mould-audit PASS, deterministic. Committed default OFF (env S2_CAMPAIGN=1 to enable).
S2_CAMPAIGN_ENABLED = (os.environ.get("S2_CAMPAIGN", "0") != "0")
S2_SKU_CAP = int(os.environ.get("S2_SKU_CAP", "4"))               # 4 = no-op (tighter HURT July)
S2_MIN_CAMPAIGN_MINS = int(os.environ.get("S2_MIN_CAMPAIGN_MINS", "185"))  # mid of the [180,190] plateau
S2_MAX_CO_PER_DAY = int(os.environ.get("S2_MAX_CO_PER_DAY", "0"))  # 0 = disabled (blunter alt to min-camp)

# ── Concentration allocation (CONC_ALLOC) — fewer machines per SKU, longer campaigns ──
# RCA finding: the per-shift greedy has NO cap on how many building machines pile onto one
# SKU. A high-demand SKU shows a large deficit on EVERY eligible machine at once (its
# dynamic-buffer target is draw × up-to-9 shifts), so machine after machine grabs it in the
# same shift — over-provisioning mid-tier SKUs (draw < one machine's rate, yet 2-3 machines)
# into many short campaigns (July: ~464 excess campaigns, ~87 excess machine-assignments).
# This lever adds a per-shift OVER-PROVISION penalty to the Phase-B/-C selection: the FIRST
# machine on a SKU each shift is always free; an ADDITIONAL machine is DEFERRED (ranked below
# any still-under-served SKU) once this shift's committed build already keeps pace with the
# SKU's curing draw AND the SKU is not about to run dry. It is a DEFERRAL, never a block — a
# machine with no under-served eligible SKU still builds the paced one (no forced idle), so
# KPI is protected while campaigns concentrate. DEVIATION OVERRIDE: a STARVING SKU (on-hand
# GT < draw × CONC_STARV_SHIFTS) always admits extra machines so a behind SKU can be rescued
# fast — this is what keeps the concentration from hurting starvation recovery (the failure
# mode that sank SKU/day-cap=3 at −32,330 and IDLE_GAP_FILL at −56k). The pace test yields
# ~ceil(draw/rate) machines per SKU (mega-SKUs like 1225170015012LSTL0 still get ~4; mid-tier
# get 1). Env CONC_ALLOC=1 to enable; default OFF = current selection bit-for-bit.
CONCENTRATION_ENABLED = (os.environ.get("CONC_ALLOC", "0") != "0")
# Starvation-override threshold (shifts of draw on-hand below which a SKU is "about to run
# dry" and may still take extra machines). 1.0 ties to the forward-buffer risk gate
# (_FWD_RISK_SHIFTS). Higher = more aggressive rescue (more machines allowed) = weaker
# concentration; lower = stricter concentration. Env CONC_STARV_SHIFTS.
CONC_STARV_SHIFTS = float(os.environ.get("CONC_STARV_SHIFTS", "1.0"))

# ── Delivery-date / priority-flag committed-delivery SKUs (DELIVERY_PRIORITY) ──
# Client feature: the demand file may carry two optional columns —
#   "Priority Flag"  (values "0" / "1" / "Yes"; blank/NaN = not set), and
#   "Delivery Date"  (string "DD/MM/YY"; optional).
# A SKU becomes DELIVERY-COMMITTED when its flag is set OR it carries a valid
# delivery date (a date implies commitment even if the flag reads "No"/"0"/blank).
# Committed behaviour:
#   • flag set, no date → SKU must be FULLY cured by the END OF THE PLAN MONTH;
#   • with a date       → SKU must be FULLY cured ON/BEFORE that date.
# Meeting these dates is MORE important than overall KPI (client accepts a KPI
# drop). One shared, self-pacing deadline-urgency signal drives BOTH the Phase-0
# curing-CO scheduler (acquire/hold presses, EDF order, never CO a committed press
# away pre-deadline) AND building assignment (build its GT first) — curing is
# derived from building, so both stages must be delivery-aware. Competing
# commitments are resolved Earliest-Deadline-First (EDF); an infeasible SKU is
# best-effort + reported (shortfall + earliest-feasible date), never a hard-stop.
# Every edit is ORDERING-ONLY: the demand cap, historical inch-lock, mould
# feasibility, GT shelf + end-of-day cap are untouched, and no machine↔SKU pair is
# ever invented (DB-allowable + DB mould eligibility only).
# Default ON but INERT (bit-for-bit) when no priority data is present (June, cloud
# jkt_demand which has no such columns). Env DELIVERY_PRIORITY=0 forces identity
# everywhere. See b2c_pipeline._build_priority_deadline_map.
# >>> The master toggle DELIVERY_PRIORITY_ENABLED (+ DELIVERY_PRIORITY_UNDATED_TO_MONTHEND)
#     is defined at the TOP of this file (★ FEATURE TOGGLE block) so it is easy to find/flip.

# ══════════════════════════════════════════════════════════════════════════════
# 5. CURING SIMULATION  →  curing_b2c.py
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_CURING_CT = 17.0
# Fallback cure cycle time (minutes) used when a SKU's CT is absent from
# Master_Curing_Design_CycleTime. Typical PCR press CT is 15–20 min.

CURING_PRESS_COUNT = 170
# FIXED denominator for the curing capacity-utilisation KPI = number of curing
# presses in the plant roster (Master_Curing_Allowable_Machines_source has 170).
# Used for BOTH the daily rows in jkt_plan_capacityUtilisation and the monthly
# curing figure in jkt_plan_kpis, so daily average == monthly. The live
# running-moulds snapshot may hold a few extra presses than the roster; fixing
# the denominator keeps the KPI stable and comparable across runs. Change this
# one line when the curing-press roster changes.

# ── Restrict the press roster to the allowable matrix (LOCAL-ONLY, default OFF) ──
# The live Daily_Running_Moulds snapshot sometimes carries MORE unique presses
# than the 170-press allowable matrix (Master_Curing_Allowable_Machines_source):
# e.g. July has 2 extra (85214, 85215) and August 8 extra (85207–85215). Those
# presses are not in any SKU's allowable list, so they can run their Day-0 SKU
# but can never CO anywhere. When RESTRICT_PRESSES_TO_ALLOWABLE is ON, the
# running-moulds snapshot is filtered so ONLY the 170 allowable presses exist
# (assume no other press exists); their Day-0 moulds return to the free pool.
# LOCAL-ONLY: local_main.py passes this flag into run_rolling_pipeline; the cloud
# path (main.py) never passes it, so cloud is unaffected regardless of this value.
# ADOPTED business rule (default ON): only the 170 DB-allowable presses exist.
# Env PRESS_ALLOWABLE_ONLY=0 reverts to including stray non-roster presses.
RESTRICT_PRESSES_TO_ALLOWABLE = (os.environ.get("PRESS_ALLOWABLE_ONLY", "1") != "0")

# IDLE_PRESS_ACTIVATE — the other half of the 170-press business rule (default ON):
# any of the 170 roster presses ABSENT from the Day-0 running-moulds snapshot is
# brought online via a cold-start curing CO (nothing -> SKU) in Day-1 Shift A, then
# produces from Day-1 Shift B. Target = neediest allowable SKU with 2 free moulds.
# Together with RESTRICT_PRESSES_TO_ALLOWABLE this makes every run simulate EXACTLY
# the 170 certified presses. Env IDLE_PRESS_ACT=0 reverts. Logic in b2c_pipeline.py.
# KPI vs OFF (measured, mould-audit PASS, deterministic): June +8,118 / July -917 /
# Aug -3,839 (net +3,362) — adopted as a business/correctness rule, not a KPI lever.
IDLE_PRESS_ACTIVATE_ENABLED = (os.environ.get("IDLE_PRESS_ACT", "1") != "0")

# ── Curing press changeover times ────────────────────────────────────────────
# A curing press CO occupies 2 consecutive shifts:
#   Shift A (CO day)  → CHANGEOVER   (press idle, mould swap)
#   Shift B (CO day)  → MOULD_CLEAN  (press idle, mould clean)
#   Shift C (CO day)  → PRODUCTION begins on new SKU
# Building for the new SKU must start simultaneously with Shift A (see CLAUDE.md).

CURING_CO_DURATION_SHIFTS  = 1     # shifts idle during CO: Shift A only (CHANGEOVER).
#                                    Mould clean removed from scheduler model.
CURING_CO_CHANGEOVER_MINS  = 480   # Shift A: press occupied for changeover (full shift = 480 min)
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

# Building machine CODE → plant NAME (client-supplied). Output sheets show the name
# in a column next to every building-machine code so the plant reads plans by name,
# not code. Curing PRESS ids are NOT in this dict (presses stay code-only). A code
# not found here falls back to "NA".
BUILDING_MACHINE_NAMES = {
    "6001": "VMIExxium01", "6002": "VMIExxium02", "6003": "VMIExxium03", "6004": "VMIExxium04",
    "6802": "bj2stage1",   "6803": "bj3stage1",
    "6909": "nrm9stage1",  "6911": "nrm11stage1",
    "7001": "vmi1Maxx",    "7002": "vmi2Maxx",    "7003": "vmi3Maxx",    "7004": "vmi4Maxx",
    "7101": "bj4", "7102": "bj5", "7103": "bj6", "7104": "bj7", "7105": "bj9", "7106": "bj10",
    "7201": "bj8", "7301": "newirm",
    "7501": "us1", "7502": "us2", "7503": "us3",
    "7601": "ltmstage1",   "7701": "midland5stage1",
    "7801": "midland1stage1", "7802": "midland2stage1", "7803": "midland3stage1", "7804": "midland4stage1",
    "8001": "sai1stage1",  "8002": "sai2stage1",  "8003": "sai3stage1",
    "8101": "88d1stage1",  "8201": "oldirm",      "8301": "gtic1",       "8302": "gtic2",
    "8501": "vmi1",        "8502": "vmi2",
}

# ══════════════════════════════════════════════════════════════════════════════
# 7. OUTPUT PATHS  —  derived automatically from PLAN_START
# ══════════════════════════════════════════════════════════════════════════════

_OUT      = OUTPUT_DIR
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

# 9 runs executing (~12 min): 3 months × {baseline, rules@cap12, rules@cap14}, all on Daily_Running_Moulds.

# -- 
