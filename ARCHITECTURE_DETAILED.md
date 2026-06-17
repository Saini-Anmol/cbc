# CBC Scheduler — Detailed Technical Architecture

End-to-end design of the **C**uring → **B**uilding → **C**uring scheduler for
the JK Tyre BTP PCR line: where data comes from, how each stage processes it,
and how the stages are chained so curing presses no longer starve.



---

## 1. System context

Tyre production is two linked stages:

```
        BUILDING                         CURING
   (makes the green tyre)          (cures the green tyre)
   Stage1 carcass ─► Stage2/        presses consume GT
   Unistage green tyre (GT)   ──►   produced upstream
```

Two schedulers already exist and are kept intact:

- **`curing_lp.py`** — Linear-Program curing scheduler (v4). Allocates press
  minutes to SKUs over a 31-day horizon.
- **`building.py`** — Hybrid GA + LP building scheduler (v8). Rolling day-by-day
  over an 3-day (or month) horizon, two sub-stages (carcass + GT).

The problem they had: curing planned against curing capacity only, so it
assigned presses SKUs their feeding building machines couldn't supply →
**starvation**. CBC inserts a **feed-awareness** layer so curing only commits
SKUs that are buildable by each press's feeders.

Three new modules add this without modifying the two schedulers:

| Module | Adds |
|--------|------|
| `feed_map_builder.py` | learns press→feeder-machine map from history |
| `feed_aware_curing.py` | filters curing eligibility by feed feasibility |
| `cbc.py` | orchestrates Phase 0 → curing → building |

---

## 2. End-to-end data flow

```
 ┌────────────────────────── DATA SOURCES ──────────────────────────┐
 │  MySQL  jkplanningV1 @ 35.208.174.2     +   CSV / Excel files      │
 └───────────────────────────────────────────────────────────────────┘
        │                          │                         │
        ▼                          ▼                         ▼
 ┌─────────────┐         ┌──────────────────┐      ┌──────────────────┐
 │ PHASE 0     │         │  CURING (C)      │      │  BUILDING (B)    │
 │ FEED map    │         │  curing_lp.py    │      │  building.py     │
 │ builder     │         │                  │      │                  │
 │ events+     │ feed_   │  ETL ─► continuity│ cur. │  ETL ─► demand   │
 │ masters ──► │ map ──► │  ─► LP ─► round  │plan  │  ─► GA+LP (carc) │
 │ feed_map.   │ .json   │  ─► shift sched  │────► │  ─► GA+LP (GT)   │
 │ json/xlsx   │         │  + FEED filter   │      │  ─► seq ─► build │
 └─────────────┘         └──────────────────┘      │  ─► topup ─►     │
        ▲ periodic            │ Excel out          │     validate     │
        │ (history)           ▼                    └──────────────────┘
        │              PCR_Curing_CBC_FeedAware.xlsx        │ Excel out
        │                     │  (handoff: curing_plan_for_building.xlsx)
        └─────────────────────┴──────────────────────────► PCR_Building_CBC.xlsx
```

Phase 0 runs **periodically** (the feed map is stable). Curing and building run
**every planning cycle**; building consumes the curing plan.

---

## 3. Data-fetch layer

### 3.1 Database — MySQL `jkplanningV1` @ `35.208.174.2`

Connection (both schedulers): `mysql+pymysql://<user>:<pwd>@<server>/jkplanningV1`.

**Read by CURING (`curing_lp.py` → `ETL`):**

| Table | Key columns used | Produces |
|-------|------------------|----------|
| `Master_Curing_Design_CycleTime` | `Sapcode`, `Cure Time` | cycle time per SKU → `(Cure+buffer)/efficiency` |
| `Master_Curing_Allowable_Machines_source` | `SKU Code`, machine columns (`Yes`) | SKU → eligible curing presses |
| `gt_inventory_manual` | `sizeCode`, `gtInventory` | opening GT inventory per SKU |
| `Master_WC_Master` | `wcID`, `WCNAME` | work-center naming |
| `Daily_Running_Moulds` | `WCNAME`, `Side`, `Sapcode`, `Current MouldNo`, `Mould life` | currently running SKU + mould life per press → **continuity** |
| `Master_Mapping_Mould_SKU` | `Active Flag`, `Mould`/`MouldNo`, `Matl.Code` | mould ↔ SKU compatibility |

**Read by BUILDING (`building.py` → `ETL`):**

| Table | Key columns used | Produces |
|-------|------------------|----------|
| `gt_inventory_manual` | `sizeCode`, `gtInventory` | opening GT inventory |
| `carcass_inventory_manual` | `sizeCode`, `CarcassInv` | opening carcass inventory |
| `Master_Building_Allowable_Machines_source` | `SKU Code`, machine cols | SKU → eligible building machines |
| `Master_Building_ChangeoverTime` | `MachineCode`, `Same Size(Minutes)`, `Different Size(Minutes)` | per-machine changeover times |
| `Master_Curing_Allowable_Machines` | `SKUCode`, `Size` | SKU → size (drives inch-lock & CO) |
| `TBMStage1_ProductionEventData` | `WorkCenter`, `RecipeCode`, `DtAndTime` | currently running Stage-1 machines |
| `TBMStage2_ProductionEventData` | `WorkCenter`, `RecipeCode`, `DtAndTime` | currently running Stage-2 machines |

### 3.2 File inputs

| File | Used by | Columns | Purpose |
|------|---------|---------|---------|
| `Book4(Sheet4).csv` | curing | `SKUCode`, `Updated_Requirement`, `ConsolidatedPriorityScore` | curing demand + priority |
| `jkt_plan.csv` | building (default) | `skuCode`,`startTime`,`endTime`,`qty` | curing plan building must satisfy — **replaced by the feed-aware curing output under CBC** |
| `master_building_stage1_best_machine.csv` | building | `MachineNo`,`sizeCode`,`count` | 3-month history → GA seed |
| `master_building_stage2_best_machine.csv` | building | `MachineNo`,`sizeCode`,`count` | 3-month history → GA seed |
| `curing_events.xlsx` | feed map | `wcID`,`recipe id`,`dt` | which press cured which recipe |
| `building_events.xlsx` | feed map | `workcenter`,`recipecode`,`dt` | which machine built which recipe |
| `recipe_master.xlsx` | feed map | `recipe id`,`recipecode` | id → code bridge |
| `wc_master.xlsx` | feed map | `wcID`, press code | wcID → curing press |
| `building_name_map.xlsx` | feed map (optional) | `workcenter`, machine code | else built-in S1/S2 map |
| `building_allowable.xlsx` | feed-aware curing | `SKUCode`,`Machines` | SKU → building machines (for feasibility) |

### 3.3 Files produced

| File | Producer | Contents |
|------|----------|----------|
| `feed_map.json` / `feed_map.xlsx` | Phase 0 | press → feeder machines |
| `feed_map_inverse.xlsx` | Phase 0 | machine → presses (contention) |
| `feed_coverage.xlsx` | Phase 0 | validation: % cured recipes a feeder covered |
| `PCR_Curing_CBC_FeedAware.xlsx` | curing | full curing schedule (5 sheets) |
| `curing_plan_for_building.xlsx` | cbc.py | bridge file handed to building |
| `PCR_Building_CBC.xlsx` | building | full building schedule |

---

## 4. Curing scheduler (`curing_lp.py`) — internal pipeline

Horizon: 31 days × 3 shifts × 8 h. Shifts A/B/C start 07:00. 2 moulds/press,
2 cavities/mould. Changeover 300 min, mould clean 120 min.

```
Phase 0  ETL            pull the 6 inputs (§3.1), clean, normalise codes
Phase 1  Prepare SKUs   per SKU: cycle time, eligible presses, demand-minutes,
                        schedulable? (has cycle time + machines + moulds)
Phase 2  Continuity     from Daily_Running_Moulds: a press already running a SKU
                        that STILL has demand keeps running it. These press-
                        minutes are "locked" and removed from LP capacity; any
                        demand the running presses can't cover spills to the LP.
Phase 3  LP solve       scipy HiGHS. Variables = press-minutes per (SKU,press)
                        + unmet-demand slack. Objective: minimise unmet demand
                        (+ tiny changeover penalty). Constraints: press capacity
                        (minus locked), demand coverage. Eligibility bounds set
                        by MouldTracker  ◄── FEED FILTER HOOKS HERE (§7)
Phase 4  Rounding       convert continuous minutes to whole cure cycles; charge
                        changeover only for SKUs actually kept; greedy top-up of
                        residual capacity by priority.
Phase 5  Shift schedule walk each press from plan start; insert CHANGEOVER and
                        MOULD_CLEAN rows; split runs at shift boundaries.
Export                  Excel: Demand Fulfilment, Machine Schedule, Shift
                        Schedule, Machine Utilisation, Mould Tracker.
```

Key objects: `Config` (knobs), `MouldTracker` (mould ↔ SKU ↔ press eligibility),
`LP_Solver`, `Rounder`, `ScheduleBuilder`, `ExcelExporter`,
`JK_LP_Curing_Scheduler_v2` (orchestrator). Entry: `run_from_excel(...)` /
`run_from_database(...)`.

---

## 5. Building scheduler (`building.py`) — internal pipeline

Hybrid **GA (outer) + LP (inner)**, run **day-by-day** rolling, inventories and
running-machine state carried forward. Machine groups: `STAGE1` (carcass),
`STAGE2` + `UNISTAGE` (green tyre). Building day D produces GT for curing day
D+1 (`LEAD_DAYS = 1`).

```
Phase 0  ETL            curing plan (jkt_plan.csv → under CBC, the feed-aware
                        curing output), GT & carcass inventory, building
                        allowable, changeover map, SKU sizes, running machines,
                        3-month history map (GA seed).

Per planning day:
  1 Demand derive       overlap curing rows onto shifts → per-(SKU,shift) GT
                        demand; net of opening GT inventory.
  2 Inch-lock           Stage1/Unistage presses are size-restricted; assign each
                        a size (coverage-then-balance, + per-SKU rescue).
  3 GA + LP (GT)        GA evolves binary y[SKU,machine] (seeded from history);
                        for each chromosome the LP solves minute allocation
                        x[SKU,machine,shift] meeting cumulative WIP via slack,
                        respecting capacity, min-campaign, and a changeover
                        reserve. Fitness = LP obj + CO penalty − diversity bonus.
  4 Carcass demand      Stage-2 GT output → Stage-1 carcass demand (net of inv).
  5 GA + LP (carcass)   same engine on STAGE1 machines.
  6 Sequence + build    order campaigns per machine to minimise changeovers;
                        insert CHANGEOVER rows; split at shift boundaries.
  7 TopUp idle tails    fill leftover machine time pre-building near-future
                        demand, bounded by shelf life (GT 3 d, carcass 1 d).
  8 Starvation validate per-(date,shift,SKU) WIP balance: opening + built −
                        cured. <0 = STARVATION, low = WARNING.
  9 Roll forward        update GT/carcass inventory + running machines for D+1.

Reports + Excel         Demand Summary, Shift Schedule, GT & Carcass Machine
                        Schedules, Changeover Analysis, Starvation, Utilisation,
                        Curing Demand Matrix, Daily SKU Counts.
```

Key objects: `Config`, `GAConfig`, `ETL`, `DemandDeriver`, `LPMinuteSolver`,
`GeneticOptimiser`, `CampaignSequencer`, `ScheduleBuilder`,
`StarvationValidator`, `ExcelExporter`, `HybridDailyScheduler`. Entry:
`run_from_database_hybrid(...)`.

---

## 6. FEED map builder (`feed_map_builder.py`) — Phase 0

**Goal:** discover which building machines feed which curing press, since no
documented mapping exists.

**Fetch + normalise** (see §3.2): load curing events and building events; resolve
`recipe id → recipecode` (recipe master), `wcID → press` (WC master),
`workcenter → machine` (name map). Each event becomes `(recipecode, node,
weight)`; `weight = 0.5^(age_days/HALFLIFE)` when recency is on.

**Score** — co-production of the same recipe is evidence of a feeder link,
weighted by exclusivity so widely-built recipes count less:

```
exclusivity(r) = 1 / (n_presses(r) × n_machines(r))
score[Press][Machine] = Σ_r  exclusivity(r) × min(cure_weight[r,P], build_weight[r,M])
```

**Select** feeders per press: rank by score; keep if
`score ≥ SCORE_CUTOFF_FRAC × best` (and within `TOP_N_FEEDERS` if set).

**Validate** — `feed_coverage.xlsx`: per press, % of its cured recipes that at
least one chosen feeder historically built. <80% ⇒ widen the feeder set or a
feeder is missing from the data.

**Outputs:** `feed_map.json` (consumed by curing), `.xlsx`, inverse map,
coverage report.

---

## 7. Feed-aware integration (`feed_aware_curing.py`)

Curing already decides eligible presses for a SKU in:

```python
MouldTracker.get_eligible_machines_with_moulds(sku, candidate_machines, continuity_machines)
```

`install_feed_awareness(curing_lp, feed_map, building_allowable)` **wraps** this
method (monkeypatch — no edits to `curing_lp.py`). The wrapper keeps a press for
a SKU only if:

```
P is a continuity press for this run            (already running → feed exists)
  OR  FEED[P] ∩ building_allowable[SKU] ≠ ∅      (a feeder can build it)
```

Safeguards: continuity presses exempt; never strips a continuity press to empty;
permissive on missing data (`KEEP_UNKNOWN_PRESS`/`KEEP_UNKNOWN_SKU`); code
normalisation (`4401` == `"4401"` == `"4401.0"`). Net effect: infeasible
(press, SKU) pairs get a zero eligibility bound, so the LP cannot place them.

---

## 8. CBC orchestration (`cbc.py`) — end-to-end run

```
run_cbc(cfg):
  Phase 0  feed map      build_feed_map()  (or load feed_map.json if reusing)
  Phase C  curing        load building_allowable.xlsx
                         install_feed_awareness(curing_lp, feed_map, bld_allow)
                         curing_lp.run_from_excel(... )  ──► PCR_Curing_CBC_FeedAware.xlsx
                         export shift_schedule ──► curing_plan_for_building.xlsx
  Phase B  building      import building
                         building.run_from_database_hybrid(...) ──► PCR_Building_CBC.xlsx
```

**Handoff detail (important):** building's `ETL.load_curing_schedule` currently
reads `jkt_plan.csv`. Under CBC, point it at `curing_plan_for_building.xlsx`
(the feed-aware curing output) so building schedules against the achievable
plan. `cbc.py` writes that bridge file automatically; the one wiring step is to
make `load_curing_schedule` read it.

All paths/flags live in `CBCConfig` (curing inputs, plan start, building
allowable, `REBUILD_FEED_MAP`, `RUN_BUILDING`).

---

## 9. Operations

| Step | Command | Cadence |
|------|---------|---------|
| Refresh feed map | `python feed_map_builder.py` | periodic (monthly / on layout change) |
| Full CBC run | `python cbc.py` | each planning cycle |
| Curing only | `curing_lp.run_from_excel(...)` | ad-hoc |
| Building only | `building.run_from_database_hybrid(...)` | ad-hoc |

Reuse an existing feed map with `REBUILD_FEED_MAP=False`. For an A/B starvation
check, `uninstall_feed_awareness(curing_lp)` reverts to the original eligibility.

---

## 10. Assumptions & failure modes

- **Code alignment** — press codes in `wc_master` must equal the curing machine
  codes (`4401…`); building machine codes must match across feed map and
  building-allowable. Mismatches drop rows silently in the join (load prints
  before/after counts; `feed_coverage.xlsx` surfaces the impact).
- **Recipe key** — `recipe id → recipecode` and `recipecode` in building events
  must be the same coding; unresolved events are dropped.
- **Permissive defaults** — when feed data is missing, the filter keeps the
  option (avoids wrongly idling a press). Set `KEEP_UNKNOWN_*=False` for strict
  mode once the map is trusted.
- **What CBC fixes** — structural starvation (a press's feeders can't build the
  SKU). **What it doesn't yet** — fine shift-level timing / shared-feeder bursts;
  add a cumulative build-≥-cure-per-shift constraint (LP-coupled or iterated
  C↔B) in a later phase if needed.
- **Data freshness** — running-machine and inventory snapshots must be current
  at run time; both schedulers roll state forward from them.
