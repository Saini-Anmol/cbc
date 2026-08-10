"""Rolling receding-horizon driver for the CP-SAT window scheduler.

Chains fixed-size windows across the whole month. Each step COMMITS 10 days but is
SOLVED over a window of up to 13 days (10 committed + up to a 3-day lookahead tail so
the 3-day GT-shelf seam is seen inside the window). Carry-over state (GT inventory,
carcass inventory, per-SKU running press counts) at the last committed shift seeds the
next window; committed cured is subtracted from a shrinking per-SKU demand so the
sacred demand cap holds across the whole month, not just one window.

Deterministic: sorted iteration throughout, fixed seed forwarded to the solver.

Do NOT edit optimizer/model.py or optimizer/data.py — this module only orchestrates
the stable build_and_solve interface.
"""
from __future__ import annotations

import copy
import os
from collections import defaultdict
from datetime import timedelta

import bc_config
from optimizer.data import load_model_inputs
from optimizer.model import build_and_solve
from optimizer.warmstart import greedy_hint
from optimizer.greedy_warmstart import extract_month_greedy, window_hint, window_hint_seeded
from optimizer.writer import recover_assignment

_SHIFT_LABEL = ("A", "B", "C")

COMMIT_DAYS = 10          # days committed per rolling step
LOOKAHEAD_TAIL = 3        # extra lookahead days (3-day GT shelf seam)
MAX_WINDOW_DAYS = COMMIT_DAYS + LOOKAHEAD_TAIL   # 13

# Warm-start mode (env WARMSTART). Default 'none' -> PURE CP-SAT (all-zero seed + let it
# search); reproduced BIT-FOR-BIT.
#   'none'         : pure CP-SAT, all-zero seed (default; bit-for-bit prior behaviour).
#   'simple'       : constructive optimizer.warmstart.greedy_hint (month-opening seeded;
#                    seam-blind for W>1).
#   'greedy'       : SEAM-ALIGNED real-greedy warm-start (window_hint_seeded) — replay the
#                    greedy month plan FORWARD through EACH window's ACTUAL carry-over state
#                    (init_n/init_gt from the previous COMMITTED CP-SAT window + shrinking
#                    demand), clamped to every model cap, so the emitted (n,g) hint is a
#                    FEASIBLE partial assignment for THIS window (no seam GT-underflow, no
#                    seam CO-cap blow, no demand-cap overshoot). Fixes the misaligned slice.
#   'greedy_slice' : the OLD raw month-plan slice (window_hint) that IGNORES carry-over —
#                    kept for A/B ONLY; measured INFEASIBLE at the seam (GT underflow +
#                    demand-cap overshoot) and it regresses the full month. Do not ship.
WARMSTART = os.environ.get("WARMSTART", "none")

# ── Feasibility / accounting fixes, each env-toggle-gated (default ON; "0" = OFF =
#    prior behaviour bit-for-bit). See the per-fix docstrings below. ──
# Fix A — clip each press's per-shift cured at its physical shift capacity (cure_rate),
#   drop excess instead of piling onto too-few presses, and cap physical curing COs at
#   MAX_CHANGEOVERS_PER_DAY/day; realized (post-clip) cured drives demand_remaining.
_CURE_CLIP  = os.environ.get("OPT_CURE_CLIP",  "1") != "0"
# Fix B — post-process mould-life countdown + 8h mould cleans (drops clean-shift cured).
_MOULD_CLEAN = os.environ.get("OPT_MOULD_CLEAN", "1") != "0"
# Fix C — plumb per-committed-day end-of-day GT reservoir total to the building sheet.
_ENDDAY_GT  = os.environ.get("OPT_ENDDAY_GT",  "1") != "0"
# Fix D — expose the recovered per-shift press->mould mounts for the mould sheets.
_MOULD_INFO = os.environ.get("OPT_MOULD_INFO", "1") != "0"
# Building seam carry — persist each building machine's SKU across the window commit
#   seam so the next window charges a first-shift (t=0) building CO when a machine
#   changes SKU/inch across the boundary (feeds build_and_solve's init_mach_sku). Only
#   has effect when the model's OPT_BLD_CO building-CO pricing is ON. OPT_BLD_SEAM=0 ->
#   pass init_mach_sku=None (prior behaviour: no seam building CO, bit-for-bit).
_BLD_SEAM = os.environ.get("OPT_BLD_SEAM", "1") != "0"
# Per-machine MONTHLY diff-CO caps carried across rolling windows. The driver seeds each
#   machine's monthly allowance by its penalty-group, passes the REMAINING balance to each
#   window as build_and_solve(diff_budget=...), and decrements by the committed diff COs
#   (sol["diff_co_by_machine"]). BJ/Unistage/Stage-1 = 2/machine/month (keep them on their
#   inch); VMI = moderate; Stage-2 = uncapped (absent from the dict). OPT_DIFF_BUDGET=0 ->
#   pass diff_budget=None (no cap, bit-for-bit prior).
_DIFF_BUDGET = os.environ.get("OPT_DIFF_BUDGET", "1") != "0"
_DIFF_CAP_BY_GROUP = {
    "BJ":  int(os.environ.get("OPT_DIFF_CAP_BJ",  "2")),
    "UNI": int(os.environ.get("OPT_DIFF_CAP_UNI", "2")),
    "S1":  int(os.environ.get("OPT_DIFF_CAP_S1",  "2")),
    "VMI": int(os.environ.get("OPT_DIFF_CAP_VMI", "12")),
    # "S2" absent by default -> Stage-2 uncapped per-machine ("no limit on Stage-2"). Set
    # OPT_DIFF_CAP_S2 > 0 to give Stage-2 a (generous) per-machine allowance so the TOTAL
    # diff-CO count lands in the 80-120 target incl. Stage-2.
}
_s2_cap = int(os.environ.get("OPT_DIFF_CAP_S2", "0"))
if _s2_cap > 0:
    _DIFF_CAP_BY_GROUP["S2"] = _s2_cap
# SAME-size building CO capacity (post-process trim; rules 15/7). The CP-SAT model charges
# DIFF-inch building CO capacity but NOT same-inch SKU changes (per-SKU continuation binaries
# wreck convergence — measured -96k). We deduct the same-size CO here: any building shift whose
# production (Qty*CT/60) + its same-size CO minutes exceeds 480 is trimmed to fit, and the GT cut
# cascades to curing (cured <= opening + built). OPT_SAME_CO_TRIM=0 -> no trim (prior behaviour).
_SAME_CO_TRIM = os.environ.get("OPT_SAME_CO_TRIM", "0") != "0"
# Decoupled JIT Stage-1 carcass reschedule — discard the CP-SAT's front-loaded carcass and
# re-schedule Stage-1 carcass just-in-time to feed the Stage-2 GT within the 1-day shelf
# (rules 9C + 5 satisfied by construction, no convergence cost). OPT_CARC_RESCHED=0 -> keep the
# model's gc carcass (prior behaviour).
_CARC_RESCHED = os.environ.get("OPT_CARC_RESCHED", "0") != "0"
# GT 3-day (9-shift) + carcass 1-day (3-shift) aging (rule 9), POST-PROCESS. In-model aging
# constraints wreck CP-SAT convergence (~-80k, mostly artifact). Instead we FIFO-reconstruct
# per-SKU inventory (mirroring validate_schedule.py) and remove exactly the stale-stock
# consumption: cures served only by >3-day GT are dropped; Stage-2 GT served only by >1-day
# carcass is dropped (and cascades to cured). OPT_AGE_TRIM=0 -> no aging trim (prior behaviour).
_AGE_TRIM = os.environ.get("OPT_AGE_TRIM", "0") != "0"

_MAX_CO_PER_DAY = int(getattr(bc_config, "MAX_CHANGEOVERS_PER_DAY", 12))
try:
    from b2c_pipeline import CURING_CAVITIES as _CURING_CAVITIES
except Exception:                                   # pragma: no cover
    _CURING_CAVITIES = 2
_MOULD_CLEAN_CYCLES = int(getattr(bc_config, "MOULD_CLEAN_CYCLES", 3000))


def _accumulate_window_rows(sol, mi, day_start, commit_days, building_rows, curing_rows,
                            carry_setup=None, mould_rows=None, carry_moulds=None):
    """Append this window's COMMITTED-days plan (t=0..commit_days*3-1) to the
    month-global row lists, mapping window-local shift t to a global Day/Date/Shift.

    Building rows come from the plan's g (S2/UNI GT) + gc (S1 carcass); curing rows
    come from optimizer.writer.recover_assignment — the CONTINUITY-PRESERVING physical
    press->SKU map recovered from the CP-SAT count vector, seeded with `carry_setup`
    (the press set-up state carried across the previous window's commit seam so a press
    is NOT spuriously reshuffled at the seam). The shift's model-cured units are split
    across ONLY the PRODUCING presses of each SKU (a press newly CO'd this shift produces
    0 — matching the model's prod = n - co), so a press-shift is never double-booked with
    both a 480-min CO and a production share.

    Fix A (env OPT_CURE_CLIP, `_CURE_CLIP`): the split now CAPS each producing press at
    its physical shift capacity `cure_rate[s]` (units one press cures in a full 480-min
    shift) and DROPS the excess when the model's cured total for (s,t) exceeds
    k_physical * cure_rate (rather than piling it onto too-few presses -> occupancy>100%).
    The recovery is also asked (max_co_per_day) to cap physical COs at
    MAX_CHANGEOVERS_PER_DAY/day. The REALIZED (post-clip) cured per SKU is accumulated and
    returned so the driver can shrink demand_remaining by what was actually cured, not the
    abstract model total. With OPT_CURE_CLIP=0 the old conserve-cured split is reproduced
    bit-for-bit and realized == the abstract model total.

    Returns (setup_state, realized_cured, carry_moulds_out):
        setup_state  = press set-up state at the LAST COMMITTED shift (seed next window),
        realized_cured = {sku: realized cured over the committed shifts},
        carry_moulds_out = {press: [mould ids]} mounted at the last committed shift
                           (Fix D continuity carry; {} unless _MOULD_INFO).
    """
    plan = sol["plan"]
    commit_shifts = commit_days * 3
    realized: dict = defaultdict(int)

    def _stamp(t):
        # window-local t -> (global_day, date_str, shift_label)
        local_day = t // 3 + 1
        global_day = (day_start - 1) + local_day
        date_str = (mi.plan_start + timedelta(days=global_day - 1)).strftime("%Y-%m-%d")
        return global_day, date_str, _SHIFT_LABEL[t % 3]

    # ---- building: g = GT (S2/UNI), gc = carcass (S1) ----
    for kind, book in (("GT", plan.get("g", {})), ("carcass", plan.get("gc", {}))):
        for (m, s, t), q in book.items():
            if t >= commit_shifts or q <= 0:
                continue
            gday, date_str, shift = _stamp(t)
            building_rows.append({
                "Day": gday, "Date": date_str, "Shift": shift,
                "Machine": m, "Group": mi.machine_group.get(m, ""),
                "Type": kind, "SKUCode": s, "Inch": mi.sku_inch.get(s, ""),
                "Qty": int(q),
            })

    # ---- curing: CONTINUITY-preserving physical press assignment recovered from the
    #      counts, seeded with the carried-over set-up state (seam persistence) ----
    cured_plan = plan.get("cured", {})
    assignment, _infeasible, co_by_shift, setup_by_shift, mould_by_shift = recover_assignment(
        sol, mi, init_setup=carry_setup, return_extra=True,
        max_co_per_day=(_MAX_CO_PER_DAY if _CURE_CLIP else None),
        compute_moulds=_MOULD_INFO, init_moulds=carry_moulds)
    for t in range(commit_shifts):
        assign_t = assignment.get(t, {})
        if not assign_t:
            continue
        gday, date_str, shift = _stamp(t)
        co_presses = co_by_shift.get(t, set())
        mb_t = mould_by_shift.get(t, {})
        # A press newly onboarded this shift does its 480-min CO now and produces 0.
        # Emit a Qty=0 row (with its NEW SKU) so output_full attributes the 480 CO to
        # THIS shift (0 running-mins), never piling it onto a producing shift -> the
        # press-shift is never double-booked and occupancy stays <= 100%.
        for press in sorted(co_presses):
            s = assign_t.get(press)
            if s is None:
                continue
            curing_rows.append({
                "Day": gday, "Date": date_str, "Shift": shift,
                "Press": press, "SKUCode": s, "Inch": mi.sku_inch.get(s, ""),
                "Qty": 0,
            })
        # group the PRODUCING presses (assigned this shift, NOT newly CO'd) by SKU.
        by_sku: dict = {}
        for press, s in assign_t.items():
            if press in co_presses:               # in CO this shift -> produces 0
                continue
            by_sku.setdefault(s, []).append(press)
        for s in sorted(cured_plan_keys_at(cured_plan, t)):
            total = int(cured_plan.get((s, t), 0))
            if total <= 0:
                continue
            presses = sorted(by_sku.get(s, []))
            if _CURE_CLIP:
                # Fix A steps 1-2: cap each press at cure_rate[s], DROP the excess. No
                # producing press => the cured is dropped (never piled on a CO press).
                cap = int(mi.cure_rate.get(s, 0))
                k = len(presses)
                if k == 0 or cap <= 0:
                    continue
                real_total = min(total, k * cap)      # excess over physical capacity dropped
                realized[s] += real_total             # realized (post-clip) cured
                base, rem = divmod(real_total, k)     # base <= cap always (real_total<=k*cap)
            else:
                # prior behaviour: conserve the full model cured, splitting evenly even if
                # that over-charges too-few presses (the bug this fix addresses).
                realized[s] += total
                if not presses:
                    presses = sorted(p for p, ss in assign_t.items() if ss == s)
                    if not presses:
                        continue
                k = len(presses)
                base, rem = divmod(total, k)
            for i, press in enumerate(presses):
                q = base + (1 if i < rem else 0)
                if q <= 0:
                    continue
                curing_rows.append({
                    "Day": gday, "Date": date_str, "Shift": shift,
                    "Press": press, "SKUCode": s, "Inch": mi.sku_inch.get(s, ""),
                    "Qty": int(q),
                })
        # ---- Fix D: record the per-shift physical mould mounts for the mould sheets ----
        if _MOULD_INFO and mould_rows is not None and mb_t:
            for press in sorted(assign_t):
                moulds = mb_t.get(press)
                if not moulds:
                    continue
                mould_rows.append({
                    "Day": gday, "Date": date_str, "Shift": shift, "Press": press,
                    "SKUCode": assign_t[press], "Moulds": list(moulds),
                    "InCO": press in co_presses,
                })

    # set-up + mould state at the last committed shift (persist across the next seam).
    last_moulds: dict = {}
    if _MOULD_INFO:
        last_moulds = {p: list(ms) for p, ms in mould_by_shift.get(commit_shifts - 1, {}).items()
                       if ms}
    return setup_by_shift.get(commit_shifts - 1, {}), dict(realized), last_moulds


def cured_plan_keys_at(cured_plan, t):
    """SKUs with a cured entry at shift t (deterministic iteration)."""
    return [s for (s, tt) in cured_plan if tt == t]


def run_rolling(mi, det_time_s: float = 90.0, workers: int = 8, seed: int = 1,
                verbose: bool = True) -> dict:
    """Roll fixed windows over mi.planning_days. Returns per-window records + month totals.

    For each step:
      1. win_mi = copy.copy(mi) with demand replaced by the *remaining* per-SKU demand.
      2. Solve a window of min(MAX_WINDOW_DAYS, days_left) days, seeded with carry-over state.
      3. Commit min(COMMIT_DAYS, days_left) days: accumulate committed cured, shrink demand.
      4. Carry over GT/carcass/press-count state at the last committed shift.
    """
    total_demand = sum(mi.demand.values())
    demand_remaining = dict(mi.demand)          # sacred cap tracker, shrinks as we commit

    # ---- REAL-GREEDY per-window warm-start: extract the greedy's month plan ONCE ----
    greedy_n = greedy_g = None
    if WARMSTART in ("greedy", "greedy_slice"):
        try:
            greedy_n, greedy_g, gmeta = extract_month_greedy(mi)
            if verbose:
                print(f"  [GREEDY_WARM] using real-greedy month plan "
                      f"({len(greedy_n):,} n / {len(greedy_g):,} g entries); meta={gmeta}")
        except Exception as _e:
            greedy_n = greedy_g = None
            if verbose:
                print(f"  [GREEDY_WARM] extraction failed ({_e}); "
                      f"falling back to constructive greedy_hint")

    # carry-over state (None on the first window -> model uses opening state)
    cur_gt = None
    cur_carc = None
    cur_n = None
    # physical press set-up state carried across the commit seam (None -> Day-0 mounted
    # SKU) so the continuity recovery does not spuriously reshuffle presses at the seam.
    carry_setup = None
    carry_moulds = None          # Fix D: press->[moulds] carried across the seam
    # building machine set-up carried across the seam: {machine -> last-committed SKU}
    # (feeds the NEXT window's init_mach_sku so a seam SKU/inch change is charged a
    # building CO). None on the first window -> model charges no t=0 building CO.
    cur_mach_sku = None
    # per-machine REMAINING monthly diff-CO budget (seeded by penalty-group; Stage-2 and
    # unknown-group machines are omitted -> uncapped). Decremented by committed diff COs.
    _pen_group = getattr(mi, "pen_group", {}) or {}
    diff_remaining = {}
    if _DIFF_BUDGET:
        for _m, _g in _pen_group.items():
            if _g in _DIFF_CAP_BY_GROUP:
                diff_remaining[str(_m)] = _DIFF_CAP_BY_GROUP[_g]

    windows: list = []
    total_cured = 0
    total_co = 0
    total_wall_s = 0.0

    # month-global committed-days plan rows (for the Excel schedule writer)
    building_rows: list = []
    curing_rows: list = []
    mould_rows: list = []                    # Fix D: per-shift press->mould mounts
    endday_gt_by_date: dict = {}             # Fix C: {date_str: end-of-day total GT reservoir}

    day_start = 1
    days_left = int(mi.planning_days)

    while days_left > 0:
        win_days = min(MAX_WINDOW_DAYS, days_left)
        commit_days = min(COMMIT_DAYS, days_left)

        win_mi = copy.copy(mi)
        win_mi.demand = dict(demand_remaining)

        # Greedy warm-start incumbent for this window.
        #  'greedy'      : SEAM-ALIGNED replay (window_hint_seeded) — greedy's month plan
        #                  replayed FORWARD through THIS window's ACTUAL carry-over state
        #                  (cur_n / cur_gt from the previous COMMITTED CP-SAT window, and the
        #                  shrinking demand_remaining), clamped to every model cap. The emitted
        #                  (n,g) is therefore a FEASIBLE partial assignment for this window —
        #                  it never underflows GT / blows the seam CO cap / overshoots demand
        #                  (the failures the raw slice had), so it HELPS instead of misleading.
        #  'greedy_slice': the OLD raw absolute-index slice (window_hint) that ignores carry-
        #                  over. A/B only — measured infeasible at the seam.
        if WARMSTART == "greedy" and greedy_n is not None:
            try:
                win_hint = window_hint_seeded(
                    mi, greedy_n, greedy_g, day_start, win_days,
                    init_n=cur_n, init_gt=cur_gt, demand=demand_remaining)
            except Exception as _e:
                win_hint = None
                if verbose:
                    print(f"  [window day {day_start}] seam-aligned greedy hint failed ({_e}); all-zero start")
        elif WARMSTART == "greedy_slice" and greedy_n is not None:
            try:
                win_hint = window_hint(mi, greedy_n, greedy_g, day_start, win_days)
            except Exception as _e:
                win_hint = None
                if verbose:
                    print(f"  [window day {day_start}] real-greedy slice failed ({_e}); all-zero start")
        elif WARMSTART == "simple":
            try:
                win_hint = greedy_hint(win_mi, day_start, win_days)
            except Exception as _e:
                win_hint = None
                if verbose:
                    print(f"  [window day {day_start}] greedy_hint failed ({_e}); all-zero start")
        else:
            win_hint = None    # PURE CP-SAT: build_and_solve applies the minimal all-zero seed

        sol = build_and_solve(
            win_mi, day_start, n_days=win_days, commit_days=commit_days,
            days_left=days_left,   # FIX 3: pro-rata priority-floor pacing denominator
            init_gt=cur_gt, init_n=cur_n, init_carc=cur_carc,
            init_mach_sku=(cur_mach_sku if _BLD_SEAM else None),  # building seam carry
            diff_budget=(diff_remaining if _DIFF_BUDGET else None),  # per-machine monthly cap
            det_time_s=det_time_s, seed=seed, workers=workers, hint=win_hint,
        )

        status = sol.get("status", "UNKNOWN")
        wall_s = float(sol.get("wall_s", 0.0) or 0.0)
        total_wall_s += wall_s

        ok = status in ("OPTIMAL", "FEASIBLE") and "plan" in sol
        commit_shifts = range(commit_days * 3)      # t = 0 .. commit_days*3 - 1
        tc = commit_days * 3 - 1                     # last committed shift index

        cured_committed = 0
        if ok:
            plan = sol["plan"]
            cured_plan = plan["cured"]
            inv_plan = plan["inv"]
            carc_plan = plan["carc"]
            n_plan = plan["n"]

            # carry-over state at the last committed shift
            cur_gt = {s: inv_plan.get((s, tc), 0) for s in mi.skus}
            cur_carc = {s: carc_plan.get((s, tc), 0) for s in mi.skus}
            cur_n = {s: n_plan.get((s, tc), 0) for s in mi.skus}

            # Building seam carry: each machine's MOST-RECENT producing SKU within the
            # committed window (g = GT machines, gc = Stage-1 carcass). A machine idle at
            # every committed shift is absent -> next window charges it no t=0 CO. Feeds
            # the next window's init_mach_sku (Fix: persist building setup across seams).
            if _BLD_SEAM:
                _last_bld: dict = {}          # machine -> (shift_t, sku)
                _cshifts = commit_days * 3
                for _book in (plan.get("g", {}), plan.get("gc", {})):
                    for (_m, _s, _t), _q in _book.items():
                        if _t >= _cshifts or _q <= 0:
                            continue
                        _prev = _last_bld.get(_m)
                        if _prev is None or _t > _prev[0]:
                            _last_bld[_m] = (_t, _s)
                cur_mach_sku = {_m: _sv for _m, (_t, _sv) in _last_bld.items()}

            # Per-machine monthly diff-CO budget: decrement each capped machine's remaining
            # balance by the diff COs it did over the COMMITTED shifts this window, so later
            # windows see the shrunk allowance (BJ/UNI/S1 -> 2/month, VMI -> 6/month).
            if _DIFF_BUDGET and diff_remaining:
                _dcm = sol.get("diff_co_by_machine", {}) or {}
                for _m in list(diff_remaining):
                    _used = int(_dcm.get(_m, 0))
                    if _used:
                        diff_remaining[_m] = max(0, diff_remaining[_m] - _used)

            # Fix C: per-committed-day end-of-day (shift C) total GT reservoir.
            if _ENDDAY_GT:
                for d in range(1, commit_days + 1):
                    t_end = d * 3 - 1                       # last shift of local day d
                    gday = (day_start - 1) + d
                    date_str = (mi.plan_start + timedelta(days=gday - 1)).strftime("%Y-%m-%d")
                    endday_gt_by_date[date_str] = int(
                        sum(inv_plan.get((s, t_end), 0) for s in mi.skus))

            # accumulate this window's committed-days plan into the month schedule.
            # _accumulate_window_rows returns the REALIZED (post-clip, Fix A) cured per
            # SKU so demand_remaining shrinks by what was physically cured, not the
            # abstract model total. On failure fall back to the abstract totals so the
            # demand accounting stays robust (prior behaviour).
            realized_cured = None
            try:
                carry_setup, realized_cured, carry_moulds = _accumulate_window_rows(
                    sol, mi, day_start, commit_days, building_rows, curing_rows,
                    carry_setup=carry_setup, mould_rows=mould_rows,
                    carry_moulds=carry_moulds)
            except Exception as _e:
                if verbose:
                    print(f"  [window day {day_start}] schedule-row accumulation "
                          f"failed ({_e}); continuing")
                realized_cured = None
            if realized_cured is None:
                realized_cured = {}
                for s in mi.skus:
                    c = sum(int(cured_plan.get((s, t), 0)) for t in commit_shifts)
                    if c:
                        realized_cured[s] = c
            for s, c in realized_cured.items():
                if c:
                    cured_committed += c
                    demand_remaining[s] = max(0, demand_remaining[s] - c)
        else:
            # window failed to produce a usable plan: commit nothing, keep prior
            # carry-over state, and advance (robust to infeasible/unknown windows).
            if verbose:
                print(f"  [window day {day_start}] status={status} -> no plan, "
                      f"committed cured=0, carrying prior state")

        # Count ONLY committed-day COs (t in commit_shifts), not the 3-day lookahead
        # tail — else each window's tail COs are double-counted across the horizon.
        if ok:
            co_plan = sol["plan"].get("co", {})
            total_co += sum(int(co_plan.get((s, t), 0))
                            for s in mi.skus for t in commit_shifts if (s, t) in co_plan)
        total_cured += cured_committed

        gap = sol.get("gap")
        rec = {
            "day_start": day_start,
            "win_days": win_days,
            "commit_days": commit_days,
            "status": status,
            "cured_committed": cured_committed,
            "gap": gap,
            "wall_s": wall_s,
        }
        windows.append(rec)

        if verbose:
            gap_s = f"{gap:.3f}" if isinstance(gap, (int, float)) else "  -  "
            print(f"  window day {day_start:>2} (win={win_days}d commit={commit_days}d) "
                  f"status={status:<8} cured_committed={cured_committed:>7,} "
                  f"gap={gap_s} wall={wall_s:>6.1f}s")

        day_start += COMMIT_DAYS
        days_left -= commit_days

    # ── Dump the RAW solver rows (pre-post-process) so the post-processing (trims / aging /
    #    carcass reschedule) can be iterated + re-validated STANDALONE, with ZERO re-solves.
    _dump = os.environ.get("OPT_DUMP_RAW")
    if _dump:
        try:
            import pickle
            with open(_dump, "wb") as _f:
                pickle.dump({"building_rows": building_rows, "curing_rows": curing_rows,
                             "mould_rows": mould_rows, "endday_gt_by_date": dict(endday_gt_by_date),
                             "total_cured_raw": total_cured, "total_co": total_co,
                             "total_demand": total_demand, "windows": windows}, _f)
            if verbose:
                print(f"  [dump] raw solver rows -> {_dump}")
        except Exception as _e:
            if verbose:
                print(f"  [dump] failed: {_e}")

    return _finalize_plan(mi, building_rows, curing_rows, mould_rows, endday_gt_by_date,
                          total_cured, total_co, total_demand, windows, total_wall_s, verbose)


def _finalize_plan(mi, building_rows, curing_rows, mould_rows, endday_gt_by_date,
                   total_cured, total_co, total_demand, windows, total_wall_s=0.0, verbose=True):
    """ALL post-solve processing, in order: same-CO trim -> mould clean -> GT aging write-off ->
    JIT carcass reschedule. Operates purely on the CP-SAT output rows (no solver) so it can be
    replayed STANDALONE from a dumped raw plan (optimizer/finalize_from_dump.py) — iterate +
    re-validate any post-process fix with ZERO re-solves. Returns the res dict for the writers."""
    # ── SAME-size building CO capacity charge (post-process trim; rules 15/7) ──
    if _SAME_CO_TRIM:
        n_tr, gt_tr, cured_dr, gt_redist = _same_co_trim(mi, building_rows, curing_rows)
        total_cured -= cured_dr
        if verbose:
            print(f"  [same-CO trim] {n_tr} over-packed building shifts; {gt_redist:,} GT units "
                  f"REDISTRIBUTED to spare same-SKU shifts, {gt_tr:,} cut, cured -{cured_dr:,}")

    # ── Fix B: post-process mould-life countdown + 8h mould cleans ──────────────
    mould_clean_events: list = []
    final_mould_life: dict = {}
    if _MOULD_CLEAN:
        mould_clean_events, final_mould_life, clean_dropped = _apply_mould_cleans(
            mi, curing_rows)
        total_cured -= clean_dropped                # realized cured drops by clean-shift prod
        if verbose and clean_dropped:
            print(f"  [mould clean] {len(mould_clean_events)} cleans, "
                  f"dropped {clean_dropped:,} cured on clean shifts")

    # ── GT aging write-off (post-process; rule 9-GT). Runs after mould-clean, BEFORE the carcass
    #    reschedule + cured≤built cascade (which enforces cured<=built so no cured-without-GT). ──
    if _AGE_TRIM:
        gt_wo, carc_wo = _apply_aging(mi, building_rows, curing_rows)
        if verbose:
            print(f"  [aging write-off] aged-surplus GT>3d removed {gt_wo:,}; "
                  f"carcass>1d removed {carc_wo:,} (KPI-neutral; curing untouched)")

    # ── Decoupled JIT Stage-1 carcass reschedule (rules 9C + 5 by construction) ──
    #    Runs LAST — on the FINAL Stage-2 GT — so nothing downstream breaks its 1-day freshness.
    if _CARC_RESCHED:
        try:
            from optimizer.carcass_sched import reschedule_carcass
            n_c, tot_c = reschedule_carcass(mi, building_rows)
            rep = getattr(reschedule_carcass, "report", {}) or {}
            s2_red = int(rep.get("stage2_reduced", 0))
            # The reschedule reduces (in place) any Stage-2 GT it cannot back with 1-day-fresh
            # carcass (rule 5 by construction). Cascade to cured: cured cannot exceed a SKU's
            # available GT (opening + built) — reduce the excess so no curing-without-GT.
            try:
                from b2c_pipeline import _MACHINE_GROUP as _MG
                _S1 = {m for m, g in _MG.items() if g == "STAGE1"}
            except Exception:
                _S1 = {"6802", "6803", "6909", "6911", "7601", "7701", "7801", "7802",
                       "7803", "7804", "8001", "8002", "8003", "8101"}
            built_gt = defaultdict(float)
            for r in building_rows:
                if str(r["Machine"]) not in _S1 and float(r["Qty"]) > 0:
                    built_gt[str(r["SKUCode"])] += float(r["Qty"])
            cure_by_sku = defaultdict(list); cured_tot = defaultdict(float)
            for r in curing_rows:
                if float(r["Qty"]) > 0:
                    cure_by_sku[str(r["SKUCode"])].append(r)
                    cured_tot[str(r["SKUCode"])] += float(r["Qty"])
            sf_dropped = 0
            for sku, cu in cured_tot.items():
                avail = built_gt.get(sku, 0.0) + float(mi.opening_gt.get(sku, 0.0))
                if cu > avail + 0.5:
                    sf_dropped += int(_reduce_sku_rows(cure_by_sku, sku, cu - avail))
            total_cured -= sf_dropped
            if verbose:
                print(f"  [carcass reschedule] {n_c:,} JIT carcass rows, {tot_c:,} units "
                      f"(1-day shelf); Stage-2 GT reduced {s2_red} units -> cured -{sf_dropped:,}")
        except Exception as _e:
            if verbose:
                print(f"  [carcass reschedule] FAILED ({_e}); keeping model carcass")

    coverage = (total_cured / total_demand) if total_demand else 0.0
    return {
        "windows": windows,
        "total_demand": total_demand,
        "total_cured": total_cured,
        "total_co": total_co,
        "total_wall_s": round(total_wall_s, 1),
        "coverage": coverage,
        "building_rows": building_rows,
        "curing_rows": curing_rows,
        "mould_rows": mould_rows,                          # Fix D
        "endday_gt_by_date": (endday_gt_by_date if _ENDDAY_GT else None),  # Fix C
        "mould_clean_events": mould_clean_events,          # Fix B
        "final_mould_life": final_mould_life,              # Fix B
    }


def _reduce_sku_rows(rows_by_sku, sku, amount):
    """Reduce `amount` units from a SKU's rows, latest (day,shift) first. Returns dropped."""
    _SORD = {"A": 0, "B": 1, "C": 2}
    need = float(amount); dropped = 0.0
    for r in sorted(rows_by_sku.get(sku, []),
                    key=lambda r: (int(r["Day"]), _SORD.get(r["Shift"], 0)), reverse=True):
        if need <= 1e-9:
            break
        q = float(r["Qty"]); cut = min(q, need)
        r["Qty"] = int(round(q - cut)); need -= cut; dropped += cut
    return dropped


def _fifo_stale(inflow, outflow, opening, limit_shifts, skus, horizon_shifts):
    """Mirror validate_schedule.py FIFO: per SKU, return {sku: total outflow that FIFO can
    ONLY serve with stock older than `limit_shifts` (i.e. stale/invalid consumption)}.
    Opening stock is treated as fresh (never ages out — its pre-horizon age is unknown)."""
    from collections import deque
    short = defaultdict(float)
    for s in skus:
        q = deque()
        op = float(opening.get(s, 0.0))
        if op > 0:
            q.append([-10**9, op])                       # opening: fresh
        for t in range(horizon_shifts):
            while q and q[0][0] >= 0 and (t - q[0][0]) > limit_shifts:
                q.popleft()                              # writeoff aged in-horizon stock
            add = inflow.get((s, t), 0.0)
            if add > 0:
                q.append([t, add])
            need = outflow.get((s, t), 0.0)
            while need > 1e-9 and q:
                if q[0][1] <= need + 1e-9:
                    need -= q[0][1]; q.popleft()
                else:
                    q[0][1] -= need; need = 0.0
            if need > 1e-9:
                short[s] += need                         # could not serve fresh -> invalid
    return short


def _age_fifo_process(inflow_at, outflow_at, opening, limit_days, skus, n_days):
    """Shelf-life FIFO on per-(sku,day) inflow/outflow row-lists — DAY-LEVEL, matching
    validate_schedule.py exactly (aging measured in DAYS, write-off at end of each day). For each
    SKU walk days: add the day's inflow (tagged with the day index), serve the day's outflow FIFO,
    then WRITE OFF (remove from the inflow rows) any stock older than `limit_days` that was never
    consumed — the validator flags exactly this aged-built stock, and since it was not consumed,
    removing it does NOT reduce output. Any outflow that cannot be served from non-aged stock is
    returned per (sku,day) as UNSERVABLE (invalid consumption to be dropped). Opening stock is
    fresh. Mutates inflow_at row Qty (aged writeoff). Returns {(sku,day): unservable}."""
    from collections import deque
    def qsum(m, key):
        return sum(float(x["Qty"]) for x in m.get(key, []))
    def qreduce(m, key, amt):
        need = amt
        for r in m.get(key, []):
            if need <= 1e-9:
                break
            q = float(r["Qty"]); cut = min(q, need); r["Qty"] = int(round(q - cut)); need -= cut
        return amt - need
    unservable = defaultdict(float)
    for s in skus:
        queue = deque()
        op = float(opening.get(s, 0.0))
        if op > 0:
            queue.append([-10**9, op])                    # opening: fresh, never ages
        for d in range(n_days):
            add = qsum(inflow_at, (s, d))
            if add > 0:
                queue.append([d, add])
            need = qsum(outflow_at, (s, d))
            while need > 1e-9 and queue:
                if queue[0][1] <= need + 1e-9:
                    need -= queue[0][1]; queue.popleft()
                else:
                    queue[0][1] -= need; need = 0.0
            if need > 1e-9:
                unservable[(s, d)] += need                # consume with no non-aged stock -> invalid
            while queue and queue[0][0] >= 0 and (d - queue[0][0]) > limit_days:
                a = queue.popleft()
                qreduce(inflow_at, (s, a[0]), a[1])       # end-of-day writeoff of aged built stock
    return unservable


def _apply_aging(mi, building_rows, curing_rows):
    """Enforce GT 3-day + carcass 1-day aging (rule 9) by WRITING OFF aged-UNCONSUMED built
    stock, EXACTLY matching validate_schedule.py's day-level FIFO (end-of-day write-off). GT
    built but not cured within 3 days, and carcass built but not consumed by Stage-2 within 1
    day, are surplus that expires — removed from the BUILDING rows. This is KPI-NEUTRAL: the
    removed stock was never cured/consumed, so curing is untouched (cured unchanged), and it only
    trims wasted GT/carcass the plant would not build. Curing rows are NOT modified (so mould
    feasibility + cured are preserved). Mutates building_rows Qty in place. Returns
    (gt_written_off, carc_written_off) — reported, NOT subtracted from cured."""
    from collections import deque
    try:
        from b2c_pipeline import _MACHINE_GROUP
    except Exception:
        _MACHINE_GROUP = {}
    _S1 = {m for m, g in _MACHINE_GROUP.items() if g == "STAGE1"}
    _S2 = {m for m, g in _MACHINE_GROUP.items() if g == "STAGE2"}
    _SORD = {"A": 0, "B": 1, "C": 2}

    # inflow BUILDING rows (to write off) + outflow CONSUMPTION sums, per (sku, day, shift) —
    # keyed EXACTLY like validate_schedule.py (per-date, per-shift) so the write-off FIFO is
    # byte-identical to the validator's _fifo_age_check and leaves ZERO residual.
    gt_rows = defaultdict(list); carc_rows = defaultdict(list)
    cured_out = defaultdict(float); s2_out = defaultdict(float)
    for r in building_rows:
        if float(r["Qty"]) <= 0:
            continue
        m = str(r["Machine"]); dss = (str(r["SKUCode"]), int(r["Day"]), str(r["Shift"]))
        if m in _S1:
            carc_rows[dss].append(r)
        else:
            gt_rows[dss].append(r)
            if m in _S2:
                s2_out[dss] += float(r["Qty"])           # Stage-2 GT consumes carcass
    for r in curing_rows:
        if float(r["Qty"]) > 0:
            cured_out[(str(r["SKUCode"]), int(r["Day"]), str(r["Shift"]))] += float(r["Qty"])

    def _writeoff(inflow_rows, outflow_sum, opening, limit_days, skus):
        # Per-shift FIFO, aging measured in CALENDAR days (the day number, NOT an enumeration of
        # the sparse active-day set — the latter compresses gaps and is unstable for sparsely-built
        # carcass). Walk ALL calendar days min..max so a gap correctly ages stock. End-of-day
        # write-off of stock older than limit_days; reduce the exact building rows.
        keydays = {k[1] for k in inflow_rows} | {k[1] for k in outflow_sum}
        if not keydays:
            return 0.0
        dmin, dmax = min(keydays), max(keydays)
        total = 0.0
        for s in skus:
            q = deque()                                  # items: [day_number, qty, day, shift]
            op = float(opening.get(s, 0.0))
            if op > 0:
                q.append([-10**9, op, None, None])       # opening: fresh (never ages)
            for d in range(dmin, dmax + 1):
                for sh in ("A", "B", "C"):
                    add = sum(float(x["Qty"]) for x in inflow_rows.get((s, d, sh), []))
                    if add > 0:
                        q.append([d, add, d, sh])
                    take = outflow_sum.get((s, d, sh), 0.0)
                    while take > 1e-9 and q:
                        if q[0][1] <= take + 1e-9:
                            take -= q[0][1]; q.popleft()
                        else:
                            q[0][1] -= take; take = 0.0
                while q and q[0][0] > -10**8 and (d - q[0][0]) > limit_days:
                    aged = q.popleft()                   # end-of-day write-off of aged surplus
                    rem = aged[1]
                    for r in inflow_rows.get((s, aged[2], aged[3]), []):
                        if rem <= 1e-9:
                            break
                        qv = float(r["Qty"]); cut = min(qv, rem)
                        r["Qty"] = int(round(qv - cut)); rem -= cut; total += cut
        return total

    gt_skus = {k[0] for k in gt_rows} | {k[0] for k in cured_out} | set(mi.opening_gt)
    # iterate each write-off to a fixed point (removing aged stock reshuffles the FIFO and can
    # expose a little more aged stock the next pass; converges in a few rounds).
    gt_wo = 0.0
    for _ in range(8):
        w = _writeoff(gt_rows, cured_out, mi.opening_gt, int(os.environ.get("OPT_GT_SHELF_DAYS", "3")), gt_skus)
        gt_wo += w
        if w <= 0.5:
            break
    # Carcass: when the JIT reschedule owns carcass, it is already 1-day-fresh by construction —
    # writing it off here would strip carcass Stage-2 needs and break rules 9C/5. Skip it.
    carc_wo = 0.0
    if not _CARC_RESCHED:
        carc_skus = {k[0] for k in carc_rows} | {k[0] for k in s2_out} | set(mi.opening_carcass)
        for _ in range(8):
            w = _writeoff(carc_rows, s2_out, mi.opening_carcass, int(os.environ.get("OPT_CARC_SHELF_DAYS", "1")), carc_skus)
            carc_wo += w
            if w <= 0.5:
                break
    return (int(gt_wo), int(carc_wo))


def _same_co_trim(mi, building_rows, curing_rows):
    """Post-process SAME-size building CO capacity charge (rules 15/7).

    The CP-SAT model charges DIFF-inch building CO capacity but NOT same-inch SKU changes
    (per-SKU continuation binaries wreck convergence, measured -96k). We deduct the same-size
    CO here: per machine, walk its producing shifts; a same-size CO occurs when the SKU changes
    vs the previous producing shift with the SAME inch. If that shift's production (Qty*CT/60)
    plus the group's same-size CO minutes exceeds 480, TRIM the built Qty so Prod_Mins+CO==480.
    The GT cut cascades to curing: cured cannot exceed opening + built, so the part of the trim
    not absorbed by a SKU's closing (never-cured) GT buffer reduces cured (clipped latest-first).
    Mutates building_rows + curing_rows in place. Returns (n_trimmed_shifts, gt_units_cut,
    cured_dropped). After trimming, each such shift's production exactly fills (480 - CO_Mins),
    so the writer's CO-block + production wall-clock is consistent."""
    try:
        from b2c_pipeline import _bld_ct_sec, _MACHINE_GROUP
        import bc_config as bc
    except Exception:
        return (0, 0, 0)
    CO_SAME = getattr(bc, "BUILDING_CO_SAME_SIZE", {})
    _S1 = {m for m, g in _MACHINE_GROUP.items() if g == "STAGE1"}
    _SORD = {"A": 0, "B": 1, "C": 2}

    # pre-trim per-SKU built (GT only) + cured, for the cascade's closing-GT buffer
    built_old = defaultdict(float); cured_old = defaultdict(float)
    for r in building_rows:
        if str(r["Machine"]) not in _S1 and float(r["Qty"]) > 0:
            built_old[str(r["SKUCode"])] += float(r["Qty"])
    for r in curing_rows:
        if float(r["Qty"]) > 0:
            cured_old[str(r["SKUCode"])] += float(r["Qty"])

    _REDIST = os.environ.get("OPT_SAME_CO_REDIST", "1") != "0"   # redistribute vs cut

    # index building production rows by (machine, day, shift)
    bmap = defaultdict(list)
    for r in building_rows:
        if float(r["Qty"]) > 0:
            bmap[(str(r["Machine"]), int(r["Day"]), str(r["Shift"]))].append(r)

    gt_trim = defaultdict(float)             # per-SKU units that end up CUT (cascade to cured)
    excess_ms = defaultdict(float)           # (machine,sku) -> over-packed units to re-place
    fill_slots = defaultdict(list)           # (machine,sku) -> [(rows, spare_units, ct, day)] continuation shifts with room
    n_trim = 0
    for m in sorted({k[0] for k in bmap}):
        co_same = CO_SAME.get(_MACHINE_GROUP.get(m, "?"), 0)
        if not co_same:
            continue
        shifts = sorted({(k[1], _SORD.get(k[2], 0), k[2]) for k in bmap if k[0] == m})
        prev_sku = prev_inch = None
        for (d, _o, sh) in shifts:
            rows = bmap[(m, d, sh)]
            sku = str(rows[0]["SKUCode"]); inch = str(rows[0].get("Inch", ""))
            qty = sum(float(x["Qty"]) for x in rows)
            ct = _bld_ct_sec(m, sku)
            same_co = sku != prev_sku and prev_sku is not None and inch == prev_inch
            continuation = (prev_sku is not None and sku == prev_sku)
            prev_sku, prev_inch = sku, inch
            if ct <= 0:
                continue
            if same_co:
                prod_min = qty * ct / 60.0
                if prod_min + co_same <= 480 + 1e-6:
                    continue
                new_qty = int((480 - co_same) * 60.0 / ct)
                trimmed = qty - new_qty
                if trimmed <= 0:
                    continue
                n_trim += 1
                scale = (new_qty / qty) if qty > 0 else 0.0
                for x in rows:
                    x["Qty"] = int(round(float(x["Qty"]) * scale))
                if m not in _S1:
                    excess_ms[(m, sku)] += trimmed
            elif _REDIST and continuation and m not in _S1:
                # a pure continuation shift (no CO) can hold up to a full 480-min of this SKU;
                # its spare capacity is a re-placement target for same-machine same-SKU excess.
                cap_units = int(480 * 60.0 / ct)
                spare = cap_units - qty
                if spare > 0:
                    fill_slots[(m, sku)].append((rows, spare, ct, d))

    # ── REDISTRIBUTE: move over-packed GT into the same machine's same-SKU spare shifts
    #    (keeps built[s] whole -> no cured drop). Only the un-placeable remainder is CUT.
    #    Clamped to each SKU's demand headroom so re-placement never overbuilds (rule 8B). ──
    built_now = defaultdict(float)
    for r in building_rows:
        if str(r["Machine"]) not in _S1 and float(r["Qty"]) > 0:
            built_now[str(r["SKUCode"])] += float(r["Qty"])
    redistributed = 0
    for (m, sku), exc in excess_ms.items():
        remaining = exc
        if _REDIST:
            for (rows, spare, ct, d) in sorted(fill_slots.get((m, sku), []), key=lambda z: z[3]):
                if remaining <= 0.5:
                    break
                head = float(mi.demand.get(sku, 0)) - built_now[sku]   # demand headroom (no overbuild)
                add = min(spare, remaining, head)
                if add <= 0.5:
                    continue
                cur = sum(float(x["Qty"]) for x in rows)
                scale = (cur + add) / cur if cur > 0 else 0.0
                new_tot = 0.0
                for x in rows:
                    x["Qty"] = int(round(float(x["Qty"]) * scale)); new_tot += float(x["Qty"])
                placed = new_tot - cur
                remaining -= placed; redistributed += placed; built_now[sku] += placed
        if remaining > 0.5:
            gt_trim[sku] += remaining

    if not gt_trim:
        return (n_trim, 0, 0, int(redistributed))

    # cascade to curing: cured <= opening + built_new per SKU (drop beyond the closing buffer)
    dropped = 0
    rows_by_sku = defaultdict(list)
    for r in curing_rows:
        if float(r["Qty"]) > 0:
            rows_by_sku[str(r["SKUCode"])].append(r)
    for sku, trim in gt_trim.items():
        opening = float(mi.opening_gt.get(sku, 0.0))
        closing = opening + built_old.get(sku, 0.0) - cured_old.get(sku, 0.0)  # never-cured GT buffer
        need = max(0.0, trim - max(0.0, closing))
        for r in sorted(rows_by_sku.get(sku, []),
                        key=lambda r: (int(r["Day"]), _SORD.get(r["Shift"], 0)), reverse=True):
            if need <= 0:
                break
            q = float(r["Qty"]); cut = min(q, need)
            r["Qty"] = int(q - cut); need -= cut; dropped += cut

    return (n_trim, int(sum(gt_trim.values())), int(dropped), int(redistributed))


def _apply_mould_cleans(mi, curing_rows):
    """Fix B — POST-PROCESS mould life over the month-global curing plan.

    The optimizer never models mould life, so its throughput is optimistic by the
    ~50-65 clean-shifts/month the greedy pays. We walk each press's shifts in order,
    seeding remaining life from the DB (`mi.mould_life`, the min over the press's 2
    moulds), decrement by cycles produced (qty // CURING_CAVITIES) each running shift,
    and when life hits <= 0 fire an 8h (480-min = one shift) clean: that shift is IDLED
    (its cured is DROPPED -> realized cured falls, honest KPI haircut), then life resets
    to MOULD_CLEAN_CYCLES. A curing CO (SKU change) also resets life to full (its 480-min
    CO includes a clean) — no separate clean on a CO shift.

    Mutates curing_rows in place (sets a clean shift's Qty to 0). Returns
    (clean_events, final_life_by_press, total_cured_dropped).
    """
    _SORD = {"A": 0, "B": 1, "C": 2}
    rows = sorted(curing_rows, key=lambda r: (int(r["Day"]), _SORD.get(r["Shift"], 0),
                                              str(r["Press"])))
    life: dict = {}                     # press -> remaining cycles
    last_sku: dict = {}                 # press -> last SKU seen (CO detection)
    events: list = []
    dropped = 0
    cav = max(1, int(_CURING_CAVITIES))

    def _life0(p):
        v = mi.mould_life.get(p)
        try:
            v = int(v)
        except (TypeError, ValueError):
            v = _MOULD_CLEAN_CYCLES
        return v if v > 0 else _MOULD_CLEAN_CYCLES

    for r in rows:
        p = str(r["Press"]); sku = str(r["SKUCode"]); qty = int(r["Qty"])
        if p not in life:
            life[p] = _life0(p)
        # A curing CO (SKU change) resets mould life; a Qty=0 CO row also flags the change.
        if last_sku.get(p) is not None and last_sku[p] != sku:
            life[p] = _MOULD_CLEAN_CYCLES
        last_sku[p] = sku
        if qty <= 0:                    # CO / idle row -> no production, no life burn
            continue
        if life[p] <= 0:                # life exhausted -> clean this shift (idle it)
            dropped += qty
            r["Qty"] = 0
            events.append({
                "Date": r["Date"], "Day": int(r["Day"]), "Shift": r["Shift"],
                "Press": p, "From_SKU": sku, "Target_SKU": sku,
                "CO_Type": "Mould Clean", "Mins": float(bc_config.MOULD_CLEAN_MINS),
            })
            life[p] = _MOULD_CLEAN_CYCLES
        else:
            life[p] -= qty // cav       # burn cycles for the units produced
    return events, {p: max(0, v) for p, v in life.items()}, dropped


if __name__ == "__main__":
    import bc_config

    mi = load_model_inputs()
    print("ModelInputs:", mi.summary())
    print(f"Month: plan_start={mi.plan_start} planning_days={mi.planning_days} "
          f"total_demand={sum(mi.demand.values()):,}")
    print(f"Rolling driver: commit={COMMIT_DAYS}d window<=%d d det_time=90s workers=8 seed=1"
          % MAX_WINDOW_DAYS)
    print("-" * 78)

    res = run_rolling(mi, det_time_s=90.0, workers=8, seed=1, verbose=True)

    print("-" * 78)
    print(f"FULL MONTH: cured={res['total_cured']:,} / demand={res['total_demand']:,} "
          f"= coverage {res['coverage'] * 100:.2f}%")
    print(f"           curing COs={res['total_co']:,}  total wall={res['total_wall_s']:,}s "
          f"({res['total_wall_s'] / 60:.1f} min)  windows={len(res['windows'])}")

    # Write the committed month plan to inspectable Excel in main_output/.
    from optimizer.output import write_schedules
    paths = write_schedules(res, mi, out_dir="main_output")
    print("-" * 78)
    print(f"Building schedule: {paths['building']}  ({paths['n_building_rows']:,} rows)")
    print(f"Curing schedule:   {paths['curing']}  ({paths['n_curing_rows']:,} rows)")
