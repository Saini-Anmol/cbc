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
active curing presses (from the running-moulds snapshot named by
`RUNNING_MOULDS_TABLE` in `bc_config.py` — currently `Daily_Running_Moulds`, 167 presses).
The planning horizon is 31 days × 3 shifts
(A 07:00 / B 15:00 / C 23:00) × 480 min/shift.

---

## Running a new month — LOCAL run (edit only these 4 in `bc_config.py`)

```
PLAN_START           = datetime(2026, M, 1, 7, 0, 0)   # first shift
PLANNING_DAYS        = 30 or 31                          # days in the month
DEMAND_FILE          = ".../<month>_demand.xlsx"
RUNNING_MOULDS_TABLE = "<snapshot>_Daily_Running_Moulds" # Day-0 curing press state
```

**All 4 must be consistent for the same month** — a mixed config (e.g. July `PLAN_START`
with May `DEMAND_FILE`) runs without error but produces a meaningless plan.

Verified month/snapshot pairs:

| Month | DEMAND_FILE | PLANNING_DAYS | RUNNING_MOULDS_TABLE |
|-------|-------------|---------------|----------------------|
| May   | `demand_may.xlsx`                     | 31 | `Daily_Running_Moulds` |
| June  | `demand_tomerji_june_normalized.xlsx` | 30 | `testing_Daily_Running_Moulds` |
| July  | `july_demand_tomerJi1.xlsx`           | 31 | `june_Daily_Running_Moulds` |

Everything else derives automatically: **all 5 output paths are stamped** with `PLAN_START`
(+ horizon) — `bc_building_schedule_<date>.xlsx`, `bc_curing_b2c_<date>.xlsx`,
`curing_consumption_<days>day_<date>.xlsx` — so a new run never overwrites the previous month.
`RUNNING_MOULDS_TABLE` feeds all 4 curing SQL sites from one line.
Run `python local_main.py` (or `python b2c_pipeline.py` — equivalent for the rolling path).

> **These 4 lines affect the LOCAL run ONLY.** The cloud path (`main.py` / `app.py`) reads
> plan dates, horizon, demand, CO cap and efficiency from the DB per run — see
> **Deployment** below.

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
2. **Curing press changeover cap** is configurable via `MAX_CHANGEOVERS_PER_DAY` in `bc_config.py` (currently **12/day** this cycle; plant hard limit is **18/day**). Building machine changeovers are capped per-machine-per-shift by `MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT` (=2), **raised to 4 for inch-flex machines** via `_INCH_FLEX_EXTRA_COS`.
3. **Stage-2 cannot run without Stage-1 carcass** (same shift or S-1 preferred).
   Unistage machines have no Stage-1 dependency.
4. **No waste GT.** Building output ≤ curing consumption. In B2C, this is
   architecture: curing is derived from building, not the other way around.

---

## Curing press physical facts & changeover timing rule

### Mould setup + mould clean (NOW MODELLED — LIVE)

Each curing press holds **2 moulds**; the model uses `CURING_CAVITIES = 2` → **1 cycle
= 2 tyres**. Mould clean triggers after **3,000 cycles = 6,000 tyres**.

**Mould clean IS modelled as of this cycle** (previously it was absorbed into the CO
window — that text is superseded). Toggle `_MOULD_CLEAN_ENABLED` (default ON). Each press
carries `remaining_mould_life` (cycles, starts at `MOULD_CLEAN_CYCLES = 3000`, counts
down). At 0, an **8h clean = `MOULD_CLEAN_MINS = 480` = 1 shift** fires **immediately**
(mid-shift allowed): production caps at the 3,000th cycle, the clean fills the rest of that
shift, the overhang carries into the next shift, then life resets to 3,000. **A curing CO
also resets mould life** (the CO includes a clean — no separate clean on a CO shift).
Impact is tiny (~4 presses trigger, ≤−250 tyres) because v1 starts every press **fresh at
3,000**. **v2:** real opening mould life lives in the running-moulds DB tables
(`Mould life` / `Target life` columns, mean ~2,000 remaining) — loading it would make ~110
of 167 presses need a clean; deferred. Output columns in curing Machine Utilization:
`total_cycle` (renamed from `Total_Cycles`), `Mould_Clean_Mins`, `Mould_Clean_Utilization_%`,
`Remaining_Mould_Life`; identity `Available = Used + CO + MouldClean + Idle`.

**LH / RH press labelling:** Each physical curing press appears as two rows in
`RUNNING_MOULDS_TABLE` — one with suffix `LH` and one with `RH`.
`load_running_moulds()` strips the suffix and groups both rows into a single
press record keyed by the numeric press label (e.g. `"75206"`). CO events and
press_state always use this clean numeric key.

### Changeover timing — building starts simultaneously with the curing CO

Every curing CO costs `CURING_CO_CHANGEOVER_MINS = 480` (1 shift). Building for the new SKU
starts in the **same shift** as that press's CO (the simultaneity rule). Two CO kinds:

- **Planned** (static schedule): now **spread across A/B/C** (`_CO_SHIFT_SPREAD_ENABLED`, env
  hardcoded `True`) — placed in the shift the press finishes its old SKU (already
  finished → A so a free press never waits; won't finish today → A preemptively). Was
  hardcoded to Shift A (97% of CO downtime in A, artificial ~6.6k shift-A dip); now 90/30/27.
  Before its CO shift the press runs the OLD sku; on the CO shift = CHANGEOVER; after = NEW sku.
- **Dynamic** (reactive, fires the instant demand hits 0): **now charged** the full 480 min
  (previously FREE — the 78 dynamic COs cost nothing, slightly inflating KPI). It fires
  **mid-shift**, eats the rest of that shift, overhang carries into the next shift, so the
  **new SKU starts mid-shift** (e.g. 02:00), not at the boundary.

**Pre-build injection:** on each press's OWN CO shift (not blanket Shift A),
`shift_cure_demand[new_sku] += _cure_qty_per_shift(new_ct)` per CO-target press so building
starts the new SKU's GT simultaneously. Total CO time charged = (planned + dynamic) × 480.

**Curing Shift Schedule now carries real wall-clock `StartTime`/`EndTime`** ("YYYY-MM-DD HH:MM",
mid-shift capable) plus `CO_Mins` / `Mould_Clean_Mins` columns so every CO (planned full-shift,
dynamic mid-shift, and overhang) is visible in the sheet — not only in the Changeover Plan.

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

### Tier 1.6: Inch-Flexibility (`_INCH_FLEX_ENABLED = True` — LIVE, generalizes the soft-lock)

Plant production data (`data/plant_data/production_stage2_as.csv`) proves the plant runs VMI
machines across **4–7 inches** and BJ across ~2.4 — far more flexibly than our single-inch hard
locks. Inch-flexibility generalizes the 7001/7003 soft-lock to a **brute-forced-optimal machine set**:

- **Flex set = all VMI (6001-6004, 7001-7004) + all BJ (7101-7106, 7201)** — 15 machines. These are
  dropped from `_HARD` (`_HARD.pop`) so their full DB allowable (all eligible inches) becomes eligible.
- **UNI_NARROW (7501-7503) and STAGE2/IRM stay locked.** Brute-force found STAGE2 in the flex set is
  catastrophic (−57k, ~88% — Stage-1 carcass-dependency disruption) and UNI regresses (−3.9k, tight
  13" supply). Plant data agrees: IRM runs 1.5"/machine (most specialized), UNI 2.3".
- **Anti-carry-over reclamation guard** (the critical safety): a flex machine that carried onto an
  off-inch SKU must return to its dominant inch when a dominant-inch SKU regains deficit (Campaign 1
  `_flex_reclaim` + a merged-sort in Campaign 2+ that puts `inch_penalty` first so dominant always
  wins). Without this, generalizing the soft-lock reintroduces the carry-over lock-in that regressed
  earlier attempts (starvation-feed 96.4→92.9%). With it, dominant-inch press starvation went DOWN.
- **Off-inch gate**: a flex machine serves off-inch only when its dominant demand is done for the
  shift (`primary_demand_done`, the round-trip-widened buffer is filled). Off-inch target ranked by
  `_INCH_FLEX_OFFINCH_ORDER = "starving_first"` (beat `"demand_first"`), tiebreak within off-inch only.
- **Extra CO budget** (`_INCH_FLEX_EXTRA_COS = 2`): flex machines get 4 building COs/shift (vs 2) so
  they can take an off-inch excursion after exhausting same-inch work. The 30% CO-cost guard is
  bypassed for a flex machine going off-inch once its own inch is done (else the diff-inch CO — up to
  180 min — always exceeds 30% of a shift and blocks; the machine would otherwise idle).

**Honest finding:** off-inch building itself is a near-no-op (+52 units, ~2 off-inch COs/month —
building is not the bottleneck). The real +1,800 gain is the **extra CO budget** letting VMI+BJ
machines serve more of their *own* dominant-inch SKUs. The brute-force found the exact machine set
where that budget pays off (VMI+BJ) and avoids where it backfires (STAGE2/UNI). Fully toggle-gated;
`_INCH_FLEX_ENABLED = False` reproduces the 668,937 baseline bit-for-bit.

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
| `MAX_CHANGEOVERS_PER_DAY` | **12** | Curing CO cap per calendar day (12 this cycle for the surplus-release test; plant hard limit is 18). Single source of truth in `bc_config.py`. Sweep with forward-buffer live (8→14): all 98.9–99.9%; 14→692,988/99.89%, 12→690,180/99.49% (lowest starvation). Total curing COs scale 174→250. |
| `MAX_ENDOFDAY_GT_INVENTORY` | **8000** | Hard plant storage limit: total GT held overnight (all SKUs, after curing + writeoff) ≤ 8,000 (was 10,000). Enforced proactively by the forward-buffer (`_ENDOFDAY_GT_CAP_ENABLED`). Audit column `EndDay_GT_Inventory` written to building "Daily GT & Carcass" sheet (verified max ~4,978). |
| `MOULD_CLEAN_CYCLES` / `MOULD_CLEAN_MINS` | **3000 / 480** | Mould clean: 3,000 cycles (=6,000 tyres) → 8h (480 min = 1 shift) clean, then reset. Toggle `_MOULD_CLEAN_ENABLED` (default ON). See "Mould setup + mould clean". |
| `RUNNING_MOULDS_TABLE` | **"Daily\_Running\_Moulds"** | Single source of truth for the Day-0 running-moulds ETL table (curing press state). All 4 SQL sites import it — change ONE line per cycle. Options: `june_Daily_Running_Moulds` (July plan, 26-Jun, 169 presses), `testing_Daily_Running_Moulds` (June plan, 27-May, 165), `Daily_Running_Moulds` (live, 167). |
| `_CO_SHIFT_SPREAD_ENABLED` | **True** | Spread planned curing COs across A/B/C by when each press finishes its old SKU (hardcoded ON; flip to `False` → old shift-A-only). |
| `_FORWARD_BUFFER_ENABLED` / `_FWD_RISK_SHIFTS` | **ON / 1.0** | Forward-buffer (Phase C) + starvation-risk gate. `b2c_pipeline.py:197/206`. Idle machines pre-build about-to-starve SKUs up to `GT_SHELF_LIFE_SHIFTS = 9` ahead, capped at 10k. The +16k win (674k→690k). OFF (`FWD_BUF=0`) = 674,422 baseline. |
| `CO_CLASS_B_THRESHOLD` | **0.8** | CO fires if `current_days > H × 0.8`. Lower = more COs scheduled. |
| `GT_BUFFER_SHIFTS` | **2** | Rolling pipeline: shifts of GT to pre-build as buffer. VMI uses 2; BJ/UNI/STAGE use 1. |
| `MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT` | **2** | Cap on changeovers a single building machine may perform in one shift (rolling pipeline only). Plant averages 0.57 CO/shift/machine; upper bound of 2 lets one machine serve up to 3 SKU campaigns/shift. Must be `same_size_CO` to hold the 80% utilisation floor — 2× `diff_size_CO` (240 min VMI) would blow past it and is blocked. |
| `MIN_SHIFT_UTILISATION` | **0.80** | Target: each building machine should hit ≥80% production time per shift (384 of 480 min). **Defined in `bc_config.py` but not currently imported/referenced by `b2c_pipeline.py`, `building_b2c.py`, or `building.py`** — grep confirms no other file reads this constant. Treat it as an aspirational target documented in `bc.md` §19, not an enforced guard, until it is wired into the rolling-pipeline code. |
| `CURING_CO_DURATION_SHIFTS` | **1** | Shifts a curing press is idle during CO: Shift A only. Mould-clean removed from model. |
| `CURING_CO_CHANGEOVER_MINS` | **490** | Shift A duration for curing CO (full shift occupied). |
| `PRE_START_SHIFTS` | **2** | **LEGACY LP path only** (`building_b2c.py`). The **rolling pipeline does NOT pre-start** — building and curing both begin Day 1 Shift A 07:00 simultaneously (building runs before curing within each shift, so Day-1 GT is built and cured same shift; opening GT covers the rest). Day-1 starvation is negligible (~11 events). Do not assume pre-build in the rolling path. |
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

## Forward-buffer + GT cap + starvation-risk gate (LIVE, the +16k win)

> **The cap value is now `MAX_ENDOFDAY_GT_INVENTORY = 8000`** (user lowered it from 10,000). The
> numbers below (mean inv, the +16k decomposition) were measured at **10k** and remain the correct
> illustration of the *mechanism*; the tighter 8k cap is non-binding on the current plan (max end-day
> inv ~4,978), so it does not change the story — just read "10k" as "the end-of-day GT cap".

The residual gap was NOT curing-press/15"-tooling limited as previously believed — it was **mostly
building-side starvation**: late-month building machines sit 51–56% utilised (~15k idle machine-min/day)
while presses run (have demand) with zero GT. Three coupled changes close it: **674,422 → 690,180
cured (97.2% → 99.5%)**, starvation 1,508 → 911. All toggle-gated; OFF reproduces 674,422 bit-for-bit.

**Feature 1 — 10k end-of-day GT-inventory cap** (`_ENDOFDAY_GT_CAP_ENABLED`, `b2c_pipeline.py:186`,
env `GT_CAP`; value `MAX_ENDOFDAY_GT_INVENTORY = 10000` in `bc_config.py`). Total GT held overnight
(sum over all SKUs, after curing + writeoff) ≤ 10,000 — a hard plant storage limit. Enforced
**proactively** (the forward buffer is bounded so overnight carry never exceeds 10k), NOT reactive
writeoff. Audit column `EndDay_GT_Inventory` added to building "Daily GT & Carcass" sheet (verified
max ~4,900, 0 days over). By itself a near no-op (we carried ~0); its job is the legal headroom that
bounds Feature 2.

**Feature 2 — Forward-buffer (Phase C)** (`_FORWARD_BUFFER_ENABLED`, `b2c_pipeline.py:197`, env
`FWD_BUF`; `GT_SHELF_LIFE_SHIFTS = GT_SHELF_LIFE_DAYS × 3 = 9`, `b2c_pipeline.py:211`). Runs in
`_assign_building_shift` **after Phase A (continuation) and Phase B (global CO pairing)**. For each
machine with idle shift-minutes left, while total projected inventory < 10k:
- Candidate SKU must have a **live cure-draw** (`shift_cure_demand[sku] > 0` — a press is actively
  pulling it, never a random SKU), `demand_remaining > 0`, AND be at **starvation risk** (see gate).
- **Shelf-safe forward target** = `min(demand_remaining, draw × 9) − projected_gt`. Never more than
  3 days of the SKU's own draw → **auto-targets building-limited SKUs** (high draw = big room) and
  **auto-skips press-limited SKUs** (tiny draw → nothing to pre-build → no writeoff).
- **10k bound:** `entry_carry_gt + forward_added ≤ 10,000` (base Phase A/B build is cure-neutral so
  excluded; only the forward buffer adds net overnight carry). Hard demand-cap clamp; respects the
  flex/soft-lock off-inch gate and CO budget.

**Feature 3 — Starvation-risk gate** (`_FWD_RISK_SHIFTS = 1.0`, `b2c_pipeline.py:206`, env `FWD_RISK`).
Forward-build a SKU **only if** `projected_gt[sku] < draw × _FWD_RISK_SHIFTS` (on-hand below 1 shift =
about to run dry). This is the decisive multiplier: without it the buffer front-loads every SKU whenever
slack exists → **clogs the 10k buffer** (mean inv 7,877) → can't respond when a SKU later starves →
only 681k. With it, well-supplied SKUs are skipped (mean inv 3,664, lots of headroom) → the scarce
buffer is always available for the next SKU that runs dry → **690k (+9k over ungated)**. Higher = more
aggressive/more front-load; `0` = gate off.

**Three buffers, three different jobs:** flat `GT_BUFFER_SHIFTS` (steady curing, 2 shifts) · round-trip
buffer (survive a rotation to a partner and back — needs a partner) · **forward-buffer** (bank GT for a
live SKU's own future need using idle time, up to 9 shifts, **no partner required** — the gap the other
two never covered).

**Honest limit:** forward-buffer is a throughput **accelerator** — it cures more, *earlier*, so it
**front-loads building** (daily-GT CV 12.4% → 16%), the opposite of the plant's flat ~22.4k/day curve.
Making the building plan flat (like the plant) needs a separate **pacing lever** (build less early),
not yet built. `result_checker`-audited: no overbuild beyond the known 162-unit (0.02%) min-campaign
rounding; every physical/demand invariant holds.

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
      (Live path = GLOBAL-ASSIGN: Phase A continuation + captive-max, Phase B global
       (machine,SKU) pair scoring with constraint=min(flex_m,flex_s), mode="below")
      Phase C — Forward-buffer (LIVE): idle machines pre-build starvation-risk SKUs
        (on-hand < 1 shift of draw) up to 9 shifts (3-day shelf), bounded by the 10k
        end-of-day GT cap. See "Forward-buffer + 10k GT cap" section above.

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
| BJ 20k gap (`1D25212812086FXPC0` etc.) | Believed curing-press limited | **CORRECTED Jul 14 2026 — it was building-side starvation, not curing presses.** The forward-buffer (feed idle machines' GT to starving presses 3 days ahead) closed most of this gap → overall coverage 99.5%. The old "fix = curing CO to add presses" diagnosis was wrong for this class of gap. |
| ~59k unmet demand (7 SKUs) | No allowable building machine in master data | **Resolved as of Jul 10 2026** — confirmed via `bc_building_schedule_2026-05-01.xlsx` (Demand Fulfillment (B2C) sheet): 0 of 97 SKUs have `Eligible_Machines == 0`. Table renamed `Master_Building_Allowable_Machines_source` → `Master_Building_Allowable_Machines`. Remaining unmet demand (8 UNMET SKUs, 3,687 units total) all have eligible building machines — the gap is now production-days/curing-throughput limited, not a missing-data problem. |
| Curing press IDs short by 30 | `_load_press_state` used `wcID` instead of `WCNAME_clean` | **Fixed:** curing_b2c.py uses WCNAME_clean (e.g. "75206") |
| **Phase-0 CO budget used the WRONG horizon** (30-day months) | `run_dynamic_consumption` received `planning_days` but never forwarded it to `COScheduler.schedule()`, which fell back to its **import-time default** `planning_days = PLANNING_DAYS` (the `bc_config` constant). `simulate()` read the module global the same way. | **Fixed (commit `2b11cda`)** — both now take/forward `planning_days`. Symptom: June (30d) on the cloud path got `12 × 31 = 372` CO slots instead of `12 × 30 = 360` → 189 COs instead of 163 → **−8,508 cured** (91.91% vs 93.06%). Hidden because `bc_config.PLANNING_DAYS = 31` and May/July are both 31-day months, so the stale value coincidentally matched. **Local runs were never wrong in practice** (the constant is edited to match the month); it only bit when a caller passed a horizon differing from the constant — i.e. the cloud path. |

---

## Relevant source files

| File | Role |
|------|------|
| [bc_config.py](bc_config.py) | **SINGLE SOURCE OF TRUTH for ALL parameters**. Edit only this file. |
| [b2c_pipeline.py](b2c_pipeline.py) | **CORRECT ENTRY POINT** — `python b2c_pipeline.py`. Runs curing_consumption_dynamic → building_b2c → curing_b2c. |
| [building_b2c.py](building_b2c.py) | B2C building scheduler — Phase 1a/1b/2a/2b/3. |
| [curing_b2c.py](curing_b2c.py) | B2C curing simulation (Phase 4) — GT-balance shift-by-shift. Press IDs = `WCNAME_clean`. |
| [curing_consumption.py](curing_consumption.py) | Phase 0 — Day 0 snapshot. Reads from `bc_config.RUNNING_MOULDS_TABLE`. |
| [curing_consumption_dynamic.py](curing_consumption_dynamic.py) | Phase 0 Extended — 31-day CO schedule. Class A + demand-done Class B, cap = `MAX_CHANGEOVERS_PER_DAY`. |
| [building.py](building.py) | Base building machinery (LP engine + DemandHeuristicAssigner). |
| [approach/bc.md](approach/bc.md) | Full B2C architecture spec (authoritative). |

### Deployment layer (cloud)

| File | Role |
|------|------|
| [local_main.py](local_main.py) | **LOCAL entry point** — Excel in/out, reads `bc_config`. Parity anchor. |
| [main.py](main.py) | **CLOUD orchestrator** — `run_plan(plan_id)`: `read_db` → inject cfg → engine → `write_db`. Holds `CLOUD_CONFIG` (18 pinned params). |
| [connection.py](connection.py) | DB adapter — `read_db()` (3 input tables) / `write_db()` (4 output tables) + `now_ist()`. |
| [app.py](app.py) | **Flask API** — `POST /app/v1/jkt/planning-scheduling/plan/generate-plan {plan_id}`, `GET /health`. Synchronous. |
| [approach/deployment.md](approach/deployment.md) | Deployment spec — DB contract, config mapping, phases, parity-gate results. |
| [requirements.txt](requirements.txt) | Pinned runtime deps (Flask, SQLAlchemy, PyMySQL, pandas, numpy, scipy, openpyxl). |

---

## Deployment — local vs cloud (what drives what)

One engine, two I/O paths. **Only these cross the boundary: demand, per-run params, outputs.**
Masters + running-moulds are read from the DB by the engine's own ETL on both paths.

| Value | LOCAL source | CLOUD source | Editing `bc_config` affects cloud? |
|-------|--------------|--------------|-----------------------------------|
| `PLAN_START` / `PLANNING_DAYS` | `bc_config` | `jkt_plan_params.planStartDate/planEndDate` | **No** |
| `DEMAND_FILE` | `bc_config` | `jkt_demand` (staged to a temp xlsx) | **No** |
| `MAX_CHANGEOVERS_PER_DAY` | `bc_config` | `jkt_plan_params.noOfChangeOver` | **No** |
| `PRESS_EFFICIENCY` | `ConsumptionConfig` | `jkt_plan_params.efficiency` (stored as %, ÷100) | **No** |
| The 18 tuning knobs (GT cap, mould clean, campaign mins, `RUNNING_MOULDS_TABLE`, …) | `bc_config` | **`main.CLOUD_CONFIG`** (pinned, applied before the engine imports) | **No — pinned** |

To change a **cloud** tuning value edit `main.CLOUD_CONFIG`; to change a cloud per-run value
edit the **DB row**. `bc_config` drives the local run only.

**Priority score** is computed in code (min-max of requirement) in
`curing_consumption.load_demand` + `b2c_pipeline` — so `jkt_demand` needs only
`skuCode` + `requirement`, no priority column. Any priority column in a local
Excel is ignored.

**API:** synchronous (returns `elapsed_seconds` when the run finishes, ~1–4 min);
planning mode only; re-run **overwrites** that `plan_id`; 409 = a run already in progress.

---

## Known Calculation Pitfalls

### Ratio / coverage metrics — universe must match on both sides

```
fulfilled = total_demand - demand_remaining
```
Only correct when `total_demand` and `demand_remaining` cover the **same SKU universe**.
Excluded SKUs in `total_demand` but absent from `demand_remaining` silently inflate "fulfilled".

**Rule:** `set(SKUs in numerator) == set(SKUs in denominator)` before writing any KPI.

### ConsolidatedPriorityScore is COMPUTED in code (v1), not read from the file

```
score(sku) = (req − req_min) / (req_max − req_min)     # min-max over the whole demand
```
Computed in `curing_consumption.load_demand` and `b2c_pipeline` (`priority_score_map`), from the
**per-SKU summed** requirement. **Any priority column in the demand file/DB is deliberately
ignored** — so `jkt_demand` needs only `skuCode` + `requirement`, and local Excel and cloud DB
score identically. Guard: `req_max == req_min` → uniform 1.0.

**Consequence:** `demand_may.xlsx` / the June file ship a *weighted* score (market + target-date,
via the `MarketScore` / `ReqRatio` columns) that v1 throws away. Do NOT assume swapping the
scoring back is KPI-neutral — on May it is worth **+3,291 cured (+0.5pp)**. The
`jkt_plan_params` weightage columns (`marketWeightage`, `quantityWeightage`,
`targetdateWeightage`, per-market ints) exist for a v2 weighted score and are dormant today.

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

## Current KPIs (confirmed Jul 20 2026 — priority-score v1 + planning_days fix live)

The rolling pipeline is **deterministic** (verified: 4 identical runs, fixed and random
`PYTHONHASHSEED`). Committed default = forward-buffer risk=1.0 + **8k cap** ON, surplus-release ON,
mould-clean ON, CO-shift-spread ON, `MAX_CHANGEOVERS_PER_DAY = 12`.

**Verified on all 3 months, LOCAL and CLOUD byte-identical** (the parity gate — see
`approach/deployment.md`). Each month uses its own Day-0 snapshot:

| Month | Demand | Running moulds | GT built | GT cured | Coverage | Curing COs | Cleans | Writeoff | Starvation |
|-------|--------|----------------|----------|----------|----------|-----------|--------|----------|------------|
| May  | 693,748 (85 SKUs)  | `Daily_Running_Moulds`         | 681,029 | **687,028** | **99.03%** | 200 | 4 | 796   | 1,340 |
| June | 742,094 (120 SKUs) | `testing_Daily_Running_Moulds` | 687,371 | **690,556** | **93.06%** | 177 | 1 | 3,297 | 700 |
| July | 779,000 (105 SKUs) | `june_Daily_Running_Moulds`    | 700,255 | **703,365** | **90.29%** | 182 | 3 | 2,942 | 981 |

> **May moved 690,319 → 687,028 (99.5% → 99.03%) — this is the priority-score v1 change, not a
> regression.** `demand_may.xlsx` and the June file carry a **weighted** `ConsolidatedPriorityScore`
> (market + target-date), which v1 **deliberately discards** in favour of pure min-max of
> `Requirement` (see "Known Calculation Pitfalls"). Measured cost on May: **−3,291 cured (−0.5pp)**.
> July is unaffected because its file's score already equals min-max(requirement) exactly.
> If coverage matters more than scoring simplicity, restoring the weighted score (or implementing
> the `jkt_plan_params` weightages) is the lever — it is currently dormant by choice.

**Earlier history:** 690,180 → 690,319 came from (a) the 8k cap (was 10k), (b) mould clean (−~250),
(c) charging dynamic COs (was free), (d) CO shift-spread — all net ~flat, physically honest.

**Live scheduling toggles producing this baseline** (all in `b2c_pipeline.py` unless noted):
- `_FORWARD_BUFFER_ENABLED = True` / `_FWD_RISK_SHIFTS = 1.0` / `_ENDOFDAY_GT_CAP_ENABLED = True` — the +16k win (see "Forward-buffer + 10k GT cap" section). OFF (`GT_CAP=0 FWD_BUF=0`) = 674,422 / 97.2% bit-for-bit.
- `_GLOBAL_ASSIGN_ENABLED = True` / `_GLOBAL_CONSTRAINT_MODE = "below"` / `_CAPTIVE_MAX_ENABLED = True` — global (machine,SKU) pair scoring supersedes the sequential per-machine greedy (this was the prior +6.5k win to 681k; captive machine 7301 handled generally, no hardcoded rule).
- `_ROUND_TRIP_BUFFER_ENABLED = True`, `_BUILDING_RATIO_ENABLED = True`, `_CURING_RATIO_ENABLED = True` (`curing_consumption_dynamic.py`), `_INCH_FLEX_ENABLED = True` (VMI+BJ), `_SURPLUS_RELEASE_ENABLED = True` (`curing_consumption_dynamic.py`).

**KPI progression** (deterministic): 668,937/96.4% (round-trip + ratios + overbuild fix) →
670,744/96.7% (inch-flex) → 681,078 (global-assign + captive-max) → **690,180/99.5%
(forward-buffer + 10k cap + risk gate)**. Gain decomposition of the last step: forward-buffer
ungated +6.8k (→681k), risk gate +9k more (→690k, by keeping the 10k buffer unclogged).

**CO-cap sweep (forward-buffer live, 8→14):** all 98.9–99.9%; cap=14 best (692,988 / 99.89%,
flattest CV 15.9%), cap=12 lowest starvation (911). Total curing COs scale 174→250. All ≤ plant
limit of 18. The CO cap is now a secondary knob — the forward-buffer dominates.

**MAJOR CORRECTION to the old "structural ceiling" claim (do not repeat the old belief):**
The residual ~3% gap was **previously documented as curing-press / 15"-tooling limited**. The
forward-buffer proved that was **wrong** — the gap was **mostly building-side starvation**: idle
late-month machines (51–56% util, ~15k idle machine-min/day) not feeding presses that had demand.
Feeding them 3 days ahead lifts coverage to 99.5%. The true remaining gap is now <1%.

**Honest limit — consistency (NOT yet solved):** the forward-buffer is a throughput **accelerator**
(cures more, earlier), so it **front-loads building** — daily-GT CV 12.4% → 16%, the opposite of the
plant's flat ~22.4k/day curve (plant CV 3.8%). Matching the plant's flat building plan needs a
separate **pacing lever** (build less early to spread demand across the month), which is the next
piece of work. Forward-buffer (KPI) and pacing (consistency) are opposed levers.

**Known calculation pitfall fixed (Jul 2026):**
- CT bug: `cure_ct_map` was always empty due to wrong column key (`"CT_Min"` vs `"CycleTime_min"`).
  All SKUs fell back to `DEFAULT_CURING_CT = 17.0`. Fixed in `b2c_pipeline.py` line ~1132.
- Building CT: `_BLD_CT_SEC` (per-machine seconds/unit) corrected against plant production norms
  in commit `f86f70b` — every value in the dict changed (e.g. `6801` 150→127, `8101` 300→230,
  `7101` 102.0→83.0).
