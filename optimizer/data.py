"""Model-input assembly for the CP-SAT optimizer.

Reuses the EXISTING ETLs + engine helpers (no re-derivation) so the optimizer sees
byte-identical data to the greedy engine — same demand, cycle times, allowable
matrices, mould bipartite, inch-lock sets, and opening inventories.

Month context (RUNNING_MOULDS_MONTH / PLAN_MONTH) is read from bc_config, which the
caller configures via env before import (same discipline as scratch_parity_run.py).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import bc_config

# Targeted inch-flexibility (env OPT_FLEX_MACHINES="6001:15,8301:13,..."): ADD specific inches
# to specific machines' historical allowed-inch set so idle machines can build a starving inch
# they are DB-allowable for. Empty -> bit-identical to the pure historical inch-lock.
_FLEX_MAP: dict = {}
for _pair in os.environ.get("OPT_FLEX_MACHINES", "").split(","):
    _pair = _pair.strip()
    if ":" in _pair:
        _mm, _ii = _pair.split(":", 1)
        _FLEX_MAP.setdefault(_mm.strip(), set()).add(_ii.strip())

# DOMINANT-inch override (env OPT_DOM_INCH="7004:18,7002:18"): FORCE an inch as a machine's
# dominant (rank-0) inch even if plant history never ran it there — for closing a zero-production
# gap (e.g. 18") on an idle machine. The inch is added to its allowed set + ranked first.
_DOM_OV: dict = {}      # machine -> (inch, rank). "7004:18" -> rank 0 (dominant); "7002:18:1" -> rank 1 (2nd)
for _pair in os.environ.get("OPT_DOM_INCH", "").split(","):
    _pair = _pair.strip()
    _parts = _pair.split(":")
    if len(_parts) >= 2:
        _DOM_OV[_parts[0].strip()] = (_parts[1].strip(), int(_parts[2]) if len(_parts) >= 3 else 0)

# TARGETED scarce-inch flex (env OPT_TARGET_FLEX="13,15", OPT_TARGET_FLEX_GROUPS="VMI"): auto-add
# ONLY the named scarce inches to every machine in the named groups that is DB-allowable for them,
# ranked AFTER its historical inches (low preference -> taken only when its dominant demand is idle
# and a diff-CO pays off). Tiny, bounded expansion (a couple inches on VMI only) vs the FULL
# DB-allowable flex that exploded the search space (Aug 542k<616k). Empty -> narrow hist-lock.
_TARGET_FLEX_INCHES = {x.strip() for x in os.environ.get("OPT_TARGET_FLEX", "").split(",") if x.strip()}
_TARGET_FLEX_GROUPS = {x.strip().upper() for x in
                       os.environ.get("OPT_TARGET_FLEX_GROUPS", "VMI").split(",") if x.strip()}
from curing_consumption import ConsumptionETL
from building_b2c import B2C_ETL
from cbc_env import make_engine

# Reuse the engine's own rate/inch tables + helpers (source of truth).
from b2c_pipeline import (
    _bld_ct_sec,             # (machine, sku) -> building seconds/unit
    _cure_qty_per_shift,     # (ct_min)      -> cured units/press/shift (x2 cavities)
    _MACHINE_ALLOWED_INCH_SET,  # machine -> historical allowed-inch set (hist-lock)
    _MACHINE_DOMINANT_INCH_RANKED,  # machine -> [inch] dominant-first (for flex rank)
    DEFAULT_CURING_CT,
    SHIFT_MINS,              # 480
)

# Building machine groups (authoritative, per CLAUDE.md / bc.md).
STAGE1 = {"6802", "6803", "6909", "6911", "7601", "7701",
          "7801", "7802", "7803", "7804", "8001", "8002", "8003", "8101"}
STAGE2 = {"8201", "8301", "8302", "8501", "8502", "7301"}
UNISTAGE = {"6001", "6002", "6003", "6004", "7001", "7002", "7003", "7004",
            "7101", "7102", "7103", "7104", "7105", "7106", "7201",
            "7501", "7502", "7503"}
_GT_MACHINES = STAGE2 | UNISTAGE          # machines that produce GT (Stage-1 = carcass only)

# --- Building-CO time sub-groups (for building-changeover pricing, OPT_BLD_CO) ---
# The optimizer lumps all GT-independent machines into one "UNI" model group, but the
# plant's CO-time tables (bc_config.BUILDING_CO_SAME_SIZE / _DIFF_SIZE) distinguish
# VMI / BJ / UNISTAGE-narrow. Map each machine to its CO-time table key so same/diff
# building-CO minutes are priced correctly per machine.
_VMI_MACHINES = {"6001", "6002", "6003", "6004", "7001", "7002", "7003", "7004"}
_BJ_MACHINES  = {"7101", "7102", "7103", "7104", "7105", "7106", "7201"}
_UNI_NARROW   = {"7501", "7502", "7503"}


def _co_time_key(m: str) -> str:
    """Machine id -> bc_config CO-time table key (VMI/BJ/UNISTAGE/STAGE2/STAGE1)."""
    if m in _VMI_MACHINES:
        return "VMI"
    if m in _BJ_MACHINES:
        return "BJ"
    if m in _UNI_NARROW:
        return "UNISTAGE"
    if m in STAGE2:
        return "STAGE2"
    if m in STAGE1:
        return "STAGE1"
    return "STAGE1"          # conservative fallback (most expensive same/diff)


def _pen_group(m: str) -> str:
    """Machine id -> diff-CO PENALTY group key (S2/VMI/BJ/UNI/S1). The model's coarse
    machine_group lumps VMI+BJ+UNISTAGE-narrow into one 'UNI'; the plant's diff-CO
    penalty gradient needs them split (VMI light, BJ/UNI-narrow heavy)."""
    if m in STAGE2:
        return "S2"
    if m in _VMI_MACHINES:
        return "VMI"
    if m in _BJ_MACHINES:
        return "BJ"
    if m in _UNI_NARROW:
        return "UNI"
    if m in STAGE1:
        return "S1"
    return "S1"             # conservative fallback (heavy penalty)


def _inch_of(sku: str) -> str:
    """SKU inch class — the engine's documented fallback: chars [8:10] of the code."""
    return sku[8:10] if len(sku) >= 10 else ""


@dataclass
class ModelInputs:
    # --- horizon (set by the driver per window) ---
    plan_start:    object                       # datetime of shift 0
    planning_days: int

    # --- SKUs ---
    skus:        list = field(default_factory=list)          # sorted SKU codes, demand>0
    demand:      dict = field(default_factory=dict)          # sku -> units (int)
    cure_rate:   dict = field(default_factory=dict)          # sku -> cured units/press/shift
    sku_inch:    dict = field(default_factory=dict)          # sku -> "14" etc.

    # --- building machines ---
    machines:            list = field(default_factory=list)  # sorted machine ids
    machine_group:       dict = field(default_factory=dict)  # machine -> "S1"|"S2"|"UNI"
    machine_allowed_skus: dict = field(default_factory=dict) # machine -> sorted [sku] (DB, in-demand)
    build_rate:          dict = field(default_factory=dict)  # (machine,sku) -> built units/shift
    machine_allowed_inches: dict = field(default_factory=dict) # machine -> set(inch) (hist-lock, widened under INCH_FLEX)
    machine_inch_rank:      dict = field(default_factory=dict) # machine -> {inch: rank} (0=dominant; for flex penalty)
    co_same:             dict = field(default_factory=dict)  # machine -> same-size building-CO minutes
    co_diff:             dict = field(default_factory=dict)  # machine -> diff-size building-CO minutes
    pen_group:           dict = field(default_factory=dict)  # machine -> diff-CO penalty group (S2/VMI/BJ/UNI/S1)

    # --- curing presses ---
    presses:          list = field(default_factory=list)     # sorted 170 roster
    press_allowed_skus: dict = field(default_factory=dict)   # press -> sorted [sku] (DB, in-demand)
    sku_presses:      dict = field(default_factory=dict)     # sku -> sorted [press] eligible
    press_init_sku:   dict = field(default_factory=dict)     # press -> sku running at t0 (None=idle)
    n_free_start:     int = 0                                 # #presses ABSENT from running-moulds -> CO-free day-1 start
    mould_life:       dict = field(default_factory=dict)     # press -> remaining cycles at t0

    # --- moulds (bipartite) ---
    sku_moulds:  dict = field(default_factory=dict)          # sku -> sorted [mould id]
    mould_pairs: dict = field(default_factory=dict)          # sku -> #mould-pairs (len//2), cap on presses
    press_init_moulds: dict = field(default_factory=dict)    # press -> [mould ids] mounted at t0

    # --- opening inventory ---
    opening_gt:      dict = field(default_factory=dict)      # sku -> GT units on hand at t0
    opening_carcass: dict = field(default_factory=dict)      # sku -> carcass units at t0

    def summary(self) -> str:
        return (f"SKUs={len(self.skus)} demand={sum(self.demand.values()):,} | "
                f"machines={len(self.machines)} (S1={sum(1 for m in self.machines if self.machine_group[m]=='S1')},"
                f"S2={sum(1 for m in self.machines if self.machine_group[m]=='S2')},"
                f"UNI={sum(1 for m in self.machines if self.machine_group[m]=='UNI')}) | "
                f"presses={len(self.presses)} idle@t0={sum(1 for p in self.presses if not self.press_init_sku.get(p))} | "
                f"moulded_skus={sum(1 for s in self.skus if self.mould_pairs.get(s,0)>=1)} | "
                f"openGT={sum(self.opening_gt.values()):,.0f} openCarcass={sum(self.opening_carcass.values()):,.0f}")


def load_model_inputs(demand_path: str | None = None,
                      plan_start=None, planning_days: int | None = None) -> ModelInputs:
    """Assemble ModelInputs from the live DB + demand file (reuses the engine ETLs)."""
    demand_path   = demand_path   or bc_config.DEMAND_FILE
    plan_start    = plan_start    or bc_config.PLAN_START
    planning_days = planning_days or bc_config.PLANNING_DAYS

    engine = make_engine()
    cetl   = ConsumptionETL(engine)
    betl   = B2C_ETL(engine)

    mi = ModelInputs(plan_start=plan_start, planning_days=planning_days)

    # ---- demand + cure rate + inch ----
    dem_df = cetl.load_demand(demand_path)
    mi.demand = {str(r.SKUCode): int(round(r.Quantity)) for r in dem_df.itertuples()}
    mi.skus   = sorted(mi.demand)
    ct_df = cetl.load_cycle_times()
    ct_map = {str(r.SKUCode): float(r.CycleTime_min) for r in ct_df.itertuples()}
    _press_eff = float(os.environ.get("OPT_PRESS_EFF", "1.0"))   # real cure rate = 100% * efficiency
    for s in mi.skus:
        # apply PRESS_EFFICIENCY so per-press throughput matches the plant (never exceeds its max)
        mi.cure_rate[s] = int(_cure_qty_per_shift(ct_map.get(s, DEFAULT_CURING_CT)) * _press_eff)
        mi.sku_inch[s]  = _inch_of(s)
    _demand_set = set(mi.skus)

    # ---- building machines (allowable matrix, DB) ----
    ba_df = betl.load_machine_allowable()          # rows: machine + allowable SKU list
    # load_machine_allowable returns a df with a 'Machines' comma-list per SKU OR per-machine;
    # normalise to machine -> set(sku). Detect schema by columns.
    m_alw: dict = {}
    cols = [c.lower() for c in ba_df.columns]
    if "machines" in cols:                          # schema: SKUCode + comma 'Machines'
        skucol = ba_df.columns[[c.lower().startswith("sku") or c.lower() in ("sapcode",) for c in ba_df.columns].index(True)]
        for r in ba_df.itertuples(index=False):
            sku = str(getattr(r, skucol)).strip()
            if sku not in _demand_set:
                continue
            raw = getattr(r, "Machines")
            ms = raw if isinstance(raw, (list, tuple, set)) else str(raw).split(",")
            for m in ms:
                m = str(m).strip()
                if m:
                    m_alw.setdefault(m, set()).add(sku)
    else:                                           # schema: Machine + SKUCode rows
        mcol, scol = ba_df.columns[0], ba_df.columns[1]
        for r in ba_df.itertuples(index=False):
            m, sku = str(getattr(r, mcol)).strip(), str(getattr(r, scol)).strip()
            if sku in _demand_set:
                m_alw.setdefault(m, set()).add(sku)

    # ── PLANT-CAPABILITY expansion (OPT_PLANT_ALLOW): the DB allowable matrix is more
    #    conservative than what the plant actually runs. Using the 4-month plant report
    #    (Inch_Counts_Matrix), a GT machine may build any demand SKU whose INCH it runs
    #    >= OPT_PLANT_ALLOW_SHARE% of the time. This unstrands the scarce-inch (13"/15") gap
    #    the DB matrix hides. Also drives machine_allowed_inches (plant-actual) below. ──
    _PLANT_INCH: dict = {}
    if os.environ.get("OPT_PLANT_ALLOW", "0") == "1":
        import pandas as _pd
        _share = float(os.environ.get("OPT_PLANT_ALLOW_SHARE", "5")) / 100.0
        _pm = _pd.read_excel("data/analysis_aug/machine_inch_dominant_4months_Apr-Jul.xlsx",
                             sheet_name="Inch_Counts_Matrix")
        _icol = {'12"': '12', '13"': '13', '14"': '14', '15"': '15', '16"': '16', '17"': '17', '18"': '18'}
        for _, _r in _pm.iterrows():
            _mm = str(_r['Machine']).strip(); _tot = _r['Total'] or 1
            _sh = [(_i, _r[_c] / _tot) for _c, _i in _icol.items() if _r[_c] / _tot >= _share]
            _PLANT_INCH[_mm] = [_i for _i, _v in sorted(_sh, key=lambda z: -z[1])]  # dominant(highest-share)-first
        _sku_by_inch: dict = {}
        for _s in _demand_set:
            _sku_by_inch.setdefault(_inch_of(_s), []).append(_s)
        _added = 0
        for _mm, _inset in _PLANT_INCH.items():
            _grp = "S1" if _mm in STAGE1 else "S2" if _mm in STAGE2 else "UNI" if _mm in UNISTAGE else None
            if _grp in (None, "S1"):                  # GT machines only (Stage-1 carcass excluded)
                continue
            for _i in _inset:
                for _s in _sku_by_inch.get(_i, ()):
                    if _s not in m_alw.get(_mm, set()):
                        m_alw.setdefault(_mm, set()).add(_s); _added += 1
        print(f"  [PLANT_ALLOW] +{_added} (machine,SKU) pairs from 4-month plant inch usage (>={_share*100:.0f}%)")

    for m in sorted(m_alw):
        grp = "S1" if m in STAGE1 else "S2" if m in STAGE2 else "UNI" if m in UNISTAGE else None
        if grp is None:                              # machine not in a known group -> skip
            continue
        mi.machines.append(m)
        mi.machine_group[m] = grp
        mi.machine_allowed_skus[m] = sorted(m_alw[m])
        mi.machine_allowed_inches[m] = set(_MACHINE_ALLOWED_INCH_SET.get(m, set()))
        if m in _FLEX_MAP:                       # targeted inch-flexibility (env-gated)
            mi.machine_allowed_inches[m] |= _FLEX_MAP[m]
        # ── Dominance-ranked FULL flex (OPT_INCH_FLEX): allow every DB-allowable inch, ranked
        #    dominant-first so the model prefers the historical inch and pays a graded penalty
        #    to flex off it (long amortized campaigns, not thrash — the CO cost enforces that). ──
        _ranked = list(_MACHINE_DOMINANT_INCH_RANKED.get(m, []))
        if m in _PLANT_INCH and grp != "S1":         # PLANT-actual inch set + share-ranked (dominant-first)
            mi.machine_allowed_inches[m] = set(_PLANT_INCH[m])
            mi.machine_inch_rank[m] = {inch: i for i, inch in enumerate(_PLANT_INCH[m])}
        elif os.environ.get("OPT_INCH_FLEX", "0") == "1" and grp != "S1":  # no flex on Stage-1 carcass
            _db_inches = {mi.sku_inch.get(s, "") for s in m_alw[m]}
            _db_inches.discard("")
            # FULL DB-allowable inch flex: a machine may build ANY inch it is DB-allowable for
            # (the DB matrix is the physical truth; historical single-inch is only a policy). Ranked
            # dominant-first so the historical inch is preferred and flexing off it costs a diff-CO.
            # Deploys idle machines to any inch they can physically run -> max capacity, no DB violation.
            mi.machine_allowed_inches[m] = set(_db_inches) if _db_inches else set(_MACHINE_ALLOWED_INCH_SET.get(m, set()))
            _rank = {inch: i for i, inch in enumerate(_ranked)}  # dominant-first from plant report
            _nxt = len(_ranked)
            for _inch in sorted(mi.machine_allowed_inches[m]):   # remaining DB-allowable inches: higher rank
                if _inch not in _rank:
                    _rank[_inch] = _nxt; _nxt += 1
            mi.machine_inch_rank[m] = _rank
        elif _TARGET_FLEX_INCHES and grp != "S1" and (
                (m in _VMI_MACHINES and "VMI" in _TARGET_FLEX_GROUPS) or
                (m in _BJ_MACHINES  and "BJ"  in _TARGET_FLEX_GROUPS) or
                (m in UNISTAGE and "UNI" in _TARGET_FLEX_GROUPS)):
            # TARGETED scarce-inch flex: add ONLY the named scarce inches this machine is
            # DB-allowable for, ranked at the TAIL (low preference). Deploys idle capacity to the
            # short inches without the full-flex blowup. Base set is the narrow historical (l.244/246).
            _db_inches = {mi.sku_inch.get(s, "") for s in m_alw[m]}; _db_inches.discard("")
            _hist = set(mi.machine_allowed_inches[m])
            _adds = {i for i in _TARGET_FLEX_INCHES if i in _db_inches and i not in _hist}
            if _adds:
                mi.machine_allowed_inches[m] = _hist | _adds
                _order = [i for i in _ranked if i in _hist]      # historical, dominant-first
                for i in sorted(_hist):
                    if i not in _order: _order.append(i)
                for i in sorted(_adds):                            # scarce adds at the tail (low pref)
                    if i not in _order: _order.append(i)
                mi.machine_inch_rank[m] = {i: idx for idx, i in enumerate(_order)}
        if m in _DOM_OV and grp != "S1":             # FORCE an inch at a given rank (18" on an idle machine)
            _di, _dr = _DOM_OV[m]
            mi.machine_allowed_inches.setdefault(m, set()).add(_di)
            _order = [i for i, _ in sorted(mi.machine_inch_rank.get(m, {}).items(), key=lambda z: z[1])
                      if i != _di]
            _order.insert(min(_dr, len(_order)), _di)   # place _di at rank _dr in the existing order
            mi.machine_inch_rank[m] = {i: idx for idx, i in enumerate(_order)}
        _cok = _co_time_key(m)
        mi.co_same[m] = int(bc_config.BUILDING_CO_SAME_SIZE.get(_cok, 60))
        mi.co_diff[m] = int(bc_config.BUILDING_CO_DIFF_SIZE.get(_cok, 180))
        mi.pen_group[m] = _pen_group(m)
        for s in m_alw[m]:
            ct_sec = _bld_ct_sec(m, s)
            mi.build_rate[(m, s)] = int(SHIFT_MINS * 60 / ct_sec) if ct_sec > 0 else 0
    mi.machines.sort()

    # ---- curing presses (170 roster) ----
    roster = {str(p) for p in cetl.load_allowable_press_ids()}
    mi.presses = sorted(roster)
    ca_df = cetl.load_curing_allowable()             # SKUCode + Machines(list of presses)
    for r in ca_df.itertuples(index=False):
        sku = str(r.SKUCode).strip()
        if sku not in _demand_set:
            continue
        raw = r.Machines
        ps = raw if isinstance(raw, (list, tuple, set)) else str(raw).split(",")
        elig = sorted(str(p).strip() for p in ps if str(p).strip() in roster)
        if elig:
            mi.sku_presses[sku] = elig
    for p in mi.presses:
        mi.press_allowed_skus[p] = sorted(s for s, ps in mi.sku_presses.items() if p in ps)

    # ---- moulds (bipartite) ----
    elig = cetl.load_mould_eligibility()             # {"sku_moulds": {sku:set}, ...}
    sku_moulds = elig.get("sku_moulds", {})
    for s in mi.skus:
        ms = sorted(str(m) for m in sku_moulds.get(s, ()))
        mi.sku_moulds[s]  = ms
        mi.mould_pairs[s] = len(ms) // 2

    # ---- initial press state (running moulds snapshot) ----
    rm_df = cetl.load_running_moulds()               # [Machine, SKUCode, MouldNos, MouldLife_remaining, Num_Moulds]
    running = {}
    for r in rm_df.itertuples(index=False):
        pid = str(r.Machine).strip()
        running[pid] = (str(r.SKUCode).strip(),
                        [str(x) for x in (r.MouldNos or [])],
                        int(r.MouldLife_remaining) if r.MouldLife_remaining is not None else bc_config.MOULD_CLEAN_CYCLES)
    for p in mi.presses:
        sku, moulds, life = running.get(p, (None, [], bc_config.MOULD_CLEAN_CYCLES))
        mi.press_init_sku[p]    = sku if sku in _demand_set else None   # idle if its SKU has no demand
        mi.press_init_moulds[p] = moulds
        mi.mould_life[p]        = life
    # ABSENT presses (in the allowable roster but NOT in the running-moulds snapshot) have no moulds
    # mounted -> they can start a demand SKU FRESH on Day-1 Shift A with NO curing CO (a Runner-Out
    # press, which HAS moulds, still pays the CO to swap). Count -> a CO-free pool at t=0 in the model.
    mi.n_free_start = sum(1 for p in mi.presses if p not in running)

    # ---- opening inventories ----
    try:
        from curing_b2c import _load_opening_gt, _load_opening_carcass
        mi.opening_gt      = {str(k): float(v) for k, v in (_load_opening_gt(engine) or {}).items() if v}
        mi.opening_carcass = {str(k): float(v) for k, v in (_load_opening_carcass(engine) or {}).items() if v}
    except Exception as e:                            # opening inv is optional
        print(f"  [opt.data] opening inventory load failed ({e}); assuming 0")

    return mi


if __name__ == "__main__":
    # Smoke test: python -m optimizer.data  (month comes from bc_config / env)
    mi = load_model_inputs()
    print("ModelInputs:", mi.summary())
    # spot-checks
    _s = mi.skus[0]
    print(f"  sample SKU {_s}: demand={mi.demand[_s]} cure_rate={mi.cure_rate[_s]}/press/shift "
          f"inch={mi.sku_inch[_s]} pairs={mi.mould_pairs[_s]} presses={len(mi.sku_presses.get(_s,[]))}")
    _m = mi.machines[0]
    print(f"  sample machine {_m}: group={mi.machine_group[_m]} allowed_skus={len(mi.machine_allowed_skus[_m])} "
          f"inches={sorted(mi.machine_allowed_inches[_m])}")
    print(f"  build_rate entries={len(mi.build_rate)}  sku_presses={len(mi.sku_presses)}")
