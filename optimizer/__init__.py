"""Global full-month CP-SAT optimizer for the JK Tyre BTP B2C scheduler.

10-day joint building+curing windows, rolling (receding) horizon, deterministic,
<=30 min, cloud-ready. Toggle GLOBAL_OPT (OFF = current greedy engine bit-for-bit).

Modules:
  data   — assembles model inputs from the existing ETLs (reused, not re-derived)
  model  — the single-window joint CP-SAT model (building + curing together)
  driver — the rolling driver (chains windows via carry-over state)
  writer — plan -> output-schedule + mould-audit gate

See approach/bc.md and the plan file for the full design.
"""
