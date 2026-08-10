"""Single-window joint building+curing CP-SAT model (v1 core).

Key formulation choice: for a MAX-CURED objective the curing side is modeled by
per-SKU **press COUNTS** n[s,t] (not per-press booleans) — this collapses the model
from ~490k booleans to ~30k vars and is what makes it solvable. Which physical press
cures which SKU is a bipartite matching recovered/audited post-hoc (count relaxation).

v1 scope: building GT (Stage-2 + Unistage) + curing counts + GT reservoir + mould
count-cap + curing-CO cost + demand cap + historical inch-lock, objective = max cured.
Deferred to v2 (TODO): Stage-1 carcass gating, campaigns/min-dwell, inch-lock
RELAXATION budgets, shared-mould contention + lazy exact-bipartite cuts.

Determinism: single worker, fixed seed, stop on max_deterministic_time (NOT wall).
"""
from __future__ import annotations

import os

from ortools.sat.python import cp_model

import bc_config
from optimizer.data import ModelInputs

MAX_CO_PER_DAY = int(getattr(bc_config, "MAX_CHANGEOVERS_PER_DAY", 12))
GT_CAP         = int(os.environ.get("GT_CAP_MAX", str(getattr(bc_config, "MAX_ENDOFDAY_GT_INVENTORY", 8000))))
CARC_CAP       = int(os.environ.get("CARC_CAP_MAX", str(getattr(bc_config, "MAX_ENDOFDAY_CARCASS_INVENTORY", 1200))))
# Phase-1 pacing (anti-front-load + min-campaign). OFF (=0) reproduces the current plan.
MIN_CAMP       = os.environ.get("OPT_MIN_CAMP", "0") == "1"      # build >=MIN_CAMP_UNITS or 0 (no slivers)
MIN_CAMP_UNITS = int(os.environ.get("OPT_MIN_CAMP_UNITS", str(getattr(bc_config, "MIN_CAMPAIGN_UNITS", 40))))
DAY_CURE_CAP   = int(os.environ.get("OPT_DAY_CURE_CAP", "0"))    # units/day HARD cap on total cured; 0 = off
# Phase-2 dominance-ranked inch flex: allow all DB-allowable inches, graded soft penalty by
# rank off dominant (data.py widens machine_allowed_inches + builds machine_inch_rank). OFF = 0.
INCH_FLEX      = os.environ.get("OPT_INCH_FLEX", "0") == "1"
INCH_RANK_STEP = int(os.environ.get("OPT_INCH_RANK_STEP", "6")) # cured-units penalty per rank-step off dominant
INCH_DWELL     = int(os.environ.get("OPT_INCH_DWELL", "9"))    # min shifts a machine holds an inch once switched (anti-thrash; 9=3d, 15=5d)
GT_GROUPS      = ("S2", "UNI")

# Objective shaping (env-tunable). CO_PENALTY = cured-units charged per curing CO
# (keeps productive COs, cuts gratuitous churn; must be small — NOT a x1000-scale
# coeff that would re-break CP-SAT search). TERM_GT_W = weight on end-of-window GT
# inventory (value-to-go) to stop the rolling windows front-loading — 0 = off.
CO_PENALTY = int(os.environ.get("OPT_CO_PEN", "40"))     # cured-units/CO: higher -> COs only "when needed"
TERM_GT_W  = int(os.environ.get("OPT_TERM_GT", "0"))     # front-loading fix; enabled in step 2

SHIFT_MINS = int(getattr(bc_config, "SHIFT_MINS", 480))

# ------------------------------------------------------------------------------
# BUILDING DIFF-INCH-CHANGEOVER PRICING (OPT_BLD_CO, default ON). The joint model priced
# ONLY curing COs: building machines could flip inch every shift for FREE (not penalized,
# not charged against build capacity). The plant wants same-size (recipe) COs HIGH and only
# DIFF-size (inch) COs LOW, and 100% of diff-size COs land on the 12 FLEXIBLE machines. So
# when ON we, for FLEXIBLE machines ONLY, (1) detect diff-INCH changes across consecutive
# in-window shifts (+ the t=0 seam vs init_mach_sku), (2) subtract the diff-CO minutes from
# that machine's build capacity in the CO shift, and (3) penalize ONLY diff COs. Same-size
# COs are left free & unmodelled (the writer derives them post-hoc). OFF ("0") reproduces
# the prior model BIT-FOR-BIT (every new var/constraint/term is guarded by this toggle).
BLD_CO          = os.environ.get("OPT_BLD_CO", "1") == "1"
# PER-GROUP diff-inch-CO penalty (cured-units per diff CO). Plant gradient: Stage2 = 0
# (GT bottleneck, unlimited inch changes), VMI light, BJ / UNISTAGE-narrow / Stage1 heavy.
# Detection + capacity charge apply to EVERY >1-inch machine regardless of weight; only
# the objective penalty is per-group (weight 0 = capacity-charged but not penalized).
PEN_DIFF_BY_GROUP = {
    "S2":  int(os.environ.get("OPT_PEN_DIFF_S2",  "0")),    # no limit
    "VMI": int(os.environ.get("OPT_PEN_DIFF_VMI", "4")),    # light
    "BJ":  int(os.environ.get("OPT_PEN_DIFF_BJ",  "20")),   # heavy / very few
    "UNI": int(os.environ.get("OPT_PEN_DIFF_UNI", "20")),   # heavy / very few
    "S1":  int(os.environ.get("OPT_PEN_DIFF_S1",  "20")),   # heavy / very few
}
# PER-MACHINE MONTHLY diff-CO budget (HARD cap). When a diff_budget dict is passed to
# build_and_solve, each machine's in-window diff COs are capped at its remaining monthly
# allowance (the driver carries the balance across rolling windows). This is the PRIMARY
# control for BJ/UNI/S1/VMI (Stage2 stays uncapped + penalty-0, shaped via penalty only).
# OPT_DIFF_BUDGET=0 ignores diff_budget even if passed (for A/B); default ON.
DIFF_BUDGET_ON   = os.environ.get("OPT_DIFF_BUDGET", "1") == "1"
# Optional soft min-dwell (SEPARATE toggle, default OFF): discourage a machine changing
# SKU two consecutive shifts. Prefer capacity+penalty alone; enable only if COs stay high.
BLD_MINDWELL     = os.environ.get("OPT_BLD_MINDWELL", "0") == "1"
BLD_MINDWELL_PEN = int(os.environ.get("OPT_BLD_MINDWELL_PEN", "4"))  # cured-units / back-to-back CO

# ------------------------------------------------------------------------------
# INCH-LOCK RELAXATION (v2, toggle-gated). Default OFF = v1 hard-lock bit-for-bit.
# When ON, a building machine keeps its HISTORICAL inches (free/preferred) AND may
# ADDITIONALLY reach the SCARCE high-demand inches (_RELAX_INCHES = 13"/15", where the
# ceiling headroom lives) among its DB-allowable SKUs — NOT every inch (blanket relax
# was intractable, -11%). A diff-INCH changeover is (a) COUNTED per machine, (b) CAPPED
# by a per-window budget from a per-MONTH group budget, (c) charged a small penalty, so
# the optimizer only spends spare/freed capacity on 13"/15" when it beats staying put.
INCH_RELAX  = os.environ.get("INCH_RELAX", "0") == "1"
_RELAX_INCHES = set(os.environ.get("RELAX_INCHES", "13,15").split(","))   # scarce targets
INCH_PEN   = int(os.environ.get("OPT_INCH_PEN", "8"))    # cured-units charged per diff-inch CO
# Per-MONTH diff-inch-CO budget per machine group (owner's rule):
#   S1  = 2  (up to 2 diff-inch COs/machine/month, reverts allowed) ;
#   UNI = 2  (VMI 6001-04/7001-04 + BJ 7101-06/7201 + UNI-narrow 7501-03 -> "very few") ;
#   S2  = 12 ("no practical limit on diff-inch COs").
_INCH_MONTHLY_BUDGET = {"S1": 2, "UNI": 2, "S2": 12}

# ------------------------------------------------------------------------------
# DEMAND-PRIORITY (FIX 3, toggle-gated). Default OFF = flat max-cured bit-for-bit.
# The flat objective weights every cured unit equally, so it fills easy small SKUs
# and leaves big CAPACITY-FEASIBLE SKUs (e.g. 1225170015012LSTL0) half-served even
# with idle presses/machines. When ON we add a MILD completion gradient PLUS SOFT
# pro-rata floors on the top-N big capacity-feasible SKUs:
#   (a) WEIGHT cured[s,t] by w[s] = SCALE*(1 + alpha*demand[s]/max_demand), a gentle
#       1.0..~1.2 gradient (SCALE keeps it integer). Small SKUs still get served by
#       spare capacity — the gradient only breaks ties toward finishing big SKUs.
#   (b) SOFT FLOOR: for the top-N highest-demand CAPACITY-FEASIBLE SKUs,
#       sum_committed cured[s] + slack[s] >= floor_target[s], slack>=0 penalized
#       (NEVER hard -> never infeasible). floor_target is a PRO-RATA per-window share
#       (remaining_demand * commit_days / days_left) so big SKUs are PACED across
#       windows, not all deferred. CAPACITY-FEASIBLE is checked per SKU (cure ceiling
#       AND build ceiling over the committed horizon both >= floor); genuinely scarce
#       SKUs are skipped (flooring them only displaces others).
PRIORITY        = os.environ.get("PRIORITY", "0") == "1"
# COEFFICIENT DISCIPLINE (the tuning lesson): the priority objective keeps its DOMINANT
# term `total_cured` at the SAME coefficient as the flat objective (PRIO_SCALE=1) and adds
# only SMALL terms — an additive completion-gradient bonus (0..PRIO_ALPHA per cured unit on
# the biggest SKUs) + penalized soft floors. Do NOT reintroduce a large global multiplier:
# big objective coefficients slow CP-SAT and shrink the incumbent at equal wall time
# (measured on a July 13-day window: the old SCALE=10 path lost ~-11k committed cured vs
# the flat baseline purely from worse convergence, even gradient-only with no floors). Keep
# PRIO_SCALE at 1 unless you also raise the time budget.
PRIO_SCALE      = int(os.environ.get("PRIO_SCALE", "1"))      # base weight on cured — KEEP 1 (>1 inflates coeffs)
PRIO_ALPHA      = float(os.environ.get("PRIO_ALPHA", "2.0"))  # gradient: max EXTRA integer weight on the biggest SKU
PRIO_TOPN       = int(os.environ.get("PRIO_TOPN", "10"))      # #big capacity-feasible SKUs floored
PRIO_FLOOR_FRAC = float(os.environ.get("PRIO_FLOOR_FRAC", "0.4"))  # fraction of the pro-rata per-window share
PRIO_SLACK_PEN  = int(os.environ.get("PRIO_SLACK_PEN", "2"))  # points charged per unmet floor unit (vs PRIO_SCALE per cured unit)

# ------------------------------------------------------------------------------
# HARD-RULE FIXES (validator-flagged gaps between plan and plant HARD rules). Each is a
# SEPARATE env toggle, DEFAULT OFF -> OFF reproduces the prior model BIT-FOR-BIT. Every
# new var/constraint/objective term below is guarded by its toggle. All independently
# combinable with each other and with the existing toggles.
#
# FIX 4 (OPT_BUILT_CAP, rule 8): total GT BUILT for a SKU <= its demand (built + init_gt
#   carried-in <= demand). Building today overbuilds ~12 SKUs -> stranded GT. Hard linear.
BUILT_CAP   = os.environ.get("OPT_BUILT_CAP", "0") == "1"
# FIX 2 (OPT_GT_AGE, rule 9-GT): GT cannot sit > 3 days (=9 shifts) before curing. The
#   aggregate reservoir lets GT persist forever; bound on-hand inv to GT built in the last
#   9 shifts (+ opening only through the first 9 shifts). A penalized writeoff var absorbs
#   GT that is physically un-cureable in time (no press / no demand) so the bound never
#   makes the model infeasible.
GT_AGE      = os.environ.get("OPT_GT_AGE", "0") == "1"
GT_AGE_WOFF_PEN = int(os.environ.get("OPT_GT_AGE_WOFF_PEN", "3"))   # cured-units charged per GT unit written off
# FIX 3 (OPT_CARC_AGE, rule 9-carcass): Stage-1 carcass cannot sit > 1 day (=3 shifts).
#   Same bound over the carcass reservoir; penalized writeoff for un-consumable opening carcass.
CARC_AGE    = os.environ.get("OPT_CARC_AGE", "0") == "1"
CARC_AGE_WOFF_PEN = int(os.environ.get("OPT_CARC_AGE_WOFF_PEN", "3"))  # charged per carcass unit written off
# FIX 1 (OPT_BLD_SAME_CO, rules 15/7): charge the SAME-size building CO's capacity. Today
#   only DIFF-inch building COs are charged capacity; a SAME-inch SKU change is FREE, so a
#   machine's Prod_Mins + CO_Mins can exceed 480. Detect a same-size CO (active both shifts,
#   inch unchanged, SKU changed) via per-SKU + per-inch continuation binaries and subtract
#   co_same[m] minutes from that shift's build capacity. Same-size COs are NOT penalized in
#   the objective (the plant wants them plentiful) — only their CAPACITY is charged.
BLD_SAME_CO = os.environ.get("OPT_BLD_SAME_CO", "0") == "1"

# ------------------------------------------------------------------------------
# PRODUCTION SMOOTHING + CARCASS/GT JIT PACING (anti front-loading). Each is a SEPARATE env
# toggle, DEFAULT OFF -> OFF reproduces the prior model BIT-FOR-BIT (every new var/constraint/
# objective term is guarded by its toggle). Convergence-safe by design: features 1+2 are CAPS
# (constraints), NOT big penalties; total_cured keeps coefficient 1; any slack penalty <= 2.
#
# SMOOTH 1 (OPT_DAY_BUILD_CAP, PRIMARY smoother): a HARD per-committed-day cap on TOTAL GT built
#   (all GT machines, all SKUs, the day's 3 shifts) <= DAY_BUILD_CAP. Trims extreme early
#   over-building (the source of the high built-CV) without touching the normal ~25k/day rate.
#   Stage-1 carcass (gc) is EXCLUDED (this caps GT only). The env value DOUBLES as the cap:
#     "0"/absent -> OFF ; "1" -> ON with a GENEROUS default cap
#       = ceil(OPT_DAY_BUILD_CAP_MULT * sum(mi.demand)/planning_days) (mult default 1.15) ;
#     any int >1 -> ON with that value as the explicit units/day cap.
#   1 constraint/committed day (cheap; no objective coeff -> zero convergence risk). NOTE: the
#   "1" default derives the cap from mi.demand, which the rolling driver SHRINKS per window
#   (win_mi.demand = remaining) while planning_days stays the month -> late windows get a
#   tighter default. For a full-month run pass an EXPLICIT units/day cap (e.g. 28900). For a
#   single/first window (incl. the smoke test) the default equals the true monthly rate.
_DAY_BUILD_CAP_ENV = os.environ.get("OPT_DAY_BUILD_CAP", "0").strip()
DAY_BUILD_CAP_ON   = _DAY_BUILD_CAP_ENV not in ("0", "")
_DAY_BUILD_CAP_RAW = int(_DAY_BUILD_CAP_ENV) if _DAY_BUILD_CAP_ENV.lstrip("-").isdigit() else 1
DAY_BUILD_CAP_MULT = float(os.environ.get("OPT_DAY_BUILD_CAP_MULT", "1.15"))  # default-cap headroom

# SMOOTH 2 (OPT_CARC_JIT): carcass just-in-time pacing (fixes rule 9C aging + rule 5 at source).
#   Per S2 SKU, CUMULATIVE Stage-1 carcass BUILT through committed day d <= CUMULATIVE Stage-2
#   GT BUILT through day d+1 -> carcass stays at most ~1 day ahead of its Stage-2 consumption
#   and never ages >1 day. Scoped to S2/carcass SKUs (few). A tiny PENALIZED slack keeps it from
#   ever making the model infeasible (penalty weight <= 2, convergence-safe).
CARC_JIT     = os.environ.get("OPT_CARC_JIT", "0") == "1"
CARC_JIT_PEN = min(2, int(os.environ.get("OPT_CARC_JIT_PEN", "2")))   # cured-units / slack unit (<=2)

# SMOOTH 3 (OPT_GT_JIT, default OFF, TESTING lever only): per-SKU GT JIT pacing. CUMULATIVE GT
#   built through committed day d <= CUMULATIVE cured through day d+3 (3-day GT shelf) + tiny
#   penalized slack. Stronger / closer to the hard shelf that hurt CP-SAT convergence -> keep
#   OFF for production; enable only for A/B. Penalty weight <= 2.
GT_JIT     = os.environ.get("OPT_GT_JIT", "0") == "1"
GT_JIT_PEN = min(2, int(os.environ.get("OPT_GT_JIT_PEN", "2")))       # cured-units / slack unit (<=2)


def build_and_solve(mi: ModelInputs, day_start: int, n_days: int,
                    init_gt: dict | None = None,
                    init_press_sku: dict | None = None,
                    init_n: dict | None = None,
                    init_carc: dict | None = None,
                    det_time_s: float = 60.0, seed: int = 1, log: bool = False,
                    probe_feasible: bool = False, workers: int = 1,
                    hint: dict | None = None, commit_days: int | None = None,
                    days_left: int | None = None,
                    init_mach_sku: dict | None = None,
                    diff_budget: dict | None = None) -> dict:
    """Solve one window [day_start, day_start+n_days). Returns a solution dict."""
    W = n_days * 3
    T = range(W)
    init_gt        = dict(init_gt if init_gt is not None else mi.opening_gt)
    init_press_sku = dict(init_press_sku if init_press_sku is not None else mi.press_init_sku)
    _init_carc     = dict(init_carc if init_carc is not None else mi.opening_carcass)
    # Building setup carry across window seams: machine -> SKU it was building on the
    # previous window's last committed shift (GT + Stage-1). None/absent -> no t=0 CO
    # (mirrors curing's init_press_sku). Default {} keeps OFF + None paths bit-for-bit.
    _init_mach_sku = dict(init_mach_sku) if init_mach_sku else {}
    m = cp_model.CpModel()

    # ================= BUILDING (per-machine, inch-locked) =================
    bld_pairs = []
    for mac in mi.machines:
        if mi.machine_group[mac] not in GT_GROUPS:      # Stage-1 = carcass only (v2)
            continue
        inns = mi.machine_allowed_inches.get(mac, set())
        for s in mi.machine_allowed_skus.get(mac, ()):
            if mi.build_rate.get((mac, s), 0) <= 0:
                continue
            _inch = mi.sku_inch.get(s, "")
            if inns and _inch not in inns and not (INCH_RELAX and _inch in _RELAX_INCHES):
                continue     # keep historical inches always; +13"/15" only when INCH_RELAX
            bld_pairs.append((mac, s))
    bld_pairs.sort()
    gt_machines = sorted({mac for mac, _ in bld_pairs})
    skus_built  = {s for _, s in bld_pairs}

    x, g = {}, {}
    for (mac, s) in bld_pairs:
        for t in T:
            x[mac, s, t] = m.NewBoolVar(f"x_{mac}_{s}_{t}")
            g[mac, s, t] = m.NewIntVar(0, mi.build_rate[(mac, s)], f"g_{mac}_{s}_{t}")
            m.Add(g[mac, s, t] <= mi.build_rate[(mac, s)] * x[mac, s, t])
            if MIN_CAMP:                                            # build >= floor or 0 (no slivers)
                _L = min(MIN_CAMP_UNITS, int(mi.demand.get(s, 0)))
                if _L > 0:
                    m.Add(g[mac, s, t] >= _L * x[mac, s, t])
    for mac in gt_machines:
        macs_skus = [s for (mm, s) in bld_pairs if mm == mac]
        for t in T:
            m.Add(sum(x[mac, s, t] for s in macs_skus) <= 1)

    # ---- Stage-1 CARCASS gating: Stage-2 GT needs Stage-1 carcass ----
    s1_pairs = []
    for mac in mi.machines:
        if mi.machine_group[mac] != "S1":
            continue
        inns = mi.machine_allowed_inches.get(mac, set())
        for s in mi.machine_allowed_skus.get(mac, ()):
            if mi.build_rate.get((mac, s), 0) <= 0:
                continue
            _inch = mi.sku_inch.get(s, "")
            if inns and _inch not in inns and not (INCH_RELAX and _inch in _RELAX_INCHES):
                continue     # keep historical inches always; +13"/15" only when INCH_RELAX
            s1_pairs.append((mac, s))
    s1_pairs.sort()
    s1_machines = sorted({mac for mac, _ in s1_pairs})
    s2_skus = sorted({s for (mac, s) in bld_pairs if mi.machine_group[mac] == "S2"})

    xc, gc = {}, {}
    for (mac, s) in s1_pairs:
        for t in T:
            xc[mac, s, t] = m.NewBoolVar(f"xc_{mac}_{s}_{t}")
            gc[mac, s, t] = m.NewIntVar(0, mi.build_rate[(mac, s)], f"gc_{mac}_{s}_{t}")
            m.Add(gc[mac, s, t] <= mi.build_rate[(mac, s)] * xc[mac, s, t])
    for mac in s1_machines:
        macs = [s for (mm, s) in s1_pairs if mm == mac]
        for t in T:
            m.Add(sum(xc[mac, s, t] for s in macs) <= 1)

    # ---- INCH-LOCK RELAXATION: diff-inch-CO tracking + per-window budget (v2) ----
    # A machine builds one SKU/shift; its inch = that SKU's inch. Track a committed-inch
    # STATE a[mac,i,t] (one-hot, persists through idle shifts, tied to production) and
    # charge dci[mac,t]=1 whenever that committed inch CHANGES across shifts (a diff-size
    # building CO; reverts count: 14->13->14 = 2 COs). Per-machine sum(dci) <= a per-window
    # budget = ceil(monthly_budget[group] * commit_days / planning_days). Historical inch =
    # free default: starting the window on a non-historical inch also costs one dci.
    diff_co_vars = []
    dci_by_mt = {}      # (machine, shift) -> dci var, for committed-day diff-CO accounting
    if INCH_RELAX:
        import math
        _planning_days = int(getattr(mi, "planning_days", n_days)) or n_days
        _cd = commit_days if commit_days is not None else n_days

        def _track_inch(mac, pairs, xvars):
            skus_m = sorted({s for (mm, s) in pairs if mm == mac})
            inch_skus: dict = {}
            for s in skus_m:
                inch_skus.setdefault(mi.sku_inch.get(s, ""), []).append(s)
            inches = sorted(inch_skus)
            if len(inches) <= 1:                # only one inch reachable -> no diff-inch CO possible
                return
            hist = mi.machine_allowed_inches.get(mac, set())
            grp  = mi.machine_group[mac]
            cap  = math.ceil(_INCH_MONTHLY_BUDGET.get(grp, 2) * _cd / _planning_days)
            a = {}
            for i in inches:
                for t in T:
                    a[i, t] = m.NewBoolVar(f"ainch_{mac}_{i}_{t}")
            for t in T:
                m.Add(sum(a[i, t] for i in inches) == 1)           # exactly one committed inch/shift
                for i in inches:                                   # producing inch i => committed to i
                    m.Add(sum(xvars[mac, s, t] for s in inch_skus[i]) <= a[i, t])
            mac_dci = []
            for t in T:
                dci = m.NewBoolVar(f"dci_{mac}_{t}")
                if t == 0:
                    for i in inches:
                        if i not in hist:                          # start off the historical default = 1 CO
                            m.Add(dci >= a[i, 0])
                else:
                    for i in inches:
                        m.Add(dci >= a[i, t] - a[i, t - 1])        # committed inch changed = 1 CO
                mac_dci.append(dci)
                diff_co_vars.append(dci)
                dci_by_mt[(mac, t)] = dci
            m.Add(sum(mac_dci) <= cap)                             # per-window diff-inch-CO budget

        for mac in gt_machines:
            _track_inch(mac, bld_pairs, x)
        for mac in s1_machines:
            _track_inch(mac, s1_pairs, xc)

    # ---- BUILDING DIFF-INCH-CHANGEOVER pricing (OPT_BLD_CO) — REDESIGNED ----
    # The plant WANTS same-size building COs HIGH (cheap 20-60 min recipe swaps, target
    # 2,000-2,900/month) and only DIFF-size (inch) COs LOW (target 80-120). Diff-inch COs
    # are physically possible on ANY machine whose allowed-inch set spans >1 inch (fixed
    # single-inch machines can't change inch, by the inch-lock). So this model:
    #   (1) creates CO vars on EVERY >1-inch machine, across ALL groups (S1/S2/VMI/BJ/UNI);
    #       fixed single-inch machines get NONE. (Charging same-size CO on all 38 machines
    #       needed per-SKU continuation binaries that wrecked the CP-SAT gap: -90k KPI at
    #       zero penalty — the binary count here is tiny, only >1-inch machines.)
    #   (2) detects diff-INCH changes via a PERSISTENT per-machine INCH-STATE (setup[m,i,t])
    #       that survives IDLE shifts — a diff CO is charged whenever the machine produces an
    #       inch different from the LAST inch it produced, even across idle gaps. This matches
    #       the writer's physical definition (inch change between consecutive PRODUCING shifts);
    #       the old `both`-gated rule UNDERCOUNTED (a machine could idle one shift to dodge the
    #       CO + the cap). No per-SKU vars, no same-size CO vars (same COs stay free/unlimited).
    #   (3) charges ONLY the diff-CO minutes (BUILDING_CO_DIFF_SIZE) against that machine's
    #       build capacity in the CO shift (UNCONDITIONAL — even Stage2), penalizes diff COs
    #       PER GROUP (Stage2=0 unlimited, VMI light, BJ/UNI/S1 heavy), and each machine's
    #       in-window diff COs are hard-capped by diff_budget[m] (the per-machine monthly cap).
    # A diff CO fires at shift t when the set-up inch CHANGES (machine produces a new inch after
    # its last-produced inch, across any idle gap; at t=0 vs the carried init_mach_sku prior
    # inch). The machine's FIRST production in-window (no prior setup) is NOT a CO.
    # OFF (BLD_CO=False) creates none of this (bit-for-bit prior).
    diff_co_vars_bld = []
    bld_rank_terms = []                                         # INCH_FLEX: graded off-dominant penalty terms
    bld_co_min = {}          # (machine, t) -> co_min IntVar (minutes lost to a diff building CO)
    diff_co_by_mt = {}
    if BLD_CO:
        def _track_bld_co(mac, pairs, xvars, gvars):
            # FLEXIBLE machines only: the historical allowed-inch set must span >1 inch.
            if len(mi.machine_allowed_inches.get(mac, set())) <= 1:
                return
            skus_m = sorted({s for (mm, s) in pairs if mm == mac})
            if len(skus_m) <= 1:
                return
            inch_skus: dict = {}
            for s in skus_m:
                inch_skus.setdefault(mi.sku_inch.get(s, ""), []).append(s)
            inches = sorted(inch_skus)
            if len(inches) <= 1:            # only one inch reachable in-window -> no diff CO
                return
            grp_diff = int(mi.co_diff.get(mac, 180))
            # Link x == (producing): the base model allows x=1 with g=0 (a phantom assignment
            # the solver can flip for free), which would fabricate setup changes / diff COs the
            # writer never sees (it counts inch changes between g>0 shifts). Forcing x<=g (with
            # the existing g<=rate*x) pins x=1 <=> g>=1, so inch-state tracks REAL production.
            # Scoped to this flexible machine only + BLD_CO only -> OFF/fixed paths untouched.
            for s in skus_m:
                for t in T:
                    m.Add(xvars[mac, s, t] <= gvars[mac, s, t])
            def _active(t):   return sum(xvars[mac, s, t] for s in skus_m)          # 0/1 (one-hot)
            def _ainch(i, t): return sum(xvars[mac, s, t] for s in inch_skus[i])    # 0/1
            # prior-inch carry into t=0 (building setup seam). Absent -> no t=0 CO.
            prior_sku  = _init_mach_sku.get(mac)
            prior_inch = mi.sku_inch.get(prior_sku, "") if prior_sku is not None else None
            # persistent inch-STATE: setup[i,t] = machine set up for inch i at shift t.
            setup = {}
            for i in inches:
                for t in T:
                    setup[i, t] = m.NewBoolVar(f"bco_setup_{mac}_{i}_{t}")
            if INCH_FLEX:                                        # prefer dominant; graded penalty off it
                _rk = mi.machine_inch_rank.get(mac, {})
                for i in inches:
                    _r = _rk.get(i, 0)
                    if _r > 0:
                        for t in T:
                            bld_rank_terms.append(INCH_RANK_STEP * _r * setup[i, t])
            if INCH_FLEX and INCH_DWELL > 1:                     # min-inch-dwell: hold an inch >=DWELL shifts once switched (anti-thrash)
                for i in inches:
                    for t in T:
                        _prev_i = (1 if (prior_inch is not None and prior_inch == i) else 0) if t == 0 else setup[i, t - 1]
                        for _d in range(1, INCH_DWELL):
                            if t + _d < W:                       # if i turns ON at t, it stays ON through t+DWELL-1
                                m.Add(setup[i, t + _d] >= setup[i, t] - _prev_i)
            for t in T:
                # prev[i] = set-up inch at shift t-1 (for t=0: the carried seam prior, or all-0).
                if t == 0:
                    prev = {i: (1 if (prior_inch is not None and i == prior_inch) else 0) for i in inches}
                else:
                    prev = {i: setup[i, t - 1] for i in inches}
                prev_any = sum(prev[i] for i in inches)     # 0 before first setup, else 1
                for i in inches:
                    ai = _ainch(i, t)
                    m.Add(setup[i, t] >= ai)                            # produce i => set up for i
                    # setup may only turn ON i while producing i; may only turn OFF i while
                    # producing a DIFFERENT inch. When idle (active=0) both force setup==prev
                    # (state persists through the idle gap).
                    m.Add(setup[i, t] - prev[i] <= ai)
                    m.Add(prev[i] - setup[i, t] <= _active(t) - ai)
                m.Add(sum(setup[i, t] for i in inches) <= 1)            # one-hot (0 before start)
                # diff CO at t: the set-up inch changed AND a prior inch existed (skip 1st start).
                if t == 0 and prior_sku is None:
                    continue            # no carried setup -> first production is not a CO
                dco = m.NewIntVar(0, 1, f"bco_diff_{mac}_{t}")
                for i in inches:
                    # dco >= (i turned ON) - (no prior setup existed): free 1st start, else charged
                    m.Add(dco >= setup[i, t] - prev[i] - (1 - prev_any))
                cmin = m.NewIntVar(0, grp_diff, f"bco_cmin_{mac}_{t}")
                m.Add(cmin == grp_diff * dco)
                diff_co_vars_bld.append(dco)
                diff_co_by_mt[(mac, t)] = dco
                bld_co_min[(mac, t)] = cmin
                # capacity: the diff-CO shift loses grp_diff minutes of production on the
                # active SKU. SHIFT_MINS*g <= rate*(SHIFT_MINS - cmin), big-M slack for x=0.
                for s in skus_m:
                    br = mi.build_rate[(mac, s)]
                    m.Add(SHIFT_MINS * gvars[mac, s, t]
                          <= br * (SHIFT_MINS - cmin)
                          + br * SHIFT_MINS * (1 - xvars[mac, s, t]))
                if BLD_MINDWELL and t >= 2:   # optional soft min-dwell: back-to-back diff COs
                    prev_dco = diff_co_by_mt.get((mac, t - 1))
                    if prev_dco is not None:
                        b2b = m.NewIntVar(0, 1, f"bco_b2b_{mac}_{t}")
                        m.Add(b2b >= dco + prev_dco - 1)
                        _bld_b2b_vars.append(b2b)

        _bld_b2b_vars: list = []
        for mac in gt_machines:
            _track_bld_co(mac, bld_pairs, x, g)
        for mac in s1_machines:
            _track_bld_co(mac, s1_pairs, xc, gc)

        # PER-MACHINE MONTHLY diff-CO budget (HARD cap, primary control). Cap each machine's
        # in-window diff COs (INCLUDING the t=0 seam var) at its remaining monthly allowance.
        # Machines absent from diff_budget are UNCAPPED. Off unless OPT_DIFF_BUDGET + a dict.
        if DIFF_BUDGET_ON and diff_budget:
            _dco_by_m: dict = {}
            for (mac, t), dco in diff_co_by_mt.items():
                _dco_by_m.setdefault(mac, []).append(dco)
            for mac, dcos in _dco_by_m.items():
                cap = diff_budget.get(mac)
                if cap is not None:
                    m.Add(sum(dcos) <= max(0, int(cap)))

    # ---- FIX 1: SAME-SIZE building-CO capacity charge (OPT_BLD_SAME_CO) ----
    # Today only DIFF-inch building COs cost capacity (BLD_CO, above); a machine switching
    # to a DIFFERENT SKU of the SAME inch between shifts is FREE, so Prod_Mins + CO_Mins can
    # exceed 480. Here we charge that same-size CO's minutes against the machine's build
    # capacity in the CO shift — WITHOUT penalizing it in the objective (the plant wants
    # same-size COs plentiful). Applies to EVERY machine (GT + Stage-1) with >1 eligible SKU
    # (a single-eligible-SKU machine can never change SKU -> no same CO). Detection uses
    # per-SKU + per-inch CONTINUATION binaries between consecutive PRODUCING shifts:
    #   cont_sku[m,s,t] = 1  iff m built s in BOTH t-1 and t   (SKU carried)
    #   cont_inch[m,i,t]= 1  iff m built inch i in BOTH t-1 and t (inch carried)
    #   same_co[m,t] = (Σ_i cont_inch) - (Σ_s cont_sku)  ∈ {0,1}
    #      = 1 exactly when the machine was active both shifts, inch UNCHANGED, SKU CHANGED
    #        (SKU continued -> both sums 1 -> 0 ; inch changed -> both sums 0 -> 0 ; idle -> 0).
    # This is DISJOINT from BLD_CO's diff CO in the same shift (a single SKU change is either
    # same-inch or diff-inch, never both), so the two capacity constraints compose as a min
    # per shift — no double-charge, and FIX 1 needs no change to the BLD_CO block. To keep x
    # meaning "producing" (the base model allows x=1 with g=0, a phantom the solver could flip
    # to fabricate/hide COs), we pin x <= g on these machines/shifts (with g <= rate*x already,
    # x=1 <=> g>=1). Continuation vars are created ONLY for buildable (m,s) pairs and only for
    # t>=1 (+ the t=0 seam vs init_mach_sku), keeping the binary count tight.
    same_co_by_mt = {}
    if BLD_SAME_CO:
        def _track_same_co(mac, pairs, xvars, gvars):
            skus_m = sorted({s for (mm, s) in pairs if mm == mac})
            if len(skus_m) <= 1:                 # cannot change SKU -> no same-size CO possible
                return
            inch_skus: dict = {}
            for s in skus_m:
                inch_skus.setdefault(mi.sku_inch.get(s, ""), []).append(s)
            inches = sorted(inch_skus)
            co_same_m = int(mi.co_same.get(mac, 60))
            # pin x <=> producing (see note above) so continuation tracks REAL production.
            for s in skus_m:
                for t in T:
                    m.Add(xvars[mac, s, t] <= gvars[mac, s, t])
            def _ainch(i, t): return sum(xvars[mac, s, t] for s in inch_skus[i])   # 0/1
            prior_sku  = _init_mach_sku.get(mac)
            prior_inch = mi.sku_inch.get(prior_sku, "") if prior_sku is not None else None
            for t in T:
                if t == 0 and prior_sku is None:
                    continue                     # no carried SKU -> first production is not a CO
                # Σ cont_sku: 1 iff the SKU built at t equals the SKU built at t-1 (both active).
                cont_sku_terms = []
                for s in skus_m:
                    cs = m.NewBoolVar(f"csku_{mac}_{s}_{t}")
                    if t == 0:
                        m.Add(cs <= xvars[mac, s, 0])
                        prev_x = 1 if s == prior_sku else 0
                        m.Add(cs <= prev_x)
                        m.Add(cs >= xvars[mac, s, 0] + prev_x - 1)
                    else:
                        m.Add(cs <= xvars[mac, s, t])
                        m.Add(cs <= xvars[mac, s, t - 1])
                        m.Add(cs >= xvars[mac, s, t] + xvars[mac, s, t - 1] - 1)
                    cont_sku_terms.append(cs)
                # Σ cont_inch: 1 iff the inch built at t equals the inch built at t-1 (both active).
                cont_inch_terms = []
                for i in inches:
                    ci = m.NewBoolVar(f"cinch_{mac}_{i}_{t}")
                    ai_t = _ainch(i, t)
                    if t == 0:
                        prev_ai = 1 if i == prior_inch else 0
                        m.Add(ci <= ai_t)
                        m.Add(ci <= prev_ai)
                        m.Add(ci >= ai_t + prev_ai - 1)
                    else:
                        ai_p = _ainch(i, t - 1)
                        m.Add(ci <= ai_t)
                        m.Add(ci <= ai_p)
                        m.Add(ci >= ai_t + ai_p - 1)
                    cont_inch_terms.append(ci)
                same_co = m.NewIntVar(0, 1, f"same_co_{mac}_{t}")
                m.Add(same_co == sum(cont_inch_terms) - sum(cont_sku_terms))
                same_co_by_mt[(mac, t)] = same_co
                # capacity: the same-size CO shift loses co_same_m minutes on the active SKU.
                # SHIFT_MINS*g <= rate*(SHIFT_MINS - co_same*same_co), big-M slack for x=0.
                for s in skus_m:
                    br = mi.build_rate[(mac, s)]
                    m.Add(SHIFT_MINS * gvars[mac, s, t]
                          <= br * (SHIFT_MINS - co_same_m * same_co)
                          + br * SHIFT_MINS * (1 - xvars[mac, s, t]))

        for mac in gt_machines:
            _track_same_co(mac, bld_pairs, x, g)
        for mac in s1_machines:
            _track_same_co(mac, s1_pairs, xc, gc)

    # ---- FIX 4: BUILT <= DEMAND cap (OPT_BUILT_CAP) ----
    # Total GT BUILT for a SKU across this window (all GT machines, all shifts) plus the GT
    # already built & carried in (init_gt) must not exceed the SKU's demand -> no stranded
    # overbuild. Stage-1 carcass (gc) is NOT GT and is excluded (built[] sums only GT g).
    # Enforced on built[] below (defined in the GT reservoir), so it is added there.

    # SMOOTHING slack/aux (empty / None unless the smoothing toggles are ON -> bit-for-bit).
    carc_jit_slack: list = []
    gt_jit_slack: list = []
    day_build_cap_value = None

    # carcass reservoir per S2 SKU: carc_inv >= 0 enforces "S2 GT <= carcass available".
    # c_built_at[(s,t)] = Stage-1 carcass produced for SKU s at shift t (for FIX 3 aging).
    carc_inv = {}
    c_built_at = {}
    carc_woff_vars = []          # FIX 3 carcass-aging writeoff vars (empty unless CARC_AGE)
    for s in s2_skus:
        _rate = sum(mi.build_rate[(m1, ss)] for (m1, ss) in s1_pairs if ss == s)
        c_ub = max(int(_init_carc.get(s, 0)), _rate * 4) + 1
        cw = {}                                                              # FIX 3 carcass writeoff
        for t in T:
            carc_inv[s, t] = m.NewIntVar(0, c_ub, f"carc_{s}_{t}")
            c_built = sum(gc[m1, s, t] for (m1, ss) in s1_pairs if ss == s)   # 0 if no S1 supply
            c_built_at[s, t] = c_built
            c_used  = sum(g[m2, s, t] for (m2, ss) in bld_pairs
                          if ss == s and mi.machine_group[m2] == "S2")
            prev = carc_inv[s, t - 1] if t > 0 else int(_init_carc.get(s, 0))
            if CARC_AGE:
                # penalized writeoff absorbs opening/aged carcass no Stage-2 can consume in
                # time, so the 3-shift aging bound below never makes the model infeasible.
                cw[s, t] = m.NewIntVar(0, c_ub, f"carcwoff_{s}_{t}")
                m.Add(carc_inv[s, t] == prev + c_built - c_used - cw[s, t])
                carc_woff_vars.append(cw[s, t])
            else:
                m.Add(carc_inv[s, t] == prev + c_built - c_used)             # carc_inv>=0 gates S2 GT
        if CARC_AGE:
            # FIX 3: carcass cannot sit > 1 day (=3 shifts). On-hand carcass at shift t may
            # only be carcass produced in the last 3 shifts (opening carcass counts fresh
            # only through the first 3 shifts of the window -> forced consumed/written off).
            for t in T:
                recent = sum(c_built_at[s, tau] for tau in range(max(0, t - 2), t + 1))
                opening = int(_init_carc.get(s, 0)) if t < 3 else 0
                m.Add(carc_inv[s, t] <= recent + opening)

    for t in T:                                                     # per-SHIFT total carcass cap (hard plant limit)
        m.Add(sum(carc_inv[s, t] for s in s2_skus) <= CARC_CAP)

    # ---- SMOOTH 2: CARCASS just-in-time pacing (OPT_CARC_JIT) ----
    # Per S2 SKU, for each committed day d: cumulative Stage-1 carcass BUILT through day d must
    # not exceed cumulative Stage-2 GT BUILT through day d+1 (carcass at most ~1 day ahead of
    # its consumption -> never ages >1 day; Stage-1 stops front-loading carcass). A tiny
    # penalized slack (weight <= 2) keeps the constraint from ever forcing infeasibility.
    if CARC_JIT:
        _cdays_cj = commit_days if commit_days is not None else n_days
        _s2_mac_for: dict = {}
        for (_m2, _ss) in bld_pairs:
            if mi.machine_group[_m2] == "S2":
                _s2_mac_for.setdefault(_ss, []).append(_m2)
        for s in s2_skus:
            _s2macs = _s2_mac_for.get(s, ())
            for d in range(_cdays_cj):
                carc_through_d = sum(c_built_at[s, tau] for tau in range((d + 1) * 3))
                s2_hi = min(W, (d + 2) * 3)
                s2gt_through_d1 = sum(g[m2, s, t] for m2 in _s2macs for t in range(s2_hi))
                sl = m.NewIntVar(0, 10 ** 7, f"carcjit_slack_{s}_{d}")
                m.Add(carc_through_d <= s2gt_through_d1 + sl)
                carc_jit_slack.append(sl)

    # ================= CURING (per-SKU press counts) =======================
    n_press = len(mi.presses)
    elig_cnt = {s: sum(1 for p in mi.presses if s in mi.press_allowed_skus.get(p, ()))
                for s in mi.skus}
    if init_n is None:                                     # driver may pass carry-over counts directly
        init_n = {s: sum(1 for p in mi.presses if init_press_sku.get(p) == s) for s in mi.skus}
    cure_skus = [s for s in mi.skus
                 if mi.mould_pairs.get(s, 0) >= 1 and mi.cure_rate.get(s, 0) > 0 and elig_cnt[s] > 0]

    n, co, prod = {}, {}, {}
    _free = {}                                                     # CO-free Day-1 starts (ABSENT presses, no moulds)
    _n_free = int(getattr(mi, "n_free_start", 0))
    for s in cure_skus:
        cap_s = min(mi.mould_pairs[s], elig_cnt[s])
        for t in T:
            n[s, t]  = m.NewIntVar(0, cap_s, f"n_{s}_{t}")          # presses on s
            co[s, t] = m.NewIntVar(0, cap_s, f"co_{s}_{t}")         # presses newly CO'd onto s (in CO this shift)
            prod[s, t] = m.NewIntVar(0, cap_s, f"prod_{s}_{t}")     # presses actually producing
            prev = n[s, t - 1] if t > 0 else min(init_n[s], cap_s)
            if t == 0 and _n_free > 0:
                # an absent press (no moulds) starts s FRESH -> not charged a CO, produces this shift
                _free[s] = m.NewIntVar(0, cap_s, f"freestart_{s}")
                m.Add(co[s, t] >= n[s, t] - prev - _free[s])
            else:
                m.Add(co[s, t] >= n[s, t] - prev)
            m.Add(co[s, t] <= n[s, t])
            m.Add(prod[s, t] == n[s, t] - co[s, t])                 # CO'd presses don't produce their CO shift
    if _free:
        m.Add(sum(_free.values()) <= _n_free)                      # total CO-free Day-1 starts <= #absent presses
    for t in T:                                                     # press-count cap
        m.Add(sum(n[s, t] for s in cure_skus) <= n_press)
    for d in range(n_days):                                         # daily curing-CO cap
        day_ts = [d * 3 + k for k in range(3)]
        m.Add(sum(co[s, t] for s in cure_skus for t in day_ts) <= MAX_CO_PER_DAY)

    # shared-mould contention: SKUs sharing a mould pool compete for it. Union-find
    # over sku_moulds -> components; per component per shift: sum n[s,t] <= |union moulds|//2.
    # Tighter than the per-SKU pair cap when SKUs share moulds (the 15"/13" pattern).
    _parent = {s: s for s in cure_skus}
    def _find(a):
        while _parent[a] != a:
            _parent[a] = _parent[_parent[a]]; a = _parent[a]
        return a
    _owner = {}
    for s in cure_skus:
        for md in mi.sku_moulds.get(s, ()):
            if md in _owner:
                ra, rb = _find(s), _find(_owner[md])
                if ra != rb:
                    _parent[ra] = rb
            else:
                _owner[md] = s
    _comps: dict = {}
    for s in cure_skus:
        _comps.setdefault(_find(s), []).append(s)
    n_shared = 0
    for comp_skus in _comps.values():
        if len(comp_skus) <= 1:
            continue                                                # disjoint -> per-SKU domain cap suffices
        union_moulds = set()
        for s in comp_skus:
            union_moulds.update(mi.sku_moulds.get(s, ()))
        cap = len(union_moulds) // 2
        n_shared += 1
        for t in T:
            m.Add(sum(n[s, t] for s in comp_skus) <= cap)

    # ================= GT RESERVOIR (building <-> curing) ==================
    # Tight per-SKU per-shift upper bounds (huge domains cripple CP-SAT presolve).
    built_ub = {s: sum(mi.build_rate[(mac, ss)] for (mac, ss) in bld_pairs if ss == s) for s in mi.skus}
    cured_ub = {s: (mi.cure_rate.get(s, 0) * min(mi.mould_pairs.get(s, 0), elig_cnt.get(s, 0))
                    if s in cure_skus else 0) for s in mi.skus}
    cured, built, inv = {}, {}, {}
    gt_woff_vars = []            # FIX 2 GT-aging writeoff vars (empty unless GT_AGE)
    for s in mi.skus:
        gw = {}                                                     # FIX 2 per-shift writeoff
        for t in T:
            cured[s, t] = m.NewIntVar(0, max(0, cured_ub[s]), f"cured_{s}_{t}")
            built[s, t] = m.NewIntVar(0, max(0, built_ub[s]), f"built_{s}_{t}")
            inv[s, t]   = m.NewIntVar(0, GT_CAP, f"inv_{s}_{t}")
            m.Add(cured[s, t] == mi.cure_rate.get(s, 0) * prod[s, t]) if (s in cure_skus) else m.Add(cured[s, t] == 0)
            gp = [g[mac, s, t] for (mac, ss) in bld_pairs if ss == s]
            m.Add(built[s, t] == sum(gp)) if gp else m.Add(built[s, t] == 0)
            prev_inv = inv[s, t - 1] if t > 0 else int(init_gt.get(s, 0))
            if GT_AGE:
                # penalized writeoff absorbs GT that is physically un-cureable in time (no
                # eligible press / no remaining demand), so the aging bound below never makes
                # the model infeasible; the solver minimizes it -> writes off only when forced.
                gw[s, t] = m.NewIntVar(0, GT_CAP + max(0, built_ub[s]), f"gtwoff_{s}_{t}")
                m.Add(inv[s, t] == prev_inv + built[s, t] - cured[s, t] - gw[s, t])
                gt_woff_vars.append(gw[s, t])
            else:
                m.Add(inv[s, t] == prev_inv + built[s, t] - cured[s, t])
        m.Add(sum(cured[s, t] for t in T) <= mi.demand[s])          # sacred demand cap
        if BUILT_CAP:
            # FIX 4: total GT built this window + GT already built/carried in <= demand.
            # built[] sums ONLY GT machines (g), so Stage-1 carcass is excluded automatically.
            m.Add(sum(built[s, t] for t in T) <= max(0, int(mi.demand.get(s, 0)) - int(init_gt.get(s, 0))))
        if GT_AGE:
            # FIX 2: GT cannot sit > 3 days (=9 shifts). On-hand GT at shift t may only be GT
            # built in the last 9 shifts (opening GT counts fresh only through the first 9
            # shifts -> forced cured/written off early). Writeoff (in the reservoir above)
            # reduces inv, so an un-cureable stale SKU stays feasible.
            for t in T:
                recent = sum(built[s, tau] for tau in range(max(0, t - 8), t + 1))
                opening = int(init_gt.get(s, 0)) if t < 9 else 0
                m.Add(inv[s, t] <= recent + opening)
    for t in T:                                                     # per-SHIFT total GT cap (hard plant limit)
        m.Add(sum(inv[s, t] for s in mi.skus) <= GT_CAP)

    if DAY_CURE_CAP > 0:                                            # anti-front-load: hard daily cured ceiling
        _cdays_cc = commit_days if commit_days is not None else n_days
        for d in range(_cdays_cc):
            day_ts = [d * 3 + k for k in range(3)]
            m.Add(sum(cured[s, t] for s in cure_skus for t in day_ts) <= DAY_CURE_CAP)

    # ---- SMOOTH 1: per-committed-day GT-BUILD cap (OPT_DAY_BUILD_CAP) ----
    # Hard cap on TOTAL GT built per committed day (all GT machines/SKUs, the day's 3 shifts).
    # built[] sums ONLY GT (g); Stage-1 carcass (gc) is excluded automatically. Trims extreme
    # early over-building without touching the normal rate. 1 constraint/day, no objective coeff.
    if DAY_BUILD_CAP_ON:
        import math
        if _DAY_BUILD_CAP_RAW > 1:
            day_build_cap_value = _DAY_BUILD_CAP_RAW
        else:
            day_build_cap_value = int(math.ceil(
                DAY_BUILD_CAP_MULT * sum(mi.demand.values()) / max(1, int(mi.planning_days))))
        _cdays_db = commit_days if commit_days is not None else n_days
        for d in range(_cdays_db):
            day_ts = [d * 3 + k for k in range(3)]
            m.Add(sum(built[s, t] for s in mi.skus for t in day_ts) <= day_build_cap_value)

    # ---- SMOOTH 3: per-SKU GT just-in-time pacing (OPT_GT_JIT, default OFF, testing) ----
    # Cumulative GT built through committed day d <= cumulative cured through day d+3 (3-day GT
    # shelf) + tiny penalized slack (weight <= 2). Stronger lever (closer to the hard shelf that
    # hurt CP-SAT convergence) -> keep OFF for production; enable only for A/B.
    if GT_JIT:
        _cdays_gj = commit_days if commit_days is not None else n_days
        for s in mi.skus:
            for d in range(_cdays_gj):
                built_through_d = sum(built[s, tau] for tau in range((d + 1) * 3))
                cured_hi = min(W, (d + 4) * 3)
                cured_through_d3 = sum(cured[s, tau] for tau in range(cured_hi))
                sl = m.NewIntVar(0, 10 ** 7, f"gtjit_slack_{s}_{d}")
                m.Add(built_through_d <= cured_through_d3 + sl)
                gt_jit_slack.append(sl)

    # ================= OBJECTIVE: max cured =====
    # Maximizing cured already discourages COs (a CO'd press produces nothing that
    # shift), so no explicit CO penalty / large scaling is needed. probe_feasible
    # drops the objective to test that a feasible solution exists at all.
    total_cured = sum(cured[s, t] for s in mi.skus for t in T)
    total_co    = sum(co[s, t] for s in cure_skus for t in T)
    # Terminal value at the COMMIT boundary (the GT actually handed to the next window),
    # not the discarded window tail: reward end-of-commit GT so windows stop front-loading.
    _cd = commit_days if commit_days is not None else n_days
    term_shift = min(W - 1, _cd * 3 - 1)
    term_gt = sum(inv[s, term_shift] for s in mi.skus)
    inch_pen = INCH_PEN * sum(diff_co_vars) if diff_co_vars else 0
    bld_rank_pen = sum(bld_rank_terms) if bld_rank_terms else 0   # INCH_FLEX: graded off-dominant preference

    # building diff-inch-CO penalty (OPT_BLD_CO): ONLY diff (inch) COs are penalized, and
    # PER-GROUP (Stage2=0 unlimited, VMI light, BJ/UNI/S1 heavy). Same-size COs are free &
    # unmodelled. 0 (int) when OFF -> objective bit-for-bit.
    bld_co_pen = 0
    if BLD_CO and diff_co_vars_bld:
        bld_co_pen = sum(PEN_DIFF_BY_GROUP.get(mi.pen_group.get(mac, "S1"), 0) * dco
                         for (mac, t), dco in diff_co_by_mt.items())
        if BLD_MINDWELL and _bld_b2b_vars:
            bld_co_pen = bld_co_pen + BLD_MINDWELL_PEN * sum(_bld_b2b_vars)

    # ---- FIX 2 / FIX 3 aging-writeoff penalty (0 when both toggles OFF -> bit-for-bit) ----
    # Writeoff is a last-resort escape for physically un-cureable/un-consumable stale GT or
    # carcass; penalizing it keeps the solver from writing off voluntarily (it would rather
    # cure/consume). Small integer weight per written-off unit.
    woff_pen = 0
    if GT_AGE and gt_woff_vars:
        woff_pen = woff_pen + GT_AGE_WOFF_PEN * sum(gt_woff_vars)
    if CARC_AGE and carc_woff_vars:
        woff_pen = woff_pen + CARC_AGE_WOFF_PEN * sum(carc_woff_vars)

    # ---- SMOOTHING slack penalty (0 when both JIT smoothing toggles OFF -> bit-for-bit).
    # Only the JIT features carry slack; OPT_DAY_BUILD_CAP is a hard cap with no penalty term.
    smooth_pen = 0
    if CARC_JIT and carc_jit_slack:
        smooth_pen = smooth_pen + CARC_JIT_PEN * sum(carc_jit_slack)
    if GT_JIT and gt_jit_slack:
        smooth_pen = smooth_pen + GT_JIT_PEN * sum(gt_jit_slack)

    # ---- DEMAND-PRIORITY (FIX 3): mild demand-weighted cured + soft pro-rata floors ----
    # OFF (default): SCALE=1, w[s]=1, no slack -> objective is IDENTICAL to the flat path.
    slack_vars = []
    prio_floors = []                 # (sku, floor_target) for the report
    if PRIORITY:
        SCALE = PRIO_SCALE                             # base weight on cured (KEEP 1)
        _dvals = [mi.demand.get(s, 0) for s in mi.skus if mi.demand.get(s, 0) > 0]
        _max_dem = max(_dvals) if _dvals else 1
        # ADDITIVE completion gradient: a small integer BONUS per cured unit that grows with
        # SKU demand (0..PRIO_ALPHA). The base term stays `total_cured` at coeff SCALE, so
        # coefficients never blow up; this only nudges the solver to finish the big under-
        # served SKUs on near-ties. bonus[s] = round(alpha * demand/max_demand) in {0..alpha}.
        bonus = {s: int(round(PRIO_ALPHA * mi.demand.get(s, 0) / _max_dem)) for s in mi.skus}
        # pro-rata soft floors on the top-N big CAPACITY-FEASIBLE SKUs
        _days_left  = int(days_left) if days_left else n_days
        _cshifts    = _cd * 3                          # committed shifts this window
        _committed  = range(_cshifts)
        feas = []
        for s in cure_skus:
            rem = mi.demand.get(s, 0)
            if rem <= 0 or built_ub.get(s, 0) <= 0:    # in demand AND buildable
                continue
            floor_t = int(PRIO_FLOOR_FRAC * rem * _cd / max(1, _days_left))
            floor_t = min(floor_t, rem)                # sits BELOW the sacred demand cap
            if floor_t <= 0:
                continue
            cap_presses      = min(mi.mould_pairs.get(s, 0), elig_cnt.get(s, 0))
            cure_cap_commit  = cap_presses * mi.cure_rate.get(s, 0) * _cshifts
            build_cap_commit = built_ub.get(s, 0) * _cshifts
            # CAPACITY-FEASIBLE: reachable within the committed horizon on BOTH sides
            if cure_cap_commit >= floor_t and build_cap_commit >= floor_t:
                feas.append((rem, s, floor_t))
        feas.sort(key=lambda r: (-r[0], r[1]))         # biggest remaining demand first, deterministic
        for rem, s, floor_t in feas[:PRIO_TOPN]:
            sl = m.NewIntVar(0, floor_t, f"prioslack_{s}")
            m.Add(sum(cured[s, t] for t in _committed) + sl >= floor_t)   # SOFT: slack>=0 -> never infeasible
            slack_vars.append(sl)
            prio_floors.append((s, floor_t))
        # base (coeff SCALE) + additive gradient (coeff <= PRIO_ALPHA on the biggest SKUs).
        weighted_cured = (SCALE * total_cured
                          + sum(bonus[s] * cured[s, t] for s in mi.skus for t in T if bonus[s]))
        slack_pen = PRIO_SLACK_PEN * sum(slack_vars) if slack_vars else 0
        # Penalties/terminal-GT keep the SAME relative strength as the flat objective (scaled
        # by SCALE so, at SCALE=1, they are byte-identical to the flat path's coefficients).
        priority_obj = (weighted_cured
                        - SCALE * CO_PENALTY * total_co
                        + TERM_GT_W * term_gt
                        - SCALE * inch_pen
                        - SCALE * bld_co_pen
                        - SCALE * bld_rank_pen
                        - SCALE * woff_pen
                        - SCALE * smooth_pen
                        - slack_pen)

    if not probe_feasible:
        if PRIORITY:
            m.Maximize(priority_obj)
        else:
            m.Maximize(total_cured - CO_PENALTY * total_co + TERM_GT_W * term_gt
                       - inch_pen - bld_co_pen - bld_rank_pen - woff_pen - smooth_pen)

    # Warm-start HINT so CP-SAT starts from an incumbent (best != -inf) and spends its
    # budget IMPROVING, not hunting for a first solution. hint=None -> guaranteed-feasible
    # all-zero (bit-for-bit prior behaviour); hint={'n','g'} from the greedy warm-start.
    #
    # COMPLETING the partial hint (the fix): the hint carries only the objective-driving
    # n[s,t] (curing press counts) and g[m,s,t] (building GT). The DEPENDENT decision vars
    # (co/prod/cured/built/inv + Stage-1 gc/xc/carc) were left unhinted, so CP-SAT had to
    # SEARCH for a feasible completion — and single-worker starves before finding one,
    # returning UNKNOWN/0 (measured WORSE than the cold trivial solution). We now DERIVE a
    # consistent value for every dependent var so the hint is a COMPLETE, self-consistent
    # incumbent CP-SAT can accept and improve from:
    #   co   = max(0, n - prev_n)            (minimal press-add -> matches co>=n-prev, co<=n)
    #   prod = n - co  ;  cured = rate*prod  ;  built = Σ g  ;  inv = reservoir recursion
    #   gc/xc = a same-shift carcass pack on eligible Stage-1 machines to cover each S2 SKU's
    #           GT (so carc_inv>=0)  ;  carc_inv = its recursion.
    # repair_hint (set below) tolerates any small residual (e.g. a carcass-capacity-short
    # shift covered by opening buffer) by repairing rather than rejecting the whole incumbent.
    if hint is None:
        for (mac, s) in bld_pairs:
            for t in T:
                m.AddHint(x[mac, s, t], 0)
        for s in cure_skus:
            for t in T:
                m.AddHint(n[s, t], 0)
    else:
        hn, hg = hint.get("n", {}), hint.get("g", {})
        # ---- curing: n + derived co/prod (so cured/inv propagate exactly) ----
        _cap = {s: min(mi.mould_pairs.get(s, 0), elig_cnt.get(s, 0)) for s in cure_skus}
        prod_h = {}
        for s in cure_skus:
            prev = min(int(init_n.get(s, 0)), _cap[s])
            for t in T:
                nv = max(0, min(int(hn.get((s, t), 0)), _cap[s]))
                cov = max(0, nv - prev)
                m.AddHint(n[s, t], nv)
                m.AddHint(co[s, t], cov)
                m.AddHint(prod[s, t], nv - cov)
                prod_h[(s, t)] = nv - cov
                prev = nv
        # ---- building GT (mutable): mirror g into a per-(mac,s,t) dict we can TRIM ----
        g_h = {(mac, s, t): int(hg.get((mac, s, t), 0)) for (mac, s) in bld_pairs for t in T}
        _mac_of_s = {}
        for (mac, s) in bld_pairs:
            _mac_of_s.setdefault(s, []).append(mac)
        # GT-CAP TRIM (the single-worker enabler): the greedy banks GT above the model's
        # hard end-of-day cap `sum(inv[s,tc]) <= GT_CAP`, so its raw plan is INFEASIBLE for
        # the model and single-worker CP-SAT starves trying to repair it -> UNKNOWN. We trim
        # SURPLUS building shift-by-shift so the RUNNING total inventory never exceeds GT_CAP
        # (stricter than end-of-day, so both hold), reducing g_h on the highest-inventory SKUs
        # while keeping every SKU's inv >= 0 (delta <= that SKU's own tentative inv). This
        # makes the completed hint fully feasible -> accepted as a real incumbent even at
        # workers=1. Repair_hint mops up any residual.
        inv_prev = {s: int(init_gt.get(s, 0)) for s in mi.skus}
        for t in T:
            _bl = {s: sum(g_h[(mac, s, t)] for mac in _mac_of_s.get(s, ())) for s in mi.skus}
            _cu = {s: (int(mi.cure_rate.get(s, 0)) * prod_h.get((s, t), 0)
                       if s in cure_skus else 0) for s in mi.skus}
            invt = {s: inv_prev[s] + _bl[s] - _cu[s] for s in mi.skus}
            total = sum(max(0, v) for v in invt.values())
            if total > GT_CAP:
                excess = total - GT_CAP
                # reduce highest-inv SKUs first; never below inv>=0 (delta <= invt[s]) and
                # never remove more than was built this shift (delta <= _bl[s]).
                for s in sorted(mi.skus, key=lambda z: -invt[z]):
                    if excess <= 0:
                        break
                    red = min(excess, max(0, invt[s]), _bl[s])
                    if red <= 0:
                        continue
                    rem = red
                    for mac in _mac_of_s.get(s, ()):        # spread the cut across machines
                        if rem <= 0:
                            break
                        take = min(rem, g_h[(mac, s, t)])
                        g_h[(mac, s, t)] -= take
                        rem -= take
                    invt[s] -= (red - rem)
                    excess -= (red - rem)
            inv_prev = {s: max(0, invt[s]) for s in mi.skus}
        # ---- building GT: g + x (from the trimmed g_h) ----
        for (mac, s) in bld_pairs:
            for t in T:
                gv = g_h[(mac, s, t)]
                m.AddHint(g[mac, s, t], gv)
                m.AddHint(x[mac, s, t], 1 if gv > 0 else 0)
        # ---- Stage-1 carcass: same-shift pack covering each S2 SKU's (trimmed) GT ----
        _s1_for = {}
        for (mac, s) in s1_pairs:
            _s1_for.setdefault(s, []).append(mac)
        gc_h = {}
        for t in T:
            _used_mac = set()
            for s in s2_skus:
                need = sum(g_h[(m2, s, t)] for (m2, ss) in bld_pairs
                           if ss == s and mi.machine_group[m2] == "S2")
                rem = need
                for mac in _s1_for.get(s, ()):
                    if rem <= 0:
                        break
                    if mac in _used_mac:
                        continue
                    br = int(mi.build_rate.get((mac, s), 0))
                    if br <= 0:
                        continue
                    q = min(rem, br)
                    gc_h[(mac, s, t)] = q
                    _used_mac.add(mac)
                    rem -= q
        for (mac, s) in s1_pairs:
            for t in T:
                q = gc_h.get((mac, s, t), 0)
                m.AddHint(gc[mac, s, t], q)
                m.AddHint(xc[mac, s, t], 1 if q > 0 else 0)
        # ---- derived reservoirs: built / cured / inv (all SKUs) + carc_inv (S2) ----
        inv_prev = {s: int(init_gt.get(s, 0)) for s in mi.skus}
        for t in T:
            for s in mi.skus:
                built_v = sum(g_h[(mac, s, t)] for mac in _mac_of_s.get(s, ()))
                cured_v = int(mi.cure_rate.get(s, 0)) * prod_h.get((s, t), 0) if s in cure_skus else 0
                inv_v = inv_prev[s] + built_v - cured_v
                m.AddHint(built[s, t], max(0, built_v))
                m.AddHint(cured[s, t], max(0, cured_v))
                m.AddHint(inv[s, t], max(0, min(GT_CAP, inv_v)))   # clamp to var domain
                inv_prev[s] = inv_v
        carc_prev = {s: int(_init_carc.get(s, 0)) for s in s2_skus}
        for t in T:
            for s in s2_skus:
                cb = sum(gc_h.get((mac, s, t), 0) for mac in _s1_for.get(s, ()))
                cu = sum(g_h[(m2, s, t)] for (m2, ss) in bld_pairs
                         if ss == s and mi.machine_group[m2] == "S2")
                cv = carc_prev[s] + cb - cu
                if (s, t) in carc_inv:
                    m.AddHint(carc_inv[s, t], max(0, cv))
                carc_prev[s] = cv

    # ================= SOLVE (deterministic) ==============================
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = workers
    solver.parameters.random_seed = seed
    if workers <= 1:
        solver.parameters.max_deterministic_time = det_time_s     # deterministic (cloud)
    else:
        solver.parameters.max_time_in_seconds = det_time_s        # wall-clock (local multi-worker)
    solver.parameters.log_search_progress = log
    # WARM-START REPAIR: the completion above hints EVERY var (n/co/prod/x/g/gc/xc +
    # derived built/cured/inv/carc) as a self-consistent, GT-cap-feasible solution, but the
    # BLD-CO setup/dco vars are still left to propagate and any tiny residual must be mopped
    # up. repair_hint tells CP-SAT to treat the hint as a starting point to COMPLETE/repair
    # into a feasible incumbent (and we explicitly do NOT fix vars to hinted values), so even
    # single-worker accepts it instead of starving on an un-completable partial hint (which
    # returned UNKNOWN/0). Gated on a hint present -> hint=None stays BIT-FOR-BIT (no param
    # change on the cold path). OPT_REPAIR_HINT=0 disables for A/B.
    if hint is not None and os.environ.get("OPT_REPAIR_HINT", "1") != "0":
        solver.parameters.repair_hint = True
        solver.parameters.fix_variables_to_their_hinted_value = False
    # Optional feasibility pump for the hinted path (A/B; measured NO-OP single-worker on
    # this model, default OFF). Gated on a hint -> hint=None bit-for-bit.
    if hint is not None and os.environ.get("OPT_HINT_FJ", "0") == "1":
        solver.parameters.use_feasibility_pump = True
    if probe_feasible:
        solver.parameters.stop_after_first_solution = True
    status = solver.Solve(m)
    ok = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    sol = {
        "status": solver.StatusName(status),
        "cured": int(solver.Value(total_cured)) if ok else 0,
        "n_co":  int(solver.Value(total_co)) if ok else None,
        "objective": solver.ObjectiveValue() if ok else None,
        "best_bound": solver.BestObjectiveBound() if ok else None,
        "wall_s": round(solver.WallTime(), 1),
        "det_time": round(solver.deterministic_time() if callable(getattr(solver, "deterministic_time", None))
                          else getattr(solver, "deterministic_time", 0.0), 1),
        "n_bld_pairs": len(bld_pairs), "n_cure_skus": len(cure_skus), "n_skus_built": len(skus_built),
    }
    if ok and BLD_SAME_CO and same_co_by_mt:
        sol["n_bld_same_co"] = int(sum(solver.Value(v) for v in same_co_by_mt.values()))
    if ok and GT_AGE and gt_woff_vars:
        sol["gt_writeoff"] = int(sum(solver.Value(v) for v in gt_woff_vars))
    if ok and CARC_AGE and carc_woff_vars:
        sol["carc_writeoff"] = int(sum(solver.Value(v) for v in carc_woff_vars))
    if ok and diff_co_vars:
        sol["n_diff_co"] = int(sum(solver.Value(v) for v in diff_co_vars))
    if ok and BLD_CO and diff_co_vars_bld:
        # diff-inch (diff-size) building COs only; same-size COs are unmodelled here and
        # derived post-hoc by the writer. n_bld_seam_diff_co = the t=0 window-seam subset
        # (0 when init_mach_sku is None/absent, since no t=0 CO vars are created).
        sol["n_bld_diff_co"] = int(sum(solver.Value(v) for v in diff_co_vars_bld))
        sol["n_bld_seam_diff_co"] = int(sum(solver.Value(v) for (mm, tt), v in diff_co_by_mt.items() if tt == 0))
        _by_grp = {}
        for (mac, tt), v in diff_co_by_mt.items():
            _by_grp[mi.pen_group.get(mac, "S1")] = _by_grp.get(mi.pen_group.get(mac, "S1"), 0) + int(solver.Value(v))
        sol["n_bld_diff_co_by_group"] = _by_grp     # {S1,S2,VMI,BJ,UNI -> count}
        # Per-machine diff-CO counts for the driver to DECREMENT its rolling monthly budget.
        # Reported over the COMMITTED shifts (t < commit_days*3) — the part the driver keeps;
        # the hard budget CONSTRAINT itself is over the full window (see the cap above).
        _commit_shifts = (commit_days if commit_days is not None else n_days) * 3
        _by_mac_commit, _by_mac_full = {}, {}
        for (mac, tt), v in diff_co_by_mt.items():
            val = int(solver.Value(v))
            if val:
                _by_mac_full[mac] = _by_mac_full.get(mac, 0) + val
                if tt < _commit_shifts:
                    _by_mac_commit[mac] = _by_mac_commit.get(mac, 0) + val
        sol["diff_co_by_machine"] = _by_mac_commit          # committed shifts only (driver decrements this)
        sol["diff_co_by_machine_full"] = _by_mac_full       # full window (incl discarded tail)
    if ok and PRIORITY:
        sol["n_prio_floors"] = len(prio_floors)
        sol["prio_slack"] = int(sum(solver.Value(v) for v in slack_vars)) if slack_vars else 0
    if ok and DAY_BUILD_CAP_ON:
        sol["day_build_cap"] = day_build_cap_value
    if ok and CARC_JIT and carc_jit_slack:
        sol["carc_jit_slack"] = int(sum(solver.Value(v) for v in carc_jit_slack))
    if ok and GT_JIT and gt_jit_slack:
        sol["gt_jit_slack"] = int(sum(solver.Value(v) for v in gt_jit_slack))
    if ok and sol["best_bound"] and sol["best_bound"] > 0:
        sol["gap"] = round(max(0.0, (sol["best_bound"] - sol["cured"]) / sol["best_bound"]), 4)
    if ok:
        V = solver.Value
        # Full solution values (consumed by driver = carry-over state, writer = output schedule).
        sol["plan"] = {
            "n":     {k: V(v) for k, v in n.items()},        # (sku,t) -> #presses on sku
            "prod":  {k: V(v) for k, v in prod.items()},     # (sku,t) -> #presses producing
            "co":    {k: V(v) for k, v in co.items()},       # (sku,t) -> #presses newly CO'd on
            "cured": {k: V(v) for k, v in cured.items()},    # (sku,t) -> units cured
            "built": {k: V(v) for k, v in built.items()},    # (sku,t) -> GT units built
            "inv":   {k: V(v) for k, v in inv.items()},      # (sku,t) -> GT inventory EOS
            "g":     {k: V(v) for k, v in g.items()},        # (machine,sku,t) -> GT built (S2+UNI)
            "gc":    {k: V(v) for k, v in gc.items()},       # (machine,sku,t) -> carcass built (S1)
            "carc":  {k: V(v) for k, v in carc_inv.items()}, # (sku,t) -> carcass inventory EOS
        }
        if dci_by_mt:
            sol["plan"]["dci"] = {k: V(v) for k, v in dci_by_mt.items()}  # (machine,t) -> 1 if diff-inch CO
        sol["meta"] = {"n_days": n_days, "W": W, "day_start": day_start,
                       "cure_rate": {s: mi.cure_rate.get(s, 0) for s in mi.skus}}
    return sol


if __name__ == "__main__":
    from optimizer.data import load_model_inputs
    mi = load_model_inputs()
    print("Inputs:", mi.summary())
    print("Solving a 10-day window (det_time=60s)...")
    sol = build_and_solve(mi, day_start=1, n_days=10, det_time_s=60.0, seed=1)
    for k, v in sol.items():
        print(f"  {k}: {v}")
