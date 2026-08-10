"""optimizer/carcass_sched.py — decoupled just-in-time Stage-1 carcass scheduler.

The CP-SAT window model schedules Stage-1 carcass (via the plan's `gc` vars) FRONT-LOADED:
it banks carcass early, but the Stage-2 GT that consumes it runs >1 calendar day later, so
the plant's HARD 1-day carcass shelf (validator rule 9C) is massively violated and rule 5
(Stage-2 GT <= available carcass) can break at the seam.

`reschedule_carcass` DECOUPLES the two stages. It KEEPS every GT decision the CP-SAT made
(Stage-2 GT + VMI/BJ/UNI GT rows untouched), DISCARDS the model's carcass rows, and
RE-SCHEDULES Stage-1 carcass just-in-time to feed exactly the Stage-2 GT that remains —
1 carcass per 1 Stage-2 GT unit, built same shift or (at most) 1 calendar day before, on the
Stage-1 machines allowed for that SKU, within each machine's per-shift minute budget
(production + changeover <= 480, one SKU per machine per shift).

This mirrors the greedy pipeline's adopted policy ("STAGE1_CO ON, Stage-1 supplies 100% of
Stage-2 carcass demand, pre-build within 1-day aging") but operates on the optimizer's
`building_rows` dict list rather than the greedy's internal state.

Public API
----------
    reschedule_carcass(mi, building_rows) -> (n_carcass_rows, total_carcass_units)

MUTATES `building_rows` in place: removes all Stage-1 rows, appends fresh JIT carcass rows.
The emitted rows use the SAME dict shape the driver's `_accumulate_window_rows` produces:
    {"Day", "Date", "Shift", "Machine", "Group", "Type", "SKUCode", "Inch", "Qty"}
with Type="carcass", Machine in the Stage-1 set, SKUCode = the Stage-2 GT SKU it feeds.

Do NOT edit optimizer/driver.py or optimizer/model.py — the coordinator wires this module.
"""
from __future__ import annotations

from collections import defaultdict

import bc_config as bc
from b2c_pipeline import _MACHINE_GROUP, _bld_ct_sec

_SHIFT_ORD = {"A": 0, "B": 1, "C": 2}
_SHIFT_LABEL = ("A", "B", "C")

# Stage-1 / Stage-2 machine sets, taken from the single source of truth in b2c_pipeline.
STAGE1 = frozenset(m for m, g in _MACHINE_GROUP.items() if g == "STAGE1")
STAGE2 = frozenset(m for m, g in _MACHINE_GROUP.items() if g == "STAGE2")

_SHIFT_MINS = int(getattr(bc, "SHIFT_MINS", 480))
_CO_SAME = int(bc.BUILDING_CO_SAME_SIZE.get("STAGE1", 60))     # 60 min  same-inch carcass CO
_CO_DIFF = int(bc.BUILDING_CO_DIFF_SIZE.get("STAGE1", 180))    # 180 min diff-inch carcass CO
_CARCASS_EOD_CAP = int(getattr(bc, "MAX_ENDOFDAY_CARCASS_INVENTORY", 1200))


def _s1_sku_to_machines(mi) -> dict[str, list[str]]:
    """{Stage-2 GT SKU -> sorted eligible Stage-1 carcass machines}, from mi's allowable map.

    A Stage-1 machine builds carcass for the Stage-2 GT SKUs listed in its allowable set
    (`mi.machine_allowed_skus`); the carcass SKU IS the Stage-2 GT SKU it feeds. This mirrors
    b2c_pipeline's `s1_sku_to_machines` (built from the same DB allowable matrix, Stage-1 rows).
    """
    s1map: dict[str, set] = defaultdict(set)
    for m in mi.machines:
        if _MACHINE_GROUP.get(str(m), "") != "STAGE1":
            continue
        for s in mi.machine_allowed_skus.get(m, ()):  # allowed (in-demand) Stage-2 GT SKUs
            s1map[str(s)].add(str(m))
    return {s: sorted(ms) for s, ms in s1map.items()}


def reschedule_carcass(mi, building_rows: list) -> tuple[int, int]:
    """Decoupled JIT Stage-1 carcass reschedule. MUTATES `building_rows` in place.

    Steps:
      1. Remove every Stage-1 (carcass) row; keep all GT rows (Stage-2 + VMI/BJ/UNI).
      2. Derive per-(SKU, day, shift) Stage-2 GT built = the 1:1 carcass demand.
      3. Greedily schedule Stage-1 carcass just-in-time: for each consuming shift (chrono
         order) build the required carcass SAME shift first, then spill back at most 1
         calendar day (never earlier — rule 9C), on that SKU's allowable Stage-1 machines,
         within each machine's Prod+CO<=480 budget and one-SKU-per-machine-per-shift.
         Opening carcass (never ages; FIFO-consumed first by the validator) covers the
         earliest consumption so no phantom carcass is left to age out.
      4. Append the new carcass rows (driver row shape) back into building_rows.

    Returns (n_carcass_rows_emitted, total_carcass_units_built).
    Also stashes a diagnostic report on the function attribute `reschedule_carcass.report`.
    """
    s1map = _s1_sku_to_machines(mi)

    # ---- Step 1: split off Stage-1 rows, keep the rest ----
    kept = [r for r in building_rows if str(r["Machine"]) not in STAGE1]

    # ---- Step 2: carcass demand = Stage-2 GT per (sku, global-shift) ----
    #   global-shift g = (day-1)*3 + shift_ord, so chronological order is a plain int sort.
    demand: dict = defaultdict(float)                 # (sku, g) -> Stage-2 GT units
    day_date: dict = {}                               # day-int -> date string (for row stamping)
    for r in kept:
        if str(r["Machine"]) not in STAGE2:
            continue
        q = float(r["Qty"])
        if q <= 0:
            continue
        d = int(r["Day"]); sh = str(r["Shift"])
        demand[(str(r["SKUCode"]), (d - 1) * 3 + _SHIFT_ORD.get(sh, 0))] += q
        day_date.setdefault(d, r["Date"])

    total_s2_gt = sum(demand.values())

    # Opening carcass per SKU (never ages; the validator FIFO-consumes it FIRST). We mirror
    # that by covering the earliest consumption with opening and building only the residual —
    # otherwise same-shift carcass built for that early consumption would be left uncured by
    # FIFO and age out (rule 9C).
    opening_rem: dict = defaultdict(float,
                                    {str(k): float(v) for k, v in (mi.opening_carcass or {}).items() if v})

    # ---- Step 3: greedy JIT allocation ----
    # Per (machine, global-shift): the single carcass SKU + units + CO minutes placed there.
    slot_sku: dict = {}                               # (m, b) -> sku (rule 16: one SKU/machine/shift)
    slot_qty: dict = defaultdict(float)               # (m, b) -> units built
    slot_co: dict = {}                                # (m, b) -> CO minutes this slot incurred
    mach_timeline: dict = defaultdict(list)           # m -> sorted [(shift, sku)] for CO lookup

    no_elig: dict = defaultdict(float)                # SKU has no allowable Stage-1 machine
    shortfall: dict = defaultdict(float)              # (sku, g) -> carcass we could not place in-window

    def _ct(m, s):
        c = _bld_ct_sec(str(m), s)
        return c if c and c > 0 else 1.0

    def _co_between(prev, s):
        """CO minutes for a machine switching from producing `prev` to `s` (None prev = first)."""
        if prev is None or prev == s:
            return 0
        return _CO_SAME if mi.sku_inch.get(prev, "") == mi.sku_inch.get(s, "") else _CO_DIFF

    def _pred_sku(m, b):
        """SKU produced on m at the latest shift strictly before b (or None)."""
        prev = None
        for (bb, sku) in mach_timeline[m]:
            if bb < b:
                prev = sku
            else:
                break
        return prev

    def _succ(m, b):
        """(shift, sku) of m's earliest producing shift strictly after b (or None)."""
        for (bb, sku) in mach_timeline[m]:
            if bb > b:
                return bb, sku
        return None

    def _slot_cap(m, b, s):
        """Units of SKU s addable at (m,b) under Prod+CO<=480 (r15), one-SKU/machine/shift (r16).
        For a FRESH slot the immediate successor's CO is re-evaluated (its predecessor becomes s):
        the insertion is only allowed if the successor still fits. Returns (cap, co, succ_update)
        where succ_update = (succ_shift, new_succ_co) to apply on commit, or None."""
        key = (m, b)
        cur = slot_sku.get(key)
        ct = _ct(m, s)
        if cur is not None:
            if cur != s:
                return 0, 0, None                      # rule 16
            cap = int((_SHIFT_MINS - slot_co[key] - slot_qty[key] * ct / 60.0) * 60.0 / ct)
            return (cap if cap > 0 else 0), slot_co[key], None
        # fresh slot: CO vs the machine's predecessor at b
        co = _co_between(_pred_sku(m, b), s)
        succ_update = None
        sc = _succ(m, b)
        if sc is not None:
            b2, s2 = sc
            new_co2 = _co_between(s, s2)                # successor's predecessor becomes s
            ct2 = _ct(m, s2)
            if slot_qty[(m, b2)] * ct2 / 60.0 + new_co2 > _SHIFT_MINS + 1e-6:
                return 0, 0, None                      # inserting here would overflow the successor
            if new_co2 != slot_co[(m, b2)]:
                succ_update = (b2, new_co2)
        cap = int((_SHIFT_MINS - co) * 60.0 / ct)
        return (cap if cap > 0 else 0), co, succ_update

    def _place(m, b, s, want):
        """Place up to `want` carcass units of SKU s on machine m at shift b. Returns placed."""
        cap, co, succ_update = _slot_cap(m, b, s)
        if cap <= 0:
            return 0.0
        add = float(min(cap, want))
        if add <= 0:
            return 0.0
        key = (m, b)
        if key not in slot_sku:
            slot_sku[key] = s
            slot_co[key] = co
            mach_timeline[m].append((b, s))
            mach_timeline[m].sort()
            if succ_update is not None:                # inserting before an existing slot changed
                slot_co[(m, succ_update[0])] = succ_update[1]   # that successor's CO — apply it
        slot_qty[key] += add
        return add

    for g in sorted({g for (_s, g) in demand}):
        day = g // 3 + 1
        win_start = max(0, (day - 2) * 3)              # first shift of the previous calendar day
        # build-shift preference: JIT — same shift first, then back within the 1-day window
        build_shifts = list(range(g, win_start - 1, -1))
        # most-constrained-first: SKUs with the FEWEST eligible Stage-1 machines claim their
        # scarce machines before more-flexible SKUs can take them (cuts contention shortfall);
        # deterministic tiebreak on the SKU code.
        for s in sorted((sku for (sku, gg) in demand if gg == g),
                        key=lambda sk: (len(s1map.get(sk, ())), sk)):
            need = demand[(s, g)]
            # opening carcass covers the earliest consumption (validator FIFO-consumes it first)
            take = min(opening_rem[s], need)
            if take > 0:
                opening_rem[s] -= take
                need -= take
            if need <= 1e-9:
                continue
            machines = s1map.get(s)
            if not machines:
                no_elig[s] += need
                continue
            for b in build_shifts:
                if need <= 1e-9:
                    break
                # rank machines by CHEAPEST changeover first (continuation co=0, then same-inch
                # 60, then diff 180) so carcass campaigns stick to one machine and CO waste — the
                # thing that steals rule-15 capacity — is minimized; deterministic tiebreak on id.
                ranked = sorted(machines, key=lambda mm: (_slot_cap(mm, b, s)[1], str(mm)))
                for m in ranked:
                    if need <= 1e-9:
                        break
                    need -= _place(m, b, s, need)
            if need > 1e-9:
                shortfall[(s, g)] += need

    # ---- Step 3b: per-SKU FIFO reconciliation — enforce rule 5 + rule 9C BY CONSTRUCTION ----
    #
    # The greedy above builds carcass within each Stage-2 shift's 1-day window, but the
    # validator matches carcass to Stage-2 with a GLOBAL per-SKU FIFO (oldest carcass to
    # earliest consumption, 1-CALENDAR-day aging). A per-shift shortfall therefore leaves a
    # CUMULATIVE hole (rule 5 fails at the exact short shift, not the month end) and strands a
    # matching lump of carcass that then ages past 1 day (rule 9C). We replay that exact FIFO
    # here and, wherever it cannot serve a Stage-2 GT unit from carcass aged <=1 day, we
    #   (1) REDUCE the Stage-2 GT row IN PLACE at that (sku, day, shift)  -> rule 5 == 0, and
    #   (2) DROP the carcass that FIFO leaves unconsumed past 1 day        -> rule 9C == 0.
    # Both hold at every shift by construction; the driver cascades stage2_reduced to cured.
    stage2_reduced = _fifo_reconcile(mi, kept, demand, slot_sku, slot_qty)

    # ---- Step 3c: min-campaign consolidation (OPT_MIN_CAMP) — merge sub-MIN carcass pieces of
    #      the SAME (sku,shift) into fuller same-(sku,shift) slots. Shelf-safe (same shift, so
    #      rule-9C/5 FIFO totals per (sku,shift) are unchanged). A whole (sku,shift) need below
    #      MIN is KEPT (serve genuinely small demand). Removes the <40 carcass slivers. ----
    import os as _os
    if _os.environ.get("OPT_MIN_CAMP", "0") == "1":
        _MINC = int(_os.environ.get("OPT_MIN_CAMP_UNITS", "40"))
        _by_sb: dict = defaultdict(list)
        for (m, b) in list(slot_sku):
            _by_sb[(slot_sku[(m, b)], b)].append(m)
        for (s, b), ms in _by_sb.items():
            if len(ms) <= 1 or sum(slot_qty[(m, b)] for m in ms) < _MINC:
                continue                                   # single slot or whole small need -> keep
            small = [m for m in ms if 0 < slot_qty[(m, b)] < _MINC]
            big = sorted([m for m in ms if slot_qty[(m, b)] >= _MINC],
                         key=lambda mm: -slot_qty[(mm, b)])
            for sm in small:
                for bm in big:
                    if slot_qty[(sm, b)] <= 0.5:
                        break
                    room, _, _ = _slot_cap(bm, b, s)       # remaining units addable on the big slot
                    mv = min(room, slot_qty[(sm, b)])
                    if mv > 0:
                        slot_qty[(bm, b)] += mv
                        slot_qty[(sm, b)] -= mv
                if slot_qty[(sm, b)] <= 0.5:               # emptied -> drop the sliver slot
                    slot_qty.pop((sm, b), None)
                    slot_sku.pop((sm, b), None)

    # ---- Step 4: emit carcass rows in the driver row shape ----
    carc_rows: list = []
    for (m, b), s in slot_sku.items():
        q = int(round(slot_qty[(m, b)]))
        if q <= 0:
            continue
        day = b // 3 + 1
        carc_rows.append({
            "Day": day,
            "Date": day_date.get(day, ""),
            "Shift": _SHIFT_LABEL[b % 3],
            "Machine": m,
            "Group": mi.machine_group.get(m, ""),      # "S1" — same convention as driver GT rows
            "Type": "carcass",
            "SKUCode": s,
            "Inch": mi.sku_inch.get(s, ""),
            "Qty": q,
        })

    # commit the mutation: replace S1 rows with the fresh carcass plan (keep GT rows in place)
    building_rows[:] = kept + carc_rows

    total_carcass = int(sum(int(round(slot_qty[k])) for k in slot_sku))
    opening_used = int(sum(float(v) for v in (mi.opening_carcass or {}).values())
                       - sum(opening_rem.values()))

    # end-of-day carcass carried overnight (spill builds only): flag EOD-cap pressure (rule EOD)
    eod_carry = _eod_carcass_by_day(carc_rows, demand, mi, day_date)
    over_days = {d: v for d, v in eod_carry.items() if v > _CARCASS_EOD_CAP + 0.5}

    reschedule_carcass.report = {
        "total_s2_gt": int(round(total_s2_gt)),
        "total_carcass_built": total_carcass,
        "opening_used": opening_used,
        "supplied": total_carcass + opening_used,
        "n_carcass_rows": len(carc_rows),
        "no_elig_skus": dict(no_elig),
        "no_elig_units": int(round(sum(no_elig.values()))),
        "shortfall_events": len(shortfall),
        "shortfall_units": int(round(sum(shortfall.values()))),
        "shortfall_by_sku": {s: int(round(v)) for (s, _g), v in _sum_by_sku(shortfall).items()},
        "max_eod_carcass": int(round(max(eod_carry.values()))) if eod_carry else 0,
        "eod_cap": _CARCASS_EOD_CAP,
        "eod_over_days": {int(d): int(round(v)) for d, v in over_days.items()},
        "stage2_reduced": int(round(stage2_reduced)),
    }
    return len(carc_rows), total_carcass


def _fifo_reconcile(mi, kept: list, demand: dict, slot_sku: dict, slot_qty: dict) -> float:
    """Replay the validator's exact per-SKU carcass FIFO (1-CALENDAR-day aging) over the greedy
    carcass build + Stage-2 GT demand, and make rule 5 + rule 9C hold BY CONSTRUCTION:

      * where FIFO cannot serve a Stage-2 GT unit from carcass aged <= 1 day, REDUCE the
        Stage-2 GT row(s) in `kept` at that exact (sku, day, shift)  -> rule 5 == 0;
      * DROP (from `slot_qty`) the carcass FIFO leaves unconsumed past 1 calendar day
        -> rule 9C == 0.

    Mutates `kept` Stage-2 rows and `slot_qty` in place. Returns total Stage-2 GT units reduced.
    """
    from collections import deque

    # carcass built per (sku, build-shift) + the slot keys backing it (to drop from on aging)
    build_at: dict = defaultdict(float)                # (sku, b) -> carcass units built
    keys_at: dict = defaultdict(list)                  # (sku, b) -> [slot keys (m, b)]
    for (m, b), s in slot_sku.items():
        q = slot_qty[(m, b)]
        if q > 0:
            build_at[(s, b)] += q
            keys_at[(s, b)].append((m, b))

    # Stage-2 GT rows per (sku, global-shift) — reduced in place on a rule-5 shortfall
    s2_rows: dict = defaultdict(list)
    for r in kept:
        if str(r["Machine"]) in STAGE2 and float(r["Qty"]) > 0:
            g = (int(r["Day"]) - 1) * 3 + _SHIFT_ORD.get(str(r["Shift"]), 0)
            s2_rows[(str(r["SKUCode"]), g)].append(r)

    def _reduce_s2(sku, g, amt):
        """Reduce `amt` Stage-2 GT units at (sku, g), latest row first; returns amount cut."""
        cut = 0.0
        for r in s2_rows.get((sku, g), ()):
            if amt <= 1e-9:
                break
            q = float(r["Qty"]); take = min(q, amt)
            r["Qty"] = int(round(q - take)); amt -= take; cut += take
        return cut

    def _drop_carcass(sku, b, amt):
        """Drop `amt` carcass units built at (sku, b) from its backing slots (aged-out surplus)."""
        for key in keys_at.get((sku, b), ()):
            if amt <= 1e-9:
                break
            q = slot_qty[key]; take = min(q, amt)
            slot_qty[key] = q - take; amt -= take

    skus = ({s for (s, _b) in build_at} | {s for (s, _g) in demand}
            | set(mi.opening_carcass or {}))
    stage2_reduced = 0.0
    for s in sorted(skus):
        gs = ([g for (ss, g) in demand if ss == s]
              + [b for (ss, b) in build_at if ss == s])
        if not gs and not (mi.opening_carcass or {}).get(s):
            continue
        dmin = min(g // 3 for g in gs) if gs else 0
        dmax = max(g // 3 for g in gs) if gs else 0
        q: deque = deque()                             # entries: [build_day, qty, build_shift]
        op = float((mi.opening_carcass or {}).get(s, 0.0))
        if op > 0:
            q.append([-1, op, None])                   # opening: available from day 0, never ages
        for dn in range(dmin, dmax + 1):
            for sh in range(3):
                g = dn * 3 + sh
                add = build_at.get((s, g), 0.0)
                if add > 0:
                    q.append([dn, add, g])
                need = demand.get((s, g), 0.0)
                while need > 1e-9 and q:               # FIFO consume oldest-first (validator order)
                    front = q[0]
                    take = min(front[1], need)
                    front[1] -= take; need -= take
                    if front[1] <= 1e-9:
                        q.popleft()
                if need > 1e-9:                        # carcass short at this exact shift -> cap S2
                    stage2_reduced += _reduce_s2(s, g, need)
            # end of calendar day dn: carcass older than 1 day is unconsumable -> drop it (r9C)
            while q and q[0][0] >= 0 and (dn - q[0][0]) > 1:
                aged = q.popleft()
                _drop_carcass(s, aged[2], aged[1])
    return stage2_reduced


def _sum_by_sku(shortfall: dict) -> dict:
    out: dict = defaultdict(float)
    for (s, g), v in shortfall.items():
        out[(s, 0)] += v
    return out


def _eod_carcass_by_day(carc_rows: list, demand: dict, mi, day_date: dict) -> dict:
    """Per calendar-day end-of-day carcass inventory (opening + built - consumed), summed over
    SKUs. JIT same-shift building carries ~0 overnight; only 1-day spill builds carry stock. Used
    only to flag EOD-cap pressure — the schedule itself is emitted regardless."""
    days = set()
    for r in carc_rows:
        days.add(int(r["Day"]))
    for (_s, g) in demand:
        days.add(g // 3 + 1)
    if not days:
        return {}
    dmin, dmax = min(days), max(days)
    # per (sku, day) built + consumed
    built = defaultdict(float); cons = defaultdict(float)
    for r in carc_rows:
        built[(str(r["SKUCode"]), int(r["Day"]))] += float(r["Qty"])
    for (s, g), q in demand.items():
        cons[(s, g // 3 + 1)] += q
    skus = {k[0] for k in built} | {k[0] for k in cons} | set(mi.opening_carcass or {})
    eod: dict = {}
    for d in range(dmin, dmax + 1):
        eod[d] = 0.0
    for s in skus:
        inv = float((mi.opening_carcass or {}).get(s, 0.0))
        for d in range(dmin, dmax + 1):
            inv += built.get((s, d), 0.0) - cons.get((s, d), 0.0)
            if inv < 0:
                inv = 0.0                              # never negative (rule 5 holds)
            eod[d] += inv
    return eod
