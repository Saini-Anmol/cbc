"""
local_main.py — LOCAL Excel orchestrator (deployment Phase 3).

Runs the exact same engine as the cloud path, but reads demand from
bc_config.DEMAND_FILE and writes the Excel workbooks (today's behaviour).
This is the parity anchor: main.py (DB) must reproduce these KPIs on the
same inputs.

    python local_main.py
"""
from __future__ import annotations

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
