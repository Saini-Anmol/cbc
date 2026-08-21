# B2C Scheduler — Technical Architecture (current)

> **CURRENT STATE (2026-08) — the LIVE building inch model is the HISTORICAL INCH-LOCK + Lever A
> (§4.5); this REPLACES the ±2 band / soft-lock / inch-flex model (now the `INCH_HIST_LOCK=0`
> fallback, §4.6).** Read `CLAUDE.md` §"Historical inch-lock" for the authoritative summary. The
> live engine: **(1) INCH_HIST_LOCK** — per-machine allowed-inch sets from the 4-month plant
> report (27 FIXED single-inch / 12 FLEXIBLE), ±2 band OFF; **(2) Lever A `FLEX_SCARCE_INCH`** —
> flexible machines feed the scarcest allowed inch (+3,804 net); **(3) Lever B `FIXED_ESCAPE`
> REJECTED** (−2,572, off); plus mould gate + retarget + CO scorer + mould life v2 + mould clean +
> 4-SKU/day cap + IUkeep + dynamic buffer (β=2 @ 8k GT cap). **Inputs:** per-month
> `*_tomerji.xlsx`; ONE `Daily_Running_Moulds` table keyed by `plan_month`; single
> `gt_inventory_manual`; building start-free (no building-running-machines).
> Config: `MAX_CHANGEOVERS_PER_DAY=17` (this cycle; was 12), `MAX_ENDOFDAY_GT_INVENTORY=8000`
> (env `GT_CAP_MAX`; cloud aligned — `main.CLOUD_CONFIG` pins 8000 too, so local↔cloud byte-parity;
> see §8), `MIN_INCH_DWELL_DAYS=5`, `MAX_BUILDING_SKUS_PER_DAY=4`.
> **Adopted LOCAL KPIs (hybrid CO items 1+2 §5.4, no holiday, mould-audit PASS, cap 0-over,
> deterministic, per-month opening GT, local==cloud byte-parity):** June 650,381/92.36% · July
> 685,342/94.17% · Aug 648,687/94.07% = **1,984,410 cured**. The rules are physical restrictions that
> make the plan floor-realistic (KPI below the mould-blind baseline by design).
>
> ⚠️ **ABSOLUTE KPIs DRIFT DAY-TO-DAY — treat every number here as AS-OF-DATE.** `Daily_Running_Moulds`
> is a **LIVE, daily-refreshed** table (the Day-0 snapshot changes every calendar day), so the SAME code
> on the SAME demand produces a **different absolute KPI on a different day** — July-full drifted
> 685,342 → 675,817 → 689,837 → 688,984 over consecutive days. **Compare runs only WITHIN the same
> day**; the figures above were measured at CO=12 on one snapshot and are illustrative of the
> mechanism/approach, not a fixed target. Document toggles and deltas, not absolute totals.

**Building-to-Curing (B2C):** the building schedule is the **primary output**; the
curing schedule is **fully derived** from it. Building runs first; curing consumes
exactly what building produces. Direction is the reverse of the old C2B design.

> **Two hard availability constraints, identical in structure:**
> 1. Stage-1 carcass must be available at shift **S** when Stage-2 runs (earlier is fine; same shift is the floor).
> 2. GT must be available at shift **S** when curing consumes it (earlier is fine; same shift is the floor).
>
> S-1 availability is preferred (zero-wait) but S is the hard floor.
> **Objective:** maximise building throughput. Curing is sized to building output.

---

## 0. READ THIS FIRST — which architecture actually runs

The **default and only production path** is the **per-shift rolling pipeline** in
`b2c_pipeline.py` (`run_rolling_pipeline` / `_assign_building_shift`), invoked by
`python b2c_pipeline.py` (no flags). Building assignment runs **once per shift**,
reacting to that shift's actual curing-press state; building and curing are simulated
**simultaneously** shift-by-shift. §5–§9 below document this path.

A legacy 31-day-upfront LP + `DemandHeuristicAssigner` design (`building_b2c.py` /
`building.py`) is still reachable via `python b2c_pipeline.py --legacy`. It is
summarised in one paragraph (§10) and is **not** the current architecture.

**Still-current shared component:** `curing_consumption_dynamic.py`
(`COScheduler` / `DaySimulator`) is imported and reused as-is by the rolling
pipeline for CO pre-computation. Its SKU classification, eligibility filter,
cycle-time formula, and CO-schedule logic (§3, §8) are current.

**CURRENT building inch model (2026-08):** the **historical inch-LOCK** (`INCH_HIST_LOCK`, ON) +
**Lever A** (`FLEX_SCARCE_INCH`, ON) — see §4.5. It **replaces the anchor ±2 band / four-tier
soft-lock/flex model** (now the `INCH_HIST_LOCK=0` fallback, §4.6). **Inputs this cycle:** per-month
`*_tomerji.xlsx` demand; running moulds from ONE `Daily_Running_Moulds` table keyed by a
`plan_month` column (auto-derived from `PLAN_START`); single `gt_inventory_manual` opening GT; NO
building-running-machines (building starts free). Adopted 3-month KPIs in §16.

---

## 1. Motivation — why B2C

| Dimension | C2B (old) | B2C (current) |
|-----------|-----------|---------------|
| Primary driver | Curing LP allocates press-minutes | Building throughput ceiling |
| Order | Curing first → building follows | Building first → curing derived |
| Bottleneck | Building couldn't feed curing plan | Eliminated — curing sized to building |
| Starvation | Frequent (root cause of redesign) | **Zero by architecture** — curing is derived FROM building output, never exceeds available GT |
| Waste GT | Possible (building over-runs) | Zero — building output capped to consumption |

Curing is derived FROM building, so a running press can never consume GT that does not
exist. Starvation-as-idle can still show up as a *throughput* limit (a running press with
demand but no GT that shift), but never as negative inventory.

---

## 2. Start conditions

### 2.1 Opening GT inventory (from DB, not zero)
GT inventory is loaded from `gt_inventory_manual` at plan start, **filtered by the `plan_month`
column** (`WHERE plan_month = PLAN_MONTH`, auto-derived from `PLAN_START`) — each month uses its own
opening GT (June 7,644 / July 8,989 / Aug 7,488). `PLAN_MONTH` is imported by value (like
`RUNNING_MOULDS_MONTH`), so a harness that sets `PLAN_START` after import must also export env `PLAN_MONTH`.
Pre-plan-start building output is credited to the curing sim's opening balance by
`curing_b2c.py`. Opening carcass inventory = **0** (Stage-2 waits for Stage-1 output;
Unistage is unaffected). Opening GT acts as a Day-1 buffer; it does not change the
steady-state rate.

### 2.2 Building pre-start (`PRE_START_SHIFTS = 2`) — LEGACY PATH ONLY
`PRE_START_SHIFTS = 2` applies **only to the legacy LP path** (`building_b2c.py`). The
**rolling pipeline (`b2c_pipeline.py`, the DEFAULT) does NOT pre-build** — building and curing
both start **Day 1 Shift A 07:00 simultaneously**. Day-1 curing is covered by same-shift
building (building runs before curing within each shift, §6 Step 2→4) plus opening GT inventory.
Day-1 starvation is negligible (~11 of ~897 events). Any earlier text implying the rolling
pipeline pre-starts building is superseded by this.

Legacy behaviour (for reference): building started **2 shifts before** curing (May 1 Shift A →
building Apr 30 Shift B), and pre-start GT was credited to the curing opening balance.

### 2.3 Demand file
Per-month demand file (June `correct_june_plan.xlsx` 704,194 / 30 days / 97 SKUs;
July `july_correct_plan.xlsx` 727,779 / 31 days / 102 SKUs; Aug `august_demand_tomerji.xlsx`
689,563 / 31 days / 91 SKUs). Columns:
`SKUCode`, `Requirement` (the per-SKU **demand cap**), `ConsolidatedPriorityScore` (a static input;
priority is actually recomputed in code as min-max of Requirement — any file score is ignored, v1).
(Older revisions used `demand_may.xlsx` / `june_production_data.xlsx` — retired.)

---

## 3. Phase 0 — Curing consumption & SKU classification

**File:** `curing_consumption_dynamic.py` (still current — reused by rolling pipeline).

### 3.1 SKU categories
| Category | Definition | Building role |
|----------|-----------|---------------|
| Runner-In (RI) | On a curing press AND in demand | Primary building load |
| Runner-Out (RO) | On a curing press, NOT in demand | Candidate for press CO to a new SKU |
| Non-Runner-In (NRI) | NOT on any press, in demand | Residual fill; gets a press via CO |

### 3.2 Cycle time & consumption
```
effective_CT(SKU) = round((raw_CT + 2.3) / 0.94)   min      (from Master_Curing_Design_CycleTime)
missing SKU       → DEFAULT_CYCLE_TIME_MIN = 17.0            (silent; no remark)
qty_per_press_per_shift(SKU) = floor(480 / effective_CT) × 2 cavities
```
Constants: `LOAD_UNLOAD_BUFFER_MIN = 2.3`, `PRESS_EFFICIENCY = 0.94`, `CAVITIES_PER_MOULD = 2`.
Ambiguous DB rows → match active mould via `Master_Mapping_Mould_SKU`; else minimum CT.

### 3.3 SKU eligibility filter (before classification)
A SKU is **ELIGIBLE** only if it appears in **both** master tables:
```
Building pool = Master_Building_Allowable_Machines   (only — no history fallback)
Curing pool   = Master_Curing_Allowable_Machines_source
ELIGIBLE = SKU ∈ Building_pool AND SKU ∈ Curing_pool
```
The historical-production union (`Building_Stage1/2_Best_Machines`) was **removed**
(commit `63d193f`): it let SKUs run on machines with no current certification. Those
tables are still loaded but ignored for eligibility. Excluded SKUs are listed in an
"Excluded SKUs" sheet. **CT is never an exclusion criterion.**

### 3.4 CO schedule (COScheduler)
Computed from Day-0 data alone; consumed by the rolling loop.
- **Runner-Out** presses eligible for CO from Day 1.
- **Runner-In** presses eligible for CO on the day their `Updated_Demand_Qty` hits 0.
- **CO urgency** (two-level, see §8) picks NRI targets.
- **Max `MAX_CHANGEOVERS_PER_DAY` curing COs/day**; excess deferred to next day.
- **Two kinds of curing CO, both now charged their changeover time** (§5): **planned** COs
  (from the Day-0 CO schedule — 147 this cycle) and **dynamic** COs (fired mid-horizon the moment
  an RI press's demand hits 0 — 78 this cycle). Every CO, planned or dynamic, is charged
  `CURING_CO_CHANGEOVER_MINS = 480`. Previously dynamic COs were **free** (only the 147 planned
  COs cost time), which slightly inflated the KPI. Total charged now = 225 × 480 = 108,000 min.
- **CO Rescue pass:** NRI SKUs unscheduled in the main loop can be given a press donated
  from an RI press with `n>1` that can still meet demand with `n−1`.

**Output:** `curing_consumption_<days>day_<date>.xlsx` (date/horizon stamped from `PLAN_START`, e.g.
`curing_consumption_31day_2026-05-01.xlsx`; Day_01…Day_31 + CO_Schedule + Day0_Summary + Summary).
Two hardcoded fallback paths in `curing_consumption_dynamic.py` that bypassed config were fixed so a
new month never overwrites the previous.

### 3.5 Consumption-table schema (per SKU)
`SKUCode` · `Category` (RI/RO/NRI) · `Running_Press_Count` (0 for NRI) · `Allowable_Moulds_Count`
· `Effective_CT_Min` · `Qty_Per_Press_Per_Shift` (`floor(480/CT)×2`) · `Total_GT_Per_Shift`
· `Demand_Qty` (`Requirement`) · `Priority_Score` (`ConsolidatedPriorityScore`).

### 3.6 Curing press HOLD — `_PRESS_STABLE` (ADOPTED, default ON)

`curing_consumption_dynamic.py`. A curing press **stays on its SKU until that SKU's demand is met** —
**no mid-life voluntary surplus release**. This removes the day-to-day curing press-count churn (the
release→re-acquire ping-pong where a press was freed as "surplus" one day and re-acquired the next).
A press frees **only** the two honest ways: its demand is done (→ Runner-Out) — with **n−1
protection** guarding the last press still covering a live SKU. `PRESS_STABLE=0` reverts bit-for-bit;
**cloud inherits** (module-level env-default ON).

At adoption (as-of-snapshot): curing COs **−126**, starvation **−916**, press-count churn **−57%**,
cured **−2,118 net** (June +5,850 / July −2,268 / Aug −5,700) — a deliberate churn-for-stability trade
the client wanted (a stable, floor-releasable press plan). **REJECTED sub-experiments** (all default
OFF, code kept): `PRESS_RELEASE_DAYS` (near-done early release), `PRESS_VALVE` / `PRESS_VALVE_MARGIN`
(starvation valve — net worse than the plain hold), `PRESS_RATCHET` (monotone press-count cap — no-op).

---

## 4. Machines, groups & inch policy

### 4.1 Building machine groups (38 machines)
```
Stage-1  (14): 6802,6803,6909,6911,7601,7701,7801–7804,8001–8003,8101   (6801/bj1stage1 retired by plant)
   → Carcass (semi-finished). Feeds Stage-2 only.
Stage-2  (6):  8201,8301,8302,8501,8502,7301
   → GT, requires Stage-1 carcass as input. BOTTLENECK (6 vs 14 Stage-1).
Unistage (18): 6001–6004,7001–7004 (VMI) · 7101–7106,7201 (BJ) · 7501–7503 (UNI_NARROW)
   → GT, independent (no Stage-1 dependency).
```
**Stage-1 → carcass → Stage-2** dependency is a hard constraint; Unistage independent.
**167 active curing presses** (committed cycle: `RUNNING_MOULDS_TABLE = "Daily_Running_Moulds"`).
Planning horizon: 31 days × 3 shifts (A 07:00 / B 15:00 / C 23:00) × 480 min.

**Machine-group display labels.** Internal `_MACHINE_GROUP` keys stay `VMI` / `BJ` / `UNISTAGE`
/ `STAGE2` / `STAGE1` — they are used both as logic keys AND as `bc_config.py` CO-time dict keys,
so they must **not** be renamed. A separate `_MACHINE_GROUP_DISPLAY` map + `_group_label()` gives
plant-facing names in the building Shift Schedule `Machine_Group` column **only**: VMIMAXX GROUP /
BJ GROUP / UNISTAGE GROUP / TBM STAGE2 / TBM STAGE1.

**Stage-1 carcass capacity fact (recomputed post-6801, active `_BLD_CT_SEC`):** Stage-1 supplies
≈ **2,896 carcass/shift on 14 machines** (was ≈ 3,123 on 15 — 6801/bj1stage1 ≈ 227/shift, now
retired by the plant) vs Stage-2's full-output carcass demand ≈ **2,657/shift** → **~9% headroom,
down from ~17.5% on 15 machines**. Removing 6801 roughly halved the buffer but Stage-1 stays a
**net carcass supplier — no bottleneck even at full Stage-2 output**, and the real margin is larger
because Stage-2 is demand-limited (rarely 100%). *(The older 2,459 / 2,215 / ~11% figures predate
the building-CT correction in commit `f86f70b` and are retired.)* Stage-1 util is structurally
~47–50% — by design, not a scheduling bug. It rises only if more SKUs are certified onto Stage-2.

**Group demand-vs-capacity (approximate, orient only — re-pull for exact figures):**
| Group | Machines | Demand/cap posture |
|-------|----------|--------------------|
| VMIMAXX (VMI) | 8 | Undersubscribed (~67% of capacity) — spare capacity, the flex/CO-budget target |
| BJ | 7 | Oversubscribed at the plant level — structural ceiling; residual gap = curing presses |
| UNI_NARROW | 3 | Undersubscribed but tight 12"/13" supply — do not add to the flex set |
| STAGE2 | 6 | Near-full; carcass-dependency makes it fragile — never add to the flex set |

Inch demand mix (plant-wide): 15" (~34%) › 13" (~21%) › 14" ≈ 12" (~14% each) › 16" (~12%) ›
17"/18" (tail ~5%).

### 4.2 Building changeover (CO) types & times
Two CO types: **`same_size_CO`** (new SKU same inch) and **`diff_size_CO`** (different inch).
Always say "building CO" or "curing CO" to disambiguate.

| CO type | Time by group |
|---------|---------------|
| `same_size_CO` | 20 (VMI) → 45 (BJ) → 59 (Stage-2) → 60 (Stage-1/MID) → 110 (Unistage 7501–7503) |
| `diff_size_CO` | 88 (Stage-2) → 90 (BJ) → 120 (VMI) → 180 (Stage-1/MID/Unistage) |

VMI `same_size_CO` = 20 min = 4.2% of a shift — cheapest. Stage-1/Unistage `diff_size_CO`
= 180 min = 37.5% of a shift — avoid without strong demand justification.
**Priority:** VMI same-size first; avoid Stage-1/Unistage diff-size; Stage-2 diff-size (88) OK if no VMI alternative.

> **Multi-press feeding NOT possible:** same inch ≠ same GT recipe. Each SKU has a unique
> compound + bead + construction. One building machine always produces for exactly one SKU
> at a time.

### 4.3 Per-machine dominant inch
> Under the LIVE hist-lock (§4.5) the dominant inch = the top of each machine's 4-month ranked
> set (loaded by `_load_inch_hist_lock`), which differs from this hardcoded map for a few machines
> (e.g. 7001→**15** not 16). The map below is the fallback (`INCH_HIST_LOCK=0`) / historical value.
```
VMIMAXX  7001→16  7002→14  7003→15  7004→14   6001→14  6002→15  6003→17  6004→16
BJ       7101→15  7102→15  7103→13  7104→15   7105→13  7106→13  7201→16
UNI      7501→12  7502→13  7503→13
```
**SKU inch derivation:** if absent from the size master, inch = `sku_code[8:10]`
(e.g. `"1325216814085SURL0"[8:10] = "14"`).

### 4.4 Group routing priority (inch → group)
```
12" → TWO STAGE TBM → UNISTAGE
13" → UNISTAGE → BJ → TBM
14" → VMIMAXX
15" → BJ → TBM → VMIMAXX
16"/17"/18" → VMIMAXX only
```

### 4.5 Inch-locking — HISTORICAL INCH-LOCK (LIVE, ADOPTED 2026-08)

The building inch model is now a **per-machine allowed-inch SET** from the 4-month plant report
`data/analysis_aug/machine_inch_dominant_4months_Apr-Jul.xlsx` (sheet `Inch_Counts_Matrix`),
loaded by `_load_inch_hist_lock` in `b2c_pipeline.py`. **This REPLACES the ±2 anchor band and the
four-tier model** (§4.6, now the `INCH_HIST_LOCK=0` fallback).

- **`INCH_HIST_LOCK` (ON).** An inch is in a machine's allowed set if it is ≥ `INCH_HIST_LOCK_MIN_SHARE`
  (0.02) of that machine's records, ranked, capped at `INCH_HIST_LOCK_MAX_INCHES` (3). At 2% this
  gives exactly **27 FIXED machines (single historical inch, ZERO diff-size CO ever) + 12 FLEXIBLE
  (ranked historical inches only)**. The ±2 band is DISCONTINUED — historically-evidenced +3 jumps
  are legal (7001 15↔18, 7103 13↔16, 7201 16↔13, 7803 15↔12, 8301 12↔15); an inch never run is not.
  Enforcement: the allowable-machine strip removes off-set SKUs; `machine_locked_inches` gates
  Phase B (`_inch_gate`); `_inch_ok` returns True (band off); Stage-1 `_s1_inch_ok`. The dominant
  inch (anchor + `inch_penalty`) is the top of each machine's ranked set. `INCH_HIST_LOCK_STAGE1`
  OFF (Stage-1 stays demand-optimal + carcass-FEASIBLE). `INCH_HIST_LOCK=0` → §4.6 bit-for-bit.
- **Lever A — `FLEX_SCARCE_INCH` (ON).** Among a FLEXIBLE machine's allowed inches, prefer the
  SCARCEST (biggest live curing-draw shortfall) over same-inch stickiness (a per-pick inch-scarcity
  rank overriding `inch_penalty` in `_key`, flex machines only). Feeds the about-to-starve 15"/13"
  that fixed machines can't reach. **+3,804 net over 3 months, starvation ↓/flat all 3.**
- **Lever B — `FIXED_ESCAPE` (OFF, REJECTED).** A fixed machine may take ≤1 diff-CO to a scarce
  DB-allowable inch only after its own inch's whole-month demand is done. Regresses all 3 months
  (−2,572) — the stranded 14"/17" idle capacity frees too late to redirect. Code retained. (Gate
  MUST use `demand_remaining − projected_gt`, not buffered `_defc` — buffered → −31.7k.)

### 4.6 Inch-locking — four tiers (FALLBACK, `INCH_HIST_LOCK=0` only)

> Superseded by §4.5. Runs only with `INCH_HIST_LOCK=0`. Under the live lock `_HARD` is unused
> (the strip enforces the historical set), the ±2 band is off, and `_INCH_FLEX_EXTRA_COS`/soft-lock
> still apply to VMI+BJ but the WHICH-inch decision comes from the historical set + Lever A. Below
> = the fallback model, kept for record.

**Tier 1 — Hard filter (`_HARD` in `b2c_pipeline.py`).** Restricts a machine to its
dominant inch(es). Without it, high-demand 15" SKUs capture 13" BJ machines (root cause of
`1325217613082TUNE0` gap).

| Machine | Hard filter | | Machine | Hard filter |
|---------|-------------|-|---------|-------------|
| 6001 | 14" | | 7101 | 15" |
| 6002 | 15" | | 7102 | 14"/15" |
| 6003 | 17"/18" | | 7103 | 13" |
| 6004 | 16" | | 7104 | 14"/15" |
| 7002 | 14" | | 7105 | 13" |
| 7004 | 14" | | 7106 | 13" |
| | | | 7201 | 16" |
| 7501 | 12"/13" | | 7502 | 13" |
| 7503 | 13" | | | |

**Tier 1.5 — Soft-lock (`_SOFT_LOCK_MACHINES`): 7001, 7003.** Removed from `_HARD`; serve
dominant inch first, secondary inch only in Campaign 2+ when primary demand is exhausted for
the shift (`primary_demand_done`). 7001 dom 16" (sec 14"/15"); 7003 dom 15" (sec 14"/16").
Not hard-locked because 16"/15" demand runs out and the machine would idle; soft-lock lets it
serve neighbours at 20-min same-size CO.

**Tier 1.6 — Inch-flexibility (`_INCH_FLEX_ENABLED = True`).** Generalizes the soft-lock to a
brute-forced-optimal machine set.
- **Flex set = all VMI (6001–6004, 7001–7004) + all BJ (7101–7106, 7201)** = 15 machines,
  dropped from `_HARD` so their full DB allowable becomes eligible.
- **UNI_NARROW and STAGE2 stay locked.** Brute-force: STAGE2 in the flex set is catastrophic
  (~−57k, Stage-1 carcass-dependency disruption); UNI regresses (tight 13" supply). Plant data
  agrees (IRM runs 1.5"/machine, UNI 2.3", VMI 4.6, BJ 2.4).
- **Anti-carry-over reclamation guard (the critical safety):** a flex machine that carried onto
  an off-inch SKU must return to dominant when a dominant-inch SKU regains deficit — Campaign 1
  `_flex_reclaim` + a merged sort that orders by `inch_penalty` FIRST. Without it, generalizing
  the soft-lock reintroduces carry-over lock-in (regressed starvation-feed 96.4→92.9%).
- **Off-inch gate:** off-inch served only when dominant demand is done for the shift; off-inch
  target ranked `starving_first`.
- **Extra CO budget** `_INCH_FLEX_EXTRA_COS = 2` → 4 building COs/shift for flex machines; the
  30% CO-cost guard is bypassed for a flex machine going off-inch once its own inch is done.
- **Honest finding:** off-inch building itself is a near-no-op (+52 units). The real gain
  (~+1,800) is the **extra CO budget** letting VMI+BJ serve more of their own dominant-inch
  SKUs. `_INCH_FLEX_ENABLED = False` reproduces the pre-flex baseline bit-for-bit.

**Tier 2 — Dominant-inch preference (`_MACHINE_DOMINANT_INCH`).** Within CO candidates,
`inch_penalty = 0` for dominant inch, `1` otherwise. Sort key:
`(−deficit, inch_penalty, revisit_penalty, co_cost)`.

---

## 5. Curing press physics & changeover timing

Each press holds **2 moulds × 2 cavities = 4 tyre slots** per cycle.

**LH/RH labelling:** each physical press appears twice in the running-moulds table (suffix `LH`,
`RH`). `load_running_moulds()` strips the suffix → single record keyed by the numeric label
(e.g. `"75206"`). All press IDs everywhere use this `WCNAME_clean` key.

### 5.1 Mould clean — NOW MODELLED (toggle `_MOULD_CLEAN_ENABLED`, default ON)
Mould clean **is** a scheduling event now (it was previously "not modelled / absorbed into the CO
window" — that is superseded). Each press carries `remaining_mould_life` counted in **cycles**,
starting at `MOULD_CLEAN_CYCLES = 3000` (= 6,000 tyres at `CURING_CAVITIES = 2`). When it reaches
0, an 8-hour clean (`MOULD_CLEAN_MINS = 480` = one shift) fires **immediately, mid-shift allowed**:
```
production caps at the 3,000th cycle within the shift
→ the 480-min clean fills the rest of that shift, and any overhang carries into the next shift
→ mould life resets to 3,000
```
A curing **CO also resets** mould life (a CO includes a clean). Impact this cycle is tiny (~4
presses trigger a clean because v1 starts every press **fresh at 3,000**): −31 to −250 tyres.
Config: `MOULD_CLEAN_CYCLES`, `MOULD_CLEAN_MINS` in `bc_config.py`.

> **v2 note:** real opening mould life exists in the running-moulds DB tables (`Mould life` /
> `Target life` columns, mean ~2,000 remaining). Loading it would make ~110 of 167 presses trigger
> a clean during the horizon. Deferred to v2; v1 starts every press fresh at 3,000.

Output columns added to the curing **Machine Utilization** sheet: `total_cycle` (renamed from
`Total_Cycles`), `Mould_Clean_Mins`, `Mould_Clean_Utilization_%`, `Remaining_Mould_Life`.
Balance identity: **Available = Used + CO + MouldClean + Idle.**

### 5.2 Changeover timing — building starts simultaneously with the curing CO
```
Day D CO shift: Curing press → CHANGEOVER (480 min, OCCUPIED full shift)
                Building      → START producing GT for new SKU  ← simultaneous
next shift:     Curing press → PRODUCTION begins (new SKU)
                Building      → CONTINUE
following:      Curing press → PRODUCTION continues
```
`bc_config.py`: `CURING_CO_DURATION_SHIFTS = 1`, `CURING_CO_CHANGEOVER_MINS = 480`.
**Every CO is charged 480 min** — planned AND dynamic (§3.4). A **dynamic** CO fires mid-shift the
moment demand hits 0: it eats the rest of that shift and the overhang carries into the next shift
(`co_carry`), so the **new SKU starts mid-shift** (e.g. 02:00), not at the shift boundary.
**Pre-build injection:** `shift_cure_demand[new_sku] += _cure_qty_per_shift(new_ct)` is injected on
**each press's OWN CO shift only** (see §5.3 — no longer a blanket Shift A). Never inject twice —
double injection = 2× demand signal.

### 5.3 Curing CO shift-spreading (toggle `_CO_SHIFT_SPREAD_ENABLED`, hardcoded `True`)
Previously **all** planned COs were hardcoded to **Shift A** → ~97% of CO downtime landed in shift
A, so shift-A curing sat ~6.6k below shift B (a pure artifact). Now each planned CO is placed in
the shift where its press **finishes its old SKU**:
- already finished → **Shift A** (a free press never waits);
- finishes in *n* shifts → **that shift**;
- won't finish today → **Shift A** (preemptive).

Result: planned COs spread **90 / 30 / 27** across A/B/C; the shift-A dip halved (−6,629 → −3,418);
KPI ≈ flat (−253 tyres); starvation −103. Before its CO shift the press runs its OLD SKU; on the CO
shift it is CHANGEOVER; after, the NEW SKU. The pre-build injection follows the press's own CO shift
(§5.2), preserving the simultaneity rule.

### 5.4 Hybrid curing-CO (items 1+2, ADOPTED — hardcoded ON, local + cloud)

Two coupled changes to how planned curing COs are gated/timed. Both are **module-level** flags, so
**cloud inherits them automatically**. Combined **1+2 = +29,836 cured / 3 months, feasibility-clean.**
Hybrid (`REACTIVE_ONLY = 0`) is the default; pure-reactive regressed ~−128k. Items 3
(`HYBRID_CO_CANCEL`) and 4 (adaptive starvation-CO) measured **non-additive / no-op → left OFF**.

- **Item 1 — building-aware DEFICIT-FIRST per-SKU feed** (`_PERSKU_FEED_V2 = True`,
  `curing_consumption_dynamic.py`). Replaces the optimistic un-apportioned `buildable_rate`: contending
  same-inch SKUs each claim their required draw **most-constrained-first** (preferring machines
  **outside** the target's eligible set), so the target's feasible draw = the **residual on its own
  machines**. The 5b / surplus CO guards then block only **genuinely infeasible** planned COs (no longer
  falsely-blocking feasible ones).
- **Item 2 — HYBRID planned-CO DEFER** (`_HYBRID_CO_DEFER = True`, `b2c_pipeline.py`). DEFER a planned
  curing CO to the **next working day** instead of preempting it in Shift A, **when** the press's old SKU
  still has FULFILLABLE demand (live `_supply_ok`) that won't finish today **AND** the press is not
  surplus. The mirror image of the pull-forward. **Biggest single lever (July +14,137 alone).**

---

## 6. Rolling per-shift pipeline (THE default)

Per-shift loop; building assignment runs once per shift, reacting to actual press state.
Confirmed to match plant practice (09.04.2026 schedule: 17/24 GT machines run 2–3 campaigns
per shift with intra-shift COs; VMI avg 0.57 CO/shift). `machine_current_sku` updates at the
end of **each shift** — Shift B sees Shift A's final SKU.

```
Pre-computation: CO schedule (COScheduler), allow map (_HARD filter + inch fallback),
  CT map, initial press state (WCNAME_clean), opening GT inventory, demand_remaining.

for Day D in 1..31:
  co_target_skus = {new_sku for each curing CO on Day D}

  for Shift S in [A, B, C]:

    Step 1 — Per-shift curing demand
      shift_cure_demand[sku] = Σ presses in RUNNING state × _cure_qty_per_shift
      CO presses: their OWN CO shift = CHANGEOVER (no demand, §5.3); other shifts RUNNING = full demand
      Pre-build signal (each press's OWN CO shift): shift_cure_demand[new_sku] += _cure_qty_per_shift

    Step 2 — Greedy building assignment (_assign_building_shift)  ← see §7
      Machine order: VMI → BJ → UNISTAGE → STAGE2 → (STAGE1 excluded here)

    Step 3 — Apply building output
      gt_inventory[sku] += qty_built   (Stage-1 excluded — carcass ≠ GT)
      machine_current_sku[machine] = last SKU produced this shift

    Step 3b — Stage-1 carcass scheduling
      For each Stage-2 SKU built this shift: assign carcass to eligible Stage-1 machines
      (proportional to capacity) via s1_sku_to_machines; record CO_Type="carcass" for
      utilization tracking. Does NOT touch gt_inventory. Stage-1 machines certified for no
      current Stage-2 SKU show 0% util — correct.
      RETIRED 6801 EXCLUDED: 6801 is plant-retired (14 live Stage-1 machines, §4.1) but was
      occasionally getting carcass in s1_sku_to_machines → the 38-live-machine feasibility rule
      R14 failed. It is now stripped from s1_sku_to_machines, so R14 passes and its carcass share
      redistributes across the 14 live Stage-1 machines.

    Step 4 — Curing simulation (same shift S)
      for each press:
        RUNNING:    cured = min(capacity, gt_inventory[sku], demand_left, remaining_mould_life×cavities)
                    gt_inventory -= cured; remaining_mould_life -= cycles_run
                    if remaining_mould_life == 0: MOULD CLEAN (480 min, mid-shift, overhang carries;
                      reset to 3000) — §5.1
        CHANGEOVER: idle for that press's CO shift (§5.3); CO charged 480 min; resets mould life

  End of day: apply CO transitions → press_state updated for Day D+1.
```

**Why simultaneous start is valid:** 1 building machine ≈ 240 GT/shift (2 min/tyre) feeds
≈ 4.3 curing presses (56 tyres/shift at 17 min × 2 cavities). The 2-min first-unit lag is
negligible and modelled as zero. `gt_inventory(S) += building_output(S)`;
`curing(S) ≤ gt_inventory(S)` — hard constraint unchanged.

**Shift-B/C CO restriction.** Building assignment runs with `allow_new_co = (S == "A")`. In
Shifts B/C a machine normally stays on its Shift-A anchor SKU. It may still CO if any of:
- `_deficit(cur_sku) ≤ 0` — this shift's deficit is already covered (check `_deficit`, NOT
  `demand_remaining`, which is the full 31-day total and would lock the machine — the RC1 bug).
- `sku ∈ co_target_skus` — must pre-build for a curing CO target.
- `sku ∈ starving_ri` — a running press with zero GT and demand left (hard starvation):
  `starving_ri = {s in ri_running_skus : gt_inventory[s] ≤ 0 and demand_remaining[s] > 0}`.

**Plant validation:** VMI 7001 (16") Shift A ran 330 units + 20-min CO + 100 units = 450/480 min
= 93.8% util; 7004 Shift B ran 3 campaigns / 2 same-size COs / all 14". One primary SKU per
machine per day — confirmed from the 09.04.2026 SIZE CHANGE PLAN.

---

## 7. Building assignment — `_assign_building_shift` (GLOBAL-ASSIGN, live)

`_GLOBAL_ASSIGN_ENABLED = True` supersedes the old per-machine greedy. Three phases run per
shift over all non-Stage-1 machines. Deficit for a SKU:
```
_defc(sku, buf) = min( max(0, shift_cure_demand[sku]×buf − projected_gt[sku]),
                       max(0, demand_remaining[sku] − projected_gt[sku]) )   ← demand-cap clamp
```
`projected_gt` tracks commitments across machines within the shift.

### Phase A — Continuation anchor (no CO)
Each machine continues its current SKU (zero CO cost), filling `_defc(cur, eff_buf)`.
Empty machines seed a dominant-inch-preferred deficit SKU. Round-trip buffer sizing (§9.2)
widens `eff_buf`. `_flex_reclaim` blocks a flex machine from continuing off-inch when a
dominant-inch SKU has deficit. **Captive-max** (`_CAPTIVE_MAX_ENABLED`, len(eligible)==1):
a captive machine builds its sole SKU to the full demand cap so it never idles while it has
unmet demand.

### Phase B — Global (machine, SKU) pair scoring
All remaining `(machine, SKU≠cur)` pairs with `_defc > 0` are scored **together** and assigned
best-first. Constraint = `min(flex_machine, flex_sku)` so the most constrained machines/SKUs
win (`_GLOBAL_CONSTRAINT_MODE = "below"`). Respects: same-inch/dominant preference,
soft-lock/flex off-inch gate, CO budget (`_max_cos(m)`), `CO_cost` guard, `MIN_CAMPAIGN_MINS`.
NRI candidates ranked by static ratio `demand/machine_total_demand` (`_BUILDING_RATIO_ENABLED`);
RI keeps raw-deficit ranking.

### Phase C — Forward buffer (see §8)
After A and B, idle machine-minutes bank GT for live-draw SKUs that are about to run dry.
**IUkeep (ADOPTED 2026-07-25):** among those about-to-starve, curable-path candidates, idle
machines now pick the **biggest unmet-demand gap first** (was nearest-to-starve). The two
safety gates are kept (draw>0 curable path + starvation-risk throttle → no waste GT); only the
ranking changed. Toggles `_IDLE_UNMET_ENABLED` / `_IDLE_UNMET_KEEP_GATE` (hardcoded ON; env
`IDLE_UNMET=0` reverts). Net +13,512 cured (July +12,732 → 89.11%), lower writeoff + starvation.

**Utilisation floor (target, not an override of the demand cap):** `MIN_SHIFT_UTILISATION = 0.77`
is documented but yields to the demand cap and the CO-cost guard. GT machines only — Stage-1 is
structurally ~47–50% by design (14 machines for ~11.5-equiv demand; 6801 retired).

---

## 8. NEW LOGIC — forward buffer + GT cap (8k) + risk gate

> **GT cap = `MAX_ENDOFDAY_GT_INVENTORY = 8000`** (RESOLVED — bc_config, env `GT_CAP_MAX`, default
> "8000"). `main.CLOUD_CONFIG` now pins **8000** too (previously a stale 7000), so the cap is 8,000
> **everywhere** and local↔cloud are byte-parity. The earlier "10k committed / cloud pins 7000 / a
> bc_config note mentions 6000 / final value undecided" open item is closed.

Three buffers now cover three distinct needs:
- **Flat buffer** (`GT_BUFFER_SHIFTS`): survive steady curing (VMI = 2, others = 1).
- **Round-trip buffer** (§9.2): survive a machine rotating to a partner SKU and back.
- **Forward buffer** (this section): bank GT for a live SKU's *own* future need using idle
  building time, up to 9 shifts ahead, no rotation partner required.

### 8.1 End-of-day GT-inventory cap (hard plant constraint)
- `bc_config.py`: **`MAX_ENDOFDAY_GT_INVENTORY = 8000`** (changed from 10,000). Total GT held
  overnight (summed over all SKUs, after curing + stale write-off) ≤ 8,000. Physical plant storage limit.
- Toggle `_ENDOFDAY_GT_CAP_ENABLED` (`b2c_pipeline.py:186`, default ON; env `GT_CAP=0` disables).
- **Enforced proactively**, not by reactive write-off: the forward buffer is bounded so
  overnight carry never exceeds 8k. Audit column `EndDay_GT_Inventory` **written to** the building
  "Daily GT & Carcass" sheet (was console-only). Verified: max **~4,978, 0 days over 8,000**.

### 8.2 Forward buffer / Phase C (the KPI lever)
Toggles: `_FORWARD_BUFFER_ENABLED` (`b2c_pipeline.py:197`, default ON, env `FWD_BUF`);
`_FWD_RISK_SHIFTS = 1.0` (`:206`, env `FWD_RISK`);
`GT_SHELF_LIFE_SHIFTS = GT_SHELF_LIFE_DAYS × 3 = 9` (`:211`).

Runs as **Phase C** in `_assign_building_shift`, after Phase A (continuation) and Phase B
(global CO pairing). For each machine with idle shift-minutes, while total projected inventory
< 8k, pick a candidate SKU that satisfies **all** of:
- **Live cure draw:** `shift_cure_demand[sku] > 0` — a press is actively pulling it (never a random SKU).
- **Demand left:** `demand_remaining[sku] > 0`.
- **Starvation risk:** `projected_gt[sku] < shift_cure_demand[sku] × _FWD_RISK_SHIFTS` — about to run dry.

```
forward target = min(demand_remaining, draw × GT_SHELF_LIFE_SHIFTS) − projected_gt
```
Shelf-safe: never more than 3 days (9 shifts) of the SKU's own draw. This auto-targets
**building-limited** SKUs (high draw → lots to prebuild) and auto-skips **press-limited** ones
(tiny draw → nothing to prebuild → no write-off). Candidate ranked: dominant inch first, avoid a
CO, most-starving, largest room.

**8k bound:** `entry_carry_gt + forward_added ≤ 8,000`. Base Phase A/B build is cure-neutral
(consumed same/next shift) so it is excluded; only the forward buffer adds net overnight carry.
Hard demand-cap clamp applies; respects the flex/soft-lock off-inch gate and CO budget.

**Why it works:** it uses idle late-month building capacity (machines 51–56% utilised on days
24–31) to feed presses that were STARVED (running, have demand, no GT). The **risk gate** stops
early-month front-loading and keeps the buffer unclogged so it can always respond to the
next SKU that runs dry.
```
Ungated (risk=0): buffer clogs, mean inventory 7,877 → 681k cured
Gated (risk=1.0): mean inventory 3,664           → 690k cured
```

### 8.3 Delivery-date / priority-flag committed-delivery SKUs (LIVE, local + cloud)

Client feature. A SKU is **committed** to a delivery date and must be fully **cured** by it — the
client accepts a KPI drop to hit the date (**meeting the date > overall KPI**). Master toggle
`bc_config.DELIVERY_PRIORITY_ENABLED` (top of `bc_config.py`, default ON; **INERT** with no priority
data → June + non-priority files run bit-for-bit).
- **Input (local):** two optional demand-file columns — **`Priority Flag`** (`0`/`1`/`Yes`) and
  **`Delivery Date`** (`DD/MM/YY`). A SKU is committed when its flag is set **OR** it has a valid
  date (a date implies commitment even if the flag is off); flag-set + no date → committed by
  **end of month**. **Input (cloud):** `jkt_demand.priorityFlag`/`deliveryDate`, gated by
  `jkt_plan_params.impPriorityFlag` (§17.1).
- **One shared EDF self-pacing signal** `required_presses = ceil(remaining / (rate · days_to_deadline))`
  drives BOTH stages (curing is derived from building, so a committed SKU needs presses **and** its GT
  built in time): **Phase-0 curing-CO** acquires/holds presses (a committed press is never CO'd away
  pre-deadline; acquisition capped at the SKU's **mould-pair count** — extra presses can't get moulds —
  and JIT-paced by `DP_PACE_MARGIN`, default **99 = fill to the mould cap = adopted "full mould-cap"**);
  **building** (`_assign_building_shift._bld_prio`) boosts a behind committed SKU EDF-first in
  `_key`/Phase-A/Phase-C.
- **All edits ORDERING-ONLY:** demand cap, historical inch-lock (§4.5), mould feasibility, GT
  shelf/cap all untouched; **no invented machine↔SKU pairs** (DB-allowable + DB moulds only).
  Env sub-levers: `DP_ACQUIRE`, `DP_RESERVE` (measured a NO-OP, kept as a guard), `DP_MOULDCAP`,
  `DP_PACE_MARGIN`, `DP_BLD`. A **"PRIORITY FULFILLMENT"** terminal table + `res["priority_report"]`
  give per-SKU demand / deadline / cured-by-deadline / shortfall / earliest-feasible date.
- **KPIs (deterministic 2-seed, mould-audit PASS, cap 0-over):** June ON==OFF = 634,086 (inert);
  July 672,696 → **671,324 (−1,372)** (HURL4 ~met, HTORE INFEASIBLE = only 1 allowable press);
  Aug 649,334 → **638,594 (−10,740)** (QRAT0 met, VRHE1 3,884→5,023 mould-contended, HRHB0 INFEASIBLE
  = 1 mould-pair, HRHP0 1,000→896 EDF contention). **Collateral ≈ 4 units lost per committed unit
  pulled earlier = shared-mould contention (a hard capacity limit, not misallocation); 2 SKUs are
  physically infeasible by date.**

### 8.4 Plant holidays (`PLANT_HOLIDAYS`, default INERT)

`bc_config.PLANT_HOLIDAYS` = a list of `"YYYY-MM-DD"` dates, or `False` (default = **INERT** — a
no-holiday run is **bit-for-bit identical** to before). **Cloud:** `jkt_holiday_calendar` →
`connection.read_db` → `run_cfg["holidays"]` → `main._apply_run_cfg` → `PLANT_HOLIDAYS`. A holiday is a
**fully idle day** (0 build, 0 cure); GT inventory **carries across** it; aging stays **CALENDAR-based**
(GT 3-day / carcass 1-day age across the holiday); utilization denominators **drop** the holiday shifts;
in-flight COs / cleans complete on the idle day.

Six fixes (all toggle-gated, all INERT without holidays):
- **#1 `_HOLIDAY_CO_DEFER`** (env `HOLIDAY_CO_DEFER`, default ON): **no NEW curing CO on a holiday** —
  each is deferred to the next WORKING day with CO budget (month-end overflow dropped). Makes
  planned / dynamic / scorer COs match the already-guarded reactive path. Verified **0 COs on holidays**.
- **#2 `_HOLIDAY_NO_PERISH`** (env `HOLIDAY_NO_PERISH_PREBUILD`, default ON): don't pre-build perishable
  stock that would age out over a holiday — caps the carcass PASS-2 lead **and** the GT forward-buffer
  window to the WORKING shifts reachable before the holiday. Only **SHRINKS** a target (never overbuilds).
  Cuts pre-holiday carcass write-off ~0.9–1.4k, cured-neutral.
- **#3 `_HOLIDAY_BRIDGE`** (env `HOLIDAY_BRIDGE_BUILD`, default **OFF**): pre-build extra GT *through* a
  holiday. **MEASURED NO-OP** — the existing 9-shift forward-buffer already bridges 1–2 day holidays,
  and ≥3-day holidays are shelf-blocked. Kept OFF + documented.
- **#4 reporting:** "Daily GT & Carcass" sheet gains a **`Holiday` flag** column + a row for every
  holiday date (idle day, carried GT, and GT/carcass aged out DURING the holiday now visible; the
  holiday date was previously missing).
- **#5 robustness:** unified the 3 holiday-index derivations onto one `plan_start` (fixed a
  def-time-default gotcha in `curing_consumption_dynamic._holiday_day_index_set`; aligns
  `bc_config.PLAN_START` / the curing module `PLAN_START` to the run's plan_start at pipeline entry, with
  a divergence warning) — the "imported BY VALUE" hazard also flagged for `RUNNING_MOULDS_MONTH`.
- **#6 wiring:** Day-1 setup COs (idle-press cold-start + Runner-Out Day-1 CO) gate on
  `_first_working_day`, not `day == 1`, so none fire if day 1 is a holiday.

**Honest KPI (July, no-holiday baseline 685,342 cured):** 15th → 672,724; 15th + 16th → 651,601;
15th + 27th → 649,486. A **single** holiday nets a small win vs pre-fix (+904, less waste); a
**multi**-holiday cured DROPS — the honest cost of #1 (no free changeovers on idle days, the chosen
realism model) — while **waste falls every case**. Feasibility clean (R8B/R8C demand-cap PASS, 0
overbuild); no-holiday bit-for-bit; cloud parity byte-identical.

---

## 9. Buffer mechanisms (flat + round-trip)

### 9.1 Flat GT buffer
```
GT_BUFFER_SHIFTS = 2 (VMI) / 1 (BJ, UNI, STAGE)
build target = shift_cure_demand[sku] × buf − projected_gt[sku]   (clamped to demand)
```
VMI uses 2 so sibling machines on the same inch each see a non-zero deficit and share the load.
Rules: never build if `demand_remaining = 0`; never build if no press runs/is scheduled for the
SKU this/next shift; carry-over is bounded by the buffer and rolls to shift S+1.

### 9.2 Round-trip buffer sizing
Live in `_assign_building_shift`, gated by `_ROUND_TRIP_BUFFER_ENABLED = True`. Reuses existing
constants (no new `bc_config.py` parameter). Before Phase A serves the current SKU, the machine
computes `effective_buf`:
```
Skip (fall back to flat _buf) if: only one eligible SKU, OR no other eligible SKU has unmet
  demand, OR no other eligible SKU has a real deficit.
Else pick rotation partner = eligible SKU (≠cur, demand_remaining>0, _deficit>0) with max deficit:
  partner_dwell   = max(MIN_CAMPAIGN_MINS, _deficit(partner)/rate)
  round_trip_mins = CO(cur→partner) + partner_dwell + CO(partner→cur)
  effective_buf   = max(flat_buf, round_trip_mins / SHIFT_MINS)
```
`effective_buf` only **widens** the buffer. Applies to all groups (VMI, BJ, Unistage, Stage-2).
Intent: if a machine is about to CO away to serve a live partner and come back, its current
SKU's press shouldn't starve while it's away.

---

## 10. Curing CO urgency & the legacy path (brief)

### 10.1 CO target urgency (two-level)
```
n    = Running_Press_Count[T]
rate = Qty_Per_Press_Per_Shift[T] × 3          rem = Updated_Demand_Qty[T]
H    = planning_days − current_day             current_days = rem / (n × rate)

Class A (CRITICAL): current_days > H × CO_CLASS_B_THRESHOLD → CO fires
Class B (HELPFUL):  below threshold → normally skipped
Sort: (urgency_class ASC, after_days ASC, −Priority_Score, −gt_signal, sku)
```
**demand_done_free exception:** when a Runner-In press's demand hits 0 mid-horizon it is added
to `demand_done_free`; these presses bypass the Class A gate and may CO to ANY target. Guard for
a Class B target: `gt_inventory[target] ≥ _cure_qty_per_shift(ct)`.
CO fires instantly (mid-shift) when RI demand is fulfilled; counts toward `MAX_CHANGEOVERS_PER_DAY`.
This **dynamic** CO is now charged the full 480 min like a planned one (§3.4/§5.2) — it eats the
rest of the shift, overhang carries to the next shift, and the new SKU starts mid-shift.

**Over-aggressiveness guard** (`curing_consumption_dynamic.py` ~line 339): before CO'ing an RI
press, verify `rem/((n−1)×rate) ≤ horizon_left`; else skip — remaining presses can't cover it.

**Curing press selection for an NRI target:** sort candidate presses by CT ascending (fastest
first); tie → prefer presses exclusive to this SKU (no opportunity cost); de-prioritise
multi-SKU presses (keep flexible).

### 10.2 Legacy LP path (`--legacy`, not current)
The original design planned all 31 days upfront with an LP + `DemandHeuristicAssigner`:
Phase 1a (Runner-In building with a mould-constrained priority boost), Phase 1b+2a (a joint
priority pool of NRI building + Runner-Out CO targets sharing residual capacity, with 70/30
front-loaded synthetic demand around each NRI's CO day), Phase 2b (pending CO scheduling),
Phase 3 (dynamic target lock), Phase 4 (GT-balance curing). It used a synthetic curing plan as
the building target, which is why the legacy path reported starvation events (building could miss
the synthetic plan). The rolling pipeline replaced the synthetic plan with real shift-by-shift
derivation. The legacy code remains only for regression reference.

The legacy demand cap was enforced by three layers (`_gt_remaining` TopUp tracker, a daily LP
`cur_mat` clip, and an LP per-SKU ceiling constraint) plus `OVERBUILD_BUFFER_FRAC = 0.2` headroom.
In the rolling pipeline these are replaced by the single `_defc` clamp (`min(gap, demand −
projected_gt)`) plus the forward-buffer hard clamp — simpler and tighter (near-zero overbuild).

---

## 11. Key invariants (never break)

1. **Demand cap is sacred.** Total GT built for any SKU ≤ `Requirement`. Enforced in three
   layers: the `projected_gt`/`demand_remaining` clamp inside `_defc`, the forward-buffer hard
   clamp, and the per-day accounting. `_deficit()`'s cap subtracts `projected_gt`, so total build
   ≤ demand (the overbuild fix — was 26 SKUs / 3,303 over; now ~0).
2. **Curing CO cap** = `MAX_CHANGEOVERS_PER_DAY` (hard plant constraint). Building CO cap =
   `MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT = 2`, raised to 4 for inch-flex machines via
   `_INCH_FLEX_EXTRA_COS`.
3. **Stage-2 cannot run without Stage-1 carcass** (same shift minimum, S-1 preferred). Unistage
   has no Stage-1 dependency.
4. **No waste GT.** Building output ≤ curing consumption — architectural, since curing is derived
   from building. Overnight carry ≤ `MAX_ENDOFDAY_GT_INVENTORY = 8,000`.

---

## 12. Config parameters (all in `bc_config.py` — single source of truth)

| Parameter | Value | Controls |
|-----------|-------|----------|
| `SHIFT_DURATION_MIN` / `SHIFTS_PER_DAY` | 480 / 3 | Shift length; A/B/C |
| `MAX_CHANGEOVERS_PER_DAY` | **17** | Curing CO cap/day (this cycle; was 12). Building COs uncapped by day. Cloud per-run cap comes from `jkt_plan_params.noOfChangeOver`, NOT this constant (§19.2). |
| `CO_CLASS_B_THRESHOLD` | 0.8 | Class A fires if `current_days > H × 0.8` |
| `MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT` | 2 | Building COs/machine/shift (4 for flex machines) |
| `MIN_CAMPAIGN_MINS` | 60 | Shortest production run (was 120 — blocked ≤2-press SKUs) |
| `MIN_CAMPAIGN_UNITS` | 40 | Minimum units per campaign |
| `OVERBUILD_BUFFER_FRAC` | 0.2 | LP headroom (legacy path) |
| `MIN_SHIFT_UTILISATION` | 0.77 | Aspirational GT-machine floor; yields to demand cap |
| `PRE_START_SHIFTS` | 2 | Building pre-start (**legacy path only**; rolling pipeline does not pre-build — §2.2) |
| `GT_BUFFER_SHIFTS` | 2 | Flat pre-build buffer (VMI=2, others=1) |
| `GT_SHELF_LIFE_DAYS` | 3 | GT cannot sit > 3 days; = `TOPUP_LOOKAHEAD_DAYS_GT` |
| `GT_SHELF_LIFE_SHIFTS` | 9 | `GT_SHELF_LIFE_DAYS × 3` — forward-buffer horizon |
| `CARCASS_SHELF_LIFE_DAYS` | 1 | Stage-1 carcass shelf life |
| `TOPUP_LOOKAHEAD_DAYS_GT` | 3 | TopUp pre-build days (legacy); must equal shelf life |
| `MAX_ENDOFDAY_GT_INVENTORY` | **8000** | Overnight GT-hold cap (env `GT_CAP_MAX`, default "8000"); **`main.CLOUD_CONFIG` pins 8000 too — local↔cloud aligned** (§8) |
| `DELIVERY_PRIORITY_ENABLED` | **True** | Master toggle for committed-delivery SKUs (§8.3); INERT with no priority data. Cloud-gated by `jkt_plan_params.impPriorityFlag` (§17.1) |
| `PLANT_HOLIDAYS` | **False** | List of `"YYYY-MM-DD"` idle days, or `False` = INERT (no-holiday run bit-for-bit identical). Cloud: `jkt_holiday_calendar` → `run_cfg["holidays"]` (§8.4) |
| `INCH_HIST_LOCK_ENABLED` / `_MIN_SHARE` / `_MAX_INCHES` | **True / 0.02 / 3** | Historical inch-lock: per-machine allowed-inch sets from the 4-mo report (§4.5) |
| `FLEX_SCARCE_INCH` (env) | **ON** | Lever A — flexible machines feed the scarcest allowed inch (§4.5) |
| `FIXED_ESCAPE_ENABLED` | **False** | Lever B (fixed-machine escape) — REJECTED, kept off (§4.5) |
| `CURING_CO_DURATION_SHIFTS` | 1 | Curing press idle during CO: one shift (its own CO shift, §5.3) |
| `CURING_CO_CHANGEOVER_MINS` | 480 | Curing CO duration (full shift); charged to planned AND dynamic COs |
| `MOULD_CLEAN_CYCLES` | **3000** | Cycles between mould cleans (= 6,000 tyres at 2 cavities); §5.1 |
| `MOULD_CLEAN_MINS` | **480** | Mould-clean duration = one shift; fires mid-shift when life hits 0 |
| `CURING_CAVITIES` | 2 | Cavities per press cycle (tyres per cycle) |
| `RUNNING_MOULDS_TABLE` | **"Daily_Running_Moulds"** | Day-0 running-moulds ETL table; single source for all 4 SQL sites (§17.1) |
| `DEFAULT_CYCLE_TIME_MIN` | 17.0 | Effective CT when SKU missing from DB |
| `LOAD_UNLOAD_BUFFER_MIN` / `PRESS_EFFICIENCY` | 2.3 / 0.94 | Effective-CT formula |
| `CAVITIES_PER_MOULD` | 2 | Tyres per curing cycle |
| Stage-2 CO multiplier | 2.0× | `co_time_map` Stage-2 diff × 2 (88→176) to discourage LP overload (legacy) |

**Live scheduling toggles (`b2c_pipeline.py`):** `_GLOBAL_ASSIGN_ENABLED`,
`_GLOBAL_CONSTRAINT_MODE="below"`, `_ROUND_TRIP_BUFFER_ENABLED`, `_BUILDING_RATIO_ENABLED`,
`_INCH_FLEX_ENABLED`, `_ENDOFDAY_GT_CAP_ENABLED`, `_FORWARD_BUFFER_ENABLED`, `_FWD_RISK_SHIFTS=1.0`,
`_MOULD_CLEAN_ENABLED` (§5.1), `_CO_SHIFT_SPREAD_ENABLED` (hardcoded `True`, §5.3),
`_HYBRID_CO_DEFER` (hardcoded `True`, §5.4), `_MIDMONTH_DEDUCT` (§21, default ON) — all ON by default.
`_CURING_RATIO_ENABLED`, `_PERSKU_FEED_V2` (hardcoded `True`, §5.4) and `_PRESS_STABLE` (§3.6,
default ON) in `curing_consumption_dynamic.py`.

---

## 13. Building CT (per-machine, real — not a proxy)

`b2c_pipeline.py` has a real per-machine building-CT dict `_BLD_CT_SEC` (seconds/unit),
corrected against plant norms in commit `f86f70b` (every value changed). Samples:
```
VMI:      7001=51.6s  7002=52.6s  7003=56.0s  7004=53.0s
BJ/UNI:   7101=83.0s  7104=87.0s  7501=90.0s  7502=90.0s
Stage-1:  6802=146s   8101=230s   7601=186s   (slower — carcass; 6801 retired)
```
GT machines ≈ 0.85–1.5 min/tyre — far faster than the old 17-min curing-CT proxy. The proxy
(and the old capacity figures derived from it) is retired.

---

## 14. Known calculation pitfalls

**Ratio / coverage metrics — universe must match on both sides.**
`fulfilled = total_demand − demand_remaining` is correct only when numerator and denominator
cover the **same SKU universe**. Rule: `set(SKUs in numerator) == set(SKUs in denominator)`
before writing any KPI. Excluded SKUs present in `total_demand` but absent from
`demand_remaining` silently inflate "fulfilled".

**Press ID format — `WCNAME_clean`, never `wcID`.** 30 of 167 presses have no `Master_WC_Master`
entry (NaN `wcID`) and would be silently dropped. Press key = `WCNAME_clean` everywhere.

**Overbuild demand-cap fix.** `_deficit()`'s cap now subtracts `projected_gt`, so
`Σ_days GT_built[SKU] ≤ Requirement[SKU]`. Side effect: GT write-off dropped sharply.

**CT column-key bug (fixed).** `cure_ct_map` was empty because of a wrong column key
(`"CT_Min"` vs `"CycleTime_min"`) — all SKUs fell back to `DEFAULT_CURING_CT = 17.0`.
Fixed in `b2c_pipeline.py`.

---

## 15. Known issues (current state)

| Issue | Root cause | Status |
|-------|-----------|--------|
| Stage-1 util 0% in output | Excluded from GT scheduling | **Fixed** — Step 3b simulates carcass per shift |
| `1325217613082TUNE0` (13") gap | 7106 (dom 13") served 15" SKUs | **Fixed** — BJ hard-inch filters |
| `1325218614088HURL0` zero production | 7104 missing from allowable | **Fixed** — 7104 added; FULLY MET |
| `1325218614088TVECE` partial | BJ oversubscription | **Fixed** — FULLY MET; BJ still structurally tight |
| Curing press IDs short by 30 | used `wcID` not `WCNAME_clean` | **Fixed** |
| BJ ~20k gap | Structurally oversubscribed | **Reframed** — dominant residual gap is curing-press-limited, not building. Fix = curing CO to add presses. |
| Feasibility R14 fail (39 machines) | Retired 6801 got Stage-1 carcass in `s1_sku_to_machines` | **Fixed** — 6801 stripped; carcass redistributes to the 14 live Stage-1 machines; R14 passes (§6 Step 3b) |

**Structural note superseded — see §16.** The old "~3% residual gap is curing-press /
15"-tooling limited" claim is now largely **incorrect**; the forward buffer proved most of it was
building-side starvation.

---

## 16. Current KPIs (ADOPTED approach 2026-08 — historical inch-lock + Lever A)

Committed default: **historical inch-lock (INCH_HIST_LOCK, 27 fixed / 12 flexible, ±2 band OFF) +
Lever A (FLEX_SCARCE_INCH)**, `MAX_CHANGEOVERS_PER_DAY = 17` (config now; the table below was measured
at 12), **8k GT cap** + dynamic buffer (β=2), forward buffer + IUkeep ON, curing press HOLD
(`_PRESS_STABLE`, §3.6) ON, mould-clean ON, carcass lead 2, `FIXED_ESCAPE` OFF. Inputs: per-month
`*_tomerji.xlsx`, month-keyed `Daily_Running_Moulds` (`plan_month`), single `gt_inventory_manual`,
building start-free. Fully deterministic; mould-audit PASS; demand-cap 0-over on all 3 months.

> **These are the historical-inch-lock baseline — DELIVERY_PRIORITY OFF** (`DELIVERY_PRIORITY_ENABLED`
> is INERT with no priority data). With `impPriorityFlag = 1` on committed-delivery inputs (§8.3) the
> effect is **June inert (634,086 ON==OFF) / July −1,372 / Aug −10,740** — accepted, since meeting the
> committed date outranks overall KPI.

> ⚠️ **AS-OF-DATE — these absolute totals DRIFT.** `Daily_Running_Moulds` is refreshed daily, so the
> same code + demand yields different totals on different days (July-full observed 685,342 → 675,817 →
> 689,837 → 688,984 over consecutive days). The table below was measured at `MAX_CHANGEOVERS_PER_DAY =
> 12` on one snapshot; the **committed config is now 17** (§12). **Compare only within one day** and read
> the deltas/mechanism below, not the absolute numbers (see the drift caveat in the header banner).

Now measured with the **hybrid curing-CO stack (items 1+2, §5.4)** ON and the **new demand files**
(§2.3). These SUPERSEDE the older pre-hybrid-CO numbers in earlier revisions (different CO stack + inputs):

| Month | Demand file | Demand | Days | GT built | GT cured | Coverage | Curing COs | Starv | Expired GT | Expired carcass |
|-------|-------------|--------|------|----------|----------|----------|-----------|-------|-----------|-----------------|
| June | `correct_june_plan.xlsx`     | 704,194 | 30 | 645,568 | **650,381** | **92.36%** | 256 | 2,654 | 666   | 10,008 |
| July | `july_correct_plan.xlsx`     | 727,779 | 31 | 679,223 | **685,342** | **94.17%** | 306 | 2,926 | 1,279 | 9,914  |
| Aug  | `august_demand_tomerji.xlsx` | 689,563 | 31 | 644,523 | **648,687** | **94.07%** | 284 | 3,234 | 1,420 | 10,264 |
| **Total** | | 2,121,536 | — | | **1,984,410** | — | — | — | — | — |

> Hybrid CO items 1+2 ON, no holiday, `MAX_CHANGEOVERS_PER_DAY = 12`, deterministic, feasibility-clean,
> **local == cloud byte-parity verified**. Measured with per-month opening GT (`gt_inventory_manual`
> `plan_month`-filtered) + the Lever-A determinism fix. Re-run after any DB opening-GT edit.

**Lever A delta:** June +570 / July +1,860 / Aug +1,374 = **+3,804 net** vs hist-lock-only, starvation
↓/flat all 3. **Lever B (FIXED_ESCAPE) REJECTED:** −2,572. **CO sweep** (10/12/14/16) confirmed 12 best
single value (July-only 16 = +2,677 if tuned per-month). The `INCH_HIST_LOCK=0` ±2-band baseline was
Aug 646,403 (the lock costs Aug −6k but nets positive: June +7.7k / July +25.3k).

> **These use the NEW inputs — NOT comparable to the pre-2026-08 numbers in older revisions of this
> doc** (which used `demand_may.xlsx` / `june_production_data.xlsx` and the ±2-band inch model).
> Per-SKU demand cap verified: 0 SKUs cured above demand. Overbuild ≤ rounding only.

**CO-cap sweep (forward buffer live):** all caps 8–14 land 98.9–99.9%. **cap=14 best**
(692,988 / 99.89%); **cap=12 lowest starvation** (911). Total curing COs scale 174→250 across the
sweep.

**KPI progression (historical context).** Before the forward buffer, the deterministic baseline at
CO=18 was ~670,744 cured / 96.7% (round-trip + building-ratio + curing-ratio + overbuild fix +
inch-flex, brute-forced machine set). Adding the forward buffer + end-of-day GT cap + risk gate at
CO=12 lifted May to ~690k / 99.5%. Two later corrections moved the figure to the **687,028 / 99.03%**
recorded above: the **priority-score v1 change** (min-max of requirement, discarding the weighted
score in `demand_may.xlsx` — costs ~3,291 cured on May) and the **`planning_days` fix**.
Plant benchmark (`Plant_vs_Scheduler_Report.pdf`): the AI plan (~99% cured, ~0 overbuild) beats the
plant's demand-capped effective fulfilment (95.9%, with ~61k wasted overbuild).
Re-run `python local_main.py` for the exact current figure.

### 16.1 KEY CORRECTION — the "structural ceiling" was mostly building-side
Earlier docs claimed the residual ~3% gap was **curing-press / 15"-tooling limited** and could
not be closed by scheduling. **This was wrong.** The forward buffer proved the gap was **mostly
building-side starvation** — idle late-month building machines were simply not feeding presses
that had running demand. Banking GT for those SKUs during idle time lifts coverage from ~96.7% to
**99.5%**. Treat the old "curing-limited ceiling" framing as **superseded**.

### 16.2 HONEST LIMIT — the forward buffer is a throughput accelerator, not a pacer
The forward buffer **front-loads** building: building daily-GT CV rises **12.4% → 16%**, the
*opposite* of the plant's flat ~22.4k/day curve. It maximises total throughput but makes the
building plan less even. Producing a flat, plant-like daily curve needs a **separate pacing lever**
(deliberately build less early), which is **not yet built**. Documented here as future work.

---

## 17. Data sources & outputs

### 17.1 DB tables (MySQL `jkplanningV1`)
| Table | Purpose |
|-------|---------|
| `Daily_Running_Moulds` | Which SKU each press runs today; mould life; press state (LH/RH → `WCNAME_clean`). **ONE table for all months, keyed by a `plan_month` column** ('YYYY-MM'); all 4 curing SQL sites filter `WHERE plan_month = RUNNING_MOULDS_MONTH` (auto-derived from `PLAN_START`, imported by-value). DB has 2026-06/07/08. `testing_`/`june_` variants retired. |
| `gt_inventory_manual` | Opening GT inventory per SKU (`sizeCode`, `gtInventory`) — **keyed by `plan_month`** (`WHERE plan_month = PLAN_MONTH`); each month uses its own opening GT |
| `Master_Curing_Design_CycleTime` | Raw cure time per SKU; missing → default 17.0 |
| `Master_Curing_Allowable_Machines_source` | SKU ↔ allowable curing press |
| `Master_Building_Allowable_Machines` | SKU ↔ building machines — single comma-separated `Machines` string (renamed from `_source`, reshaped from per-machine Y/Yes cols; commit `63d193f`); parsed via `_parse()` |
| `Building_Stage1/2_Best_Machines` | 3-month history — loaded but **NOT** used for eligibility (union removed) |
| `Master_Building_ChangeoverTime` | Building CO times (`Same Size(min)`, `Diff Size(min)`) |
| `Master_WC_Master` | Press code normalisation (`wcID`, `WCNAME`) — 30 presses have NaN `wcID` |
| `Master_Mapping_Mould_SKU` | Mould ↔ SKU compatibility for CO target check |
| `TBMStage1/2_ProductionEventData` | Currently running building machines at plan start |

**File inputs:** per-month `data/input/<month>_demand_tomerji.xlsx`;
`data/analysis_aug/machine_inch_dominant_4months_Apr-Jul.xlsx` (historical inch-lock source, §4.5);
`data/input/Cycle_time_Building.xlsx` (per-(SKU×machine) building CT); `feed_map.json`.

**Cloud DB contract (the boundary tables — see §19 for the full mapping).**
- **`connection.read_db` is PARAMS-ONLY** — it reads **everything** from **`jkt_plan_params`** alone:
  `planStartDate`/`planEndDate`, `noOfChangeOver`, `efficiency`, `impPriorityFlag`,
  `mouldAvailability`, `plantName`/`productName`. It does **not** read the `jkt_plan_presets` table at
  all — the **frontend** copies the selected preset's values into the params row on plan create/edit;
  the backend never writes back. Two BTP presets exist — **`BTP Preset Main`** (`impPriorityFlag = 1`)
  and **`BTP Preset`** (`impPriorityFlag = 0`); CTP presets untouched.
- **`jkt_demand`** (staged to a temp xlsx) now carries **`priorityFlag`** (`VARCHAR(10)`) +
  **`deliveryDate`** (`DATE`) columns for the committed-delivery feature (§8.3), consumed only when
  `impPriorityFlag = 1` (=0 → plain baseline even if the columns are populated). Priority *score* is
  still computed in code (min-max of requirement), so `jkt_demand` needs only `skuCode` + `requirement`
  for the base plan.
- **`main._set_plan_month(plan_start)`** derives `plan_month` per run and sets
  `RUNNING_MOULDS_MONTH`/`PLAN_MONTH` on `bc_config` + all engine modules, so a live cloud run for any
  month ≠ the `bc_config` file default reads the **correct** month's running-moulds + opening GT
  (fixes a prior multi-month cloud correctness bug).

### 17.2 Output files
All output paths are **stamped with `PLAN_START` (+ horizon)** so a new month never overwrites the
previous: `curing_consumption_<days>day_<date>.xlsx`, `bc_building_schedule_<date>.xlsx`,
`bc_curing_b2c_<date>.xlsx`.

| File | Contents |
|------|----------|
| `curing_consumption_<days>day_<date>.xlsx` | day sheets + CO_Schedule + Day0_Summary + Summary |
| `bc_building_schedule_<date>.xlsx` | Shift Schedule (+ `expired_GT` / `expired_carcass` waste rows); Machine Utilization; Daily GT & Carcass (`EndDay_GT_Inventory` audit col + `Expired_GT` / `Expired_Carcass` + `Holiday` flag cols); Demand Fulfillment (B2C) |
| `bc_curing_b2c_<date>.xlsx` | Shift Schedule; Changeover Plan (all curing COs); Machine Utilization; Demand Fulfillment |

**Building Shift Schedule — `Qty` / `CO_Mins` split.** CHANGEOVER rows previously wrote the CO
duration (minutes) into `Qty`, mixing units and minutes. Now CHANGEOVER rows have **`Qty = 0`** and a
separate **`CO_Mins`** column holds the duration; production/carcass rows have `CO_Mins = 0`. So `Qty`
is purely summable (= GT + carcass). `Machine_Group` shows the plant display labels (§4.1).

**Expired GT / carcass reporting (waste, aged out).** The building "Daily GT & Carcass" sheet gains
per-day **`Expired_GT`** + **`Expired_Carcass`** columns (after `Carcass_Produced`) plus the `Holiday`
flag (§8.4 #4). The building Shift Schedule gains **`expired_GT`** / **`expired_carcass`** waste **rows**
(`CO_Type`; `Machine = "—"`, `CO_Mins = 0`, `Qty = expired units`) — these are **DISPLAY-ONLY**, kept
**OUT of `prod_rows`**, so every aggregate / KPI / utilization / CO-count and the feasibility production
sum are **UNCHANGED** (`feasibility_test.py` skips these rows). The terminal prints **"Expired GT"** +
**"Expired carcass"** KPI lines; the result dict carries `gt_writeoff` / `carcass_writeoff`.
**Semantics:** expired GT / carcass are **WASTE** (aged out) — **EXCLUDED** from GT cured, coverage, and
usable-built (Demand-Fulfillment `Planned_Units` = gross built − expired GT); **INCLUDED** only in the
gross terminal "Total GT built" (produced then aged out). Expired GT = built-then-aged **plus** any Day-0
opening stock that expired. The Demand-Fulfillment per-SKU "expired GT/carcass" column is
**DEMAND-SCOPED** (can under-sum vs the authoritative terminal total by expiry on non-demand SKUs).

**Curing Shift Schedule — real wall-clock + CO visibility.** `StartTime`/`EndTime` are real
datetimes ("YYYY-MM-DD HH:MM", **mid-shift capable** — a partially-fed press ends mid-shift, a
mould-clean/CO overhang pushes the start later), matching the building sheet. New per-press-shift
**`CO_Mins`** / **`Mould_Clean_Mins`** columns make every CO (planned full-shift, dynamic mid-shift,
overhang) and clean **visible in the sheet** (not only in the Changeover Plan). A dynamic CO row
carries a plain `CO → <target>` remark; `STARVED (no GT)` / `IDLE (demand met)` are preserved for the
starvation-KPI reconciliation.

**Curing Demand Fulfillment — `CT_available` column.** Shows the SKU's real curing cycle time if it
exists in the curing CT table (`cure_ct_map`), else **`NA`** — meaning `CycleTime_min` fell back to
the `DEFAULT_CURING_CT = 17` default (no real data). (For May demand all 85 SKUs have real CT.)

---

## 18. Output-metric definitions & edge cases

### 18.1 Metrics
- **Utilisation(machine, shift)** = committed production minutes / 480.
- **Total_Units** = GT (Stage-2 + Unistage, the "Built" figure) + Carcass (Stage-1, internal
  intermediate). Keep the two separate when reporting demand fulfilment.
- **Starvation event** = a RUNNING curing press with demand remaining but zero GT that shift.
  Distinct from IDLE (demand met). Curing output "Remarks" distinguishes `STARVED (no GT)` vs
  `IDLE (demand met)`; the sheet's STARVED count reconciles with the printed KPI.
  **Exact definition (CONFIRMED — the client's intended metric, unchanged).** Counted per
  **(press × shift)**: an event iff **RUNNING AND `GT_Inventory == 0` AND `Qty == 0` AND
  `_demand_left > 0`** (the CURRENT SKU's remaining demand). It **excludes** CO shifts, mould-clean
  shifts, and demand-done idle presses. **RCA of the curing press-count dips:** ~**72% is
  building-supply GT-starvation** (the press stays committed — it did NOT CO away — but building
  fed it ~0 GT that day) and ~**28% is real month-end press-churn**. Tail-churn root cause =
  the month-end demand-done reassignment cascade + a small `horizon_left`: a mass Class-A flip plus
  an inflated `presses_needed = ceil(rem / (rate · horizon_left))` as `horizon_left → 1`. A
  **tail-churn damper is PROPOSED, not built.**
- **Expired GT / carcass** (`gt_writeoff` / `carcass_writeoff`) = stock that **aged out** before use
  (GT > 3 days, carcass > 1 day). WASTE — EXCLUDED from GT cured, coverage, and usable-built; counted
  only in the gross "Total GT built". See §17.2 for the sheet columns and semantics.

### 18.2 Edge cases
| Edge case | Handling |
|-----------|----------|
| Building capacity < RI consumption | Produce at max; press GT-limited (throughput gap, not negative inventory) |
| No viable CO target for a Runner-Out press | Press stays idle; logged |
| Daily curing CO budget exhausted | Defer lower-priority CO to next day; re-sort each day |
| RI demand fulfilled mid-cycle | Cap building to 0; freed press enters CO queue (`demand_done_free`) |
| Stage-1 machine unavailable for a Stage-2 assignment | Stage-2 removed that shift; carcass shortfall logged |
| Machine spare minutes < CO cost + one campaign | Machine treated as occupied; no new SKU |
| End-of-day GT would exceed 8k | Forward buffer bounded proactively so carry ≤ 8,000 |

---

## 19. Source files

| File | Role |
|------|------|
| `bc_config.py` | **SINGLE SOURCE OF TRUTH for all parameters.** Edit only this file. |
| `b2c_pipeline.py` | **ENTRY POINT** — `python b2c_pipeline.py`. Rolling pipeline; `_assign_building_shift`. |
| `curing_consumption_dynamic.py` | Phase 0 — Day-0 snapshot + eligibility filter + 31-day CO schedule (COScheduler/DaySimulator); reused by rolling loop. **The old `curing_consumption.py` was MERGED into this file and DELETED** — Phase-0 lives here now. |
| `curing_b2c.py` | Curing GT-balance simulation (legacy path); press IDs = `WCNAME_clean`. |
| `building_b2c.py` / `building.py` | Legacy LP building scheduler (`--legacy` only). |
| `approach/bc.md` | This file — architecture spec. |

---

## 20. Framing-

When answering "should we / what if / what's wrong":
1. **Which invariant does it touch?** (demand cap, CO caps, Stage-1/2 dependency, no-waste / 8k cap)
2. **Which SKU category?** (RI / RO / NRI)
3. **Which machines?** (Stage-1 under-utilised by design; VMI 7001/7003 soft-locked; flex = VMI+BJ)
4. **Config, logic, or data change?**
5. **Trade-off or strict improvement?** Most "never go idle" changes are trade-offs; the forward
   buffer's front-loading is one (throughput up, evenness down).
6. **Building vs curing bottleneck?** Since §16.1, do not assume a residual gap is
   curing-limited — check whether idle building machines are simply not feeding running presses.

---

## 19. Deployment (cloud) — added after the engine was frozen

The engine is unchanged by deployment; only its I/O is wrapped. **Three things
cross the local↔cloud boundary — demand, run params, outputs.** Masters and the
running-moulds snapshot are read from the DB by the engine's own ETL on both paths.
**Parity:** `local_main.py` == `main.py` byte-identical on all 7 KPIs for June/July/August, with
DELIVERY_PRIORITY active on cloud (read from `jkt_demand`, gated by `jkt_plan_params.impPriorityFlag`).

### 19.1 Modules

| File | Role |
|------|------|
| `local_main.py` | LOCAL entry — Excel in/out, reads `bc_config`. Parity anchor. |
| `main.py` | CLOUD orchestrator — `run_plan(plan_id)`: `read_db` → `_set_plan_month(plan_start)` (derives `plan_month`, sets `RUNNING_MOULDS_MONTH`/`PLAN_MONTH` on `bc_config` + engine modules) → inject cfg → engine → `write_db`. Holds `CLOUD_CONFIG` (pinned tuning params, now incl. `MAX_ENDOFDAY_GT_INVENTORY = 8000`, `CURING_PRESS_COUNT = 170`, `DELIVERY_PRIORITY_ENABLED`). |
| `connection.py` | DB adapter — **`read_db()` is PARAMS-ONLY** (reads only `jkt_plan_params`; `jkt_demand` staged separately) / `write_db()` (6 output tables incl `jkt_plan_moulds`), `now_ist()`. Does **not** read `jkt_plan_presets`. |
| `app.py` | Flask API (synchronous). |
| `Dockerfile` | `python:3.14-slim` + tzdata; gunicorn 1 worker / 4 threads / 1800 s. Published `anmolsaini07/jkt-btp-planning:v2` (& `:latest`), linux/amd64, ~145 MB. |
| `.dockerignore` | Re-includes 3 engine reference files read from the filesystem every run (else the cloud engine silently falls back and diverges from local): `data/input/Cycle_time_Building.xlsx`, `data/analysis_aug/machine_inch_dominant_aug.xlsx`, `data/analysis_aug/machine_inch_dominant_4months_Apr-Jul.xlsx`. |

### 19.2 What drives what

| Value | LOCAL source | CLOUD source |
|---|---|---|
| `PLAN_START` / `PLANNING_DAYS` | `bc_config` | `jkt_plan_params.planStartDate` / `planEndDate` |
| demand | `DEMAND_FILE` xlsx | `jkt_demand` (staged to a temp xlsx) |
| `MAX_CHANGEOVERS_PER_DAY` | `bc_config` | `jkt_plan_params.noOfChangeOver` |
| `PRESS_EFFICIENCY` | `ConsumptionConfig` | `jkt_plan_params.efficiency` (stored as %, ÷100) |
| committed-delivery ON/OFF (§8.3) | `DELIVERY_PRIORITY_ENABLED` + demand columns | `jkt_plan_params.impPriorityFlag` gates `jkt_demand.priorityFlag`/`deliveryDate` |
| the pinned tuning knobs incl. `RUNNING_MOULDS_TABLE`, `MAX_ENDOFDAY_GT_INVENTORY = 8000`, `CURING_PRESS_COUNT = 170` | `bc_config` | **`main.CLOUD_CONFIG`** (pinned before the engine imports) |

Editing `bc_config` therefore affects the **local run only**. To change a cloud
tuning value edit `main.CLOUD_CONFIG`; for a per-run value edit the DB row.

### 19.3 API (contract matches the existing JKT planning page)

Prefix `/app/v1/jkt/planning-scheduling`:

| Method | Path | Notes |
|---|---|---|
| `GET` | `/health` | liveness |
| `POST` | `/plan/generate-plan` | body `{"plan_id"}` (≤100 chars); **synchronous, 1–4 min**; 200 → `{status, mode, plan_id, elapsed_seconds}` |
| `GET` | `/plan/download/<plan_id>/building` | the building workbook (.xlsx) |
| `GET` | `/plan/download/<plan_id>/curing` | the curing workbook (.xlsx) |

Errors: `{status, stage, mode, plan_id, message}` + 400 / 404 / 409 (a run is
already in progress) / 422 / 500. One run at a time (`_RUN_LOCK`); re-running a
`plan_id` **overwrites** its rows. Full frontend guide: `API.md`.

### 19.4 Output tables (5, all stamped `plan_id`, timestamps IST)

`jkt_plan_building` · `jkt_plan_curing` · `jkt_plan_Infeasibility` · `jkt_plan_kpis` ·
`jkt_plan_capacityUtilisation` · `jkt_plan_moulds` (daily MouldInUse per SKU, `totalEligibleMoulds`
= SKU's eligible pool, PK `plan_id+Date+SKUCode`).

**MouldInUse = OCCUPIED moulds (redefined, `_mould_in_use_rows`).** `mouldsInUse` now =
**2 × committed presses** = the moulds **mounted on presses committed to the SKU** (presses HOLDING
its moulds), counted on **every held day** — including dry / GT-starved / idle days
(**forward-filled** across a hold). It **drops only when a press CO's AWAY**, not when the press
merely runs dry. It also counts **mould-clean** shifts (for the SKU) and **CO** shifts (credited to
the NEW SKU). Consequences: the value is **always EVEN** (2 cavities/press) and **≤ Total Eligible**.
Column names are **unchanged**; **display-only, no KPI change**. This fixes the frontend misread where
a starved-but-still-committed press looked CO'd-away (dropped 4→0 on a dry day even though it still
held its moulds). Previously `mouldsInUse` was the MAX over the day's 3 shifts of actively-running
moulds — that under-counted a committed press on a starved day.

* **`jkt_plan_kpis`** — **single row/plan, 15 metrics:** `demandFulfillment`,
  `capacityUtilisation` (curing), `building_`/`building_s2_`/`stage1_`/`vmi_`/`bj_`/`uniNarrow_capacityUtilisation`,
  `curingChangeovers`, `buildingChangeovers_sameSize`/`_diffSize`/`_total`. The curing
  `capacityUtilisation` denominator is `CURING_PRESS_COUNT = 170` (`b2c_pipeline._daily_capacity_util`:
  `cure_avail = CURING_PRESS_COUNT × 1440`), pinned in `CLOUD_CONFIG`.
* **`jkt_plan_capacityUtilisation`** — **DAILY curing utilisation** (composite PK `plan_id + date`),
  **`planning_days` rows/plan** (was mis-described as one overall/monthly row).
* **Priority score** is computed in code (min-max of requirement) — `jkt_demand`
  needs only `skuCode` + `requirement` for the base plan (the `priorityFlag`/`deliveryDate`
  columns drive the separate committed-delivery feature, §8.3).
* **`curingChangeovers`** = planned + dynamic (total).
* **`jkt_plan_Infeasibility`** stores UNMET + missing-master + **zero-production**
  SKUs. The last case matters: the engine labels a SKU UNMET only when
  `built + openingGT == 0`, so a SKU that built nothing but holds a few units of
  opening GT is labelled PARTIAL and would otherwise escape the report.
* Both workbooks are written to `PLAN_OUTPUT_DIR` (`/app/output` in Docker) and
  **kept** for download — mount a volume there or they vanish on image upgrade.

---

## 21. Mid-month plan start & production deduction — `_MIDMONTH_DEDUCT` (ADOPTED, default ON)

A plan may now start on **any calendar date**, not just the 1st. Implemented as a **two-pass wrapper**
`run_rolling_pipeline_2pass` in `b2c_pipeline.py` (toggle `_MIDMONTH_DEDUCT`, default ON). For a start
day == 1 it collapses to a **single run, bit-for-bit** identical to before.

### 21.1 The two passes (start day > 1)

1. **Run 1 — full month, ORIGINAL demand.** Simulate the whole month exactly as today.
2. **Deduct already-done production.** Take days `1 .. (start−1)` from Run 1 and estimate what the
   plant CURED in that window = **planned cured × a deterministic per-SKU factor ∈ [0.90, 1.05]**. The
   factor is **hashlib-derived** (NOT `random()` / `hash()` — those are non-deterministic across
   processes), so the deduction is reproducible. Subtract it from each SKU's demand (floored at **≥ 0**).
3. **Run 2 — replan `start → month-end` on the REDUCED demand**, **SEEDED from the day-start state**
   (start date, 07:00), so the plan Run 2 produces and the state it executes from AGREE:
   - **GT** carried as **dated lots** with **re-based ages** (a lot built on day K enters Run 2 with
     the age it actually has at the start boundary, so shelf-life/expiry stays correct);
   - **carcass** bank carried;
   - **press → SKU + running counts + mould life / mounts** carried, injected into the day-loop **and**
     into the **Phase-0 CO planner** via a new `initial_press_state` arg to `run_dynamic_consumption`
     (so the CO schedule starts from the real day-K press configuration, not a cold Day-0 snapshot).

**In-memory only** — no intermediate DB writes. **v1 uses the 1st-of-month DB opening snapshot for BOTH
runs** (running-moulds + opening GT); it does not re-pull a mid-month DB snapshot. Fully deterministic.

### 21.2 Both entry points call the wrapper (no API change)

**LOCAL** (`local_main.py`) and **CLOUD** (`main.run_plan`) both call `run_rolling_pipeline_2pass` with
the **same signature** — no API change. **local == cloud byte-identical** verified for full-month AND
mid-month, all 3 months. To run mid-month **locally**: set `bc_config.PLAN_START` to the start date and
`PLANNING_DAYS = (month-end − start + 1)`.

### 21.3 Two bug fixes shipped with it

- **(a) `curing_allowable` resync.** After the day-K press state is injected into Run 2, the curing
  allowable-machine map is **re-synced from the carried press state** — otherwise carried SKUs (running
  on a press but not in the fresh allowable map) were falsely flagged **"missing curing allowable
  machine."**
- **(b) Stage-1 carcass renderer made PLAN-RELATIVE.** The Stage-1 carcass renderer's "which day does
  Stage-2 consume" was a **day-of-month** value while the loop counts **plan-relative** days; the mismatch
  rendered **0 Stage-1 carcass for ANY mid-month start**. Fixed to use `_planday` (plan-relative). The
  full-month path is **bit-for-bit** unchanged.

A **sibling default-OFF** site — `_CV2_A4` / `_fifo_reconcile_greedy` (an alternate FIFO GT-lot
reconciliation) — is **flagged in-code** as the related seam, left OFF.

### 21.4 Feasibility auditor is mid-month-aware — `feasibility_test.py --midmonth-opening <file>`

The 2pass wrapper writes `<building_output>_midmonth_opening.json` (day-start **aged GT lots** +
**carcass totals**). Pass it to the auditor with `--midmonth-opening <file>` so R5 / R9C / R9G seed from
the **start-date inventory**, not the 1st-of-month DB snapshot. **Finding:** R5 / R9C / R9G mostly
reflect the plan's **real GT / carcass write-off** (they fail in the FULL-month run too), not a
mid-month opening artifact — the flag rules out the artifact, it does not paper over genuine waste.
