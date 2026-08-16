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

import bc_config
from b2c_pipeline import run_rolling_pipeline


def main() -> dict:
    # LOCAL-ONLY press-roster restriction (default OFF). When
    # bc_config.RESTRICT_PRESSES_TO_ALLOWABLE is ON, only the 170 presses in the
    # allowable matrix are used; extra running-moulds presses are dropped. The
    # cloud path (main.py) never passes this, so cloud is unaffected.
    result = run_rolling_pipeline(  # bc_config defaults (demand, plan_start, days)
        restrict_to_allowable_presses=bc_config.RESTRICT_PRESSES_TO_ALLOWABLE,
    )
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
