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
- **Case A:** Machine pool missing the right SKU → check `Master_Building_Allowable_Machines_source`
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
| `MAX_CHANGEOVERS_PER_DAY` | **14** | Curing CO cap per calendar day. Single source of truth in `bc_config.py`. |
| `CO_CLASS_B_THRESHOLD` | **0.8** | CO fires if `current_days > H × 0.8`. Lower = more COs scheduled. |
| `GT_BUFFER_SHIFTS` | **2** | Rolling pipeline: shifts of GT to pre-build as buffer. VMI uses 2; BJ/UNI/STAGE use 1. |
| `CURING_CO_DURATION_SHIFTS` | **1** | Shifts a curing press is idle during CO: Shift A only. Mould-clean removed from model. |
| `CURING_CO_CHANGEOVER_MINS` | **490** | Shift A duration for curing CO (full shift occupied). |
| `PRE_START_SHIFTS` | **2** | Building pre-starts N shifts before plan_start. |
| `GT_SHELF_LIFE_DAYS` | 3 | GT cannot sit >3 days before curing. Must equal `TOPUP_LOOKAHEAD_DAYS_GT`. |
| `CARCASS_SHELF_LIFE_DAYS` | 1 | Stage-1 carcass shelf life: 1 day. |
| `Stage-2 CO time multiplier` | **2.0×** | Stage-2 `co_time_map` uses `diff × 2.0` (88 → 176 min) to discourage LP from overloading Stage-2. |

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
| HURL0 (1325218614088HURL0) zero production | 7104 missing from DB allowable for HURL0 | **Data fix needed:** add 7104 to `Master_Building_Allowable_Machines_source` for HURL0 |
| TVECE (1325218614088TVECE) partial production | BJ structural oversubscription (249k demand / 184k capacity = 136%) | Structural — needs more BJ capacity or VMI certification |
| Starvation events (~3,033) | BJ oversubscription — building cannot supply all BJ presses at full rate | Pre-existing structural; not caused by scheduling changes |
| VMIMAXX 27k gap | CO overhead + idle tail (scheduling recoverable ~10–15k) | Structural ceiling at 88.6%; VMIMAXX is undersubscribed at 67% capacity |
| BJ 20k gap | Structurally oversubscribed; 1225221715115SSTL0 capped at curing throughput | **Root cause is curing presses, not building.** Fix = curing CO to add presses |
| ~59k unmet demand (7 SKUs) | No allowable building machine in master data | Permanently unbuilt until `Master_Building_Allowable_Machines_source` updated |
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

## Current KPIs (May 2026, Jul 2026 run — after soft-lock + allow_new_co removed)

| KPI | Value |
|-----|-------|
| Total demand (demand_may.xlsx) | 693,748 |
| GT built | 621,507 |
| GT cured | 623,230 |
| Demand coverage | 89.8% |
| Curing COs scheduled | 98 |
| GT written off | 2,616 |
| Starvation events | 2,857 |

**Structural ceilings (cannot fix via scheduling alone):**
- BJ: 249k demand / ~184k capacity = 136% oversubscribed → ~20k permanently unmet
- No-machine-data SKUs: ~59k unbuilt → needs master data certification
- Curing-limited RI SKUs: building correct, need more curing presses

**Scheduler ceiling (with HURL0 data fix + allowable master updates): ~620–625k**

**Known calculation pitfall fixed (Jul 2026):**
- CT bug: `cure_ct_map` was always empty due to wrong column key (`"CT_Min"` vs `"CycleTime_min"`).
  All SKUs fell back to `DEFAULT_CURING_CT = 17.0`. Fixed in `b2c_pipeline.py` line ~1132.
