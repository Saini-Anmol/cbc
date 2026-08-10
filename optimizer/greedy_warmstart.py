"""optimizer/greedy_warmstart.py

REAL-GREEDY per-window warm-start for the rolling CP-SAT driver.

The simple constructive `optimizer.warmstart.greedy_hint` gives CP-SAT a feasible
incumbent, but it is a WEAK plan (unpaced) so each window under-solves and the month
lands ~2% below the actual greedy engine. `optimizer.greedy_extract` proved that
warm-starting a SINGLE window with the ACTUAL greedy engine's own plan reaches/beats
greedy. This module GENERALISES that: run the real greedy ONCE for the whole month,
extract its per-shift plan into a MONTH-GLOBAL form, then let the driver SLICE that
plan into each window's hint.

Public API:
  * extract_month_greedy(mi) -> (greedy_n, greedy_g, meta)
      greedy_n: {(sku, gt): press_count}   over the whole horizon, gt = (day-1)*3 + shift
      greedy_g: {(machine, sku, gt): units} for S2/UNI GT builds
      meta:     {'cured', 'built', 'coverage', 'n_co'} of the greedy run (for reference)
      Cached to disk; env GX_REUSE=1 reuses the greedy Shift-Schedule sheets / month-plan
      cache so the (slow) greedy engine only runs once per session.
  * window_hint(mi, greedy_n, greedy_g, day_start, n_days) -> {'n':.., 'g':..}
      Slice the month-global plan for a driver window and DROP any (sku)/(machine,sku)
      not representable in the model (cure_skus / bld_pairs, honouring INCH_RELAX).

The extraction mirrors optimizer.greedy_extract exactly (same sheet parsing, same
model-admissibility filters) but generalised from days 1-10 to all `planning_days`.
"""
from __future__ import annotations

import os
import pickle
import tempfile
from datetime import datetime

SHIFT_IDX = {"A": 0, "B": 1, "C": 2}
_SENTINEL = {"CHANGEOVER", "MOULD CLEAN", "MOULD_CLEAN", ""}
GT_GROUPS = {"S2", "UNI"}


def _gt_of(date_str, shift, plan_start, planning_days) -> int | None:
    """(date 'YYYY-MM-DD', shift 'A/B/C') -> month-global shift index gt, or None if outside."""
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
    except Exception:
        return None
    day = (d.date() - plan_start.date()).days + 1          # day1 = plan_start
    si = SHIFT_IDX.get(str(shift).strip().upper())
    if si is None or day < 1:
        return None
    gt = (day - 1) * 3 + si
    return gt if 0 <= gt < planning_days * 3 else None


# --------------------------------------------------------------------------- #
# model-admissibility helpers (replicate model.py's bld_pairs / cure_skus)     #
# --------------------------------------------------------------------------- #
def _inch_relax() -> bool:
    return os.environ.get("INCH_RELAX", "0") == "1"


def model_bld_pairs(mi) -> set:
    """The exact (machine,sku) set the model instantiates as GT-build vars (honours INCH_RELAX)."""
    relax = _inch_relax()
    pairs = set()
    for mac in mi.machines:
        if mi.machine_group.get(mac) not in GT_GROUPS:
            continue
        inns = mi.machine_allowed_inches.get(mac, set())
        for s in mi.machine_allowed_skus.get(mac, ()):
            if mi.build_rate.get((mac, s), 0) <= 0:
                continue
            if inns and not relax and mi.sku_inch.get(s, "") not in inns:
                continue
            pairs.add((mac, s))
    return pairs


def model_cure_skus(mi) -> set:
    """The exact SKU set the model instantiates curing count vars for."""
    elig_cnt = {s: sum(1 for p in mi.presses if s in mi.press_allowed_skus.get(p, ()))
                for s in mi.skus}
    return {s for s in mi.skus
            if mi.mould_pairs.get(s, 0) >= 1 and mi.cure_rate.get(s, 0) > 0 and elig_cnt[s] > 0}


# --------------------------------------------------------------------------- #
# STEP 1 — run the greedy once, extract the month-global plan                  #
# --------------------------------------------------------------------------- #
def _cache_dir() -> str:
    wd = os.environ.get("GX_OUTDIR") or os.path.join(tempfile.gettempdir(), "gx_greedy_cache")
    os.makedirs(wd, exist_ok=True)
    return wd


def _greedy_sheets(mi):
    """Run the real greedy engine for the whole month, return (bld_xlsx, cure_xlsx) paths.

    Reuses the sheets on disk when GX_REUSE=1 so the slow engine only runs once/session.
    """
    import bc_config as bc
    wd = _cache_dir()
    bout = os.path.join(wd, "gx_bld.xlsx")
    cout = os.path.join(wd, "gx_cure.xlsx")

    if os.environ.get("GX_REUSE") and os.path.exists(bout) and os.path.exists(cout):
        print(f"  [greedy_warmstart] reuse greedy sheets in {wd}")
        return bout, cout

    from b2c_pipeline import run_rolling_pipeline
    print(f"  [greedy_warmstart] running REAL greedy for {int(mi.planning_days)} days "
          f"(plan_start={mi.plan_start}) ...")
    res = run_rolling_pipeline(
        demand_path=bc.DEMAND_FILE, plan_start=mi.plan_start, planning_days=int(mi.planning_days),
        build_output=bout, curing_output=cout,
    )
    print(f"  [greedy_warmstart] greedy month: built={res['total_built']:,.0f} "
          f"cured={res['total_cured']:,.0f} coverage={res['demand_coverage']:.2f}% n_co={res['n_co']}")
    with open(os.path.join(wd, "gx_meta.pkl"), "wb") as fh:
        pickle.dump({"built": res["total_built"], "cured": res["total_cured"],
                     "coverage": res["demand_coverage"], "n_co": res["n_co"]}, fh)
    return bout, cout


def extract_month_greedy(mi):
    """Run/reuse the greedy and return (greedy_n, greedy_g, meta) over the whole horizon.

    The result is cached to `<outdir>/gx_monthplan.pkl`; GX_REUSE=1 reuses it directly
    (no Excel re-parse). Keys are month-global: gt = (day-1)*3 + shift_idx.
    """
    import pandas as pd

    wd = _cache_dir()
    plan_cache = os.path.join(wd, "gx_monthplan.pkl")
    if os.environ.get("GX_REUSE") and os.path.exists(plan_cache):
        with open(plan_cache, "rb") as fh:
            d = pickle.load(fh)
        print(f"  [greedy_warmstart] reuse month-plan cache ({len(d['n']):,} n / "
              f"{len(d['g']):,} g entries)")
        return d["n"], d["g"], d.get("meta", {})

    bout, cout = _greedy_sheets(mi)
    plan_start = mi.plan_start
    pdays = int(mi.planning_days)
    demand_set = set(mi.skus)

    bdf = pd.read_excel(bout, sheet_name="Shift Schedule", skiprows=2)
    cdf = pd.read_excel(cout, sheet_name="Shift Schedule")
    bdf.columns = [str(c).strip() for c in bdf.columns]
    cdf.columns = [str(c).strip() for c in cdf.columns]

    # ---- greedy_g from building production rows (S2/UNI GT only, in build_rate) ----
    greedy_g: dict = {}
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
        if mi.machine_group.get(mac) not in GT_GROUPS:      # Stage-1 carcass / unknown -> not a GT hint
            continue
        if (mac, sku) not in mi.build_rate:                 # pair the model can't represent at all
            continue
        gt = _gt_of(rd.get("Date"), rd.get("Shift"), plan_start, pdays)
        if gt is None:
            continue
        greedy_g[(mac, sku, gt)] = greedy_g.get((mac, sku, gt), 0) + qty
    # clamp to the model's per-shift build_rate cap (a shift can't exceed it in the model)
    for k in list(greedy_g):
        mac, sku, _ = k
        cap = mi.build_rate.get((mac, sku), 0)
        if greedy_g[k] > cap:
            greedy_g[k] = cap

    # ---- greedy_n from curing production rows (distinct presses per (sku, gt)) ----
    presses_on: dict = {}
    greedy_cured = 0
    ccols = list(cdf.columns)
    for row in cdf.itertuples(index=False, name=None):
        rd = dict(zip(ccols, row))
        sku = str(rd.get("SKUCode", "")).strip()
        if sku.upper() in _SENTINEL:
            continue
        co_m = float(rd.get("CO_Mins", 0) or 0)
        cl_m = float(rd.get("Mould_Clean_Mins", 0) or 0)
        if co_m > 0 or cl_m > 0:                            # changeover / clean -> not producing
            continue
        gt = _gt_of(rd.get("Date"), rd.get("Shift"), plan_start, pdays)
        if gt is None:
            continue
        press = str(rd.get("Machine", "")).strip()
        try:
            greedy_cured += int(round(float(rd.get("Qty", 0) or 0)))
        except Exception:
            pass
        presses_on.setdefault((sku, gt), set()).add(press)
    greedy_n = {k: len(v) for k, v in presses_on.items()}

    meta = {}
    mpk = os.path.join(wd, "gx_meta.pkl")
    if os.path.exists(mpk):
        with open(mpk, "rb") as fh:
            meta = pickle.load(fh)
    meta.setdefault("cured_from_sheet", greedy_cured)

    with open(plan_cache, "wb") as fh:
        pickle.dump({"n": greedy_n, "g": greedy_g, "meta": meta}, fh)
    print(f"  [greedy_warmstart] extracted month plan: {len(greedy_n):,} n-entries, "
          f"{len(greedy_g):,} g-entries (cured-from-sheet {greedy_cured:,})")
    return greedy_n, greedy_g, meta


# --------------------------------------------------------------------------- #
# STEP 2 — slice the month-global plan into one window's hint                   #
# --------------------------------------------------------------------------- #
def window_hint(mi, greedy_n, greedy_g, day_start: int, n_days: int) -> dict:
    """Slice the month-global greedy plan for [day_start, day_start+n_days) into a model hint.

    local_t = 0 .. n_days*3-1  maps to  gt = (day_start-1)*3 + local_t.
    Drops (sku)/(machine,sku) not representable in the model (cure_skus / bld_pairs).
    """
    W = n_days * 3
    base = (day_start - 1) * 3
    bld_pairs = model_bld_pairs(mi)
    cure_skus = model_cure_skus(mi)

    hn: dict = {}
    for local_t in range(W):
        gt = base + local_t
        for s in cure_skus:
            c = greedy_n.get((s, gt), 0)
            if c:
                hn[(s, local_t)] = int(c)
    hg: dict = {}
    for (mac, sku, gt), q in greedy_g.items():
        local_t = gt - base
        if 0 <= local_t < W and (mac, sku) in bld_pairs and q > 0:
            hg[(mac, sku, local_t)] = int(q)
    return {"n": hn, "g": hg}


# --------------------------------------------------------------------------- #
# STEP 2b — SEAM-ALIGNED feasible replay of the greedy plan for one window      #
# --------------------------------------------------------------------------- #
# `window_hint` above slices the greedy MONTH plan by ABSOLUTE global-shift index.
# That plan carries GREEDY's own trajectory (its GT reservoir, press counts, and
# demand consumption evolving over the month). For window W>1 the CP driver enters
# the window with a DIFFERENT init state — init_gt / init_n / init_carc come from the
# PREVIOUS COMMITTED CP-SAT window (which cures less than greedy, so more demand is
# left and the reservoir/press config differ). The sliced hint is therefore an
# assignment that is NOT jointly feasible with the window's init state:
#   * seam press CO: co[s,0] >= n_hint[s,0] - init_n[s]; a big mismatch at t=0 can
#     blow the MAX_CHANGEOVERS_PER_DAY daily curing-CO cap on the seam day;
#   * GT reservoir: cured (from the n-hint) can exceed init_gt + built (the g-hint)
#     -> inv[s,t] would go negative.
# AddHint is advisory so this never causes INFEASIBILITY, but an assignment CP-SAT
# cannot extend to a feasible solution is discarded and MISLEADS the early search
# (measured: it wastes the deterministic-time budget and lands BELOW the cold start).
#
# `window_hint_seeded` fixes the seam by REPLAYING the greedy plan FORWARD through the
# window's ACTUAL init state (init_n, init_gt, window demand), clamping every step to
# exactly the caps model.py enforces (press-count, per-SKU mould pair, shared-mould
# contention, daily curing-CO, GT reservoir >= 0, end-of-day GT cap, sacred demand
# cap). The greedy plan supplies the per-shift TARGET (which SKUs to cure on how many
# presses, greedy_n; which machine/SKU GT to build, greedy_g) — its good pacing — while
# the forward clamp GUARANTEES the emitted (n, g) is a jointly feasible partial
# assignment CP-SAT can extend. Building is topped up need-based so the desired curing
# is actually fed (high implied cured). Result stashed under '_implied_cured' (model.py
# ignores unknown hint keys). Determinism: stable sorts throughout.
def window_hint_seeded(mi, greedy_n, greedy_g, day_start: int, n_days: int,
                       init_n: dict | None = None, init_gt: dict | None = None,
                       demand: dict | None = None, uni_only: bool = False) -> dict:
    W = n_days * 3
    base = (day_start - 1) * 3
    N_PRESS = len(mi.presses)

    cure_skus = sorted(model_cure_skus(mi))
    elig_cnt = {s: sum(1 for p in mi.presses if s in mi.press_allowed_skus.get(p, ()))
                for s in cure_skus}
    press_cap = {s: min(mi.mould_pairs.get(s, 0), elig_cnt[s]) for s in cure_skus}
    cure_rate = {s: int(mi.cure_rate.get(s, 0)) for s in cure_skus}
    sku_moulds = {s: frozenset(mi.sku_moulds.get(s, ())) for s in cure_skus}

    # building machines that make GT (UNI first — no carcass dep — then Stage-2), and
    # per-machine buildable cure-SKUs (matching model_bld_pairs).
    bld_pairs = model_bld_pairs(mi)
    def _gt_ms(grp):
        return sorted(m for m in mi.machines if mi.machine_group.get(m) == grp)
    gt_machines = _gt_ms("UNI") + ([] if uni_only else _gt_ms("S2"))
    machine_skus: dict = {}
    for mac in gt_machines:
        machine_skus[mac] = sorted(s for (mm, s) in bld_pairs
                                   if mm == mac and s in press_cap)

    import bc_config as _bc
    GT_CAP = int(getattr(_bc, "MAX_ENDOFDAY_GT_INVENTORY", 8000))
    MAX_CO = int(getattr(_bc, "MAX_CHANGEOVERS_PER_DAY", 12))

    # ---- running state seeded from THIS window's carry-over (not month-opening) ----
    dem = demand if demand is not None else mi.demand
    dem_rem = {s: int(dem.get(s, 0)) for s in mi.skus}
    if init_gt is not None:
        gt = {s: int(init_gt.get(s, 0)) for s in mi.skus}
    else:
        gt = {s: int(mi.opening_gt.get(s, 0)) for s in mi.skus}
    if init_n is not None:
        n_prev = {s: int(init_n.get(s, 0)) for s in cure_skus}
    else:
        n_prev = {s: 0 for s in cure_skus}
        for p in mi.presses:
            s0 = mi.press_init_sku.get(p)
            if s0 in n_prev:
                n_prev[s0] += 1
    for s in cure_skus:
        n_prev[s] = min(n_prev[s], press_cap[s])

    dem_rem0 = {s: int(dem.get(s, 0)) for s in mi.skus}   # window build cap (cumulative)
    built_total = {s: 0 for s in mi.skus}

    hint_n: dict = {}
    hint_g: dict = {}
    implied_cured = 0

    for local_t in range(W):
        gt_g = base + local_t
        if local_t % 3 == 0:
            co_left = MAX_CO

        # ---- desired curing this shift: KEEP the running presses (feed them), LIFTED
        #      toward greedy's per-shift target (its pacing). Never below the running
        #      count so a fed press is never dropped; clamped to every cap. ----
        want = {}
        for s in cure_skus:
            if dem_rem[s] <= 0 or cure_rate[s] <= 0:
                want[s] = 0
                continue
            raw = max(n_prev[s], int(greedy_n.get((s, gt_g), 0)))
            want[s] = min(raw, press_cap[s], dem_rem[s] // cure_rate[s])

        # ================= BUILDING (runs before curing this shift) =================
        # Proven constructive feed: 1) cover running presses whose GT is short (need),
        # 2) else pre-build for SKUs Pass B will ramp onto. Bounded so post-cure
        # end-of-day total GT <= GT_CAP; greedy_g biases the machine->SKU tiebreak.
        need = {s: max(0, cure_rate[s] * want[s] - gt[s]) for s in cure_skus}
        planned_cure = sum(cure_rate[s] * min(want[s], n_prev[s]) for s in cure_skus)
        total_gt = sum(gt.values())
        build_budget = GT_CAP + planned_cure - total_gt
        g_pref = {}                        # greedy's machine->SKU pick this global shift
        for (mac, s, tt), q in greedy_g.items():
            if tt == gt_g and mac in machine_skus and s in press_cap and q > 0:
                if mac not in g_pref or q > g_pref[mac][0]:
                    g_pref[mac] = (int(q), s)
        for mac in gt_machines:
            if build_budget <= 0:
                break
            allowed = machine_skus.get(mac, ())
            if not allowed:
                continue
            gp = g_pref.get(mac)
            gp_s = gp[1] if gp else None
            best = None
            for s in allowed:              # 1st: running presses short of GT (biggest need)
                if need[s] > 0 and built_total[s] < dem_rem0[s]:
                    keyv = need[s] + (1 if s == gp_s else 0)   # nudge greedy's own pick
                    if best is None or keyv > best[0] or (keyv == best[0] and s < best[1]):
                        best = (keyv, s)
            if best is None:               # 2nd: pre-build for a SKU Pass B will grow onto
                for s in allowed:
                    if dem_rem[s] <= 0 or built_total[s] >= dem_rem0[s]:
                        continue
                    if gt[s] >= cure_rate[s] * press_cap[s]:    # already 1 full shift banked
                        continue
                    keyv = dem_rem[s] + (1 if s == gp_s else 0)
                    if best is None or keyv > best[0] or (keyv == best[0] and s < best[1]):
                        best = (keyv, s)
            if best is None:
                continue
            s = best[1]
            cap_gt = cure_rate[s] * press_cap[s]              # never bank >1 shift full draw
            qty = min(int(mi.build_rate.get((mac, s), 0)),
                      dem_rem0[s] - built_total[s],            # cumulative window build cap
                      cap_gt - gt[s],
                      build_budget)
            if qty <= 0:
                continue
            hint_g[(mac, s, local_t)] = int(qty)
            gt[s] += qty
            built_total[s] += qty
            build_budget -= qty
            need[s] = max(0, need[s] - qty)

        # ================= CURING (per-SKU press counts) =================
        free_moulds = set().union(*(sku_moulds[s] for s in cure_skus)) if cure_skus else set()
        n_cur = {s: 0 for s in cure_skus}
        used = 0

        # Pass A — retain producers up to greedy's want (no new CO beyond prev). Then
        # Pass B — greedy wants MORE presses on a SKU than are running: CO them on
        # (bounded by the daily CO budget). Deterministic order: greedy's want desc.
        order = sorted(cure_skus, key=lambda x: (-want[x], x))
        # Pass A: keep min(want, prev) producers, fed by GT on hand.
        for s in order:
            keep = min(want[s], n_prev[s], press_cap[s],
                       dem_rem[s] // cure_rate[s] if cure_rate[s] else 0,
                       gt[s] // cure_rate[s] if cure_rate[s] else 0,
                       N_PRESS - used)
            if keep <= 0:
                continue
            pool = sorted(free_moulds & sku_moulds[s])
            keep = min(keep, len(pool) // 2)
            if keep <= 0:
                continue
            free_moulds.difference_update(pool[:2 * keep])
            n_cur[s] = keep
            used += keep
            cured = keep * cure_rate[s]
            gt[s] -= cured
            dem_rem[s] -= cured
            implied_cured += cured
        # Pass B: CO extra presses onto SKUs greedy wants above the running count. A
        # newly CO'd press produces 0 this shift (matches model prod = n - co); raise
        # n_cur AT/ABOVE n_prev only, and charge one CO per added press.
        for s in order:
            if co_left <= 0 or used >= N_PRESS:
                break
            target = min(want[s], press_cap[s])
            if target <= n_cur[s] or n_cur[s] < n_prev[s]:
                continue
            room = min(target - n_cur[s], N_PRESS - used, co_left)
            if room <= 0:
                continue
            pool = sorted(free_moulds & sku_moulds[s])
            add = min(room, len(pool) // 2)
            if add <= 0:
                continue
            free_moulds.difference_update(pool[:2 * add])
            n_cur[s] += add
            used += add
            co_left -= add                                    # each added press = 1 CO

        for s in cure_skus:
            if n_cur[s]:
                hint_n[(s, local_t)] = n_cur[s]
        n_prev = n_cur

    return {"n": hint_n, "g": hint_g, "_implied_cured": implied_cured}


if __name__ == "__main__":
    # Smoke test: extract the month plan + slice two windows.
    import cbc_env  # noqa: F401  (sets up env/paths)
    from optimizer.data import load_model_inputs

    mi = load_model_inputs()
    print("ModelInputs:", mi.summary())
    gn, gg, meta = extract_month_greedy(mi)
    print("meta:", meta)
    h = window_hint(mi, gn, gg, day_start=1, n_days=13)
    print(f"window day1 (13d): {len(h['n'])} n-entries, {len(h['g'])} g-entries")
    h11 = window_hint(mi, gn, gg, day_start=11, n_days=13)
    print(f"window day11 (13d): {len(h11['n'])} n-entries, {len(h11['g'])} g-entries")
