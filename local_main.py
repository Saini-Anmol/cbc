"""
local_main.py — LOCAL Excel orchestrator (deployment Phase 3).

Runs the exact same engine as the cloud path, but reads demand from
bc_config.DEMAND_FILE and writes the Excel workbooks (today's behaviour).
This is the parity anchor: main.py (DB) must reproduce these KPIs on the
same inputs.

    python local_main.py
"""
from __future__ import annotations

import os as _os
# ── ADOPTED config — HYBRID CO (planned COScheduler + reactive layer) ──────────
# `python local_main.py` runs the HYBRID engine (the well-tested baseline; benefits from
# the end-of-horizon "+1 planning-day" boost). The pure-reactive Part-B engine is retained
# and can be turned on for A/B with the env vars below (REACTIVE_ONLY=1 + optional sub-toggles):
#   REACTIVE_ONLY=1   pure-reactive (no planned COs) + all levers (retarget-on-block,
#                     feed-guard relax, supply-gate, machine-swap, starvation switch, pre-position)
#   RCO_ARBITER=0     mid-shift base (best timing) | 1 = once-per-shift arbiter
#   RCO_STARV_SHIFTS=4  supply-aware starvation switch after N 0-GT shifts
#   RCO_PREPOS=1 / RCO_PREPOS_MAX=4   forward-looking pre-positioning
# setdefault → an explicit env override still wins for A/B.
_os.environ.setdefault("REACTIVE_ONLY", "0")     # HYBRID CO (default). Set 1 for pure-reactive.
# ADOPTED hybrid planned-CO fixes (items 1+2): defer-not-preempt + deficit-first building
# supply. +29,836 / 3 months, feasibility-clean. Item 3 (HYBRID_CO_CANCEL) and item 4
# (CURING_ADAPT_CO) are OFF (non-additive / no-op). Env overrides still win.
_os.environ.setdefault("HYBRID_CO_DEFER", "1")   # item 2 — defer a fulfillable, needed SKU's CO
_os.environ.setdefault("PERSKU_FEED_V2", "1")    # item 1 — deficit-first per-SKU building supply

import numpy as _np
import pandas as _pd

import bc_config
from b2c_pipeline import run_rolling_pipeline_2pass


def _build_deducted_demand() -> None:
    """Rebuild bc_config.DEMAND_FILE = DEMAND_BASE_FILE minus actual production.

    Runs automatically on every local run, so the daily routine is only:
    edit PLAN_START / PLANNING_DAYS / ACTUAL_PRODUCTION_FILE in bc_config, then
    `python local_main.py`.

    Production file: ONE ROW = ONE CURED TYRE (qty is the row count). Only two
    columns are read — `dtandTime` (production day = (dtandTime - 7h).date(),
    07:00-anchored) and `recipeID` (-> 18-char SKUCode via RECIPE_MASTER_FILE).
    `isCured` is filtered when present; extra columns are ignored.

    Deducts production days 1 .. (PLAN_START - 1), ONCE PER SKU, then re-splits
    the remainder across that SKU's duplicate demand rows (MTS/Replacement +
    MTO/OE) in proportion to their original Requirement — deducting per row
    would double-count. Floored at 0; non-Requirement columns preserved.
    Always reads the untouched BASE file, so re-running can never double-deduct.
    """
    prod_f = getattr(bc_config, "ACTUAL_PRODUCTION_FILE", "")
    base_f = getattr(bc_config, "DEMAND_BASE_FILE", "")
    rmap_f = getattr(bc_config, "RECIPE_MASTER_FILE", "")
    out_f  = bc_config.DEMAND_FILE
    start  = bc_config.PLAN_START

    if not (prod_f and base_f and rmap_f):
        print("[demand] ACTUAL_PRODUCTION_FILE / DEMAND_BASE_FILE / RECIPE_MASTER_FILE "
              "not all set — using DEMAND_FILE as-is.")
        return
    for _p, _n in ((prod_f, "ACTUAL_PRODUCTION_FILE"), (base_f, "DEMAND_BASE_FILE"),
                   (rmap_f, "RECIPE_MASTER_FILE")):
        if not _os.path.exists(_p):
            raise SystemExit(f"[demand] ERROR: {_n} not found: {_p}")
    if _os.path.abspath(base_f) == _os.path.abspath(out_f):
        raise SystemExit("[demand] ERROR: DEMAND_BASE_FILE == DEMAND_FILE — "
                         "refusing to deduct a file onto itself.")

    days = [d.strftime("%Y-%m-%d") for d in
            _pd.date_range(start.replace(day=1), start - _pd.Timedelta(days=1), freq="D")]

    dm = _pd.read_excel(base_f)
    dm["_s"] = dm["SKUCode"].astype(str).str.strip()
    dm["_o"] = _pd.to_numeric(dm["Requirement"], errors="coerce").fillna(0.0)

    if not days:                                    # 1st-of-month start: nothing produced yet
        dm.drop(columns=["_s", "_o"]).to_excel(out_f, index=False)
        print(f"[demand] PLAN_START is the 1st — no production to deduct; "
              f"{_os.path.basename(out_f)} = base ({int(dm['_o'].sum()):,}).")
        return

    rm = _pd.read_csv(rmap_f)
    rm["_c"] = rm["description"].astype(str).str.strip().str.upper()
    rm = rm[rm["_c"].str.len() == 18]
    id2sku = dict(zip(rm["iD"].astype(int), rm["_c"]))

    lg = _pd.read_excel(prod_f)
    n_raw = len(lg)
    if "isCured" in lg.columns:                     # tolerate raw OR pre-filtered
        lg = lg[lg["isCured"] == True]
    for _c in ("dtandTime", "recipeID"):
        if _c not in lg.columns:
            raise SystemExit(f"[demand] ERROR: production file missing column '{_c}': {prod_f}")

    lg = lg[["dtandTime", "recipeID"]].copy()
    lg["_day"] = (_pd.to_datetime(lg["dtandTime"], errors="coerce")
                  - _pd.Timedelta(hours=7)).dt.date.astype(str)
    lg = lg[lg["_day"].isin(days)]
    lg["_sku"] = lg["recipeID"].map(id2sku)

    bad = lg[lg["_sku"].isna()]
    if len(bad):
        print(f"[demand] !! {len(bad)} row(s) DROPPED — recipeID not in "
              f"{_os.path.basename(rmap_f)}: {sorted(bad['recipeID'].dropna().unique().tolist())}")
        print(f"[demand] !! their production is NOT deducted — add them to the recipe master.")
    lg = lg.dropna(subset=["_sku"])
    prod = lg.groupby("_sku").size().to_dict()

    tot   = dm.groupby("_s")["_o"].transform("sum")
    ded   = dm["_s"].map(prod).fillna(0.0)
    rem   = (tot - ded).clip(lower=0)
    share = _np.where(tot > 0, dm["_o"] / tot, 0.0)
    dm["Requirement"] = _np.floor(rem * share + 0.5).astype(int)
    dm.drop(columns=["_s", "_o"]).to_excel(out_f, index=False)

    # ── audit trail: dated per-SKU record of what was ACTUALLY deducted ────────
    # BTP_SEPT26_DEMAND_deducted.xlsx is overwritten every run, so this file is the
    # only record of which SKU lost how much on a given plan date. Deducted is taken
    # as (base - new) per SKU, i.e. what the demand file really lost after the
    # floor-at-0 and the proportional re-split across duplicate rows.
    _aud = (dm.groupby("_s")
              .agg(_base=("_o", "sum"), _new=("Requirement", "sum"))
              .reset_index())
    _aud["Qty"] = (_aud["_base"] - _aud["_new"]).round().astype(int)
    _aud = (_aud[_aud["Qty"] > 0][["_s", "Qty"]]
            .rename(columns={"_s": "SKU Code"})
            .sort_values("Qty", ascending=False))
    _adir = _os.path.join(bc_config.OUTPUT_DIR, "deducted")
    _os.makedirs(_adir, exist_ok=True)
    _apath = _os.path.join(_adir, f"deducted_{start.strftime('%Y-%m-%d')}.xlsx")
    _aud.to_excel(_apath, index=False)

    # produced MORE than the SKU's whole demand -> the excess is DISCARDED (floored at 0).
    _base_tot = dm.groupby("_s")["_o"].sum().to_dict()
    _over = {k: (v, _base_tot[k]) for k, v in prod.items()
             if k in _base_tot and v > _base_tot[k]}

    o, n = int(dm["_o"].sum()), int(dm["Requirement"].sum())
    miss = sorted(set(prod) - set(dm["_s"]))
    print(f"[demand] {_os.path.basename(prod_f)}: {n_raw:,} rows -> {sum(prod.values()):,} units "
          f"over {len(prod)} SKUs, production days {days[0]}..{days[-1]}")
    print(f"[demand] per day: {lg.groupby('_day').size().to_dict()}")
    print(f"[demand] {_os.path.basename(base_f)} {o:,} -> {_os.path.basename(out_f)} {n:,} "
          f"(deducted {o - n:,})")
    print(f"[demand] audit -> {_apath}  ({len(_aud)} SKUs, {int(_aud['Qty'].sum()):,} units)")
    if miss:
        print(f"[demand] note: {len(miss)} produced SKU(s) absent from demand "
              f"({sum(prod[k] for k in miss):,} units, nothing to deduct): {miss}")
    if _over:
        print(f"[demand] note: {len(_over)} SKU(s) produced MORE than their whole demand "
              f"(excess DISCARDED, floored at 0):")
        for _k, (_p, _d) in sorted(_over.items(), key=lambda x: x[1][0] - x[1][1], reverse=True):
            print(f"[demand]        {_k}  produced {int(_p):,} vs demand {int(_d):,} "
                  f"-> {int(_p - _d):,} discarded")


def main() -> dict:
    _build_deducted_demand()
    # 2pass wrapper: for a mid-month PLAN_START (day != 1) with MIDMONTH_DEDUCT=1 it runs a
    # full-month simulation first, deducts already-produced tyres from demand, then plans the
    # remaining period. day==1 or toggle OFF → single run, identical to before.
    result = run_rolling_pipeline_2pass()  # bc_config defaults (demand, plan_start, days)
    print("\n" + "=" * 60)
    print("  LOCAL RUN COMPLETE (Excel path)")
    print("=" * 60)
    print(f"  GT built   : {result['total_built']:>10,.0f}")
    print(f"  GT cured   : {result['total_cured']:>10,.0f}")
    print(f"  Coverage   : {result['demand_coverage']:>9.1f}%")
    print(f"  Curing COs : {result['n_co']:>10,}  "
          f"(planned {result['n_co_planned']} + dynamic {result['n_co_dynamic']})")
    print(f"  Building   : {result['build_output']}")
    print(f"  Curing     : {result['curing_output']}")
    return result


if __name__ == "__main__":
    main()
