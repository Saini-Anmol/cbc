# CBC / B2C Scheduler — Agent Context for Logic & Approach Brainstorming

This file gives a terminal AI agent enough context to reason about scheduling
logic trade-offs for the **JK Tyre BTP PCR line**. Read this before answering
any "should we change X" or "what should we do about Y" question.

---

## What this system does (one paragraph)

There are two production stages: **Building** (makes green tyres, GT) and
**Curing** (vulcanises GT into finished tyres). The B2C scheduler runs
**Building first** and derives Curing from it. Curing consumes exactly what
Building produces — starvation is zero by architectural design (curing is derived
FROM building output by `curing_b2c.py`, so it can never exceed available GT).
Building machines are constrained by a strict per-SKU demand
cap: total build across the horizon ≤ 100% of customer demand. There are
39 building machines (15 Stage-1 carcass, 6 Stage-2 GT, 18 Unistage GT), 167
active curing presses (as of May 2026 — from `testing_Daily_Running_Moulds`).
The planning horizon is 31 days × 3 shifts
(A 07:00 / B 15:00 / C 23:00) × 480 min/shift.

---

## Building Machine CO Types and Times

Two CO types exist for building machines. Always refer to them as `same_size_CO`
and `diff_size_CO` in code, output sheets, and conversation. Always specify
"building CO" or "curing CO" to avoid ambiguity.

| CO Type | Meaning | Time by machine group |
|---------|---------|----------------------|
| `same_size_CO` | New SKU is same tyre inch as current | 20 min (VMI) → 45 min (BJ) → 59 min (Stage-2) → 60 min (Stage-1/MID) → 110 min (Unistage 7501–7503) |
| `diff_size_CO` | New SKU is different inch | 88 min (Stage-2) → 90 min (BJ) → 120 min (VMI) → 180 min (Stage-1/MID/Unistage) |

**VMI same_size_CO = 20 min = 4.2% of one shift. The cheapest building CO.**
**Stage-1 / Unistage diff_size_CO = 180 min = 37.5% of one shift. Never do this without strong demand justification.**

### Machine CO priority rules
1. Assign building `same_size_CO` on VMI machines first (6001–6004, 7001–7004).
2. Avoid `diff_size_CO` on Stage-1 and Unistage (180 min = half a shift).
3. Stage-2 `diff_size_CO` = 88 min — acceptable if no VMI alternative.

### Multi-press feeding from one building machine — NOT POSSIBLE (confirmed plant data)
Same size ≠ same GT recipe. Each SKU has a unique compound + bead + construction.
GT built for SKU A cannot be cured as SKU B even if both are 16".
**One building machine always produces for exactly one SKU at a time.**

---

## Key invariants the agent must never break

1. **Demand cap is sacred.** Total GT built for any SKU ≤ `Demand_Qty`. Enforced
   in three layers: `_gt_remaining` tracker, daily `cur_mat` clip, LP ceiling
   constraint. Any proposed change must preserve these.
2. **Curing press changeover cap** is configurable via `MAX_CHANGEOVERS_PER_DAY` in `bc_config.py` (currently **14/day**). Building machine changeovers have NO cap.
3. **Stage-2 cannot run without Stage-1 carcass** (same shift or S-1 preferred).
   Unistage machines have no Stage-1 dependency.
4. **No waste GT.** Building output ≤ curing consumption. In B2C, this is
   architecture: curing is derived from building, not the other way around.

---

## Curing press physical facts & changeover timing rule

### Mould setup (physical — not a scheduling constraint)

Each curing press holds **2 moulds simultaneously**, each with **2 cavities** →
4 tyre slots per press per cycle. Mould clean is triggered after **3,000 cycles
= 6,000 tyres** produced. **This does NOT need to be modelled as a scheduling
event** — mould clean is absorbed into the CO window at the plant level.

**LH / RH press labelling:** Each physical curing press appears as two rows in
`testing_Daily_Running_Moulds` — one with suffix `LH` and one with `RH`.
`load_running_moulds()` strips the suffix and groups both rows into a single
press record keyed by the numeric press label (e.g. `"75206"`). CO events and
press_state always use this clean numeric key.

### Changeover timing — building MUST start simultaneously with curing CO

When a curing press starts a CO to a new target SKU on Day D Shift A, the
building machine(s) for that target SKU must ALSO start producing GT in **Shift A of Day D**.

```
Day D  Shift A:  Curing press  → CHANGEOVER (490 min, OCCUPIED — full shift)
                 Building mach → START producing GT for new SKU   ← simultaneous
Day D  Shift B:  Curing press  → PRODUCTION begins (new SKU)      ← no mould-clean idle
                 Building mach → CONTINUE producing GT
Day D  Shift C:  Curing press  → PRODUCTION continues
```

**Mould-clean is NOT modelled.** `bc_config.py`: `CURING_CO_DURATION_SHIFTS = 1`, `CURING_CO_CHANGEOVER_MINS = 490`.

**Implementation:** In **Shift A only**, `shift_cure_demand[new_sku] += _cure_qty_per_shift(new_ct)` is injected for every CO target SKU. Do not inject in Shift B — double injection creates 2× demand signal.

---

## SKU categories (Phase 0 classification)

| Category | Definition | Building approach |
|----------|------------|-------------------|
| Runner-In (RI) | On a curing press + in demand | Phase 1a — first priority |
| Runner-Out (RO) | On a curing press + NOT in demand | Candidates for press CO to a new SKU |
| Non-Runner-In (NRI) | NOT on any curing press + in demand | Phase 1b (joint pool) — residual capacity |

---

## Building machine groups

```
Stage-1  (15 machines: 6801, 6802, 6803, 6909, 6911, 7601, 7701, 7801–7804, 8001–8003, 8101)
  → Output: Carcass (semi-finished). Feeds Stage-2 only.

Stage-2  (6 machines: 8201, 8301, 8302, 8501, 8502, 7301)
  → Output: GT (requires Stage-1 carcass as input). BOTTLENECK (6 vs 15 Stage-1).

Unistage (18 machines: 6001–6004, 7001–7004, 7101–7106, 7201, 7501–7503)
  → Output: GT. Independent — no Stage-1 dependency.
```

---

## Inch-Run Study — Machine Group Inch Policies (CONFIRMED from May plant data)

```
MG Group     | Machines              | Allowed inches   | Policy
-------------|----------------------|------------------|-----------------------------
VMIMAXX      | 6001–6004, 7001–7004 | 14"–18"          | Hard-locked per dominant inch EXCEPT 7001/7003 (soft-locked)
BJ           | 7101–7106, 7201      | 13", 14", 15", 16"| Hard-locked per machine
TWO STAGE TBM| Stage-1 + Stage-2    | 12", 15", 13"    | ~Half single-inch machines
UNISTAGE     | 7501, 7502, 7503     | 12", 13" ONLY    | HARD — never assign 14"+
```

**Per-machine dominant inch:**
```
7001→16"  7002→14"  7003→15"  7004→14"   (VMIMAXX)
6001→14"  6002→15"  6003→17"  6004→16"   (VMIMAXX)
7101→15"  7102→15"  7103→13"  7104→15"   (BJ)
7105→13"  7106→13"  7201→16"             (BJ)
7501→12"  7502→13"  7503→13"             (UNISTAGE — perfectly locked)
```

**Group routing priority (inch → which group serves it first):**
```
12" → TWO STAGE TBM first → UNISTAGE second
13" → UNISTAGE first → BJ second → TBM last
14" → VMIMAXX first
15" → BJ first → TBM second → VMIMAXX last
16"/17"/18" → VMIMAXX only
```

---

## Inch Locking Policy — Three-tier approach

### Tier 1: Hard filter (`_HARD` in `b2c_pipeline.py`)

Applied to VMIMAXX (selected), BJ, and UNI_NARROW. Restricts eligible machines to their dominant inch.
Without BJ hard filters, high-demand 15" SKUs capture 13" BJ machines (e.g., 7106) because
their deficit signal dominates — this starves 13" demand (root cause of 1325217613082TUNE0 gap).

| Machine | Hard filter | Reason |
|---------|------------|--------|
| 6001 | 14" | dom=14" |
| 6002 | 15" | dom=15" |
| 6003 | 17"/18" | dom=17" — sole 17"/18" machine |
| 6004 | 16" | dom=16" |
| 7002 | 14" | dom=14" |
| 7004 | 14" | dom=14" |
| 7101 | 15" | dom=15" — BJ primary 15" machine |
| 7102 | 14"/15" | also covers 2 BJ-exclusive 14" RI SKUs |
| 7103 | 13" | dom=13" |
| 7104 | 14"/15" | also covers 2 BJ-exclusive 14" RI SKUs |
| 7105 | 13" | dom=13" |
| 7106 | 13" | dom=13" |
| 7201 | 16" | sole BJ 16" machine |
| 7501 | 12"/13" | confirmed allowable for 13" SKUs |
| 7502 | 13" | hard |
| 7503 | 13" | hard |

### Tier 1.5: Soft-lock machines (`_SOFT_LOCK_MACHINES` in `b2c_pipeline.py`)

**7001 and 7003 are soft-locked** — removed from `_HARD`. They serve their dominant inch first;
secondary inch is allowed in Campaign 2+ **only when primary inch demand is exhausted for that shift**
(controlled by `primary_demand_done` flag, scoped only to these two machines).

| Machine | Dominant (primary) | Secondary (allowed when primary done) | Reason |
|---------|--------------------|---------------------------------------|--------|
| 7001 | 16" | 14", 15" (same-size CO = 20 min) | Goes idle after 16" demand runs out without soft lock |
| 7003 | 15" | 14", 16" (same-size CO = 20 min) | Goes idle after 15" demand runs out without soft lock |

**Why not hard-lock 7001/7003:** 16" demand = ~57k total, 7001 capacity ≈ 17k units/month.
After 16" demand exhausted, hard lock forces 7001 idle. Soft lock lets it serve 14"/15" at 0 CO penalty.

### Tier 2: Dominant-inch preference (`_MACHINE_DOMINANT_INCH` in `b2c_pipeline.py`)

Sort CO candidates with `inch_penalty = 0` for dominant inch, `1` for other.
Sort key: `(−deficit, inch_penalty, revisit_penalty, co_cost)`.

**SKU inch derivation:** if missing from size master, inch = `sku_code[8:10]`.
E.g. `"1325216814085SURL0"[8:10] = "14"`.

**DB state (updated):**
- `1325215513073TUHL0` (13"): inserted with machines 7501, 7502, 7503
- `1325216814085SURL0` (14"): already in DB; was broken only because inch was unknown — now derived from SKU code
- `1325218415084TTMX0` (15"): inserted with all 8 VMIMAXX machines

---

## CO target urgency score — two-level priority

```
n     = current Running_Press_Count[T]
rate  = Qty_Per_Press_Per_Shift[T] × 3  (per-day production rate)
rem   = Updated_Demand_Qty[T]
H     = planning_days − current_day

Class A (CRITICAL): current_days > H × CO_CLASS_B_THRESHOLD → CO fires
Class B (HELPFUL):  below threshold → normally skipped

Exception — demand_done_free presses:
  When a Runner-In press's demand hits 0 mid-horizon, it is added to `demand_done_free`.
  These presses bypass the Class A gate and can CO to ANY target (Class A or B).
  Guard: Class B target requires gt_inventory[target] ≥ _cure_qty_per_shift(ct)
         to avoid starvation on the next shift.

Sort key: (urgency_class ASC, after_days ASC, −Priority_Score, −gt_signal, sku)
```

CO fires instantly when Runner-In demand is fulfilled; counts toward `MAX_CHANGEOVERS_PER_DAY`.

---

## Core scheduling tensions

**Tension 1 — Idle vs CO:** Every CO on a building machine costs production time. VMI same-size CO = 20 min (4.2%). Stage-1/Unistage diff-size CO = 180 min (37.5%). Never go idle if a genuine NRI SKU has spare curing capacity and CO cost is low relative to remaining shift. Accept idle when demand is fulfilled or remaining time < CO cost.

**Tension 2 — Low utilisation + unfulfilled demand (5 root causes):**
- **Case A:** Machine pool missing the right SKU → check `Master_Building_Allowable_Machines` (renamed from `Master_Building_Allowable_Machines_source`; schema is now a single comma-separated `Machines` column)
- **Case B:** CO budget starved (7001–7004 pattern) → campaign consolidation
- **Case C:** LP cap collapse Day 2+ → `OVERBUILD_BUFFER_FRAC = 0.2` prevents this
- **Case D:** NRI CO deferred past horizon → allow earlier CO scheduling
- **Case E:** Stage-1 structural under-utilisation < 33% — by design, not a bug

**Tension 3 — Demand cap preventing LP:** When `_gt_remaining[SKU]` → 0, LP idles machine. Fix order: (1) TopUp to NRI SKU, (2) pay CO if time allows, (3) accept idle. Never overbuild.

---

## Key config parameters (what's tunable)

**All parameters live in `bc_config.py` — single source of truth.**

| Parameter | Current value | What it controls |
|-----------|--------------|-----------------|
| `MIN_CAMPAIGN_MINS` | **60 min** | Shortest allowed production run. Was 120 — blocked ≤2 press SKUs. |
| `MIN_CAMPAIGN_UNITS` | 40 | Minimum units per campaign. |
| `OVERBUILD_BUFFER_FRAC` | 0.2 | LP headroom above net demand per day (prevents cap collapse). |
| `TOPUP_LOOKAHEAD_DAYS_GT` | **3** | How many days ahead TopUp pre-builds GT. Must equal `GT_SHELF_LIFE_DAYS = 3`. |
| `MAX_CHANGEOVERS_PER_DAY` | **14** | Curing CO cap per calendar day. Single source of truth in `bc_config.py`. Tuning history (from inline comments in `bc_config.py`): 8/day → ~594k GT baseline; 10/day → ~615k (balanced NRI activation); 14/day → ~650k target (activates more NRI COs — TTMX0/MSXT0/TUHL0/HURL0 gain ~20k units; also routes more Runner-Out presses to FXPC0). |
| `CO_CLASS_B_THRESHOLD` | **0.8** | CO fires if `current_days > H × 0.8`. Lower = more COs scheduled. |
| `GT_BUFFER_SHIFTS` | **2** | Rolling pipeline: shifts of GT to pre-build as buffer. VMI uses 2; BJ/UNI/STAGE use 1. |
| `MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT` | **2** | Cap on changeovers a single building machine may perform in one shift (rolling pipeline only). Plant averages 0.57 CO/shift/machine; upper bound of 2 lets one machine serve up to 3 SKU campaigns/shift. Must be `same_size_CO` to hold the 80% utilisation floor — 2× `diff_size_CO` (240 min VMI) would blow past it and is blocked. |
| `MIN_SHIFT_UTILISATION` | **0.80** | Target: each building machine should hit ≥80% production time per shift (384 of 480 min). **Defined in `bc_config.py` but not currently imported/referenced by `b2c_pipeline.py`, `building_b2c.py`, or `building.py`** — grep confirms no other file reads this constant. Treat it as an aspirational target documented in `bc.md` §19, not an enforced guard, until it is wired into the rolling-pipeline code. |
| `CURING_CO_DURATION_SHIFTS` | **1** | Shifts a curing press is idle during CO: Shift A only. Mould-clean removed from model. |
| `CURING_CO_CHANGEOVER_MINS` | **490** | Shift A duration for curing CO (full shift occupied). |
| `PRE_START_SHIFTS` | **2** | Building pre-starts N shifts before plan_start. |
| `GT_SHELF_LIFE_DAYS` | 3 | GT cannot sit >3 days before curing. Must equal `TOPUP_LOOKAHEAD_DAYS_GT`. |
| `CARCASS_SHELF_LIFE_DAYS` | 1 | Stage-1 carcass shelf life: 1 day. |
| `Stage-2 CO time multiplier` | **2.0×** | Stage-2 `co_time_map` uses `diff × 2.0` (88 → 176 min) to discourage LP from overloading Stage-2. |

### Round-trip buffer sizing (scheduling-logic mechanism, not a new config constant)

Live in `_assign_building_shift` (`b2c_pipeline.py`, function starts ~line 274), gated by the
module-level flag `_ROUND_TRIP_BUFFER_ENABLED = True` (~line 99). Reuses existing constants
(`MIN_CAMPAIGN_MINS`, `SHIFT_MINS`, the CO-cost tables) — no dedicated `bc_config.py` parameter.

Previously the buffer target for how far a machine builds ahead of curing consumption was a
flat multiplier: `_buf = GT_BUFFER_SHIFTS` (2) for VMI machines, `1` for everyone else. Now, each
shift, before Campaign 1 runs, the machine computes an `effective_buf` for its current SKU:

- **Skip conditions (fall back to flat `_buf`):** machine has only one eligible SKU
  (`len(eligible) <= 1`); no other eligible SKU has unmet demand
  (`demand_remaining[s] <= 0`); or no other eligible SKU has a real current deficit
  (`_deficit(s) <= 0`).
- **When a genuine rotation partner exists** (another eligible SKU with unmet demand and a real
  deficit, picked by `max(_deficit)`), the buffer is sized to survive: `CO(cur→partner)` +
  partner's own deficit-driven dwell time (floored at `MIN_CAMPAIGN_MINS`) + `CO(partner→cur)`,
  divided by `SHIFT_MINS`.
- `effective_buf = max(flat_buf, round_trip_buf)` — it only ever **widens** the buffer, never
  shrinks it below the old flat behavior.

Intent: if a machine is about to CO away to serve another live SKU and come back later, the
current SKU's press shouldn't starve while the machine is away. Applies to all machine groups
(VMI, BJ, Unistage, Stage-2), not just VMI.

---

## Pipeline execution order

> **ROLLING PIPELINE IS THE DEFAULT** (`python b2c_pipeline.py`). Legacy available via `--legacy`.

### Rolling pipeline (DEFAULT)

Per-shift loop — building assignment runs once per shift (A, B, C), reacting to
that shift's actual curing press state. Curing and building simulated simultaneously shift-by-shift.

```
Pre-computation: CO schedule, allow map (_HARD filter), CT map, press state, GT inventory, demand

for Day D in 1..31:
  co_target_skus = {new_sku for each curing CO on Day D}

  for Shift S in [A, B, C]:

    Step 1 — Per-shift curing demand
      shift_cure_demand[sku] = presses in RUNNING state × capacity
      Pre-build signal (Shift A ONLY): shift_cure_demand[new_sku] += _cure_qty_per_shift

    Step 2 — Greedy building assignment (_assign_building_shift)
      For each machine M (VMI first, then BJ, Unistage, Stage2):
        Round-trip buffer sizing: widen the buffer for cur_sku if a genuine
          rotation partner (other eligible SKU, real deficit + unmet demand)
          exists — see "Round-trip buffer sizing" under Key config parameters
        Campaign 1: serve current SKU (no CO)
        Campaign 2+: CO to deficit SKUs any shift (A/B/C), same-inch first (dominant inch preferred)
        guard: CO_cost ≤ 30% remaining, max 2 COs/shift
          urgent_co_set = co_target_skus eligible on this machine with gt_inventory=0 AND demand>0
          (urgent targets bypass the 30% cost guard)
        7001/7003 soft-lock: Campaign 2+ non-dominant inch only when primary_demand_done=True

    Step 3 — Add GT to inventory; record building rows
      gt_inventory[sku] += qty_built  (Stage-1 machines excluded — carcass ≠ GT)
      machine_current_sku[machine] = last_sku_in_shift  ← updated per-SHIFT

    Step 3b — Stage-1 carcass scheduling
      For each Stage-2 SKU built this shift:
        assign carcass production to eligible Stage-1 machines (proportional to capacity)
        record in bld_shift_rows with CO_Type="carcass" for utilization tracking
        does NOT affect gt_inventory

    Step 4 — Curing simulation
      for each press:
        RUNNING:    cured = min(capacity, gt_available, demand_left)
        CHANGEOVER: idle (Shift A of CO day only; Shift B = RUNNING)

  End of day: apply CO transitions (press_state updated after all 3 shifts)
```

**Stage-1 tracking (implemented):** `s1_sku_to_machines` dict (SKU → eligible S1 machines) is
built during allowable loading. Step 3b uses this to simulate carcass supply proportional to
Stage-2 output. Stage-1 machines now show real utilization in the Machine Utilization sheet.
Machines not certified for any current-demand Stage-2 SKU show 0% — correct behaviour.

---

## Known issues (current state)

| Issue | Root cause | Status |
|-------|-----------|--------|
| Stage-1 util 0% in output | Stage-1 excluded from GT scheduling | **Fixed:** Step 3b added — carcass production simulated per shift; real util shows in output |
| 1325217613082TUNE0 (13") gap | 7106 (dom=13") was serving 15" truck SKUs due to high deficit | **Fixed:** BJ hard-inch filters in `_HARD` dict — 7106/7103/7105 restricted to 13" only |
| HURL0 (1325218614088HURL0) zero production | 7104 missing from DB allowable for HURL0 | **Fixed and confirmed:** 7104 added to `Master_Building_Allowable_Machines` (table renamed from `Master_Building_Allowable_Machines_source`, schema changed to a comma-string `Machines` column — commit `63d193f`). Jul 10 2026 output shows this SKU FULLY MET (5,995 built / 5,876 demand = 102%). |
| TVECE (1325218614088TVECE) partial production | BJ structural oversubscription (249k demand / 184k capacity = 136%) | **Improved — confirmed FULLY MET in the Jul 10 2026 output** (both TVECE SKU variants, 96.5% and 95.9% of demand). BJ is still structurally oversubscribed plant-wide (see BJ 20k gap row below); this particular SKU is no longer the visible symptom. |
| Starvation events (~3,033) | BJ oversubscription — building cannot supply all BJ presses at full rate | **Not currently tracked in rolling-pipeline output.** No "starvation" sheet/column exists in `bc_curing_b2c.xlsx` or `bc_building_schedule_*.xlsx` as of Jul 10 2026 — this number is from a legacy run and cannot be reconfirmed without new instrumentation. Architecturally, curing is derived from building output (zero starvation by construction), so this figure describes the pre-derivation legacy path, not the current rolling pipeline. |
| VMIMAXX 27k gap | CO overhead + idle tail (scheduling recoverable ~10–15k) | Structural ceiling at 88.6%; VMIMAXX is undersubscribed at 67% capacity. **Not reconfirmed against the Jul 10 2026 run** — the building-CT correction (commit `f86f70b`) changes VMIMAXX per-machine throughput assumptions; this analysis predates that fix. |
| BJ 20k gap | Structurally oversubscribed | **Still valid, dominant SKU changed.** In the Jul 10 2026 output the largest PARTIAL-status gap is `1D25212812086FXPC0` (Runner-In, demand 21,217, built 1,280, gap 19,937 — curing-throughput limited). `1225121715115SSTL0`'s gap has shrunk to 3,032 (2,517 of 5,549 built, 55% fulfilled) — still curing-limited but no longer the largest contributor. **Root cause is curing presses, not building.** Fix = curing CO to add presses. |
| ~59k unmet demand (7 SKUs) | No allowable building machine in master data | **Resolved as of Jul 10 2026** — confirmed via `bc_building_schedule_2026-05-01.xlsx` (Demand Fulfillment (B2C) sheet): 0 of 97 SKUs have `Eligible_Machines == 0`. Table renamed `Master_Building_Allowable_Machines_source` → `Master_Building_Allowable_Machines`. Remaining unmet demand (8 UNMET SKUs, 3,687 units total) all have eligible building machines — the gap is now production-days/curing-throughput limited, not a missing-data problem. |
| Curing press IDs short by 30 | `_load_press_state` used `wcID` instead of `WCNAME_clean` | **Fixed:** curing_b2c.py uses WCNAME_clean (e.g. "75206") |

---

## Relevant source files

| File | Role |
|------|------|
| [bc_config.py](bc_config.py) | **SINGLE SOURCE OF TRUTH for ALL parameters**. Edit only this file. |
| [b2c_pipeline.py](b2c_pipeline.py) | **CORRECT ENTRY POINT** — `python b2c_pipeline.py`. Runs curing_consumption_dynamic → building_b2c → curing_b2c. |
| [building_b2c.py](building_b2c.py) | B2C building scheduler — Phase 1a/1b/2a/2b/3. |
| [curing_b2c.py](curing_b2c.py) | B2C curing simulation (Phase 4) — GT-balance shift-by-shift. Press IDs = `WCNAME_clean`. |
| [curing_consumption.py](curing_consumption.py) | Phase 0 — Day 0 snapshot. Reads from `testing_Daily_Running_Moulds`. |
| [curing_consumption_dynamic.py](curing_consumption_dynamic.py) | Phase 0 Extended — 31-day CO schedule. Class A + demand-done Class B, cap = `MAX_CHANGEOVERS_PER_DAY`. |
| [building.py](building.py) | Base building machinery (LP engine + DemandHeuristicAssigner). |
| [approach/bc.md](approach/bc.md) | Full B2C architecture spec (authoritative). |

---

## Known Calculation Pitfalls

### Ratio / coverage metrics — universe must match on both sides

```
fulfilled = total_demand - demand_remaining
```
Only correct when `total_demand` and `demand_remaining` cover the **same SKU universe**.
Excluded SKUs in `total_demand` but absent from `demand_remaining` silently inflate "fulfilled".

**Rule:** `set(SKUs in numerator) == set(SKUs in denominator)` before writing any KPI.

### Press ID format — WCNAME_clean, not wcID

Press key = `WCNAME_clean` everywhere. Never use `wcID` from `Master_WC_Master` — 30 of
167 presses have no WC Master entry (NaN wcID) and would be silently dropped.

### CO over-aggressiveness — RI presses CO'd before demand fulfilled

Guard implemented in `curing_consumption_dynamic.py` main CO loop (~line 339):
```python
if rem_old / (n_remaining * rate_old) > horizon_left:
    continue  # remaining presses cannot cover old_sku demand
```

---

## Framing for the agent

You are a **scheduling logic advisor** for a tyre manufacturing planning system.
When answering "should we / what if / what's wrong" questions:

1. **Which invariant does it touch?** (Demand cap, CO limit, Stage-1/2 dependency, no waste GT)
2. **Which SKU category is affected?** (RI / RO / NRI)
3. **Which machines are involved?** (Stage-1 always under-utilised by design; 7001–7004 have structural CO problem)
4. **Is this a config, logic, or data change?**
5. **Is it a trade-off or strict improvement?** Most "never go idle" changes are trade-offs.
6. When user says "low utilisation + unmet demand" — diagnose Case A–E before prescribing fix.

---

## Current KPIs (confirmed Jul 12 2026 — round-trip buffer sizing live, `python b2c_pipeline.py`)

Output history across recent commits/sessions (GT built/cured):
609k/611k → 620k → 623k → 618k (after removing the allowable-matrix history union — a
deliberate correctness trade-off, see Known Issues) → 647,521/648,992 (`f86f70b`, real
per-machine `_BLD_CT_SEC` building cycle times) → **654,572/655,845** (round-trip buffer
sizing added to `_assign_building_shift`; running-machine seeding, scarcity-first machine
ordering, and demand-ratio candidate ranking were all tried this session and explicitly
reverted — only round-trip buffer sizing is live).

| KPI | Value | Confirmed how |
|-----|-------|----------------|
| Total demand (demand_may.xlsx) | 693,748 | Matches across all recent runs |
| GT built | **654,572** | Console output of `python b2c_pipeline.py`, "ROLLING PIPELINE — Results" ("Total GT built"), re-run and confirmed Jul 12 2026 |
| GT cured (total units) | **655,845** | Same console output ("Total cured") |
| Demand coverage | **94.5%** | 655,845 / 693,748, printed directly by the pipeline |
| Curing COs scheduled | 98 | Console output ("Curing COs scheduled") |
| GT written off | **3,440** | Console output ("GT written off") — cumulative `_writeoff_stale_gt` over the 31-day run |
| Starvation events | **1,908** | Console output ("Starvation events") — a press RUNNING with insufficient GT that shift; not necessarily zero because the rolling loop can still schedule a press to run before that shift's GT lands |

**Structural ceilings (cannot fix via scheduling alone):**
- BJ: still structurally oversubscribed (249k demand vs ~184k capacity)
- No-machine-data SKUs: resolved — 0 of 97 SKUs show `Eligible_Machines == 0`
- Curing-limited RI SKUs: building correct, need more curing presses (worst remaining: `1D25212812086FXPC0` at 9,562 units, per the Jul 12 2026 run's "Worst 10 SKUs by remaining demand")

**Scheduler ceiling: confirmed ~654–656k as of Jul 12 2026** (superseding the earlier "~647–649k" figure, which predates round-trip buffer sizing). Re-run `python b2c_pipeline.py` for an exact up-to-the-minute figure if precision matters — starvation/writeoff counts can vary slightly run-to-run.

**Known calculation pitfall fixed (Jul 2026):**
- CT bug: `cure_ct_map` was always empty due to wrong column key (`"CT_Min"` vs `"CycleTime_min"`).
  All SKUs fell back to `DEFAULT_CURING_CT = 17.0`. Fixed in `b2c_pipeline.py` line ~1132.
- Building CT: `_BLD_CT_SEC` (per-machine seconds/unit) corrected against plant production norms
  in commit `f86f70b` — every value in the dict changed (e.g. `6801` 150→127, `8101` 300→230,
  `7101` 102.0→83.0).
