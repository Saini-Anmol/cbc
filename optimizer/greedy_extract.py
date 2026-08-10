"""optimizer/greedy_extract.py

DECISIVE TEST: does the CP-SAT model REACH the greedy engine's throughput when it
is warm-started with the greedy's OWN plan?

Procedure (AUGUST, days 1-10, shifts t=0..29):
  1. Run the real greedy (`b2c_pipeline.run_rolling_pipeline`) for August, writing its
     building + curing Shift-Schedule sheets.
  2. Extract from those sheets:
       g_hint[(machine, sku, t)] = GT units an S2/UNI machine built for sku in shift t
       n_hint[(sku, t)]          = # presses curing sku in shift t
     mapped to the model's (mi.machines, mi.skus), t = (day-1)*3 + {A:0,B:1,C:2}.
  3. Feed the greedy plan as the CP-SAT hint and solve the same 10-day window.
  4. Compare optimizer_cured (warm-started) vs the greedy's OWN cured over days 1-10.
  5. Cross-check the greedy plan against every model cap → A (under-solving) vs
     B (model-conservatism, name the binding constraint).

Run:  myenv/bin/python -m optimizer.greedy_extract
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime

# ---- month context MUST be set before bc_config import (imported by value) ----
os.environ.setdefault("PLAN_MONTH", "2026-08")
os.environ.setdefault("RUNNING_MOULDS_MONTH", "2026-08")

N_DAYS = 10
W = N_DAYS * 3
SHIFT_IDX = {"A": 0, "B": 1, "C": 2}
_SENTINEL = {"CHANGEOVER", "MOULD CLEAN", "MOULD_CLEAN", ""}


def _t_of(date_str: str, shift: str, plan_start) -> int | None:
    """(date_str 'YYYY-MM-DD', shift 'A/B/C') -> model shift index, or None if outside window."""
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
    except Exception:
        return None
    day = (d.date() - plan_start.date()).days + 1          # day1 = plan_start
    si = SHIFT_IDX.get(str(shift).strip().upper())
    if si is None or day < 1:
        return None
    t = (day - 1) * 3 + si
    return t if 0 <= t < W else None


def main() -> None:
    import cbc_env
    import bc_config as bc

    bc.DEMAND_FILE = os.path.join(cbc_env.INPUT_DIR, "august_demand_tomerji.xlsx")
    bc.PLAN_START = datetime(2026, 8, 1, 7, 0, 0)
    bc.PLANNING_DAYS = 31
    bc.RUNNING_MOULDS_TABLE = "Daily_Running_Moulds"
    bc.MAX_CHANGEOVERS_PER_DAY = int(os.environ.get("MAX_CO", "12"))
    plan_start = bc.PLAN_START

    import pandas as pd
    from b2c_pipeline import run_rolling_pipeline

    wd = os.environ.get("GX_OUTDIR") or tempfile.mkdtemp()
    os.makedirs(wd, exist_ok=True)
    bout = os.path.join(wd, "gx_bld.xlsx")
    cout = os.path.join(wd, "gx_cure.xlsx")

    print("=" * 78)
    print("STEP 1 — run the REAL greedy for August (full month), capture Shift Schedules")
    print("=" * 78)
    if os.environ.get("GX_REUSE") and os.path.exists(bout) and os.path.exists(cout):
        print(f"  [reuse] using existing greedy sheets in {wd}")
    else:
        res = run_rolling_pipeline(
            demand_path=bc.DEMAND_FILE, plan_start=plan_start, planning_days=31,
            build_output=bout, curing_output=cout,
        )
        print(f"\n  greedy month: built={res['total_built']:,.0f} cured={res['total_cured']:,.0f} "
              f"coverage={res['demand_coverage']:.2f}% n_co={res['n_co']}")

    # -------- read the greedy Shift Schedules --------
    # building sheet: title row1, blank row2, header row3 -> skiprows=2
    bdf = pd.read_excel(bout, sheet_name="Shift Schedule", skiprows=2)
    cdf = pd.read_excel(cout, sheet_name="Shift Schedule")            # header row1
    bdf.columns = [str(c).strip() for c in bdf.columns]
    cdf.columns = [str(c).strip() for c in cdf.columns]

    print("=" * 78)
    print("STEP 2 — load model inputs + extract greedy plan for days 1-10 (t=0..29)")
    print("=" * 78)
    from optimizer.data import load_model_inputs
    from optimizer.model import build_and_solve
    mi = load_model_inputs()
    print("  ModelInputs:", mi.summary())

    demand_set = set(mi.skus)
    GT_GROUPS = {"S2", "UNI"}

    # ---------- g_hint from building production rows (S2/UNI GT only) ----------
    g_hint: dict = {}
    g_excluded_inch = 0        # greedy built a (machine,sku) the model's inch-lock forbids
    g_excluded_norate = 0      # greedy built a (machine,sku) NOT in mi.build_rate
    g_over_rate = 0            # greedy shift-qty exceeded model build_rate for that pair
    g_stage1_or_unknown = 0
    bcols = list(bdf.columns)
    for row in bdf.itertuples(index=False, name=None):
        rd = dict(zip(bcols, row))
        if str(rd.get("CO_Type", "")).strip() != "production":
            continue
        mac = str(rd.get("Machine", "")).strip()
        sku = str(rd.get("SKUCode", "")).strip()
        try:
            qty = int(round(float(rd.get("Qty", 0) or 0)))
        except Exception:
            qty = 0
        if qty <= 0 or sku not in demand_set:
            continue
        grp = mi.machine_group.get(mac)
        if grp not in GT_GROUPS:               # Stage-1 carcass / unknown machine -> not a GT hint
            g_stage1_or_unknown += qty
            continue
        t = _t_of(rd.get("Date"), rd.get("Shift"), plan_start)
        if t is None:
            continue
        # classify against the model's building admissibility
        if (mac, sku) not in mi.build_rate:
            g_excluded_norate += qty
            continue
        inns = mi.machine_allowed_inches.get(mac, set())
        if inns and mi.sku_inch.get(sku, "") not in inns:
            g_excluded_inch += qty            # model DROPS this pair from bld_pairs (inch-lock)
            continue
        g_hint[(mac, sku, t)] = g_hint.get((mac, sku, t), 0) + qty

    # over-build-rate check (per shift-pair, after aggregation)
    for (mac, sku, t), q in g_hint.items():
        if q > mi.build_rate.get((mac, sku), 0):
            g_over_rate += q - mi.build_rate[(mac, sku)]

    # ---------- n_hint from curing RUNNING rows ----------
    # A press "cures sku in shift t" = a production (non-CO, non-clean) row for that sku.
    presses_on: dict = {}      # (sku,t) -> set(press)
    greedy_cured = 0
    ccols = list(cdf.columns)
    for row in cdf.itertuples(index=False, name=None):
        rd = dict(zip(ccols, row))
        sku = str(rd.get("SKUCode", "")).strip()
        if sku.upper() in _SENTINEL:
            continue
        co_m = float(rd.get("CO_Mins", 0) or 0)
        cl_m = float(rd.get("Mould_Clean_Mins", 0) or 0)
        if co_m > 0 or cl_m > 0:               # changeover / clean segment -> not producing
            continue
        t = _t_of(rd.get("Date"), rd.get("Shift"), plan_start)
        if t is None:
            continue
        press = str(rd.get("Machine", "")).strip()
        try:
            qty = int(round(float(rd.get("Qty", 0) or 0)))
        except Exception:
            qty = 0
        greedy_cured += qty
        presses_on.setdefault((sku, t), set()).add(press)

    n_hint = {k: len(v) for k, v in presses_on.items()}
    # restrict n_hint to SKUs the model actually cures (else the hint key is ignored anyway)
    cure_skus_model = {s for s in mi.skus
                       if mi.mould_pairs.get(s, 0) >= 1 and mi.cure_rate.get(s, 0) > 0
                       and sum(1 for p in mi.presses if s in mi.press_allowed_skus.get(p, ())) > 0}
    n_hint_model = {(s, t): c for (s, t), c in n_hint.items() if s in cure_skus_model}
    n_dropped_skus = sorted({s for (s, t) in n_hint if s not in cure_skus_model})

    print(f"  g_hint entries (in-model bld pairs) : {len(g_hint):,}")
    print(f"  n_hint entries (all cured skus)     : {len(n_hint):,}  "
          f"(model-cured only: {len(n_hint_model):,})")
    print(f"  greedy cured days 1-10              : {greedy_cured:,}")
    if g_stage1_or_unknown:
        print(f"  [info] greedy production on Stage-1/unknown machines (not GT hint): "
              f"{g_stage1_or_unknown:,} units")

    # ============ STEP 2b — cross-check greedy plan against every model cap ============
    print("=" * 78)
    print("STEP 2b — is the greedy plan FEASIBLE in the model? (cap-by-cap)")
    print("=" * 78)
    N_PRESS = len(mi.presses)
    elig_cnt = {s: sum(1 for p in mi.presses if s in mi.press_allowed_skus.get(p, ()))
                for s in mi.skus}

    # (i) per-SKU mould-pair cap + press-eligibility cap: n[s,t] <= min(mould_pairs, elig)
    mould_viol = []
    for (s, t), c in n_hint_model.items():
        cap_s = min(mi.mould_pairs.get(s, 0), elig_cnt.get(s, 0))
        if c > cap_s:
            mould_viol.append((s, t, c, cap_s))
    # (ii) 170 presses per shift
    per_shift = {}
    for (s, t), c in n_hint_model.items():
        per_shift[t] = per_shift.get(t, 0) + c
    max_shift_presses = max(per_shift.values()) if per_shift else 0
    # (iii) shared-mould component cap (replicate model union-find)
    parent = {s: s for s in cure_skus_model}
    def _find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a
    owner = {}
    for s in cure_skus_model:
        for md in mi.sku_moulds.get(s, ()):
            if md in owner:
                ra, rb = _find(s), _find(owner[md])
                if ra != rb:
                    parent[ra] = rb
            else:
                owner[md] = s
    comps = {}
    for s in cure_skus_model:
        comps.setdefault(_find(s), []).append(s)
    comp_caps = {}
    for root, comp in comps.items():
        if len(comp) <= 1:
            continue
        um = set()
        for s in comp:
            um.update(mi.sku_moulds.get(s, ()))
        comp_caps[root] = (comp, len(um) // 2)
    shared_viol = []
    for root, (comp, cap) in comp_caps.items():
        cset = set(comp)
        for t in range(W):
            tot = sum(c for (s, tt), c in n_hint_model.items() if tt == t and s in cset)
            if tot > cap:
                shared_viol.append((root, t, tot, cap, len(comp)))

    print(f"  (i)   per-SKU mould-pair cap  : {'VIOLATED' if mould_viol else 'OK'} "
          f"({len(mould_viol)} (sku,shift) over cap)")
    if mould_viol[:5]:
        for s, t, c, cap in mould_viol[:5]:
            print(f"          {s} @t{t}: greedy n={c} > cap {cap} "
                  f"(mould_pairs={mi.mould_pairs.get(s,0)}, elig={elig_cnt.get(s,0)})")
    print(f"  (ii)  <=170 presses/shift     : {'VIOLATED' if max_shift_presses > N_PRESS else 'OK'} "
          f"(max in a shift = {max_shift_presses})")
    print(f"  (iii) shared-mould comp cap   : {'VIOLATED' if shared_viol else 'OK'} "
          f"({len(shared_viol)} (component,shift) over cap)")
    if shared_viol[:5]:
        for root, t, tot, cap, nsk in shared_viol[:5]:
            print(f"          component<{nsk} skus> @t{t}: sum n={tot} > cap {cap}")
    print(f"  (iv)  inch-lock on building   : {'VIOLATED' if g_excluded_inch else 'OK'} "
          f"({g_excluded_inch:,} greedy GT units on machine/sku the model's inch-lock forbids)")
    print(f"  (v)   pair in mi.build_rate   : {'VIOLATED' if g_excluded_norate else 'OK'} "
          f"({g_excluded_norate:,} greedy GT units on a (machine,sku) not in build_rate)")
    print(f"  (vi)  build_rate/shift cap    : {'VIOLATED' if g_over_rate else 'OK'} "
          f"({g_over_rate:,} GT units above per-shift build_rate)")
    if n_dropped_skus:
        print(f"  [info] {len(n_dropped_skus)} cured SKUs are NOT in the model's cure set "
              f"(mould_pairs<1 / no cure_rate / no eligible press) — model can't cure them at all")

    # ============ STEP 3 — solve the window WITH the greedy warm-start ============
    print("=" * 78)
    print("STEP 3 — CP-SAT solve of days 1-10 warm-started with the greedy plan")
    print("=" * 78)
    hint = {"n": n_hint_model, "g": g_hint}
    sol = build_and_solve(
        mi, day_start=1, n_days=N_DAYS,
        hint=hint, workers=8, det_time_s=120, seed=1, log=True,
    )
    opt_cured = sol.get("cured", 0)
    print(f"\n  optimizer status={sol.get('status')} cured={opt_cured:,} "
          f"n_co={sol.get('n_co')} gap={sol.get('gap')} wall={sol.get('wall_s')}s "
          f"bld_pairs={sol.get('n_bld_pairs')} cure_skus={sol.get('n_cure_skus')}")

    # ---- also solve WITHOUT any warm-start (all-zero) as the baseline reference ----
    print("\n  [reference] solving the SAME window with NO warm-start (all-zero hint)...")
    sol0 = build_and_solve(mi, day_start=1, n_days=N_DAYS,
                           hint=None, workers=8, det_time_s=120, seed=1, log=False)
    cold_cured = sol0.get("cured", 0)

    # ============ VERDICT ============
    print("=" * 78)
    print("DECISIVE NUMBERS")
    print("=" * 78)
    print(f"  greedy cured (days 1-10)                 : {greedy_cured:,}")
    print(f"  optimizer cured (GREEDY warm-start)      : {opt_cured:,}")
    print(f"  optimizer cured (NO warm-start, cold)    : {cold_cured:,}")
    print(f"  optimizer >= greedy ?                    : {opt_cured >= greedy_cured}  "
          f"(diff {opt_cured - greedy_cured:+,})")
    any_infeas = bool(mould_viol or shared_viol or g_excluded_inch
                      or g_excluded_norate or g_over_rate or max_shift_presses > N_PRESS)
    print("\n  Greedy plan vs model caps:", "ALL FEASIBLE" if not any_infeas
          else "INFEASIBLE in the model — binding constraint(s) below")
    if any_infeas:
        if mould_viol:      print(f"    - per-SKU mould-pair cap ({len(mould_viol)} sku-shifts over)")
        if shared_viol:     print(f"    - shared-mould component cap ({len(shared_viol)} comp-shifts over)")
        if g_excluded_inch: print(f"    - historical inch-lock ({g_excluded_inch:,} GT units on forbidden machine/sku)")
        if g_excluded_norate: print(f"    - (machine,sku) not in build_rate ({g_excluded_norate:,} units)")
        if g_over_rate:     print(f"    - per-shift build_rate ({g_over_rate:,} units over)")
        if max_shift_presses > N_PRESS: print(f"    - 170-press cap (max {max_shift_presses})")

    print()
    if opt_cured >= greedy_cured:
        print("  VERDICT: (A) UNDER-SOLVING — with the greedy plan as a start the optimizer")
        print("           reaches/beats greedy; the model CAN represent the greedy plan, the")
        print("           solver just needed a better incumbent.")
    else:
        if any_infeas:
            print("  VERDICT: (B) MODEL-CONSERVATISM — the greedy's real plan is INFEASIBLE in")
            print("           the CP-SAT model (see binding constraint(s) above); the model cannot")
            print("           represent what the greedy actually does, so it lands below greedy.")
        else:
            print("  VERDICT: (A/under-solving flavour) — greedy plan is feasible in the model")
            print("           yet the warm-started solve still fell short of greedy within the time")
            print("           budget: the solver did not fully exploit the hint (needs more time /")
            print("           better search), NOT a model-representation gap.")
    print("=" * 78)


if __name__ == "__main__":
    main()
