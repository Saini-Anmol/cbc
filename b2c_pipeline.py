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
                Shift Schedule (Clean) | Daily GT & Carcass | Demand Fulfillment (B2C)
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
from curing_consumption_dynamic import run_dynamic_consumption, ConsumptionConfig, COScheduler
from building_b2c import run_from_database_b2c
from curing_b2c import run_curing_b2c
from curing_consumption import ConsumptionETL

# ── All params from bc_config (single source of truth) ────────────────────────
from bc_config import (
    PLAN_START,
    PLANNING_DAYS,
    DEMAND_FILE,
    GT_SHELF_LIFE_DAYS,
    MAX_CHANGEOVERS_PER_DAY,
    MIN_CAMPAIGN_MINS,
    BUILD_LEAD_SHIFTS,
    MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT,
    GT_BUFFER_SHIFTS,
    BUILDING_CO_SAME_SIZE,
    BUILDING_CO_DIFF_SIZE,
    SHIFT_MINS,
    SHIFT_STARTS,
    SHIFT_ENDS,
    POOL_SIZE,
    STARVATION_BUFFER_MINS,
    CO_CLASS_B_THRESHOLD,
    DYNAMIC_CC_OUTPUT  as CC_OUTPUT,
    BUILDING_OUTPUT    as BUILD_OUTPUT,
    CURING_B2C_OUTPUT  as CURING_OUTPUT,
)

# ── Machine group map ─────────────────────────────────────────────────────────
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
    def _max_cos(mach: str) -> int:
        # Flex machines get extra CO budget so they can take an off-inch
        # excursion AFTER exhausting same-inch work (which uses the normal 2).
        if mach in _INCH_FLEX_MACHINES:
            return MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT + _INCH_FLEX_EXTRA_COS
        return MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT
    projected_gt: dict[str, float] = dict(gt_inventory)

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
            if cur in eligible and _defc(cur, eff_buf) > 0 and not flex_reclaim:
                mins = min(s["remaining"],
                           _defc(cur, eff_buf) / rate if rate > 0 else s["remaining"])
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
                    if (m in (_SOFT_LOCK_MACHINES | _INCH_FLEX_MACHINES)
                            and to_inch != dom and not s["primary_done"]):
                        continue
                    cost = _co_cost(m, cur_inch, to_inch)
                    if s["remaining"] - cost < MIN_CAMPAIGN_MINS:
                        continue
                    is_urgent = (sku in co_target_skus and projected_gt.get(sku, 0.0) == 0
                                 and demand_remaining.get(sku, 0.0) > 0)
                    _flex_off_ok = (m in _INCH_FLEX_MACHINES and to_inch != dom
                                    and s["primary_done"])
                    if cost > 0.30 * s["remaining"] and not is_urgent and not _flex_off_ok:
                        continue
                    avail = s["remaining"] - cost
                    mins = min(avail, d / rate if rate > 0 else avail)
                    qty = int(mins * rate)
                    if mins < MIN_CAMPAIGN_MINS or qty <= 0:
                        continue
                    tier, primary = _tierg(sku, m, d)
                    inch_penalty = 0 if to_inch == dom else 1
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
        _flex_reclaim = (
            machine in _INCH_FLEX_MACHINES
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
) -> None:
    """
    Write building Excel matching the legacy bc_building_schedule output.

    Sheets:
      1. Shift Schedule         — per-shift rows (title at row 1, blank at row 2, header at row 3)
      2. Changeover Plan        — building machine CO events
      3. SKU Classification     — category summary from Day 0 consumption
      4. Shift Schedule (Clean) — production-only rows (no CO sentinels)
      5. Daily GT & Carcass     — daily GT and carcass totals
      6. Demand Fulfillment (B2C) — per-SKU demand vs planned GT
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
    bld_cols = ["Machine", "Date", "Shift", "SKUCode", "Qty",
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

    # ── Sheet 4: Shift Schedule (Clean) — production rows only ────────────────
    ws_clean = wb.create_sheet("Shift Schedule (Clean)")
    _xl_header(ws_clean, 1, bld_cols)
    prod_rows = [r for r in bld_shift_rows if str(r.get("SKUCode","")).upper() not in _SENTINEL]
    for ri, row in enumerate(prod_rows, 2):
        for ci, col in enumerate(bld_cols, 1):
            ws_clean.cell(row=ri, column=ci, value=row.get(col, "")).alignment = _ctr()
    for col in ws_clean.columns:
        w = max((len(str(c.value or "")) for c in col), default=8)
        ws_clean.column_dimensions[get_column_letter(col[0].column)].width = min(w + 2, 38)

    # ── Sheet 5: Daily GT & Carcass ────────────────────────────────────────────
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
    daily_cols = ["Date", "GT_Produced", "Carcass_Produced", "Total_Units",
                  "Active_SKUs", "Cumulative_GT"]
    _xl_header(ws_daily, 1, daily_cols)
    cum_gt = 0
    for ri, (date, v) in enumerate(sorted(daily_agg.items()), 2):
        cum_gt += v["GT_Produced"]
        vals = [date, v["GT_Produced"], v["Carcass_Produced"],
                v["Total_Units"], len(v["Active_SKUs"]), cum_gt]
        for ci, val in enumerate(vals, 1):
            ws_daily.cell(row=ri, column=ci, value=val).alignment = _ctr()
    ws_daily.column_dimensions["A"].width = 14
    for ltr in "BCDEF":
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
        "Util_Pct", "CO_Pct", "Idle_Pct",
        "SKUs_Served", "COs_Done",
    ]
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
            util_pct, co_pct, idle_pct,
            len(mach_skus[m]), mach_co_count[m],
        ]
        for ci, val in enumerate(vals, 1):
            cell = ws_util.cell(row=ri, column=ci, value=val)
            cell.fill = _fill(_GREEN_U if grp != "Stage-1" and util_pct >= 0.80 else
                              _AMBER_U if util_pct >= 0.40 or grp == "Stage-1" else _RED_U)
            cell.alignment = _ctr()
            if ci in (9, 10, 11):  # percent columns
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

    for ltr_idx, w in enumerate([14,16,16,12,14,12,10,10,10,10,10,12,10], 1):
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
    cols = ["SKUCode", "Priority", "Demand", "GT_Inventory", "Planned_Units",
            "Gap", "Fulfillment_Pct", "Status", "CycleTime_min",
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
    u_cols = ["Machine", "Available_Mins", "Used_Mins", "CO_Mins", "Idle_Mins",
              "Utilization_Pct", "CO_Pct", "Idle_Pct",
              "SKUs_Count", "Total_Cycles", "Total_Units"]
    _hdr(ws, 2, u_cols)
    for ri, press in enumerate(all_presses, 3):
        s    = press_stats[press]
        used = s["running_mins"]
        co   = s["co_mins"]
        idle = max(0, avail_mins - used - co - s["clean_mins"])
        pct      = used / avail_mins if avail_mins else 0.0
        co_pct   = co   / avail_mins if avail_mins else 0.0
        idle_pct = idle / avail_mins if avail_mins else 0.0
        color = _GREEN if pct >= 0.90 else (_AMBER if pct >= 0.60 else _RED)
        vals  = [press, avail_mins, round(used), round(co), round(idle),
                 pct, co_pct, idle_pct,
                 len(s["skus"]), s["cycles"], s["units"]]
        for ci, v in enumerate(vals, 1):
            cell = ws.cell(row=ri, column=ci, value=v)
            cell.fill = _fill(color); cell.alignment = _ctr()
            if ci in (6, 7, 8): cell.number_format = "0.0%"
    for ltr in "ABCDEFGH": ws.column_dimensions[ltr].width = 17
    ws.freeze_panes = "A3"

    # ── Sheet 3: Shift Schedule ────────────────────────────────────────────────
    ws = wb.create_sheet("Shift Schedule")
    ss_cols = ["Date", "Shift", "Machine", "SKUCode", "StartTime", "EndTime",
               "Qty", "CycleTime_min", "GT_Inventory", "Remarks"]
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

    # ── Sheet 4: Mould Tracker (placeholder) ─────────────────────────────────
    ws = wb.create_sheet("Mould Tracker")
    mt_cols = ["MouldNo", "Compatible_SKUs", "Life_Remaining", "Assigned_Machine"]
    _hdr(ws, 1, mt_cols)
    ws.cell(row=2, column=1,
            value="Mould tracking not available in rolling pipeline — check curing_b2c output"
            ).font = Font(italic=True, color="888888")
    ws.column_dimensions["A"].width = 20; ws.column_dimensions["B"].width = 44

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
    cc_result = run_dynamic_consumption(
        demand_path=demand_path, output_path=CC_OUTPUT,
        plan_start=plan_start, planning_days=planning_days,
        max_co_per_day=MAX_CHANGEOVERS_PER_DAY,
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

    # ── E: Demand ─────────────────────────────────────────────────────────────
    demand_df = pd.read_excel(demand_path)
    sku_col = next((c for c in demand_df.columns if "SKU"  in str(c)), demand_df.columns[0])
    qty_col = next(
        (c for c in demand_df.columns
         if any(x in str(c) for x in ("Requirement","Demand","Qty","Quantity"))),
        demand_df.columns[1],
    )
    demand_dict: dict[str, float] = {
        str(r[sku_col]): float(r[qty_col] or 0)
        for _, r in demand_df.iterrows() if pd.notna(r.get(qty_col))
    }
    demand_remaining: dict[str, float] = dict(demand_dict)
    total_demand = sum(demand_dict.values())
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

    # Priority score for dynamic CO target selection (higher = serve first)
    _prio_col = next(
        (c for c in demand_df.columns if "Priority" in str(c) or "Score" in str(c)), None
    )
    priority_score_map: dict[str, float] = {}
    if _prio_col:
        priority_score_map = {
            str(r[sku_col]): float(r[_prio_col] or 0)
            for _, r in demand_df.iterrows() if pd.notna(r.get(_prio_col))
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

    print("\n" + "=" * 70)
    print("  ROLLING PIPELINE — Day-by-day simulation")
    print("=" * 70)

    for day in range(1, planning_days + 1):
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
        co_press_map: dict[str, str] = {p: ns for p, _, ns in today_cos}
        # SKUs that curing presses are switching TO today — building must pre-build for these
        co_target_skus_today: frozenset = frozenset(co_press_map.values())

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
                    # Shift A = CHANGEOVER (idle); Shift B + C = PRODUCTION (no mould clean)
                    if shift in ("B", "C"):
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
            if shift == "A":
                _co_press_counts: dict[str, int] = defaultdict(int)
                for _cp_sku in co_press_map.values():
                    _co_press_counts[_cp_sku] += 1
                for new_sku in co_target_skus_today:
                    new_ct    = cure_ct_map.get(new_sku, DEFAULT_CURING_CT)
                    n_co      = _co_press_counts.get(new_sku, 1)
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
                            "Qty":           co_mins,
                            "StartTime":     _fmt_dt(_co_start),
                            "EndTime":       _fmt_dt(_cursor),
                            "Machine_Group": _MACHINE_GROUP.get(machine, ""),
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
                        "StartTime":     _fmt_dt(_prod_start),
                        "EndTime":       _fmt_dt(_cursor),
                        "Machine_Group": _MACHINE_GROUP.get(machine, ""),
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
            for sku, need in sorted(stage2_built_this_shift.items(), key=lambda kv: (-kv[1], kv[0])):
                if need <= 0:
                    continue
                eligible = sorted(
                    (m for m in s1_sku_to_machines.get(sku, ())
                     if m not in s1_machines_used_this_shift),
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
                        "StartTime":     _fmt_dt(_c_start),
                        "EndTime":       _fmt_dt(_c_end),
                        "Machine_Group": "STAGE1",
                        "CO_Type":       "carcass",
                    })

            # ── 4. Curing simulation ───────────────────────────────────────
            for press in sorted(press_state):
                st  = press_state[press]
                sku = st["sku"]

                if press in co_press_map:
                    if shift == "A":
                        status = "CHANGEOVER"      # CO shift: full shift idle
                    else:                           # Shift B + C: production begins (no mould clean)
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

                if status == "RUNNING":
                    # Cap curing at remaining demand — never over-produce.
                    demand_left = max(0.0, demand_remaining.get(sku, 0.0))
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
                            and press not in co_press_map
                            and press not in dynamic_co_tracker):
                        _next_day_cos = {p for p, _, _ in co_by_day.get(day + 1, [])}
                        if press not in _next_day_cos:
                            _slots_left = MAX_CHANGEOVERS_PER_DAY - daily_co_count[day]
                            if _slots_left > 0:
                                _horizon_left = planning_days - day + 1
                                if _RATIO_CO_ALLOCATION_ENABLED or _EARLY_CO_ENABLED:
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
                                if _target is not None:
                                    # CO starts now (cur_shift_global); next shift = RUNNING
                                    press_count[sku] = max(
                                        0, press_count.get(sku, 0) - 1
                                    )
                                    dynamic_co_tracker[press] = (cur_shift_global, _target)
                                    daily_co_count[day] += 1
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
                    elif status == "MOULD_CLEAN":
                        press_stats[press]["clean_mins"] += SHIFT_MINS

                # Transparent Remarks — makes the output sheet self-consistent with
                # the printed Starvation KPI. A RUNNING press with zero output is only
                # STARVED if it still has demand left AND no GT; if its demand is
                # already met it is correctly IDLE, not starved (this is exactly the
                # KPI's guard — the sheet's STARVED count now equals the KPI number).
                if status == "CHANGEOVER":
                    _co_tgt = co_press_map.get(press)
                    if _co_tgt is None and press in dynamic_co_tracker:
                        _co_tgt = dynamic_co_tracker[press][1]
                    remark = f"CO → {_co_tgt}" if _co_tgt else "CHANGEOVER"
                elif status == "MOULD_CLEAN":
                    remark = "MOULD_CLEAN"
                elif status == "RUNNING":
                    _dleft = demand_remaining.get(sku, 0.0)
                    if cured > 0:
                        remark = ""                      # producing normally
                    elif _dleft <= 0:
                        remark = "IDLE (demand met)"      # NOT starvation — job done
                    elif int(round(gt_avail)) == 0:
                        remark = "STARVED (no GT)"        # genuine starvation
                    else:
                        remark = ""                      # has GT + demand but capped (rare)
                else:
                    remark = status

                cure_shift_rows.append({
                    "Date":          date_str,
                    "Shift":         shift,
                    "Machine":       press,
                    "SKUCode":       sku,
                    "StartTime":     SHIFT_STARTS.get(shift, ""),
                    "EndTime":       SHIFT_ENDS.get(shift, ""),
                    "Qty":           cured,
                    "CycleTime_min": round(ct, 1),
                    "GT_Inventory":  int(round(gt_avail)),
                    "Remarks":       remark,
                    "_status":       status,
                    "_demand_left":  demand_remaining.get(sku, 0.0) if status == "RUNNING" else None,
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

        # ── 6. Apply CO transitions ───────────────────────────────────────
        for press, old_sku, new_sku in today_cos:
            press_count[old_sku] = max(0, press_count.get(old_sku, 0) - 1)
            press_count[new_sku] = press_count.get(new_sku, 0) + 1
            press_state[press]   = {"sku": new_sku, "status": "RUNNING"}
            curing_allowable[new_sku].append(press)
            # Planned COs execute the CHANGEOVER in Shift A of this day (co_press_map).
            # Record here (once per day) so both static-schedule and rolling-horizon
            # planned COs appear in the curing Changeover Plan output sheet.
            cure_co_events.append({
                "Date":       date_str,
                "Day":        day,
                "Shift":      "A",
                "Press":      press,
                "From_SKU":   old_sku,
                "Target_SKU": new_sku,
                "CO_Type":    "Planned",
            })

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

    print("\n" + "=" * 70)
    print("  ROLLING PIPELINE — Results")
    print("=" * 70)
    print(f"  Total GT built       : {total_built:>10,.0f}")
    print(f"  Total cured          : {total_cured:>10,.0f}")
    print(f"  GT written off       : {writeoff_total:>10,.0f}")
    print(f"  Starvation events    : {starvation_n:>10,}")
    print(f"  Demand coverage      : {final_cov:>9.1f}%  ({dem_met:,.0f} / {total_demand:,.0f})")

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
        "n_co":              len(co_events),
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
        print(f"  Curing COs scheduled  : {result['n_co']}")
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
