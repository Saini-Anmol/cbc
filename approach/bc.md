# B2C Scheduler — Detailed Technical Architecture

**Building-to-Curing (B2C)**: the building schedule is the **primary output**;
the curing schedule is **fully derived** from it. Direction is the reverse of C2B.

> **Hard constraints (two, both identical in structure):**
> 1. Stage-1 carcass must be available **at shift S** when Stage-2 runs (earlier is fine; same shift is the minimum). Stage-2 cannot start before Stage-1 output is ready.
> 2. GT must be available **at shift S** when Curing consumes it (earlier is fine; same shift is the minimum).
>
> In both cases S-1 availability is preferred and enables zero-wait production, but S is the hard floor.
>
> **Objective:** Maximise building production. Curing consumes exactly what building produces.

---

## 1. Why B2C — Motivation vs C2B

| Dimension | C2B (old) | B2C (new) |
|-----------|-----------|-----------|
| Primary driver | Curing LP allocates press-minutes | Building throughput ceiling |
| Scheduling order | Curing first → building follows | Building first → curing derived |
| Bottleneck | Building couldn't feed curing plan | Eliminated — curing is sized to building |
| GT flow | Building tries to satisfy curing LP | Curing consumes exactly what building produces |
| Starvation risk | Frequent (root cause of redesign) | Zero by design |
| Waste GT | Possible (building over-runs) | Zero — building output capped to consumption |

---

## 2. Start Conditions

### 2.1 Opening GT Inventory (from DB — not zero)

GT inventory is loaded from `gt_inventory_manual` at plan start. This is **not** a cold start for curing.

```
GT_inventory(SKU, start)      = gt_inventory_manual.gtInventory[SKU]   (from DB)
Carcass_inventory(SKU, start) = 0   (B2C cold start — Stage-2 waits for Stage-1 output)
```

**Carcass cold start rationale:** Stage-2 GT machines depend on Stage-1 carcass output.
Opening carcass inventory is assumed **zero**. Stage-2 is blocked until Stage-1 produces
in the same or earlier shift. Unistage machines are unaffected (they produce GT directly
with no Stage-1 dependency).

Opening GT inventory acts as a Day-1 buffer: curing can start immediately on
June 1 Shift A using existing stock, even before the building pre-shift completes.
It does not change the steady-state daily/monthly production rate — it is a
one-time boost to early demand fulfillment.

### 2.2 Building Pre-Start — One Full Day Before Curing

Building starts **1 full day (3 shifts = `BUILD_LEAD_SHIFTS`) before** curing consumption begins.

```
Example: Curing starts  June 1  Shift A (07:00)
         Building starts May 31 Shift A (07:00)  ← 3 shifts = 1 full day prior
```

**Why 1 day ahead (`BUILD_LEAD_SHIFTS = 3`)**:
- LP cap: `cap = max(0, gross_curing − opening_WIP)`. In same-day mode Day 2+ WIP ≈ Day 2 demand → cap ≈ 0 → machines idle.
- Lead-time fix: each day's LP targets the NEXT day's curing demand. WIP is net-adjusted by subtracting today's curing (`gt_inv_for_cap = wip0 − today_cure`) before comparing to tomorrow's gross demand. LP sees full tomorrow demand every day.

```
May 31 (Shifts A→C): Building targets June 1 curing demand   ← 3-shift pre-start
June 1 (Shifts A→C): Building targets June 2 curing demand
June 2 (Shifts A→C): Building targets June 3 curing demand
...
Last planning day: no Day+1 demand → falls back to today's demand automatically.
```

- Machine running state is taken from `Daily_Running_Moulds` as of the **curing start date** (June 1 snapshot).
- Opening GT inventory supplements May 31 output for early June 1 curing shifts.
- Starvation validation and inventory roll always use **today's** actual curing demand, not the lead window.

---

## 3. Resource "FREE" Definition

> **Applies everywhere in the pipeline — Phase 1b, Phase 2, Phase 3.
> A resource is either FREE or OCCUPIED. No partial states.**

### 3.1 Curing Press — FREE in shift S means ALL of:
```
✗ NOT running any SKU (no active production)
✗ NOT in changeover window  — 300 min, press OCCUPIED the entire shift it falls in
✗ NOT in mould clean window — 120 min, press OCCUPIED the entire shift it falls in
```

Changeover always takes Shift A (of the assigned day). Mould clean takes Shift B.
First production of new SKU = Shift C onward.

> **Curing changeover cap: MAX 8 curing press changeovers per day (plant-wide hard limit).**
> Building machine changeovers are UNLIMITED — no daily cap applies.

### 3.2 Building Machine — FREE in shift S means ALL of:
```
✗ NOT committed to any SKU production assignment
✗ NOT in a building changeover window (building COs are UNLIMITED per day)

Spare-minutes check — a machine with leftover minutes is only FREE for a new SKU if:
  usable_spare = shift_remaining_min - changeover_cost(new_SKU)
  if usable_spare < build_cycle_time(new_SKU) → machine is OCCUPIED
  else                                         → machine is FREE for new_SKU
```

---

## 4. System Overview

```
 ┌──────────────────────────── B2C PIPELINE ─────────────────────────────┐
 │  INPUTS                                                                 │
 │  ├─ Demand file            (SKUs, Qty, ConsolidatedPriorityScore)       │
 │  ├─ Daily Running Moulds   (which curing press runs which SKU today)    │
 │  ├─ Master_Curing_Design_CycleTime  (raw cure time per SKU from DB)     │
 │  ├─ Master_Curing_Allowable_Machines_source  (SKU ↔ curing press)       │
 │  ├─ Master_Building_Allowable_Machines_source (SKU ↔ building machine)  │
 │  ├─ Building_Stage1/2_Best_Machines  (building history — fallback)      │
 │  ├─ Master_Building_ChangeoverTime  (building machine CO times)          │
 │  └─ feed_map.json          (curing press → feeder building machines)    │
 │                                                                         │
 │  PHASE 0 ── Curing Consumption Table                                    │
 │             ① SKU Eligibility Filter (see §4.1)                        │
 │             ② Classify SKUs · compute effective CT · GT/shift per press │
 │             ③ Initialise active_press_count(SKU, shift) for horizon    │
 │             Output: curing_consumption_table.xlsx                       │
 │                       (includes "Excluded SKUs" sheet if any)          │
 │                                 ↓                                       │
 │  PHASE 1 ── Building Schedule  [current pipeline stop point]            │
 │             Phase 1a: Runner-In (Stage-1/Stage-2/Unistage)             │
 │             Phase 1b + 2a: Joint Priority Pool (NRI + RO CO)           │
 │             Phase 2b: Changeover scheduling (max 8/day)                │
 │             Phase 3: Dynamic target lock                                │
 │             Output: bc_building_schedule.xlsx                           │
 │                                 ↓                                       │
 │  [DEFERRED] PHASE 2 ── Curing Schedule Derivation                      │
 │             100% deterministic from building GT output                  │
 │             Approach to be decided after reviewing building schedule    │
 │                                 ↓                                       │
 │  [DEFERRED] PHASE 3 ── Analysis & KPI Report                           │
 └─────────────────────────────────────────────────────────────────────────┘
```

### 4.1 SKU Eligibility Filter (Phase 0 — before classification)

Every demand SKU is checked against two source pools before entering planning.
SKUs that fail are **excluded entirely** and shown in the "Excluded SKUs" sheet.

```
Building pool = Master_Building_Allowable_Machines_source
              ∪ Building_Stage1_Best_Machines
              ∪ Building_Stage2_Best_Machines

Curing pool   = Master_Curing_Allowable_Machines_source
              ∪ Daily_Running_Moulds  (historical curing press data)

SKU is ELIGIBLE  : SKU ∈ Building_pool  AND  SKU ∈ Curing_pool
SKU is EXCLUDED  : fails either check
```

**Remark format for excluded SKUs** (written to "Excluded SKUs" sheet):
```
"No PDE & master data- building machine"        ← missing only from building sources
"No PDE & master data- curing mould"            ← missing only from curing sources
"No PDE & master data- building machine, curing mould"  ← missing from both
```

**Cycle time (CT) is NOT an exclusion criterion.** If a SKU is eligible but
its CT is absent from `Master_Curing_Design_CycleTime`:
```
effective_CT = DEFAULT_CYCLE_TIME_MIN = 17.0 min  (already effective; no formula applied)
```
This default silently applies — no remark, SKU proceeds to planning normally.

---

## 5. Data Sources

### 5.1 Database — MySQL `jkplanningV1`

| Table | Key Columns | Used In | Purpose |
|-------|-------------|---------|---------|
| `Daily_Running_Moulds` | `WCNAME`, `Sapcode`, `Current MouldNo`, `Mould life` | Phase 0, 1a, 2a | Which SKU each press runs today; mould life remaining; curing history fallback |
| `gt_inventory_manual` | `sizeCode`, `gtInventory` | Phase 0, 4 | Opening GT inventory per SKU at plan start |
| `Master_Curing_Design_CycleTime` | `Sapcode`, `Cure Time` | Phase 0 | Raw cure time per SKU; missing → default 17.0 min |
| `Master_Curing_Allowable_Machines_source` | `SKU Code`, machine cols (`Yes`) | Phase 0 (eligibility), 1b, 2a | Which presses a SKU is allowed to run on |
| `Master_Building_Allowable_Machines_source` | `SKU Code`, machine cols | Phase 0 (eligibility), 1a, 1b, 2a | Which building machines can make each SKU |
| `Building_Stage1_Best_Machines` | `MachineNo`, `sizeCode`, `count` | Phase 0 (eligibility fallback) | 3-month Stage-1 building history; used when master data absent |
| `Building_Stage2_Best_Machines` | `MachineNo`, `sizeCode`, `count` | Phase 0 (eligibility fallback) | 3-month Stage-2/Unistage building history; used when master data absent |
| `Master_Building_ChangeoverTime` | `MachineCode`, `Same Size(min)`, `Diff Size(min)` | Phase 1a, 1b | Building changeover cost |
| `Master_WC_Master` | `wcID`, `WCNAME` | Phase 0 | Press code normalisation |
| `Master_Mapping_Mould_SKU` | `MouldNo`, `Matl.Code`, `Active Flag` | Phase 2a | Mould ↔ SKU compatibility for changeover target check |
| `TBMStage1_ProductionEventData` | `WorkCenter`, `RecipeCode`, `DtAndTime` | Phase 1a | Currently running Stage-1 machines |
| `TBMStage2_ProductionEventData` | `WorkCenter`, `RecipeCode`, `DtAndTime` | Phase 1a | Currently running Stage-2 / Unistage machines |

### 5.2 File Inputs

| File | Used In | Key Columns | Purpose |
|------|---------|-------------|---------|
| Demand file (`*.xlsx`) | All phases | `SKUCode`, `Updated_Requirement` / `Requirement`, `ConsolidatedPriorityScore` | Demand + priority |
| `feed_map.json` | Phase 1b, 2a | `press → [machines]` | Feeder building machine mapping (pre-built) |

> `ConsolidatedPriorityScore` is a static input from the demand file. Higher = higher priority. Not computed in this pipeline.

### 5.3 Outputs

| File | Phase | Contents |
|------|-------|----------|
| `curing_consumption_table.xlsx` | Phase 0 | Per-SKU: category, press count, effective CT, GT/shift |
| `bc_building.xlsx` | Phase 1a/1b | Per building machine, per shift: SKU, qty, status, stage |
| `curing_changeover_plan.xlsx` | Phase 2a/2b | Per press: from SKU → to SKU, changeover shift |
| `bc_curing.xlsx` | Phase 4 | Per curing press, per shift: SKU, qty, status |
| `b2c_analysis.xlsx` | Phase 5 | Utilisation, GT alignment, starvation, KPIs |

---

## 6. Phase 0 — Curing Consumption Table

### 6.1 SKU Universe Classification

```
Demand file (120 SKUs)
├── Currently on a curing press (Daily_Running_Moulds)  → 80 SKUs
│   ├── IN demand   → "Runner-In"     (65 SKUs)  ← primary building load
│   └── NOT demand  → "Runner-Out"    (15 SKUs)  ← changeover candidates
└── Not on any curing press
    └── IN demand   → "Non-Runner-In" (55 SKUs)  ← secondary fill (Phase 1b)
```

> Counts are illustrative — computed dynamically each cycle.
> Presses mid-changeover at snapshot time are OCCUPIED and excluded from running_press_count.

### 6.2 Cycle Time Formula

```
From DB (Master_Curing_Design_CycleTime):
  effective_CT(SKU) = round( (raw_CT[SKU] + 2.3) / 0.94 )  minutes

SKU not in DB:
  effective_CT(SKU) = 17.0 minutes  (default; already effective — no further formula)

Where DB has multiple rows per SKU: match active mould from Master_Mapping_Mould_SKU.
If still ambiguous: use minimum CT (most conservative / highest consumption).
```

Constants: `LOAD_UNLOAD_BUFFER_MIN = 2.3`, `PRESS_EFFICIENCY = 0.94`, `DEFAULT_CT = 17.0`.

### 6.3 Consumption Formula

```
SHIFT_DURATION_MIN = 480           # 8 h; Shifts A(07:00) B(15:00) C(23:00)
CAVITIES_PER_MOULD = 2             # 2 tyres per curing cycle

qty_per_press_per_shift(SKU) = floor(480 / effective_CT(SKU)) × 2

Runner-In + Runner-Out:
  total_GT_per_shift(SKU) = qty_per_press_per_shift(SKU) × running_press_count(SKU)

Non-Runner-In:
  unit_GT_per_press(SKU) = qty_per_press_per_shift(SKU)  [multiplier = 0 now]
```

### 6.3a GT Building Target — Demand Cap Rule

> **Building output for any SKU must NEVER exceed customer demand.**

```
demand_per_shift(SKU) = Demand_Qty(SKU) / (planning_days × SHIFTS_PER_DAY)

building_target_per_shift(SKU) = min(
    total_GT_per_shift(SKU),   ← curing press consumption (physics ceiling)
    demand_per_shift(SKU)      ← customer demand (business cap)
)
```

When `demand_per_shift < total_GT_per_shift` (demand is met before month-end):
- Building stops for that SKU once cumulative output = `Demand_Qty`
- The freed curing press enters the changeover queue → reassigned to an NRI SKU

For Non-Runner-In SKUs: building target = `demand_per_shift` (same formula, press consumption = 0 initially).

### 6.3b NRI SKU Building Start

Non-Runner-In SKUs do **not** wait for a curing press changeover confirmation before building starts.
Building machines are assigned to NRI SKUs immediately (including Unistage machines 7001/7002/7003).
Curing press changeovers for NRI SKUs are planned in parallel and limited to 8/day.
GT built for NRI SKUs before their curing press CO will sit in GT inventory until the press is ready.

### 6.4 Dynamic Press Count Initialisation

```
active_press_count(SKU, shift = pre_shift_C) = running_press_count(SKU)
# pre_shift = May 31 Shift C (the single building pre-start shift)

This table is updated in Phase 3 as changeover events change press assignments.
All per-shift building targets and curing consumption use this table.
```

### 6.5 Consumption Table Schema

| Column | Source |
|--------|--------|
| `SKUCode` | demand file |
| `SKU_Description` | master |
| `Category` | computed: Runner-In / Runner-Out / Non-Runner-In |
| `Running_Press_Count` | `Daily_Running_Moulds` (0 for Non-Runner-In) |
| `Allowable_Moulds_Count` | `Master_Mapping_Mould_SKU` |
| `Effective_CT_Min` | §6.2 |
| `Qty_Per_Press_Per_Shift` | `floor(480/CT) × 2` |
| `Total_GT_Per_Shift_Day0` | `qty_per_press × running_press_count` |
| `Unit_GT_Per_Press` | Non-Runner-In sizing |
| `Demand_Qty` | demand file |
| `Priority_Score` | `ConsolidatedPriorityScore` |

---

## 7. Phase 1a — Runner-In Building (Stage-1 + Stage-2 + Unistage)

### 7.0 Machine Stages — Physical Setup (CONFIRMED)

Three building stage groups — 39 machines total.
**Source of truth:** column names of `Master_Building_Allowable_Machines_source` (39 numeric columns).

```
Stage-1  (Carcass machines — 15 machines)
  Machines: 6801, 6802, 6803, 6909, 6911,
            7601, 7701,
            7801, 7802, 7803, 7804,
            8001, 8002, 8003, 8101
  Output  : Carcass (semi-finished tyre body) → feeds Stage-2 GT machines
  Flow    : EVERY Stage-2 tyre also needs a Stage-1 carcass (two-step process)
  SKUs    : 53 demand SKUs use the Stage-2 path (require both Stage-1 AND Stage-2)
  Demand  : ~335,059 units

Stage-2  (GT-from-Carcass machines — 6 machines)
  Machines: 8201, 8301, 8302, 8501, 8502, 7301
  Output  : Completed GT tyre — takes Stage-1 carcass as input
  SKUs    : 53 demand SKUs (same set as Stage-1 above, two-step path)
  Demand  : ~335,059 GTs
  Bottleneck: Stage-2 is the constraint (6 machines vs 15 Stage-1 machines)

Unistage  (Independent GT machines — 18 machines)
  Machines: 6001, 6002, 6003, 6004,
            7001, 7002, 7003, 7004,
            7101, 7102, 7103, 7104, 7105, 7106,
            7201,
            7501, 7502, 7503
  Output  : Completed GT tyre — fully independent (no Stage-1 dependency)
  SKUs    : 94 demand SKUs
  Demand  : ~397,722 GTs
```

**Capacity note (building CT ≠ curing CT):**
```
No building cycle-time table exists in DB. Curing CT (avg ~17 min) is used as a proxy.
Actual building CT is much shorter (~2–3 min/tyre for PCR building machines).
All capacity figures below are APPROXIMATIONS.

Stage-2 capacity (6 machines, curing CT proxy @17 min):  6 × 90 × 56 = 30,240 GTs
Unistage capacity (18 machines, curing CT proxy @17 min): 18 × 90 × 56 = 90,720 GTs
```

**7001–7004 constraint (within Unistage):**
```
SKUs allowed on any of 7001–7004: 48 SKUs
Combined demand for those SKUs:   ~224,340 units
Physical capacity (building CT @2.5 min, 1 cavity): 4×90×192 = 69,120 units
Physical capacity (building CT @3.0 min, 1 cavity): 4×90×160 = 57,600 units

→ Even at real building CT, demand (224k) is 3–4× capacity (58k–69k).
  These 4 machines CANNOT satisfy all 224k demand in a 30-day plan.
  Actual production ~54k ≈ theoretical max → machines were nearly fully utilised.
  Root cause of low output: 48 SKUs on 4 machines → very short campaigns per SKU,
  excessive CO time, low effective utilisation per SKU.
  Fix: reduce distinct SKUs on 7001–7004, run longer campaigns.
```

### 7.1 The Two-Level Availability Chain

```
Hard constraint (minimum):
  Stage-1 carcass available at shift S  AND  Stage-2 runs in shift S
  Stage-2 GT available at shift S       AND  Curing runs in shift S
  (same shift = minimum; earlier = preferred)

Preferred (when achievable):
  Stage-1 produces in shift S-1 → Stage-2 runs in shift S  (zero-wait)
  Stage-2/Unistage produces in shift S-1 → Curing in shift S (zero-wait)
```

**Sequencing rule within a shift (when S-1 is not available):**
```
Stage-1 starts FIRST within shift S → produces carcass
Stage-2 starts AFTER Stage-1 has produced → uses same-shift carcass
Net GT from shift S is available for Curing in the SAME shift S (OR shift S+1)
```

Shift mapping for S-1 preferred case:
```
Consumer Shift A (Day N) ← Producer outputs in Shift C (Day N-1)
Consumer Shift B (Day N) ← Producer outputs in Shift A (Day N)
Consumer Shift C (Day N) ← Producer outputs in Shift B (Day N)
```

### 7.2 Stage-1 Carcass Assignment — Available at S or Earlier

**Capacity facts:**
```
Stage-1 capacity:   2,459 carcass/shift  (15 machines)
Stage-2 demand:     2,215 carcass/shift  (6 machines at full output)
Surplus:             +244/shift = 11% headroom  → no bottleneck, no sustained delay
```

**Pre-shift timeline (May 31 Shift C — the single building pre-shift):**
```
May 31 Shift C:  Stage-1 RUNS FIRST → 2,459 carcass produced
                 Stage-2 RUNS AFTER  → uses same-shift carcass → 2,215 GT
                 Unistage RUNS       → 7,306 GT  (unaffected, no S1 dependency)
                 ──────────────────────────────────────────────────────────
                 Total GT pre-built in 1 shift: 9,521 units
                 + Opening GT inventory from DB → buffers June 1 Shift A curing

June 1 Shift A onward (curing + building both run):
  Each shift: S1 starts first within the shift → S2 follows → GT available same shift
              or S1 built in S-1 → S2 uses it → GT available at S (preferred)
```

**Zero startup cost** (Stage-2 idle shift eliminated vs old S-1 rule).

**Resilience:** When S-1 pre-production is achieved (preferred), Stage-2 buffer exists
and absorbs a Stage-1 1-shift stoppage at 0 GT loss. When only same-shift S is achieved,
a Stage-1 stop costs 2,215 GT that shift — acceptable, logged as CARCASS_SHORTFALL.

```
For each Stage-2 machine assignment (SKU, S2_machine, shift S):
  Constraint: carcass must be available at shift S or earlier (any shift ≤ S).
  Stage-2 CANNOT begin until Stage-1 has produced carcass.

  If S1 produced carcass in shift S-1 → Stage-2 starts freely at S (preferred)
  If S1 produces carcass in shift S   → Stage-2 starts AFTER S1 within shift S (minimum)

  eligible_S1 = S1_allowable[SKU] ∩ currently_running_S1_machines[SKU]
  Assign S1 machine(s) for shift S (or S-1 when achievable).
  If no S1 machine available → remove S2 assignment; log CARCASS_SHORTFALL.

Unistage machines: no Stage-1 assignment. Produce GT directly from pre-shift onward.
```

### 7.3 Stage-2 + Unistage GT Assignment

For each Runner-In SKU (sorted by `Priority_Score DESC`):

```
1. eligible_machines(SKU) = building_allowable[SKU] ∩ currently_running_machines[SKU]
   (prefer already-running; avoids changeover cost)

2. output_per_machine_per_shift = floor(usable_min / machine_CT) × units_per_cycle
   (usable_min = 480 minus changeover cost if machine is switching SKU)

3. machines_needed = ceil(total_GT_per_shift(SKU) / output_per_machine_per_shift)

4. Assign up to machines_needed machines.
   If insufficient → log CAPACITY_SHORTFALL; produce at max available.

5. Deduct from available building capacity pool.
```

Building output cap per shift per SKU:
```
building_output_cap(SKU, shift) = total_GT_per_shift(SKU, shift)
                                  [no excess GT — strict cap]
```

---

## 8. Phase 2a — Runner-Out Changeover Eligibility

Runs after Phase 1a and jointly with Phase 1b (see §9 Joint Pool).

For each Runner-Out press P (running a non-demand SKU):

```
Step 1: candidate_targets(P) = curing_allowable[P] ∩ demand_SKUs

Step 2: For each target T:
          bld_free(T) = feed_map[P]
                        ∩ building_allowable[T]
                        ∩ FREE_building_machines  (§3.2, post-Phase-1a)
          if bld_free(T) ≠ ∅ → (P → T) is VIABLE

Step 3: No viable target → P is STRANDED_PRESS (IDLE all cycle); log it.

Step 4: Select target T* = highest Priority_Score among viable targets.

Step 5: Add (P → T*, priority = Priority_Score(T*)) to JOINT POOL (§9).
```

**Changeover scheduling:**
```
Sort JOINT POOL by: Priority_Score DESC, mould_life_remaining ASC
Assign greedily per day: up to 8 total changeovers/day (plant-wide hard limit)
Changeover always in Shift A of assigned day:
  Shift A → CHANGEOVER (300 min, press OCCUPIED whole shift)
  Shift B → MOULD_CLEAN (120 min, press OCCUPIED whole shift)
  Shift C → new SKU production begins

On assignment of (P → T*, Day D):
  active_press_count(T*, shift ≥ Day D Shift C) += 1
  active_press_count(old_SKU, shift ≥ Day D Shift A) -= 1
```

**Shared-feeder contention:** if two changeover candidates need the same building
machine M, the one with higher Priority_Score(target) gets M; loser deferred one day.

**Demand-satisfied transition:** when cumulative GT built ≥ demand_qty for a Runner-In SKU:
```
1. Cap building output to 0 for that SKU.
2. Freed press enters changeover queue with same §8 eligibility check.
3. If no viable target: press goes IDLE (prevents waste GT).
4. Update active_press_count(SKU, from this shift) -= 1.
```

---

## 9. Phase 1b + 2a — Joint Priority Pool

Phase 1b (Non-Runner-In building) and Phase 2a (Runner-Out changeovers) share
one priority-sorted pool. Both draw from the same residual building capacity.
This prevents either phase from claiming machines the other needs.

```
JOINT POOL = [
  Non-Runner-In SKU assignments  → priority = ConsolidatedPriorityScore(SKU)
  Runner-Out changeover targets  → priority = ConsolidatedPriorityScore(target_SKU)
]
Sort by priority DESC.

For each item:

  If Non-Runner-In SKU:
    free_bld(SKU) = building_allowable[SKU] ∩ FREE_building_machines (§3.2)
    free_cur(SKU) = curing_allowable[SKU]
                    MINUS presses running Runner-In SKUs
                    MINUS presses running Runner-Out SKUs
                    MINUS presses already claimed in this pass
                    [applying FREE from §3.1 — not in CO, not in mould clean]

    viable_bld = free_bld(SKU) ∩ inverse_feed_map(free_cur(SKU))
    viable_cur = {p ∈ free_cur | feed_map[p] ∩ free_bld(SKU) ≠ ∅}

    if viable_bld EMPTY or viable_cur EMPTY → DEFERRED; log reason; skip
    else:
      Assign 1 building machine (from viable_bld)
      Assign 1 curing press     (from viable_cur, highest priority match)
      Mark both OCCUPIED; add press to PENDING_CHANGEOVER for Phase 2b

  If Runner-Out changeover:
    Confirm building machine still FREE (not claimed by higher-priority item)
    If FREE  → commit changeover; mark building machine OCCUPIED
    If taken → re-check next-best target; if none → STRANDED_PRESS
```

---

## 10. Phase 2b — Pending Changeover Scheduling

Presses marked PENDING_CHANGEOVER (from Phase 1b) are scheduled alongside
Phase 2a changeovers, sharing the same MAX_CHANGEOVERS_PER_DAY = 8 budget.

```
Combined list = Phase 2a + Phase 2b candidates
Sort: Priority_Score DESC, mould_life_remaining ASC
Assign: up to 8/day, Shift A of each day

For each (P, T, Day D):
  active_press_count(T, shift ≥ Day D Shift C) += 1
  Building machine must produce for T from Day D Shift B onward
  (Stage-2/Unistage: S1 also assigned for Day D Shift A if T needs carcass)
```

---

## 11. Phase 3 — Dynamic Building Target Lock

After all changeover assignments, update `active_press_count(SKU, shift)` for the
full horizon and lock per-shift building targets:

```
For each (SKU, shift S):
  building_target(SKU, S) = qty_per_press_per_shift(SKU) × active_press_count(SKU, S)
  building_output_cap(SKU, S) = building_target(SKU, S)   ← strict cap

Machine idles once cap is reached — no excess GT produced.
```

This locked per-shift schedule is the final `bc_building.xlsx`.

---

## 12. Phase 4 — Curing Schedule Derivation (100% Deterministic)

### 12.1 GT Availability for Curing Shift S

GT is available for curing shift S from two sources combined:

```
GT_available(SKU, shift S) =
    GT_inventory_remaining(SKU, S)         ← rolling balance carried from prior shifts
  + building_output(SKU, shifts ≤ S)       ← same-shift S production counts (relaxed rule)

GT_inventory_remaining(SKU, shift 1) = gt_inventory_manual[SKU]  ← from DB at plan start

Rolling balance each shift:
  GT_inventory_remaining(SKU, S+1) =
      GT_inventory_remaining(SKU, S)
    + building_output(SKU, S)
    - GT_consumed_by_curing(SKU, S)

  Must be ≥ 0. If negative → STARVATION event.
```

**Preferred case (S-1 building — zero wait):**
```
GT from building shift S-1 is already in pool at START of shift S.
Curing starts immediately, no intra-shift waiting.
```

**Minimum case (S building — same shift):**
```
Building produces GT early in shift S → curing uses it later in shift S.
Intra-shift sequencing: building output must precede curing consumption within S.
```

### 12.2 Shift Mapping (preferred S-1 case)

```
Curing Shift A (Day N) ← GT preferred from Building Shift C (Day N-1)
Curing Shift B (Day N) ← GT preferred from Building Shift A (Day N)
Curing Shift C (Day N) ← GT preferred from Building Shift B (Day N)
Minimum: same-shift building output also counts.
```

### 12.3 Per-Press Per-Shift Assignment

```
For each curing shift S, each press P:

  Case A — CHANGEOVER:   Status = CHANGEOVER,  Qty = 0
  Case B — MOULD_CLEAN:  Status = MOULD_CLEAN, Qty = 0
  Case C — RUNNING, GT available (from inventory or building S-1 or building S):
    GT_avail = GT_inventory_remaining(P.SKU, S) + building_output(P.SKU, S)
    Qty      = min(qty_per_press_per_shift(P.SKU), GT_avail)
    Status   = RUNNING
    Deduct Qty from GT_inventory_remaining
  Case D — RUNNING, GT = 0 (no inventory, no building output at S):
    Status = WAITING_GT, Qty = 0  → flag STARVATION
  Case E — IDLE:
    Status = IDLE, Qty = 0
```

### 12.4 bc_curing.xlsx Schema

| Column | Description |
|--------|-------------|
| `Press_ID` | curing press |
| `Date` | |
| `Shift` | A / B / C |
| `SKU_Code` | SKU being cured |
| `Status` | RUNNING / CHANGEOVER / MOULD_CLEAN / WAITING_GT / IDLE |
| `Qty_Produced` | tyres cured (0 unless RUNNING) |
| `GT_Source` | INVENTORY / BUILDING_S-1 / BUILDING_SAME_SHIFT |
| `GT_Consumed` | total GT drawn this shift for this press |
| `GT_Inventory_Remaining` | rolling balance after this shift |
| `Active_Press_Count` | `active_press_count(SKU, S)` |

---

## 13. Phase 5 — Analysis & KPI Report

### 13.1 Expected KPIs (from actual machine data)

**System facts (from codebase):**
- 170 curing presses total; 80 running (55 Runner-In, 25 Runner-Out)
- 39 building machines: 15 Stage-1, 6 Stage-2, 18 Unistage
- Building theoretical max: **9,521 GT/shift** (S2: 2,215 + Unistage: 7,306)
- Stage-1 carcass capacity: 2,459/shift; Stage-2 demand: 2,215/shift → 11% headroom
- Curing at CT=17 min default: **56 tyres/press/shift**

**Impact of the 4 updates:**
- Update 1 (opening GT inventory): Day-1 buffer; no change to daily/monthly rate
- Update 2 (relaxed S constraint): Stage-2 startup cost eliminated (+2,215 units)
- Update 3 (1 day pre-start): building day D targets day D+1 curing → LP sees full demand every day, eliminates Day-2+ idle
- Update 4 (BUILD_LEAD_SHIFTS WIP cap fix): `gt_inv_for_cap = wip0 − today_cure` prevents LP cap collapse in steady state

| KPI | C2B | B2C | Confidence |
|-----|-----|-----|------------|
| Starvation events | Frequent | **0** | Architectural guarantee |
| Waste GT | Possible | **0** | Architectural guarantee |
| Max changeovers/day | Uncontrolled | **≤ 8** | Hard constraint |
| Opening GT inventory used | Ignored | **From DB** | Per update 1 |
| Stage-2 startup idle shifts | 0 | **0** (relaxed rule, S allowed) | Per update 2 |
| Building pre-production | 3 shifts (1 day) | **3 shifts / 1 day** (May 31 Shift A) | Per update 3 |
| Building utilisation | ~65% | **~90–95%** | High-confidence estimate |
| Curing util — Runner-In (55 presses) | ~80% (starvation) | **~98%** | High estimate |
| Runner-Out CO complete | Never planned | **Day 4** (25 ÷ 8/day) | Calculated |
| CO production loss | — | **2,800 tyres total** | Calculated |
| **Daily tyres — Day 1** | ~7,392 | **~10,372** | Calculated (ramp) |
| **Daily tyres — Day 4 (80 presses)** | ~7,392 | **~13,171** | Calculated |
| **Daily tyres — Day 5+ (+ Phase 1b)** | ~7,392 | **~15,641** | Calculated |
| **Monthly tyres — conservative (80P)** | ~221,760 | **~392,336** | Calculated |
| **Monthly tyres — expected (95P)** | ~221,760 | **~457,856** | Calculated |
| Daily uplift vs C2B | — | **+78% (Day 4), +112% (Day 5+)** | Calculated |
| Monthly uplift vs C2B | — | **+77% conservative, +106% expected** | Calculated |
| New SKU coverage (Phase 1b) | 0 | **15–20 SKUs added** | Data-dependent |

### 13.2 Starvation Report

| Column | Description |
|--------|-------------|
| `Date`, `Shift`, `SKU_Code`, `Press_ID` | |
| `GT_Deficit` | qty building fell short |
| `Severity` | STARVATION / WARNING / OK |

### 13.3 Changeover Summary

| Column | Description |
|--------|-------------|
| `Day` | |
| `Changeovers_Taken` | must be ≤ 8 |
| `Stranded_Presses` | no viable target found |
| `Deferred_Presses` | viable but budget full; next day |
| `Total_Downtime_Shifts` | CHANGEOVER + MOULD_CLEAN press-shifts |

### 13.4 Building Utilisation

```
utilisation(machine, shift) = committed_production_minutes / 480
```

---

## 14. Module Structure

```
bc/
├── b2c.py                      # top-level orchestrator
├── curing_consumption.py       # Phase 0: consumption table + press count init
├── building_scheduler_b2c.py   # Phases 1a/1b: Stage-1 + Stage-2 + Unistage
├── changeover_planner.py       # Phases 2a/2b + joint pool
├── building_target_lock.py     # Phase 3: dynamic per-shift cap
├── curing_schedule_deriver.py  # Phase 4: curing derivation
└── b2c_analyser.py             # Phase 5: analysis + KPI report
```

### b2c.py orchestration

```python
run_b2c(cfg):
  consumption, press_count = build_curing_consumption_table(cfg)        # Phase 0
  bld_pool = run_runner_in_building(consumption, press_count, cfg)       # Phase 1a
  co_candidates = compute_runner_out_candidates(bld_pool, cfg)           # Phase 2a (eligibility)
  bld_schedule, co_plan = resolve_joint_priority_pool(                   # Phase 1b + 2a joint
      co_candidates, consumption, bld_pool, press_count, cfg)
  co_plan = schedule_pending_changeovers(co_plan, cfg)                   # Phase 2b
  bld_schedule = lock_per_shift_building_targets(bld_schedule,           # Phase 3
                                                 press_count, co_plan)
  curing_schedule = derive_curing_schedule(bld_schedule, co_plan,        # Phase 4
                                           press_count, cfg)
  run_analysis(bld_schedule, curing_schedule, consumption, co_plan, cfg) # Phase 5
```

---

## 15. Key Config Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| `SHIFT_DURATION_MIN` | 480 | minutes per shift |
| `SHIFTS_PER_DAY` | 3 | A(07:00) / B(15:00) / C(23:00) |
| `GT_LEAD_SHIFTS` | 1 | GT must be built 1 shift before curing (hard) |
| `CARCASS_LEAD_SHIFTS` | 1 | Carcass must be built 1 shift before Stage-2 (hard) |
| `LOAD_UNLOAD_BUFFER_MIN` | 2.3 | added to raw cure time |
| `PRESS_EFFICIENCY` | 0.94 | divisor in effective CT formula |
| `DEFAULT_CYCLE_TIME_MIN` | 17.0 | effective CT when SKU missing from DB |
| `CAVITIES_PER_MOULD` | 2 | tyres per curing cycle |
| `MOULDS_PER_PRESS` | 2 | tracked for mould-life; not in output formula |
| `MAX_CURING_CHANGEOVERS_PER_DAY` | **8 (HARD)** | curing press changeovers only; **no limit on building machine COs** |
| `CURING_CO_MIN` | 300 | changeover (press OCCUPIED whole shift) |
| `MOULD_CLEAN_MIN` | 120 | mould clean (press OCCUPIED whole shift) |
| `OPENING_GT_INVENTORY` | from DB | loaded from `gt_inventory_manual` at plan start |
| `OPENING_CARCASS_INVENTORY` | **0 (cold start)** | Stage-2 blocked until Stage-1 produces carcasses in this plan |
| `BUILDING_GT_CAP` | `min(press_consumption, demand/90)` | building output ≤ customer demand per SKU |
| `BUILD_LEAD_SHIFTS` | **3** | building targets curing demand this many shifts ahead; 3 = 1 full day |
| `OVERBUILD_BUFFER_FRAC` | 0.0 | fractional surplus allowed above net curing demand; 0 = exact match |
| `BUILDING_START_OFFSET_SHIFTS` | -3 | building pre-start = 3 shifts (1 day) before curing Shift A |
| `BUILDING_PRE_SHIFT` | `Shift A of Day-1` | e.g. May 31 Shift A if curing starts June 1 |

---

## 16. Edge Cases

| ID | Edge Case | Handling |
|----|-----------|----------|
| EC-01 | Building capacity < Runner-In consumption | Produce at max; log CAPACITY_SHORTFALL; curing press → WAITING_GT |
| EC-02 | No viable CO target for Runner-Out press | STRANDED_PRESS; IDLE all cycle; logged |
| EC-03 | 8-CO daily budget exhausted | Defer lower-priority to next day; resort each day |
| EC-04 | Shared feeder machine contention | Higher Priority_Score target wins; loser deferred |
| EC-05 | Non-Runner-In: no free building machine | DEFERRED; log NO_FREE_BLD |
| EC-06 | Non-Runner-In: free building, no free curing press | DEFERRED; log NO_FREE_CUR; building not allocated |
| EC-07 | Non-Runner-In: free curing press, no free building | DEFERRED; log NO_FREE_BLD; curing not reserved |
| EC-08 | All building capacity consumed by Phase 1a | Phase 1b skipped entirely; all Non-Runner-In DEFERRED |
| EC-09 | Runner-In demand fulfilled mid-cycle | Cap building to 0; freed press enters CO queue |
| EC-10 | Near-expiry mould on Runner-Out press | Priority in CO queue (secondary sort: mould_life ASC) |
| EC-11 | Two presses want same CO target SKU | Higher Priority_Score press gets it; other gets next-best |
| EC-12 | Stage-1 machine unavailable for a Stage-2 assignment | Stage-2 removed from that shift; CARCASS_SHORTFALL logged |
| EC-13 | Machine spare minutes < CO cost + 1 cycle | Machine counts as OCCUPIED (§3.2 check) |
| EC-14 | Stage-2 Day 0 Shift A idle (S1 rule cold start) | By design; Unistage still runs; GT loss = 2,215 units = 0.25% |
| EC-15 | Phase 1b building machine later claimed by higher-priority joint pool item | Joint pool resolves at assignment time; higher priority wins |

---

## 17. Assumptions

1. **Cycle time is SKU-level.** One effective CT per SKU. Ambiguous DB rows → minimum CT.
2. **Each shift is exactly 480 minutes.** No partial carry-over.
3. **Changeover = Shift A OCCUPIED, Mould Clean = Shift B OCCUPIED.** New SKU production = Shift C.
4. **Stage-1 and Stage-2 are offset by 1 shift** (not inline). Unistage has no Stage-1 dependency.
5. **Feed map is pre-built** (`feed_map_builder.py`). Reused as-is unless `REBUILD_FEED_MAP=True`.
6. **ConsolidatedPriorityScore** is a static input from the demand file. Not computed here.
7. **Building machine running state** read from `TBMStage1/2_ProductionEventData` as of `PLAN_START`.
8. **Mould clean mid-production** (when mould life expires within a running shift, not at CO time):
   effective output = `floor((480 - 120) / CT) × 2`. Accounted for when reading mould life from `Daily_Running_Moulds`.
9. **NRI building starts without waiting for curing CO.** Building machines (including Unistage 7001/7002/7003) are assigned to NRI SKUs immediately. GT accumulates in inventory until the curing CO is executed. Curing CO limit = 8/day applies separately.

---

## 18. Output Metrics — Definitions

### 18.1 Starvation (in Starvation Report sheet)

**Starvation** = the curing press tries to consume GT that does not yet exist in inventory.

```
WIP_Balance(SKU, shift S) = Σ(Build_Qty, shifts 0..S) − Σ(Cure_Qty, shifts 0..S)

STARVATION  : WIP_Balance < 0   → curing press idle waiting for GT
WARNING     : WIP_Balance = 0   → buffer empty; any miss next shift causes starvation
OK          : WIP_Balance > 0   → healthy surplus buffer
```

Starvation is a **future-state metric** for Phase 2 (curing). In Phase 1 (building), the Starvation Report represents a projection: if curing ran at full consumption rate, when would each SKU run out of GT?

### 18.2 Total_Units in Machine Utilisation

```
Total_Units = Σ(production Qty) across ALL building machine types

Breakdown:
  Stage-2 + Unistage (GT machines) → these are FINISHED GREEN TYRES for curing
  Stage-1             (Carcass)     → these are SEMI-FINISHED CARCASSES for Stage-2

  GT_Total   = Stage-2 qty + Unistage qty   ← what the Demand header "Built" shows
  Carcass    = Stage-1 qty                  ← internal intermediate product
  Total_Units = GT_Total + Carcass          ← both types combined
```

Example: GT_Total = 621,297 + Carcass = 48,144 → Total_Units = **669,441**.

### 18.3 Low Machine Utilisation — Root Cause Analysis

**GT/Unistage machines (6003, 6004, 7001, 7002, 7003):**

| Machine | Type | Util % | Root Cause |
|---------|------|--------|-----------|
| 6003 | Unistage | ~57% | Moderate — mixed NRI SKUs with some CO overhead |
| 6004 | Unistage | ~54% | Moderate — mixed NRI SKUs with some CO overhead |
| 7001 | Unistage | ~28% | **Excessive COs** — 30 changeovers / 36 production shifts = 45% of time in CO |
| 7002 | Unistage | ~26% | **Excessive COs** — 32 changeovers / 38 production shifts = 46% in CO |
| 7003 | Unistage | ~25% | Fewer COs (10) but small-lot NRI SKUs with short campaign length |

Fix: Campaign consolidation — assign 7001/7002/7003 to 1–2 NRI SKUs for longer runs instead of cycling through 7–10 SKUs per month.

**Stage-1 machines (6803, 7601, 8101, 7701, 8002, 7802, 7801, 7803, 7804, 8001, 6802, 6911, 6909, 6801, 8003 — ALL below 33%):**

Root cause is **not** a scheduling inefficiency. Stage-1 util is structurally limited by Stage-2 GT demand:
```
Stage-2 GT demand ≈ 1,886 carcasses/shift (169,735 GT ÷ 90 shifts)
Stage-1 capacity  = 2,459 carcasses/shift (15 machines × 164/machine/shift)
Effective Stage-1 needed ≈ 1,886 / 164 ≈ 11.5 machines

→ 15 Stage-1 machines for 11.5 machine-equivalents of demand = 77% theoretical peak
  further reduced because carcass cold start blocks Stage-2 on Shift 1 (Day 1)
```

Stage-1 utilisation improves only if Stage-2 GT demand grows — i.e., more Runner-In or NRI SKUs are assigned to Stage-2 (two-piece tyre) building machines. This is driven by `Master_Building_Allowable_Machines_source`.

### 18.4 NRI SKUs with Zero Production — Root Cause

NRI SKUs can have zero production for two distinct reasons:

| Root Cause | Skip_Reason in output | Fix |
|---|---|---|
| No allowable building machine in master data or history | `NRI: no building machine allocated — check allowable machines master data` | Add SKU to `Master_Building_Allowable_Machines_source` or source from history |
| Building capacity already consumed by higher-priority SKUs | `NRI: partial build — building capacity shared with higher-priority SKUs` | Free up capacity by tightening Runner-In demand cap; check CO budget |
| Curing CO deferred past horizon | `NRI: curing CO deferred — 8 CO/day cap reached` | Extend horizon or allow more COs in early days |

NRI SKUs that ARE produced but partially: building machines are assigned, but the LP allocates less than total demand because higher-priority Runner-In SKUs claim the capacity first.
