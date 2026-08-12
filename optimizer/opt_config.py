"""optimizer/opt_config.py — SINGLE consolidated config for the CP-SAT optimizer model.

EDIT VALUES HERE. Every optimizer file (data.py / model.py / driver.py) reads these — this file
pushes each value into the environment variable that file already reads, so this is the one place
to change anything. An explicit shell env var still overrides a value here (for quick A/B tests).

`optimizer/main.py` imports THIS FIRST, then runs the whole model. So:  myenv/bin/python -m optimizer.main

Grouped: PLAN · SOLVE/WINDOWS · CURING · BUILDING CO · CAPS/LIMITS · INCH ALLOCATION · POST-PROCESS.
"""
import os


def _cfg(env_key, value):
    """Register a config value: set the env var the optimizer reads (unless already overridden
    by a real shell env var), and return the value for reference/printing."""
    os.environ.setdefault(env_key, str(value))
    # reflect any real override back into the returned value
    v = os.environ.get(env_key, str(value))
    try:
        return type(value)(v)
    except Exception:
        return v


# ══════════════════════════════ 1. PLAN (month / demand / horizon) ══════════════════════════════
# Change these 3 to run a different month. plan_month = YYYY-MM is derived and drives the DB reads
# for opening GT (gt_inventory_manual), opening carcass, and the Daily_Running_Moulds snapshot.
MONTH_YEAR    = _cfg("MONTH_YEAR", 2026)
MONTH_NUM     = _cfg("MONTH_NUM",  8)                 # 6 | 7 | 8 ...
PLANNING_DAYS = _cfg("MONTH_DAYS", 32)               # 29 (Aug) | 31 (Jul) | 30 (Jun)
DEMAND_FILE   = _cfg("MONTH_DEMAND", "correct_july.xlsx")   # file under data/input/

PLAN_MONTH = f"{int(MONTH_YEAR):04d}-{int(MONTH_NUM):02d}"
os.environ.setdefault("RUNNING_MOULDS_MONTH", PLAN_MONTH)   # opening GT + carcass + running moulds
os.environ.setdefault("PLAN_MONTH", PLAN_MONTH)
os.environ["RUNNING_MOULDS_MONTH"] = os.environ.get("RUNNING_MOULDS_MONTH", PLAN_MONTH)
os.environ["PLAN_MONTH"]           = os.environ.get("PLAN_MONTH", PLAN_MONTH)

# ══════════════════════════════ 2. SOLVE / ROLLING WINDOWS ══════════════════════════════
# The month is solved as ~3-4 rolling windows of WINDOW_COMMIT_DAYS committed + WINDOW_LOOKAHEAD
# lookahead. Each window is solved for DET_TIME_PER_WINDOW deterministic seconds.
DET_TIME_PER_WINDOW = _cfg("OPT_DET_TIME", 600)      # seconds per window (deterministic stop)
WORKERS             = _cfg("OPT_WORKERS", 8)
WARMSTART           = _cfg("WARMSTART", "greedy")    # "greedy" | "simple" | "none"
WINDOW_COMMIT_DAYS  = _cfg("OPT_COMMIT_DAYS", 10)    # days committed per window
WINDOW_LOOKAHEAD    = _cfg("OPT_LOOKAHEAD", 3)       # lookahead tail (covers 3-day GT shelf)
CO_PENALTY          = _cfg("OPT_CO_PEN", 80)         # cured-units charged per curing CO

# ══════════════════════════════ 3. CURING (rate / efficiency / moulds) ══════════════════════════════
DEFAULT_CURING_CT = _cfg("OPT_DEFAULT_CURING_CT", 17.0)   # min, fallback when a SKU has no DB CT
PRESS_EFFICIENCY  = _cfg("OPT_PRESS_EFF", 0.94)           # real cure rate = int(480*eff/CT)*2 (data.py)
CURING_CAVITIES   = _cfg("OPT_CAVITIES", 2)               # tyres per cycle
MAX_CO_PER_DAY    = _cfg("OPT_MAX_CO_PER_DAY", 12)        # curing changeovers per calendar day

# ══════════════════════════════ 4. BUILDING CO TIMES ══════════════════════════════
# Per-group same/diff-size CO minutes live in bc_config.BUILDING_CO_SAME_SIZE / _DIFF_SIZE (dicts
# by machine group) — edit them THERE. Same-size CO is charged post-process (driver _same_co_trim);
# diff-inch CO is modelled in CP-SAT with these per-group penalties + monthly budgets:
PEN_DIFF_S2   = _cfg("OPT_PEN_DIFF_S2",  0)          # Stage-2: unlimited inch changes
PEN_DIFF_VMI  = _cfg("OPT_PEN_DIFF_VMI", 12)
PEN_DIFF_BJ   = _cfg("OPT_PEN_DIFF_BJ",  12)
PEN_DIFF_UNI  = _cfg("OPT_PEN_DIFF_UNI", 20)
PEN_DIFF_S1   = _cfg("OPT_PEN_DIFF_S1",  20)
DIFF_CAP_VMI  = _cfg("OPT_DIFF_CAP_VMI", 4)          # per-machine monthly diff-CO budget (VMI = flex group)
DIFF_CAP_BJ   = _cfg("OPT_DIFF_CAP_BJ",  2)          # BJ stays on dominant 13/15/16 -> high util
DIFF_CAP_UNI  = _cfg("OPT_DIFF_CAP_UNI", 2)
DIFF_CAP_S1   = _cfg("OPT_DIFF_CAP_S1",  2)
# S2 stays uncapped (OPT_DIFF_CAP_S2 not set = 0 = unlimited; GT bottleneck needs inch freedom)

# ══════════════════════════════ 5. CAPS / LIMITS ══════════════════════════════
GT_CAP_PER_SHIFT   = _cfg("GT_CAP_MAX", 8000)        # total GT held per shift (plant storage)
CARC_CAP_PER_SHIFT = _cfg("CARC_CAP_MAX", 1200)      # total carcass held per shift
DAY_CURE_CAP       = _cfg("OPT_DAY_CURE_CAP", 0)     # 0 = NO daily cure cap (build max, bounded by aging+moulds)
MIN_CAMPAIGN       = _cfg("OPT_MIN_CAMP", 0)         # 1 = >=40/shift floor in-model (0 = off; breaks warm-start)
MIN_CAMPAIGN_UNITS = _cfg("OPT_MIN_CAMP_UNITS", 40)

# ══════════════════════════════ 6. INCH ALLOCATION ══════════════════════════════
INCH_FLEX       = _cfg("OPT_INCH_FLEX", 0)           # 0 = NARROW historical inch sets (converges; beats greedy).
                                                     # 1 = FULL DB-allowable flex = search-space explosion -> solver
                                                     # drowns, commits BELOW greedy warm-start (Aug 542k<616k). KEEP 0.
INCH_RANK_STEP  = _cfg("OPT_INCH_RANK_STEP", 0)      # per-shift off-dominant penalty (0 = rely on CO cost)
INCH_DWELL      = _cfg("OPT_INCH_DWELL", 1)          # min shifts on an inch once switched (1 = off)
PLANT_ALLOW     = _cfg("OPT_PLANT_ALLOW", 0)         # 0 = DB-allowable ONLY (no cross-recipe expansion)
DOM_INCH        = _cfg("OPT_DOM_INCH", "")           # forced dominant/2nd inch overrides (empty = none)
# STEP 4 — TARGETED scarce-inch flex (env OPT_TARGET_FLEX). MEASURED A REGRESSION on Aug:
# 15"-on-idle-VMI flex added diff-COs -> more same-CO trim + aging + worse convergence ->
# 601,889 (87.29%) vs narrow 615,993 (89.33%), -14k. LEFT OFF. The 89.33% is a hard attractor;
# inch allocation is not the lever to 640k (the ~14k same-CO post-process cut is the real loss).
TARGET_FLEX        = _cfg("OPT_TARGET_FLEX", "")             # "" = OFF (narrow hist-lock = best). "13,15" regressed.
TARGET_FLEX_GROUPS = _cfg("OPT_TARGET_FLEX_GROUPS", "VMI")

# ══════════════════════════════ 7. POST-PROCESS (driver) ══════════════════════════════
SAME_CO_TRIM   = _cfg("OPT_SAME_CO_TRIM", 1)         # charge same-size CO time post-solve
SAME_CO_REDIST = _cfg("OPT_SAME_CO_REDIST", 1)       # redistribute over-packed GT vs cut
BUILT_CAP      = _cfg("OPT_BUILT_CAP", 1)            # built <= demand per SKU
AGE_TRIM       = _cfg("OPT_AGE_TRIM", 1)             # GT 3-day aging write-off
CARC_RESCHED   = _cfg("OPT_CARC_RESCHED", 1)         # JIT Stage-1 carcass reschedule (1-day shelf)
MOULD_CLEAN    = _cfg("OPT_MOULD_CLEAN", 1)          # 8h mould clean at life 0
ENDDAY_GT      = _cfg("OPT_ENDDAY_GT", 1)
MOULD_INFO     = _cfg("OPT_MOULD_INFO", 1)
CURE_CLIP      = _cfg("OPT_CURE_CLIP", 1)
GT_JIT         = _cfg("OPT_GT_JIT", 0)               # in-model build pacing (0 = off; convergence risk)

# ══════════════════════════════ 8. PHYSICAL CONSTANTS (plant physics — rarely change) ══════════════════════════════
SHIFT_MINS              = _cfg("OPT_SHIFT_MINS", 480)        # minutes per shift (A / B / C)
SHIFTS_PER_DAY          = _cfg("OPT_SHIFTS_PER_DAY", 3)
GT_SHELF_LIFE_DAYS      = _cfg("OPT_GT_SHELF_DAYS", 3)       # GT aging: must be cured within 3 days
CARCASS_SHELF_LIFE_DAYS = _cfg("OPT_CARC_SHELF_DAYS", 1)     # carcass aging: consumed by Stage-2 within 1 day
CURING_CO_MINS          = _cfg("OPT_CURING_CO_MINS", 480)    # curing changeover = one full shift
MOULD_CLEAN_MINS        = _cfg("OPT_MOULD_CLEAN_MINS", 480)  # 8h mould clean = one full shift
MOULD_CLEAN_CYCLES      = _cfg("OPT_MOULD_CLEAN_CYCLES", 3000)  # clean after 3000 cycles (= 6000 tyres)


def summary() -> str:
    return (f"MONTH={PLAN_MONTH} days={PLANNING_DAYS} demand={DEMAND_FILE} | "
            f"det_time={DET_TIME_PER_WINDOW}s/window workers={WORKERS} warmstart={WARMSTART} | "
            f"press_eff={PRESS_EFFICIENCY} day_cure_cap={DAY_CURE_CAP} GT_cap={GT_CAP_PER_SHIFT} | "
            f"plant_allow={PLANT_ALLOW} inch_flex={INCH_FLEX} dom_inch={DOM_INCH!r}")
