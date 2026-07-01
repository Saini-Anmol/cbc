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
| Starvation risk | Frequent (root cause of redesign) | **Zero — Phase 4 active in `curing_b2c.py`**; curing is derived FROM building output, never exceeds available GT |
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

- Machine running state is taken from `testing_Daily_Running_Moulds` as of the **curing start date** (June 1 snapshot).
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

> **Curing changeover cap: configurable (currently **8/day**); set `MAX_CHANGEOVERS_PER_DAY` in `bc_config.py` — single source of truth.**
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
 │               └─ Mould-constrained priority boost (1+curr_days/31,≤4x)│
 │             Phase 1b + 2a: Joint Priority Pool (NRI + RO CO)           │
 │               └─ NRI demand front-loaded 70% pre-CO / 30% post-CO     │
 │             Phase 2b: Changeover scheduling (max MAX_CHANGEOVERS_PER_DAY/day)│
 │               └─ CO Rescue pass: spare-press donation for stranded NRI │
 │             Phase 3: Dynamic target lock                                │
 │             Output: bc_building_schedule.xlsx                           │
 │                                 ↓                                       │
 │  PHASE 4 ── Curing Schedule Derivation (ACTIVE — curing_b2c.py)       │
 │             GT-balance shift-by-shift simulation. Curing ≤ GT in pool. │
 │             ★ Zero starvation guaranteed by architecture.               │
 │             167 presses (WCNAME format); CO transitions from bld output │
 │             Output: bc_curing_b2c.xlsx                                  │
 │                                 ↓                                       │
 │  Phase 5 ── Analysis & KPI Report (pending)                            │
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
              ∪ testing_Daily_Running_Moulds  (historical curing press data)

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
| `testing_Daily_Running_Moulds` | `WCNAME`, `Sapcode`, `Current MouldNo`, `Mould life` | Phase 0, 1a, 2a | Which SKU each press runs today; mould life remaining; curing history fallback |
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
| `curing_consumption_table.xlsx` | Phase 0 | Per-SKU: category, press count, effective CT, GT/shift (Day 0 snapshot) |
| `curing_consumption_31day.xlsx` | Phase 0 Extended | 31-sheet workbook: one day per sheet (Day_01–Day_31) + CO_Schedule + Day0_Summary |
| `bc_building.xlsx` | Phase 1a/1b | Per building machine, per shift: SKU, qty, status, stage |
| `curing_changeover_plan.xlsx` | Phase 2a/2b | Per press: from SKU → to SKU, changeover shift |
| `bc_curing.xlsx` | Phase 4 | Per curing press, per shift: SKU, qty, status |
| `b2c_analysis.xlsx` | Phase 5 | Utilisation, GT alignment, starvation, KPIs |

---

## 6. Phase 0 — Curing Consumption Table

### 6.1 SKU Universe Classification

```
Demand file (120 SKUs)
├── Currently on a curing press (testing_Daily_Running_Moulds)  → 80 SKUs
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

> **Building output for any SKU must NEVER exceed 100% of customer demand. Hard limit — no overproduction under any circumstance.**

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

### 6.3c Hard 100% Demand Cap — Three-Layer Enforcement

The demand cap is enforced at three layers to guarantee total production ≤ 100% of `Demand_Qty` per SKU across the full 30-day horizon.

**Layer 1 — Horizon remaining tracker (`_gt_remaining`)**
```
At plan start:  _gt_remaining[SKU] = min(curing_plan_total, Demand_Qty[SKU])
After each day: _gt_remaining[SKU] -= actual_GT_produced_today[SKU]
                (clamped to 0, never negative)
```
TopUp idle-fill uses `_gt_remaining` as its target — once it reaches 0 for a SKU,
TopUp builds nothing more for that SKU. This has been in place since the initial B2C design.

**Layer 2 — Daily LP `cur_mat` clip (new)**
```
Before each day's LP run, per SKU:
  remaining = _gt_remaining[SKU]
  daily_target = cur_mat[SKU].sum()
  if daily_target > remaining:
      cur_mat[SKU, :] *= (remaining / daily_target)   ← scale down proportionally
```
Effect: LP never *sees* demand beyond what's still owed. A SKU at 100% fulfillment
gets `cur_mat = 0` → machines that were serving it become available for other SKUs
rather than idling or overbuilding.

**Layer 3 — LP hard per-SKU ceiling constraint (new)**
```
For each SKU: Σ_{m,t} x[SKU, m, t] / CT_m  ≤  _gt_remaining[SKU]
```
Enforced as an additional LP inequality constraint alongside the existing `gross × buf` cap.
The binding constraint is whichever is tighter:
- Early days: `gross × 1.2` binds (normal buffer operation)
- Late days / tail: `_gt_remaining` binds (hard 100% ceiling)

This prevents the `OVERBUILD_BUFFER_FRAC = 0.2` LP headroom from accumulating
beyond customer demand on the last production days of each SKU.

**Why all three layers are needed:**
| Layer | What it prevents |
|-------|-----------------|
| Layer 1 (TopUp tracker) | TopUp from stockpiling beyond demand |
| Layer 2 (cur_mat clip) | LP from targeting phantom demand for fulfilled SKUs |
| Layer 3 (LP ceiling) | LP's 1.2× buffer from producing >100% on tail days |

Together they guarantee: `Σ_days GT_built[SKU] ≤ Demand_Qty[SKU]` for every SKU.

### 6.3b NRI SKU Building Start

Non-Runner-In SKUs do **not** wait for a curing press changeover confirmation before building starts.
Building machines are assigned to NRI SKUs immediately (including Unistage machines 7001/7002/7003).
Curing press changeovers for NRI SKUs are planned in parallel and limited to `MAX_CHANGEOVERS_PER_DAY`/day (set in `bc_config.py`).
GT built for NRI SKUs before their curing press CO will sit in GT inventory until the press is ready.

**NRI demand front-loading (70/30 split):** When the CO schedule provides a confirmed
`CO_Day_Index` for an NRI SKU, 70% of demand is spread uniformly over the pre-CO window
and 30% over the post-CO window. The LP sees higher urgency (larger per-shift target) before
the CO day and pre-builds a GT buffer. When the curing CO fires on Shift C of `CO_Day`,
2 full shifts of GT inventory are already available → eliminates Day-1 starvation for new-press
SKUs. If no confirmed CO day is found, demand is spread uniformly across the full horizon.
Implemented via `co_day_map` in `_make_synthetic_curing()` in `building_b2c.py`.

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
| `Running_Press_Count` | `testing_Daily_Running_Moulds` (0 for Non-Runner-In) |
| `Allowable_Moulds_Count` | `Master_Mapping_Mould_SKU` |
| `Effective_CT_Min` | §6.2 |
| `Qty_Per_Press_Per_Shift` | `floor(480/CT) × 2` |
| `Total_GT_Per_Shift_Day0` | `qty_per_press × running_press_count` |
| `Unit_GT_Per_Press` | Non-Runner-In sizing |
| `Demand_Qty` | demand file |
| `Priority_Score` | `ConsolidatedPriorityScore` |

### 6.6 Phase 0 Extended — 31-Day Dynamic Curing Consumption Pre-Computation

**File:** `curing_consumption_dynamic.py`
**Output:** `data/output/curing_consumption_31day.xlsx`

Pre-computes how curing consumption evolves across all 31 days of May using a
two-pass approach that is fully independent of the building scheduler.

#### Pass 1 — CO Schedule (`COScheduler`)

Compute the full changeover plan from Day 0 data alone:

1. **Runner-Out presses** → eligible for CO from Day 1.
2. **Runner-In presses** → eligible for CO on the day `Updated_Demand_Qty` hits 0.
3. **NRI targets** ranked by two-level urgency score (§8 CO urgency):
   - Class A (CRITICAL): `current_production_days > horizon_left` — demand cannot be met without this CO.
   - Class B (HELPFUL): already fulfillable with existing presses — **SKIPPED** (Class A only filter active).
   - Sort: `(class ASC, −Priority_Score, after_co_days ASC)`
   - Rationale for Class A only: firing Class B COs activates too many NRI SKUs simultaneously, causing building CO explosion (1,958 building COs observed vs 1,458 baseline). Building cannot supply all newly-activated presses → starvation events increase.
4. **Max `MAX_CHANGEOVERS_PER_DAY` COs per day** (currently **9**; set in `bc_config.py` — single source of truth); excess deferred to next day.
5. CO takes effect same day (new SKU's press count updates on CO day; Shift C produces for new SKU).
6. **CO Rescue pass** (runs after the main 31-day loop): NRI SKUs that received no CO
   in the main loop are sorted by the same urgency score. For each, search for an RI press
   with `n_presses > 1` whose RI SKU can still meet demand with `n−1` presses
   (`ri_days_without ≤ planning_days`). If found, donate one press to the NRI SKU
   (counts toward the daily CO cap; deferred to Day D+1 if cap is full). NRI SKUs with no
   compatible spare press remain unscheduled and are logged. Implemented in
   `COScheduler.schedule()` in `curing_consumption_dynamic.py`.

#### Pass 2 — Day Simulation (`DaySimulator`)

Simulate 31 days using the CO schedule from Pass 1:

| Per-day update | Detail |
|----------------|--------|
| Apply CO events | `Running_Press_Count` ±1; CO'd NRI SKU category flips to Runner-In |
| Build day sheet | All columns per §6.5 plus `Total_GT_Per_Shift_DayN`, `Updated_Demand_Qty`, `Production_Days` |
| Drain demand | `Updated_Demand_Qty -= Running_Press_Count × Qty_Per_Press_Per_Shift × 3` |

`Production_Days` = blank if `Running_Press_Count = 0` (NRI before its CO fires).

#### Excel workbook structure

| Sheet | Contents |
|-------|----------|
| `Summary` | Coverage KPIs: total demand, built GT, curing coverage %, day-31 remaining by SKU |
| `Day0_Summary` | Mirrors `curing_consumption_table.xlsx` (snapshot at plan start) |
| `CO_Schedule` | All CO events: Day, Press, Old_SKU → New_SKU + day-level count summary |
| `curing_daily_cons` | Two-column table: Day 01…31 + Total. Each row = total curing consumption supplied to building (RI + NRI SKUs only, Runner-Out excluded). Total should match ~634k for May. |
| `Day_01` … `Day_31` | Per-day consumption table; colour-coded by category (green=RI, orange=RO, yellow=NRI) |

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

### 7.0a Inch Study — Machine Group Inch Policies (from May Plant Data)

> **Source: Inch-Run Study Report (May production, 30 building machines, 32 days).**
> These are confirmed plant behaviours — the scheduler must encode them as constraints,
> not suggestions.

**Inch demand distribution (plant-wide):**
15" (34%) › 13" (21%) › 14" (14%) ≈ 12" (14%) › 16" (12%) › 17"/18" (tail ~5%)

**Four machine groups with distinct inch policies:**

| MG Group | Building machines | Allowed inches | Policy | Dominant-inch lock strength |
|----------|------------------|----------------|--------|-----------------------------|
| VMIMAXX | 6001–6004, 7001–7004 | 14"–18" | Flexible overflow absorber | 60–90% (4–7 inches per machine) |
| BJ | 7101–7106, 7201 | 13", 14", 15", 16" | Well-locked per machine | 83–99% dominant |
| TWO STAGE TBM | Stage-1 + Stage-2 machines | 12", 15", 13" | ~Half single-inch | 62–100% dominant |
| UNISTAGE | 7501, 7502, 7503 | **12", 13" ONLY** | **Perfectly inch-locked** | **97–100% dominant** |

**Per-machine dominant inch (from study — use as soft-lock seed):**

| Machine | Group | Dom. inch | Dom. share |
|---------|-------|-----------|-----------|
| 7104 | BJ | 15" | 99% |
| 7105 | BJ | 13" | 99% |
| 7101 | BJ | 15" | 98% |
| 7201 | BJ | 16" | 94% |
| 7103 | BJ | 13" | 89% |
| 7106 | BJ | 13" | 84% |
| 7102 | BJ | 15" | 83% |
| 7502 | UNISTAGE | 13" | 100% |
| 7503 | UNISTAGE | 13" | 100% |
| 7501 | UNISTAGE | 12" | 97% |
| 6004 | VMIMAXX | 16" | 90% |
| 6001 | VMIMAXX | 14" | 89% |
| 7004 | VMIMAXX | 14" | 86% |
| 6002 | VMIMAXX | 15" | 83% |
| 6003 | VMIMAXX | 17" | 80% |
| 7003 | VMIMAXX | 15" | 80% |
| 7002 | VMIMAXX | 14" | 73% |
| 7001 | VMIMAXX | 16" | 60% |

**Scheduling rules derived from the study:**

```
1. HARD inch constraint — UNISTAGE (7501/7502/7503):
   Never assign a 14"+ SKU to these machines. Hard filter BEFORE heuristic assigner.

2. Group routing priority — prefer locked groups for their dominant inch:
   12" demand  → TWO STAGE TBM first → UNISTAGE second
   13" demand  → UNISTAGE first → BJ second → TWO STAGE TBM last
   14" demand  → VMIMAXX first (their dominant); 7102/7104 carry 14" in BJ for
                 2 BJ-exclusive 14" RI SKUs (1325218614088HURL0, 1325217514082TVECH)
   15" demand  → BJ first → TWO STAGE TBM second → VMIMAXX last
   16"/17"/18" → VMIMAXX only

3. VMIMAXX as overflow absorber:
   VMIMAXX machines absorb demand that BJ/TBM cannot cover.
   Do NOT pre-fill VMIMAXX with high-volume primary-inch demand
   if a more inch-locked group can serve it — this causes unnecessary COs on VMIMAXX.

4. Per-machine dominant inch as soft-lock seed:
   In the heuristic assigner, prefer SKUs of the machine's dominant inch.
   Same-dominant-inch SKUs score higher than off-inch SKUs with equal demand.
   This replaces randomness in the history_map seed with a plant-confirmed preference.

5. Root cause of 7001–7004 low utilisation (25–28%):
   These VMIMAXX machines carry 5–7 different inches each month → 30–32 COs → 45% time in CO.
   Fix: restrict each machine to its dominant inch (7001→16", 7002→14", 7003→15", 7004→14").
   Expected utilisation improvement: 25% → 75%+.
6. HARD per-machine dominant-inch lock (_MACHINE_HARD_INCH in building_b2c.py):
   Applied after group inch policy filter, before heuristic assigner.
   Each machine filtered to its dominant inch(es) only:
     VMIMAXX: 7001→{16}, 7002→{14}, 7004→{14}, 6001→{14}
              6002→{15}, 7003→{15}, 6003→{17,18}, 6004→{16}
     BJ:      7101→{15}, 7102→{14,15}, 7103→{13}, 7104→{14,15}
              7105→{13}, 7106→{13}, 7201→{16}
     UNISTAGE:7501→{12}, 7502→{13}, 7503→{13}
   7001 locked to {16} only (not {16,17,18}) so TIER-3 mach_elig_count matches
   6004/7201 and 7001 competes fairly for 16" SKUs.
   Result: 6001 util 3%→58%, 7001 util 8%→45%, BJ avg 73%→83%.
```

**Priority-first machine reservation for Unistage group (new logic):**

```
Pass 1 — Reserve machines for top-priority SKUs:
  - Sort all demand SKUs by Priority_Score DESC
  - For each top-priority SKU (top 30% by score):
      Pick the eligible machine with best dominant-inch match
      Reserve that machine (LOCKED_TO_PRIORITY flag)
  - True UNISTAGE (7501/7502/7503): dedicate one machine per top 12"/13" SKU
    → zero COs on those machines, maximum output for highest-priority small tyres

Pass 2 — Fill remaining machines with lower-priority SKUs:
  - Normal heuristic assign over non-reserved machines
  - LOCKED_TO_PRIORITY machines are not available to lower-priority SKUs
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

**Mould-constrained priority boost (applied before heuristic assignment):**
```
For each Runner-In SKU with Running_Press_Count > 0 and Demand_Qty > 0:
  rate_day    = Qty_Per_Press_Per_Shift × SHIFTS_PER_DAY
  current_days = Demand_Qty / (Running_Press_Count × rate_day)
  multiplier   = min(1.0 + current_days / PLANNING_DAYS, 4.0)
  priority_map[SKU] *= multiplier   (if multiplier > 1.01)
```
A SKU with few presses and high demand has large `current_days` → multiplied priority →
LP assigns building machines to it first. Prevents late discovery that a slow-throughput
SKU cannot meet demand within the horizon. Implemented in `building_b2c.py` after
`priority_map` construction.

**DemandHeuristicAssigner sort key (5 tiers — history bias removed):**

| Tier | Sort key | Purpose |
|------|----------|---------|
| TIER 0 | `0 if dominant_inch(m) == sku_inch else 1` | Route by inch first — VMI gets 16", BJ gets 15"/13", etc. |
| TIER 1 | `_inch_mach_dmins[(inch, m)]` | Per-inch demand-minutes balance: siblings share volume not count |
| TIER 2 | `−demand_frac` (UNISTAGE only) | High-demand high-priority SKUs prefer UNISTAGE |
| TIER 3 | `mach_elig_count(m)` | More specialised machines served first |
| TIER 4 | `m_idx[m]` | Stable tiebreaker |

History bias (`−history_map[(m, sku)]`) **removed** — old run history encoded wrong-inch
routing and caused 6001 to receive only low-demand fragments of 14" pool.

`_inch_mach_dmins`: dict of (inch_str, machine) → cumulative demand-minutes assigned.
After each SKU-machine assignment, `demand_mins` for that SKU is added. Same-inch siblings
(e.g. 6001/7002/7004 for 14") receive equal total demand-minutes load, so high-volume
and low-volume 14" SKUs distribute evenly rather than all large-volume SKUs landing on
one sibling.

For each Runner-In SKU (sorted by `Priority_Score DESC` after boost):

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

**CO target urgency score (replaces simple Priority_Score sort):**

When a freed press (Runner-Out or fulfilled Runner-In) is assigned to an NRI target,
the target is selected by a two-level urgency score:

```
For each candidate NRI target SKU T:
  n     = current Running_Press_Count[T]
  rate  = Qty_Per_Press_Per_Day[T]  =  Qty_Per_Press_Per_Shift[T] × 3
  rem   = Updated_Demand_Qty[T]
  horizon_left = planning_days − current_day

  current_days = rem / (n × rate)      if n > 0 else ∞
  after_days   = rem / ((n+1) × rate)

  urgency_class:
    Class A (CRITICAL)  — current_days > horizon_left
                          demand CANNOT be met without this CO
    Class B (HELPFUL)   — current_days ≤ horizon_left
                          demand can be met with existing presses

Sort candidates: (urgency_class ASC, −Priority_Score, after_days ASC)
  → Class A first; within class: highest priority, then fewest days after CO
```

**Objective**: fulfill demand on time AND by priority.
- Class A SKUs are always served before Class B (missing their deadline is worse than serving lower priority)
- Among same-class SKUs, Priority_Score breaks ties
- `after_days` breaks final ties: prefer the assignment that makes the SKU fulfillable soonest

**Changeover scheduling:**
```
Sort JOINT POOL by urgency score (see above)
Assign greedily per day: up to MAX_CHANGEOVERS_PER_DAY total changeovers/day (set in bc_config.py)
CO fires INSTANTLY when demand is fulfilled for a Runner-In SKU (same day, Shift A).
If Day D's CO budget is full, defer to Day D+1.

Changeover always starts Shift A of assigned day:
  Shift A → CHANGEOVER (300 min, press OCCUPIED whole shift)
  Shift B → MOULD_CLEAN (120 min, press OCCUPIED whole shift)
  Shift C → new SKU production begins
  Building machine → starts producing GT for new SKU simultaneously with Shift A
                     (2 full shifts of GT pre-built before curing fires up)

On assignment of (P → T*, Day D):
  active_press_count(T*, shift ≥ Day D Shift C) += 1
  active_press_count(old_SKU, shift ≥ Day D Shift A) -= 1
  building_machine(T*) → start Shift A of Day D  ← simultaneous with CO
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
Phase 2a changeovers, sharing the same `MAX_CHANGEOVERS_PER_DAY` budget (set in `bc_config.py`).

```
Combined list = Phase 2a + Phase 2b candidates
Sort: Priority_Score DESC, mould_life_remaining ASC
Assign: up to MAX_CHANGEOVERS_PER_DAY/day, Shift A of each day

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

**Impact of the 5 updates:**
- Update 1 (opening GT inventory): Day-1 buffer; no change to daily/monthly rate
- Update 2 (relaxed S constraint): Stage-2 startup cost eliminated (+2,215 units)
- Update 3 (1 day pre-start): building day D targets day D+1 curing → LP sees full demand every day, eliminates Day-2+ idle
- Update 4 (BUILD_LEAD_SHIFTS WIP cap fix): `gt_inv_for_cap = wip0 − today_cure` prevents LP cap collapse in steady state
- Update 5 (hard 100% demand cap): Three-layer enforcement — `_gt_remaining` tracker + daily `cur_mat` clip + LP per-SKU ceiling constraint. Guarantees total production ≤ `Demand_Qty` per SKU. Freed capacity redistributed to under-served SKUs by TopUp.

| KPI | C2B | B2C | Confidence |
|-----|-----|-----|------------|
| Starvation events | Frequent | **0 (Phase 4 — implemented in curing_b2c.py)** | Phase 4 = architectural guarantee; GT-limited curing can never exceed available GT |
| Waste GT | Possible | **0** | Architectural guarantee |
| Overproduction per SKU | Uncontrolled | **0% — hard 100% cap** | Per update 5 (3-layer LP enforcement) |
| Max changeovers/day | Uncontrolled | **≤ 8** | Hard constraint |
| Opening GT inventory used | Ignored | **From DB** | Per update 1 |
| Stage-2 startup idle shifts | 0 | **0** (relaxed rule, S allowed) | Per update 2 |
| Building pre-production | 3 shifts (1 day) | **3 shifts / 1 day** (May 31 Shift A) | Per update 3 |
| Building utilisation | ~65% | **~90–95%** | High-confidence estimate |
| Curing util — Runner-In (55 presses) | ~80% (starvation) | **~98%** | High estimate |
| Runner-Out CO complete | Never planned | **Day 4** (25 ÷ 8/day; `MAX_CHANGEOVERS_PER_DAY=8`) | Calculated |
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

## 14. Module Structure (ACTUAL — as of June 2026)

```
cbc/
├── bc_config.py                    # SINGLE SOURCE OF TRUTH — all params
├── b2c_pipeline.py                 # ENTRY POINT — run all 3 steps
├── curing_consumption.py           # Phase 0: Day-0 snapshot
├── curing_consumption_dynamic.py   # Phase 0 Extended: 31-day CO schedule
├── building_b2c.py                 # Building scheduler: Phases 1a/1b/2a/2b/3
├── building.py                     # LP engine + DemandHeuristicAssigner
├── curing_b2c.py                   # Phase 4: GT-balance curing simulation
└── cbc_env.py                      # DB connection
```

### b2c_pipeline.py orchestration (actual)

```
python b2c_pipeline.py [demand_file.xlsx]

Step 1 (curing_consumption_dynamic.py):
  Phase 0 → Day-0 snapshot + SKU classification (RI/RO/NRI)
  Phase 0+ → 31-day CO schedule (Class A only, cap=MAX_CHANGEOVERS_PER_DAY)
  Output: curing_consumption_31day.xlsx

Step 2 (building_b2c.py):
  Phase 1a → Runner-In building (mould-constrained priority boost)
  Phase 1b+2a → Joint priority pool (NRI + RO) with 70/30 synthetic demand
  Phase 2b → Pending CO scheduling
  Phase 3 → Dynamic target lock
  Output: bc_building_schedule_YYYY-MM-DD.xlsx

Step 3 (curing_b2c.py):
  Phase 4 → GT-balance shift-by-shift curing simulation
           → 167 presses from testing_Daily_Running_Moulds (WCNAME format)
           → CO transitions from building Changeover Plan sheet
           → Pre-plan-start GT credited to opening balance
  Output: bc_curing_b2c.xlsx
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
| `MAX_CHANGEOVERS_PER_DAY` | **8** — set in `bc_config.py` (single source) | curing press changeovers only; **no limit on building machine COs**. Propagates to `building_b2c.py` and `curing_consumption_dynamic.py` automatically. |
| `CURING_CO_MIN` | 300 | changeover (press OCCUPIED whole shift) |
| `MOULD_CLEAN_MIN` | 120 | mould clean (press OCCUPIED whole shift) |
| `OPENING_GT_INVENTORY` | from DB | loaded from `gt_inventory_manual` at plan start |
| `OPENING_CARCASS_INVENTORY` | **0 (cold start)** | Stage-2 blocked until Stage-1 produces carcasses in this plan |
| `BUILDING_GT_CAP` | `min(press_consumption, demand/90)` | building output ≤ customer demand per SKU (per-shift target cap — Layer 1 of 3-layer enforcement) |
| `PER_SKU_DEMAND_CEILING` | `_gt_remaining[SKU]` (dynamic) | Hard LP constraint: Σ production ≤ remaining demand. Updates each day as production is drained. Prevents `OVERBUILD_BUFFER_FRAC` from allowing >100% fulfillment (Layer 3 of 3-layer enforcement) |
| `BUILD_LEAD_SHIFTS` | **3** — set in `bc_config.py` | building targets curing demand this many shifts ahead; 3 = 1 full day |
| `TOPUP_LOOKAHEAD_DAYS_GT` | **3** | idle-tail TopUp pre-builds up to 3 days ahead. Must equal `GT_SHELF_LIFE_DAYS = 3`. Value 1 was unnecessarily conservative — OVERBUILD_BUFFER_FRAC = 0.2 already handles cap collapse, so TopUp = 3 fills idle tails aggressively without violating the demand ceiling. |
| `OVERBUILD_BUFFER_FRAC` | **0.2** | fractional LP headroom above net curing demand per SKU per day. 0.0 caused LP cap to collapse to 0 on Days 2+ when TopUp pre-build partially covered the lead window; 0.2 keeps the LP active without violating the "total build ≤ 30-day demand" ceiling enforced by `gt_topup_target` |
| `BUILDING_START_OFFSET_SHIFTS` | -3 | building pre-start = 3 shifts (1 day) before curing Shift A |
| `BUILDING_PRE_SHIFT` | `Shift A of Day-1` | e.g. May 31 Shift A if curing starts June 1 |
| `PRE_START_SHIFTS` | **2** (set in `bc_config.py`) | Building starts 2 shifts before plan_start = Apr 30 Shift B (15:00). Pre-start GT is credited to the curing simulation's opening balance by `curing_b2c.py`. Prevents zero-inventory Day-1 starvation for RI SKUs. |
| `MIN_CAMPAIGN_MINS` | **120** — set in `bc_config.py` (base in `building.py` = 45) | Minimum production minutes per SKU campaign. Raised from 45→120 to prevent building CO explosion when many NRI SKUs activate simultaneously. |
| `Stage-2 CO multiplier` | **2.0×** (`building.py HybridDailyScheduler`) | `co_time_map` for Stage-2 machines uses `diff_CO_time × 2.0` (88 min → 176 min). Discourages LP from over-assigning SKU switches to Stage-2; routes CO-intensive work to VMI (20 min same-size CO) instead. |

---

## 16. Edge Cases

| ID | Edge Case | Handling |
|----|-----------|----------|
| EC-01 | Building capacity < Runner-In consumption | Produce at max; log CAPACITY_SHORTFALL; curing press → WAITING_GT |
| EC-02 | No viable CO target for Runner-Out press | STRANDED_PRESS; IDLE all cycle; logged |
| EC-03 | Daily CO budget exhausted (`MAX_CHANGEOVERS_PER_DAY`) | Defer lower-priority to next day; resort each day |
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
   effective output = `floor((480 - 120) / CT) × 2`. Accounted for when reading mould life from `testing_Daily_Running_Moulds`.
9. **NRI building starts without waiting for curing CO.** Building machines (including Unistage 7001/7002/7003) are assigned to NRI SKUs immediately. GT accumulates in inventory until the curing CO is executed. Curing CO limit = `MAX_CHANGEOVERS_PER_DAY`/day (set in `bc_config.py`) applies separately.

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

---

### 18.5 Starvation Root Cause Analysis — Synthetic Curing Plan

> **Architecture clarification:** "Zero starvation by design" holds ONLY in a
> fully-derived B2C (Phase 4), where the curing schedule is derived FROM the
> building output. The current implementation (Phase 1) uses a **synthetic**
> curing plan as the building target. Building must match it shift-by-shift.
> Any gap between building output and the synthetic plan appears as starvation
> in `StarvationValidator`. This is not a scheduler bug — it is a consequence
> of deferring Phase 4.

**Three starvation failure modes (from May baseline — 1,241 starvation events at 65.3% avg util):**

| Mode | Root Cause | Description | Fix |
|------|-----------|-------------|-----|
| Mode A — Machine idle | LP heuristic assigns machine to nothing | Some machines (7001, 7003, 6004) are not assigned to any SKU; they sit idle while curing runs its synthetic demand. Building output = 0 for those shifts → validator sees deficit. | Mould-constrained priority boost (implemented) forces LP to assign mould-limited SKUs first. |
| Mode B — Physical constraint | Too few building machines for the curing volume | E.g. 24 curing presses consume `24 × qps / shift` but only 3 building machines are assigned to that SKU → structural output gap. Even at 100% util, building can't match curing. | Assign more building machines to under-served SKUs; or implement Phase 4 (curing derives FROM building — physical constraint becomes the schedule, not a starvation). |
| Mode C — NRI CO timing gap | 2-shift buffer at CO start is insufficient | Building starts Shift A of CO Day; curing starts Shift C. That gives only 2 shifts of pre-built GT. If diff-size building CO (180 min) consumes most of Shift A, buffer shrinks and any LP under-production on those 2 shifts starves the press in Shift C. | NRI demand front-loading (implemented): 70% of demand targets the pre-CO window → LP urgently pre-builds GT buffer before CO day. |

**Why small building CT doesn't fix Mode A or B:**
Building CT is ~2–3 min/tyre vs curing CT ~17 min — building is 6–8× faster per machine.
Speed is not the problem. A machine producing at 100% speed but assigned to **no SKU** (Mode A)
or **too few machines assigned** (Mode B) still produces zero for those SKUs. Building CT only
determines per-shift output once a machine IS assigned to a SKU.

**Phase 4 is now active in `curing_b2c.py`.**
Curing schedule is derived after building completes — a curing press runs only when GT is
available in inventory. By construction: `Cure_Qty(shift S) ≤ GT_inventory(shift S)` always
→ WIP balance ≥ 0 → zero starvation events. Modes A and B become throughput constraints
(lower production), not starvation events.

**Open issue — CO over-aggressiveness (curing_consumption_dynamic.py):**
The CO scheduler can fire COs that transfer RI presses to NRI SKUs before the RI SKU's demand
is fully covered by remaining presses. Example: SKU with demand 18,913 and 4 presses has 3
presses CO'd to NRI targets by Day 3. Remaining 1 press can only cure 5,208 of 18,913 demand.
Fix: before CO'ing an RI press, check `remaining_demand / ((n-1) × rate_per_day) ≤ horizon`.

---

## 18. Demand Skew & KPI Ceiling Analysis (May 2026)

### 18.1 Machine group definitions (39 total building machines)

| Group | Machines | Count | Output | Inch constraint |
|-------|----------|-------|--------|-----------------|
| VMIMAXX | 6001–6004, 7001–7004 | 8 | GT direct | 14"–18" (hard-locked per machine to dominant) |
| BJ | 7101–7106, 7201 | 7 | GT direct | 13"/14"/15"/16" (hard-locked per machine) |
| UNI\_NARROW | 7501–7503 | 3 | GT direct | 12"/13" ONLY |
| STAGE2 | 8201, 8301, 8302, 8501, 8502, 7301 | 6 | GT (needs carcass) | Mixed |
| STAGE1 | 6801, 6802, 6803, 6909, 6911, 7601, 7701, 7801–7804, 8001–8003, 8101 | 15 | Carcass only | Mixed |

### 18.2 Exclusive demand bucket assignment

Rule: each SKU assigned to ONE bucket by priority VMIMAXX > BJ > UNI\_NARROW > STAGE2 > none.
Total must equal 694,973.

| Bucket | Machines | SKUs | Demand | Built GT | Gap | Coverage | Avg Util |
|--------|----------|------|--------|----------|-----|----------|----------|
| VMIMAXX | 8 | 39 | 239,156 | 211,808 | 27,348 | 88.6% | 55.3% |
| BJ | 7 | 22 | 249,633 | 228,931 | 20,702 | 91.7% | 83.2% |
| UNI\_NARROW | 3 | 7 | 56,717 | 44,193 | 12,524 | 77.9% | 58.4% |
| STAGE2 | 6 | 14 | 89,549 | 85,684 | 3,865 | 95.7% | 81.2% |
| No machine data | — | 7 | 59,918 | 8,417 | 51,501 | 14.0% | — |
| **TOTAL** | **39** | **89** | **694,973** | **579,033** | **115,940** | **83.3%** | |

Note: 8,417 built for "no machine data" = 2 Runner-In SKUs served via fallback path.

### 18.3 Structural ceiling analysis

| Group | Demand | Theoretical capacity | Demand/cap ratio | Status |
|-------|--------|---------------------|-----------------|--------|
| VMIMAXX (8) | 239,156 | **364,560** (6001–6004 at CT=1.0 min → 44,640 each; 7001–7004 at CT=0.96 min → 46,500 each) | 66% | Undersubscribed — spare capacity exists |
| BJ (7) | 249,633 | ~184,000 (at 1.7 min CT) | **136%** | **Oversubscribed — hard ceiling** |
| UNI\_NARROW (3) | 56,717 | ~89,280 (at 1.5 min CT) | 64% | Undersubscribed |
| STAGE2 (6) | 89,549 | ~107,136 (at 2.5 min CT) | 84% | Near-full |

BJ is the only group where demand physically exceeds machine capacity. The 20,702 BJ gap
is structural: even at 100% utilisation BJ machines can produce at most ~184k units.
The scheduler already offloads ~43k of BJ-bucket SKU production to Stage-2 (for SKUs
also eligible on Stage-2). The remaining gap needs either more BJ presses or VMI certification.

### 18.4 BJ SKU-level gap analysis (May 2026 actuals)

**BJ capacity breakdown (7 machines × 31 d × 3 shifts × 480 min = 312,480 min):**

| | Minutes | % of available |
|---|---|---|
| Production | 255,515 | 81.8% |
| CO overhead | 18,535 | 5.9% |
| Idle tail | 38,430 | 12.3% |
| **Units built** | **181,539** | |

The 38,430 min idle does NOT mean spare capacity exists — idle occurs at the end of campaigns when individual SKU demand is fully capped and no more BJ-eligible demand is left. It is NOT recoverable for oversubscribed RI SKUs.

**BJ-exclusive SKUs with unmet demand (run ONLY on BJ machines, no VMIMAXX/Stage-2 alternative):**

| SKU | Category | Demand | Built | Gap | Fulfillment | Root cause |
|-----|----------|--------|-------|-----|-------------|------------|
| `1225221715115SSTL0` | Runner-In | 13,106 | 7,709 | **5,397** | 58.8% | Curing-limited — too few presses; building correctly capped at curing throughput |
| `1225119015010QSTL0` | NRI | 1,744 | 1,233 | 511 | 70.7% | CO Day 25 — only 6 post-CO days remain; pre-CO buffer window too short |
| `1325119015008SRBT0` | NRI | 4,439 | 4,080 | 359 | 91.9% | CO Day 19 — late CO, limited post-CO build window |
| `1225219015010QSTL0` | NRI | 1,534 | 1,181 | 353 | 77.0% | CO Day 27 — only 4 post-CO days; nearly no build time |
| **Total BJ-exclusive gap** | | **20,823** | **14,203** | **6,620** | | |

**Curing-limited SKUs (run on BJ + other machines; gap = insufficient curing presses, not building capacity):**

| SKU | Category | Demand | Total Built | Gap | Fulfillment | Root cause |
|-----|----------|--------|-------------|-----|-------------|------------|
| `1325216814085STMX0` | Runner-In | 18,863 | 11,160 | 7,703 | 59.2% | 1 curing press — capacity ~11k/month |
| `1D25215013008SXC11` | Runner-In | 20,374 | 14,508 | 5,866 | 71.2% | Curing-limited |
| `1325119815106QBRQ0` | Runner-In | 11,581 | 7,997 | 3,584 | 69.1% | Curing-limited |
| `1325123715105SBRT0` | NRI | 5,846 | 4,380 | 1,466 | 74.9% | CO Day 13 — partial pre-build window |
| `1225170015010LSTL0` | Runner-In | 5,743 | 4,575 | 1,168 | 79.7% | Curing-limited |
| `1325121715100SBRT0` | Runner-In | 4,231 | 3,335 | 896 | 78.8% | Curing-limited |
| `1325121715115SLTB0` | NRI | 3,788 | 3,235 | 553 | 85.4% | CO Day 20 — limited window |

**Fix matrix:**

| Gap type | Size | Scheduler fix | Plant fix |
|----------|------|---------------|-----------|
| Curing-limited RI SKUs | ~20k | None — building already caps at curing throughput | Add curing COs to bring more presses onto these SKUs |
| NRI late CO timing | ~1,800 | Bump CO day earlier in `curing_consumption_dynamic.py` for these 3 SKUs | — |
| BJ structural oversubscription | ~5k residual | Certify BJ SKUs onto VMIMAXX machines | More BJ presses |

### 18.5 KPI improvement ceiling

| Source of gap | Gap units | Max recoverable by scheduler | Required fix |
|---------------|-----------|------------------------------|--------------|
| VMIMAXX scheduling overhead | 27,348 | ~10–15k | Longer campaigns, fewer COs |
| BJ structural capacity | 20,702 | ~5k | New BJ presses / VMI certification |
| UNI\_NARROW scheduling | 12,524 | ~5–8k | Better 12"/13" campaign allocation |
| STAGE2 (near ceiling) | 3,865 | ~1–2k | Marginal |
| No machine data | 51,501 | 0 | Master data certification only |
| **Total gap** | **115,940** | **~21–30k** | |

**Scheduler ceiling:** ~600–610k / 694,973 ≈ **86–88%** coverage
**True ceiling (all master data fixed):** ~630k+ / 694,973 ≈ **91%+** coverage

---

## 19. NEW ARCHITECTURE — Day-by-Day Rolling Simulation with Simultaneous Building + Curing

> **Status: Approved design — not yet implemented.**
> This supersedes the 31-day upfront LP planning approach described in §4.
> The old approach remains documented above for reference; the new approach is the target state.

---

### 19.1 Core Principle Changes

| Dimension | Old (31-day upfront LP) | New (day-by-day rolling) |
|-----------|------------------------|--------------------------|
| Horizon | Plan all 31 days at once | Plan one day at a time; roll forward |
| Building→Curing sync | Building pre-built 3 days ahead; curing derived afterward | Building and curing start **simultaneously** |
| GT pre-build window | 3 days (`TOPUP_LOOKAHEAD_DAYS_GT = 3`) | **1 shift** — build only what curing presses consume today |
| Building machine COs | Unlimited per day | **Max 2 COs per building machine per day** |
| One machine → one SKU | Yes (at any given time) | Yes — but max 2 COs allows 3 campaigns per machine per day (serving up to 3 curing press groups) |
| Utilisation target | Best-effort | **≥ 80% per building machine per shift** |
| Planning unit | Full 31-day horizon | Daily: Day D input → Day D building + curing → Day D+1 input |

---

### 19.2 Rolling Day Loop

For each day D (D = 1 … 31):

```
Step 1 — Curing Consumption for Day D
  Input:  press_state(D)      ← which SKU each press is currently running
          gt_inventory(D)     ← GT available at start of Day D
          demand_remaining(D) ← unmet demand per SKU entering Day D
  Output: curing_target(D)   ← per-SKU GT needed from building machines today

Step 2 — Building Schedule for Day D
  Input:  curing_target(D)
  Rules:
    • Building starts Shift A of Day D (simultaneous with curing — see §19.3)
    • For Runner-In SKUs whose building pre-started 2 shifts earlier (§19.4):
        use Shift A of Day D-1 pre-start GT already in inventory
    • Max 2 COs per building machine across the day
    • 80%+ utilisation target — machine must not go idle if any reachable
        demand SKU exists with a reachable CO cost < remaining shift time
    • TOPUP_LOOKAHEAD = 1 shift (not 3 days)
  Output: building_schedule(D)

Step 3 — Curing Schedule for Day D
  Input:  building_schedule(D)
          press_state(D)
          gt_inventory(D)    ← updated with today's building output (§19.3)
  Rule:   Curing starts AS SOON AS building produces (no wait — see §19.3)
  Output: curing_schedule(D)
          gt_inventory(D+1)  ← closing GT balance carried forward
          demand_remaining(D+1)
          press_state(D+1)   ← CO transitions applied

→ Repeat for Day D+1
```

---

### 19.3 Simultaneous Start — Why It Works

In the old approach building ran first, then curing derived from it. The new approach starts both together on Shift A of Day D.

**Why this is physically valid:**

```
Building CT ≈  2 min / tyre  (Unistage machines)
Curing  CT ≈ 17 min / tyre  (PCR press)

In one shift (480 min):
  1 building machine  →  480 / 2  =  240 tyres
  1 curing press      →  480 / 17 × 2 cavities  ≈  56 tyres

→  1 building machine produces enough GT to feed  240 / 56  ≈  4.3 curing presses
   continuously within the SAME shift.
```

Because building produces a tyre every 2 minutes and curing only needs one every 8.5 minutes (at 2 cavities), the curing press is never starved from the moment building starts. The 2-minute lag before the first GT unit is available is operationally negligible and modelled as zero.

**Inventory accounting:**
```
gt_inventory(shift S) += building_output(shift S)    ← updated in real time
curing_consumption(shift S) ≤ gt_inventory(shift S)  ← hard constraint unchanged
```

This eliminates the need for a GT pre-build buffer. `TOPUP_LOOKAHEAD_DAYS_GT` drops from 3 days to 1 shift.

---

### 19.4 Building Pre-Start for Curing COs (unchanged rule)

The simultaneous-start rule applies to **ongoing (steady-state) production**. For curing press COs the existing rule still applies:

```
Day D  Shift A:  Curing press  → CHANGEOVER (idle)
                 Building mach → START GT for new SKU   ← 2-shift pre-build
Day D  Shift B:  Curing press  → MOULD_CLEAN (idle)
                 Building mach → CONTINUE GT
Day D  Shift C:  Curing press  → PRODUCTION begins; 2 shifts of GT already in pool
```

For the rolling loop, when a CO is scheduled on Day D, building pre-start for the new SKU is entered on Day D itself (not a separate pre-horizon run). The 2-shift pre-build fills the GT inventory before Shift C.

---

### 19.5 Curing Press Assignment Priority (for CO target selection)

When selecting which curing press to assign to an NRI SKU (via CO from a Runner-Out press),
apply the following priority order:

```
STEP 1 — Sort candidate presses by cycle time (CT) ASCENDING
          Fastest press first -> maximises throughput for this SKU.

STEP 2 — If CT is equal:
          Prefer presses EXCLUSIVE to this SKU
          (mould can only cure this one SKU -- no other SKU eligible).
          Reason: exclusive presses have no opportunity cost.

STEP 3 — If press can cure MULTIPLE SKUs:
          De-prioritise it -- keep it available for flexibility.
          Assign only if no exclusive or lower-CT press is available.
```

Rationale: an exclusive press assigned to the correct SKU wastes no flexibility.
A flexible press assigned to one SKU blocks it from serving other SKUs later.

---

### 19.6 Building Machine CO Decision -- Dynamic Per-Shift Rule

The decision to CO a building machine to a second SKU within a shift is made
**dynamically at the start of each shift**, not pre-planned. Trigger condition:

```
For building machine M currently producing SKU_A:

  supply_today[SKU_A] = total GT committed for SKU_A by ALL eligible machines this shift
  demand_today[SKU_A] = press_count[SKU_A] x qty_per_press_per_shift

  IF supply_today[SKU_A] >= demand_today[SKU_A]:
      SKU_A is covered; M may CO to another SKU

  FOR each candidate SKU_B where:
      - M is eligible for SKU_B
      - demand_today[SKU_B] > supply_today[SKU_B]        (under-supplied)
      - CO cost (M: SKU_A -> SKU_B) <= 20% of remaining shift minutes
      - remaining_mins - CO_cost >= MIN_CAMPAIGN_MINS

      -> M COs to SKU_B; campaign = remaining_mins - CO_cost
      -> Prefer same_size_CO (same inch) over diff_size_CO
      -> Stop after 2 COs in the shift (MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT = 2)
```

A CO only fires when curing demand for another SKU exceeds building supply AND
the machine has already met its obligation to SKU_A. Never CO if it drops
production below the 80% floor.

---

### 19.7 CO Pre-Build Logic (Building Before Curing Press Starts)

Context: a curing press does CO on Day D Shift A, Mould Clean on Shift B,
starts production on **Shift C**. Building must have GT ready for Shift C.
Machines may be busy serving running SKUs in Shift A and B.

```
INPUTS:
  target_SKU     = SKU the press will cure from Shift C
  eligible_bld   = building machines eligible for target_SKU
  press_C_need   = floor(SHIFT_MINS / CT_cure) x CAVITIES_PER_PRESS
  GT_BUFFER_SHIFTS = 1  (default; 1 or 2 -- see section 19.8)
  build_target   = press_C_need x GT_BUFFER_SHIFTS
  already_built  = 0   (accumulates across Shift A + B)

FOR pre_shift in {Shift_A(Day_D), Shift_B(Day_D)}:
  FOR machine M in eligible_bld (sorted by CO cost ascending):

    -- PRIORITY CHECK -------------------------------------------------
    under_supplied = {
        sku : demand_today[sku] > supply_today[sku]
        for sku in running_skus
        if M in eligible_machines[sku]
    }
    IF under_supplied is NOT empty:
        M must serve the under-supplied running SKU first.
        SKIP M for pre-build in this shift. CONTINUE.

    -- IDLE / SPARE TIME CHECK ----------------------------------------
    free_mins = SHIFT_MINS - already_committed[M][pre_shift]
    co_cost   = CO_time(M.current_sku -> target_SKU)
                (same_size_CO if same inch, diff_size_CO otherwise)

    IF free_mins < co_cost + MIN_CAMPAIGN_MINS:
        CONTINUE   # not enough time

    -- BUILD QUANTITY -------------------------------------------------
    build_mins = free_mins - co_cost
    build_qty  = floor(build_mins / CT_build[target_SKU])
    build_qty  = min(build_qty, build_target - already_built)

    IF build_qty < MIN_CAMPAIGN_UNITS:
        CONTINUE   # campaign too small

    -- ASSIGN ---------------------------------------------------------
    Schedule M: CO -> target_SKU, produce build_qty in pre_shift.
    already_built          += build_qty
    supply_today[target_SKU] += build_qty

    IF already_built >= build_target:
        BREAK   # buffer complete

OUTCOME:
  already_built >= build_target  -->  press starts Shift C with full buffer  OK
  already_built <  build_target  -->  PARTIAL_PREBUILD flag; press starts Shift C
                                       GT-limited for first few cycles (acceptable --
                                       building continues producing in Shift C too)
```

Key rules encoded:
1. Running SKUs have strict priority over pre-builds.
2. A machine only pre-builds when it would otherwise be idle.
3. Build only what Shift C needs (press_C_need), optionally +1 shift buffer.
4. Multiple machines share the pre-build load if one is insufficient.
5. diff_size_CO pre-builds allowed only if no same-size machine is free.

---

### 19.8 GT Buffer Rule -- How Much to Pre-Build

```
GT_BUFFER_SHIFTS = 1   (default; configurable in bc_config.py)

Build target for any SKU in any shift =
    press_count[SKU] x qty_per_press_per_shift x GT_BUFFER_SHIFTS

  GT_BUFFER_SHIFTS = 1 -> build exactly what today's presses consume today
  GT_BUFFER_SHIFTS = 2 -> build today's + 1 shift extra (next-shift safety buffer)

Rules:
  1. Never build if demand_remaining[SKU] = 0 (demand fully met).
  2. Never build if no curing press is running or scheduled for this SKU
     within the current shift OR next shift.
  3. GT built beyond (GT_BUFFER_SHIFTS x press_consumption) is NOT allowed
     even if demand_remaining > 0 -- avoids shelf-life risk.
  4. GT shelf life = 3 days. With GT_BUFFER_SHIFTS = 1-2 (~0.33-0.67 day buffer),
     shelf life is never hit under normal operation.

Carry-over (GT overloaded):
  GT built in shift S but not consumed rolls to shift S+1 as opening inventory.
  This is expected -- one building machine feeds multiple consecutive curing shifts.
  Carry-over is bounded by GT_BUFFER_SHIFTS x press_consumption.
```

---

### 19.9 Starvation Prevention Guarantee

```
INVARIANT: Curing press for SKU X runs in shift S
           ONLY IF gt_inventory[X][S] >= qty_per_press_per_shift[X]

In rolling loop:
  At start of each shift S:
    FOR each press P running SKU X:
      IF gt_inventory[X] < qty_needed_this_shift:
          Press P does NOT cure this shift (GT not available).
          Building machine for X assigned immediately (top priority).
          Curing resumes next shift once GT is available.

  Curing is derived FROM building output -- it can never exceed available GT.
  Starvation (press runs with zero GT) = IMPOSSIBLE by construction.
```

Why old starvation events (1,241 baseline) occurred: the legacy architecture
used a SYNTHETIC curing plan that said "cure X units" even when building
hadn't produced them. In the rolling architecture there is no synthetic plan --
curing only consumes what building has actually produced this shift.

---

### 19.10 Building Machine CO Rule (max 2 per machine per shift)

Max 2 COs per building machine per shift. Driven dynamically by section 19.6.

```
Per shift:
  Campaign 1: SKU A (start of shift)
  CO 1:       switch to SKU B  (if demand_today[B] > supply_today[B])
  Campaign 2: SKU B
  CO 2:       switch to SKU C  (only if remaining time still warrants it)
  Campaign 3: SKU C

  Plant current average: 0.57 CO/shift/machine
  Maximum allowed:       2 CO/shift/machine

  CO type priority: same_size_CO first (20 min VMI, 45 min BJ)
                    diff_size_CO only if no same-inch under-supplied SKU exists
```

80% utilisation floor (target, not override of demand cap):

```
  At 1 same_size_CO VMI (20 min):   production = 460 min -> 95.8%  OK
  At 2 same_size_CO VMI (20 min x2):production = 440 min -> 91.7%  OK
  At 2 diff_size_CO BJ (90 min x2): production = 300 min -> 62.5%  BLOCKED

  EXCEPTION: if demand_remaining = 0 for all reachable SKUs, machine is
  allowed below 80% and eventually idle. Demand cap always beats 80% floor.
  80% floor applies to GT machines ONLY -- NOT Stage-1 carcass machines
  (Stage-1 is structurally ~47% by design with 15 machines for 11.5-equiv demand).
```

---

### 19.11 Parameter Changes from Old to New Architecture

| Parameter | Old value | New value | Reason |
|-----------|-----------|-----------|--------|
| `TOPUP_LOOKAHEAD_DAYS_GT` | 3 days | 1-2 shifts | Build only for current+next shift |
| `GT_BUFFER_SHIFTS` | implicit 3 days | **1** (default) / 2 | Exact shift-level buffer control |
| `GT_SHELF_LIFE_DAYS` | 3 | 3 (unchanged) | Physical limit; rarely hit with 1-2 shift buffer |
| `PRE_START_SHIFTS` | 2 (global) | 0 steady-state; 2 CO pre-build only | No global pre-start; only CO presses need it |
| `MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT` | unlimited (0.57/shift actual) | **2** | Hard cap; dynamic trigger via section 19.6 |
| `MIN_SHIFT_UTILISATION` | not set | **0.80** | GT machines only; yields to demand cap |
| `BUILD_LEAD_SHIFTS` | 3 | **0** (steady-state) | Simultaneous building + curing |
| `OVERBUILD_BUFFER_FRAC` | 0.2 | **0** | Replaced by GT_BUFFER_SHIFTS |
| `CO_urgency_scoring` | Once for 31 days | Re-scored each Day D | horizon_left shrinks daily |

---

### 19.12 Implementation Scope

| File | Change required |
|------|----------------|
| `curing_consumption_dynamic.py` | Per-day function; re-score CO urgency daily using `horizon_left = 31 - D`; use previous day's actual curing output as input |
| `building_b2c.py` | Per-shift scheduler; section 19.6 dynamic CO trigger; section 19.7 CO pre-build logic; GT_BUFFER_SHIFTS instead of TOPUP_LOOKAHEAD; MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT = 2 |
| `curing_b2c.py` | Per-shift simulation; section 19.9 starvation prevention; shift-level GT carry-over |
| `b2c_pipeline.py` | Rolling loop: `for day in range(31): for shift in [A,B,C]:` |
| `bc_config.py` | Add `GT_BUFFER_SHIFTS = 1`, `MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT = 2`, `MIN_SHIFT_UTILISATION = 0.80`; update comments |

---

## 20. KPI Comparison — Old Architecture vs New Architecture (May 2026)

> **Old architecture:** 31-day upfront LP; building first, curing derived after; TOPUP = 3 days.
> **New architecture:** Day-by-day rolling loop; simultaneous building + curing; max 2 COs/shift; TOPUP = 1 shift.
> Numbers marked **[ACTUAL]** are measured from the May 2026 run. Numbers marked **[PROJECTED]** are estimates based on structural analysis.

### 20.1 Building Schedule KPIs

| KPI | Old Arch [ACTUAL] | New Arch [PROJECTED] | Change | Driver |
|-----|-------------------|----------------------|--------|--------|
| Customer demand | 653,138 | 653,138 | — | Fixed |
| GT built | **594,384** | **~610–625k** | +16–31k | Better VMI/UNI util from 2-CO-per-shift |
| Demand fulfillment | **91.0%** | **~93–96%** | +2–5 pp | Scheduler-recoverable gap reduced |
| GT machine avg utilisation | **71.7%** | **~80–83%** | +8–11 pp | 80% floor enforced per shift |
| Machines ≥ 80% util | **10 of 24** | **~18–20 of 24** | +8–10 | Floor rule + 2-CO distribution |
| Machines < 60% util | **9 of 24** | **~2–4 of 24** | −5–7 | VMI/UNI now fill idle tail via COs |
| Stage-1 avg utilisation | **47.4%** | **~47–50%** | ~0 | Structural (15 machines for 11.5-equiv demand) |
| True idle (avail − prod − CO) | **21.8%** (233,682 min) | **~12–15%** | −7–10 pp | 80% floor absorbs most idle tail |
| CO overhead (GT machines) | **6.5%** (69,853 min) | **~8–10%** | +1.5–3.5 pp | More COs (0.57 → ~0.8–1.2 per shift) but same-size only |
| Avg COs per machine per shift | **0.57** | **~0.8–1.2** | +0.2–0.6 | Up to 2 CO/shift; actual depends on press demand |
| Total building COs | **1,278** | **~1,600–2,200** | +300–900 | More in-shift COs for multi-SKU feeding |

**Structural ceiling (cannot improve via scheduling):**
- BJ oversubscription: ~20k gap — needs more BJ presses
- No-machine-data SKUs: ~51k gap — needs master data certification
- Curing-limited RI SKUs: ~20k gap — needs curing COs to add presses

---

### 20.2 Curing Schedule KPIs

| KPI | Old Arch [ACTUAL] | New Arch [PROJECTED] | Change | Driver |
|-----|-------------------|----------------------|--------|--------|
| GT cured | **558,218** | **~595–610k** | +37–52k | Better GT→Curing conversion |
| Customer demand fulfillment | **85.5%** | **~91–94%** | +5.5–8.5 pp | GT supply matched to curing better |
| GT → Curing efficiency | **93.9%** | **~97–98%** | +3–4 pp | Simultaneous start eliminates timing waste |
| Closing GT balance (horizon end) | **~36,166** | **~8–12k** | −24k | No pre-build surplus; only DEMAND_MET carry-over |
| GT wasted (NO_PRESS SKUs) | **10,729** | **~0–2k** | −9k | NRI-no-CO filter already coded |
| Curing press avg utilisation | **70.8%** | **~75–80%** | +4–9 pp | Consistent GT supply; less starvation wait |
| Presses ≥ 80% util | **74 of 167** | **~95–110 of 167** | +20–36 | GT flows immediately; no starvation gaps |
| Curing press idle | **26.3%** (1,962,076 min) | **~18–22%** | −4–8 pp | GT available in-shift; presses wait less |

---

### 20.3 Key Trade-offs in New Architecture

| Aspect | Old | New | Net verdict |
|--------|-----|-----|-------------|
| CO count on building machines | 1,278 total / 0.57 per shift | ~1,600–2,200 / 0.8–1.2 per shift | **Acceptable** — all same-size; overhead ≤ 8–10% |
| GT pre-build buffer | 3 days (may spoil) | 1 shift (consumed same day) | **Better** — zero shelf-life waste |
| Curing starvation risk | Higher (GT pre-built then consumed) | Near-zero (building feeds curing in real-time) | **Better** — simultaneous eliminates lag |
| Planning complexity | Simple sequential (3 steps) | Day-by-day loop (more complex) | **Trade-off** — higher code complexity, better KPIs |
| 80% util enforcement | Best-effort | Hard floor per shift | **Strict improvement** — no machine goes below 80% unless structurally forced |

---

### 20.4 What New Architecture Cannot Fix (requires plant/data action)

| Gap | Size | Required action |
|-----|------|----------------|
| BJ structural oversubscription | ~20k | Add BJ curing presses or certify BJ SKUs on VMI |
| No-machine-data SKUs (7 SKUs) | ~51k | Add to `Master_Building_Allowable_Machines_source` |
| Curing-limited RI SKUs | ~20k | Schedule curing COs to add presses for these SKUs |
| **Permanent ceiling (scheduler only)** | **~91k gap remains** | — |
| **Achievable with new arch + no plant action** | **~562–571k / 653k = 86–87% curing** | Up from 85.5% |
| **Achievable with new arch + all plant actions** | **~630k+ / 653k = 96%+** | All structural gaps closed |
