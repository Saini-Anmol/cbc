# Just confirm that new table in db- building allowable machines is got updated succesfully and now for more SKU will be eligible on the VMI machines in allowable matrix. 
# Now, I have updated the building allowable machines data from the plant, Now, i think we should get the better KPIs in the building side so accordingly curing side KPIs should also get better automatically. But, now i am getting the output KPIs around- GT built= 609507 and curing 610955, from the same code we are able to generate GT of around 615535 and curing qty around- 616626. Why our KPIs got decreased now. So, where we are making mistake now. We only have to work on this- update in the inch size locking to machine group/machine? Anything else needed or some issues in the logic as well. Now, I have added 2 more SKU in the demand file- 1. "1D25212812074FXC10	28089	0.040417398	and 1325215813079TTMX0	12521	0.018016527	[sku, qty, prioirty_score]. And, after adding more than 40k qty we are getting the KPIs as building- 615,533 and 617,359. Now, why we laggin in KPIs now? Find the Root cause for the same. 


"""
b2c_pipeline.py — End-to-end B2C scheduling pipeline.

Two modes:
  LEGACY (run_pipeline):
    Step 1: Curing Consumption (dynamic) — 31-day CO schedule
    Step 2: Building Scheduler (B2C)     — 31-day LP building plan
    Step 3: Curing Schedule (B2C)        — shift-wise curing from GT output

  ROLLING (run_rolling_pipeline):   <-- NEW DEFAULT
    Pre-compute: CO schedule + master data (once)
    For each Day D (1..31):
      1. Compute curing demand from actual press_state
      2. Greedy building assignment (projected GT — Option B)
      3. Per-shift: add building to GT inventory FIRST, then cure min(capacity, gt_available)
      4. GT shelf-life writeoff at end of day
      5. Apply CO transitions

    Output: SAME Excel files and sheet names as the legacy pipeline:
      Building → bc_building_schedule_{date}.xlsx
        Sheets: Shift Schedule | Changeover Plan | SKU Classification |
                Daily GT & Carcass | Demand Fulfillment (B2C)
      Curing  → bc_curing_b2c.xlsx
        Sheets: Demand Fulfillment | Machine Utilization | Shift Schedule |
                Mould Tracker | Machine Schedule | Daily Cured tyres | GT Gap Diagnostic

All parameters are read from bc_config.py — edit there, not here.

Usage:
    python b2c_pipeline.py                           # rolling pipeline (new default)
    python b2c_pipeline.py data/input/demand_may.xlsx
    python b2c_pipeline.py --legacy                  # run old 31-day LP pipeline
"""

import os
import math
import sys
import tempfile
from collections import defaultdict, Counter
from datetime import datetime, timedelta

import pandas as pd

import cbc_env
from curing_consumption_dynamic import (
    run_dynamic_consumption, ConsumptionConfig, COScheduler, _SURPLUS_RELEASE_ENABLED,
)
from building_b2c import run_from_database_b2c
from curing_b2c import run_curing_b2c
from curing_consumption import ConsumptionETL

# ── All params from bc_config (single source of truth) ────────────────────────
from bc_config import (
    PLAN_START,
    PLANNING_DAYS,
    DEMAND_FILE,
    GT_SHELF_LIFE_DAYS,
    MAX_ENDOFDAY_GT_INVENTORY,
    MOULD_CLEAN_CYCLES,
    MOULD_CLEAN_MINS,
    CURING_CO_CHANGEOVER_MINS,
    MAX_CHANGEOVERS_PER_DAY,
    MIN_CAMPAIGN_MINS,
    MIN_CAMPAIGN_UNITS,
    BUILD_LEAD_SHIFTS,
    MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT,
    GT_BUFFER_SHIFTS,
    BUILDING_CO_SAME_SIZE,
    BUILDING_CO_DIFF_SIZE,
    SHIFT_MINS,
    SHIFT_STARTS,
    POOL_SIZE,
    STARVATION_BUFFER_MINS,
    CO_CLASS_B_THRESHOLD,
    DYNAMIC_CC_OUTPUT  as CC_OUTPUT,
    BUILDING_OUTPUT    as BUILD_OUTPUT,
    CURING_B2C_OUTPUT  as CURING_OUTPUT,
)

# Optional env override for the daily curing-CO cap — lets us sweep it (e.g. 8-13)
# without editing bc_config. Unset ⇒ the committed bc_config value.
if os.environ.get("MAX_CO"):
    MAX_CHANGEOVERS_PER_DAY = int(os.environ["MAX_CO"])

# Optional OUT_TAG — suffix all output files so parallel runs don't clobber each
# other (used for concurrent CO-cap sweeps). Unset ⇒ the normal output paths.
if os.environ.get("OUT_TAG"):
    _ot = os.environ["OUT_TAG"]
    BUILD_OUTPUT  = BUILD_OUTPUT.replace(".xlsx", f"_{_ot}.xlsx")
    CURING_OUTPUT = CURING_OUTPUT.replace(".xlsx", f"_{_ot}.xlsx")
    CC_OUTPUT     = CC_OUTPUT.replace(".xlsx", f"_{_ot}.xlsx")

# ── Machine group map ─────────────────────────────────────────────────────────
# NOTE: these values are INTERNAL LOGIC KEYS — they are compared against string
# literals throughout (_S1_MACHINES, _buf_of, _co_cost, _pri, captive-max, the
# Stage-1 carcass step) AND used as dict keys into bc_config's
# BUILDING_CO_SAME_SIZE / BUILDING_CO_DIFF_SIZE. Do NOT rename them.
# For plant-friendly names in the output sheets, edit _MACHINE_GROUP_DISPLAY below.
_MACHINE_GROUP: dict[str, str] = {}
for _m in ("6001","6002","6003","6004","7001","7002","7003","7004"):
    _MACHINE_GROUP[_m] = "VMI"
for _m in ("7101","7102","7103","7104","7105","7106","7201"):
    _MACHINE_GROUP[_m] = "BJ"
for _m in ("7501","7502","7503"):
    _MACHINE_GROUP[_m] = "UNISTAGE"
for _m in ("8201","8301","8302","8501","8502","7301"):
    _MACHINE_GROUP[_m] = "STAGE2"
for _m in ("6801","6802","6803","6909","6911","7601","7701",
           "7801","7802","7803","7804","8001","8002","8003","8101"):
    _MACHINE_GROUP[_m] = "STAGE1"

# Plant-facing display labels — used ONLY for the Machine_Group column in the
# building Shift Schedule output sheet. Never used in scheduling logic, so these
# are safe to rename freely.
_MACHINE_GROUP_DISPLAY: dict[str, str] = {
    "VMI":      "VMIMAXX GROUP",
    "BJ":       "BJ GROUP",
    "UNISTAGE": "UNISTAGE GROUP",
    "STAGE2":   "TBM STAGE2",
    "STAGE1":   "TBM STAGE1",
}


def _group_label(machine: str) -> str:
    """Plant-facing group name for output sheets (falls back to the internal key)."""
    g = _MACHINE_GROUP.get(str(machine), "")
    return _MACHINE_GROUP_DISPLAY.get(g, g)


_S1_MACHINES = frozenset(m for m, g in _MACHINE_GROUP.items() if g == "STAGE1")

# Round-trip buffer sizing: when a machine alternates between its current SKU
# and another live, unfulfilled SKU, the buffer left behind for the current
# SKU must survive CO(cur->partner) + partner's own dwell time + CO(partner->cur),
# not just a flat GT_BUFFER_SHIFTS multiplier. Skipped entirely (falls back to
# the flat buffer) when the machine has only one eligible SKU, when no other
# eligible SKU has unmet demand, or when no other eligible SKU currently has a
# real curing-driven deficit — see _assign_building_shift.
_ROUND_TRIP_BUFFER_ENABLED = True

# Building-side RI-first-then-ratio: NRI (press_count<=0) candidates ranked by
# static demand[sku]/machine_total_demand[machine] instead of raw deficit; RI
# candidates (any live press) keep raw-deficit ranking unchanged and always
# sort ahead of NRI (tier 0 vs tier 1) — see _priority_tier.
_BUILDING_RATIO_ENABLED = True

# ── EXPERIMENT: RI-by-ratio (two approaches, both default OFF = bit-for-bit) ────
# User request: assign RI SKUs by the max-ratio formula instead of raw deficit.
# Approach 1 (_RI_RATIO_ENABLED): ranking-only. In _priority_tier, RI candidates
#   (live press) are ranked by static demand[sku]/machine_total_demand[machine]
#   (same formula as NRI) instead of raw curing-deficit. RI still outranks NRI
#   (tier 0 vs 1); only the WITHIN-RI order changes. Machine order + Campaign
#   loop unchanged. NOTE: drops the live deficit (starvation-urgency) signal for
#   RI, so this may raise starvation — that is what the experiment measures.
# Approach 2 (_RI_RATIO_GLOBAL): drop the fixed VMI->BJ->US->Stage2 machine order
#   and instead process machines in DESCENDING order of the best (highest) ratio
#   deficit-SKU each can serve, so the ratio — not the group order — drives which
#   machine-SKU combination is claimed first. Stage-1 stays last. Implies the
#   ratio ranking (auto-enables Approach 1's _priority_tier branch for RI).
_RI_RATIO_ENABLED = os.environ.get("RI_RATIO") == "1"
_RI_RATIO_GLOBAL  = os.environ.get("RI_RATIO_GLOBAL") == "1"

# EXPERIMENT: seed each building machine's starting SKU from the plant's ACTUAL
# running-machine snapshot (data/running_prod/building_running_machines_39_near7AM.xlsx)
# instead of the DB loader (which returns empty here -> machines start blank and
# the scheduler derives its own machine->SKU). Off = current behaviour (blank/DB).
_SEED_FROM_PLANT_RUNNING = os.environ.get("PLANT_SEED") == "1"
_PLANT_RUNNING_FILE = "data/running_prod/building_running_machines_39_near7AM.xlsx"

# EXPERIMENT: captive-first ordering. A captive building machine (eligible for
# exactly ONE SKU, e.g. 7301 -> only LSTL0) sits idle whenever flexible machines
# processed earlier in the VMI->BJ->US->Stage2 order drain that SKU's deficit
# first. Promoting captives to the FRONT of the machine loop lets them claim
# their sole SKU before flexible machines, maxing captive utilisation and freeing
# the flexible (oversubscribed) machines for SKUs only they can serve. Stage-1
# captives are NOT promoted (carcass, always last). Off = current behaviour.
# _CAPTIVE_FIRST_ENABLED = os.environ.get("CAPTIVE_FIRST") == "1"
_CAPTIVE_FIRST_ENABLED = True

# EXPERIMENT: global machine-SKU scoring assignment. Replaces the sequential
# VMI->BJ->US->Stage2 per-machine greedy with ONE global pass: after each machine
# continues its current SKU (Phase A, no CO), all remaining (machine,SKU) pairs are
# scored together and assigned best-first (Phase B). The score includes
# constraint = min(flex_machine, flex_sku), so a captive machine (e.g. 7301) OR a
# sole-supplier SKU wins WITHOUT any hardcoded rule — symmetric on both sides,
# unlike the machine-only _SCARCITY_ORDER that regressed. When on, it early-returns
# before the captive-first re-sort (so it fully supersedes it). Off = per-machine greedy.
_GLOBAL_ASSIGN_ENABLED  = True
# constraint placement in the pair sort key: "above" (constraint before SKU tier),
# "below" (after tier), or "captive" (only min(flex)<=1 boosted). Sensitivity knob.
_GLOBAL_CONSTRAINT_MODE = "below"

# EXPERIMENT (global branch only): force a CAPTIVE machine (exactly 1 eligible SKU,
# e.g. 7301 -> LSTL0) to build its sole SKU at FULL shift capacity in Phase A —
# capped ONLY by the demand cap, not by the curing-demand buffer — so it never
# idles while its SKU still has unmet demand. Remaining demand is filled by the
# normal global logic on other machines. Off = buffer-throttled (current global).
# Flip this line True/False to turn captive-max on/off (env CAPTIVE_MAX also works):
# _CAPTIVE_MAX_ENABLED = os.environ.get("CAPTIVE_MAX") == "1"
_CAPTIVE_MAX_ENABLED = True

# EXPERIMENT: End-of-day 12k total GT-inventory cap (hard plant constraint).
# Plant can hold at most MAX_ENDOFDAY_GT_INVENTORY units of GT overnight (summed
# over all SKUs). Enforced PROACTIVELY in the build-room calc (never build past the
# ceiling) — see _defc / captive-max _room. An end-of-day audit column records the
# actual total so we can confirm it never violates. Off = no cap (current behaviour).
# env GT_CAP=0 forces OFF for the bit-for-bit baseline check; default ON.
_ENDOFDAY_GT_CAP_ENABLED = True

# EXPERIMENT: Forward-buffer level-loading. Use idle building capacity (weeks 4-5 run
# at 51-56% util while presses starve) to pre-build a SHELF-LIFE-SAFE forward buffer
# for SKUs that WILL be cured in the next 3 days. Phase C (slack-fill) runs after
# Phase A/B: for a machine that would otherwise idle, build MORE of its most
# starvation-prone eligible SKU (must have a LIVE cure-draw shift_cure_demand>0 — a
# press is actively pulling it — and demand remaining), up to min(demand_remaining,
# 3-day cure-draw) and bounded by the 12k cap. Builds only REQUIRED GT, never random
# SKUs. Auto-targets building-limited SKUs, auto-skips press-limited ones. Off = idle.
# env FWD_BUF=0 forces OFF; default ON.
_FORWARD_BUFFER_ENABLED = True

# Starvation-risk gate for the forward buffer: only pre-build a SKU whose on-hand GT
# is BELOW this many shifts of its live cure-draw (i.e. it is about to starve). This
# stops early-month front-loading (weeks 1-3 machines have slack but presses are NOT
# starving — building keeps up), so the forward buffer fires mainly where presses
# actually run dry (weeks 4-5), lifting the tail without pulling demand forward.
# Higher = fires more aggressively (more front-load); lower = only near-starvation.
# 0 ⇒ gate off (fill whenever slack+demand exist, the pure front-loading behaviour).
_FWD_RISK_SHIFTS = True

# Shelf-life expressed in shifts (3 days x 3 shifts/day = 9). Forward-buffer never
# pre-builds a SKU beyond this many shifts of its live cure-draw (so it cannot age
# out to writeoff within the shelf window).
GT_SHELF_LIFE_SHIFTS = GT_SHELF_LIFE_DAYS * 3

# BUSINESS RULE: curing-press mould clean. After every MOULD_CLEAN_CYCLES cycles
# (= 6,000 tyres) a press takes an 8h (MOULD_CLEAN_MINS = 480 = 1 shift) mould clean
# during which it produces nothing; mould life then resets. A curing CO also resets
# mould life (the CO already includes a clean). env MOULD_CLEAN=0 disables → the
# pre-mould-clean 690,180 baseline reproduces bit-for-bit.
_MOULD_CLEAN_ENABLED = True

# ── MOULD→SKU AVAILABILITY GATE (client hard rule) ────────────────────────────
# A press can only run/CO to an SKU if it physically has 2 eligible moulds mounted
# (Master_Mapping_Mould_SKU). Inventory = 1,284 physical moulds, one copy each; a
# mould serves one press at a time (contention). Mounting a spare is free; mould
# movement rides the existing 480-min press CO (no extra time). Mould life stays
# per-press 3,000 for v1 (real per-mould life is v2).
# Default ON — this makes the plan physically real (a moderate KPI drop is the new
# baseline). MOULD_GATE=0 reproduces the current mould-blind engine bit-for-bit.
_MOULD_GATE_ENABLED = True

# Phase 2 mould optimisation (raise the Phase-1 contention baseline). Only meaningful
# when the gate is ON. Two levers, both toggle-gated by MOULD_OPT:
#   (a) scarce-first ordering — when several presses CO the same day, allocate moulds
#       to the SCARCEST new-SKU first (fewest eligible moulds), so a 2-mould SKU is
#       not blocked by a 6-mould SKU grabbing a shared mould first.
#   (b) retarget-on-block — a planned CO whose mould claim fails does NOT just idle on
#       its (usually demand-done) old SKU; it retargets to the most-needy eligible SKU
#       that still HAS 2 free moulds, recovering the wasted CO slot.
# MOULD_OPT=0 → pure Phase-1 gate (scheduling identical to the locked mould baseline).
_MOULD_OPT_ENABLED = True

# Phase 3 — Unified CO scorer. Replaces the three ad-hoc changeover paths (static
# planned COs, per-press retarget-on-block, mid-shift dynamic CO) with ONE scoring
# function that ranks {execute planned, pull-forward tomorrow's planned, dynamic,
# retarget, idle} per press and solves them as a GLOBAL greedy over the shared
# resources (mould pool + a quantitative building-feed estimate + daily CO cap).
# Default ON — measured ≥ Phase-2 on all 3 months (May +3,780, June +2,261, July +2,987;
# all exact-mould-audit PASS, deterministic, demand cap holds). CO_SCORER=0 → Phase-2 path.
_CO_SCORER_ENABLED = True
# Sub-flag (measured rollout): False = ADDITIVE (planned COs always kept; scorer only
# fills idle presses + pulls forward tomorrow's planned COs) — the shipped mode.
# True = FULL RE-OPT (planned COs may be cancelled/replaced) — MEASURED WORSE (utility
# picker churns and drops good planned COs, e.g. May −40k), left OFF.
_SCORER_FULL_REOPT = os.environ.get("SCORER_FULL", "0") != "0"

# BUSINESS RULE: spread planned curing COs across shifts A/B/C.
# Planned COs were hardcoded to Shift A (all 147), so 97% of changeover downtime
# landed in Shift A and its curing output sat ~6.6k below Shift B — an artifact, not
# plant physics. Real plants change over across all three shifts, firing a CO as soon
# as a press finishes its current SKU (subject to the daily cap). With this ON, each
# planned CO is placed in the shift where its press is projected to exhaust its old
# SKU — Shift A if it is ALREADY finished, so a free press never waits — falling back
# to Shift A when the SKU will not finish today (the static scheduler booked that CO
# for a reason: a preemptive Class-A move). Flip to False to disable (all COs → Shift A).
_CO_SHIFT_SPREAD_ENABLED = True

# Reversed machine processing order: Stage2 -> Unistage -> BJ -> VMI instead of
# today's VMI -> BJ -> Unistage -> Stage2. Tests whether scarce/inch-locked
# groups should claim their deficit signal before flexible VMI machines mop up
# residual demand last.
_MACHINE_ORDER_REVERSED = False

# Scarcity-first machine ordering: process machines with the FEWEST eligible
# SKUs first, so captive/specialized machines (e.g. 7301, eligible only for
# LSTL0) claim their specific SKU's deficit before flexible machines that
# share that SKU but have many other options poach it. Complements the ratio
# formula (which ranks WHICH SKU a machine picks); this fixes WHICH MACHINE
# claims a shared SKU first. Fixes the 7301-at-7%-util problem generally.
_SCARCITY_ORDER_ENABLED = False

# Starvation-feed: when a machine would otherwise IDLE and an eligible SKU has
# starving presses (curing demand this shift but zero GT), let the machine take
# the CO to feed it as a LAST RESORT. Inch preference is FULLY preserved — the
# starving SKU stays low priority (same/diff bucketing + inch_penalty unchanged),
# so a machine only feeds an off-inch starving SKU when it has no same-inch work
# left. Only the 30%-CO-cost guard is relaxed for starving SKUs (generalizing
# the existing curing-CO-target bypass to ANY starving SKU); MIN_CAMPAIGN
# feasibility and the demand cap stay enforced, so no overbuild, no forced
# infeasible CO. General case — all machines, all SKUs.
_STARVATION_FEED_ENABLED = False

# Reactive "instant CO" mechanism (dynamic_co_tracker): when a curing press
# finishes its SKU's demand mid-month, it is immediately changed over to a new
# needed SKU. Default True = current behaviour ("dynamic consumption": upfront
# pre-planned Phase-0 schedule PLUS reactive COs). Set False for a "static
# consumption" run where curing follows ONLY the pre-planned schedule.
_DYNAMIC_CO_TRACKER_ENABLED = True

# Dynamic per-day curing CO planner: replaces the static, upfront 31-day CO
# schedule (COScheduler's proxy-simulated demand drain) with a fresh decision
# made once per day, before Shift A, using REAL live state (demand_remaining,
# press_state, gt_inventory) — same real-state discipline as the existing
# reactive dynamic_co_tracker mechanism, extended to the whole press fleet.
# COScheduler/co_by_day stays completely untouched when this is off.
# ABANDONED — a purely reactive planner (no lookahead) regressed severely
# (670k->~594k GT, 96.7%->~86% coverage) across three attempts; root cause:
# it can only redistribute capacity from presses already idle today, never
# proactively reassign a press ahead of a deadline the way a whole-horizon
# simulation can. Code kept in place, toggle permanently off. Superseded by
# _ROLLING_HORIZON_CO_ENABLED below.
_DYNAMIC_CO_PLANNER_ENABLED = False

# Nested sub-toggle (only meaningful when _DYNAMIC_CO_PLANNER_ENABLED=True):
# adds sku_campaign_tier (building's primary/secondary/tertiary campaign
# position for each SKU) as a tiebreak in CO target ranking — a SKU with
# committed primary-campaign building capacity is a safer CO target than one
# only produced as an opportunistic secondary/tertiary blip. Day-granularity,
# tiebreak-only role (never a primary gating signal) — avoids the live
# per-shift-signal thrashing failure mode seen earlier this session.
_CAMPAIGN_TIER_TIEBREAK_ENABLED = False

# Rolling-Horizon COScheduler: reuses the existing, proven COScheduler.schedule()
# (the artifact behind the 670,431/670,649/96.7% baseline) as a receding-horizon
# planner — called once per simulated day, seeded from live rolling-pipeline
# state (press_state/press_count/demand_remaining), with a SHRINKING remaining
# horizon (Day 1 plans 31 days ahead, Day 10 plans 22, ...). Only that call's
# relative-day-1 events are kept; the rest of its lookahead is discarded and
# recomputed fresh tomorrow. The scheduling ALGORITHM is not rewritten — only
# its inputs change per call. See plan file for the full design rationale.
_ROLLING_HORIZON_CO_ENABLED = False

# Ratio-based curing CO allocation: no proactive CO at all — a press keeps
# producing its current SKU until that SKU's demand is FULLY exhausted (no
# urgency/Class A/B/horizon-threshold early CO). When a press frees up, pick
# the highest-static-ratio compatible SKU that still needs more press
# capacity to finish within the remaining horizon (a "presses needed" tracker
# skips SKUs that already have enough). Building scheduler untouched. Reuses
# the existing reactive trigger site (press's demand hits 0, mid-shift) —
# only the SELECTION function changes; co_by_day is emptied so nothing
# proactive from the static schedule fires under this toggle.
_RATIO_CO_ALLOCATION_ENABLED = False

# Nested sub-toggle (only meaningful when _RATIO_CO_ALLOCATION_ENABLED=True):
# enriches _select_ratio_co_target's ranking with COScheduler's other four
# levels (Class A/B, after_days, cycle time, target-side scarcity) on top of
# ratio, testing whether a richer ranking recovers ground among whatever
# candidates are ALREADY eligible right now. Does NOT touch eligibility
# timing — a press still only frees up once its current SKU is fully done,
# exactly as specified; this only changes which SKU wins once it's free.
_RATIO_CO_RICH_RANKING_ENABLED = False

# Early-CO (dynamic consumption via surplus-press reassignment): the one lever
# every prior dynamic attempt left unpulled. All of them waited for a press's
# demand to hit ZERO before reassigning, so COs clustered late and regressed.
# The static approach wins because its proxy sim frees presses EARLY. This adds
# that: a press may CO away from its current SKU BEFORE its demand is complete,
# IFF that SKU is already on track to finish with its REMAINING presses (n-1)
# within the horizon — i.e. this press is surplus to it. The next SKU is chosen
# by the same max-ratio formula as building (via _select_ratio_co_target), and
# only fires if a genuinely under-served target exists. Builds ON TOP of the
# current hybrid (static co_by_day base + reactive layer) — it can only ADD COs,
# so it has a real shot at beating baseline rather than replacing what works.
_EARLY_CO_ENABLED = False

# ── Building machine CT (seconds/unit) ────────────────────────────────────────
_BLD_CT_SEC: dict[str, float] = {
    "7001":51.6,  "7002":52.6,  "7003":56.0,  "7004":53.0,
    "6001":53.0,  "6002":52.0,  "6003":73.8,  "6004":60.0,
    "7101":83.0, "7102":86.0, "7103":60.0,  "7104":87.0,
    "7105":60.0, "7106":60,  "7201":70.0,
    "7501":90.0, "7502":90.0, "7503":90.0,
    "8201":62.0,  "8301":60.0,  "8302":60.0,
    "8501":70.0, "8502":70.0, "7301":70.0,
    "6801":127,   "6802":146,   "6803":146,
    "6909":157,   "6911":115,   "7601":186,
    "7701":163,   "7801":135,   "7802":135,
    "7803":135,   "7804":135,   "8001":113,
    "8002":113,   "8003":113,   "8101":230,
}

DEFAULT_CURING_CT = ConsumptionConfig.DEFAULT_CYCLE_TIME_MIN
CURING_CAVITIES   = 2

# ── Dominant inch per building machine ────────────────────────────────────────
# Used to prioritise same-dominant-inch SKUs when choosing campaigns/COs.
# When a machine's dominant-inch SKUs have no deficit, it can build other inches
# per its allowable list — but dominant inch is always tried first.
_MACHINE_DOMINANT_INCH: dict[str, str] = {
    "6001": "14", "6002": "15", "6003": "17", "6004": "16",
    "7001": "16", "7002": "14", "7003": "15", "7004": "14",
    "7101": "15", "7102": "15", "7103": "13", "7104": "15",
    "7105": "13", "7106": "13", "7201": "16",
    "7501": "12", "7502": "13", "7503": "13",
}

# Machines removed from _HARD (soft-locked): serve non-dominant inch ONLY when primary demand done.
# All other machines (BJ etc.) were never hard-locked; their Campaign 2+ inch freedom is unchanged.
_SOFT_LOCK_MACHINES: frozenset[str] = frozenset({"7001", "7003"})

# ── Inch-flexibility (_INCH_FLEX_ENABLED) ──────────────────────────────────────
# Generalizes the 7001/7003 soft-lock to the remaining HARD-locked machines so
# they don't sit idle once their own inch is met — plant data proves these run
# 4-7 inches. When off (empty flex set) the scheduler runs bit-for-bit baseline.
# UNI_NARROW (7501-7503) is a separate sub-toggle: CLAUDE.md says they can't
# physically run 14"+, so it's independently back-out-able.
_INCH_FLEX_ENABLED            = True   # master
_INCH_FLEX_INCLUDE_UNI_NARROW = False   # add 7501-7503 (trust DB allowable)
# Off-inch candidate ordering (tiebreak WITHIN the off-inch bucket only — never
# overrides inch preference). "starving_first" = feed urgent starving presses
# first; "demand_first" = largest remaining demand first (best amortization of
# the expensive diff-inch CO). Tested both ways — see plan verification.
_INCH_FLEX_OFFINCH_ORDER      = "starving_first"
_INCH_FLEX_EXTRA_COS          = 2   # extra building-CO budget for flex machines (off-inch excursions)

# ── CLIENT INCH RULES (hard plant rules on building inch movement) ────────────
# Rule 1 — one-way inch movement. A machine may take a diff_size_CO only when the
#          demand it can still serve at its CURRENT inch is finished, and it may
#          NEVER return to an inch it has already left (14->15->14 illegal;
#          14->15->13 legal, 13 was never used).
# Rule 2 — +/-2 band. The machine's inch must stay within anchor +/- 2 for the
#          whole month (anchor 14" => 12".."16"; 14"->17" illegal).
#
# Anchor = the inch of the machine's FIRST assignment. There is no Day-0 building
# state to anchor on (machine_current_sku starts empty; TBMStage1/2_ProductionEventData
# are both empty), so the first SKU the scheduler assigns fixes the band.
#
# These are RESTRICTIONS: they cut expensive diff-size COs (freeing production
# minutes) but remove flexibility (some SKUs become unreachable). Net KPI effect
# is measured, not assumed. OFF reproduces the previous behaviour bit-for-bit.
# DEFAULT OFF — the inch rules are a SEPARATE, parked, not-yet-approved plan. They
# must not be live for the mould baseline (they dropped July ~90%→79%). Turn on
# explicitly with INCH_RULES=1 only when working the inch plan.
_INCH_RULES_ENABLED      = os.environ.get("INCH_RULES", "0") != "0"
_INCH_BAND_WIDTH         = int(os.environ.get("INCH_BAND", "2"))   # Rule 2: anchor +/- N
# Variant A (True): the +/-2 band REPLACES the _HARD dominant-inch locks.
# Variant B (False): keep _HARD as well, so the machine is bound by the
# intersection (most restrictive). Chosen by measurement — see plan.
_INCH_BAND_REPLACES_HARD = os.environ.get("INCH_BAND_REPLACES_HARD", "1") != "0"
# Keep the opportunistic forward buffer (Phase C) on the machine's CURRENT inch:
# under one-way movement an inch change is irreversible, so it should be spent on
# real demand (Phase B), not on speculative pre-building.
_INCH_RULES_PHASE_C_SAME_INCH = os.environ.get("INCH_PHASEC_SAME", "1") != "0"
# Rule 1a (never re-use an inch the machine has left) as its own sub-toggle, so
# the cost of the one-way rule can be measured separately from the +/-2 band.
_INCH_NO_REVISIT = os.environ.get("INCH_NO_REVISIT", "1") != "0"
# Treat a sub-campaign leftover deficit as "inch finished" so the machine may
# leave. DEFAULT OFF — measured on all 3 months and it made every one WORSE
# (May -13,197 / June -8,550 / July -28,304). Under one-way movement an easier
# exit burns the machine's limited inches sooner. Kept as a toggle to document
# the experiment; do not enable without re-measuring.
_INCH_GATE_CAMPAIGN_THRESHOLD = os.environ.get("INCH_GATE_THRESH", "0") != "0"

# Machine groups for the flex brute-force (plant multi-inch ranking: VMI 4.6 >
# BJ 2.4 ~ UNI 2.3 > IRM 1.5 inches/machine).
_FLEX_GROUPS: dict[str, frozenset[str]] = {
    "VMIMAXX": frozenset({"6001", "6002", "6003", "6004", "7002", "7004"}),
    "VMISOFT": frozenset({"7001", "7003"}),
    "BJ":      frozenset({"7101", "7102", "7103", "7104", "7105", "7106", "7201"}),
    "UNI":     frozenset({"7501", "7502", "7503"}),
    "STAGE2":  frozenset({"8201", "8301", "8302", "8501", "8502", "7301"}),
}
_INCH_FLEX_VMIMAXX = _FLEX_GROUPS["VMIMAXX"]
_INCH_FLEX_UNI     = _FLEX_GROUPS["UNI"]

# Brute-force override: env FLEX_SET="VMIMAXX,BJ" (group names) selects the flex
# set for a run without editing code. Falls back to the toggle-based default.
def _resolve_flex_machines() -> frozenset:
    _env = os.environ.get("FLEX_SET")
    if _env is not None:
        out: set = set()
        for tok in _env.split(","):
            tok = tok.strip()
            if tok in _FLEX_GROUPS:
                out |= set(_FLEX_GROUPS[tok])
            elif tok:
                out.add(tok)
        return frozenset(out)
    if not _INCH_FLEX_ENABLED:
        return frozenset()
    # Brute-force winner (plant-guided): the multi-inch groups VMI (VMIMAXX +
    # VMISOFT) + BJ = 670,744 cured / 96.7% / 1,845 starvation / 627 write-off,
    # best on every metric. STAGE2/IRM is catastrophic in the flex set (-57k,
    # carcass-dependency disruption) and UNI hurts (tight 13") — both excluded.
    _sel = _FLEX_GROUPS["VMIMAXX"] | _FLEX_GROUPS["VMISOFT"] | _FLEX_GROUPS["BJ"]
    if _INCH_FLEX_INCLUDE_UNI_NARROW:
        _sel = _sel | _FLEX_GROUPS["UNI"]
    return _sel

_INCH_FLEX_MACHINES: frozenset[str] = _resolve_flex_machines()


# ══════════════════════════════════════════════════════════════════════════════
# ROLLING PIPELINE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _bld_qty_per_shift(machine: str) -> int:
    ct_min = _BLD_CT_SEC.get(str(machine), 120.0) / 60.0
    return int(SHIFT_MINS / ct_min)


def _compute_buildable_rate(engine, demand_path: str) -> dict:
    """Per-SKU sustainable building GT/day for the surplus-release 5b guard.

    For each eligible building machine, its GT/day (_bld_qty_per_shift * 3) is
    apportioned across the SKUs it can build by demand share, then summed per SKU.
    A building-oversubscribed SKU (e.g. BJ 15") gets a rate below its cure demand,
    so the guard blocks moving more curing presses onto it (which would starve).
    Stage-1 machines are excluded (carcass, not GT).
    """
    import ast
    from building_b2c import B2C_ETL as _BETL
    ddf  = pd.read_excel(demand_path)
    scol = next((c for c in ddf.columns if "SKU" in str(c)), ddf.columns[0])
    qcol = next((c for c in ddf.columns
                 if any(x in str(c) for x in ("Requirement", "Demand", "Qty", "Quantity"))),
                ddf.columns[1])
    # Sum duplicate SKU rows (see Section E) so buildable-rate isn't understated.
    _dq = ddf[[scol, qcol]].copy()
    _dq[scol] = _dq[scol].astype(str).str.strip()
    _dq[qcol] = pd.to_numeric(_dq[qcol], errors="coerce")
    _dq = _dq.dropna(subset=[qcol])
    demand_dict = _dq.groupby(scol)[qcol].sum().to_dict()
    df_allow = _BETL(engine).load_machine_allowable()
    machine_skus: dict[str, set] = {}
    for _, r in df_allow.iterrows():
        sku = str(r["SKUCode"]).strip()
        ms  = r.get("Machines", [])
        if isinstance(ms, str):
            try:
                ms = ast.literal_eval(ms)
            except Exception:
                ms = []
        for m in ms:
            m = str(m).strip()
            if _MACHINE_GROUP.get(m, "") == "STAGE1":
                continue
            machine_skus.setdefault(m, set()).add(sku)
    # Building is DYNAMIC: a machine can pour its FULL throughput into one SKU when
    # its other SKUs don't need production that shift. So a SKU's buildable rate is
    # the FULL summed throughput of its eligible machines — NOT apportioned by demand
    # share (which underestimated by median ~44x and blocked almost every valid CO).
    # The guard then blocks only genuinely building-oversubscribed SKUs (full
    # capacity < what the added presses would consume).
    buildable: dict[str, float] = {}
    for m, skus in machine_skus.items():
        m_day = _bld_qty_per_shift(m) * 3                      # GT/day this machine
        for s in skus:
            buildable[s] = buildable.get(s, 0.0) + m_day       # full throughput, not apportioned
    return buildable


def _shift_start_dt(date_str: str, shift: str) -> "datetime":
    """Wall-clock datetime at which (date_str, shift) begins.

    Uses SHIFT_STARTS from bc_config ("07:00"/"15:00"/"23:00"). Shift C starts
    at 23:00 on the plan date and runs into 07:00 the next calendar day — the
    date_str is still the shift's own calendar day, matching how every other
    sheet keys shifts. Downstream schedulers consuming StartTime/EndTime get a
    monotonic per-machine timeline because within a shift the cursor only ever
    advances (production + changeover minutes), and shift boundaries never
    overlap for a given machine.
    """
    d  = datetime.strptime(date_str, "%Y-%m-%d")
    hh, mm = SHIFT_STARTS.get(shift, "07:00").split(":")
    return d.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)


def _fmt_dt(dt: "datetime") -> str:
    """Format a building-schedule row timestamp for the output sheet."""
    return dt.strftime("%Y-%m-%d %H:%M")


def _urgency_score(
    sku: str,
    demand_remaining: dict,
    press_count: dict,
    cure_ct_map: dict,
    days_left: int,
    cavities: int = 2,
) -> float:
    """
    Urgency = demand that CANNOT be covered by current curing presses in remaining horizon.
    Higher → more urgent → becomes primary in pool ranking.

    If days_left ≤ 0 or no presses: return full remaining demand (maximally urgent).
    """
    dem = demand_remaining.get(sku, 0.0)
    if dem <= 0:
        return 0.0
    n  = press_count.get(sku, 0)
    ct = float(cure_ct_map.get(sku, DEFAULT_CURING_CT))
    if n <= 0 or ct <= 0 or days_left <= 0:
        return dem
    rate_per_shift = int(SHIFT_MINS / ct) * cavities   # units one press cures per shift
    max_curable    = n * rate_per_shift * 3 * days_left  # 3 shifts/day
    return max(0.0, dem - max_curable)


def _build_machine_pools(
    machine_skus:    dict,
    sku_inch:        dict,
    demand_dict:     dict,
    press_count:     dict,
    cure_ct_map:     dict,
    planning_days:   int,
    pool_size:       int = POOL_SIZE,
) -> dict:
    """
    Build a fixed pool of up to `pool_size` same-dominant-inch SKUs per machine.
    Pool ordering = urgency descending at Day 1.
    Returns: {machine: [sku_primary, sku_sec1, sku_sec2, ...]}
    """
    pools: dict[str, list[str]] = {}
    for machine, skus in machine_skus.items():
        dom_inch = _MACHINE_DOMINANT_INCH.get(str(machine), "")

        # Prefer same dominant inch + has demand + has active presses
        same_inch = [
            s for s in skus
            if sku_inch.get(s, "") == dom_inch
            and demand_dict.get(s, 0) > 0
            and press_count.get(s, 0) > 0
        ]
        if not same_inch:
            # Fallback: any eligible SKU with demand and presses
            same_inch = [
                s for s in skus
                if demand_dict.get(s, 0) > 0 and press_count.get(s, 0) > 0
            ]

        same_inch.sort(
            key=lambda s: (-_urgency_score(s, demand_dict, press_count, cure_ct_map, planning_days), s)
        )
        pools[machine] = same_inch[:pool_size]

    return pools


def _cure_qty_per_shift(ct_min: float) -> int:
    return int(SHIFT_MINS / ct_min) * CURING_CAVITIES


def _co_cost(machine: str, from_inch: str, to_inch: str) -> int:
    mg = _MACHINE_GROUP.get(str(machine), "VMI")
    if from_inch == to_inch:
        return BUILDING_CO_SAME_SIZE.get(mg, 60)
    return BUILDING_CO_DIFF_SIZE.get(mg, 120)


def _inch_num(inch: str):
    """Inch string -> int, or None when it isn't a usable number."""
    try:
        return int(str(inch).strip())
    except (TypeError, ValueError):
        return None


def _inch_ok(to_inch: str, cur_inch: str, anchor: str, used: set) -> bool:
    """Client inch rules — Rule 2 (+/-2 band) and Rule 1a (never revisit an inch).

    anchor == "" means the machine has not been assigned yet, so its first
    assignment is unconstrained (that assignment sets the anchor).
    Staying on the current inch is always allowed.
    """
    if not _INCH_RULES_ENABLED:
        return True
    if to_inch == cur_inch:
        return True

    # Rule 2 — must stay within anchor +/- _INCH_BAND_WIDTH.
    a, t = _inch_num(anchor), _inch_num(to_inch)
    if a is not None and t is not None and abs(t - a) > _INCH_BAND_WIDTH:
        return False

    # Rule 1a — an inch the machine has already left can never be re-used.
    if _INCH_NO_REVISIT and to_inch in (used or ()):
        return False
    return True


def _inch_demand_done(machine: str, cur_inch: str, machine_skus: dict,
                      sku_inch: dict, deficit_fn, buf, rate: float = 0.0) -> bool:
    """Rule 1b — may this machine leave `cur_inch` for a different inch?

    True when no SKU at the machine's current inch still has a deficit it could
    usefully serve. (Per the client clarification this is "no unmet demand it can
    serve now", NOT "all demand at that inch globally exhausted".)

    IMPORTANT — the deficit must be big enough to form a LEGAL campaign. A
    machine needs >= MIN_CAMPAIGN_MINS of work to build anything, so a tiny
    residual deficit (say 10 units) would otherwise pin the machine forever:
    too small to build, too big to leave. That trap was the single largest
    source of idle time under the one-way rule, so a sub-campaign remainder
    counts as "inch finished".
    """
    if not _INCH_RULES_ENABLED or not cur_inch:
        return True
    # Threshold below which a leftover deficit is treated as "inch finished".
    # MEASURED RESULT: raising this to a full campaign made every month WORSE
    # (May -13,197 / June -8,550 / July -28,304). Under one-way movement leaving
    # is PERMANENT, so an easier exit just burns the machine's inches sooner and
    # it runs out of legal work. Reluctance to leave is protective here — keep
    # the strict "any deficit blocks departure" default (threshold = 0).
    min_units = 0.0
    if _INCH_GATE_CAMPAIGN_THRESHOLD:
        min_units = (max(MIN_CAMPAIGN_UNITS, MIN_CAMPAIGN_MINS * rate)
                     if rate > 0 else MIN_CAMPAIGN_UNITS)
    for s in machine_skus.get(machine, ()) or ():
        if sku_inch.get(s, "") != cur_inch:
            continue
        try:
            d = deficit_fn(s, buf)
        except TypeError:          # deficit closures that take only the SKU
            d = deficit_fn(s)
        if d > min_units:
            return False
    return True


def _select_dynamic_co_target(
    old_sku: str,
    demand_remaining: dict,
    press_count: dict,
    cure_ct_map: dict,
    priority_score_map: dict,
    gt_inventory: dict,
    horizon_left: int,
    already_targeted: set,
) -> "str | None":
    """Select the best curing CO target when a press finishes its SKU demand mid-plan.

    The calling press is already idle (old_sku demand = 0), so any production on a
    new SKU is strictly better than idle. Both Class A and Class B targets are eligible.

    Sort key: Class A first (critical, can't meet demand without this press), then Class B.
    Within class: fewest after-CO days → highest priority score → most GT in inventory.
    `already_targeted` prevents multiple dynamic COs going to the same new SKU in
    the same shift.
    """
    candidates = []
    for sku, rem in demand_remaining.items():
        if rem <= 0 or sku == old_sku or sku in already_targeted:
            continue
        n    = press_count.get(sku, 0)
        ct   = cure_ct_map.get(sku, DEFAULT_CURING_CT)
        rate = _cure_qty_per_shift(ct) * 3          # per-day curing rate
        if rate <= 0:
            continue
        current_days = rem / (n * rate) if n > 0 else float("inf")
        # Class A = cannot meet demand without this press; Class B = helpful but not critical.
        urgency_class = 0 if current_days > horizon_left else 1
        # Class B allowed only if GT inventory already covers ≥ 1 shift of curing.
        # Without this guard, a Class B press starts curing next shift but finds zero GT
        # (building never pre-built for it) → starvation event instead of production.
        gt_inv = gt_inventory.get(sku, 0.0)
        if urgency_class == 1:
            ct_sku    = cure_ct_map.get(sku, DEFAULT_CURING_CT)
            shift_need = _cure_qty_per_shift(ct_sku)
            if gt_inv < shift_need:
                continue  # no GT ready → skip Class B to avoid starvation
        after_days = rem / ((n + 1) * rate)
        prio       = priority_score_map.get(sku, 0.0)
        gt_signal  = min(gt_inv, rate)  # cap at 1 day's rate
        candidates.append((urgency_class, after_days, -prio, -gt_signal, sku))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][4]


def _select_ratio_co_target(
    old_sku: str,
    press: str,
    demand_remaining: dict,
    press_count: dict,
    pending_counts: dict,
    demand_dict: dict,
    cure_ct_map: dict,
    press_to_demand_targets: dict,
    press_total_demand: dict,
    horizon_left: int,
    sku_to_press_count: dict | None = None,
    rich_ranking: bool = False,
) -> "str | None":
    """Select the next SKU for a press whose current SKU demand just hit zero
    (_RATIO_CO_ALLOCATION_ENABLED). Eligibility timing is untouched by
    rich_ranking — this is called ONLY once a press has already fully
    exhausted its current SKU (no urgency/Class A/B/horizon-threshold
    early-CO, per spec); rich_ranking only changes which candidate wins
    among those already eligible right now.

    rich_ranking=False (default): ranked purely by static ratio
    (demand_dict[target]/press_total_demand[press], same formula as the
    building scheduler's NRI ranking, never decremented).

    rich_ranking=True (_RATIO_CO_RICH_RANKING_ENABLED): adds COScheduler's
    other four ranking levels on top of ratio — Class A/B (is the target
    under-resourced relative to its own deadline?), after_days (prefer
    targets closest to completion once this press joins), cycle time
    (faster-curing preferred), and target-side scarcity (SKU with fewer
    compatible presses preferred — protects thin SKUs). Still only reorders
    candidates that pass the presses-needed eligibility gate below; never
    makes a press eligible sooner than "current SKU fully done."

    Before ranking (either mode), a press-allocation tracker skips any SKU
    that already has enough presses (assigned + in-flight/pending this
    evaluation window) to finish its remaining demand within the remaining
    horizon — so a free press is never handed to an already-adequately-served
    SKU merely because it ranks well.

    Compatibility is enforced by construction: only SKUs in
    press_to_demand_targets[press] (the physical mould/allowable-machines
    mapping) are ever considered.
    """
    candidates: list[tuple] = []
    for target in press_to_demand_targets.get(press, []):
        if target == old_sku:
            continue
        rem = demand_remaining.get(target, 0.0)
        if rem <= 0:
            continue
        ct   = cure_ct_map.get(target, DEFAULT_CURING_CT)
        rate = _cure_qty_per_shift(ct) * 3  # per-press per-day rate
        if rate <= 0 or horizon_left <= 0:
            continue
        effective_n = press_count.get(target, 0) + pending_counts.get(target, 0)
        presses_needed = max(1, math.ceil(rem / (rate * horizon_left)))
        if effective_n >= presses_needed:
            continue  # already has enough presses assigned — try next-highest ratio
        ratio = demand_dict.get(target, 0.0) / press_total_demand.get(press, 1e-9)
        if rich_ranking:
            current_days = rem / (effective_n * rate) if effective_n > 0 else float("inf")
            cls = 0 if current_days > horizon_left * CO_CLASS_B_THRESHOLD else 1
            after_days = rem / ((effective_n + 1) * rate)
            scarcity = (sku_to_press_count or {}).get(target, 0)
            candidates.append((cls, -ratio, after_days, ct, scarcity, target))
        else:
            candidates.append((-ratio, target))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][-1]


def _score_co_candidate(
    target: str,
    press: str,
    demand_remaining: dict,
    press_count: dict,
    cure_ct_map: dict,
    priority_score_map: dict,
    demand_dict: dict,
    press_total_demand: dict,
    sku_campaign_tier: dict,
    nri_skus: frozenset,
    horizon_left: int,
) -> tuple:
    """Score a (press, target) CO candidate for the dynamic day-planner
    (_DYNAMIC_CO_PLANNER_ENABLED). Mirrors curing_consumption_dynamic.py's
    _urgency_sort_key/_priority_signal: RI keeps Priority_Score-driven
    urgency untouched, NRI ranked by static demand[target]/press_total_demand[press]
    (confirmed win this session — same formula, duplicated here rather than
    imported, to keep COScheduler's own tie-break ordering completely
    unperturbed). sku_campaign_tier only applied as a tiebreak, and only when
    _CAMPAIGN_TIER_TIEBREAK_ENABLED — day-granularity, never a primary signal.
    Final (press, target) pair is always the last tuple element for full
    determinism regardless of dict/set iteration order upstream.
    """
    rem  = demand_remaining.get(target, 0.0)
    n    = press_count.get(target, 0)
    ct   = cure_ct_map.get(target, DEFAULT_CURING_CT)
    rate = _cure_qty_per_shift(ct) * 3
    current_days = rem / (n * rate) if (n > 0 and rate > 0) else float("inf")
    cls = 0 if current_days > horizon_left * CO_CLASS_B_THRESHOLD else 1
    after_days = rem / ((n + 1) * rate) if rate > 0 else float("inf")

    if target in nri_skus:
        signal = -(demand_dict.get(target, 0.0) / press_total_demand.get(press, 1e-9))
    else:
        signal = -priority_score_map.get(target, 0.0)

    tier = sku_campaign_tier.get(target, 99) if _CAMPAIGN_TIER_TIEBREAK_ENABLED else 0

    return (cls, signal, after_days, tier, press, target)


def _plan_day_cos(
    day: int,
    press_state: dict,
    demand_remaining: dict,
    press_count: dict,
    cure_ct_map: dict,
    priority_score_map: dict,
    demand_dict: dict,
    press_to_demand_targets: dict,
    press_total_demand: dict,
    sku_campaign_tier: dict,
    ri_skus: frozenset,
    nri_skus: frozenset,
    daily_co_count: dict,
    planning_days: int,
) -> list:
    """Dynamic per-day CO planner (_DYNAMIC_CO_PLANNER_ENABLED). Replaces the
    static co_by_day.get(day, []) lookup with a decision grounded in real
    live state, made fresh each day before Shift A — same real-state
    discipline as the existing reactive dynamic_co_tracker mechanism
    (b2c_pipeline.py _select_dynamic_co_target), extended from "one press
    reactively" to "the whole eligible press fleet, once a day."
    """
    horizon_left = planning_days - day + 1
    slots_left = MAX_CHANGEOVERS_PER_DAY - daily_co_count.get(day, 0)
    if slots_left <= 0:
        return []

    # Presses whose current SKU has no remaining demand — generalizes RO +
    # just-finished-RI into one real-state check (sorted: deterministic seed
    # order regardless of press_state's dict iteration order). NOTE: no
    # early-return when this is empty — the donation pass below doesn't need
    # any press to be pre-idle (it creates capacity by pulling from a healthy
    # RI SKU), and gating it behind newly_free caused a severe regression
    # (670k->593k): most days have zero naturally-idle presses, so the
    # donation pass — the mechanism actually capable of proactive
    # reallocation — never got a chance to run on those days at all.
    newly_free = sorted(
        p for p, st in press_state.items()
        if demand_remaining.get(st["sku"], 0.0) <= 0
    )

    # Local working copies, mutated as assignments are made this pass, so a
    # target already sufficiently covered by an earlier pick this same day
    # stops absorbing further presses (mirrors COScheduler's own
    # "re-check with CURRENT press_count" guard).
    _press_count = dict(press_count)

    candidates = []
    for p in newly_free:
        old_sku = press_state[p]["sku"]
        for target in press_to_demand_targets.get(p, []):
            if target == old_sku:
                continue
            rem = demand_remaining.get(target, 0.0)
            if rem <= 0:
                continue
            is_nri = target in nri_skus
            is_ri  = target in ri_skus
            if is_nri:
                pass  # always eligible
            elif is_ri and _press_count.get(target, 0) > 0:
                n_t = _press_count.get(target, 0)
                ct_t = cure_ct_map.get(target, DEFAULT_CURING_CT)
                rate_t = _cure_qty_per_shift(ct_t) * 3
                current_days = rem / (n_t * rate_t) if rate_t > 0 else float("inf")
                if current_days <= horizon_left:
                    continue  # RI already on track — skip
            else:
                continue
            key = _score_co_candidate(
                target, p, demand_remaining, _press_count, cure_ct_map,
                priority_score_map, demand_dict, press_total_demand,
                sku_campaign_tier, nri_skus, horizon_left,
            )
            candidates.append((key, p, old_sku, target))

    candidates.sort(key=lambda x: x[0])

    today_events: list = []
    assigned_press: set = set()
    for key, p, old_sku, target in candidates:
        if slots_left <= 0:
            break
        if p in assigned_press:
            continue
        rem = demand_remaining.get(target, 0.0)
        if rem <= 0:
            continue
        if target in ri_skus and target not in nri_skus:
            n_t = _press_count.get(target, 0)
            if n_t > 0:
                ct_t = cure_ct_map.get(target, DEFAULT_CURING_CT)
                rate_t = _cure_qty_per_shift(ct_t) * 3
                if rate_t > 0 and rem / (n_t * rate_t) <= horizon_left:
                    continue  # became sufficiently covered by an earlier pick this pass
        today_events.append((p, old_sku, target))
        assigned_press.add(p)
        _press_count[old_sku] = max(0, _press_count.get(old_sku, 0) - 1)
        _press_count[target]  = _press_count.get(target, 0) + 1
        slots_left -= 1

    # Donation pass: any Class-A-critical target (RI or NRI) may pull a spare
    # press from an RI SKU that can safely give one up (n>1, still meets its
    # own full demand within the horizon with n-1) — generalized from an
    # earlier NRI-only version. That narrower version left critically
    # under-served RI SKUs (some presses, but not enough to meet demand
    # within the horizon) with no path to get help, because the main pass
    # above only ever acts on presses that are ALREADY idle today — the root
    # cause of a severe regression (670k->593k) when first tested: reactive
    # "fill an idle press" logic alone never proactively reallocates capacity
    # the way COScheduler's upfront horizon-wide simulation does.
    if slots_left > 0:
        served_targets = {t for _, _, t in today_events}

        def _current_days(sku: str) -> float:
            rem  = demand_remaining.get(sku, 0.0)
            n    = _press_count.get(sku, 0)
            ct   = cure_ct_map.get(sku, DEFAULT_CURING_CT)
            rate = _cure_qty_per_shift(ct) * 3
            if n <= 0 or rate <= 0:
                return float("inf")
            return rem / (n * rate)

        needs_help = sorted(
            (
                s for s in (set(ri_skus) | set(nri_skus))
                if s not in served_targets
                and demand_remaining.get(s, 0.0) > 0
                and _current_days(s) > horizon_left * CO_CLASS_B_THRESHOLD
            ),
            key=lambda s: (-_current_days(s), -demand_remaining.get(s, 0.0), s),
        )
        if os.environ.get("DYNCO_DEBUG"):
            print(f"    [DynCO-debug] Day {day}: newly_free={len(newly_free)} "
                  f"needs_help={len(needs_help)} slots_left={slots_left} "
                  f"sample_needs_help={needs_help[:5]}")
        _donors_found = 0
        for target in needs_help:
            if slots_left <= 0:
                break
            donor = None
            for p in sorted(press_state.keys()):
                if p in assigned_press:
                    continue
                cur_sku = press_state[p]["sku"]
                if cur_sku == target or cur_sku not in ri_skus:
                    continue
                if target not in press_to_demand_targets.get(p, []):
                    continue
                n = _press_count.get(cur_sku, 0)
                if n <= 1:
                    continue
                rem_cur  = demand_remaining.get(cur_sku, 0.0)
                ct_cur   = cure_ct_map.get(cur_sku, DEFAULT_CURING_CT)
                rate_cur = _cure_qty_per_shift(ct_cur) * 3
                if rate_cur > 0 and rem_cur / ((n - 1) * rate_cur) <= horizon_left:
                    donor = (p, cur_sku)
                    break
            if donor is not None:
                p, cur_sku = donor
                today_events.append((p, cur_sku, target))
                assigned_press.add(p)
                _press_count[cur_sku] = max(0, _press_count.get(cur_sku, 0) - 1)
                _press_count[target]  = _press_count.get(target, 0) + 1
                slots_left -= 1
                _donors_found += 1
        if os.environ.get("DYNCO_DEBUG") and needs_help:
            print(f"    [DynCO-debug] Day {day}: donors_found={_donors_found}/{len(needs_help)}")

    return today_events


def _rolling_horizon_co_call(
    day: int,
    planning_days: int,
    press_state: dict,
    press_count: dict,
    demand_remaining: dict,
    demand_dict: dict,
    priority_score_map: dict,
    df_allowable: "pd.DataFrame",
    ct_map: dict,
    dynamic_co_tracker: dict,
    scheduler: "COScheduler",
    max_co_per_day: int = MAX_CHANGEOVERS_PER_DAY,
) -> list:
    """Rolling-Horizon COScheduler call (_ROLLING_HORIZON_CO_ENABLED). Reuses
    the existing, proven COScheduler.schedule() unchanged — only synthesizes
    fresh Day-0-shaped inputs from live rolling-pipeline state each day, with
    a shrinking remaining horizon, and keeps only that call's relative-day-1
    events (the rest of its lookahead is discarded and recomputed tomorrow).

    Finding 2: presses mid-transition in dynamic_co_tracker are excluded from
    this day's snapshot — press_count already reflects them, but press_state
    won't until the shift their transition resolves; including them would let
    schedule() assign them a second, conflicting CO.

    Finding 1: Category mirrors SKUClassifier exactly — press_count>0 is
    always "Runner-In" for a demand SKU (even at zero remaining demand;
    schedule()'s own demand_done_free logic handles that case with the
    any-class bypass it already has). Only a non-demand SKU occupying a press
    is "Runner-Out".
    """
    horizon_left = planning_days - day + 1
    demand_skus = set(demand_remaining.keys())

    running_rows = [
        {"Machine": p, "SKUCode": st["sku"]}
        for p, st in sorted(press_state.items())
        if p not in dynamic_co_tracker
    ]
    df_running_moulds = pd.DataFrame(running_rows, columns=["Machine", "SKUCode"])

    day0_rows = [
        {
            "SKUCode": sku,
            "Category": "Runner-In" if press_count.get(sku, 0) > 0 else "Non-Runner-In",
            "Running_Press_Count": press_count.get(sku, 0),
        }
        for sku in sorted(demand_skus)
    ]
    ro_counts: dict[str, int] = defaultdict(int)
    for p, st in sorted(press_state.items()):
        if p in dynamic_co_tracker:
            continue
        if st["sku"] not in demand_skus:
            ro_counts[st["sku"]] += 1
    for sku in sorted(ro_counts):
        day0_rows.append({
            "SKUCode": sku, "Category": "Runner-Out",
            "Running_Press_Count": ro_counts[sku],
        })
    df_day0 = pd.DataFrame(day0_rows, columns=["SKUCode", "Category", "Running_Press_Count"])

    df_demand = pd.DataFrame(
        [
            {
                "SKUCode": sku,
                "Quantity": max(0.0, demand_remaining.get(sku, 0.0)),
                "Priority": priority_score_map.get(sku, 0.0),
            }
            for sku in sorted(demand_skus)
        ],
        columns=["SKUCode", "Quantity", "Priority"],
    )

    if os.environ.get("ROLLCO_DEBUG"):
        print(f"    [RollCO-debug] Day {day}: horizon_left={horizon_left} "
              f"presses={len(df_running_moulds)} skus={len(demand_skus)}")

    co_events = scheduler.schedule(
        df_day0=df_day0,
        df_demand=df_demand,
        df_allowable=df_allowable,
        df_running_moulds=df_running_moulds,
        ct_map=ct_map,
        max_co_per_day=max_co_per_day,
        planning_days=horizon_left,
        ratio_demand_map=demand_dict,
    )
    today = sorted(
        (ev["press"], ev["old_sku"], ev["new_sku"])
        for ev in co_events if ev["day"] == 1
    )
    if os.environ.get("ROLLCO_DEBUG"):
        print(f"    [RollCO-debug] Day {day}: {len(today)} events {today}")
    return today


def _assign_building_shift(
    shift_cure_demand:      dict,
    machine_skus:           dict,
    machine_current_sku:    dict,
    sku_inch:               dict,
    demand_remaining:       dict,
    gt_inventory:           dict,
    machine_pool:           dict,
    machine_minutes_on_sku: dict,
    cure_ct_map:            dict,
    press_count:            dict,
    co_target_skus:         frozenset = frozenset(),
    days_left:              int = 31,
    demand_dict:            dict | None = None,
    machine_total_demand:   dict | None = None,
    machine_anchor_inch:    dict | None = None,
    machine_used_inches:    dict | None = None,
) -> dict:
    """
    Greedy per-SHIFT building assignment.

    Plant-accurate behaviour:
    - CO can happen in any shift (A/B/C) whenever a deficit SKU exists and CO cost fits.
    - co_target_skus: NRI SKUs with curing CO firing today — the 30% guard is bypassed
      for urgent ones (0 GT inventory + active demand) in Campaign 2+.
    - Dominant-inch priority: when selecting CO candidates, SKUs matching the
      machine's dominant inch sort before other eligible inches.
    - All machines: MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT COs per shift.

    Returns: {machine: [(sku, qty_int, co_type_str)]}
      co_type: "start" | "same_size_CO" | "diff_size_CO"
    """
    # Client inch-rule state (persisted across shifts by run_rolling_pipeline).
    machine_anchor_inch = machine_anchor_inch if machine_anchor_inch is not None else {}
    machine_used_inches = machine_used_inches if machine_used_inches is not None else {}

    def _inch_gate(m: str, to_inch: str, cur_inch: str) -> bool:
        """Rules 1a + 2 for a candidate (machine, to_inch)."""
        return _inch_ok(to_inch, cur_inch,
                        machine_anchor_inch.get(m, ""),
                        machine_used_inches.get(m, set()))

    def _max_cos(mach: str) -> int:
        # Flex machines get extra CO budget so they can take an off-inch
        # excursion AFTER exhausting same-inch work (which uses the normal 2).
        if mach in _INCH_FLEX_MACHINES:
            return MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT + _INCH_FLEX_EXTRA_COS
        return MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT
    projected_gt: dict[str, float] = dict(gt_inventory)
    # GT actually carried INTO this shift (before any building) — the base for the
    # end-of-day 10k cap. Base Phase A/B build is cure-neutral (curing consumes it
    # this/next shift, so carry stays flat without a forward buffer); only Phase C's
    # forward buffer adds NET overnight carry, so we bound entry_carry + forward ≤ 10k.
    _entry_carry_gt = sum(v for v in gt_inventory.values() if v > 0)

    if _GLOBAL_ASSIGN_ENABLED:
        # ══ Global machine-SKU scoring assignment (supersedes per-machine greedy) ══
        # Phase A: each machine continues its current SKU (no CO). Phase B: all
        # remaining (machine,SKU) pairs are scored together and assigned best-first,
        # with constraint=min(flex_machine,flex_sku) so constrained machines/SKUs win.
        def _buf_of(m: str) -> float:
            return GT_BUFFER_SHIFTS if _MACHINE_GROUP.get(m, "") == "VMI" else 1

        def _defc(sku: str, _b: float) -> float:
            built_ahead = projected_gt.get(sku, 0.0)
            gap = shift_cure_demand.get(sku, 0.0) * _b - built_ahead
            cap = demand_remaining.get(sku, 0.0) - built_ahead
            return min(max(0.0, gap), max(0.0, cap))

        def _gt_headroom(fwd_added: float) -> float:
            # Strict room under the end-of-day 10k cap for the forward buffer. We do NOT
            # credit this shift's curing to the forward GT (conservative → even if
            # nothing cures, carry stays ≤ 10k). Base Phase A/B build is cure-neutral and
            # is excluded; only entry_carry + forward-added is bounded. Cap OFF ⇒ inf.
            if not _ENDOFDAY_GT_CAP_ENABLED:
                return float("inf")
            return max(0.0, MAX_ENDOFDAY_GT_INVENTORY - (_entry_carry_gt + fwd_added))

        def _tierg(sku: str, m: str, d: float) -> tuple:
            _is_ri = press_count.get(sku, 0) > 0
            _ri_ratio = (_RI_RATIO_ENABLED or _RI_RATIO_GLOBAL) and _is_ri
            if (_BUILDING_RATIO_ENABLED and (press_count.get(sku, 0) <= 0 or _ri_ratio)
                    and demand_dict is not None and machine_total_demand is not None):
                ratio = demand_dict.get(sku, 0.0) / machine_total_demand.get(m, 1e-9)
                return (0 if _is_ri else 1, -ratio)
            return (0, -d)

        machines = [m for m in machine_skus if _MACHINE_GROUP.get(m, "") != "STAGE1"]
        stg = {
            m: {
                "remaining": float(SHIFT_MINS), "co_count": 0, "max_cos": _max_cos(m),
                "cur_sku": machine_current_sku.get(m, ""),
                "rate": _bld_qty_per_shift(m) / SHIFT_MINS,
                "dom": _MACHINE_DOMINANT_INCH.get(
                    str(m), sku_inch.get(machine_current_sku.get(m, ""), "")),
                "primary_done": True, "campaigns": [],
            }
            for m in machines
        }

        # ── Phase A: continuation anchor (no CO) ──
        for m in sorted(machines):
            s = stg[m]; buf = _buf_of(m); rate = s["rate"]
            eligible = machine_skus.get(m, set()); dom = s["dom"]
            cur = s["cur_sku"]
            # seed empty machine with a dom-inch-preferred deficit SKU (== "start")
            if not cur:
                cands = [x for x in eligible if _defc(x, buf) > 0]
                if cands:
                    cur = min(cands, key=lambda x: (
                        0 if sku_inch.get(x, "") == dom else 1,
                        *_tierg(x, m, _defc(x, buf)), x))
                    s["cur_sku"] = cur
            cur_inch = sku_inch.get(cur, "")
            # round-trip buffer sizing (same as per-machine path)
            eff_buf = buf
            if _ROUND_TRIP_BUFFER_ENABLED and cur and len(eligible) > 1:
                pc = [x for x in eligible if x != cur
                      and demand_remaining.get(x, 0.0) > 0 and _defc(x, buf) > 0]
                if pc:
                    if m in _INCH_FLEX_MACHINES:
                        partner = max(pc, key=lambda x: (
                            _co_cost(m, cur_inch, sku_inch.get(x, ""))
                            + _co_cost(m, sku_inch.get(x, ""), cur_inch), _defc(x, buf), x))
                    else:
                        partner = max(pc, key=lambda x: (_defc(x, buf), x))
                    p_inch = sku_inch.get(partner, "")
                    p_dwell = max(MIN_CAMPAIGN_MINS,
                                  _defc(partner, buf) / rate if rate > 0 else MIN_CAMPAIGN_MINS)
                    rt = _co_cost(m, cur_inch, p_inch) + p_dwell + _co_cost(m, p_inch, cur_inch)
                    eff_buf = max(buf, rt / SHIFT_MINS)
            flex_reclaim = (m in _INCH_FLEX_MACHINES and cur_inch != dom
                            and any(sku_inch.get(x, "") == dom and _defc(x, buf) > 0
                                    for x in eligible))
            # Captive-max experiment: a captive machine (only 1 eligible SKU) builds
            # its sole SKU to the full demand cap (not just the buffer) so it runs
            # flat-out and never idles while its SKU has unmet demand.
            _cap_max = (_CAPTIVE_MAX_ENABLED and len(eligible) == 1
                        and _MACHINE_GROUP.get(m, "") != "STAGE1")
            _room = (max(0.0, demand_remaining.get(cur, 0.0) - projected_gt.get(cur, 0.0))
                     if _cap_max else _defc(cur, eff_buf))
            if cur in eligible and _room > 0 and not flex_reclaim:
                mins = min(s["remaining"], _room / rate if rate > 0 else s["remaining"])
                qty = int(mins * rate)
                if mins >= MIN_CAMPAIGN_MINS and qty > 0:
                    s["campaigns"].append((cur, qty, "start"))
                    projected_gt[cur] = projected_gt.get(cur, 0.0) + qty
                    s["remaining"] -= mins
                s["primary_done"] = _defc(cur, eff_buf) <= 0

        # ── Phase B: global pair-scoring greedy for remaining capacity ──
        _guard = 0
        while _guard < 100000:
            _guard += 1
            pairs = []
            flex_m: dict = {}
            flex_s: dict = {}
            for m in machines:
                s = stg[m]
                if s["remaining"] < MIN_CAMPAIGN_MINS or s["co_count"] >= s["max_cos"]:
                    continue
                buf = _buf_of(m); rate = s["rate"]; dom = s["dom"]
                cur = s["cur_sku"]; cur_inch = sku_inch.get(cur, "")
                for sku in machine_skus.get(m, set()):
                    if sku == cur:
                        continue
                    d = _defc(sku, buf)
                    if d <= 0:
                        continue
                    to_inch = sku_inch.get(sku, "")
                    # ── Client inch rules (Rule 1a no-revisit + Rule 2 band) ──
                    if not _inch_gate(m, to_inch, cur_inch):
                        continue
                    # ── Rule 1b: leave the current inch only when nothing at that
                    #    inch still needs this machine right now ──
                    if (_INCH_RULES_ENABLED and to_inch != cur_inch
                            and not _inch_demand_done(m, cur_inch, machine_skus,
                                                      sku_inch, _defc, buf, rate)):
                        continue
                    if (m in (_SOFT_LOCK_MACHINES | _INCH_FLEX_MACHINES)
                            and to_inch != dom and not s["primary_done"]
                            and not _INCH_RULES_ENABLED):
                        continue
                    cost = _co_cost(m, cur_inch, to_inch)
                    if s["remaining"] - cost < MIN_CAMPAIGN_MINS:
                        continue
                    is_urgent = (sku in co_target_skus and projected_gt.get(sku, 0.0) == 0
                                 and demand_remaining.get(sku, 0.0) > 0)
                    _flex_off_ok = (m in _INCH_FLEX_MACHINES and to_inch != dom
                                    and s["primary_done"])
                    # With the client inch rules a diff-inch move has already passed
                    # the Rule-1b gate (nothing left to serve at the current inch), so
                    # the 30% cost guard must not block it — the machine would idle.
                    if _INCH_RULES_ENABLED and to_inch != cur_inch:
                        _flex_off_ok = True
                    if cost > 0.30 * s["remaining"] and not is_urgent and not _flex_off_ok:
                        continue
                    avail = s["remaining"] - cost
                    mins = min(avail, d / rate if rate > 0 else avail)
                    qty = int(mins * rate)
                    if mins < MIN_CAMPAIGN_MINS or qty <= 0:
                        continue
                    tier, primary = _tierg(sku, m, d)
                    # Prefer staying on the CURRENT inch (cheap same-size CO) once the
                    # inch rules are live — "dominant inch" is superseded by the anchor
                    # band, so the old dominant-inch preference no longer applies.
                    inch_penalty = ((0 if to_inch == cur_inch else 1) if _INCH_RULES_ENABLED
                                    else (0 if to_inch == dom else 1))
                    pairs.append((m, sku, cost, to_inch, tier, primary, qty, mins, inch_penalty))
                    flex_m[m] = flex_m.get(m, 0) + 1
                    flex_s[sku] = flex_s.get(sku, 0) + 1
            if not pairs:
                break

            def _key(p):
                m, sku, cost, to_inch, tier, primary, qty, mins, inch_penalty = p
                constraint = min(flex_m[m], flex_s[sku])
                if _GLOBAL_CONSTRAINT_MODE == "below":
                    return (inch_penalty, tier, primary, constraint, cost, m, sku)
                elif _GLOBAL_CONSTRAINT_MODE == "captive":
                    return (inch_penalty, 0 if constraint <= 1 else 1,
                            tier, primary, cost, m, sku)
                return (inch_penalty, constraint, tier, primary, cost, m, sku)  # "above"

            m, sku, cost, to_inch, tier, primary, qty, mins, inch_penalty = min(pairs, key=_key)
            s = stg[m]
            co_type = "same_size_CO" if to_inch == sku_inch.get(s["cur_sku"], "") else "diff_size_CO"
            s["campaigns"].append((sku, qty, co_type))
            projected_gt[sku] = projected_gt.get(sku, 0.0) + qty
            s["remaining"] -= (cost + mins)
            s["co_count"] += 1
            s["cur_sku"] = sku

        # ── Phase C: forward-buffer slack-fill (level-loading) ──
        # Use building capacity that Phase A/B left idle to PRE-BUILD a shelf-life-safe
        # forward buffer for SKUs that WILL be cured in the next 3 days. Builds ONLY
        # required GT: a candidate must have a LIVE cure-draw (shift_cure_demand>0 → a
        # press is actively pulling it — never a random SKU) and unmet demand. Per-SKU
        # forward target = min(demand_remaining, 3-day cure-draw); the shelf-safe cap
        # auto-targets building-limited SKUs (high draw) and auto-skips press-limited
        # ones (tiny draw → nothing to pre-build). Bounded by the 10k end-of-day cap.
        if _FORWARD_BUFFER_ENABLED:
            _fwd_added = 0.0   # total forward GT queued this shift (across all machines)
            for m in sorted(machines):
                s = stg[m]; rate = s["rate"]
                if rate <= 0:
                    continue
                _cguard = 0
                while s["remaining"] >= MIN_CAMPAIGN_MINS and _cguard < 1000:
                    _cguard += 1
                    hr = _gt_headroom(_fwd_added)
                    if _ENDOFDAY_GT_CAP_ENABLED and hr <= 0:
                        break
                    dom = s["dom"]; cur = s["cur_sku"]; cur_inch = sku_inch.get(cur, "")
                    best = None; best_key = None; best_room = 0.0
                    for sku in machine_skus.get(m, set()):
                        draw = shift_cure_demand.get(sku, 0.0)
                        if draw <= 0:                      # not needed soon → not "required"
                            continue
                        dr = demand_remaining.get(sku, 0.0)
                        if dr <= 0:
                            continue
                        # starvation-risk gate: skip SKUs that already hold enough GT
                        # (>= _FWD_RISK_SHIFTS shifts of draw) — they are NOT about to
                        # starve, so pre-building them would only front-load early month.
                        if (_FWD_RISK_SHIFTS > 0
                                and projected_gt.get(sku, 0.0) >= draw * _FWD_RISK_SHIFTS):
                            continue
                        need_co = (sku != cur)
                        if need_co and s["co_count"] >= s["max_cos"]:
                            continue
                        to_inch = sku_inch.get(sku, "")
                        # ── Client inch rules (same gates as Phase B) ──
                        # Under one-way inch movement every inch change is a
                        # ONE-TIME door. The forward buffer is opportunistic
                        # pre-building, so letting it spend that door permanently
                        # closes an inch the machine may need for real demand
                        # later. Restrict Phase C to the machine's current inch.
                        if (_INCH_RULES_ENABLED and _INCH_RULES_PHASE_C_SAME_INCH
                                and to_inch != cur_inch):
                            continue
                        if not _inch_gate(m, to_inch, cur_inch):
                            continue
                        if (_INCH_RULES_ENABLED and to_inch != cur_inch
                                and not _inch_demand_done(m, cur_inch, machine_skus,
                                                          sku_inch, _defc, _buf_of(m), rate)):
                            continue
                        # respect the flex/soft-lock off-dominant-inch gate (as Phase B)
                        if (m in (_SOFT_LOCK_MACHINES | _INCH_FLEX_MACHINES)
                                and to_inch != dom and not s["primary_done"]
                                and not _INCH_RULES_ENABLED):
                            continue
                        target = min(dr, draw * GT_SHELF_LIFE_SHIFTS)
                        room = target - projected_gt.get(sku, 0.0)
                        if room <= 0:
                            continue
                        key = (0 if to_inch == dom else 1,        # dominant inch first
                               0 if sku == cur else 1,            # avoid a CO if possible
                               -(draw / (projected_gt.get(sku, 0.0) + 1.0)),  # most starving
                               -room, sku)
                        if best is None or key < best_key:
                            best = sku; best_key = key; best_room = room
                    if best is None:
                        break
                    to_inch = sku_inch.get(best, "")
                    cost = 0.0 if best == cur else _co_cost(m, cur_inch, to_inch)
                    if s["remaining"] - cost < MIN_CAMPAIGN_MINS:
                        break
                    room = min(best_room, hr) if _ENDOFDAY_GT_CAP_ENABLED else best_room
                    avail = s["remaining"] - cost
                    mins = min(avail, room / rate)
                    qty = int(mins * rate)
                    # hard demand-cap clamp (sacred invariant): never build past the
                    # SKU's remaining demand headroom, even by a min-campaign rounding.
                    qty = min(qty, max(0, int(demand_remaining.get(best, 0.0)
                                              - projected_gt.get(best, 0.0))))
                    if mins < MIN_CAMPAIGN_MINS or qty <= 0:
                        break
                    co_type = ("start" if best == cur
                               else ("same_size_CO" if to_inch == cur_inch else "diff_size_CO"))
                    s["campaigns"].append((best, qty, co_type))
                    projected_gt[best] = projected_gt.get(best, 0.0) + qty
                    _fwd_added += qty
                    s["remaining"] -= (cost + mins)
                    if best != cur:
                        s["cur_sku"] = best
                        s["co_count"] += 1

        return {m: stg[m]["campaigns"] for m in machines if stg[m]["campaigns"]}

    if _MACHINE_ORDER_REVERSED:
        _pri = {"STAGE2": 0, "UNISTAGE": 1, "BJ": 2, "VMI": 3, "STAGE1": 9}
    else:
        _pri = {"VMI": 0, "BJ": 1, "UNISTAGE": 2, "STAGE2": 3, "STAGE1": 9}
    if _RI_RATIO_GLOBAL and demand_dict is not None and machine_total_demand is not None:
        # Approach 2: ratio-driven machine order — replace the fixed VMI->BJ->US
        # ->Stage2 group order with DESCENDING best-ratio order, so the machine
        # whose best-fit deficit SKU has the highest ratio claims first. A SKU is
        # "in play" for this ordering if it has curing demand this shift and unmet
        # demand remaining. Stage-1 last (carcass). Group priority + machine name
        # break ties deterministically.
        def _best_ratio(m: str) -> float:
            mtd  = machine_total_demand.get(m, 1e-9)
            best = 0.0
            for s in machine_skus.get(m, ()):
                if shift_cure_demand.get(s, 0.0) > 0 and demand_remaining.get(s, 0.0) > 0:
                    r = demand_dict.get(s, 0.0) / mtd
                    if r > best:
                        best = r
            return best
        sorted_machines = sorted(
            machine_skus.keys(),
            key=lambda m: (
                9 if _MACHINE_GROUP.get(m, "") == "STAGE1" else 0,
                -_best_ratio(m),
                _pri.get(_MACHINE_GROUP.get(m, ""), 9),
                m,
            ),
        )
    elif _SCARCITY_ORDER_ENABLED:
        # Scarcity-first: machines with the FEWEST eligible SKUs claim their
        # shared-SKU deficit before flexible machines can poach it. A captive
        # machine (e.g. 7301, eligible for only LSTL0) can build nothing else,
        # so it must lock in its specialty first — freeing flexible machines
        # (which share that SKU but have many others) for SKUs only they serve.
        # Stage-1 stays last (carcass, not GT). Eligible-count is the primary
        # key; group priority + machine name break ties deterministically.
        sorted_machines = sorted(
            machine_skus.keys(),
            key=lambda m: (
                9 if _MACHINE_GROUP.get(m, "") == "STAGE1" else 0,
                len(machine_skus.get(m, ())),
                _pri.get(_MACHINE_GROUP.get(m, ""), 9),
                m,
            ),
        )
    else:
        sorted_machines = sorted(
            machine_skus.keys(),
            key=lambda m: (_pri.get(_MACHINE_GROUP.get(m, ""), 9), m),
        )

    # Captive-first: stable re-sort putting non-Stage-1 captive machines (exactly
    # one eligible SKU) at the FRONT so they claim their sole SKU before flexible
    # machines drain its deficit. Stable → preserves the base order within groups.
    if _CAPTIVE_FIRST_ENABLED:
        sorted_machines = sorted(
            sorted_machines,
            key=lambda m: 0 if (len(machine_skus.get(m, ())) == 1
                                and _MACHINE_GROUP.get(m, "") != "STAGE1") else 1,
        )

    plan: dict[str, list] = {}

    for machine in sorted_machines:
        group = _MACHINE_GROUP.get(machine, "")
        _buf = GT_BUFFER_SHIFTS if group == "VMI" else 1

        def _deficit(sku: str, _b: float = _buf) -> float:
            built_ahead = projected_gt.get(sku, 0.0)
            need = shift_cure_demand.get(sku, 0.0) * _b
            gap  = need - built_ahead
            # Hard demand cap — subtract projected_gt (= gt_inventory + GT already
            # built THIS shift by earlier machines). demand_remaining is cured-
            # decremented and projected_gt is build-tracked, so
            # (demand_remaining - projected_gt) = D - total_GT_built_so_far.
            # Without the projected_gt term, two machines eligible for the same
            # SKU each build up to the full remaining demand in one shift → the
            # 1-4% overbuild seen across ~26 SKUs. This keeps total build ≤ demand.
            cap  = demand_remaining.get(sku, 0.0) - built_ahead
            return min(max(0.0, gap), max(0.0, cap))

        def _priority_tier(sku: str, d: float) -> tuple:
            # RI (has a live/CO'd press) always outranks NRI — tier 0 < tier 1.
            # NRI candidates are ranked by static demand[sku]/machine_total_demand[machine]
            # (never decremented) instead of raw deficit magnitude, so thin NRI SKUs aren't
            # structurally starved by high-volume ones competing for the same machine.
            _is_ri = press_count.get(sku, 0) > 0
            # Approach 1/2: when RI-ratio is on, RI candidates are ALSO ranked by
            # ratio (not raw deficit). RI keeps tier 0 (outranks NRI); only the
            # within-RI ordering switches from -deficit to -ratio.
            _ri_ratio = (_RI_RATIO_ENABLED or _RI_RATIO_GLOBAL) and _is_ri
            if (
                _BUILDING_RATIO_ENABLED
                and (press_count.get(sku, 0) <= 0 or _ri_ratio)
                and demand_dict is not None
                and machine_total_demand is not None
            ):
                ratio = demand_dict.get(sku, 0.0) / machine_total_demand.get(machine, 1e-9)
                return (0 if _is_ri else 1, -ratio)
            return (0, -d)

        eligible = machine_skus.get(machine, set())
        if not any(_deficit(s) > 0 for s in eligible):
            continue

        remaining = SHIFT_MINS
        co_count  = 0
        MAX_COS   = _max_cos(machine)
        cur_sku   = machine_current_sku.get(machine, "")
        cur_inch  = sku_inch.get(cur_sku, "")
        dom_inch  = _MACHINE_DOMINANT_INCH.get(str(machine), cur_inch)
        rate      = _bld_qty_per_shift(machine) / SHIFT_MINS
        campaigns: list[tuple] = []

        # ── Round-trip buffer sizing for cur_sku ────────────────────────────
        # Skip conditions (fall back to flat _buf): (1) machine has only one
        # eligible SKU — len(eligible)<=1, nowhere to rotate to; (2) no other
        # eligible SKU has unmet demand — demand_remaining<=0 filters it out;
        # (3) no other eligible SKU currently has a real curing-driven deficit
        # — _deficit(s)<=0 (flat-buf, no circularity) filters it out. When a
        # genuine rotation partner exists, size cur_sku's buffer to survive
        # CO(cur->partner) + partner's own deficit-driven dwell (floored at
        # MIN_CAMPAIGN_MINS) + CO(partner->cur), so cur_sku's press doesn't
        # starve while this machine is away serving the partner.
        effective_buf = _buf
        if _ROUND_TRIP_BUFFER_ENABLED and cur_sku and len(eligible) > 1:
            partner_candidates = [
                s for s in eligible
                if s != cur_sku
                and demand_remaining.get(s, 0.0) > 0
                and _deficit(s) > 0
            ]
            if partner_candidates:
                if machine in _INCH_FLEX_MACHINES:
                    # Flex machines may rotate to an OFF-inch partner (expensive
                    # diff-inch CO both ways). Size the own-inch buffer for the
                    # worst-case round trip so the own-inch press never starves
                    # while the machine is away — pick the partner with the
                    # largest CO round-trip cost, then deficit, then name.
                    partner = max(
                        partner_candidates,
                        key=lambda s: (
                            _co_cost(machine, cur_inch, sku_inch.get(s, ""))
                            + _co_cost(machine, sku_inch.get(s, ""), cur_inch),
                            _deficit(s), s,
                        ),
                    )
                else:
                    partner = max(partner_candidates, key=lambda s: (_deficit(s), s))
                partner_inch = sku_inch.get(partner, "")
                partner_dwell = max(
                    MIN_CAMPAIGN_MINS,
                    _deficit(partner) / rate if rate > 0 else MIN_CAMPAIGN_MINS,
                )
                round_trip_mins = (
                    _co_cost(machine, cur_inch, partner_inch)
                    + partner_dwell
                    + _co_cost(machine, partner_inch, cur_inch)
                )
                effective_buf = max(_buf, round_trip_mins / SHIFT_MINS)

        # ── Campaign 1: continue current SKU (no CO cost) ──────────────────
        if not cur_sku:
            best_start = min(
                (s for s in eligible if _deficit(s) > 0),
                key=lambda s: (
                    0 if sku_inch.get(s, "") == dom_inch else 1,
                    *_priority_tier(s, _deficit(s)),
                    s,
                ),
                default=None,
            )
            if best_start is not None:
                cur_sku  = best_start
                cur_inch = sku_inch.get(cur_sku, "")

        # urgent_co_set: co_target SKUs eligible on this machine with zero GT inventory.
        # These bypass allow_new_co=False guard in Campaign 2+ and the 30% cost guard.
        urgent_co_set = frozenset(
            s for s in co_target_skus
            if s in eligible and s != cur_sku
            and projected_gt.get(s, 0.0) == 0
            and demand_remaining.get(s, 0.0) > 0
        )

        # starving_set: eligible SKUs whose curing presses are running THIS shift
        # but have zero GT to cure (they will starve). Generalizes urgent_co_set
        # to any starving SKU so an otherwise-idle machine can feed it as a last
        # resort — see _STARVATION_FEED_ENABLED. Only relaxes the 30% cost guard;
        # inch preference and MIN_CAMPAIGN feasibility are untouched.
        starving_set = frozenset(
            s for s in eligible
            if s != cur_sku
            and shift_cure_demand.get(s, 0.0) > 0
            and projected_gt.get(s, 0.0) <= 0
            and demand_remaining.get(s, 0.0) > 0
        ) if _STARVATION_FEED_ENABLED else frozenset()

        # Default: allow non-dominant inch in Campaign 2+ unless Campaign 1 had unfinished demand.
        primary_demand_done = True

        # Reclamation guard (flex machines): if the machine carried over onto an
        # OFF-inch SKU but a dominant-inch SKU now has real deficit, do NOT
        # continue the off-inch campaign — force a return to the dominant inch
        # via Campaign 2+ (merged sort prefers dominant). This is the anti-
        # carry-over protection that lets us generalize the 7001/7003 soft-lock
        # to the high-demand VMIMAXX inches without the prior regression.
        # Disabled under the client inch rules: forcing a return to the dominant
        # inch IS a revisit, which Rule 1a forbids. (Expect the inch-flex gain to
        # go with it — that is the cost of one-way inch movement.)
        _flex_reclaim = (
            not _INCH_RULES_ENABLED
            and machine in _INCH_FLEX_MACHINES
            and cur_inch != dom_inch
            and any(sku_inch.get(s, "") == dom_inch and _deficit(s) > 0 for s in eligible)
        )
        if (cur_sku in eligible and _deficit(cur_sku, effective_buf) > 0
                and not _flex_reclaim):
            mins = min(remaining, _deficit(cur_sku, effective_buf) / rate if rate > 0 else remaining)
            qty  = int(mins * rate)
            if mins >= MIN_CAMPAIGN_MINS and qty > 0:
                campaigns.append((cur_sku, qty, "start"))
                projected_gt[cur_sku] = projected_gt.get(cur_sku, 0.0) + qty
                remaining -= mins

            # Track whether Campaign 1 exhausted demand (vs cut by shift time).
            # Soft-lock machines may serve non-dominant inch only when primary is done.
            primary_demand_done = _deficit(cur_sku, effective_buf) <= 0

        # ── Campaign 2+: CO to deficit SKUs ──────────────────────────────
        _is_flex = machine in _INCH_FLEX_MACHINES
        while remaining >= MIN_CAMPAIGN_MINS and co_count < MAX_COS:
            same_cands: list = []
            diff_cands: list = []
            flex_cands: list = []
            seen_in_plan = {sku for sku, _, _ in campaigns}

            for sku in eligible:
                d = _deficit(sku)
                if sku == cur_sku or d <= 0:
                    continue
                to_inch = sku_inch.get(sku, "")
                is_urgent = sku in urgent_co_set or sku in starving_set
                # Off-inch gate: soft-lock AND flex machines serve non-dominant
                # inch ONLY when primary (dominant, buffer-sized) demand is done
                # this shift. BJ/other machines were never locked — unchanged.
                if (machine in (_SOFT_LOCK_MACHINES | _INCH_FLEX_MACHINES)
                        and to_inch != dom_inch and not primary_demand_done):
                    continue
                cost = _co_cost(machine, cur_inch, to_inch)
                if remaining - cost < MIN_CAMPAIGN_MINS:
                    continue
                # 30% cost guard normally blocks expensive COs. Bypass for urgent
                # co_target SKUs, AND for a flex machine taking an off-inch CO once
                # its own inch is done this shift (primary_demand_done): the diff-
                # inch CO (up to 180 min) exceeds 30% of a shift, but the machine
                # would otherwise IDLE the whole shift, so any production beats idle.
                _flex_offinch_ok = (_is_flex and to_inch != dom_inch and primary_demand_done)
                if cost > 0.30 * remaining and not is_urgent and not _flex_offinch_ok:
                    continue
                revisit_penalty = 1 if sku in seen_in_plan else 0
                inch_penalty    = 0 if to_inch == dom_inch else 1
                tier, primary   = _priority_tier(sku, d)
                if _is_flex:
                    # Flex tuple puts inch_penalty FIRST so dominant inch always
                    # wins (strict reclamation), then off_key orders WITHIN the
                    # off-inch group only (dominant candidates get neutral 0).
                    if inch_penalty == 0:
                        off_key = 0.0
                    elif _INCH_FLEX_OFFINCH_ORDER == "starving_first":
                        _starving = (shift_cure_demand.get(sku, 0.0) > 0
                                     and projected_gt.get(sku, 0.0) <= 0
                                     and demand_remaining.get(sku, 0.0) > 0)
                        off_key = 0.0 if _starving else 1.0
                    else:  # "demand_first" — largest deficit first (best CO amortization)
                        off_key = -float(d)
                    flex_cands.append((inch_penalty, off_key, tier, primary,
                                       revisit_penalty, cost, sku))
                else:
                    bucket = same_cands if to_inch == cur_inch else diff_cands
                    bucket.append((tier, primary, inch_penalty, revisit_penalty, cost, sku))

            if _is_flex:
                if flex_cands:
                    flex_cands.sort()
                    best_cost, best_sku = flex_cands[0][-2], flex_cands[0][-1]
                    co_type = ("same_size_CO"
                               if sku_inch.get(best_sku, "") == cur_inch else "diff_size_CO")
                    if os.environ.get("FLEX_DEBUG"):
                        _noff = sum(1 for c in flex_cands if c[0] == 1)  # inch_penalty==1
                        if _noff:
                            print(f"    [FLEX] {machine} cur_inch={cur_inch} "
                                  f"off_cands={_noff} best={best_sku}({sku_inch.get(best_sku,'')}) "
                                  f"co={co_type} rem={remaining:.0f}")
                else:
                    break
            elif same_cands:
                same_cands.sort()
                _, _, _, _, best_cost, best_sku = same_cands[0]
                co_type = "same_size_CO"
            elif diff_cands:
                diff_cands.sort()
                _, _, _, _, best_cost, best_sku = diff_cands[0]
                co_type = "diff_size_CO"
            else:
                break

            avail = remaining - best_cost
            mins  = min(avail, _deficit(best_sku) / rate if rate > 0 else avail)
            qty   = int(mins * rate)
            if mins < MIN_CAMPAIGN_MINS or qty <= 0:
                break

            campaigns.append((best_sku, qty, co_type))
            projected_gt[best_sku] = projected_gt.get(best_sku, 0.0) + qty
            remaining -= (best_cost + mins)
            co_count  += 1
            cur_sku    = best_sku
            cur_inch   = sku_inch.get(cur_sku, "")

        if campaigns:
            plan[machine] = campaigns

    return plan


def _writeoff_stale_gt(gt_inventory, last_build_day, current_day, shelf_days=GT_SHELF_LIFE_DAYS):
    total = 0.0
    for sku in list(gt_inventory.keys()):
        qty = gt_inventory[sku]
        if qty > 0 and (current_day - last_build_day.get(sku, 0)) > shelf_days:
            total += qty
            gt_inventory[sku] = 0.0
    return total


# ══════════════════════════════════════════════════════════════════════════════
# OUTPUT WRITERS — same sheet names as legacy pipeline
# ══════════════════════════════════════════════════════════════════════════════

def _xl_header(ws, row: int, cols: list, bg="1F3864", fg="FFFFFF"):
    from openpyxl.styles import PatternFill, Font, Alignment
    fill = PatternFill("solid", fgColor=bg)
    font = Font(bold=True, size=10, color=fg)
    aln  = Alignment(horizontal="center", vertical="center")
    for ci, h in enumerate(cols, 1):
        c = ws.cell(row=row, column=ci, value=h)
        c.fill, c.font, c.alignment = fill, font, aln


def _xl_fill(ws, row_num: int, n_cols: int, hex_color: str):
    from openpyxl.styles import PatternFill
    fill = PatternFill("solid", fgColor=hex_color)
    for ci in range(1, n_cols + 1):
        ws.cell(row=row_num, column=ci).fill = fill


def _write_rolling_building_excel(
    output_path: str,
    bld_shift_rows: list,          # per-shift rows (includes CO sentinels)
    bld_co_events: list,           # building machine CO events
    df_day0: "pd.DataFrame",       # Day 0 curing consumption (SKU classification)
    sku_machine_map: dict,         # {sku: set(machines)} for eligibility
    opening_gt: dict,              # opening GT inventory
    demand_dict: dict,             # {sku: demand_qty} from demand file
    planning_days: int,
    n_curing_cos: int = 0,         # curing press CO count (from co_events)
    endday_gt_by_date: "dict | None" = None,  # {date_str: end-of-day total GT inventory}
) -> None:
    """
    Write building Excel matching the legacy bc_building_schedule output.

    Sheets:
      1. Shift Schedule         — per-shift rows (production + carcass + CHANGEOVER; title row 1, header row 3)
      2. Changeover Plan        — building machine CO events
      3. SKU Classification     — category summary from Day 0 consumption
      4. Daily GT & Carcass     — daily GT and carcass totals
      5. Demand Fulfillment (B2C) — per-SKU demand vs planned GT
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    _NAVY  = "1F3864"; _WHITE = "FFFFFF"; _GREEN = "E2EFDA"
    _AMBER = "FFF2CC"; _RED   = "FFE0E0"; _GREY  = "D3D3D3"
    _CO    = "FFC000"

    def _fill(h):   return PatternFill("solid", fgColor=h)
    def _bold(s=10): return Font(bold=True, size=s)
    def _ctr():      return Alignment(horizontal="center", vertical="center")

    # Present the shift schedule as a chronological timeline: ascending StartTime,
    # then Machine as a stable tiebreak. StartTime is "YYYY-MM-DD HH:MM" so its
    # lexical order IS chronological (post-midnight Shift-C rows carry the real
    # next-day date, so they sort correctly). Display-only — every aggregate
    # sheet below sums over the rows and is order-independent.
    bld_shift_rows = sorted(
        bld_shift_rows,
        key=lambda r: (str(r.get("StartTime", "")), str(r.get("Machine", ""))),
    )

    _SENTINEL = {"CHANGEOVER", "MOULD_CLEAN", "C/O", "CO"}

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    # ── Sheet 1: Shift Schedule (header at row 3 — matches legacy header=2 read) ─
    ws = wb.create_sheet("Shift Schedule")
    ws.cell(row=1, column=1, value="BC Building Schedule (Rolling Pipeline)").font = _bold(12)
    # Row 2: blank
    # Row 3: headers
    # Qty = units produced (0 on CHANGEOVER rows — a CO makes no tyres).
    # CO_Mins = changeover duration in minutes (0 on production/carcass rows).
    bld_cols = ["Machine", "Date", "Shift", "SKUCode", "Qty", "CO_Mins",
                "StartTime", "EndTime", "Machine_Group", "CO_Type"]
    _xl_header(ws, 3, bld_cols)
    for ri, row in enumerate(bld_shift_rows, 4):
        is_co = str(row.get("SKUCode", "")).upper() in _SENTINEL
        for ci, col in enumerate(bld_cols, 1):
            cell = ws.cell(row=ri, column=ci, value=row.get(col, ""))
            cell.alignment = _ctr()
            if is_co:
                cell.fill = _fill(_CO)
                cell.font = Font(bold=True)
    for col in ws.columns:
        w = max((len(str(c.value or "")) for c in col), default=8)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(w + 2, 38)

    # ── Sheet 2: Changeover Plan ───────────────────────────────────────────────
    ws_co = wb.create_sheet("Changeover Plan")
    co_cols = ["Machine", "Date", "Day", "From_SKU", "Target_SKU",
               "CO_Type", "CO_Cost_Mins", "CO_Day_Index", "Status"]
    _xl_header(ws_co, 1, co_cols)
    for ri, row in enumerate(bld_co_events, 2):
        for ci, col in enumerate(co_cols, 1):
            ws_co.cell(row=ri, column=ci, value=row.get(col, "")).alignment = _ctr()
    for col in ws_co.columns:
        w = max((len(str(c.value or "")) for c in col), default=8)
        ws_co.column_dimensions[get_column_letter(col[0].column)].width = min(w + 2, 38)

    # ── Sheet 3: SKU Classification ────────────────────────────────────────────
    ws_cat = wb.create_sheet("SKU Classification")
    cat_counts: dict[str, dict] = defaultdict(lambda: {"SKU_Count": 0, "Total_Demand": 0, "Avg_Priority": []})
    if df_day0 is not None and not df_day0.empty:
        for _, r in df_day0.iterrows():
            cat = str(r.get("Category", "Unknown"))
            sku = str(r.get("SKUCode", ""))
            dem = float(r.get("Demand_Qty", 0) or 0)
            pri = float(r.get("Priority_Score", 0) or 0)
            cat_counts[cat]["SKU_Count"]    += 1
            cat_counts[cat]["Total_Demand"] += dem
            cat_counts[cat]["Avg_Priority"].append(pri)
    cat_data = [
        {"Category": cat, "SKU_Count": v["SKU_Count"],
         "Total_Demand": int(v["Total_Demand"]),
         "Avg_Priority": round(sum(v["Avg_Priority"]) / max(len(v["Avg_Priority"]), 1), 4)}
        for cat, v in sorted(cat_counts.items())
    ]
    cat_cols = ["Category", "SKU_Count", "Total_Demand", "Avg_Priority"]
    _xl_header(ws_cat, 1, cat_cols)
    for ri, row in enumerate(cat_data, 2):
        for ci, col in enumerate(cat_cols, 1):
            ws_cat.cell(row=ri, column=ci, value=row.get(col, "")).alignment = _ctr()
    # KPI footer
    n_bld_co = sum(1 for r in bld_shift_rows if str(r.get("SKUCode","")).upper() in _SENTINEL)
    ws_cat.cell(row=len(cat_data) + 3, column=1, value="Building COs scheduled").font = _bold()
    ws_cat.cell(row=len(cat_data) + 3, column=2, value=n_bld_co)
    ws_cat.column_dimensions["A"].width = 22
    for ltr in "BCD":
        ws_cat.column_dimensions[ltr].width = 16

    # Production-only rows (CO/clean sentinels excluded) — used by the aggregate
    # sheets below (Daily GT & Carcass, Demand Fulfillment, Machine Utilization).
    # The separate "Shift Schedule (Clean)" sheet was removed; the full "Shift
    # Schedule" sheet (with production + carcass + CHANGEOVER rows) is the one output.
    prod_rows = [r for r in bld_shift_rows if str(r.get("SKUCode","")).upper() not in _SENTINEL]

    # ── Sheet 4: Daily GT & Carcass ────────────────────────────────────────────
    ws_daily = wb.create_sheet("Daily GT & Carcass")
    daily_agg: dict[str, dict] = defaultdict(lambda: {"GT_Produced": 0, "Carcass_Produced": 0,
                                                       "Total_Units": 0, "Active_SKUs": set()})
    for row in prod_rows:
        d    = str(row.get("Date", ""))
        mach = str(row.get("Machine", ""))
        qty  = int(row.get("Qty", 0) or 0)
        sku  = str(row.get("SKUCode", ""))
        if mach in _S1_MACHINES:
            daily_agg[d]["Carcass_Produced"] += qty
        else:
            daily_agg[d]["GT_Produced"] += qty
        daily_agg[d]["Total_Units"]  += qty
        daily_agg[d]["Active_SKUs"].add(sku)
    # EndDay_GT_Inventory: total GT held overnight (all SKUs, after curing + writeoff)
    # — audits the MAX_ENDOFDAY_GT_INVENTORY plant cap directly in the sheet.
    _eod = endday_gt_by_date or {}
    daily_cols = ["Date", "GT_Produced", "Carcass_Produced", "Total_Units",
                  "Active_SKUs", "Cumulative_GT", "EndDay_GT_Inventory"]
    _xl_header(ws_daily, 1, daily_cols)
    cum_gt = 0
    for ri, (date, v) in enumerate(sorted(daily_agg.items()), 2):
        cum_gt += v["GT_Produced"]
        vals = [date, v["GT_Produced"], v["Carcass_Produced"],
                v["Total_Units"], len(v["Active_SKUs"]), cum_gt,
                int(round(_eod.get(date, 0)))]
        for ci, val in enumerate(vals, 1):
            ws_daily.cell(row=ri, column=ci, value=val).alignment = _ctr()
    ws_daily.column_dimensions["A"].width = 14
    for ltr in "BCDEFG":
        ws_daily.column_dimensions[ltr].width = 16

    # ── Sheet 6: Demand Fulfillment (B2C) ─────────────────────────────────────
    ws_dem = wb.create_sheet("Demand Fulfillment (B2C)")
    prod_by_sku: dict[str, int] = defaultdict(int)
    for row in prod_rows:
        sku = str(row.get("SKUCode", ""))
        if sku and sku.upper() not in _SENTINEL and str(row.get("Machine","")) not in _S1_MACHINES:
            prod_by_sku[sku] += int(row.get("Qty", 0) or 0)

    dem_cols = ["SKUCode", "Category", "Priority", "Demand", "GT_Inventory",
                "Planned_Units", "Planned+GT", "Gap", "Fulfillment_Pct", "Status",
                "CycleTime_min", "Eligible_Machines", "Presses_Needed", "Skip_Reason"]
    _xl_header(ws_dem, 1, dem_cols)

    cat_map_d0: dict[str, str]   = {}
    pri_map_d0: dict[str, float] = {}
    if df_day0 is not None and not df_day0.empty:
        for _, r in df_day0.iterrows():
            s = str(r.get("SKUCode","")).strip()
            cat_map_d0[s] = str(r.get("Category",""))
            pri_map_d0[s] = float(r.get("Priority_Score", 0) or 0)

    # Average building CT per SKU (seconds → minutes), across eligible machines
    _PRESS_NORM = 86_400.0  # 60 days × 24h × 60min (presses_needed normalisation)
    def _avg_bld_ct(sku: str):
        machs = sku_machine_map.get(sku, set())
        cts   = [_BLD_CT_SEC.get(str(m), 120.0) / 60.0 for m in machs]
        return round(sum(cts) / len(cts), 1) if cts else None

    dem_rows_out = []
    for sku, dem in sorted(demand_dict.items(), key=lambda x: -x[1]):
        planned  = float(prod_by_sku.get(sku, 0))
        gt_inv   = float(opening_gt.get(sku, 0))
        # Total GT available to cure = building output THIS horizon + opening
        # inventory carried in on Day 0. The curing sheet consumes both, so its
        # Gap/Fulfillment already reflect this; Gap here now matches (previously
        # Gap = demand − planned ignored the opening GT_Inventory).
        planned_plus_gt = planned + gt_inv
        gap      = max(0, int(dem) - int(planned_plus_gt))
        fill_pct = round(100 * planned_plus_gt / dem, 1) if dem > 0 else 0.0
        status   = ("FULLY MET" if planned_plus_gt >= dem * 0.95
                    else "PARTIAL" if planned_plus_gt > 0 else "UNMET")
        avg_ct   = _avg_bld_ct(sku)
        p_needed = (round(dem * avg_ct / _PRESS_NORM, 2) if avg_ct else "NA")
        dem_rows_out.append({
            "SKUCode": sku, "Category": cat_map_d0.get(sku, ""),
            "Priority": round(pri_map_d0.get(sku, 0), 7),
            "Demand": int(dem),
            "GT_Inventory": int(gt_inv),
            "Planned_Units": int(planned), "Planned+GT": int(planned_plus_gt),
            "Gap": gap,
            "Fulfillment_Pct": f"{fill_pct}%", "Status": status,
            "CycleTime_min": avg_ct if avg_ct is not None else "NA",
            "Eligible_Machines": len(sku_machine_map.get(sku, set())) or "NA",
            "Presses_Needed": p_needed,
            "Skip_Reason": "" if planned > 0 else (
                "No eligible building machine" if not sku_machine_map.get(sku) else ""),
        })
    status_colors = {"FULLY MET": _GREEN, "PARTIAL": _AMBER, "UNMET": _RED}
    pu_col_idx = dem_cols.index("Planned_Units") + 1
    for ri, row in enumerate(dem_rows_out, 2):
        color = status_colors.get(row["Status"], _GREY)
        for ci, col in enumerate(dem_cols, 1):
            cell = ws_dem.cell(row=ri, column=ci, value=row.get(col, ""))
            cell.fill = _fill(color)
            cell.alignment = _ctr()
            if ci == pu_col_idx:
                cell.font = Font(bold=True)
    ws_dem.column_dimensions["A"].width = 34
    for ltr in "BCDEFGHIJKLMN":
        ws_dem.column_dimensions[ltr].width = 15

    # KPI footer (matches building_b2c.py _append_b2c_sheets format)
    n_full  = sum(1 for r in dem_rows_out if r["Status"] == "FULLY MET")
    n_part  = sum(1 for r in dem_rows_out if r["Status"] == "PARTIAL")
    n_unmet = sum(1 for r in dem_rows_out if r["Status"] == "UNMET")
    tot_bld = sum(r["Planned_Units"] for r in dem_rows_out)
    tot_dem = sum(r["Demand"]        for r in dem_rows_out)
    tot_avail = sum(r["Planned+GT"]  for r in dem_rows_out)  # built + opening GT
    kpi_pct = round(100 * tot_bld / tot_dem, 1) if tot_dem else 0.0
    kpi_pct_avail = round(100 * tot_avail / tot_dem, 1) if tot_dem else 0.0
    n_co_bld = sum(1 for r in bld_shift_rows if str(r.get("SKUCode","")).upper() in _SENTINEL)
    footer = len(dem_rows_out) + 3
    ws_dem.cell(row=footer,   column=1, value="KPI SUMMARY").font = Font(bold=True)
    ws_dem.cell(row=footer+1, column=1, value="Total Customer Demand (units)")
    ws_dem.cell(row=footer+1, column=2, value=tot_dem)
    ws_dem.cell(row=footer+2, column=1, value="Total GT Built (units)")
    ws_dem.cell(row=footer+2, column=2, value=tot_bld)
    _kv = ws_dem.cell(row=footer+3, column=1, value="KPI — GT Built / Customer Demand")
    _kv.font = Font(bold=True)
    _kv2 = ws_dem.cell(row=footer+3, column=2, value=f"{kpi_pct}%")
    _kv2.font = Font(bold=True)
    _kv3 = ws_dem.cell(row=footer+4, column=1,
                       value="KPI — (GT Built + Opening GT) / Customer Demand")
    _kv3.font = Font(bold=True)
    _kv4 = ws_dem.cell(row=footer+4, column=2, value=f"{kpi_pct_avail}%")
    _kv4.font = Font(bold=True)
    ws_dem.cell(row=footer+5, column=1, value="Total SKUs in demand file")
    ws_dem.cell(row=footer+5, column=2, value=len(dem_rows_out))
    ws_dem.cell(row=footer+6, column=1, value="Fully Met (≥95% of demand, built+opening GT)")
    ws_dem.cell(row=footer+6, column=2, value=n_full)
    ws_dem.cell(row=footer+7, column=1, value="Partial (0 < built+opening GT < 95%)")
    ws_dem.cell(row=footer+7, column=2, value=n_part)
    ws_dem.cell(row=footer+8, column=1, value="Unmet (built+opening GT = 0)")
    ws_dem.cell(row=footer+8, column=2, value=n_unmet)
    ws_dem.cell(row=footer+9, column=1, value="Total Building COs")
    ws_dem.cell(row=footer+9, column=2, value=n_co_bld)
    ws_dem.cell(row=footer+10, column=1, value=f"Curing COs scheduled (≤{MAX_CHANGEOVERS_PER_DAY}/day)")
    ws_dem.cell(row=footer+10, column=2, value=n_curing_cos)

    # ── Sheet 7: Machine Utilization ──────────────────────────────────────────
    ws_util = wb.create_sheet("Machine Utilization")

    _GREEN_U = "E2EFDA"; _AMBER_U = "FFF2CC"; _RED_U = "FFE0E0"

    def _mgroup(m: str) -> str:
        if m in {"6001","6002","6003","6004","7001","7002","7003","7004"}: return "VMI"
        if m in {"7101","7102","7103","7104","7105","7106","7201"}:        return "BJ"
        if m in {"7501","7502","7503"}:                                    return "UNI_NARROW"
        if m in {"8201","8301","8302","8501","8502","7301"}:               return "Stage-2"
        return "Stage-1"

    avail_per_mach = planning_days * 3 * SHIFT_MINS  # 44,640 min

    # Production time per machine
    mach_prod_mins: dict[str, float] = defaultdict(float)
    mach_carcass:   dict[str, int]   = defaultdict(int)
    mach_gt:        dict[str, int]   = defaultdict(int)
    mach_skus:      dict[str, set]   = defaultdict(set)
    for row in prod_rows:
        m   = str(row.get("Machine", ""))
        qty = int(row.get("Qty", 0) or 0)
        sku = str(row.get("SKUCode", ""))
        ct_sec = _BLD_CT_SEC.get(m, 120.0)
        mach_prod_mins[m] += qty * ct_sec / 60.0
        if m in _S1_MACHINES:
            mach_carcass[m] += qty
        else:
            mach_gt[m] += qty
        mach_skus[m].add(sku)

    # CO time per machine from bld_co_events
    mach_co_mins: dict[str, float] = defaultdict(float)
    mach_co_count: dict[str, int]  = defaultdict(int)
    for ev in bld_co_events:
        m    = str(ev.get("Machine", ""))
        cost = float(ev.get("CO_Cost_Mins", 0) or 0)
        mach_co_mins[m]  += cost
        mach_co_count[m] += 1

    # All 39 building machines — explicit set so zero-production machines (e.g. 8101)
    # are always included regardless of whether they appear in production or CO dicts.
    _ALL_39_MACHINES = frozenset({
        "6801","6802","6803","6909","6911","7601","7701",
        "7801","7802","7803","7804","8001","8002","8003","8101",  # Stage-1 (15)
        "8201","8301","8302","8501","8502","7301",                # Stage-2 (6)
        "7001","7002","7003","7004","6001","6002","6003","6004",  # VMI (8)
        "7101","7102","7103","7104","7105","7106","7201",         # BJ (7)
        "7501","7502","7503",                                     # UNI_NARROW (3)
    })
    all_machines = sorted(
        _ALL_39_MACHINES,
        key=lambda m: (
            {"VMI":0,"BJ":1,"UNI_NARROW":2,"Stage-2":3,"Stage-1":4}.get(_mgroup(m), 5),
            m
        )
    )

    # Summary header
    def _u_avg(machines):
        vals = [mach_prod_mins[m] / avail_per_mach for m in machines]
        return sum(vals) / len(vals) if vals else 0
    avg_gt_mach = [m for m in all_machines if _mgroup(m) != "Stage-1"]
    avg_util = _u_avg(avg_gt_mach) if avg_gt_mach else 0
    high = sum(1 for m in avg_gt_mach if mach_prod_mins[m]/avail_per_mach >= 0.80)
    low  = sum(1 for m in avg_gt_mach if mach_prod_mins[m]/avail_per_mach < 0.40)
    ws_util.cell(row=1, column=1, value=(
        f"Avg GT-machine util (prod only): {avg_util:.1%}  |  "
        f"High(≥80%): {high}  |  Low(<40%): {low}  |  "
        f"Note: Stage-1 always <77% by design (15 machines for 11.5-equiv S2 demand)"
    )).font = Font(bold=True, size=10)

    util_cols = [
        "Machine", "Machine_Group", "Available_Mins",
        "GT_Built", "Carcass_Built",
        "Prod_Mins", "CO_Mins", "Idle_Mins",
        "Util_Pct", "CO_Pct", "Idle_Pct", "Occupancy_Pct",
        "SKUs_Served", "COs_Done",
    ]
    # Util_Pct = production only. Occupancy_Pct = (prod + CO)/available — this is
    # the metric stored in jkt_plan_kpis / jkt_plan_capacityUtilisation.
    _xl_header(ws_util, 2, util_cols)

    for ri, m in enumerate(all_machines, 3):
        prod  = mach_prod_mins[m]
        co    = mach_co_mins[m]
        idle  = max(0.0, avail_per_mach - prod - co)
        util_pct = prod / avail_per_mach
        co_pct   = co   / avail_per_mach
        idle_pct = idle / avail_per_mach
        grp = _mgroup(m)
        # Color: red <40%, amber 40-85%, green ≥80% (Stage-1 always amber by design)
        if grp == "Stage-1":
            color = _AMBER_U
        elif util_pct >= 0.85:
            color = _GREEN_U
        elif util_pct >= 0.40:
            color = _AMBER_U
        else:
            color = _RED_U
        vals = [
            m, grp, avail_per_mach,
            mach_gt.get(m, 0), mach_carcass.get(m, 0),
            round(prod), round(co), round(idle),
            util_pct, co_pct, idle_pct, util_pct + co_pct,
            len(mach_skus[m]), mach_co_count[m],
        ]
        for ci, val in enumerate(vals, 1):
            cell = ws_util.cell(row=ri, column=ci, value=val)
            cell.fill = _fill(_GREEN_U if grp != "Stage-1" and util_pct >= 0.80 else
                              _AMBER_U if util_pct >= 0.40 or grp == "Stage-1" else _RED_U)
            cell.alignment = _ctr()
            if ci in (9, 10, 11, 12):  # percent columns
                cell.number_format = "0.0%"

    # Totals row
    tot_row = len(all_machines) + 3
    ws_util.cell(row=tot_row, column=1, value="TOTAL / AVERAGE").font = Font(bold=True)
    ws_util.cell(row=tot_row, column=3, value=avail_per_mach * len(all_machines)).font = Font(bold=True)
    ws_util.cell(row=tot_row, column=4, value=sum(mach_gt.values())).font = Font(bold=True)
    ws_util.cell(row=tot_row, column=5, value=sum(mach_carcass.values())).font = Font(bold=True)
    ws_util.cell(row=tot_row, column=6, value=round(sum(mach_prod_mins.values()))).font = Font(bold=True)
    ws_util.cell(row=tot_row, column=7, value=round(sum(mach_co_mins.values()))).font = Font(bold=True)
    tot_idle = max(0, avail_per_mach * len(all_machines)
                   - sum(mach_prod_mins.values()) - sum(mach_co_mins.values()))
    ws_util.cell(row=tot_row, column=8, value=round(tot_idle)).font = Font(bold=True)
    avg_pct = sum(mach_prod_mins.values()) / (avail_per_mach * len(all_machines))
    cell = ws_util.cell(row=tot_row, column=9, value=avg_pct)
    cell.font = Font(bold=True); cell.number_format = "0.0%"

    for ltr_idx, w in enumerate([14,16,16,12,14,12,10,10,10,10,10,12,12,10], 1):
        ws_util.column_dimensions[get_column_letter(ltr_idx)].width = w
    ws_util.freeze_panes = "A3"

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    wb.save(output_path)
    print(f"  [Rolling] Building output → {output_path}")


def _write_rolling_curing_excel(
    output_path: str,
    cure_shift_rows: list,         # per-shift press events
    cure_co_events: list,          # curing press CO events (Planned + Dynamic) — Changeover Plan sheet
    press_stats: dict,             # {press: {running_mins, co_mins, clean_mins, skus, cycles, units}}
    press_sku_stats: dict,         # {(press,sku): {cycles, units, mins_used}}
    daily_cured: dict,             # {date_str: qty}
    sku_cured: dict,               # {sku: qty}
    closing_gt_bal: dict,          # {sku: gt_remaining}
    build_by_shift_sku: dict,      # {(date,shift): {sku: qty}} — for GT diagnostic
    opening_gt: dict,
    demand_dict: dict,             # {sku: demand_qty}
    cure_ct_map: dict,             # {sku: ct_min}
    curing_allowable: dict,        # {sku: [press_ids]}
    planning_days: int,
    plan_start: datetime,
    df_day0: "pd.DataFrame | None" = None,  # Day 0 consumption (has Priority_Score, Category)
    mould_life: "dict | None" = None,       # {press: remaining mould life (cycles) at horizon end}
    mould_info: "dict | None" = None,       # end-of-plan mould state for the Mould Tracker sheet
) -> None:
    """
    Write curing Excel matching the legacy bc_curing_b2c output.

    Sheets:
      1. Demand Fulfillment  — per-SKU demand vs cured + fulfillment %
      2. Machine Utilization — per-press running / idle / CO minutes + utilization %
      3. Shift Schedule      — per-shift press events (RUNNING / CO / MOULD_CLEAN)
      4. Mould Tracker       — placeholder (mould cycle not tracked in rolling)
      5. Machine Schedule    — per (press, SKU) summary
      6. Daily Cured tyres   — daily cured totals
      7. GT Gap Diagnostic   — closing GT balance by SKU
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    _GREEN = "C6EFCE"; _AMBER = "FFEB9C"; _RED   = "FFC7CE"; _LGREY = "D9D9D9"
    _NAVY  = "1F3864"; _WHITE = "FFFFFF"; _BLUE  = "DCE6F1"; _LYELL = "FFF2CC"
    _DGREY = "F2F2F2"; _ORANGE= "FFC000"

    def _fill(h): return PatternFill("solid", fgColor=h)
    def _bold(s=10, color="000000"): return Font(bold=True, size=s, color=color)
    def _ctr(): return Alignment(horizontal="center", vertical="center", wrap_text=True)

    def _hdr(ws, row, cols, bg=_NAVY, fg=_WHITE):
        for ci, h in enumerate(cols, 1):
            c = ws.cell(row=row, column=ci, value=h)
            c.fill = _fill(bg); c.font = _bold(10, fg); c.alignment = _ctr()

    # ── Build priority + category lookup from df_day0 ─────────────────────────
    pri_map: dict[str, float] = {}
    cat_map: dict[str, str]   = {}
    if df_day0 is not None and not df_day0.empty:
        for _, r in df_day0.iterrows():
            s = str(r.get("SKUCode", "")).strip()
            pri_map[s] = float(r.get("Priority_Score", 0) or 0)
            cat_map[s] = str(r.get("Category", ""))

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    avail_mins = planning_days * 3 * SHIFT_MINS

    # ── Sheet 1: Demand Fulfillment ───────────────────────────────────────────
    ws = wb.create_sheet("Demand Fulfillment")
    # CT_available: the SKU's real curing cycle time if it exists in the curing CT
    # table (cure_ct_map), else "NA" — meaning no data, so CycleTime_min fell back to
    # the DEFAULT_CURING_CT (17 min).
    cols = ["SKUCode", "Priority", "Demand", "GT_Inventory", "Planned_Units",
            "Gap", "Fulfillment_Pct", "Status", "CycleTime_min", "CT_available",
            "Eligible_Machines", "Presses_Needed", "Skip_Reason"]
    _hdr(ws, 1, cols)
    status_fill = {"FULLY MET": _GREEN, "PARTIAL": _AMBER, "UNMET": _RED, "NO DATA": _LGREY}
    rows_out = []
    for sku, dem in sorted(demand_dict.items(), key=lambda x: -x[1]):
        planned = float(sku_cured.get(sku, 0))
        gap     = max(0, dem - planned)
        pct     = planned / dem if dem > 0 else 0.0
        ct      = cure_ct_map.get(sku, DEFAULT_CURING_CT)
        cap_day = _cure_qty_per_shift(ct) * 3 * planning_days
        p_needed= max(1, round(dem / cap_day)) if cap_day > 0 else "-"
        status  = ("FULLY MET" if planned >= dem * 0.999
                   else "PARTIAL" if planned > 0
                   else ("NO DATA" if dem <= 0 else "UNMET"))
        rows_out.append({
            "SKUCode": sku, "Priority": round(pri_map.get(sku, 0.0), 4),
            "Demand": int(dem),
            "GT_Inventory": int(opening_gt.get(sku, 0)),
            "Planned_Units": int(planned), "Gap": int(gap),
            "Fulfillment_Pct": pct, "Status": status,
            "CycleTime_min": round(ct, 2),
            "CT_available": round(cure_ct_map[sku], 2) if sku in cure_ct_map else "NA",
            "Eligible_Machines": len(curing_allowable.get(sku, [])),
            "Presses_Needed": p_needed, "Skip_Reason": "",
        })
    for ri, r in enumerate(rows_out, 2):
        f = _fill(status_fill.get(r["Status"], _WHITE))
        for ci, h in enumerate(cols, 1):
            cell = ws.cell(row=ri, column=ci, value=r[h])
            cell.fill = f; cell.alignment = _ctr()
            if h == "Fulfillment_Pct":
                cell.number_format = "0.0%"
    tr = len(rows_out) + 3
    ws.cell(row=tr, column=1, value="TOTAL").font = _bold(11)
    ws.cell(row=tr, column=3, value=sum(r["Demand"]        for r in rows_out)).font = _bold(11)
    ws.cell(row=tr, column=5, value=sum(r["Planned_Units"] for r in rows_out)).font = _bold(11)
    ws.cell(row=tr, column=6, value=sum(r["Gap"]           for r in rows_out)).font = _bold(11)
    tot_d = sum(r["Demand"] for r in rows_out); tot_p = sum(r["Planned_Units"] for r in rows_out)
    tc = ws.cell(row=tr, column=7, value=tot_p / tot_d if tot_d else 0)
    tc.font = _bold(11); tc.number_format = "0.0%"
    ws.column_dimensions["A"].width = 34
    for ltr in "BCDEFGHIJKL": ws.column_dimensions[ltr].width = 15
    ws.freeze_panes = "A2"

    # ── Sheet 2: Machine Utilization ──────────────────────────────────────────
    ws = wb.create_sheet("Machine Utilization")
    all_presses = sorted(press_stats)
    if all_presses:
        avg_u    = sum(press_stats[p]["running_mins"] / avail_mins for p in all_presses) / len(all_presses)
        total_co = sum(press_stats[p]["co_mins"] for p in all_presses)
        high     = sum(1 for p in all_presses if press_stats[p]["running_mins"] / avail_mins >= 0.90)
        low      = sum(1 for p in all_presses if press_stats[p]["running_mins"] / avail_mins < 0.05)
        ws.cell(row=1, column=1,
                value=(f"Avg util: {avg_u:.1%}  |  High(≥90%): {high}  |  Idle(<5%): {low}  |  "
                       f"Presses: {len(all_presses)}  |  Total CO_Mins: {int(total_co):,}")
                ).font = _bold(10)
    _ml = mould_life or {}
    # Utilization_Pct = production only. Occupancy_Pct = (used + CO + mould-clean)/
    # available — the metric stored in jkt_plan_kpis / jkt_plan_capacityUtilisation.
    u_cols = ["Machine", "Available_Mins", "Used_Mins", "CO_Mins", "Mould_Clean_Mins",
              "Idle_Mins", "Utilization_Pct", "CO_Pct", "Mould_Clean_Utilization_%",
              "Idle_Pct", "Occupancy_Pct", "SKUs_Count", "total_cycle", "Total_Units",
              "Remaining_Mould_Life"]
    _hdr(ws, 2, u_cols)
    for ri, press in enumerate(all_presses, 3):
        s     = press_stats[press]
        used  = s["running_mins"]
        co    = s["co_mins"]
        clean = s["clean_mins"]
        idle  = max(0, avail_mins - used - co - clean)
        pct       = used  / avail_mins if avail_mins else 0.0
        co_pct    = co    / avail_mins if avail_mins else 0.0
        clean_pct = clean / avail_mins if avail_mins else 0.0
        idle_pct  = idle  / avail_mins if avail_mins else 0.0
        color = _GREEN if pct >= 0.90 else (_AMBER if pct >= 0.60 else _RED)
        vals  = [press, avail_mins, round(used), round(co), round(clean), round(idle),
                 pct, co_pct, clean_pct, idle_pct, pct + co_pct + clean_pct,
                 len(s["skus"]), s["cycles"], s["units"],
                 _ml.get(press, MOULD_CLEAN_CYCLES)]
        for ci, v in enumerate(vals, 1):
            cell = ws.cell(row=ri, column=ci, value=v)
            cell.fill = _fill(color); cell.alignment = _ctr()
            if ci in (7, 8, 9, 10, 11): cell.number_format = "0.0%"
    for ci in range(1, len(u_cols) + 1):
        ws.column_dimensions[ws.cell(row=2, column=ci).column_letter].width = 18
    ws.freeze_panes = "A3"

    # ── Sheet 3: Shift Schedule ────────────────────────────────────────────────
    ws = wb.create_sheet("Shift Schedule")
    # Qty = tyres cured. CO_Mins / Mould_Clean_Mins = minutes this press-shift spent
    # in changeover / mould clean (covers planned full-shift COs, dynamic mid-shift
    # COs and their overhang) — so every CO is visible here, not only in the
    # Changeover Plan sheet.
    ss_cols = ["Date", "Shift", "Machine", "SKUCode", "StartTime", "EndTime",
               "Qty", "CO_Mins", "Mould_Clean_Mins",
               "CycleTime_min", "GT_Inventory", "Remarks"]
    _hdr(ws, 1, ss_cols)
    s_fill = {"A": _fill(_BLUE), "B": _fill(_LYELL), "C": _fill(_DGREY)}
    for ri, r in enumerate(cure_shift_rows, 2):
        st = r.get("_status", "RUNNING")
        if st == "CHANGEOVER":
            f = _fill(_ORANGE)
        elif st == "MOULD_CLEAN":
            f = _fill(_AMBER)
        else:
            f = s_fill.get(r.get("Shift", ""), _fill(_WHITE))
        for ci, h in enumerate(ss_cols, 1):
            cell = ws.cell(row=ri, column=ci, value=r.get(h, ""))
            cell.fill = f; cell.alignment = _ctr()
            if st in ("CHANGEOVER", "MOULD_CLEAN"):
                cell.font = Font(bold=True)
    ws.column_dimensions["A"].width = 14; ws.column_dimensions["D"].width = 32
    ws.column_dimensions["I"].width = 16; ws.freeze_panes = "A2"

    # ── Sheet 3b: Changeover Plan — every curing press CO (Planned + Dynamic) ──
    # Makes dynamic/reactive COs visible in the output, not just the console log.
    ws = wb.create_sheet("Changeover Plan")
    co_cols = ["Date", "Day", "Shift", "Press", "From_SKU", "Target_SKU", "CO_Type"]
    _hdr(ws, 1, co_cols)
    _co_sorted = sorted(
        (cure_co_events or []),
        key=lambda e: (int(e.get("Day", 0)),
                       {"A": 0, "B": 1, "C": 2}.get(e.get("Shift", "A"), 0),
                       str(e.get("Press", ""))),
    )
    for ri, e in enumerate(_co_sorted, 2):
        f = _fill(_ORANGE) if e.get("CO_Type") == "Dynamic" else _fill(_LYELL)
        for ci, h in enumerate(co_cols, 1):
            cell = ws.cell(row=ri, column=ci, value=e.get(h, ""))
            cell.fill = f; cell.alignment = _ctr()
            if h == "CO_Type":
                cell.font = Font(bold=True)
    _n_plan = sum(1 for e in _co_sorted if e.get("CO_Type") == "Planned")
    _n_dyn  = sum(1 for e in _co_sorted if e.get("CO_Type") == "Dynamic")
    _foot = len(_co_sorted) + 3
    ws.cell(row=_foot, column=1,
            value=f"Total curing COs: {len(_co_sorted)}  "
                  f"(Planned: {_n_plan}, Dynamic: {_n_dyn})").font = Font(bold=True)
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["E"].width = 32; ws.column_dimensions["F"].width = 32
    ws.freeze_panes = "A2"

    # ── Sheet 4: Mould Tracker ────────────────────────────────────────────────
    # End-of-plan mould state per curing press: which 2 moulds are mounted, whether
    # they are eligible for the SKU the press ends on (feasibility proof), and how
    # over-subscribed that SKU's mould pool is (contention — explains unmet demand).
    ws = wb.create_sheet("Mould Tracker")
    if not mould_info:
        _hdr(ws, 1, ["MouldNo", "Compatible_SKUs", "Life_Remaining", "Assigned_Machine"])
        ws.cell(row=2, column=1,
                value="Mould gate OFF — no mould state to report (run with MOULD_GATE=1)"
                ).font = Font(italic=True, color="888888")
        ws.column_dimensions["A"].width = 20; ws.column_dimensions["B"].width = 44
    else:
        _pm  = mould_info.get("press_moulds", {})
        _fs  = mould_info.get("final_sku", {})
        _msk = mould_info.get("mould_skus", {})
        _sm  = mould_info.get("sku_moulds", {})
        _pc  = mould_info.get("press_count", {})
        _sc  = mould_info.get("scorer") or {}
        _ev  = mould_info.get("events", []) or []
        _asg = mould_info.get("assignments", []) or []
        # swaps per press (for the header "never swapped" count)
        _swaps: dict = defaultdict(int)
        for _e in _ev:
            _swaps[_e["press"]] += 1
        # summary header — CO provenance + contention + movement totals for the client
        _n_moulds = sum(len(v) for v in _pm.values())
        _n_shared = sum(1 for m, s in _msk.items() if len(s) > 1)
        _n_fixed  = sum(1 for _p in _pm if _swaps.get(_p, 0) == 0)
        ws.cell(row=1, column=1, value=(
            f"Moulds mounted: {_n_moulds}  |  Shared moulds (serve >1 SKU): {_n_shared}  |  "
            f"Total mould swaps this month: {len(_ev)}  |  "
            f"Presses that never swapped: {_n_fixed}/{len(_pm)}  |  "
            f"Day-0 2nd-mould top-ups: {mould_info.get('day0_topups', 0)}  |  "
            f"Mould-blocked COs: {mould_info.get('blocked', 0)}  |  "
            f"Retargeted: {mould_info.get('retargeted', 0)}"
            + (f"  |  Pulled-forward: {_sc.get('pullfwd', 0)}  |  "
               f"Idle-fill/dynamic: {_sc.get('dynamic', 0)}" if _sc else "")
        )).font = _bold(10)
        # EXPANDED: one row per (press, mould) building a SKU — Day-0 opening plus
        # every changeover mount. So a press that runs one SKU all month = 2 rows
        # (its 2 moulds); a press that swaps N times = 2 × (N+1) rows. Total >> 167.
        mt_cols = ["Press", "Mould", "SKU_Built", "Day_Mounted", "Mould_Eligible_For_SKU",
                   "Shared_Mould", "SKU_Eligible_Mould_Pool", "Presses_On_SKU",
                   "SKU_Tooling_Pressure"]
        _hdr(ws, 2, mt_cols)
        _rows = []
        for _a in _asg:
            _p   = _a["press"]; _sku = _a["sku"]; _day = _a["day"]
            _pool = len(_sm.get(_sku, set()))
            _npr  = int(_pc.get(_sku, 0))
            _tp   = round((_npr * 2) / _pool, 2) if _pool else ""
            for _m in _a.get("moulds", []):
                _elig   = "Yes" if _sku in _msk.get(_m, set()) else "No"
                _shared = "Yes" if len(_msk.get(_m, set())) > 1 else "No"
                _rows.append([_p, _m, _sku, _day, _elig, _shared, _pool, _npr, _tp])
        _rows.sort(key=lambda r: (str(r[0]), int(r[3]), str(r[1])))   # press, day, mould
        for _ri, _r in enumerate(_rows, 3):
            for _ci, _v in enumerate(_r, 1):
                _cell = ws.cell(row=_ri, column=_ci, value=_v)
                _cell.alignment = _ctr()
                if _ci == 5 and _v == "No":       # mould not eligible for the SKU — flag red
                    _cell.font = _bold(10, "CC0000")
        ws.cell(row=1, column=1).value = ws.cell(row=1, column=1).value + \
            f"  |  Rows (press×mould×SKU-run): {len(_rows)}"
        ws.column_dimensions["A"].width = 12
        ws.column_dimensions["B"].width = 40
        ws.column_dimensions["C"].width = 30
        for _ltr in ("D", "E", "F", "G", "H", "I"):
            ws.column_dimensions[_ltr].width = 16
        ws.freeze_panes = "A3"

        # ── Sheet 4b: Mould Movement (one row per swap — the actual "tracker") ────
        ws2 = wb.create_sheet("Mould Movement")
        ws2.cell(row=1, column=1, value=(
            f"Every mould change during the month ({len(_ev)} swaps). A press keeps its "
            f"moulds until it changes over to an SKU those moulds cannot cure, then swaps."
        )).font = _bold(10)
        mv_cols = ["Day", "Press", "New_SKU", "Moulds_Mounted", "Moulds_Removed"]
        _hdr(ws2, 2, mv_cols)
        for _ri, _e in enumerate(
                sorted(_ev, key=lambda e: (e["day"], str(e["press"]))), 3):
            for _ci, _v in enumerate([
                    _e["day"], _e["press"], _e["sku"],
                    ", ".join(_e.get("added", [])),
                    ", ".join(_e.get("removed", []))], 1):
                ws2.cell(row=_ri, column=_ci, value=_v).alignment = _ctr()
        ws2.column_dimensions["A"].width = 8
        ws2.column_dimensions["B"].width = 12
        ws2.column_dimensions["C"].width = 30
        ws2.column_dimensions["D"].width = 40
        ws2.column_dimensions["E"].width = 40
        ws2.freeze_panes = "A3"

    # ── Sheet 5: Machine Schedule ─────────────────────────────────────────────
    ws = wb.create_sheet("Machine Schedule")
    ms_rows = []
    for (press, sku), s in sorted(press_sku_stats.items()):
        if s["units"] == 0:
            continue
        ct        = cure_ct_map.get(sku, DEFAULT_CURING_CT)
        days_used = s["mins_used"] / (3 * SHIFT_MINS) if s["mins_used"] else 0
        ms_rows.append({
            "Machine": press, "SKUCode": sku,
            "Priority": round(pri_map.get(sku, 0.0), 4),
            "CycleTime_min": round(ct, 2),
            "Cycles": s["cycles"], "Units_Planned": s["units"],
            "Mins_Used": round(s["mins_used"]), "Days_Used": round(days_used, 2),
        })
    ms_rows.sort(key=lambda r: (r["Machine"], -r["Units_Planned"]))
    tot_u = sum(r["Units_Planned"] for r in ms_rows)
    tot_c = sum(r["Cycles"] for r in ms_rows)
    ws.cell(row=1, column=1,
            value=f"Press-SKU pairs: {len(ms_rows)}  |  Total Units: {tot_u:,}  |  Total Cycles: {tot_c:,}"
            ).font = _bold(10)
    ms_cols = ["Machine", "SKUCode", "Priority", "CycleTime_min",
               "Cycles", "Units_Planned", "Mins_Used", "Days_Used"]
    _hdr(ws, 2, ms_cols)
    for ri, r in enumerate(ms_rows, 3):
        for ci, h in enumerate(ms_cols, 1):
            ws.cell(row=ri, column=ci, value=r.get(h, "")).alignment = _ctr()
    ws.column_dimensions["A"].width = 12; ws.column_dimensions["B"].width = 34
    for ltr in "CDEFGH": ws.column_dimensions[ltr].width = 14
    ws.freeze_panes = "A3"

    # ── Sheet 6: Daily Cured tyres ────────────────────────────────────────────
    ws = wb.create_sheet("Daily Cured tyres")
    _hdr(ws, 1, ["Date", "Cured_Qty"])
    total_c = 0
    for d in range(planning_days):
        date_str = (plan_start + timedelta(days=d)).strftime("%Y-%m-%d")
        qty      = int(daily_cured.get(date_str, 0))
        ws.cell(row=d + 2, column=1, value=date_str).alignment = _ctr()
        c = ws.cell(row=d + 2, column=2, value=qty)
        c.alignment = _ctr()
        c.fill = _fill(_BLUE) if qty > 0 else _fill(_RED)
        total_c += qty
    tr = planning_days + 3
    ws.cell(row=tr, column=1, value="TOTAL").font = _bold(11)
    t = ws.cell(row=tr, column=2, value=total_c)
    t.font = _bold(11); t.fill = _fill(_GREEN)
    ws.column_dimensions["A"].width = 14; ws.column_dimensions["B"].width = 14

    # ── Sheet 7: GT Gap Diagnostic ────────────────────────────────────────────
    ws = wb.create_sheet("GT Gap Diagnostic")
    _hdr(ws, 1, ["SKUCode", "GT_Built", "GT_Cured", "Closing_Balance", "Reason"])
    built_per_sku: dict[str, float] = defaultdict(float)
    for sku_qty in build_by_shift_sku.values():
        for sku, qty in sku_qty.items():
            built_per_sku[str(sku)] += float(qty)
    for sku, qty in opening_gt.items():
        built_per_sku[str(sku)] += float(qty)
    press_skus = set(sku_cured.keys())
    ri = 2
    for sku in sorted(closing_gt_bal, key=lambda s: -closing_gt_bal[s]):
        bal = closing_gt_bal[sku]
        if bal < 0.5:
            continue
        built  = built_per_sku.get(sku, 0.0)
        cured  = float(sku_cured.get(sku, 0))
        if sku not in press_skus:
            reason = "NO_PRESS"; fill = _fill(_RED)
        elif cured > 0:
            reason = "DEMAND_MET"; fill = _fill(_AMBER)
        else:
            reason = "RESIDUAL"; fill = _fill(_LGREY)
        for ci, val in enumerate([sku, round(built), round(cured), round(bal), reason], 1):
            c = ws.cell(row=ri, column=ci, value=val)
            if ci == 4: c.fill = fill
            c.alignment = _ctr()
        ri += 1
    # Summary + note (matches curing_b2c.py format)
    ws.cell(row=ri + 1, column=1, value="TOTAL").font = _bold(11)
    total_built_diag  = sum(round(built_per_sku.get(s, 0)) for s in closing_gt_bal if closing_gt_bal[s] >= 0.5)
    total_cured_diag  = sum(round(float(sku_cured.get(s, 0))) for s in closing_gt_bal if closing_gt_bal[s] >= 0.5)
    total_bal_diag    = sum(round(closing_gt_bal[s]) for s in closing_gt_bal if closing_gt_bal[s] >= 0.5)
    ws.cell(row=ri + 1, column=2, value=total_built_diag).font = _bold(11)
    ws.cell(row=ri + 1, column=3, value=total_cured_diag).font = _bold(11)
    tc = ws.cell(row=ri + 1, column=4, value=total_bal_diag)
    tc.font = _bold(11); tc.fill = _fill(_RED)
    note = ws.cell(row=ri + 3, column=1,
        value=("NO_PRESS = built but no curing press (main gap cause)  |  "
               "DEMAND_MET = carry-over to next month  |  "
               "RESIDUAL = last-shift lag (next month opening inventory)"))
    note.font = _bold(9)
    ws.column_dimensions["A"].width = 34
    for ltr in "BCDE": ws.column_dimensions[ltr].width = 16

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    wb.save(output_path)
    print(f"  [Rolling] Curing output  → {output_path}")


# ══════════════════════════════════════════════════════════════════════════════
# ROLLING PIPELINE — main function
# ══════════════════════════════════════════════════════════════════════════════

def run_rolling_pipeline(
    demand_path:    str | None = None,
    plan_start:     datetime | None = None,
    planning_days:  int | None = None,
    build_output:   str | None = None,
    curing_output:  str | None = None,
) -> dict:
    """
    Rolling day-by-day B2C pipeline.

    Generates building and curing schedules simultaneously:
      - Building machines are assigned based on actual GT deficit each day
      - Curing presses cure only what GT is available (GT-limited)
      - Both schedules written to the same Excel format as the legacy pipeline
    """
    demand_path   = demand_path   or DEMAND_FILE
    plan_start    = plan_start    or PLAN_START
    planning_days = planning_days or PLANNING_DAYS
    build_output  = build_output  or BUILD_OUTPUT
    curing_output = curing_output or CURING_OUTPUT

    print("\n" + "=" * 70)
    print("  ROLLING PIPELINE — Pre-computation")
    print("=" * 70)

    # ── A: CO schedule ────────────────────────────────────────────────────────
    print("  [Rolling] Computing CO schedule …")
    # Surplus-release 5b guard needs a per-SKU building-supply estimate (curing
    # scheduler is otherwise building-independent). Compute it only when the
    # feature is on; failure falls back to None (guard becomes a no-op).
    _buildable_rate = None
    if _SURPLUS_RELEASE_ENABLED:
        try:
            from cbc_env import make_engine as _mk
            _buildable_rate = _compute_buildable_rate(_mk(), demand_path)
            print(f"  [Rolling] Surplus-release ON — buildable_rate for "
                  f"{len(_buildable_rate)} SKUs (5b guard)")
        except Exception as _e:
            print(f"  [Rolling] buildable_rate computation failed ({_e}); 5b guard disabled")
    cc_result = run_dynamic_consumption(
        demand_path=demand_path, output_path=CC_OUTPUT,
        plan_start=plan_start, planning_days=planning_days,
        max_co_per_day=MAX_CHANGEOVERS_PER_DAY,
        buildable_rate=_buildable_rate,
    )
    co_events = cc_result["co_events"]
    df_day0   = cc_result["df_day0"]
    print(f"  [Rolling] {len(co_events)} curing CO events pre-computed")

    co_by_day: dict[int, list] = defaultdict(list)
    for ev in co_events:
        co_by_day[int(ev["day"])].append((ev["press"], ev["old_sku"], ev["new_sku"]))

    # Dynamic planner takes over CO decisions entirely — discard the static
    # schedule's events so every remaining co_by_day consumer (daily_co_count
    # seeding, today_cos lookup, the reactive mechanism's "tomorrow" lookahead)
    # is neutralized by this single reset. df_day0/co_events themselves are
    # still needed (SKU classification) so run_dynamic_consumption still runs.
    if _DYNAMIC_CO_PLANNER_ENABLED or _ROLLING_HORIZON_CO_ENABLED or _RATIO_CO_ALLOCATION_ENABLED:
        co_by_day = {}

    # ── B: Master data ────────────────────────────────────────────────────────
    from cbc_env import make_engine
    engine = make_engine()

    cetl = ConsumptionETL(engine)
    df_ct_raw = cetl.load_cycle_times()
    cure_ct_map: dict[str, float] = {
        str(r["SKUCode"]): float(r["CycleTime_min"])
        for _, r in df_ct_raw.iterrows() if r.get("CycleTime_min")
    }

    # Building CTs are hardcoded in _BLD_CT_SEC above (sourced from plant data)

    # ── _ROLLING_HORIZON_CO_ENABLED pre-computation (zero cost when off) ───────
    # Static, reused unchanged for every one of the 31 daily schedule() calls —
    # physical press<->SKU compatibility doesn't change day to day. Uses
    # cc_result["ct_map"], not cure_ct_map above, which has no NaN guard/default
    # (see plan Finding 3) — schedule() has no NaN guard of its own.
    _df_curing_allow_static: "pd.DataFrame | None" = None
    _rolling_co_scheduler: "COScheduler | None" = None
    if _ROLLING_HORIZON_CO_ENABLED:
        _df_curing_allow_static = cetl.load_curing_allowable()
        _rolling_co_scheduler = COScheduler()

    machine_skus: dict[str, set]  = defaultdict(set)
    sku_machine_map: dict[str, set] = defaultdict(set)
    s1_sku_to_machines: dict[str, set] = defaultdict(set)  # Stage-1 carcass eligibility (kept
    # OUT of machine_skus/sku_machine_map so it never feeds the Stage-2 deficit signal — see
    # the STAGE1 skip below. Used only for Step 3b carcass-utilization simulation.
    sku_inch: dict[str, str] = {}
    try:
        from building_b2c import B2C_ETL as _BETL
        _etl = _BETL(engine)
        df_allow    = _etl.load_machine_allowable()
        sku_to_size = _etl.load_sku_sizes()
        sku_inch = {str(k): str(v).strip().replace('"', "") for k, v in sku_to_size.items()}

        # Fallback: derive inch from characters 9–10 of SKU code (1-indexed) for
        # any SKU missing from the size master.  E.g. "1325216814085SURL0"[8:10] = "14".
        for _, row in df_allow.iterrows():
            sku = str(row["SKUCode"])
            if sku not in sku_inch or not sku_inch[sku]:
                if len(sku) >= 10:
                    sku_inch[sku] = sku[8:10]

        # Hard-inch filter: restricts each machine to its dominant inch(es).
        # Prevents carry-over locking: without this, a machine that does a diff_size_CO
        # in Shift A gets locked onto the non-dominant inch for the rest of the month
        # via machine_current_sku carry-over, starving dominant-inch SKUs.
        # VMIMAXX inches confirmed from May 2026 plant inch-run study (CLAUDE.md §inch-run).
        _HARD = {
            # VMIMAXX — dominant-inch locks (each machine serves its primary inch group)
            # 7001 (dom=16") and 7003 (dom=15") are SOFT-locked: no hard filter here.
            # They prefer dominant inch via inch_penalty sort key; serve others when primary done.
            "6001": {"14"},             # dom=14" — 3 machines share 14" (6001/7002/7004)
            "6002": {"15"},             # dom=15" — 2 machines share 15" (6002/7003)
            "6003": {"17", "18"},       # dom=17" — sole 17"/18" machine; serves HURL1/HRHT0
            "6004": {"16"},             # dom=16" — primary 16" machine; 7001 is secondary
            "7002": {"14"},             # dom=14"
            "7004": {"14"},             # dom=14"
            # BJ — no hard filter; dominant-inch preference via _MACHINE_DOMINANT_INCH + inch_penalty.
            # Removing hard locks allows BJ machines to serve off-dominant SKUs in their
            # allowable table (e.g. 7103 can produce HURL0 at 14" when 13" demand is met).
            # UNI_NARROW — physically cannot run 14"+ (genuine hard constraint)
            # 7501 dominant=12" but can also run 13" (confirmed allowable)
            "7501":{"12","13"},  "7502":{"13"},        "7503":{"13"},
        }
        # Client inch rules, variant A: the +/-2 anchor band REPLACES the
        # dominant-inch hard locks, so every machine starts from its full DB
        # allowable and is bound only by anchor+/-2 and the no-revisit rule.
        # Variant B (_INCH_BAND_REPLACES_HARD=False) keeps _HARD as well, so the
        # machine is bound by the INTERSECTION (most restrictive) — this also
        # preserves the UNI_NARROW 12"/13" physical limit.
        if _INCH_RULES_ENABLED and _INCH_BAND_REPLACES_HARD:
            _HARD = {}
        # Inch-flexibility: drop flex machines from _HARD so their full DB
        # allowable (all eligible inches) passes through — off-inch access is
        # then gated at Campaign-2+ (primary_demand_done) + reclamation guard.
        for _fm in _INCH_FLEX_MACHINES:
            _HARD.pop(_fm, None)
        for idx, row in df_allow.iterrows():
            sku = str(row["SKUCode"]); si = sku_inch.get(sku, "")
            ml  = list(row.get("Machines", []) or [])
            df_allow.at[idx, "Machines"] = [
                m for m in ml if str(m) not in _HARD or si in _HARD[str(m)]
            ]
        for _, row in df_allow.iterrows():
            sku = str(row["SKUCode"])
            for m in (row.get("Machines") or []):
                m_str = str(m)
                # Rolling pipeline tracks GT only (not carcass).
                # Stage-1 machines produce carcass that feeds Stage-2 implicitly.
                # Excluding them from machine_skus prevents:
                #   (a) phantom projected_gt updates inside _assign_building_shift
                #       that fool Stage-2 into seeing deficit=0 for Stage-2 SKUs
                #   (b) Stage-1 machines "wasting" shifts on carcass campaigns whose
                #       output (correctly) doesn't add to gt_inventory (line 1388)
                # The planning assumption: Stage-1 always has capacity to supply
                # whatever Stage-2 needs (validated: Stage-1 util ≈ 33% by design).
                if _MACHINE_GROUP.get(m_str, "") == "STAGE1":
                    s1_sku_to_machines[sku].add(m_str)
                    continue
                machine_skus[m_str].add(sku)
                sku_machine_map[sku].add(m_str)
        print(f"  [Rolling] Allowable map: {len(machine_skus)} machines")
    except Exception as _e:
        print(f"  [Rolling] Allowable map: failed ({_e})")

    # ── C: Press state ────────────────────────────────────────────────────────
    df_moulds = cetl.load_running_moulds()
    press_state: dict[str, dict] = {}
    for _, r in df_moulds.iterrows():
        press_state[str(r["Machine"])] = {"sku": str(r["SKUCode"]), "status": "RUNNING"}

    press_count: dict[str, int] = defaultdict(int)
    for st in press_state.values():
        press_count[st["sku"]] += 1

    # ── C2: Mould pool (client mould-availability gate) ───────────────────────
    # sku_moulds[sku] = eligible mould IDs; mould_owner[mould] = press it's mounted
    # on (None = free in storage). Day-0 mounting comes from the running-moulds
    # MouldNos list (2 moulds per press). A press CO/run is feasible only if 2
    # eligible moulds are free (or already on this press). See _MOULD_GATE_ENABLED.
    _sku_moulds: dict[str, set] = {}
    _mould_skus: dict[str, set] = {}
    _mould_owner: dict[str, str] = {}
    _press_moulds: dict[str, set] = {}
    mould_blocked_cos = 0
    mould_day0_topups = 0                        # Day-0 single-mould presses given a 2nd mould
    # Full (press, SKU, 2-moulds) timeline — Day-0 opening + every changeover mount.
    # Expanded in the Mould Tracker sheet to one row per (press, mould) building a SKU.
    mould_assignments: list = []
    _mould_selfcheck = [0]                      # debug: count RUNNING-without-2-moulds
    _mould_gate = _MOULD_GATE_ENABLED          # local (may disable on load failure)
    _mould_opt  = _MOULD_OPT_ENABLED           # Phase-2 optimisation (needs the gate)
    mould_retargeted_cos = 0                    # planned COs saved by retarget-on-block
    if _mould_gate:
        try:
            _elig = cetl.load_mould_eligibility()
            _sku_moulds = {k: set(v) for k, v in _elig["sku_moulds"].items()}
            _mould_skus = {k: set(v) for k, v in _elig["mould_skus"].items()}
        except Exception as _e:
            print(f"  [Rolling] mould eligibility load FAILED ({_e}); gate disabled")
            _mould_gate = False
    if _mould_gate:
        # Seed Day-0 mounted moulds from the running-moulds MouldNos list.
        for _, r in df_moulds.iterrows():
            _p = str(r["Machine"]); _sku0 = str(r["SKUCode"])
            _mn = r.get("MouldNos", []) or []
            for _m in _mn:
                _m = str(_m).strip()
                if not _m or _m.lower() == "nan":
                    continue
                _mould_owner[_m] = _p
                _press_moulds.setdefault(_p, set()).add(_m)
                # Fold Day-0 orphan pairs into eligibility so the seed never
                # self-violates (5 mounted moulds aren't listed for their SKU).
                _sku_moulds.setdefault(_sku0, set()).add(_m)
                _mould_skus.setdefault(_m, set()).add(_sku0)

        # Day-0 second-mould top-up: a few presses list only 1 mould in
        # Daily_Running_Moulds (e.g. 75214, 9404). A press physically has 2 cavities,
        # so give each such press a 2nd COMPATIBLE FREE mould for its Day-0 SKU (the
        # realistic floor state). Deterministic (sorted free pool). Runs AFTER all
        # presses are seeded so the free pool is complete.
        _topped = 0
        for _, r in df_moulds.iterrows():
            _p = str(r["Machine"]); _sku0 = str(r["SKUCode"])
            _have = _press_moulds.get(_p, set())
            if len(_have) >= 2:
                continue
            _elig0 = _sku_moulds.get(_sku0, set())
            _free0 = sorted(m for m in _elig0 if _mould_owner.get(m) is None)
            for _m in _free0:
                if len(_press_moulds.get(_p, set())) >= 2:
                    break
                _mould_owner[_m] = _p
                _press_moulds.setdefault(_p, set()).add(_m)
                _topped += 1
        mould_day0_topups = _topped
        if _topped:
            print(f"  [Rolling] Day-0 second-mould top-up: {_topped} moulds "
                  f"assigned to single-mould presses")

        # Record each press's Day-0 opening (press, SKU, 2 moulds) so the expanded
        # tracker timeline starts from the floor state before any changeover.
        for _, r in df_moulds.iterrows():
            _p = str(r["Machine"]); _sku0 = str(r["SKUCode"])
            mould_assignments.append({
                "day": 1, "press": _p, "sku": _sku0,
                "moulds": sorted(_press_moulds.get(_p, set())),
            })

    # Old moulds whose release is deferred to end-of-day: a planned CO placed in
    # shift B/C keeps running its OLD sku (needs its old moulds) until the CO fires,
    # so the old moulds must stay reserved to the press until the day completes —
    # freeing them at day-start let another press grab them and made the OLD sku's
    # pre-CO production physically mould-less. {press: set(old moulds to free)}.
    _deferred_free: dict[str, set] = {}

    # Mould-movement log: every time a press mounts a DIFFERENT mould (a genuine swap
    # on a changeover), record it so the output can show moulds moving over the month
    # (the tracker sheet's end-state snapshot alone hides this). {day, press, sku,
    # added, removed}. _cur_day is set at the top of each day loop iteration.
    mould_events: list = []
    _cur_day = [0]

    def _try_mount(press: str, new_sku: str, defer_free: bool = False) -> bool:
        """Allocate 2 eligible moulds for `new_sku` on `press`, or return False.

        Prefers moulds already on this press (shared → no movement, leaves more free
        moulds for others). `defer_free`=True (planned COs): keep the press's OLD
        moulds reserved to it until end-of-day (they still serve the old SKU in
        pre-CO shifts). `defer_free`=False (reactive mid-shift CO): free old moulds
        immediately (the press is CHANGEOVER that shift, not producing).
        """
        if not _mould_gate:
            return True
        elig = _sku_moulds.get(new_sku, set())
        if len(elig) < 2:
            return False
        own = _press_moulds.get(press, set())
        # candidates: eligible moulds free OR already on this press. SORTED — set
        # iteration order is hash-randomised → would break determinism.
        reuse = sorted(m for m in own if m in elig)
        free  = sorted(m for m in elig if _mould_owner.get(m) is None and m not in reuse)
        chosen = (reuse + free)[:2]
        if len(chosen) < 2:
            return False
        old_extra = [m for m in own if m not in chosen]
        if defer_free:
            # keep old moulds reserved to the press (still serving old sku); free
            # them at end-of-day. The press temporarily "holds" old ∪ new.
            _deferred_free.setdefault(press, set()).update(old_extra)
            _press_moulds[press] = set(own) | set(chosen)
        else:
            for m in old_extra:
                _mould_owner[m] = None
            _press_moulds[press] = set(chosen)
        for m in chosen:
            _mould_owner[m] = press
        # Full assignment: this press now builds `new_sku` on these 2 moulds.
        mould_assignments.append({
            "day": _cur_day[0], "press": press, "sku": new_sku,
            "moulds": list(chosen),
        })
        _added = [m for m in chosen if m not in own]
        if _added:                                  # a genuine mould swap (not pure reuse)
            mould_events.append({
                "day":     _cur_day[0],
                "press":   press,
                "sku":     new_sku,
                "added":   _added,
                "removed": list(old_extra),
            })
        return True

    def _n_free_for(new_sku: str, press: str) -> int:
        """How many eligible moulds could `press` mount for `new_sku` right now
        (own moulds reusable for free + currently-free eligible moulds). ≥2 ⇒ a
        _try_mount would succeed."""
        elig = _sku_moulds.get(new_sku, set())
        if len(elig) < 2:
            return 0
        own = _press_moulds.get(press, set())
        reuse = sum(1 for m in own if m in elig)
        free  = sum(1 for m in elig if _mould_owner.get(m) is None and m not in own)
        return reuse + free

    def _pick_retarget(press: str):
        """Phase 2b: a planned CO whose new-SKU has no free moulds would idle the
        press on its (usually demand-done) old SKU. Instead retarget it to the
        NEEDIEST SKU the press is allowable for that still has 2 free moulds.
        Returns the SKU or None. Deterministic (sorted candidate list, tuple key)."""
        _cur = press_state.get(press, {}).get("sku")
        best = None
        best_key = None
        for s in press_allow_skus.get(press, ()):     # pre-sorted list
            if s == _cur:
                continue
            rem = demand_remaining.get(s, 0.0)
            if rem <= 0:
                continue
            if _n_free_for(s, press) < 2:
                continue
            npr = max(1, press_count.get(s, 0))
            key = (rem / npr, rem, s)                  # neediest per serving-press first
            if best_key is None or key > best_key:
                best_key = key
                best = s
        return best

    def _release_deferred():
        """Free the deferred old moulds at end-of-day (their CO has now completed).

        `_olds` is exactly the set of old moulds NOT kept as current (computed as
        old_extra in _try_mount), so free them unconditionally: drop from the
        press's owned set AND release ownership so they return to the free pool.
        """
        for _p, _olds in _deferred_free.items():
            _cur = _press_moulds.get(_p, set())
            for _m in _olds:
                if _mould_owner.get(_m) == _p:
                    _mould_owner[_m] = None
                _cur.discard(_m)
            _press_moulds[_p] = _cur
        _deferred_free.clear()

    # Curing allowable: {sku: [press_ids]} for demand fulfillment sheet
    curing_allowable: dict[str, list] = defaultdict(list)
    for press, st in press_state.items():
        curing_allowable[st["sku"]].append(press)

    # ── D: Opening GT inventory ───────────────────────────────────────────────
    try:
        from curing_b2c import _load_opening_gt
        opening_gt = _load_opening_gt(engine)
    except Exception:
        opening_gt = {}
    gt_inventory: dict[str, float] = defaultdict(float, opening_gt)

    # ── D2: Mould-clean state (per press) ─────────────────────────────────────
    # remaining_mould_life = cycles a press may still run before a mandatory clean
    # (starts at MOULD_CLEAN_CYCLES; v2 will load real opening life). clean_carry =
    # minutes of an in-progress clean owed at the start of a press's next shift.
    mould_life:  dict[str, int]   = defaultdict(lambda: MOULD_CLEAN_CYCLES)
    clean_carry: dict[str, float] = defaultdict(float)
    # Minutes of an in-progress CURING CHANGEOVER owed at the start of a press's
    # next shift. A dynamic (instant) CO fires mid-shift the moment demand is met,
    # so its CURING_CO_CHANGEOVER_MINS overhang spills past the shift boundary —
    # the new SKU therefore starts MID-shift, not at the boundary.
    co_carry: dict[str, float] = defaultdict(float)

    # ── E: Demand ─────────────────────────────────────────────────────────────
    demand_df = pd.read_excel(demand_path)
    sku_col = next((c for c in demand_df.columns if "SKU"  in str(c)), demand_df.columns[0])
    qty_col = next(
        (c for c in demand_df.columns
         if any(x in str(c) for x in ("Requirement","Demand","Qty","Quantity"))),
        demand_df.columns[1],
    )
    # Sum duplicate SKU rows (a SKU may appear on several demand line-items).
    # MUST match curing_consumption.py's groupby-sum, otherwise the building
    # demand universe silently diverges from curing's: a plain dict keyed by
    # SKUCode would keep only the LAST duplicate row and drop the rest (e.g.
    # LSTL0 71,000 + 20,680 → only 20,680 survives), capping building far below
    # the real demand while curing presses still pull the full amount → mass
    # starvation on the biggest SKUs.
    _dq = demand_df[[sku_col, qty_col]].copy()
    _dq[sku_col] = _dq[sku_col].astype(str).str.strip()
    _dq[qty_col] = pd.to_numeric(_dq[qty_col], errors="coerce")
    _dq = _dq.dropna(subset=[qty_col])
    demand_dict: dict[str, float] = _dq.groupby(sku_col)[qty_col].sum().to_dict()
    _n_rows_raw = len(_dq)
    demand_remaining: dict[str, float] = dict(demand_dict)
    total_demand = sum(demand_dict.values())
    if _n_rows_raw != len(demand_dict):
        print(f"  [Rolling] Demand: collapsed {_n_rows_raw} rows → "
              f"{len(demand_dict)} unique SKUs (summed {_n_rows_raw - len(demand_dict)} duplicate rows)")
    print(f"  [Rolling] Demand: {len(demand_dict)} SKUs, {total_demand:,.0f} units")

    # Static per-machine total demand for _BUILDING_RATIO_ENABLED — computed once
    # from the fixed demand file, never decremented (see _priority_tier).
    machine_total_demand: dict[str, float] = {
        m: sum(demand_dict.get(s, 0.0) for s in skus)
        for m, skus in machine_skus.items()
    }

    # ── _DYNAMIC_CO_PLANNER_ENABLED pre-computation (zero cost when off) ───────
    # Curing press <-> SKU physical compatibility (mould/allowable-machines) —
    # run_rolling_pipeline never loaded this before; the existing reactive
    # dynamic_co_tracker mechanism has no compatibility check at all (fine at
    # its current one-press-at-a-time scale, not fine once scaled to the whole
    # press fleet once a day, so this is required here).
    press_to_demand_targets: dict[str, list] = {}
    press_total_demand: dict[str, float] = {}
    sku_to_press_count: dict[str, int] = {}
    ri_skus: frozenset = frozenset()
    nri_skus: frozenset = frozenset()
    if _DYNAMIC_CO_PLANNER_ENABLED or _RATIO_CO_ALLOCATION_ENABLED or _EARLY_CO_ENABLED:
        df_curing_allow = cetl.load_curing_allowable()
        _all_demand_skus = set(demand_dict.keys())
        sku_to_presses: dict[str, set] = {}
        for _, _row in df_curing_allow.iterrows():
            _sku = str(_row["SKUCode"]).strip()
            if _sku in _all_demand_skus:
                _machines = _row.get("Machines", [])
                if _machines:
                    sku_to_presses[_sku] = {str(_p) for _p in _machines}
        for _sku, _presses in sku_to_presses.items():
            for _p in _presses:
                press_to_demand_targets.setdefault(_p, []).append(_sku)
        press_total_demand = {
            _p: sum(demand_dict.get(_s, 0.0) for _s in _targets)
            for _p, _targets in press_to_demand_targets.items()
        }
        # Target-side scarcity for _RATIO_CO_RICH_RANKING_ENABLED — how many
        # presses could physically serve this SKU (fewer = protect it).
        sku_to_press_count = {_s: len(_p) for _s, _p in sku_to_presses.items()}
        ri_skus  = frozenset(df_day0.loc[df_day0["Category"] == "Runner-In",     "SKUCode"])
        nri_skus = frozenset(df_day0.loc[df_day0["Category"] == "Non-Runner-In", "SKUCode"])

    # Priority score for dynamic CO target selection (higher = serve first).
    # ConsolidatedPriorityScore (v1) — min-max normalise the per-SKU requirement,
    # computed from demand_dict (already summed per SKU) so it matches
    # curing_consumption.load_demand exactly and is identical whether the demand
    # came from a local Excel or the cloud DB `jkt_demand` table (no priority
    # column). v1 uses REQUIREMENT ONLY; any priority column in the file is ignored.
    #   score = (q - q_min) / (q_max - q_min)   (1.0 for all if q_max == q_min)
    priority_score_map: dict[str, float] = {}
    if demand_dict:
        _qs = list(demand_dict.values())
        _qmin, _qmax = min(_qs), max(_qs)
        _span = _qmax - _qmin
        priority_score_map = {
            _s: ((_q - _qmin) / _span if _span > 0 else 1.0)
            for _s, _q in demand_dict.items()
        }

    # ── F: Machine current SKU ────────────────────────────────────────────────
    machine_current_sku: dict[str, str] = {}
    try:
        df_running_bld = _etl.load_running_machines()
        machine_current_sku = {str(r["Machine"]): str(r["SKUCode"]) for _, r in df_running_bld.iterrows()}
    except Exception:
        pass
    if _SEED_FROM_PLANT_RUNNING:
        try:
            _pr = pd.read_excel(_PLANT_RUNNING_FILE)
            _seed = {}
            for _, r in _pr.iterrows():
                _m   = str(r["Machine_Code"]).strip()
                _sku = str(r["SKUCode"]).strip()
                if not _sku or _sku.lower() == "nan" or "IDLE" in _sku:
                    continue
                _seed[_m] = _sku
            machine_current_sku = _seed
            print(f"  [Rolling] Seeded machine_current_sku from plant running file: {len(_seed)} machines")
        except Exception as _e:
            print(f"  [Rolling] Plant-running seed FAILED: {_e}")

    # ── G: Machine pools + cross-shift minute tracker ─────────────────────────
    # Pool: fixed 2–3 same-inch SKUs per machine, ordered by Day-1 urgency.
    # Replaced only when a pool SKU's demand_remaining hits 0.
    machine_pool: dict[str, list[str]] = _build_machine_pools(
        machine_skus=machine_skus,
        sku_inch=sku_inch,
        demand_dict=demand_dict,
        press_count=press_count,
        cure_ct_map=cure_ct_map,
        planning_days=planning_days,
    )
    print(f"  [Rolling] Pools: {sum(len(v) for v in machine_pool.values())} SKU slots "
          f"across {len(machine_pool)} machines")

    # machine_minutes_on_sku: cumulative minutes each machine has spent on its
    # current SKU without a CO.  Reset to 0 on CO; incremented per campaign.
    # Guards against micro-campaigns across shift boundaries (< MIN_CAMPAIGN_MINS).
    machine_minutes_on_sku: dict[str, float] = {m: 0.0 for m in machine_skus}

    # ── Client inch-rule state (persists across the whole horizon) ────────────
    # machine_anchor_inch: the inch of the machine's FIRST assignment — fixes its
    #   +/-_INCH_BAND_WIDTH band for the month (Rule 2).
    # machine_used_inches: every inch the machine has run — an inch it has left
    #   can never be re-used (Rule 1a).
    machine_anchor_inch: dict[str, str] = {}
    machine_used_inches: dict[str, set] = {}
    # Stage-1 carcass machines are scheduled in Step 3b, not in
    # _assign_building_shift, so they need their own current-inch tracker.
    s1_current_inch: dict[str, str] = {}

    # Anchor the +/-2 band to the machine's REAL Day-0 inch whenever the plant
    # running state is known (machine_current_sku seeded above). Without this the
    # anchor is whatever the Day-1 greedy happens to pick first, which then locks
    # the machine for the whole month — the plant state is a far better anchor
    # (e.g. the plant runs a 17" machine that the greedy abandoned entirely).
    if _INCH_RULES_ENABLED and machine_current_sku:
        for _m0, _sku0 in machine_current_sku.items():
            _i0 = sku_inch.get(str(_sku0), "")
            if not _i0:
                continue
            machine_anchor_inch.setdefault(str(_m0), _i0)
            machine_used_inches.setdefault(str(_m0), set()).add(_i0)
            if str(_m0) in _S1_MACHINES:
                s1_current_inch.setdefault(str(_m0), _i0)
        print(f"  [Rolling] Inch anchors seeded from Day-0 machine state: "
              f"{len(machine_anchor_inch)} machines")

    # ══════════════════════════════════════════════════════════════════════════
    # Data accumulators (matching output sheet formats)
    # ══════════════════════════════════════════════════════════════════════════
    bld_shift_rows:  list[dict] = []   # building Shift Schedule rows (+ CO sentinels)
    bld_co_events:   list[dict] = []   # building machine CO events
    cure_shift_rows: list[dict] = []   # curing Shift Schedule rows
    cure_co_events:  list[dict] = []   # curing press CO events (Planned + Dynamic)
    press_stats:     dict = defaultdict(lambda: {
        "running_mins": 0.0, "co_mins": 0.0, "clean_mins": 0.0,
        "skus": set(), "cycles": 0, "units": 0,
    })
    press_sku_stats: dict = defaultdict(lambda: {"cycles": 0, "units": 0, "mins_used": 0.0})
    daily_cured:     dict[str, int] = defaultdict(int)
    sku_cured:       dict[str, int] = defaultdict(int)
    build_by_shift_sku: dict = {}      # {(date,shift): {sku: qty}} for GT diagnostic
    last_build_day:  dict[str, int] = {}
    daily_summary:   list[dict] = []
    writeoff_total   = 0.0
    SHIFTS           = ["A", "B", "C"]

    # ── Dynamic CO infrastructure ─────────────────────────────────────────────
    # Tracks instant COs triggered when a press finishes its SKU demand mid-plan.
    # CO starts in the SAME SHIFT demand is met (remaining time = CHANGEOVER).
    # Next shift: PRODUCTION for new SKU (no MOULD_CLEAN).
    # Key = press, value = (global_shift_idx_of_co_start, new_sku)
    # global_shift_idx = (day − 1) × 3 + shift_idx  (A=0, B=1, C=2)
    dynamic_co_tracker: dict[str, tuple[int, str]] = {}

    # Track daily CO counts (pre-planned + dynamic combined) to enforce the cap.
    daily_co_count: dict[int, int] = defaultdict(int)
    for _dco_day, _dco_evs in co_by_day.items():
        daily_co_count[_dco_day] += len(_dco_evs)

    # sku_campaign_tier: SKU -> best (min) campaign-list position it received
    # this most recent day (0=primary, 1=secondary via first CO, 2+=tertiary+).
    # Replaced (not merged) each day — see Step 3 / end-of-day finalize below.
    # Only accumulated when _DYNAMIC_CO_PLANNER_ENABLED (zero cost otherwise).
    sku_campaign_tier: dict[str, int] = {}

    if os.environ.get("ROLLCO_STAGE2_CHECK"):
        _stage2_scheduler = COScheduler()
        _stage2_allow = cetl.load_curing_allowable()
        _rolling_today = _rolling_horizon_co_call(
            day=1, planning_days=planning_days,
            press_state=press_state, press_count=press_count,
            demand_remaining=demand_remaining, demand_dict=demand_dict,
            priority_score_map=priority_score_map,
            df_allowable=_stage2_allow, ct_map=cc_result["ct_map"],
            dynamic_co_tracker=dynamic_co_tracker,
            scheduler=_stage2_scheduler, max_co_per_day=MAX_CHANGEOVERS_PER_DAY,
        )
        _legacy_today = sorted(
            (ev["press"], ev["old_sku"], ev["new_sku"])
            for ev in co_events if int(ev["day"]) == 1
        )
        print(f"\n  [Stage2Check] Rolling Day-1: {len(_rolling_today)} events")
        print(f"  [Stage2Check] Legacy  Day-1: {len(_legacy_today)} events")
        _rolling_set = set(_rolling_today)
        _legacy_set  = set(_legacy_today)
        print(f"  [Stage2Check] Match: {_rolling_set == _legacy_set}")
        print(f"  [Stage2Check] Rolling-only: {sorted(_rolling_set - _legacy_set)}")
        print(f"  [Stage2Check] Legacy-only : {sorted(_legacy_set - _rolling_set)}")
        import sys; sys.exit(0)

    # Phase 2b — press → allowable demand-SKUs (sorted), the retarget candidate
    # universe. Built once, only when the optimisation is on (a DB read otherwise
    # skipped). Restricted to demand SKUs so retarget never chases a zero-demand SKU.
    press_allow_skus: dict[str, list] = {}
    if _mould_gate and (_mould_opt or _CO_SCORER_ENABLED):
        try:
            _dfca = cetl.load_curing_allowable()
            _dem_skus = set(demand_dict.keys())
            _tmp: dict[str, set] = {}
            for _, _row in _dfca.iterrows():
                _sk = str(_row["SKUCode"]).strip()
                if _sk in _dem_skus:
                    for _mp in (_row.get("Machines", []) or []):
                        _tmp.setdefault(str(_mp), set()).add(_sk)
            press_allow_skus = {_p: sorted(_ss) for _p, _ss in _tmp.items()}
        except Exception as _e:
            print(f"  [Rolling] curing-allowable load for retarget FAILED ({_e}); "
                  f"retarget disabled")
            press_allow_skus = {}

    # ── Phase 3: Unified CO scorer ────────────────────────────────────────────
    # counters (provenance of every committed CO + why some were blocked)
    co_scorer_stats = {"planned": 0, "pullfwd": 0, "dynamic": 0, "retarget": 0,
                       "idle": 0, "cancelled": 0, "build_blocked": 0}
    _CO_COST_UNITS   = float(os.environ.get("CO_COST_UNITS", "0"))  # 0 = cost folded into shift-draw floor
    _DEFAULT_BLD_CT  = 120.0

    def _bld_free_min_shift():
        """Conservative per-machine spare building minutes THIS shift: SHIFT_MINS minus
        the minutes each GT-producing machine already owes its currently-RUNNING SKUs
        (each RUNNING SKU's per-shift draw spread evenly over its eligible machines).
        Returns {machine: free_min}. Only clearly-free minutes count (never negative)."""
        _committed: dict[str, float] = defaultdict(float)
        for _pr, _st in press_state.items():
            if _st.get("status") != "RUNNING":
                continue
            _s = _st["sku"]
            if demand_remaining.get(_s, 0.0) <= 0:
                continue
            _ms = sku_machine_map.get(_s)
            if not _ms:
                continue
            _draw = _cure_qty_per_shift(cure_ct_map.get(_s, DEFAULT_CURING_CT))
            _per  = _draw / len(_ms)
            for _m in _ms:
                _committed[_m] += _per * (_BLD_CT_SEC.get(str(_m), _DEFAULT_BLD_CT) / 60.0)
        return {str(_m): max(0.0, float(SHIFT_MINS) - _committed.get(str(_m), 0.0))
                for _m in machine_skus}

    def _bld_capacity(sku: str, bld_free: dict) -> float:
        """Units/shift of `sku` GT that the currently-spare building machines could add."""
        _ms = sku_machine_map.get(sku)
        if not _ms:
            return 0.0
        return sum(bld_free.get(str(_m), 0.0) / (_BLD_CT_SEC.get(str(_m), _DEFAULT_BLD_CT) / 60.0)
                   for _m in _ms)

    def _bld_commit(sku: str, units: float, bld_free: dict) -> None:
        """Live-decrement the shared building minutes when a CO to `sku` is committed."""
        _ms = sku_machine_map.get(sku)
        if not _ms:
            return
        _per = units / len(_ms)
        for _m in _ms:
            _ms_key = str(_m)
            bld_free[_ms_key] = max(0.0, bld_free.get(_ms_key, 0.0)
                                    - _per * (_BLD_CT_SEC.get(_ms_key, _DEFAULT_BLD_CT) / 60.0))

    def _co_utility(press: str, target: str, horizon_left: int, bld_cap: float) -> float:
        """One utility for 'press → target', in units. Higher = more worth a changeover.
        expected extra cured = min(per-press residual demand load, what the press can
        physically cure of target over the horizon, what building can actually feed),
        minus the changeover cost (one shift of the target's own production)."""
        _rem = demand_remaining.get(target, 0.0)
        if _rem <= 0:
            return -1.0
        _npr  = max(1, press_count.get(target, 0))
        _draw = _cure_qty_per_shift(cure_ct_map.get(target, DEFAULT_CURING_CT))
        _residual_load = _rem / _npr
        _horizon_cure  = _draw * 3 * max(0, horizon_left)     # this press's cure over horizon
        _extra = min(_residual_load, _horizon_cure, bld_cap)
        _co_cost = _draw + _CO_COST_UNITS                     # 1 lost shift + optional constant
        return _extra - _co_cost

    def _best_alt(press, bld_free, horizon_left, exclude=None, check_build=True):
        """Highest-utility allowable target for `press` with 2 free moulds (and, if
        check_build, enough building feed). Deterministic. Returns SKU or None."""
        _cur = press_state.get(press, {}).get("sku")
        _best = None
        _best_key = None
        for s in press_allow_skus.get(press, ()):          # pre-sorted
            if s == _cur or s == exclude:
                continue
            if demand_remaining.get(s, 0.0) <= 0:
                continue
            if _n_free_for(s, press) < 2:
                continue
            _cap = _bld_capacity(s, bld_free)
            _draw = _cure_qty_per_shift(cure_ct_map.get(s, DEFAULT_CURING_CT))
            if check_build and _cap < _draw:
                continue
            _u = _co_utility(press, s, horizon_left, _cap)
            if _u <= 0:                                     # only worthwhile changeovers
                continue
            _key = (_u, s)                                  # util desc, SKU tiebreak
            if _best_key is None or _key > _best_key:
                _best_key = _key
                _best = s
        return _best

    def _solve_day_cos(day, today_planned):
        """Global CO solve for one day. Returns the final list of committed COs
        [(press, old_sku, new_sku)], reserving moulds via _try_mount(defer_free) and
        pruning any pulled-forward COs out of tomorrow's co_by_day. Two modes:
        ADDITIVE (keep planned COs, add pull-forward + idle-fill) and FULL_REOPT
        (global utility scoring; planned COs may be cancelled/replaced)."""
        horizon_left     = planning_days - day + 1
        bld_free         = _bld_free_min_shift()
        tomorrow_planned = list(co_by_day.get(day + 1, []))
        planned_tom      = {p: ns for (p, _o, ns) in tomorrow_planned}
        committed: dict[str, tuple] = {}     # press -> (target, provenance)
        pulled: list[str] = []
        slots = [MAX_CHANGEOVERS_PER_DAY]

        def _commit(press, target, prov, check_build=True):
            if slots[0] <= 0 or press in committed:
                return False
            if _n_free_for(target, press) < 2:
                return False
            _draw = _cure_qty_per_shift(cure_ct_map.get(target, DEFAULT_CURING_CT))
            if check_build and _bld_capacity(target, bld_free) < _draw:
                co_scorer_stats["build_blocked"] += 1
                return False
            if not _try_mount(press, target, defer_free=True):
                return False
            _bld_commit(target, _draw, bld_free)
            committed[press] = (target, prov)
            slots[0] -= 1
            co_scorer_stats[prov] += 1
            return True

        if not _SCORER_FULL_REOPT:
            # ADDITIVE — planned COs kept (mould-gate + retarget-on-block, no build veto
            # so this path is a superset of Phase-2), then NEW pull-forward + idle-fill.
            for (press, _o, ns) in today_planned:
                if _commit(press, ns, "planned", check_build=False):
                    continue
                # retarget-on-block — EXACT Phase-2 (_pick_retarget, no build veto) so
                # the planned+retarget layer matches the locked mould baseline bit-for-bit.
                _alt = _pick_retarget(press)
                if not (_alt is not None and _commit(press, _alt, "retarget", check_build=False)):
                    co_scorer_stats["cancelled"] += 1
            for (press, _o, ns) in tomorrow_planned:
                if press in committed:
                    continue
                if demand_remaining.get(press_state.get(press, {}).get("sku"), 1.0) > 0:
                    continue                                  # press still busy today
                if _commit(press, ns, "pullfwd"):
                    pulled.append(press)
            for press in sorted(press_state):
                if press in committed:
                    continue
                if demand_remaining.get(press_state[press]["sku"], 1.0) > 0:
                    continue                                  # not idle — leave it running
                _alt = _best_alt(press, bld_free, horizon_left)
                if _alt is not None:
                    _commit(press, _alt, "dynamic")
        else:
            # FULL RE-OPT — every eligible press competes; planned COs are candidates.
            planned_today = {p: ns for (p, _o, ns) in today_planned}
            elig = set(planned_today) | set(planned_tom)
            for press, st in press_state.items():
                if demand_remaining.get(st["sku"], 1.0) <= 0:
                    elig.add(press)
            pairs = []
            for press in sorted(elig):
                _cur = press_state.get(press, {}).get("sku")
                _cands: dict[str, str] = {}
                if press in planned_today:
                    _cands[planned_today[press]] = "planned"
                if press in planned_tom:
                    _cands.setdefault(planned_tom[press], "pullfwd")
                for s in press_allow_skus.get(press, ()):
                    if demand_remaining.get(s, 0.0) > 0:
                        _cands.setdefault(s, "retarget")
                for t, prov in _cands.items():
                    if t == _cur:
                        continue
                    _u = _co_utility(press, t, horizon_left, _bld_capacity(t, bld_free))
                    pairs.append((_u, press, t, prov))
            pairs.sort(key=lambda x: (-x[0], x[1], x[2]))
            for _u, press, t, prov in pairs:
                if _u <= 0:
                    break
                if press in committed:
                    continue
                if _commit(press, t, prov) and prov == "pullfwd":
                    pulled.append(press)

        if pulled:
            _pset = set(pulled)
            co_by_day[day + 1] = [(p, o, n) for (p, o, n) in tomorrow_planned
                                  if p not in _pset]
            daily_co_count[day + 1] = max(0, daily_co_count.get(day + 1, 0) - len(_pset))

        co_scorer_stats["idle"] += sum(
            1 for pr, st in press_state.items()
            if pr not in committed and demand_remaining.get(st["sku"], 1.0) <= 0)

        final = [(pr, press_state.get(pr, {}).get("sku"), tgt)
                 for pr, (tgt, _prov) in committed.items()]
        daily_co_count[day] = len(final)
        return final

    print("\n" + "=" * 70)
    print("  ROLLING PIPELINE — Day-by-day simulation")
    print("=" * 70)

    for day in range(1, planning_days + 1):
        _cur_day[0] = day                          # for the mould-movement log in _try_mount
        date     = plan_start + timedelta(days=day - 1)
        date_str = date.strftime("%Y-%m-%d")
        if _ROLLING_HORIZON_CO_ENABLED:
            today_cos = _rolling_horizon_co_call(
                day=day, planning_days=planning_days,
                press_state=press_state, press_count=press_count,
                demand_remaining=demand_remaining, demand_dict=demand_dict,
                priority_score_map=priority_score_map,
                df_allowable=_df_curing_allow_static,
                ct_map=cc_result["ct_map"],
                dynamic_co_tracker=dynamic_co_tracker,
                scheduler=_rolling_co_scheduler,
                max_co_per_day=MAX_CHANGEOVERS_PER_DAY,
            )
            daily_co_count[day] += len(today_cos)
        elif _DYNAMIC_CO_PLANNER_ENABLED:
            today_cos = _plan_day_cos(
                day, press_state, demand_remaining, press_count,
                cure_ct_map, priority_score_map, demand_dict,
                press_to_demand_targets, press_total_demand,
                sku_campaign_tier, ri_skus, nri_skus,
                daily_co_count, planning_days,
            )
            daily_co_count[day] += len(today_cos)
            if os.environ.get("DYNCO_DEBUG"):
                print(f"    [DynCO-debug] Day {day}: {len(today_cos)} events "
                      f"{today_cos if today_cos else ''}")
            # Reset for today's shifts to populate fresh — sku_campaign_tier
            # must reflect the day just simulated, not accumulate forever.
            sku_campaign_tier = {}
        else:
            today_cos = co_by_day.get(day, [])
        # ── Mould gate (planned COs) ──────────────────────────────────────────
        # A planned CO can only happen if 2 eligible moulds are free for the new
        # SKU. Gate HERE (day-start) — not at apply-time — because co_press_map
        # drives the curing sim THIS day; a CO blocked later would already have
        # been cured. Feasible COs get their moulds committed now (_try_mount);
        # blocked ones are dropped so the press keeps its old SKU all day.
        if _mould_gate and _CO_SCORER_ENABLED:
            # Phase 3 — unified global CO solve (planned / pull-forward / retarget /
            # dynamic / idle under one utility + mould + building-feed gate). Runs even
            # when today_cos is empty (there may be idle presses to fill / pull-forwards).
            today_cos = _solve_day_cos(day, today_cos)
        elif _mould_gate and today_cos:
            # Phase 2a — scarce-first: claim moulds for the SCARCEST new-SKU first
            # (fewest eligible moulds) so a 2-mould SKU is not blocked by a 6-mould
            # SKU grabbing a shared mould first. Pure reordering — the SET of COs
            # attempted is unchanged, so this can only REDUCE blocks, never add them.
            # Deterministic tiebreak on (press, new_sku). MOULD_OPT=0 keeps input order.
            _co_order = today_cos
            if _mould_opt:
                _co_order = sorted(
                    today_cos,
                    key=lambda t: (len(_sku_moulds.get(t[2], set())), str(t[0]), str(t[2])),
                )
            _kept = []
            for _p, _os, _ns in _co_order:
                if _try_mount(_p, _ns, defer_free=True):
                    _kept.append((_p, _os, _ns))
                else:
                    # Phase 2b — retarget-on-block: the scheduled new-SKU has no free
                    # moulds; rather than idle the press on its old (usually demand-done)
                    # SKU, redirect the CO to the neediest allowable SKU that still has
                    # 2 free moulds. Recovers a wasted CO slot; demand cap still clips.
                    _alt = _pick_retarget(_p) if _mould_opt else None
                    if _alt is not None and _try_mount(_p, _alt, defer_free=True):
                        _kept.append((_p, _os, _alt))
                        mould_retargeted_cos += 1
                    else:
                        mould_blocked_cos += 1
            today_cos = _kept
        co_press_map: dict[str, str] = {p: ns for p, _, ns in today_cos}
        # SKUs that curing presses are switching TO today — building must pre-build for these
        co_target_skus_today: frozenset = frozenset(co_press_map.values())

        # ── Which SHIFT does each planned CO fire in? ──────────────────────────
        # Plant rule: a press goes to changeover as soon as it FINISHES its current
        # SKU (cap permitting) — not at a fixed 07:00. So place each CO in the shift
        # where its old SKU is projected to run out:
        #   already finished        → Shift A (index 0) — never make a free press wait
        #   finishes in n shifts    → that shift (clamped to C)
        #   will not finish today   → Shift A (preemptive Class-A CO — the static
        #                             scheduler booked it for this day deliberately)
        # OFF (_CO_SHIFT_SPREAD_ENABLED=False) ⇒ every CO in Shift A (old behaviour).
        co_shift_idx: dict[str, int] = {}
        for _p, _old, _new in today_cos:
            _idx = 0
            if _CO_SHIFT_SPREAD_ENABLED:
                _rem = demand_remaining.get(_old, 0.0)
                if _rem > 0:
                    _octt  = cure_ct_map.get(_old, DEFAULT_CURING_CT)
                    _odraw = _cure_qty_per_shift(_octt) * max(1, press_count.get(_old, 1))
                    _n     = math.ceil(_rem / _odraw) if _odraw > 0 else 99
                    _idx   = _n if _n <= 2 else 0      # >2 shifts ⇒ won't finish ⇒ preempt in A
            co_shift_idx[_p] = _idx

        # Per-shift simulation: build → cure for each shift independently.
        # Building assignment runs once per shift (not once per day) so each
        # shift's build plan reacts to the actual GT inventory and curing demand
        # for that specific shift — matching the plant's actual scheduling approach.
        day_built:    dict[str, float] = defaultdict(float)  # all machines (GT + carcass)
        day_gt_built: dict[str, float] = defaultdict(float)  # GT machines only (no Stage-1)
        day_cured_d:  dict[str, float] = defaultdict(float)

        for shift in SHIFTS:
            key = (date_str, shift)
            shift_bld: dict[str, int] = defaultdict(int)
            build_by_shift_sku[key] = shift_bld

            # Global shift index: unique monotonic ID used by dynamic_co_tracker.
            # (day−1)×3 + {A=0, B=1, C=2}
            cur_shift_global = (day - 1) * 3 + SHIFTS.index(shift)

            # ── 0. Apply dynamic CO transitions ──────────────────────────────
            # When demand was met in shift X (CO started immediately), the press
            # starts RUNNING for the new SKU from shift X+1 onwards.
            for _press, (_co_idx, _new_sku) in list(dynamic_co_tracker.items()):
                if cur_shift_global == _co_idx + 1:
                    # CO used remaining time of shift _co_idx; press now produces.
                    press_count[_new_sku] = press_count.get(_new_sku, 0) + 1
                    press_state[_press]   = {"sku": _new_sku, "status": "RUNNING"}
                    curing_allowable[_new_sku].append(_press)
                    del dynamic_co_tracker[_press]

            # ── 1. Per-shift curing demand (which SKUs need GT this shift) ──
            shift_cure_demand: dict[str, float] = defaultdict(float)
            for press, st in press_state.items():
                if press in co_press_map:
                    # Before its CO shift the press still draws its OLD SKU; after it,
                    # the NEW SKU. On the CO shift itself it is idle (no draw).
                    _cs = co_shift_idx.get(press, 0)
                    _si = SHIFTS.index(shift)
                    if _si < _cs:
                        _osku = st["sku"]
                        _oct  = cure_ct_map.get(_osku, DEFAULT_CURING_CT)
                        shift_cure_demand[_osku] += _cure_qty_per_shift(_oct)
                    elif _si > _cs:
                        new_sku = co_press_map[press]
                        new_ct  = cure_ct_map.get(new_sku, DEFAULT_CURING_CT)
                        shift_cure_demand[new_sku] += _cure_qty_per_shift(new_ct)
                elif press in dynamic_co_tracker:
                    # Dynamic CO: press already in CHANGEOVER this shift or started RUNNING
                    # (RUNNING case handled by press_state update at top of shift).
                    # No explicit demand signal needed — if RUNNING, press_state reflects it.
                    pass
                elif st["status"] == "RUNNING":
                    sku = st["sku"]
                    ct  = cure_ct_map.get(sku, DEFAULT_CURING_CT)
                    shift_cure_demand[sku] += _cure_qty_per_shift(ct)

            # Pre-build signal: in Shift A only, inject anticipated demand for all CO target
            # SKUs so building starts pre-building GT before their presses fire in Shift B.
            # Bug fix: original code injected 1× qty_per_shift per UNIQUE target SKU,
            # regardless of how many presses are CO'ing to it.  For SURL0 (3 CO presses),
            # this produced d = 1×56×2 = 112 — far too low to compete with a 10-press RI
            # SKU (d=1120).  Building never served SURL0 in Campaign 2.
            # Fix: count actual CO presses per target SKU and inject n_presses × qty,
            # matching how RUNNING presses accumulate their demand signal.
            # Injected on each press's OWN CO shift (not blanket Shift A), so building
            # still starts the new SKU's GT simultaneously with that press's changeover
            # — the simultaneity rule — now that COs are spread across A/B/C.
            _co_press_counts: dict[str, int] = defaultdict(int)
            for _cp, _cp_sku in co_press_map.items():
                if SHIFTS.index(shift) == co_shift_idx.get(_cp, 0):
                    _co_press_counts[_cp_sku] += 1
            for new_sku, n_co in _co_press_counts.items():
                new_ct = cure_ct_map.get(new_sku, DEFAULT_CURING_CT)
                shift_cure_demand[new_sku] += _cure_qty_per_shift(new_ct) * n_co

            # ── 2. Building assignment for this shift ──────────────────────
            shift_plan = _assign_building_shift(
                shift_cure_demand=dict(shift_cure_demand),
                machine_skus=machine_skus,
                machine_current_sku=machine_current_sku,
                sku_inch=sku_inch,
                demand_remaining=demand_remaining,
                gt_inventory=gt_inventory,
                machine_pool=machine_pool,
                machine_minutes_on_sku=machine_minutes_on_sku,
                cure_ct_map=cure_ct_map,
                press_count=press_count,
                co_target_skus=co_target_skus_today,
                days_left=planning_days - day + 1,
                demand_dict=demand_dict,
                machine_total_demand=machine_total_demand,
                machine_anchor_inch=machine_anchor_inch,
                machine_used_inches=machine_used_inches,
            )

            # ── 3. Add GT to inventory; record building rows + CO events ───
            # StartTime/EndTime: per-machine wall-clock cursor within the shift.
            # It starts at the shift's clock start and advances by each event's
            # own duration (CO minutes, then production minutes = qty × CT), so a
            # downstream scheduler reads an exact, non-overlapping timeline of
            # what each machine does and when. CT-per-unit = _BLD_CT_SEC[machine].
            _shift_start = _shift_start_dt(date_str, shift)
            for machine, campaigns in shift_plan.items():
                prev_sku  = machine_current_sku.get(machine, "")
                prev_inch = sku_inch.get(prev_sku, "")
                _cursor   = _shift_start
                _ct_sec   = _BLD_CT_SEC.get(str(machine), 120.0)
                for _tier_idx, (sku, qty, co_type) in enumerate(campaigns):
                    if co_type != "start":
                        co_mins = _co_cost(machine, prev_inch, sku_inch.get(sku, ""))
                        _co_start = _cursor
                        _cursor   = _cursor + timedelta(minutes=co_mins)
                        bld_shift_rows.append({
                            "Machine":       machine,
                            "Date":          date_str,
                            "Shift":         shift,
                            "SKUCode":       "CHANGEOVER",
                            # A changeover produces NO tyres: Qty must be 0 so the
                            # column means one thing everywhere and stays summable.
                            # The CO duration lives in its own CO_Mins column (and is
                            # already implied by StartTime→EndTime).
                            "Qty":           0,
                            "CO_Mins":       co_mins,
                            "StartTime":     _fmt_dt(_co_start),
                            "EndTime":       _fmt_dt(_cursor),
                            "Machine_Group": _group_label(machine),
                            "CO_Type":       co_type,
                        })
                        bld_co_events.append({
                            "Machine":      machine,
                            "Date":         date_str,
                            "Shift":        shift,
                            "Day":          day,
                            "CO_Day_Index": day,
                            "From_SKU":     prev_sku,
                            "Target_SKU":   sku,
                            "CO_Type":      co_type,
                            "CO_Cost_Mins": co_mins,
                            "Status":       f"Rolling CO ({co_type})",
                        })
                    _prod_start = _cursor
                    _cursor     = _cursor + timedelta(minutes=qty * _ct_sec / 60.0)
                    bld_shift_rows.append({
                        "Machine":       machine,
                        "Date":          date_str,
                        "Shift":         shift,
                        "SKUCode":       sku,
                        "Qty":           qty,
                        "CO_Mins":       0,
                        "StartTime":     _fmt_dt(_prod_start),
                        "EndTime":       _fmt_dt(_cursor),
                        "Machine_Group": _group_label(machine),
                        "CO_Type":       "production",
                    })
                    # Stage-1 produces carcass (not GT) → carcass feeds Stage-2,
                    # NOT curing presses.  Do NOT add to gt_inventory for Stage-1
                    # machines; curing must only draw real GT (Stage-2 / Unistage / VMI / BJ).
                    if machine not in _S1_MACHINES:
                        gt_inventory[sku]   = gt_inventory.get(sku, 0.0) + qty
                        day_gt_built[sku]  += qty
                        if _DYNAMIC_CO_PLANNER_ENABLED:
                            _prev_tier = sku_campaign_tier.get(sku)
                            if _prev_tier is None or _tier_idx < _prev_tier:
                                sku_campaign_tier[sku] = _tier_idx
                    day_built[sku]    += qty
                    shift_bld[sku]    += qty
                    if qty > 0:
                        last_build_day[sku] = day
                    prev_sku = sku; prev_inch = sku_inch.get(sku, "")
                if campaigns:
                    # Update current SKU at end of THIS SHIFT (not end of day)
                    machine_current_sku[machine] = campaigns[-1][0]

                    # ── Client inch rules: advance the machine's anchor + inch history.
                    # The FIRST inch a machine ever runs fixes its +/-2 band for the
                    # month; every inch it runs is recorded so it can never be re-used
                    # after the machine leaves it (Rule 1a).
                    if _INCH_RULES_ENABLED:
                        _used = machine_used_inches.setdefault(machine, set())
                        for _c_sku, _c_qty, _c_type in campaigns:
                            _ci = sku_inch.get(_c_sku, "")
                            if not _ci:
                                continue
                            if machine not in machine_anchor_inch:
                                machine_anchor_inch[machine] = _ci
                            _used.add(_ci)

                    # Update cross-shift minute tracker.
                    # Accumulate production minutes after the LAST CO in this shift.
                    # If no CO occurred: add all production minutes to existing total.
                    _had_co = False
                    _mins_after_last_co = 0.0
                    for _, _q, _ct in campaigns:
                        _ct_sec = _BLD_CT_SEC.get(machine, 150.0)
                        _prod_m = _q * _ct_sec / 60.0
                        if _ct != "start":           # CO event → reset counter
                            _had_co = True
                            _mins_after_last_co = _prod_m
                        else:
                            _mins_after_last_co += _prod_m
                    if _had_co:
                        machine_minutes_on_sku[machine] = _mins_after_last_co
                    else:
                        machine_minutes_on_sku[machine] = (
                            machine_minutes_on_sku.get(machine, 0.0) + _mins_after_last_co
                        )

            # ── 3b. Stage-1 carcass scheduling (utilization tracking only) ──
            # For every SKU that Stage-2 machines built this shift, allocate the
            # matching carcass qty (1:1 with GT) across that SKU's eligible Stage-1
            # machines, capped by each machine's per-shift capacity. Recorded in
            # bld_shift_rows (CO_Type="carcass") so Machine Utilization / Daily GT
            # & Carcass show real numbers instead of 0%. Deliberately does NOT
            # touch gt_inventory, demand_remaining, or machine_skus — Stage-1 is
            # assumed to always have spare capacity to supply Stage-2 (validated:
            # Stage-1 util stays well under 100% even at full Stage-2 output), so
            # this can never gate or double-count real GT output.
            stage2_built_this_shift: dict[str, float] = defaultdict(float)
            for machine, campaigns in shift_plan.items():
                if _MACHINE_GROUP.get(machine, "") != "STAGE2":
                    continue
                for sku, qty, _co_type in campaigns:
                    stage2_built_this_shift[sku] += qty
            # One machine = one SKU per shift (same physical constraint as every
            # other building machine — see CLAUDE.md "One building machine always
            # produces for exactly one SKU at a time"). Once a Stage-1 machine is
            # allocated to a SKU this shift it's removed from the pool even if it
            # has leftover capacity, instead of splitting it across SKUs with no
            # changeover between them.
            s1_machines_used_this_shift: set = set()

            def _s1_inch_ok(_m: str, _sku: str) -> bool:
                """Client inch rules applied to Stage-1 carcass machines too.

                Same anchor +/- band and never-revisit rules as the GT machines;
                Stage-1 keeps its own current-inch tracker because it is scheduled
                here (Step 3b, derived from Stage-2 output), not in
                _assign_building_shift.
                """
                if not _INCH_RULES_ENABLED:
                    return True
                return _inch_ok(sku_inch.get(_sku, ""),
                                s1_current_inch.get(_m, ""),
                                machine_anchor_inch.get(_m, ""),
                                machine_used_inches.get(_m, set()))

            for sku, need in sorted(stage2_built_this_shift.items(), key=lambda kv: (-kv[1], kv[0])):
                if need <= 0:
                    continue
                eligible = sorted(
                    (m for m in s1_sku_to_machines.get(sku, ())
                     if m not in s1_machines_used_this_shift and _s1_inch_ok(m, sku)),
                    key=lambda m: (-_bld_qty_per_shift(m), m),
                )
                remaining_need = need
                for m in eligible:
                    if remaining_need <= 0:
                        break
                    cap = _bld_qty_per_shift(m)
                    if cap <= 0:
                        continue
                    alloc = min(cap, remaining_need)
                    s1_machines_used_this_shift.add(m)
                    remaining_need -= alloc
                    # Advance the Stage-1 machine's inch history (first inch fixes
                    # its band; every inch run is closed once it moves on).
                    if _INCH_RULES_ENABLED:
                        _s1_i = sku_inch.get(sku, "")
                        if _s1_i:
                            if m not in machine_anchor_inch:
                                machine_anchor_inch[m] = _s1_i
                            machine_used_inches.setdefault(m, set()).add(_s1_i)
                            s1_current_inch[m] = _s1_i
                    # One SKU per Stage-1 machine per shift → carcass run starts
                    # at the shift clock start; duration = alloc × CT.
                    _c_start = _shift_start
                    _c_end   = _c_start + timedelta(
                        minutes=round(alloc) * _BLD_CT_SEC.get(str(m), 120.0) / 60.0
                    )
                    bld_shift_rows.append({
                        "Machine":       m,
                        "Date":          date_str,
                        "Shift":         shift,
                        "SKUCode":       sku,
                        "Qty":           round(alloc),
                        "CO_Mins":       0,
                        "StartTime":     _fmt_dt(_c_start),
                        "EndTime":       _fmt_dt(_c_end),
                        "Machine_Group": _group_label(m),
                        "CO_Type":       "carcass",
                    })

            # ── 4. Curing simulation ───────────────────────────────────────
            for press in sorted(press_state):
                st  = press_state[press]
                sku = st["sku"]

                if press in co_press_map:
                    # Shifts BEFORE the CO → press still runs its OLD SKU.
                    # The CO shift        → CHANGEOVER (full shift idle).
                    # Shifts AFTER        → the NEW SKU runs.
                    _cs = co_shift_idx.get(press, 0)
                    _si = SHIFTS.index(shift)
                    if _si < _cs:
                        status = st["status"]       # old SKU keeps producing until its CO
                    elif _si == _cs:
                        status = "CHANGEOVER"       # CO shift: full shift idle
                    else:
                        sku    = co_press_map[press]
                        status = "RUNNING"
                elif press in dynamic_co_tracker:
                    co_idx, _ = dynamic_co_tracker[press]
                    if cur_shift_global == co_idx:
                        status = "CHANGEOVER"      # CO started this shift (demand just met)
                    else:
                        status = st["status"]      # shouldn't occur; fallback
                else:
                    status = st["status"]

                ct       = cure_ct_map.get(sku, DEFAULT_CURING_CT)
                cap      = _cure_qty_per_shift(ct)
                gt_avail = max(0.0, gt_inventory.get(sku, 0.0))

                # ── Mould-clean carry-in: a clean that began mid-shift last shift
                # occupies the front of THIS shift. If it fills the whole shift the
                # press is in MOULD_CLEAN (no production); otherwise production runs
                # in the reduced remaining minutes _avail.
                _avail    = float(SHIFT_MINS)
                _busy_in  = 0.0    # CO/clean minutes consumed at the FRONT of this shift
                # Per-shift CO / mould-clean minutes — surfaced as CO_Mins /
                # Mould_Clean_Mins columns so every changeover (planned full-shift,
                # dynamic mid-shift trigger, and its overhang) is VISIBLE in the sheet,
                # not just in the Changeover Plan.
                _co_mins_shift    = 0.0
                _clean_mins_shift = 0.0
                _dyn_co_tgt       = None
                # Separate time-portions so production and CO/clean become SEPARATE
                # rows in the sheet (each with its own real wall-clock window):
                _seg_co_in    = 0.0   # CO overhang carried into the FRONT of this shift
                _seg_clean_in = 0.0   # mould-clean overhang carried into the front
                _seg_co_trig  = 0.0   # CO fired mid-shift AFTER production (this shift's part)
                _seg_clean_trig = 0.0 # mould clean fired mid-shift after production
                # (a) Changeover overhang: a dynamic CO that fired mid-shift last shift
                #     is still running. It blocks the front of this shift, so the new
                #     SKU's production starts mid-shift.
                if status == "RUNNING" and co_carry.get(press, 0.0) > 0:
                    _coin = min(co_carry[press], _avail)
                    co_carry[press] -= _coin
                    _avail  -= _coin
                    _busy_in += _coin
                    if _avail <= 0:
                        status = "CHANGEOVER"           # else-branch books SHIFT_MINS co
                    else:
                        press_stats[press]["co_mins"] += _coin
                        _co_mins_shift += _coin
                        _seg_co_in = _coin
                # (b) Mould-clean overhang (same pattern).
                if (_MOULD_CLEAN_ENABLED and status == "RUNNING"
                        and clean_carry.get(press, 0.0) > 0):
                    _cin = min(clean_carry[press], _avail)
                    clean_carry[press] -= _cin
                    _avail  -= _cin
                    _busy_in += _cin
                    if _avail <= 0:
                        status = "MOULD_CLEAN"          # else-branch books SHIFT_MINS clean
                    else:
                        press_stats[press]["clean_mins"] += _cin   # partial carry booked here
                        _clean_mins_shift += _cin
                        _seg_clean_in = _cin

                _cleaned  = False
                prod_mins = 0.0
                if status == "RUNNING":
                    # DEBUG self-check: a RUNNING press must own 2 moulds eligible
                    # for its SKU. Counts leaks where the gate failed to block.
                    if _mould_gate and os.environ.get("MOULD_DEBUG"):
                        _ow = [m for m in _press_moulds.get(press, set())
                               if sku in _mould_skus.get(m, set())]
                        if len(_ow) < 2:
                            _mould_selfcheck[0] += 1
                            _mould_selfcheck.append((day, shift, press, sku, len(_ow),
                                                     len(_sku_moulds.get(sku, set()))))
                        # targeted: dump the actual owned-mould state for SRBT0 presses
                        if (os.environ.get("MOULD_DUMP") and day == 22 and shift == "C"
                                and sku == "1325119015008SRBT0"):
                            print(f"    [DUMP] press {press} sku {sku} owns "
                                  f"{sorted(_press_moulds.get(press, set()))} | "
                                  f"eligible-of-those={sorted(_ow)} | "
                                  f"sku_moulds[SRBT0]={len(_sku_moulds.get(sku,set()))}")
                    # Cap curing at remaining demand — never over-produce.
                    demand_left = max(0.0, demand_remaining.get(sku, 0.0))
                    if _MOULD_CLEAN_ENABLED:
                        cap_time = int(_avail / ct) * CURING_CAVITIES   # partial-shift cap
                        cap_life = mould_life[press] * CURING_CAVITIES  # cycles left × cavities
                        cured = min(cap_time, int(gt_avail), int(demand_left), cap_life)
                    else:
                        cured = min(cap, int(gt_avail), int(demand_left))
                    gt_inventory[sku]      = gt_avail - cured
                    day_cured_d[sku]      += cured
                    sku_cured[sku]        += cured
                    daily_cured[date_str] += cured
                    demand_remaining[sku]  = max(0.0, demand_remaining.get(sku, 0.0) - cured)
                    prod_mins = cured * ct / CURING_CAVITIES
                    press_stats[press]["running_mins"] += prod_mins
                    press_stats[press]["skus"].add(sku)
                    press_stats[press]["cycles"] += cured // CURING_CAVITIES
                    press_stats[press]["units"]  += cured
                    press_sku_stats[(press, sku)]["cycles"]    += cured // CURING_CAVITIES
                    press_sku_stats[(press, sku)]["units"]     += cured
                    press_sku_stats[(press, sku)]["mins_used"] += prod_mins

                    # ── Mould-clean: decrement life; fire an immediate clean when it
                    # hits 0 (cured was capped by cap_life). The 8h clean fills the
                    # rest of THIS shift; the remainder bleeds into the next shift.
                    if _MOULD_CLEAN_ENABLED:
                        mould_life[press] -= cured // CURING_CAVITIES
                        if mould_life[press] <= 0:
                            clean_here = max(0.0, min(float(MOULD_CLEAN_MINS),
                                                      _avail - prod_mins))
                            press_stats[press]["clean_mins"] += clean_here
                            _clean_mins_shift += clean_here
                            _seg_clean_trig = clean_here
                            clean_carry[press] = MOULD_CLEAN_MINS - clean_here
                            mould_life[press]  = MOULD_CLEAN_CYCLES
                            _cleaned = True

                    # ── Instant CO when demand is met ─────────────────────────
                    # Demand just hit 0: CO starts immediately (remaining shift
                    # time = CHANGEOVER).  Next shift = PRODUCTION for new SKU.
                    # Conditions: no pre-planned CO today, not already in a
                    # dynamic CO, and no pre-planned CO tomorrow (avoid conflict).
                    _demand_done = demand_remaining.get(sku, 0.0) <= 0
                    # Early-CO (_EARLY_CO_ENABLED): a press may reassign BEFORE its
                    # SKU's demand is done, IFF that SKU's remaining demand is met by
                    # its OTHER (n-1) presses within the horizon — i.e. this press is
                    # surplus. Replicates the static proxy's early freeing, on real
                    # state. Only fires if _select_ratio_co_target finds a needy target.
                    _early_co = False
                    if (_EARLY_CO_ENABLED and not _demand_done
                            and press not in co_press_map
                            and press not in dynamic_co_tracker):
                        _n_cur = press_count.get(sku, 0)
                        if _n_cur > 1:
                            _rate_cur = _cure_qty_per_shift(ct) * 3
                            _rem_cur  = demand_remaining.get(sku, 0.0)
                            _hz_cur   = planning_days - day + 1
                            if (_rate_cur > 0
                                    and _rem_cur / ((_n_cur - 1) * _rate_cur) <= _hz_cur):
                                _early_co = True
                    if (((_DYNAMIC_CO_TRACKER_ENABLED and _demand_done) or _early_co)
                            and not _cleaned          # a just-started clean defers any CO
                            and press not in co_press_map
                            and press not in dynamic_co_tracker):
                        _next_day_cos = {p for p, _, _ in co_by_day.get(day + 1, [])}
                        # Phase 3 pull-forward: a press blocked here (planned CO booked
                        # TOMORROW) normally idles the rest of today. With the scorer on,
                        # bring tomorrow's planned CO FORWARD to now instead of idling.
                        _pf_target = None
                        if _CO_SCORER_ENABLED and _mould_gate and press in _next_day_cos:
                            _pf_target = next(
                                (ns for (p, _o, ns) in co_by_day.get(day + 1, [])
                                 if p == press), None)
                        if (press not in _next_day_cos) or (_pf_target is not None):
                            _slots_left = MAX_CHANGEOVERS_PER_DAY - daily_co_count[day]
                            if _slots_left > 0:
                                _horizon_left = planning_days - day + 1
                                _pf_fired = False
                                if _CO_SCORER_ENABLED and _mould_gate:
                                    # Pull-forward if tomorrow's planned CO is feasible now;
                                    # else fall back to the SAME tuned dynamic selector as
                                    # Phase-2 (don't disturb its behaviour — measured better
                                    # than the utility picker for reactive mid-shift COs).
                                    _bf = _bld_free_min_shift()
                                    if (_pf_target is not None
                                            and demand_remaining.get(_pf_target, 0.0) > 0
                                            and _n_free_for(_pf_target, press) >= 2
                                            and _bld_capacity(_pf_target, _bf)
                                            >= _cure_qty_per_shift(cure_ct_map.get(
                                                _pf_target, DEFAULT_CURING_CT))):
                                        _target = _pf_target
                                        _pf_fired = True
                                    else:
                                        _already = set(dynamic_co_tracker[p][1]
                                                       for p in dynamic_co_tracker)
                                        _target = _select_dynamic_co_target(
                                            sku, demand_remaining, press_count,
                                            cure_ct_map, priority_score_map,
                                            gt_inventory, _horizon_left, _already,
                                        )
                                elif _RATIO_CO_ALLOCATION_ENABLED or _EARLY_CO_ENABLED:
                                    _pending_counts = Counter(
                                        dynamic_co_tracker[p][1] for p in dynamic_co_tracker
                                    )
                                    _target = _select_ratio_co_target(
                                        sku, press, demand_remaining, press_count,
                                        _pending_counts, demand_dict, cure_ct_map,
                                        press_to_demand_targets, press_total_demand,
                                        _horizon_left,
                                        sku_to_press_count=sku_to_press_count,
                                        rich_ranking=_RATIO_CO_RICH_RANKING_ENABLED,
                                    )
                                else:
                                    _already = set(dynamic_co_tracker[p][1]
                                                   for p in dynamic_co_tracker)
                                    _target = _select_dynamic_co_target(
                                        sku, demand_remaining, press_count,
                                        cure_ct_map, priority_score_map,
                                        gt_inventory, _horizon_left, _already,
                                    )
                                # Mould gate: only fire the reactive CO if 2 eligible
                                # moulds are free for the target; else keep this SKU.
                                if _target is not None and not _try_mount(press, _target):
                                    mould_blocked_cos += 1
                                    _target = None
                                if _target is not None and _CO_SCORER_ENABLED:
                                    if _pf_fired:
                                        # consume tomorrow's planned CO so it can't fire twice
                                        co_by_day[day + 1] = [
                                            (p, o, n) for (p, o, n) in co_by_day.get(day + 1, [])
                                            if p != press]
                                        daily_co_count[day + 1] = max(
                                            0, daily_co_count.get(day + 1, 0) - 1)
                                        co_scorer_stats["pullfwd"] += 1
                                    else:
                                        co_scorer_stats["dynamic"] += 1
                                if _target is not None:
                                    # CO starts NOW — mid-shift, the moment demand was
                                    # met. Charge the real CURING_CO_CHANGEOVER_MINS from
                                    # this point: it eats the rest of THIS shift, and the
                                    # overhang carries into the next shift (co_carry), so
                                    # the new SKU begins mid-shift rather than free at the
                                    # boundary. Without this a dynamic CO cost nothing.
                                    press_count[sku] = max(
                                        0, press_count.get(sku, 0) - 1
                                    )
                                    dynamic_co_tracker[press] = (cur_shift_global, _target)
                                    daily_co_count[day] += 1
                                    _co_here = max(0.0, min(float(CURING_CO_CHANGEOVER_MINS),
                                                            _avail - prod_mins))
                                    press_stats[press]["co_mins"] += _co_here
                                    _co_mins_shift += _co_here
                                    _seg_co_trig    = _co_here
                                    _dyn_co_tgt     = _target      # surfaced in Remarks
                                    co_carry[press] = float(CURING_CO_CHANGEOVER_MINS) - _co_here
                                    # Rule 2: a curing CO resets mould life (the CO
                                    # shift already includes a clean).
                                    mould_life[press]  = MOULD_CLEAN_CYCLES
                                    clean_carry[press] = 0.0
                                    cure_co_events.append({
                                        "Date":       date_str,
                                        "Day":        day,
                                        "Shift":      shift,
                                        "Press":      press,
                                        "From_SKU":   sku,
                                        "Target_SKU": _target,
                                        "CO_Type":    "Early-CO" if (_early_co and not _demand_done) else "Dynamic",
                                    })
                                    print(
                                        f"    [DynCO] Day {day} Shift {shift}: "
                                        f"press {press} {sku}→{_target} "
                                        f"(slot {MAX_CHANGEOVERS_PER_DAY - _slots_left + 1}"
                                        f"/{MAX_CHANGEOVERS_PER_DAY})"
                                    )
                else:
                    cured = 0
                    if status == "CHANGEOVER":
                        press_stats[press]["co_mins"] += SHIFT_MINS
                        _co_mins_shift = float(SHIFT_MINS)
                    elif status == "MOULD_CLEAN":
                        press_stats[press]["clean_mins"] += SHIFT_MINS
                        _clean_mins_shift = float(SHIFT_MINS)

                # ── Emit ONE ROW PER SEGMENT ──────────────────────────────────
                # A press-shift is broken into its real time segments so production
                # and any changeover / mould clean appear as SEPARATE rows, each with
                # its own wall-clock window. Chronological order within the shift:
                #   [CO overhang carried in] → [clean overhang in] → [production] →
                #   [CO fired mid-shift] → [clean fired mid-shift].
                # A whole-shift CHANGEOVER / MOULD_CLEAN is a single row.
                # STARVED/IDLE production rows span the rest of the shift (real idle
                # window) so StartTime ≠ EndTime. The RUNNING segment is emitted exactly
                # once per press-shift, so the sheet's STARVED count still equals the KPI.
                _segs = []   # (seg_status, seg_sku, seg_mins, seg_qty, seg_remark)
                if status in ("CHANGEOVER", "MOULD_CLEAN"):
                    if status == "CHANGEOVER":
                        _co_tgt = co_press_map.get(press) or (
                            dynamic_co_tracker[press][1] if press in dynamic_co_tracker else None)
                        _r = f"CO → {_co_tgt}" if _co_tgt else "CHANGEOVER"
                    else:
                        _r = "MOULD_CLEAN"
                    _segs.append((status, sku, float(SHIFT_MINS), 0, _r))
                else:                                    # RUNNING shift
                    _dleft = demand_remaining.get(sku, 0.0)
                    if cured > 0:                    _prod_remark = ""
                    elif _dleft <= 0:                _prod_remark = "IDLE (demand met)"
                    elif int(round(gt_avail)) == 0:  _prod_remark = "STARVED (no GT)"
                    else:                            _prod_remark = ""
                    if _seg_co_in > 0:
                        # Overhang of a mid-shift dynamic CO into the NEXT shift —
                        # a continuation of an already-counted changeover, NOT a new
                        # event. Labelled distinctly so counting "CO → " rows in this
                        # sheet gives the true event count (= curingChangeovers).
                        _segs.append(("CHANGEOVER", sku, _seg_co_in, 0, f"CO (cont.) → {sku}"))
                    if _seg_clean_in > 0:
                        _segs.append(("MOULD_CLEAN", sku, _seg_clean_in, 0, "MOULD_CLEAN"))
                    _prod_dur = prod_mins
                    if cured == 0 and _seg_co_trig == 0 and _seg_clean_trig == 0:
                        _prod_dur = max(0.0, SHIFT_MINS - _seg_co_in - _seg_clean_in)  # idle rest of shift
                    _segs.append(("RUNNING", sku, _prod_dur, cured, _prod_remark))
                    if _seg_co_trig > 0:
                        _segs.append(("CHANGEOVER", sku, _seg_co_trig, 0, f"CO → {_dyn_co_tgt}"))
                    if _seg_clean_trig > 0:
                        _segs.append(("MOULD_CLEAN", sku, _seg_clean_trig, 0, "MOULD_CLEAN"))

                _s_start = _shift_start_dt(date_str, shift)
                _cursor  = 0.0
                for _sstat, _ssku, _smins, _sqty, _srem in _segs:
                    _st = _s_start + timedelta(minutes=_cursor)
                    _en = _st + timedelta(minutes=_smins)
                    _cursor += _smins
                    cure_shift_rows.append({
                        "Date":          date_str,
                        "Shift":         shift,
                        "Machine":       press,
                        "SKUCode":       _ssku,
                        "StartTime":     _fmt_dt(_st),
                        "EndTime":       _fmt_dt(_en),
                        "Qty":           _sqty,
                        "CO_Mins":          int(round(_smins)) if _sstat == "CHANGEOVER" else 0,
                        "Mould_Clean_Mins": int(round(_smins)) if _sstat == "MOULD_CLEAN" else 0,
                        "CycleTime_min": round(ct, 1),
                        "GT_Inventory":  int(round(gt_avail)),
                        "Remarks":       _srem,
                        "_status":       _sstat,
                        "_demand_left":  demand_remaining.get(_ssku, 0.0) if _sstat == "RUNNING" else None,
                    })

        # ── 5. Pool replacement: swap out any finished SKUs ─────────────────
        # If a pool SKU's demand_remaining hit 0 this day, remove it and add
        # the next best same-inch eligible SKU (highest urgency, not yet in pool).
        days_left_now = planning_days - day
        for machine in list(machine_pool.keys()):
            pool = machine_pool[machine]
            finished = [s for s in pool if demand_remaining.get(s, 0.0) <= 0]
            if not finished:
                continue
            new_pool   = [s for s in pool if demand_remaining.get(s, 0.0) > 0]
            pool_set   = set(new_pool)
            dom_inch   = _MACHINE_DOMINANT_INCH.get(str(machine), "")
            eligible_m = machine_skus.get(machine, set())
            # Candidates: same dominant inch, has demand, has active presses, not already in pool
            replacements = sorted(
                [s for s in eligible_m
                 if s not in pool_set
                 and demand_remaining.get(s, 0.0) > 0
                 and press_count.get(s, 0) > 0
                 and sku_inch.get(s, "") == dom_inch],
                key=lambda s: -_urgency_score(
                    s, demand_remaining, press_count, cure_ct_map, days_left_now
                ),
            )
            slots = POOL_SIZE - len(new_pool)
            new_pool.extend(replacements[:slots])
            machine_pool[machine] = new_pool
            if finished:
                print(f"    [Pool] Day {day}: machine {machine} dropped {finished}, "
                      f"added {replacements[:slots]}")

        # ── 6. GT shelf-life writeoff ─────────────────────────────────────
        day_writeoff = _writeoff_stale_gt(gt_inventory, last_build_day, day)
        writeoff_total += day_writeoff

        # End-of-day total GT inventory (after curing + writeoff) — audits the 10k
        # plant cap. Should never exceed MAX_ENDOFDAY_GT_INVENTORY when the cap is on.
        endday_gt_inv = sum(v for v in gt_inventory.values() if v > 0)
        if _ENDOFDAY_GT_CAP_ENABLED and endday_gt_inv > MAX_ENDOFDAY_GT_INVENTORY + 1:
            print(f"  [GT-CAP WARN] Day {day}: end-of-day GT inventory "
                  f"{endday_gt_inv:,.0f} > cap {MAX_ENDOFDAY_GT_INVENTORY:,}")

        # ── 6. Apply CO transitions ───────────────────────────────────────
        # today_cos was already mould-gated at day-start (moulds committed there),
        # so every entry here is feasible — just apply the transition.
        for press, old_sku, new_sku in today_cos:
            press_count[old_sku] = max(0, press_count.get(old_sku, 0) - 1)
            press_count[new_sku] = press_count.get(new_sku, 0) + 1
            press_state[press]   = {"sku": new_sku, "status": "RUNNING"}
            curing_allowable[new_sku].append(press)
            # Rule 2: a curing CO resets mould life (CO includes a clean).
            mould_life[press]  = MOULD_CLEAN_CYCLES
            clean_carry[press] = 0.0
            # Planned COs execute the CHANGEOVER in the shift chosen by co_shift_idx
            # (the shift the press finishes its old SKU — Shift A when it is already
            # free or when the CO is preemptive). Record here (once per day) so both
            # static-schedule and rolling-horizon planned COs appear in the curing
            # Changeover Plan output sheet.
            cure_co_events.append({
                "Date":       date_str,
                "Day":        day,
                "Shift":      SHIFTS[co_shift_idx.get(press, 0)],
                "Press":      press,
                "From_SKU":   old_sku,
                "Target_SKU": new_sku,
                "CO_Type":    "Planned",
            })

        # The planned COs have now completed for the day — release the old moulds
        # that were held reserved through their pre-CO shifts.
        if _mould_gate:
            _release_deferred()

        # Daily summary — report GT-only (not carcass) for "built" KPI
        d_gt_built = sum(day_gt_built.values())  # real GT (excludes Stage-1 carcass)
        d_built    = sum(day_built.values())      # all machines (for internal tracking)
        d_cured    = sum(day_cured_d.values())
        n_active   = sum(1 for st in press_state.values() if st["status"] == "RUNNING")
        dem_met    = total_demand - sum(max(0, v) for v in demand_remaining.values())
        cov        = dem_met / total_demand * 100 if total_demand > 0 else 0
        if day % 5 == 0 or day == 1 or day == planning_days:
            print(f"  Day {day:2d} | built {d_gt_built:6,.0f} | cured {d_cured:6,.0f} | "
                  f"presses {n_active} | COs {len(today_cos)} | "
                  f"writeoff {day_writeoff:,.0f} | coverage {cov:.1f}%")
        daily_summary.append({
            "Day": day, "Date": date_str,
            "GT_Built": int(round(d_gt_built)), "GT_Cured": int(round(d_cured)),
            "GT_Writeoff": int(round(day_writeoff)),
            "EndDay_GT_Inventory": int(round(endday_gt_inv)),
            "Active_Presses": n_active, "COs_Today": len(today_cos),
            "Demand_Coverage": round(cov, 2),
        })

    # ── Final KPIs ────────────────────────────────────────────────────────────
    total_built  = sum(r["GT_Built"]  for r in daily_summary)
    total_cured  = sum(r["GT_Cured"]  for r in daily_summary)
    dem_met      = total_demand - sum(max(0, v) for v in demand_remaining.values())
    final_cov    = dem_met / total_demand * 100 if total_demand > 0 else 0
    # A press only "starves" when it's RUNNING, has zero GT, produced zero units,
    # AND still has real demand left to cure. If demand_left is already 0, the
    # SKU's job is done — the press is correctly idle, not starved (see the
    # false-positive case this excluded: press finishes a SKU's demand, GT drains
    # to 0 naturally, and the old formula counted that success as a failure).
    starvation_n = sum(
        1 for r in cure_shift_rows
        if r.get("_status") == "RUNNING" and r.get("GT_Inventory", 1) == 0
        and r.get("Qty", 0) == 0 and (r.get("_demand_left") or 0) > 0
    )

    # Curing CO breakdown (planned schedule + reactive dynamic) and mould cleans.
    _n_co_planned = sum(1 for e in cure_co_events if e.get("CO_Type") == "Planned")
    _n_co_dynamic = sum(1 for e in cure_co_events if e.get("CO_Type") in ("Dynamic", "Early-CO"))
    _n_co_total   = _n_co_planned + _n_co_dynamic
    _n_mould_cleans = int(round(sum(s.get("clean_mins", 0.0)
                                    for s in press_stats.values()) / MOULD_CLEAN_MINS))

    print("\n" + "=" * 70)
    print("  ROLLING PIPELINE — Results")
    print("=" * 70)
    print(f"  Total GT built       : {total_built:>10,.0f}")
    print(f"  Total cured          : {total_cured:>10,.0f}")
    print(f"  GT written off       : {writeoff_total:>10,.0f}")
    print(f"  Starvation events    : {starvation_n:>10,}")
    print(f"  Curing COs (total)   : {_n_co_total:>10,}"
          f"  (planned {_n_co_planned:,} + dynamic {_n_co_dynamic:,})")
    print(f"  Mould cleans taken   : {_n_mould_cleans:>10,}  "
          f"(clean {'ON' if _MOULD_CLEAN_ENABLED else 'OFF'}, "
          f"CO-spread {'ON' if _CO_SHIFT_SPREAD_ENABLED else 'OFF'})")
    print(f"  Mould-blocked COs    : {mould_blocked_cos:>10,}  "
          f"(mould gate {'ON' if _mould_gate else 'OFF'})")
    print(f"  Mould-retargeted COs : {mould_retargeted_cos:>10,}  "
          f"(Phase-2 opt {'ON' if (_mould_gate and _mould_opt) else 'OFF'})")
    if _CO_SCORER_ENABLED:
        _cs = co_scorer_stats
        print(f"  CO scorer ({'FULL' if _SCORER_FULL_REOPT else 'ADD'}) : "
              f"planned={_cs['planned']} pullfwd={_cs['pullfwd']} "
              f"retarget={_cs['retarget']} dynamic={_cs['dynamic']} "
              f"cancelled={_cs['cancelled']} build_blocked={_cs['build_blocked']}")
    if os.environ.get("MOULD_DEBUG"):
        # Cross-press exclusivity: is any mould listed in >1 press's owned set?
        _own_by_mould: dict = {}
        for _pr, _ms in _press_moulds.items():
            for _m in _ms:
                _own_by_mould.setdefault(_m, set()).add(_pr)
        _dbl = {m: ps for m, ps in _own_by_mould.items() if len(ps) > 1}
        print(f"  [MOULD-DBL] moulds claimed by >1 press (final state): {len(_dbl)}")
        # also cross-check _mould_owner vs _press_moulds consistency
        _mismatch = sum(1 for _m, _ps in _own_by_mould.items()
                        if _mould_owner.get(_m) not in _ps)
        print(f"  [MOULD-DBL] _mould_owner/_press_moulds mismatches: {_mismatch}")
        if _mould_selfcheck[0]:
            print(f"  [MOULD-LEAK] {_mould_selfcheck[0]} running press-shifts own <2 moulds")
    print(f"  Demand coverage      : {final_cov:>9.1f}%  ({dem_met:,.0f} / {total_demand:,.0f})")
    _eod_inv = [r["EndDay_GT_Inventory"] for r in daily_summary if "EndDay_GT_Inventory" in r]
    if _eod_inv:
        _n_over = sum(1 for v in _eod_inv if v > MAX_ENDOFDAY_GT_INVENTORY)
        print(f"  End-day GT inventory : max {max(_eod_inv):>6,.0f} | "
              f"mean {sum(_eod_inv)/len(_eod_inv):>6,.0f} | "
              f"days>{MAX_ENDOFDAY_GT_INVENTORY//1000}k {_n_over}  (cap "
              f"{'ON' if _ENDOFDAY_GT_CAP_ENABLED else 'OFF'}, fwd-buf "
              f"{'ON' if _FORWARD_BUFFER_ENABLED else 'OFF'})")

    # ── Write Excel outputs (same format as legacy pipeline) ─────────────────
    closing_gt_bal = {sku: v for sku, v in gt_inventory.items() if v > 0}

    _write_rolling_building_excel(
        output_path    = build_output,
        bld_shift_rows = bld_shift_rows,
        bld_co_events  = bld_co_events,
        df_day0        = df_day0,
        sku_machine_map = sku_machine_map,
        opening_gt     = opening_gt,
        demand_dict    = demand_dict,
        planning_days  = planning_days,
        n_curing_cos   = len(co_events),
        endday_gt_by_date = {r["Date"]: r["EndDay_GT_Inventory"] for r in daily_summary},
    )
    _write_rolling_curing_excel(
        output_path       = curing_output,
        cure_shift_rows   = cure_shift_rows,
        cure_co_events    = cure_co_events,
        press_stats       = dict(press_stats),
        press_sku_stats   = dict(press_sku_stats),
        daily_cured       = dict(daily_cured),
        sku_cured         = dict(sku_cured),
        closing_gt_bal    = closing_gt_bal,
        build_by_shift_sku= build_by_shift_sku,
        opening_gt        = opening_gt,
        demand_dict       = demand_dict,
        cure_ct_map       = cure_ct_map,
        curing_allowable  = dict(curing_allowable),
        planning_days     = planning_days,
        plan_start        = plan_start,
        df_day0           = df_day0,
        mould_life        = dict(mould_life),
        mould_info        = ({
            "press_moulds":  {p: sorted(ms) for p, ms in _press_moulds.items()},
            "final_sku":     {p: st["sku"] for p, st in press_state.items()},
            "mould_skus":    {m: set(s) for m, s in _mould_skus.items()},
            "sku_moulds":    {s: set(m) for s, m in _sku_moulds.items()},
            "press_count":   dict(press_count),
            "blocked":       mould_blocked_cos,
            "retargeted":    mould_retargeted_cos,
            "day0_topups":   mould_day0_topups,
            "scorer":        (co_scorer_stats if _CO_SCORER_ENABLED else None),
            "events":        mould_events,          # per-swap movement log (day, press, sku, added, removed)
            "assignments":   mould_assignments,     # full (press, sku, 2-moulds) timeline incl Day-0
        } if _mould_gate else None),
    )

    return {
        "total_built":       total_built,
        "total_cured":       total_cured,
        "gt_writeoff":       writeoff_total,
        "starvation_events": starvation_n,
        "demand_coverage":   final_cov,
        "demand_remaining":  demand_remaining,
        "gt_inventory":      dict(gt_inventory),
        "daily_summary":     daily_summary,
        "co_events":         co_events,
        "n_co":              _n_co_total,      # planned + dynamic (was planned-only)
        "n_co_planned":      _n_co_planned,
        "n_co_dynamic":      _n_co_dynamic,
        "n_mould_cleans":    _n_mould_cleans,
        "mould_blocked_cos": mould_blocked_cos,
        "mould_retargeted_cos": mould_retargeted_cos,
        "co_scorer_stats": co_scorer_stats if _CO_SCORER_ENABLED else None,
        "build_output":      build_output,
        "curing_output":     curing_output,
    }


# ══════════════════════════════════════════════════════════════════════════════
# LEGACY PIPELINE (31-day LP — use --legacy flag)
# ══════════════════════════════════════════════════════════════════════════════

def run_pipeline(
    demand_path:   str | None = None,
    cc_output:     str | None = None,
    build_output:  str | None = None,
    curing_output: str | None = None,
    plan_start:    datetime | None = None,
    planning_days: int | None = None,
) -> dict:
    demand_path   = demand_path   or DEMAND_FILE
    cc_output     = cc_output     or CC_OUTPUT
    build_output  = build_output  or BUILD_OUTPUT
    curing_output = curing_output or CURING_OUTPUT
    plan_start    = plan_start    or PLAN_START
    planning_days = planning_days or PLANNING_DAYS

    print("\n" + "=" * 70)
    print("  PIPELINE — Step 1: Curing Consumption (Dynamic)")
    print("=" * 70)
    cc_result = run_dynamic_consumption(
        demand_path=demand_path, output_path=cc_output,
        plan_start=plan_start, planning_days=planning_days,
        max_co_per_day=MAX_CHANGEOVERS_PER_DAY,
    )
    co_events = cc_result["co_events"]
    df_day0   = cc_result["df_day0"]
    print(f"\n  [Pipeline] Step 1 complete — {len(co_events)} CO events → {os.path.basename(cc_output)}")

    df_cons = df_day0[df_day0["Category"].isin({"Runner-In", "Non-Runner-In"})].copy()
    if "Skip_Reason" in df_cons.columns:
        df_cons = df_cons[
            df_cons["Skip_Reason"].isna() | (df_cons["Skip_Reason"].astype(str).str.strip() == "")
        ].copy()
        df_cons = df_cons.drop(columns=["Skip_Reason"], errors="ignore")

    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False, dir=tempfile.gettempdir())
    tmp.close()
    with pd.ExcelWriter(tmp.name, engine="openpyxl") as writer:
        df_cons.to_excel(writer, sheet_name="Consumption Summary", index=False)

    print("\n" + "=" * 70)
    print("  PIPELINE — Step 2: Building Scheduler (B2C)")
    print("=" * 70)
    try:
        build_result = run_from_database_b2c(
            plan_start=plan_start, consumption_path=tmp.name,
            output_path=build_output, planning_days=planning_days,
            external_co_schedule=co_events,
            max_changeovers_per_day=MAX_CHANGEOVERS_PER_DAY,
            min_campaign_mins=MIN_CAMPAIGN_MINS,
            build_lead_shifts=BUILD_LEAD_SHIFTS,
        )
    finally:
        os.unlink(tmp.name)
    print(f"\n  [Pipeline] Step 2 complete → {os.path.basename(build_output)}")

    print("\n" + "=" * 70)
    print("  PIPELINE — Step 3: Curing Schedule (B2C)")
    print("=" * 70)
    curing_result = run_curing_b2c(
        building_path=build_output, output_path=curing_output,
        demand_path=demand_path, plan_start=plan_start, planning_days=planning_days,
    )
    total_cured = sum(curing_result["daily_cured"].values())
    print(f"\n  [Pipeline] Step 3 complete — Total cured: {total_cured:,.0f} → {os.path.basename(curing_output)}")

    return {
        "co_events": co_events, "n_co": len(co_events),
        "cc_output": cc_output, "build_output": build_output, "curing_output": curing_output,
        "build_result": build_result, "curing_result": curing_result,
    }


if __name__ == "__main__":
    _args   = sys.argv[1:]
    _legacy = "--legacy" in _args
    _demand = next((a for a in _args if not a.startswith("--")), None)

    if _legacy:
        print("[Pipeline] Legacy 31-day LP mode (--legacy flag)")
        result = run_pipeline(demand_path=_demand)
        print("\n" + "█" * 70)
        print("  LEGACY PIPELINE COMPLETE")
        print("█" * 70)
        print(f"  Changeovers scheduled : {result['n_co']}")
        print(f"  1. Curing consumption : {result['cc_output']}")
        print(f"  2. Building schedule  : {result['build_output']}")
        print(f"  3. Curing schedule    : {result['curing_output']}")
        total = sum(result["curing_result"]["daily_cured"].values())
        print(f"  Total cured (month)   : {total:,.0f} tyres")
        print("█" * 70)
    else:
        print("[Pipeline] Rolling day-by-day mode (new architecture)")
        result = run_rolling_pipeline(demand_path=_demand)
        print("\n" + "█" * 70)
        print("  ROLLING PIPELINE COMPLETE")
        print("█" * 70)
        print(f"  Curing COs (total)    : {result['n_co']:>10,}"
              f"  (planned {result.get('n_co_planned', 0):,} + dynamic {result.get('n_co_dynamic', 0):,})")
        print(f"  Mould cleans taken    : {result.get('n_mould_cleans', 0):>10,}")
        print(f"  GT built (month)      : {result['total_built']:>10,.0f}")
        print(f"  GT cured (month)      : {result['total_cured']:>10,.0f}")
        print(f"  GT written off        : {result['gt_writeoff']:>10,.0f}")
        print(f"  Starvation events     : {result['starvation_events']:>10,}")
        print(f"  Demand coverage       : {result['demand_coverage']:>9.1f}%")
        print(f"  Building output       : {result['build_output']}")
        print(f"  Curing  output        : {result['curing_output']}")
        print("█" * 70)
        print("\n  Worst 10 SKUs by remaining demand:")
        rem = sorted(result["demand_remaining"].items(), key=lambda x: -x[1])[:10]
        for sku, qty in rem:
            print(f"    {sku}: {qty:,.0f} units remaining")
