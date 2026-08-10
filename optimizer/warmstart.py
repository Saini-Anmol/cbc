"""Constructive greedy warm-start for the single-window CP-SAT model.

`greedy_hint(mi, day_start, n_days)` builds a FEASIBLE, non-trivial incumbent that
CP-SAT can start from (via `AddHint`) so a single-worker DETERMINISTIC solve begins
high and spends its budget IMPROVING rather than hunting for a first solution.

It returns exactly the two variable families the model exposes as a stable hint:
    {'n': {(sku, t): count},            # presses on sku at shift t  (t = 0 .. n_days*3-1)
     'g': {(machine, sku, t): units}}   # GT units built by an S2/UNI machine

The plan is built shift-by-shift with a running GT reservoir + demand tracker so it
never over-builds or over-cures past demand, and it honours every hard cap the model
enforces on `n`/`g`:

  * curing count model  : prod[s,t] = min(n[s,t], n[s,t-1]); a *newly added* press
    (a changeover) produces nothing its own shift. The greedy mirrors this so its
    implied `cured = cure_rate * prod` is exactly what the model derives from the hint.
  * press-count cap      : sum_s n[s,t]  <= 170
  * per-SKU mould cap    : n[s,t] <= mould_pairs[s]   (and <= #eligible presses)
  * shared-mould cap     : realised by allocating individual moulds from a per-shift
    free pool (2 per press) -> the union-find contention cap in model.py holds for free.
  * daily curing-CO cap  : sum of positive count increases per day <= MAX_CHANGEOVERS_PER_DAY
  * GT reservoir >= 0     : building runs first each shift; curing only draws GT on hand.
  * end-of-day GT cap     : total GT held <= MAX_ENDOFDAY_GT_INVENTORY (building throttled).
  * demand cap (sacred)   : cumulative cured <= demand[s]; cumulative built <= demand[s].
  * inch-lock             : a machine builds sku only if sku_inch[sku] in machine_allowed_inches[m].

Determinism: every iteration order is a stable sort (`(-key, sku/id)`), moulds are taken
in sorted order, so the hint is byte-identical across runs / PYTHONHASHSEED.

NB: the hint deliberately covers only `n` (curing) and `g` (S2/UNI GT). Stage-1 carcass
(`gc`/`carc`) is left for CP-SAT to complete — the greedy prefers UNISTAGE machines
(no carcass dependency) and uses Stage-2 machines only when a SKU's inch is reachable
nowhere else, keeping the carcass completion trivially feasible.
"""
from __future__ import annotations

import bc_config
from optimizer.data import ModelInputs

MAX_CO_PER_DAY = int(getattr(bc_config, "MAX_CHANGEOVERS_PER_DAY", 12))
GT_CAP         = int(getattr(bc_config, "MAX_ENDOFDAY_GT_INVENTORY", 8000))


def _cure_skus(mi: ModelInputs) -> list:
    """SKUs the model actually models on the curing side (mould-pair >=1, cureable, has presses)."""
    return sorted(
        s for s in mi.skus
        if mi.mould_pairs.get(s, 0) >= 1
        and mi.cure_rate.get(s, 0) > 0
        and len(mi.sku_presses.get(s, ())) > 0
    )


def greedy_hint(mi: ModelInputs, day_start: int, n_days: int) -> dict:
    """Return {'n': {(sku,t):count}, 'g': {(machine,sku,t):units}} — a strong feasible incumbent."""
    W = n_days * 3
    N_PRESS = len(mi.presses)

    cure_skus = _cure_skus(mi)
    press_cap = {s: min(mi.mould_pairs[s], len(mi.sku_presses[s])) for s in cure_skus}
    cure_rate = {s: int(mi.cure_rate[s]) for s in cure_skus}
    sku_moulds = {s: frozenset(mi.sku_moulds.get(s, ())) for s in cure_skus}

    # ---- building machines that make GT, UNISTAGE first (no carcass dep), then Stage-2 ----
    def _gt_ms(grp):
        return sorted(m for m in mi.machines if mi.machine_group.get(m) == grp)
    gt_machines = _gt_ms("UNI") + _gt_ms("S2")
    # per-machine buildable cure-SKUs (build_rate>0 and inch-lock ok), sorted deterministically
    machine_skus: dict = {}
    for m in gt_machines:
        inns = mi.machine_allowed_inches.get(m, set())
        ok = []
        for s in mi.machine_allowed_skus.get(m, ()):
            if s not in press_cap:                                   # only feed cureable SKUs
                continue
            if mi.build_rate.get((m, s), 0) <= 0:
                continue
            if inns and mi.sku_inch.get(s, "") not in inns:          # historical inch-lock (hard)
                continue
            ok.append(s)
        machine_skus[m] = sorted(ok)

    # ---- running state ----
    dem_rem = {s: int(mi.demand[s]) for s in mi.skus}
    gt = {s: int(mi.opening_gt.get(s, 0)) for s in mi.skus}          # GT reservoir per SKU
    built_total = {s: 0 for s in mi.skus}
    n_prev = {s: 0 for s in cure_skus}
    for p in mi.presses:                                            # presses running at t0
        s0 = mi.press_init_sku.get(p)
        if s0 in n_prev:
            n_prev[s0] += 1
    for s in cure_skus:                                             # clip to feasible domain
        n_prev[s] = min(n_prev[s], press_cap[s])

    hint_n: dict = {}
    hint_g: dict = {}

    for t in range(W):
        if t % 3 == 0:
            co_left = MAX_CO_PER_DAY                                 # daily curing-CO budget

        # ================= BUILDING (runs before curing this shift) =================
        # The model caps GT only on the POST-cure inventory (end-of-day total <= GT_CAP),
        # so building may add up to `GT_CAP + planned_cure - total_gt` this shift and let
        # the same-shift curing draw it back down. Priority 1: cover the running presses'
        # draw so no retained press starves. Priority 2 (leftover machines): pre-build GT
        # for the SKUs Pass B will change presses onto, so they can produce next shift.
        want = {}
        for s in cure_skus:
            want[s] = (min(n_prev[s], press_cap[s], dem_rem[s] // cure_rate[s])
                       if (n_prev[s] > 0 and dem_rem[s] > 0) else 0)
        planned_cure = sum(cure_rate[s] * want[s] for s in cure_skus)
        total_gt = sum(gt.values())
        build_budget = GT_CAP + planned_cure - total_gt             # keeps post-cure total <= GT_CAP
        need = {s: max(0, cure_rate[s] * want[s] - gt[s]) for s in cure_skus}
        for m in gt_machines:
            if build_budget <= 0:
                break
            # 1st choice: a SKU whose running presses still lack GT (biggest need).
            best = None
            for s in machine_skus[m]:
                if need[s] > 0 and built_total[s] < mi.demand[s]:
                    if best is None or need[s] > best[0] or (need[s] == best[0] and s < best[1]):
                        best = (need[s], s)
            if best is None:                                        # 2nd: pre-build for growth
                for s in machine_skus[m]:
                    if dem_rem[s] <= 0 or built_total[s] >= mi.demand[s]:
                        continue
                    target = cure_rate[s] * press_cap[s]            # up to 1 shift of full draw
                    if gt[s] >= target:
                        continue
                    key = dem_rem[s]
                    if best is None or key > best[0] or (key == best[0] and s < best[1]):
                        best = (key, s)
            if best is None:
                continue
            s = best[1]
            cap_gt = cure_rate[s] * press_cap[s]                    # never bank >1 shift full draw/SKU
            qty = min(mi.build_rate[(m, s)],
                      mi.demand[s] - built_total[s],                # demand cap on GT built
                      cap_gt - gt[s],
                      build_budget)
            if qty <= 0:
                continue
            hint_g[(m, s, t)] = int(qty)
            gt[s] += qty
            built_total[s] += qty
            build_budget -= qty
            need[s] = max(0, need[s] - qty)

        # ================= CURING (per-SKU press counts) =================
        free_moulds = set().union(*(sku_moulds[s] for s in cure_skus)) if cure_skus else set()
        n_cur = {s: 0 for s in cure_skus}
        used_presses = 0

        # -- Pass A: retained producers (no CO). Bounded by prev count, caps, GT and demand. --
        for s in sorted(cure_skus, key=lambda x: (-dem_rem[x], x)):
            if n_prev[s] <= 0 or dem_rem[s] <= 0:
                continue
            keep = min(n_prev[s], press_cap[s],
                       dem_rem[s] // cure_rate[s],                   # never cure past demand
                       gt[s] // cure_rate[s],                        # only draw GT on hand
                       N_PRESS - used_presses)
            if keep <= 0:
                continue
            pool = sorted(free_moulds & sku_moulds[s])               # shared-mould allocation
            keep = min(keep, len(pool) // 2)
            if keep <= 0:
                continue
            free_moulds.difference_update(pool[:2 * keep])
            n_cur[s] = keep
            used_presses += keep
            cured = keep * cure_rate[s]
            gt[s] -= cured
            dem_rem[s] -= cured

        # -- Pass B: fill idle presses as CO's onto the highest-remaining-demand SKUs. --
        #    A CO'd press produces nothing this shift (prod = min(n_cur, n_prev)); it starts
        #    next shift, so we only raise n_cur AT/ABOVE n_prev (never re-add a GT-starved drop).
        for s in sorted(cure_skus, key=lambda x: (-dem_rem[x], x)):
            if co_left <= 0 or used_presses >= N_PRESS:
                break
            if dem_rem[s] <= 0 or n_cur[s] < n_prev[s]:              # skip starved drops
                continue
            # presses still needed to eventually serve remaining demand
            needed = -(-dem_rem[s] // cure_rate[s]) - n_cur[s]       # ceil(dem_rem/rate) - current
            room = min(press_cap[s] - n_cur[s], needed,
                       N_PRESS - used_presses, co_left)
            if room <= 0:
                continue
            pool = sorted(free_moulds & sku_moulds[s])
            add = min(room, len(pool) // 2)
            if add <= 0:
                continue
            free_moulds.difference_update(pool[:2 * add])
            co = max(0, (n_cur[s] + add) - n_prev[s])                # count increase above prev = CO
            prev_co = max(0, n_cur[s] - n_prev[s])
            co_cost = co - prev_co
            if co_cost > co_left:                                    # respect daily CO budget exactly
                add = max(0, add - (co_cost - co_left))
                if add <= 0:
                    continue
                co_cost = max(0, (n_cur[s] + add) - max(n_prev[s], n_cur[s]))
            n_cur[s] += add
            used_presses += add
            co_left -= co_cost

        # record hint + roll state
        for s in cure_skus:
            hint_n[(s, t)] = n_cur[s]
        n_prev = n_cur

    return {"n": hint_n, "g": hint_g}


# ---------------------------------------------------------------------------
def _audit(mi: ModelInputs, hint: dict, n_days: int) -> dict:
    """Re-derive cured/caps from the hint EXACTLY as model.py would, and validate."""
    W = n_days * 3
    cure_skus = _cure_skus(mi)
    N_PRESS = len(mi.presses)
    n, g = hint["n"], hint["g"]

    # initial press counts (model init_n)
    prev = {s: 0 for s in cure_skus}
    for p in mi.presses:
        s0 = mi.press_init_sku.get(p)
        if s0 in prev:
            prev[s0] += 1
    for s in cure_skus:
        prev[s] = min(prev[s], min(mi.mould_pairs[s], len(mi.sku_presses[s])))

    gt = {s: int(mi.opening_gt.get(s, 0)) for s in mi.skus}
    dem_left = {s: int(mi.demand[s]) for s in mi.skus}
    total_cured = 0
    max_presses_shift = 0
    mould_ok = press_ok = inv_ok = demand_ok = co_ok = True
    worst_co = 0

    for t in range(W):
        # building
        built = {}
        for (m, s, tt), q in g.items():
            if tt == t:
                built[s] = built.get(s, 0) + q
        for s, q in built.items():
            gt[s] += q
        # curing
        cur_n = {s: n.get((s, t), 0) for s in cure_skus}
        shift_presses = sum(cur_n.values())
        max_presses_shift = max(max_presses_shift, shift_presses)
        if shift_presses > N_PRESS:
            press_ok = False
        for s in cure_skus:
            if cur_n[s] > mi.mould_pairs[s]:
                mould_ok = False
            prod = min(cur_n[s], prev[s])
            cured = prod * int(mi.cure_rate[s])
            if cured > gt[s] + 1e-9:
                inv_ok = False
            gt[s] -= cured
            dem_left[s] -= cured
            if dem_left[s] < 0:
                demand_ok = False
            total_cured += cured
        # daily CO
        if t % 3 == 0:
            day_co = 0
        for s in cure_skus:
            day_co += max(0, cur_n[s] - prev[s])
        if t % 3 == 2:
            worst_co = max(worst_co, day_co)
            if day_co > MAX_CO_PER_DAY:
                co_ok = False
        prev = cur_n
        if t % 3 == 2 and sum(gt.values()) > GT_CAP + 1e-6:
            inv_ok = False

    return {
        "implied_cured": total_cured,
        "max_presses_in_a_shift": max_presses_shift,
        "press_cap_ok(<=%d)" % N_PRESS: press_ok,
        "mould_pairs_ok": mould_ok,
        "gt_reservoir_ok(>=0)": inv_ok,
        "demand_cap_ok": demand_ok,
        "daily_co_ok(<=%d)" % MAX_CO_PER_DAY: co_ok,
        "worst_day_co": worst_co,
    }


if __name__ == "__main__":
    from optimizer.data import load_model_inputs

    mi = load_model_inputs()
    print("Inputs:", mi.summary())
    N_DAYS = 10
    hint = greedy_hint(mi, day_start=1, n_days=N_DAYS)
    print(f"greedy_hint: {len(hint['n'])} n-entries, {len(hint['g'])} g-entries "
          f"over a {N_DAYS}-day ({N_DAYS*3}-shift) window")

    rep = _audit(mi, hint, N_DAYS)
    print("\n--- warm-start audit (cured/caps re-derived exactly as model.py) ---")
    for k, v in rep.items():
        print(f"  {k}: {v:,}" if isinstance(v, int) else f"  {k}: {v}")

    # Hard assertions: the hint must respect the physical caps.
    assert rep["max_presses_in_a_shift"] <= len(mi.presses), "press-count cap violated!"
    assert rep["mould_pairs_ok"], "a SKU exceeds its mould_pairs!"
    assert rep["gt_reservoir_ok(>=0)"], "GT reservoir went negative!"
    assert rep["demand_cap_ok"], "cured past demand!"
    assert rep["daily_co_ok(<=%d)" % MAX_CO_PER_DAY], "daily curing-CO cap violated!"
    print(f"\nOK — implied total cured = {rep['implied_cured']:,} units in a {N_DAYS}-day window, "
          f"caps respected (<=170 presses/shift, <=mould_pairs/SKU, GT>=0, demand cap, "
          f"<={MAX_CO_PER_DAY} CO/day).")
