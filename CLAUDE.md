# CBC / B2C Scheduler — Agent Context for Logic & Approach Brainstorming

This file gives a terminal AI agent enough context to reason about scheduling
logic trade-offs for the **JK Tyre BTP PCR line**. Read this before answering
any "should we change X" or "what should we do about Y" question.

---

## What this system does (one paragraph)

There are two production stages: **Building** (makes green tyres, GT) and
**Curing** (vulcanises GT into finished tyres). The B2C scheduler runs
**Building first** and derives Curing from it. Curing consumes exactly what
Building produces — starvation is zero by architectural design (only once Phase 4
Curing Derivation is active; Phase 1 uses a synthetic curing plan so starvation
can still appear). Building machines are constrained by a strict per-SKU demand
cap: total build across the horizon ≤ 100% of customer demand. There are
39 building machines (15 Stage-1 carcass, 6 Stage-2 GT, 18 Unistage GT), 170
curing presses (80 active). The planning horizon is 31 days × 3 shifts
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
4. LP `co_time_map` uses "same" time for VMI (not "diff") so LP naturally allocates SKU switches to VMI first.

### Multi-press feeding from one building machine — NOT POSSIBLE (confirmed plant data)
Same size ≠ same GT recipe. Each SKU has a unique compound + bead + construction.
GT built for SKU A cannot be cured as SKU B even if both are 16".
**One building machine always produces for exactly one SKU at a time.**
`same_size_CO` is still used when switching between same-size SKUs — it is cheaper
(20 min on VMI) because the mould size doesn't change, but the recipe does.

### DemandHeuristicAssigner sort key (building.py)

Machines are sorted for each SKU in this priority order (lowest score wins):

| Tier | Key | Effect |
|------|-----|--------|
| TIER 0 | `0 if dominant_inch == sku_inch else 1` | Routes 16" SKUs to VMI, 15" to BJ, etc. |
| TIER 1 | `_inch_mach_dmins[(inch, machine)]` | Demand-minutes balance: same-inch siblings (e.g. 6001/7002/7004) share 14" demand by volume not count |
| TIER 2 | `−demand_frac` (UNISTAGE only) | High-priority SKUs prefer UNISTAGE |
| TIER 3 | `mach_elig_count` | More specialised machines (fewer eligible SKUs) get priority |
| TIER 4 | `m_idx[m]` | Stable tiebreaker |

**History bias removed (was TIER 4).** Prior 3-month run history encoded old wrong-inch routing (7001 on 15", 6001 on mixed inches), causing the heuristic to perpetuate the problem. Replaced by demand-minutes round-robin which distributes inch demand equitably by volume.

**Demand-minutes round-robin (TIER 1):** `_inch_mach_dmins` tracks total demand-minutes assigned per (inch, machine) pair. When two same-dominant-inch machines compete for a SKU, the one with fewer assigned demand-minutes wins. After assignment, `demand_mins` is added to the winner's counter. This gives true demand-proportional load balancing across siblings regardless of SKU demand size.

### Mould-constrained SKU priority
Sort Phase 1a building by `current_days = rem_demand / (press_count × rate_per_day)` descending.
A SKU with few moulds has lower rate → higher `current_days` → starts building first.
This prevents late discovery that a slow-throughput SKU can't meet demand within the horizon.

**Priority boost formula (implemented in `building_b2c.py` after `priority_map` construction):**
```
multiplier = min(1.0 + current_days / PLANNING_DAYS, 4.0)
priority_map[SKU] *= multiplier    (applied only to Runner-In SKUs with multiplier > 1.01)
```
Maximum 4× boost for SKUs where `current_days ≥ 3 × planning_horizon`.

---

## Key invariants the agent must never break

1. **Demand cap is sacred.** Total GT built for any SKU ≤ `Demand_Qty`. Enforced
   in three layers: `_gt_remaining` tracker, daily `cur_mat` clip, LP ceiling
   constraint. Any proposed change must preserve these.
2. **Max 8 curing press changeovers per day.** Hard plant limit. Building
   machine changeovers have NO cap.
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
event** — mould clean is absorbed into the CO window (Shift A = CO, Shift B =
Mould Clean). The scheduler does not need to track cycle counts or trigger
mould-clean events independently.

### Changeover timing — building MUST start simultaneously with curing CO

> **Key logic (confirmed update):** When a curing press starts a changeover
> (CO) to a new target SKU on Day D Shift A, the building machine(s) for that
> target SKU must ALSO start producing GT in **Shift A of Day D** — not Shift B,
> not Shift C.

Rationale: the curing press is idle for 2 full shifts (Shift A = CO, Shift B =
Mould Clean). If building waits until Shift B or Shift C to start, those shifts
of GT production are lost. Starting building at Shift A means 2 full shifts of
GT are pre-built and sitting in inventory by the time the curing press fires up
in Shift C. This eliminates any Day-1 starvation risk for the new SKU.

```
Day D  Shift A:  Curing press  → CHANGEOVER (300 min, OCCUPIED)
                 Building mach → START producing GT for new SKU   ← simultaneous
Day D  Shift B:  Curing press  → MOULD_CLEAN (120 min, OCCUPIED)
                 Building mach → CONTINUE producing GT
Day D  Shift C:  Curing press  → PRODUCTION begins (new SKU)
                 Building mach → CONTINUE producing GT
                 GT inventory  → 2 full shifts pre-built = immediate feed
```

This applies to ALL changeover types:
- Runner-Out press switching to a demand SKU (Phase 2a)
- Runner-In press freed after demand fulfilled, switching to NRI SKU (Phase 2b)
- Any NRI SKU assigned a curing press (Phase 1b)

**Implementation note:** Phase 2b currently sets building start to Shift B of
Day D. This should be corrected to Shift A of Day D. The `active_press_count`
update for the new SKU (`+= 1` from Shift C) is unchanged — only the building
machine start time moves earlier.

---

## SKU categories (Phase 0 classification)

| Category | Definition | Count (illustrative) | Building approach |
|----------|------------|---------------------|-------------------|
| Runner-In (RI) | On a curing press + in demand | ~55 SKUs | Phase 1a — first priority |
| Runner-Out (RO) | On a curing press + NOT in demand | ~25 SKUs | Candidates for press CO to a new SKU |
| Non-Runner-In (NRI) | NOT on any curing press + in demand | ~55 SKUs | Phase 1b (joint pool) — residual capacity |

---

## Building machine groups

```
Stage-1  (15 machines: 6801, 6802, 6803, 6909, 6911, 7601, 7701, 7801–7804, 8001–8003, 8101)
  → Output: Carcass (semi-finished). Feeds Stage-2 only.

Stage-2  (6 machines: 8201, 8301, 8302, 8501, 8502, 7301)
  → Output: GT (requires Stage-1 carcass as input). BOTTLENECK (6 vs 15 Stage-1).

Unistage (18 machines: 6001–6004, 7001–7004, 7101–7106, 7201, 7501–7503)
  → Output: GT. Independent — no Stage-1 dependency.
  → Subgroup 7001–7004: 48 allowed SKUs, demand 224k, physical cap ~58–69k.
     These 4 machines are structurally over-subscribed and suffer from
     excessive CO (30–32 COs per machine per month → 45% time in CO).
```

## Inch-Run Study — Machine Group Inch Policies (CONFIRMED from May plant data)

The 18 Unistage machines are NOT a homogeneous group. They belong to 3 distinct MG groups
with hard inch constraints. Treat each group's inch policy as a **hard scheduler constraint**.

```
MG Group     | Machines              | Allowed inches   | Policy
-------------|----------------------|------------------|-----------------------------
VMIMAXX      | 6001–6004, 7001–7004 | 14"–18"          | Flexible overflow absorber
BJ           | 7101–7106, 7201      | 13", 14", 15", 16"| Well-locked (83–99% dominant)
TWO STAGE TBM| Stage-1 + Stage-2    | 12", 15", 13"    | ~Half single-inch machines
UNISTAGE     | 7501, 7502, 7503     | 12", 13" ONLY    | HARD — never assign 14"+
```

**Per-machine dominant inch (soft-lock seed — confirmed by study):**
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

**Root cause of 7001–7004 low utilisation (25–28%):** each machine runs 5–7 different
inches → 30–32 COs/month → 45% time in CO. Fix: restrict each to its dominant inch.

## Hard Per-Machine Inch Locking (`_MACHINE_HARD_INCH`) — IMPLEMENTED

Each machine is hard-locked to its dominant inch(es) in `building_b2c.py`.
Applied AFTER `_UNISTAGE_INCH_POLICY` group filter, BEFORE heuristic assigner.

| Machine | Hard-locked inches | Reason |
|---------|-------------------|--------|
| 7001 | 16" only | Strict {16} — same elig_count as 6004/7201 so TIER-3 does not deprioritise it |
| 7002 | 14" only | Dominant; prevents 15" overflow eating 14" capacity |
| 7004 | 14" only | Dominant |
| 6001 | 14" only | Dominant; was getting 3% util when wrong-inch SKUs assigned |
| 6002 | 15" only | Dominant |
| 7003 | 15" only | Dominant |
| 6003 | 17"/18" only | Dominant |
| 6004 | 16" only | Dominant |
| 7101 | 15" only | Dominant |
| 7102 | 14"/15" | 14" kept — only BJ machine for 2 BJ-exclusive 14" RI SKUs |
| 7103 | 13" only | Dominant |
| 7104 | 14"/15" | 14" kept — only BJ machine for 2 BJ-exclusive 14" RI SKUs |
| 7105 | 13" only | Dominant |
| 7106 | 13" only | Dominant |
| 7201 | 16" only | Dominant |
| 7501 | 12" only | Hard (was already 97% dominant) |
| 7502 | 13" only | Hard (was already 100% dominant) |
| 7503 | 13" only | Hard (was already 100% dominant) |

**BJ 14" note:** Two RI SKUs (1325218614088HURL0, 1325217514082TVECH) are exclusively
eligible on BJ machines and are 14". BJ group policy expanded to include 14" to prevent
zero-machine assignment for these SKUs. 7102 and 7104 carry the 14" load within BJ.

## CO target urgency score — two-level priority

When a freed press selects an NRI target SKU T:

```
n     = current Running_Press_Count[T]
rate  = Qty_Per_Press_Per_Shift[T] × 3  (per-day production rate)
rem   = Updated_Demand_Qty[T]
H     = planning_days − current_day      (days left in horizon)

current_days = rem / (n × rate)   if n > 0 else ∞
after_days   = rem / ((n+1) × rate)

Class A (CRITICAL): current_days > H   → demand CANNOT be met without this CO
Class B (HELPFUL):  current_days ≤ H   → demand can be met with existing presses

Sort key: (class ASC, −Priority_Score, after_days ASC)
→ Class A always beats Class B. Within class: highest priority, then fewest days after CO.
```

**Objective**: fulfill demand ON TIME first, by priority second.
CO fires instantly when Runner-In demand is fulfilled; counts toward the 8/day cap.
If Day D's cap is full, defer to Day D+1.

---

## The core scheduling tension (what to brainstorm about)

### Tension 1 — Idle vs CO trade-off on building machines

**"Never go idle"** means: when a building machine finishes its current SKU
campaign, instead of stopping, it picks up another SKU (paying a CO cost).

- **Pro:** higher raw utilisation, more total GT produced.
- **Con:** every CO costs time (building CO time varies by same-size vs
  different-size). If the machine cycles through many short-campaign SKUs, CO
  overhead can dominate. On 7001/7002, this is already happening: 45–46% of
  time is CO, utilisation drops to 25–28%.
- **Con:** GT produced beyond the shift's demand cap is wasted (demand cap
  blocks it) or sits in inventory past shelf life (GT shelf life = 3 days).

**When "never go idle" makes sense:**
- There is a genuinely under-served NRI SKU with spare curing capacity waiting.
- The CO cost is low relative to the remaining shift time (same-size CO is
  cheaper than different-size).
- The SKU's demand `_gt_remaining > 0` and the machine can complete at least
  `MIN_CAMPAIGN_UNITS` in the remaining time.

**When idle is correct:**
- All reachable SKUs are at 100% demand fulfillment (`_gt_remaining = 0`).
- Remaining shift minutes < CO cost + 1 build cycle for any viable SKU.
- The only available SKUs would require a different-size CO and produce < 1
  shift's worth of GT anyway (marginal gain < CO loss).

### Tension 2 — Low utilisation + unfulfilled demand

This is a contradiction that can arise in at least three distinct ways:

**Case A — Wrong SKU in the machine pool**
The machine is capable of building SKUs X/Y/Z but the unfulfilled demand is for
SKU W which that machine cannot build. Solution: check
`Master_Building_Allowable_Machines_source` — W may need to be added.

**Case B — CO budget starved (7001–7004 pattern)**
The machine is cycling through too many SKUs, paying CO on each switch, leaving
little time to actually build. The fix is campaign consolidation: assign the
machine to 1–2 high-demand NRI SKUs for longer runs (months, not days).

**Case C — LP cap collapse (Day 2+ idle)**
If `_gt_remaining` for all assigned SKUs was partially filled by TopUp on Day 1,
the LP sees near-zero demand on Day 2 and idles the machine. Fix: `OVERBUILD_BUFFER_FRAC
= 0.2` (already applied) and `TOPUP_LOOKAHEAD_DAYS_GT = 1` (not 3) prevent
pre-filling far-future days. If this recurs, check whether TopUp is overfilling.

**Case D — NRI SKU deferred past horizon**
The curing CO was deferred (8-CO/day cap hit) so the building machine was never
assigned that NRI SKU. Building machines are idle; demand is unmet. Fix: allow
earlier CO scheduling for high-priority NRI SKUs, or accept that GT builds
before the CO and sits in inventory.

**Case E — Stage-1 structural under-utilisation**
Stage-1 util is always < 77% by design (11.5 machine-equivalents of Stage-2
demand on 15 Stage-1 machines). This is NOT a scheduler bug — it is physical.
Don't try to fix Stage-1 util by assigning extra SKUs; fix it only if Stage-2
demand grows (more RO→RI changeovers or more NRI SKUs added to Stage-2 path).

### Tension 3 — Demand cap preventing LP from using available capacity

On later days, `_gt_remaining[SKU]` approaches 0 for fulfilled SKUs. The LP
ceiling blocks production for those SKUs even if the machine is free. The
machine goes idle unless there is another SKU with remaining demand.

**The right response (in order of preference):**
1. TopUp assigns the idle tail to the nearest under-served NRI SKU.
2. If no NRI SKU is reachable without a CO, and remaining shift time > CO cost:
   pay the CO to a viable NRI.
3. If shift time < CO cost for any NRI: accept idle. Log it.
4. Do NOT overbuild a fulfilled SKU to avoid idle time — this violates the
   hard demand cap.

---

## Starvation Root Cause Analysis (Phase 1 — Synthetic Curing Plan)

> **Architecture caveat:** "Zero starvation by design" holds ONLY once Phase 4 (Curing
> Derivation) is active — curing is derived FROM building output, so it can never exceed
> available GT. Phase 1 uses a **synthetic** curing plan as the building target.
> Building must match it shift-by-shift. Gaps appear as starvation events in the validator.

Three starvation failure modes (baseline May run: 1,241 events, 65.3% avg util):

| Mode | Root Cause | Fix status |
|------|-----------|------------|
| **Mode A — Machine idle** | LP heuristic doesn't assign some machines to any SKU; they idle while curing runs at synthetic demand | **Implemented:** mould-constrained priority boost (`priority × (1 + current_days/31)`, capped 4×) forces LP to assign mould-limited SKUs first |
| **Mode B — Physical constraint** | Not enough building machines assigned to a SKU; even at 100% util they can't match curing volume | **Structural:** fix = assign more building machines, or implement Phase 4 (structural gap becomes throughput, not starvation) |
| **Mode C — NRI CO timing gap** | Building starts Shift A of CO day; curing Shift C → 2-shift buffer only. diff-size CO (180 min) eats Shift A → insufficient buffer | **Implemented:** NRI demand front-loading 70% pre-CO / 30% post-CO forces LP to pre-build buffer |

**Why small building CT doesn't fix Mode A or B:** Building CT ~2–3 min/tyre vs curing ~17 min —
building is fast enough. The problem is the machine is either not assigned (Mode A) or too few
machines assigned (Mode B). Speed only helps once a machine IS assigned.

**True fix for all modes:** Phase 4 — Curing Derivation. Curing only runs when GT is available
→ `Cure_Qty ≤ GT_inventory` by construction → zero starvation events.

---

## Current known issues (as of this design rev)

| Issue | Root cause | Current status |
|-------|-----------|----------------|
| Starvation events (Mode A) | Machine idle — LP doesn't assign some machines | **Fixed:** mould-constrained priority boost in `building_b2c.py` |
| Starvation events (Mode B) | Physical constraint — too few building machines for SKU's curing volume | **Structural:** true fix = Phase 4 (Curing Derivation) |
| Starvation events (Mode C) | NRI CO timing — 2-shift buffer sometimes insufficient | **Fixed:** NRI front-loading 70/30 in `_make_synthetic_curing()` |
| 21 UNMET NRI SKUs | No CO scheduled in main loop (no free compatible press) | **Fixed:** CO Rescue pass (spare-press donation) in `curing_consumption_dynamic.py` |
| 7001/7002 utilisation (was 25–28%) | Cross-inch SKU assignment + history bias | **Fixed:** `_MACHINE_HARD_INCH` dominant-inch lock + demand-minutes round-robin in heuristic. New util: 7001≈45%, 7002≈46% |
| Stage-1 util <33% | Structural (15 machines for 11.5-equiv demand) | By design; not a bug |
| Demand skew: BJ oversubscribed | 249,633 BJ-bucket demand vs ~184–200k BJ capacity | Structural — needs more BJ presses or VMI certification of BJ SKUs. Gap of ~20k is permanent in scheduler. |
| NRI SKUs with zero production | No allowable machine, or CO deferred past horizon | Logged per SKU; edge case table §18.4 in bc.md |
| Day-2+ LP idle | Was LP cap collapse; fixed with OVERBUILD_BUFFER_FRAC=0.2 + TOPUP_LOOKAHEAD_DAYS_GT=1 | Fixed in current code |

---

## Key config parameters (what's tunable)

| Parameter | Current value | What it controls |
|-----------|--------------|-----------------|
| `MIN_CAMPAIGN_MINS` | **120 min** (overridden in `building_b2c.py`) | Shortest allowed production run. Base value in `building.py` is 45; overridden to 120 to prevent CO explosion when many NRI SKUs activate simultaneously. |
| `MIN_CAMPAIGN_UNITS` | 40 | Minimum units per campaign. |
| `OVERBUILD_BUFFER_FRAC` | 0.2 | LP headroom above net demand per day (prevents cap collapse). |
| `TOPUP_LOOKAHEAD_DAYS_GT` | 1 | How many days ahead TopUp pre-builds. 1 prevents LP cap collapse. |
| `MAX_CURING_CHANGEOVERS_PER_DAY` | **8 (hard)** | Curing CO only. Building COs: unlimited. Defined at `curing_consumption_dynamic.py:80` as `MAX_CO_PER_DAY = 8`. Change only this constant to experiment with CO limits. |
| `CO_CLASS_FILTER` | **Class A only** | COScheduler fires only Class A (critical: `current_days > horizon_left`) COs. Class B (helpful) skipped. Prevents over-activating NRI SKUs building cannot supply simultaneously. |
| `PRE_START_SHIFTS` | **2** (set in `building_b2c.py`) | Building pre-starts N shifts before plan_start. 2 = Apr 30 Shift B (15:00) → 1 extra shift of GT pre-build before curing starts Shift C May 1. Prevents zero-inventory starvation for RI SKUs on Day 1. |
| `BUILD_LEAD_SHIFTS` | 3 (= 1 full day) | Building targets curing demand this many shifts ahead. |
| `GT_SHELF_LIFE_DAYS` | 3 | TopUp won't pre-build GT more than 3 days ahead. |
| `CARCASS_SHELF_LIFE_DAYS` | 1 | TopUp won't pre-build carcass more than 1 day ahead. |
| `Stage-2 CO time multiplier` | **2.0×** (applied in `building.py`) | Stage-2 `co_time_map` uses `diff × 2.0` (88 min → 176 min) to discourage LP from overloading Stage-2 with SKU switches. Configured in `HybridDailyScheduler.run()`. |

---

## Pipeline execution order (Phase 1 current stop point)

```
Phase 0  → Curing Consumption Table (classification + press counts + per-shift targets)
Phase 0+ → CO Schedule: urgency-ranked Pass 1 (Class A ONLY, max 8/day)
              └─ CO Rescue pass: spare-press donation for NRI SKUs without any CO
Phase 1a → Runner-In building (Stage-1, Stage-2, Unistage) — highest priority
              └─ Mould-constrained priority boost: priority × (1 + curr_days/31), capped 4×
Phase 1b + 2a → Joint Priority Pool (NRI building + Runner-Out CO eligibility) — residual capacity
              └─ NRI synthetic demand: 70% pre-CO / 30% post-CO (co_day_map front-loading)
Phase 2b → Pending CO scheduling (max 8/day)
Phase 3  → Dynamic target lock (per-shift building caps frozen)
[DEFERRED] Phase 4 → Curing derivation — TRUE zero starvation (curing derived FROM building)
[DEFERRED] Phase 5 → Analysis & KPIs
```

---

## How to think about a "should we change X" question

When the user brings a logic/approach question, reason through it along these axes:

1. **Which invariant does it touch?** (Demand cap, CO limit, Stage-1/2 dependency, no waste GT)
2. **Which SKU category is affected?** (RI / RO / NRI — each has different behaviour)
3. **Which machines are involved?** (Unistage 7001–7004 have a known structural problem; Stage-1 is always under-utilised by design)
4. **Is this a config change, a logic change, or a data change?**
   - Config change: adjust a parameter in the table above.
   - Logic change: modify Phase 1a/1b/2a/2b rules (e.g. "never go idle" = change how idle tail is handled in Phase 3 or TopUp).
   - Data change: add SKU to allowable machines, change feed map.
5. **What does the KPI table say?** (§13.1 in bc.md) — expected util is 90–95% for GT machines. If actual < 80%, something is wrong.
6. **Is the proposed change a trade-off or a strict improvement?** Most "never go idle" changes are trade-offs — they improve aggregate util but risk more COs or shelf-life waste. Quantify before committing.

---

## Relevant source files

| File | Role |
|------|------|
| [b2c_pipeline.py](b2c_pipeline.py) | **CORRECT ENTRY POINT** — runs curing consumption → building in one command (`python b2c_pipeline.py`). Do NOT run `building_b2c.py` directly; its `__main__` uses June 1 plan_start and static consumption table. |
| [building_b2c.py](building_b2c.py) | B2C building scheduler — Phase 1a/1b/2a/2b/3 |
| [curing_consumption.py](curing_consumption.py) | Phase 0 — Day 0 snapshot consumption table + press counts. `load_demand()` handles raw CSV (`skuCode`/`requirement`) and normalized XLSX (`SKUCode`/`Requirement`/`ConsolidatedPriorityScore`); missing Priority defaults to 1.0 |
| [curing_consumption_dynamic.py](curing_consumption_dynamic.py) | Phase 0 Extended — 31-day pre-computed curing consumption for May. Two-pass: Pass 1 = CO schedule (Class A only, max 8/day); Pass 2 = simulate 31 days. Output: `curing_consumption_31day.xlsx` (34 sheets incl. `curing_daily_cons`) |
| [building.py](building.py) | Base building machinery (LP engine + DemandHeuristicAssigner) reused by B2C |
| [approach/bc.md](approach/bc.md) | Full B2C architecture spec (authoritative) |
| [ARCHITECTURE_DETAILED.md](ARCHITECTURE_DETAILED.md) | C2B architecture (legacy; B2C supersedes) |
| [cbc.py](cbc.py) | Orchestrator (C2B mode) |

---

## Known Calculation Pitfalls

### Ratio / coverage metrics — universe must match on both sides

Any formula of the form:
```
fulfilled = total_demand - demand_remaining
```
is only correct when `total_demand` and `demand_remaining` are computed
over the **same SKU universe**. If excluded SKUs appear in `total_demand`
but not in `demand_remaining` (because they have no rows in daily sheets),
their demand silently becomes phantom "fulfilled" production.

Rule: before writing any summary KPI, confirm:
```
set(SKUs in numerator/remaining) == set(SKUs in denominator/total)
```

Real instance: `curing_consumption_dynamic.py` Summary sheet — `total_demand`
included 7 excluded SKUs (62,802 demand); `demand_left_day31` excluded them
→ 62,802 appeared as fulfilled. Fix: filter excluded SKUs out of `total_demand`
before computing coverage %.

---

## Framing for the agent

You are a **scheduling logic advisor** for a tyre manufacturing planning system.
The user will ask open-ended "should we / what if / what's wrong" questions about
the building machine scheduling logic. Your job is:

- Think through the trade-off fully before recommending a direction.
- Name the specific config parameter or code location if a change is proposed.
- Call out which invariant would be affected and whether it is preserved.
- Don't recommend "never go idle" unconditionally — it creates CO explosion on
  machines like 7001/7002. The right answer is conditional on remaining demand,
  CO cost, and shelf life.
- When the user says "low utilisation + unmet demand", diagnose which of Cases
  A–E (see above) is the root cause before prescribing a fix.
- Prefer minimal, targeted changes. A parameter tweak beats a logic rewrite if
  it solves the problem.

---

## Demand Skew Analysis (May 2026 — confirmed structural limits)

Exclusive demand assignment: each SKU counted once in highest-priority eligible group
(priority: VMIMAXX > BJ > UNI\_NARROW > STAGE2 > no machine data).

| Group | Machines | SKUs | Demand | Built GT | Gap | Coverage | Avg Util |
|-------|----------|------|--------|----------|-----|----------|----------|
| VMIMAXX | 8 | 39 | 239,156 | 211,808 | 27,348 | 88.6% | 55.3% |
| BJ | 7 | 22 | 249,633 | 228,931 | 20,702 | 91.7% | 83.2% |
| UNI\_NARROW | 3 | 7 | 56,717 | 44,193 | 12,524 | 77.9% | 58.4% |
| STAGE2 | 6 | 14 | 89,549 | 85,684 | 3,865 | 95.7% | 81.2% |
| No machine data | — | 7 | 59,918 | 8,417 | 51,501 | 14.0% | — |
| **TOTAL** | **39** | **89** | **694,973** | **579,033** | **115,940** | **83.3%** | |

**Structural ceilings:**
- BJ: demand/capacity ratio = 249,633 / ~184k = 136% — **physically oversubscribed**. Cannot close the 20k gap via scheduling. Fix = more BJ presses or VMI certification.
- VMIMAXX: demand/capacity = 239,156 / 357,120 = 67% ceiling — machines are **undersubscribed**. 27k gap is scheduling overhead (CO + idle tail); ~10–15k recoverable.
- UNI\_NARROW: demand/capacity = 56,717 / 89,280 = 63% ceiling — also undersubscribed. 12k gap is scheduling; ~5–8k recoverable.
- STAGE2: 95.7% coverage, near ceiling with Stage-1 feed constraints.
- No machine data: 51,501 permanently unbuilt until master data certifications added.

**Scheduler ceiling (without master data changes): ~600–610k / 694,973 ≈ 86–88%**
**True ceiling (if all 7 no-master-data SKUs certified): ~630k+ / 694,973 ≈ 91%+**
