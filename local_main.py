"""
local_main.py — LOCAL Excel orchestrator (deployment Phase 3).

Runs the exact same engine as the cloud path, but reads demand from
bc_config.DEMAND_FILE and writes the Excel workbooks (today's behaviour).
This is the parity anchor: main.py (DB) must reproduce these KPIs on the
same inputs.

    python local_main.py
"""
from __future__ import annotations

from b2c_pipeline import run_rolling_pipeline


def main() -> dict:
    result = run_rolling_pipeline()  # bc_config defaults (demand, plan_start, days)
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
