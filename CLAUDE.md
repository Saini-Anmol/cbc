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
38 building machines (14 Stage-1 carcass, 6 Stage-2 GT, 18 Unistage GT), 167
active curing presses (from the running-moulds snapshot named by
`RUNNING_MOULDS_TABLE` in `bc_config.py` — currently `Daily_Running_Moulds`, 167 presses).
The planning horizon is 31 days × 3 shifts
(A 07:00 / B 15:00 / C 23:00) × 480 min/shift.

> **⚠️ DB-DRIFT CAVEAT — read before comparing any KPI in this file.** `Daily_Running_Moulds`
> is a **LIVE, daily-refreshed** table: the Day-0 press snapshot (which presses are on which SKU,
> mould life) changes **every calendar day**, so the SAME code + SAME demand produce DIFFERENT
> absolute KPIs on different days. Example: July full-month drifted **685,342 → 675,817 → 689,837
> → 688,984** cured over consecutive days with no code change. **Only ever compare runs made on
> the SAME day**, and treat every absolute KPI in this file (tables, ± deltas) as **as-of-date**,
> not a fixed target. Document approach / toggles / mechanisms; do not chase the numbers.

---

## Running a new month — LOCAL run (edit only these 4 in `bc_config.py`)

```
PLAN_START           = datetime(2026, M, 1, 7, 0, 0)   # first shift
PLANNING_DAYS        = 30 or 31                          # days in the month
DEMAND_FILE          = ".../<month>_demand_tomerji.xlsx" # per-month demand
RUNNING_MOULDS_TABLE = "Daily_Running_Moulds"            # ALWAYS this single table now
```

**These must be consistent for the same month.** `RUNNING_MOULDS_MONTH` is **auto-derived
from `PLAN_START`** (`PLAN_START.strftime("%Y-%m")`, env-overridable) so it can never disagree.

> **Running-moulds schema (2026-08): ONE table + a `plan_month` column.** All months now live
> in the single `Daily_Running_Moulds` table, discriminated by `plan_month` ('YYYY-MM'). All 4
> curing SQL sites filter `WHERE plan_month = RUNNING_MOULDS_MONTH`. The DB has plan_month data
> for 2026-06/07/08. The retired `testing_`/`june_Daily_Running_Moulds` variants are gone.
> **`RUNNING_MOULDS_MONTH` is imported BY VALUE** into curing_consumption/curing_b2c — a harness
> that sets `bc.PLAN_START` after import must ALSO export env `RUNNING_MOULDS_MONTH`.
> **Opening GT** = `gt_inventory_manual`, **filtered by `plan_month`** (`WHERE plan_month = PLAN_MONTH`,
> auto-derived from `PLAN_START`, imported by value) → each month uses its own opening GT
> (June 7,644 / July 8,989 / Aug 7,488).
> **No building-running-machines** this cycle: building starts FREE; only the historical
> inch-lock drives the initial building state (see "Historical inch-lock" below).

Verified month inputs (this cycle):

| Month | DEMAND_FILE | Demand | PLANNING_DAYS | plan_month |
|-------|-------------|--------|---------------|-----------|
| June  | `correct_june_plan.xlsx`     | 704,194 | 30 | 2026-06 |
| July  | `july_correct_plan.xlsx`     | 727,779 | 31 | 2026-07 |
| Aug   | `august_demand_tomerji.xlsx` | 689,563 | 31 | 2026-08 |

> ⚠️ June is now `correct_june_plan.xlsx` (704,194, 97 SKUs), **NOT** the old `june_production_data.xlsx`
> (656,608) — June KPIs are not comparable to older June baselines. July is now `july_correct_plan.xlsx`
> (727,779, 102 SKUs); August (`august_demand_tomerji.xlsx`, updated in place) is 689,563 (91 SKUs).

Everything else derives automatically: **all 5 output paths are stamped** with `PLAN_START`
(+ horizon) — `bc_building_schedule_<date>.xlsx`, `bc_curing_b2c_<date>.xlsx`,
`curing_consumption_<days>day_<date>.xlsx` — so a new run never overwrites the previous month.
`RUNNING_MOULDS_TABLE` feeds all 4 curing SQL sites from one line.
Run `python local_main.py` (or `python b2c_pipeline.py` — equivalent for the rolling path).

> **These lines affect the LOCAL run ONLY.** The cloud path (`main.py` / `app.py`) reads
> plan dates, horizon, demand, CO cap and efficiency from the DB per run — see
> **Deployment** below. The historical inch-lock + Lever A are pinned ON in `main.CLOUD_CONFIG`.

---

## Historical inch-lock — the CURRENT building inch model (ADOPTED 2026-08)

This **replaces the anchor ±2 band** as the building inch policy. Default ON; verified across
June/July/August (deterministic, mould-audit PASS, demand-cap 0-over). Toggles in `bc_config.py`,
loader/logic in `b2c_pipeline.py`. **`INCH_HIST_LOCK=0` reverts to the ±2 band bit-for-bit.**

- **`INCH_HIST_LOCK` (ADOPTED, ON).** Per-machine ALLOWED-INCH sets come from the 4-month plant
  report `data/analysis_aug/machine_inch_dominant_4months_Apr-Jul.xlsx` (sheet
  `Inch_Counts_Matrix`): an inch is kept if ≥ `INCH_HIST_LOCK_MIN_SHARE` (0.02) of a machine's
  records, ranked, capped at `INCH_HIST_LOCK_MAX_INCHES` (3). At 2% this yields exactly **27 FIXED
  machines (single historical inch, ZERO diff-size CO ever) + 12 FLEXIBLE (their ranked historical
  inches only)**. The ±2 band is DISCONTINUED, so historically-evidenced +3 jumps are legal
  (7001 15↔18, 7103 13↔16, 7201 16↔13, 7803 15↔12, 8301 12↔15) but an inch a machine never ran is
  not. Enforced by the allowable-machine strip + `machine_locked_inches` gate + `_inch_ok`→True +
  Stage-1 `_s1_inch_ok`. `INCH_HIST_LOCK_STAGE1` OFF (Stage-1 stays demand-optimal + carcass-FEASIBLE).
- **Lever A `FLEX_SCARCE_INCH` (ADOPTED, ON).** Among a FLEXIBLE machine's allowed inches, prefer
  the SCARCEST (biggest live curing-draw shortfall) over same-inch stickiness — feeds the
  about-to-starve 15"/13" the fixed machines can't reach. **+3,804 net (June +570 / July +1,860 /
  Aug +1,374), starvation ↓/flat all 3, no regression.**
- **Lever B `FIXED_ESCAPE` (REJECTED, OFF, code retained).** A fixed machine takes ≤1 diff-CO to a
  scarce inch only after its own inch's WHOLE-MONTH demand is done. Regresses all 3 months
  (−2,572); the stranded 14"/17" idle capacity only frees late-month and can't be redirected
  profitably. (The gate MUST use `demand_remaining − projected_gt`, not buffered `_defc` — the
  buffered version premature-abandons for −31.7k.)

**Why fixed machines cost some coverage:** August demand is more 15"/13"-heavy than the Apr–Jul
machine history, so pinning strands spare capacity on fixed 14"/17" machines. This is inherent to
the strict single-inch rule; Lever A recovers what the 12 flexible machines can, the rest is
structural.

**MAX_CHANGEOVERS_PER_DAY = 17** this cycle (was 12; still ≤ the plant hard limit of 18). Under
the lock the 10/12/14/16 sweep had re-confirmed 12 (July-only preferred 16 for +2,677); the cap
was raised to 17 alongside the press-HOLD lever (§10) — with churn removed, the freed CO budget is
spent on genuine demand-done moves. Carcass lead kept at 2.

### Adopted-approach KPIs (hist-lock + Lever A + hybrid CO items 1+2, `MAX_CO=12`, deterministic, feasibility-clean, cap 0-over, local==cloud byte-parity)

| Month | Demand file | Demand | Days | GT built | GT cured | Coverage | Curing COs | Starvation | Expired GT | Expired carcass |
|-------|-------------|--------|------|----------|----------|----------|-----------|-----------|-----------|----------------|
| June  | `correct_june_plan.xlsx`     | 704,194 | 30 | 645,568 | **650,381** | **92.36%** | 256 | 2,654 | 666   | 10,008 |
| July  | `july_correct_plan.xlsx`     | 727,779 | 31 | 679,223 | **685,342** | **94.17%** | 306 | 2,926 | 1,279 | 9,914  |
| Aug   | `august_demand_tomerji.xlsx` | 689,563 | 31 | 644,523 | **648,687** | **94.07%** | 284 | 3,234 | 1,420 | 10,264 |
| **Total** | | 2,121,536 | | | **1,984,410** | — | — | — | — | — |

> These SUPERSEDE the older 636,201 / 673,221 / 648,118 numbers (new hybrid-CO stack + new demand
> files). They use the NEW inputs (`correct_june_plan.xlsx` / `july_correct_plan.xlsx` /
> `august_demand_tomerji.xlsx`, month-keyed `Daily_Running_Moulds`, single `gt_inventory_manual`)
> — NOT comparable to older CLAUDE.md baselines.
>
> ⚠️ **These rows are an AS-OF-DATE snapshot measured at `MAX_CO=12` and BEFORE the press-HOLD lever
> (§10). The committed config has since moved to `MAX_CHANGEOVERS_PER_DAY=17` + `_PRESS_STABLE=ON`,
> and the Day-0 snapshot drifts daily (see DB-drift caveat at top).** Treat the absolute values as
> historical, not a target; re-run on the day you need current numbers.

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
2. **Curing press changeover cap** is configurable via `MAX_CHANGEOVERS_PER_DAY` in `bc_config.py` (currently **17/day** this cycle; plant hard limit is **18/day**). Building machine changeovers are capped per-machine-per-shift by `MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT` (=2), **raised to 4 for inch-flex machines** via `_INCH_FLEX_EXTRA_COS`.
3. **Stage-2 cannot run without Stage-1 carcass** (same shift or S-1 preferred).
   Unistage machines have no Stage-1 dependency.
4. **No waste GT.** Building output ≤ curing consumption. In B2C, this is
   architecture: curing is derived from building, not the other way around.
5. **Mould availability is physical (LIVE).** A curing press can only run / CO to an SKU
   if it holds **2 eligible moulds** (`Master_Mapping_Mould_SKU`); a mould serves one press
   at a time (contention). Every plan must pass the exact bipartite mould-feasibility audit
   (`feasibility_test.py` rule R17). See "Mould→SKU availability constraint".
6. **Building inch rules (LIVE — historical inch-lock).** Each machine is bound to its
   **allowed-inch SET from the 4-month plant report** (27 FIXED to one inch, 12 FLEXIBLE to
   their ranked historical inches); the **anchor ±2 band is DISCONTINUED**. Max **4 distinct
   SKUs per machine per day**. See "Historical inch-lock" (near top). The old ±2 band + 5-day
   dwell + tier/soft-lock/flex model is the `INCH_HIST_LOCK=0` fallback (documented below).

---

## Rules & features added this cycle (all LIVE unless noted) — READ THIS

Everything below is in `b2c_pipeline.py`, toggle-gated. Several toggles are **hardcoded
`True`** (the user pinned them ON): `_MOULD_GATE_ENABLED`, `_MOULD_OPT_ENABLED`,
`_CO_SCORER_ENABLED`, `_MOULD_LIFE_FROM_DB`, `_MOULD_CLEAN_ENABLED`, `_INCH_RULES_ENABLED`,
`_BLD_SKU_CAP_ENABLED`, **`_IDLE_UNMET_ENABLED` + `_IDLE_UNMET_KEEP_GATE`** (the idle→unmet
lever, §5, adopted 2026-07-24). The `+3/−3 escape` (`_INCH_PLUS3_ENABLED`) and the **global
mould optimizer** (`_MOULD_GLOBAL_OPT_ENABLED`, §6 — REJECTED) are env-gated **default OFF**.
OFF paths reproduce the prior baseline bit-for-bit.

### 1. Mould→SKU availability constraint (curing) — LIVE
- **Gate (Phase 1):** a press runs / CO's to an SKU only if it has **2 eligible moulds**
  free. Mapping `Master_Mapping_Mould_SKU` (`Mould`/`Matl.Code`/`Active Flag`=1); loaded by
  `curing_consumption.load_mould_eligibility()`. 1,284 moulds, one copy each, one press at
  a time (contention). Movement rides the existing 480-min press CO. Env `MOULD_GATE`.
- **Retarget (Phase 2, `MOULD_OPT`):** a planned CO blocked for want of moulds redirects to
  the neediest allowable SKU with 2 free moulds instead of idling.
- **Unified CO scorer (Phase 3, `CO_SCORER`, ADDITIVE):** one utility ranks {planned /
  pull-forward / dynamic / retarget / idle} per press, gated by moulds + a quantitative
  building-feed estimate. `_co_utility` / `_solve_day_cos`. FULL re-opt sub-flag
  (`SCORER_FULL`) measured WORSE → OFF.
- **Day-0 second-mould top-up:** single-mould presses (75214, 9404) get a 2nd compatible
  free mould so both cavities clean together.
- **Mould life v2 (`MOULD_LIFE_DB`, LIVE):** opening `mould_life` per press seeded from the
  real DB remaining = `3000 − "Mould life"(consumed)`, **min over the press's 2 moulds** (so
  both clean together) — already computed by `load_running_moulds().MouldLife_remaining`.
  Countdown / 8h-clean-at-0 / CO-reset-to-3000 unchanged (per-press). Mean opening ≈2,000;
  KPI: May +564, June −99, July −12,113.
- Every plan passes the exact bipartite mould-feasibility audit (`feasibility_test.py` rule R17; the standalone `scratch_mould_audit.py` was removed as R17 supersedes it).

### 2. Building inch rules — LIVE (`INCH_RULES`, hardcoded ON)
Replaces the old permanent one-way / no-revisit rule with a **5-day minimum inch dwell**:
- A machine may change to a **different** inch only if it has dwelled **≥ `MIN_INCH_DWELL_DAYS`
  (5) days**, OR the current size's servable demand is done (deficit-done override → change
  early). It may run one size all month. `_may_leave_inch`, `machine_inch_since`.
- **±2 band kept** (`_inch_ok`, anchor = first inch). **Revisits now allowed** (dwell-gated).
- **Applies to all groups incl Stage-1.** Verified: 0 dwell violations; ~75k candidate
  inch-changes blocked/month.
- **Idle-recovery levers (both LIVE):** **Lever B** (`INCH_GATE_THRESH`, ON) — sub-campaign
  remainder counts as "inch done" so a pinned machine leaves early instead of idling.
  **Lever C** (`INCH_PHASEC_SAME`=**False**, ON) — idle machine's forward-buffer may pre-build
  any IN-BAND inch (current-inch-first). B+C recover +3.6k/+10.4k/+18.9k (May/June/July).
  **Lever D** (buffer 3/2) tested, HURT → OFF. **Note the double-negative:** Lever C is ON
  when `_INCH_RULES_PHASE_C_SAME_INCH = False`.
- **+3/−3 escape (`INCH_PLUS3`, default OFF experiment):** one inch jump of exactly 3 per
  machine per month, at an 8h CO (a full shift — CO-only, production next shift), only for a
  stranded machine with real +3/−3 demand. Net +7,218 (July +6,692). Not adopted yet.

### 3. Max 4 distinct SKUs per building machine per day — LIVE (`BLD_SKU_CAP`, ON)
Per machine, ≤ `MAX_BUILDING_SKUS_PER_DAY` (4) distinct SKUs/day (carryover = #1; both CO
types count). Gated in Phase B / Phase C / legacy candidate loops. ~KPI-neutral (July even
gained — longer, less-churny campaigns). 0 rule violations. **Value is env-overridable via
`BLD_SKU_MAX` (`b2c_pipeline.py`, default = the bc_config 4) for A/B.** Tightening to **3**
was measured a clear regression (May −7,283, June −8,646, July −16,401 = **−32,330 net**;
mould-audit PASS) — less per-machine flexibility to match building to curing draw → more
idle/starvation. **Keep at 4.**

### 4. Output-sheet changes (curing)
- **Changeover Plan** sheet now lists **mould-clean rows** (`CO_Type = "Mould Clean"`, 480
  min) alongside curing COs, with a `Mins` column. Counts reconcile across Changeover Plan /
  Shift Schedule / Machine Utilization / terminal.
- **Mould Tracker** sheet expanded to one row per (press, mould, SKU-run) — Day-0 opening +
  every changeover mount; plus a **Mould Movement** sheet (one row per swap).
- Curing **Machine Utilization** header "Avg util" now = **occupancy** =
  `(ΣUsed+ΣCO+ΣClean)/ΣAvailable`; per-press `Occupancy_Pct = Util + CO_Pct +
  Mould_Clean_Utilization_%`, `Available = planning_days×3×480` (whole month).

### 4b. Expired GT / carcass outputs (building) — LIVE
- **Building "Daily GT & Carcass"** sheet: new per-day `Expired_GT` + `Expired_Carcass` columns
  (right after `Carcass_Produced`), plus the `Holiday` flag column (from §9 fix #4).
- **Building "Shift Schedule"** sheet: new `expired_GT` / `expired_carcass` waste ROWS (`CO_Type`
  values; Machine `"—"`, `CO_Mins` 0, `Qty` = expired units, tinted). **DISPLAY-ONLY** — kept OUT
  of `prod_rows` so every aggregate sheet, KPI, utilization, CO count, and feasibility production
  sum is UNCHANGED; `feasibility_test.py` skips these rows.
- **Terminal:** "Expired GT" + "Expired carcass" KPI lines (`writeoff_total` / Σcarcass_waste).
  Result dict keys: `gt_writeoff`, `carcass_writeoff`.
- **Semantics (state clearly):** expired GT/carcass are WASTE (aged out). **EXCLUDED** from GT
  cured, coverage, and usable-built (Demand-Fulfillment `Planned_Units` = gross built − expired
  GT); **INCLUDED** only in the gross terminal "Total GT built" (it was physically produced, then
  aged out). Expired GT = built-then-aged + any Day-0 opening stock that expired. The
  Demand-Fulfillment "expired GT/carcass" per-SKU column is **DEMAND-SCOPED** (can under-sum vs the
  authoritative terminal total by the expiry on non-demand SKUs).

### 5. Idle building machines → highest-unmet-demand targeting ("IUkeep") — ADOPTED (LIVE)
The forward buffer (Phase C, §"Forward-buffer") used to fill idle building capacity toward the
**nearest-to-starve** SKU. This lever re-ranks those Phase-C candidates by **biggest unmet-demand
gap first** — idle machines aim at the largest demand holes instead of the most-starving SKU.
- **Keeps the two safety gates** so there is **no waste GT** (invariant #4): the `draw>0`
  curable-path gate (a press is drawing the SKU now, or CO'ing to it today via the Shift-A
  pre-build injection) **and** the starvation-risk throttle. It only changes the *ranking*.
- Two hardcoded-`True` toggles: `_IDLE_UNMET_ENABLED` (ranking) and `_IDLE_UNMET_KEEP_GATE`
  (keep the throttle). Env `IDLE_UNMET=0` reproduces the prior forward buffer bit-for-bit;
  `IDLE_UNMET_KEEP_GATE=0` **relaxes** the throttle too (pure front-loading) = MEASURED WORSE
  (May −6,180, July −12,698) → left OFF. The shipped variant is "keep the gate, re-rank only."
- **Measured (cap=12, mould-audit PASS, deterministic): May 684,910 (+2,650), June 632,168
  (−1,870), July 694,161 (+12,732) → net +13,512.** Biggest gain on the weak month (July
  87.48% → 89.11%). Also LOWERS writeoff (1,864→1,845) and starvation (1,203→1,063).
- **CO-cap interaction:** WITHOUT this lever, cap=10 was best (+9.5k). WITH it, July peaks at
  **cap=12** (694,161, beating 10/11/13/14/16) — the optimum flips because IUkeep feeds more
  presses and needs the CO budget to cure that GT. **Cap kept at 12.**

### 6. Global mould optimiser — REJECTED (env `MOULD_GLOBAL_OPT`, default OFF, code retained)
Experiment to reallocate scarce 15″/13″ moulds globally each day (`_global_mould_boost`): rank
the most-under-served scarce SKUs, then either DIRECT-ADD a sacrificeable press or LIBERATE a
stuck mould. Two modes (`MOULD_OPT_MODE`): `ro_only` (only sacrifice demand-done presses) /
`full_evict` (also evict running presses). **Both rejected:**
- `ro_only`: +1,596 **May only**, fires **0× in July** (nothing to reallocate) — marginal.
- `full_evict`: May −11,519 / June −6,629 / July −19,942 (**−38,090 net**). Every mould move is
  a press CO = 480 min (the 8h clean) + mould-life reset to 3000 (charged, `b2c_pipeline.py`
  end-of-day CO loop), so its ~150 July moves burned ~150 clean-shifts.
- **Finding:** July's 15″/13″ gap is **true mould scarcity, not misallocation** — every mould is
  already on an in-demand SKU. The existing daily scarce-first ordering + retarget + CO scorer
  already allocate near the ceiling. More moulds/presses (a capacity decision) is the only lever
  left there, not smarter allocation. Toggle left in the code, OFF, for the record.

### 7. Delivery-date / priority-flag committed-delivery SKUs — LIVE (`DELIVERY_PRIORITY`, default ON, INERT w/o data)

Client feature. Two OPTIONAL demand columns — **`Priority Flag`** (`0`/`1`/`Yes`) and
**`Delivery Date`** (`DD/MM/YY`) — make a SKU **delivery-committed**: it must be fully **cured** by
its date (or by END OF MONTH if flagged with no date). **A valid date forces commitment even when
the flag reads No/0/blank.** Client rule: **meeting the date > overall KPI** (KPI drop accepted).
- **Parse** (`b2c_pipeline._build_priority_deadline_map`): normalized headers (July `Delivery Date`
  vs Aug `Delivery date`), string flag; consolidated per SKU (earliest date = EDF); → `{sku:
  deadline_day}`. Absent/empty columns (June, cloud `jkt_demand`) → **inert, bit-for-bit baseline**.
- **One shared self-pacing signal drives BOTH stages** (curing is derived from building, so a
  committed SKU needs presses AND its GT built in time). **Phase-0** (`curing_consumption_dynamic`):
  committed targets fire FIRST **EARLIEST-DEADLINE-FIRST** (`_cokey` prepend), forced Class-A vs
  their OWN deadline, presses reserved pre-deadline; acquisition **capped at the SKU's mould-pair
  count** (`DP_MOULDCAP`, essential — extra presses can't get moulds) with a pacing margin
  (`DP_PACE_MARGIN=99` = fill to the cap = **adopted "full mould-cap" = max delivery**; `0` = JIT =
  half the collateral but under-delivers). **Building** (`_assign_building_shift._bld_prio`): a
  behind committed SKU is built first (EDF) in `_key`/Phase-A seed/Phase-C, and bypasses the
  Phase-C starvation-risk gate — bounded by the 3-day shelf + GT cap + demand cap (no overbuild).
- **All edits ORDERING-ONLY**: demand cap, historical inch-lock, mould feasibility, GT shelf/cap
  untouched; **no invented machine↔SKU pairs (DB-allowable + DB moulds only)**.
- **Feasibility pre-check + report**: a "PRIORITY FULFILLMENT" terminal table + `res["priority_report"]`
  gives per-SKU demand / deadline / cured-by-deadline / shortfall / **earliest-feasible date**
  (best-effort + relax-report for the physically un-meetable ones).
- **Sub-toggles** (bisected; all default the adopted config): `DP_ACQUIRE` (Phase-0 forcing),
  `DP_RESERVE` (press hold — measured a NO-OP, kept as a safety guard), `DP_MOULDCAP`,
  `DP_PACE_MARGIN`, `DP_BLD` (building boost). `DELIVERY_PRIORITY=0` forces the whole feature off.

**KPIs (deterministic 2-seed, mould-audit PASS, demand-cap 0-over):** June ON==OFF=634,086 (inert).
July 672,696→**671,324 (−1,372)** — HTORE (d20) 0→1,360 **INFEASIBLE (1 allowable press)**, HURL4
~unchanged. Aug 649,334→**638,594 (−10,740)** — QRAT0 (d19) 915→**1,540 met**, VRHE1 (d15)
3,884→5,023 (10 shared moulds contended, can't hit 9,900), HRHB0 (d11) 616→1,364 **INFEASIBLE (1
mould-pair)**, HRHP0 (d27) 1,000→896 (−104 EDF contention, accepted). **Collateral ≈4 units lost
per committed unit pulled earlier = shared-mould contention (a hard capacity limit, not
misallocation).** Phase-0-alone (no building coupling) HURTS.

**CLOUD wiring — LIVE (params-only, gated by `impPriorityFlag`).** On cloud the feature is gated by
**`jkt_plan_params.impPriorityFlag`**: `=1` → `connection.read_db` reads `jkt_demand.priorityFlag`
(VARCHAR) + `deliveryDate` (DATE) and stages them as `Priority Flag`/`Delivery Date` so the engine
activates identically to a local Excel run; `=0` → they are NOT read (plain baseline even if the
columns are populated). **`read_db` is PARAMS-ONLY** — impPriorityFlag / mouldAvailability /
noOfChangeOver / efficiency / dates all come from `jkt_plan_params`; the backend does NOT read the
preset table (the FRONTEND copies the selected preset's values into params on plan create/edit; no
backend copy-back). Two BTP presets exist: **`BTP Preset Main`** (impPriorityFlag=1) / **`BTP Preset`**
(impPriorityFlag=0); CTP presets untouched. Cloud parity verified: with `impPriorityFlag=1`, cloud ==
local (July 671,324 / Aug 638,594); with `=0`, cloud == baseline (672,696 / 649,334) — byte-identical.

### 8. Hybrid curing-CO items 1+2 — ADOPTED (LIVE, hardcoded ON, both paths)

Two coupled curing-CO levers, both hardcoded `True` at module level (so the cloud path inherits
them automatically — byte-parity verified). Combined they are **+29,836 cured over 3 months**,
feasibility-clean. `REACTIVE_ONLY=0` (hybrid) is the default; pure-reactive (`REACTIVE_ONLY=1`)
regressed **~−128k**.

- **Item 1 — `_PERSKU_FEED_V2 = True`** (`curing_consumption_dynamic.py`, hardcoded ON):
  building-aware deficit-first per-SKU feed feasibility. Replaces the optimistic un-apportioned
  `buildable_rate` — contending same-inch SKUs claim their required draw **most-constrained-first**
  (preferring machines OUTSIDE the target's set), so the target's feasible draw = the residual on
  its own machines. The 5b / surplus CO guards then block only **GENUINELY infeasible** planned COs.
- **Item 2 — `_HYBRID_CO_DEFER = True`** (`b2c_pipeline.py`, hardcoded ON): DEFER a planned curing
  CO to the next working day instead of preempting in Shift A, when the press's OLD SKU still has
  FULFILLABLE demand (live `_supply_ok`) that won't finish today AND the press is NOT surplus. The
  single biggest lever (**July +14,137 alone**). Mirror-image of pull-forward.
- **Items 3 & 4 measured non-additive / no-op → left OFF:** Item 3 (`HYBRID_CO_CANCEL`, stale-CO
  wipe) and Item 4 (adaptive starvation-CO). Both toggles are MODULE-LEVEL.

### 9. Plant-holiday feature — LIVE (config `PLANT_HOLIDAYS`, default False = INERT)

`bc_config.PLANT_HOLIDAYS` = a list of `"YYYY-MM-DD"` dates or `False` (default `False` = INERT;
no-holiday runs are **bit-for-bit identical** to before). **Cloud** reads holidays from
`jkt_holiday_calendar` → `connection.read_db` → `run_cfg["holidays"]` → `main._apply_run_cfg` →
`PLANT_HOLIDAYS`. A holiday = a **fully idle day** (0 building, 0 curing); GT inventory carries
across it; **aging stays CALENDAR-based** (GT 3-day / carcass 1-day age across a holiday);
utilization denominators drop holiday shifts; in-flight COs/cleans complete.

Six fixes (all toggle-gated, defaults as noted; all INERT without holidays):
- **#1 `_HOLIDAY_CO_DEFER`** (env `HOLIDAY_CO_DEFER`, default ON): NO new curing CO fires on a
  holiday — every CO the plan placed on a holiday is deferred to the next WORKING day with CO
  budget (month-end overflow dropped). Makes planned / dynamic / scorer match the already-guarded
  reactive path. Verified: 0 COs land on holidays.
- **#2 `_HOLIDAY_NO_PERISH`** (env `HOLIDAY_NO_PERISH_PREBUILD`, default ON): don't pre-build
  perishable stock that ages out over a holiday — caps the carcass PASS-2 lead AND the GT
  forward-buffer window to the WORKING shifts reachable before the holiday. Only ever SHRINKS a
  target (no overbuild). Cuts pre-holiday carcass writeoff ~0.9–1.4k, cured-neutral.
- **#3 `_HOLIDAY_BRIDGE`** (env `HOLIDAY_BRIDGE_BUILD`, default OFF): pre-build extra GT through a
  holiday. MEASURED NO-OP (the existing 9-shift forward-buffer already bridges 1–2 day holidays;
  ≥3-day holidays are shelf-blocked). Kept OFF + documented, like FIXED_ESCAPE / global-mould-opt.
- **#4 reporting:** building "Daily GT & Carcass" sheet gains a `Holiday` flag column + a row for
  every holiday date (idle day, carried GT, and any GT/carcass that aged out DURING the holiday
  are now visible; previously the holiday date was missing entirely).
- **#5 robustness:** unified the 3 holiday-index derivations onto one `plan_start` (fixed a
  def-time-default gotcha in `curing_consumption_dynamic._holiday_day_index_set` + aligns
  `bc_config.PLAN_START` / the curing module `PLAN_START` to the run's plan_start at pipeline entry,
  with a divergence warning) — the same "imported BY VALUE" hazard documented for `RUNNING_MOULDS_MONTH`.
- **#6 wiring:** Day-1 setup COs (idle-press cold-start + Runner-Out Day-1 CO) now gate on
  `_first_working_day`, not `day==1`, so no setup CO fires if day 1 is itself a holiday.

**Honest KPI (July, `july_correct_plan.xlsx`, no-holiday baseline 685,342 cured):** holiday on the
15th → **672,724**; 15th+16th → **651,601**; 15th+27th → **649,486**. A single holiday is a net
win vs the pre-fix behaviour (**+904**, less waste); MULTI-holiday cured DROPS — the honest cost of
#1 (no "free" changeovers on idle days, the realism model chosen) — while waste falls in every case.
Feasibility clean (R8B/R8C demand-cap PASS, 0 overbuild); no-holiday bit-for-bit; cloud parity
byte-identical.

### 10. Curing press HOLD — `_PRESS_STABLE` (ADOPTED, default ON, `curing_consumption_dynamic.py`)

A curing press **stays on its SKU until that SKU's demand is met** — no mid-life voluntary "surplus
release". This removes the day-to-day curing press-count **churn** (the release→re-acquire ping-pong,
e.g. `1225170015010LSTL0` swinging 2→10→2→6→2). A press frees **only** via demand-done + Runner-Out;
the pairing loop's n−1 protection guards the last covering press. Ramp-up unchanged. **Supersedes
the old `_SURPLUS_RELEASE_ENABLED` mid-life release** — with HOLD ON, surplus release is suppressed.
`PRESS_STABLE=0` reverts bit-for-bit. Cloud inherits (module-level env-default ON).

- **Measured at adoption** (ONE DB snapshot, cap=12 — see DB-drift caveat): curing COs **−126**,
  starvation **−916**, press-churn **−57%**, cured **−2,118 net** (June +5,850 / July −2,268 / Aug
  −5,700 — the price of stability on press-tight months).
- **REJECTED sub-experiments (default OFF, code kept):** `PRESS_RELEASE_DAYS` (near-done early
  release — regressed all 3 months); `PRESS_VALVE` + `PRESS_VALVE_MARGIN` (narrow starvation valve —
  net worse than pure hold; July −3,530 vs hold); `PRESS_RATCHET` (monotone press-count cap —
  measured a no-op / redundant with HOLD).

### 11. Mid-month plan start + production deduction — `_MIDMONTH_DEDUCT` (ADOPTED, default ON)

`run_rolling_pipeline_2pass` in `b2c_pipeline.py`. A plan may start on **ANY date**. If start day > 1:
- **Run 1** (full month, original demand) simulates production for days 1..(start−1). The plant's
  "actual" production = planned **CURED × a deterministic per-SKU factor in [0.90, 1.05]**
  (**hashlib-derived, NOT `random()`/`hash()`** — reproducible across processes); this is deducted
  from demand (**floored at 0**).
- **Run 2** replans start→month-end on the reduced demand, **SEEDED from the day-start
  (start-date 07:00) state**: GT inventory as **dated lots with ages re-based**, carcass bank,
  press→SKU + counts + mould life/mounts (injected at the Run-2 day-1 loop top) **AND the CO planner /
  Phase 0** — via a new `initial_press_state` arg to `run_dynamic_consumption`, so plan and execution
  agree.
- **1st-of-month start → single run, bit-for-bit unchanged.** In-memory only (no DB writes); v1 uses
  the SAME 1st-of-month DB opening snapshot for both runs.
- **Parity:** deterministic; LOCAL (`local_main.py` → `run_rolling_pipeline_2pass`) and CLOUD
  (`main.run_plan` → `run_rolling_pipeline_2pass`, same signature, **no API change**) are
  byte-identical (full-month + mid-month, all 3 months verified). `MIDMONTH_DEDUCT=0` disables.
- **To run mid-month LOCALLY:** set `bc_config.PLAN_START` to the start date and `PLANNING_DAYS` =
  remaining days (month-end − start + 1).
- **TWO bug fixes shipped with it:** (a) `curing_allowable` is **resynced from the carried day-K
  press state right after the injection** — else carried SKUs are falsely flagged
  `Skip_Reason="missing curing allowable machine"` / `Eligible_Machines=0` in the
  Demand-Fulfillment sheet even though they cure fully; (b) the Stage-1 carcass renderer's
  Stage-2-consume day is made **PLAN-RELATIVE via a `_planday` helper** — a day-of-month vs loop-day
  index mismatch made Stage-1 render 0 carcass for ANY mid-month start (a 1st-of-month start is
  bit-for-bit). A sibling site in the default-OFF `_CV2_A4` path (`_fifo_reconcile_greedy`) carries
  the same day-origin pattern — flagged in-code, not triggered.

### 12. Retired machine 6801 excluded from the Stage-1 carcass renderer — LIVE

`b2c_pipeline.py`, `s1_sku_to_machines` build. **6801 is plant-retired** but was still in the
pipeline's Stage-1 machine set and occasionally received carcass, failing feasibility rule **R14**
(only the 38 live building machines). Now excluded → R14 passes; full-month cured shifts slightly
(carcass redistributes to the 14 live Stage-1 machines).

### 13. MouldInUse sheet = OCCUPIED moulds (representation fix) — DISPLAY-ONLY

`_mould_in_use_rows` in `b2c_pipeline.py`. The **"Mould in USE"** column now = **2 × committed
presses** = moulds mounted on presses **COMMITTED to** (holding moulds for) the SKU, counted on
**EVERY held day incl. dry / GT-starved / idle days** (forward-filled), dropping **ONLY when a press
actually CO's AWAY** — not when a press merely runs dry with 0 GT. Always **EVEN** (each press = 2
cavities/moulds) and **≤ Total Eligible**. Also counts mould-clean shifts (for the SKU) and CO shifts
(for the NEW target SKU). Column names unchanged (**"Mould in USE"** / **"Total Eligible Moulds"**) so
the frontend doesn't break. Fixes the misread where a starved-but-committed press looked like it had
been CO'd away (4→0). **No KPI change.**

### 14. Starvation-count semantics — CONFIRMED (unchanged, the client's intended metric)

Counted **PER (press × shift)**: a press-shift is a starvation event iff **status RUNNING AND
GT_Inventory==0 AND Qty==0 AND `_demand_left`>0** (the press's current-SKU remaining demand), at
`b2c_pipeline.py` ~the final-KPI block. Explicitly **EXCLUDES** CO shifts, mould-clean shifts, and
demand-done idle presses.
- **RCA finding:** of the day-to-day press-count DIPS, **~72% are building-supply GT-starvation** (a
  press stays committed but building fed ~0 GT that day) and **~28% is real month-end press-churn**
  (presses CO away and back). The tail churn's root = the month-end demand-done reassignment cascade
  + small `horizon_left` (the Class-A boundary flips for every SKU at once and
  `presses_needed = ceil(rem/(rate·horizon_left))` inflates as horizon→1).
- A **"tail-churn damper"** (floor `horizon_left`, block month-end re-acquisition) is **PROPOSED,
  NOT yet built.**

### 15. Feasibility auditor mid-month-aware — `feasibility_test.py --midmonth-opening <file>`

The 2pass wrapper writes a `<building_output>_midmonth_opening.json` (day-start aged GT lots +
carcass totals) next to the output; passing it makes the auditor seed **R5 / R9C / R9G** from the
plan's start-date inventory instead of the 1st-of-month DB, so a mid-month audit doesn't false-fail
those balance rules. **Finding:** R5/R9C/R9G mostly reflect the plan's REAL GT/carcass writeoff (they
fail in full-month too), not an opening artifact — the seeding removed only a small part.

### 16. Local↔cloud parity fixes — carcass cap + demand int-normalize (ADOPTED 2026-08-25)

Two parity bugs were found + fixed so `local_main.py` and `main.run_plan` (cloud) produce **byte-identical**
KPIs (cured / built / coverage / curing-COs / starvation / writeoff) for every month, given matching inputs.
Verified June / July / Aug / September (full-month AND with a holiday). **Method:** stage a month's local
demand Excel into a temp `jkt_demand` + `jkt_plan_params` row, run `main.run_plan` with `connection.write_db`
monkeypatched to a no-op (full cloud path minus the DB write), diff vs the local `run_rolling_pipeline_2pass`.

- **Bug 1 — carcass cap drift (FIXED).** `main.CLOUD_CONFIG["MAX_ENDOFDAY_CARCASS_INVENTORY"]` had drifted
  to **1200** while `bc_config` is **500** (its own comment said "MUST equal bc_config"). The 500-vs-1200
  overnight carcass buffer changed the plan (e.g. June +2,660 cured on cloud). **Set back to 500.** Cloud
  now matches local. This was the SOLE KPI-affecting `CLOUD_CONFIG`-vs-`bc_config` mismatch — the other two
  diffs (`DELIVERY_PRIORITY_ENABLED` True/False, `DELIVERY_PRIORITY_UNDATED_TO_MONTHEND` True/False) are
  INERT when `impPriorityFlag=0` and are the INTENDED master-gate for the client delivery feature (leave True).
- **Bug 2 — `DEMAND_INT_NORMALIZE` (ADOPTED, env `DEMAND_INT_NORMALIZE`, default ON; `=0` reverts bit-for-bit).**
  Some xlsx demand cells carry **float dust** (e.g. HRHE0 = `13750.000000000002`, a 1-ULP artifact). LOCAL
  reads the Excel float, so `demand_remaining` never drains to EXACTLY 0 and the `_demand_done` (`<= 0`)
  reactive-CO test at `b2c_pipeline.py:9556` stays False → a reactive CO that CLOUD fires (it reads
  `jkt_demand.requirement`, an **int** → drains to exact 0) never fires locally. Only surfaced on a specific
  knife-edge (September + a holiday shifting one SKU's last unit onto a press's `_demand_done` boundary shift →
  a 586-cured tail divergence). **Fix:** `round()` demand to integer at load — in `b2c_pipeline.py` (~7015,
  `demand_dict`→`demand_remaining`) and `connection.py` `load_demand` (~311). Tyre demand is physically integer,
  so this is correct; **cloud is already int → `round()` is a no-op → cloud byte-unchanged every month; local
  converges to cloud.** Also strips the same dust from the curing-side `Updated_Demand`.

### 17. Snapshot selection — start-of-month + fallback (ADOPTED, both paths)

The Day-0 opening snapshot (running moulds + opening GT + opening carcass) is resolved by
`connection._resolve_snapshot(engine)` (called lazily inside every snapshot ETL loader, and explicitly by
`main.run_plan`), rebinding module-global `connection.PLAN_DATE`. Two rules, **identical on local and cloud**:
- **Always the 1st of the plan's month** (`{PLAN_MONTH}-01`), regardless of the plan-start DAY. The plan
  start/end dates stay as entered; only the *snapshot date* is pinned to the month's 1st. So any date in
  September (09-01 / 09-15 / 09-21) seeds from `2026-09-01`.
- **Fallback to `SNAPSHOT_FALLBACK_MONTH` (default `2026-07`) if the plan month's snapshot is missing**
  (0 rows in `Daily_Running_Moulds` for `{month}-01`). So a month with no loaded snapshot runs off the
  2026-07 snapshot instead of failing. Log line: `[snapshot] plan_month … → snapshot <date> (N rows)` or
  `… has no running-moulds → FALLBACK to 2026-07 snapshot`.
- **Cloud runs purely from `planStartDate`/`planEndDate`** — NO SAP-actual-production deduction and NO
  current-date clamp: `main.py` pins env `MIDMONTH_DEDUCT=0` / `ACTUAL_PROD=0` / `TODAY_START=0` before the
  engine imports (all inert for a 1st-of-month start anyway; verified parity-neutral).

### 18. September findings (RCA — behaviour, not bugs)

- **Holiday cost scales inversely with coverage headroom.** A plant holiday is a fully idle day (§9). On a
  **slack** month (July, ~95% coverage) the lost day is largely recovered on other days → net cost ≈ **0.58 day**
  (~13k). On a **saturated** month (September, ~84% coverage — the plant's tightest, ~145k demand unmet) the
  presses are near-max every day → the idle day is **unrecoverable** + the tight month-end drops deferred COs →
  net cost ≈ **1.45 days** (~32k). Not a bug — expected on a saturated line.
- **GT built > cured is legitimate when curing is saturated.** Balance identity `cured = opening_GT + built −
  closing_GT − expired_GT` holds EXACTLY every month (audited, residual ≈ 0). Other months consume opening
  stock → cured leads built by 1–4k; September (esp. + holiday) ends with more uncured GT (higher closing +
  expiry) because building outpaces saturated curing → built leads cured. Expired GT counts in **built** (it
  was produced) but not **cured** (§4b). *(Audit caveat: the building "Daily GT & Carcass" sheet appends a
  trailing spillover row dated the 1st of the NEXT month with `EndDay_GT_Inventory=0`; read closing GT from
  the last IN-MONTH date, not the final sheet row.)*
- **Running-moulds snapshot data-quality matters more than mould-life.** A bad `Daily_Running_Moulds` snapshot
  (missing roster presses / press labels not in `Master_Curing_Allowable_Machines_source` → silently dropped)
  costs disproportionately on a holiday: fewer usable presses can't clear the holiday's GT backlog. Diagnosed
  on an earlier native `2026-09-01` snapshot (163 usable presses vs a good roster's 167 → holiday dip 32k vs
  16k); proven NOT mould-life (disabling `MOULD_LIFE_DB` closed only ~14% of the gap). **Fix is in the DB
  snapshot data, not code:** ensure the month's snapshot carries the full allowable roster.
- **Spare-capacity opportunity (September).** VMI 14/16/17″ machines + idle machines have spare building
  capacity while ~20k of 14/16/17″ demand is under-produced. Reverting the VMI inch realloc (`INCH18_DEFER=0`)
  recovers **+13,523 cured** (84.35%→86.06%, lower waste) — realizable only by relaxing the adopted VMI
  inch-allocation policy (the 18″/structural trade-off). A client PDF report of this lives at
  `data/output/September_Spare_Capacity_Report.pdf`.

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
**v2 is now LIVE (`_MOULD_LIFE_FROM_DB`, env `MOULD_LIFE_DB`, hardcoded ON):** opening
`mould_life` per press is seeded from the **real DB remaining life** = `3000 − "Mould life"
(consumed cycles)`, taken as the **min over the press's 2 moulds** (so both clean together) —
already computed by `load_running_moulds().MouldLife_remaining`. Mean opening ≈ 2,000; 6
presses open at 0 (clean Day-1), 23 below 500; cleans jump ~4 → 52–65/month. KPI: May +564,
June −99, **July −12,113** (July is press-tight so early cleans bind). `MOULD_LIFE_DB=0`
restores v1 (everyone fresh at 3,000). Clean threshold stays the model's flat **3,000**, not
the DB `Target life` (which has 1500/30001 outliers). Also: **mould clean events now appear
as rows in the curing Changeover Plan sheet** (480 min each). Curing Machine-Utilization
columns: `total_cycle`, `Mould_Clean_Mins`, `Mould_Clean_Utilization_%`, `Remaining_Mould_Life`;
per-press `Occupancy_Pct = Util + CO_Pct + Mould_Clean_Utilization_%`; header "Avg util" now =
occupancy `(ΣUsed+ΣCO+ΣClean)/ΣAvailable`.

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
Stage-1  (14 machines: 6802, 6803, 6909, 6911, 7601, 7701, 7801–7804, 8001–8003, 8101 — 6801 retired)
  → Output: Carcass (semi-finished). Feeds Stage-2 only.

Stage-2  (6 machines: 8201, 8301, 8302, 8501, 8502, 7301)
  → Output: GT (requires Stage-1 carcass as input). BOTTLENECK (6 vs 14 Stage-1).

Unistage (18 machines: 6001–6004, 7001–7004, 7101–7106, 7201, 7501–7503)
  → Output: GT. Independent — no Stage-1 dependency.
```

---

> **⚠️ SECTIONS BELOW (Inch-Run Study + Inch Locking Policy three-tier) describe the
> PRE-hist-lock ±2/soft-lock/flex model — now the `INCH_HIST_LOCK=0` FALLBACK.** The LIVE
> building inch model is the **Historical inch-lock** (near the top of this file): per-machine
> allowed-inch sets from `machine_inch_dominant_4months_Apr-Jul.xlsx`, ±2 band OFF. The
> per-machine dominant inch is now the top of each machine's 4-month ranked set (loaded by
> `_load_inch_hist_lock`), not the hardcoded May table below. Kept for record + the fallback path.

## Inch-Run Study — Machine Group Inch Policies (CONFIRMED from May plant data — FALLBACK model)

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

## Inch Locking Policy — Three-tier approach (FALLBACK — `INCH_HIST_LOCK=0` only)

> This three-tier ±2/soft-lock/flex model is **superseded by the Historical inch-lock** (LIVE,
> near top). It runs only when `INCH_HIST_LOCK=0`. Under the live lock: `_HARD` is unused (the
> allowable-machine strip enforces the historical set instead), the ±2 band is off, and
> `_INCH_FLEX_EXTRA_COS` / soft-lock still apply to VMI+BJ but the WHICH-inch decision comes
> from the historical set + Lever A scarcity ranking. Kept below for the fallback path.

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
| `MAX_CHANGEOVERS_PER_DAY` | **17** | Curing CO cap per calendar day (**raised 12→17 this cycle** alongside the press-HOLD lever §10; plant hard limit is 18). Single source of truth in `bc_config.py`; cloud per-run cap comes from `jkt_plan_params.noOfChangeOver`. Historical sweep with forward-buffer live (8→14): all 98.9–99.9%; 14→692,988/99.89%, 12→690,180/99.49% (lowest starvation). Total curing COs scale 174→250. |
| `MAX_ENDOFDAY_GT_INVENTORY` | **8000** | Hard plant storage limit: total GT held overnight (all SKUs, after curing + writeoff) ≤ 8,000 (env `GT_CAP_MAX`, default 8000). Enforced proactively by the forward-buffer (`_ENDOFDAY_GT_CAP_ENABLED`). **`main.CLOUD_CONFIG` pins 8000 too — aligned for local↔cloud byte-parity** (the old 7000/10000 drift is resolved). Audit column `EndDay_GT_Inventory` in building "Daily GT & Carcass" sheet. |
| `MOULD_CLEAN_CYCLES` / `MOULD_CLEAN_MINS` | **3000 / 480** | Mould clean: 3,000 cycles (=6,000 tyres) → 8h (480 min = 1 shift) clean, then reset. Toggle `_MOULD_CLEAN_ENABLED` (default ON). See "Mould setup + mould clean". |
| `RUNNING_MOULDS_TABLE` | **"Daily\_Running\_Moulds"** | Single source of truth for the Day-0 running-moulds ETL table (curing press state). All 4 SQL sites import it. **ALWAYS `Daily_Running_Moulds` (the live snapshot) — every month, local and cloud. Do NOT switch to the historical `testing_` / `june_` variants; they are retired.** Pinned for cloud in `main.CLOUD_CONFIG`. |
| `_CO_SHIFT_SPREAD_ENABLED` | **True** | Spread planned curing COs across A/B/C by when each press finishes its old SKU (hardcoded ON; flip to `False` → old shift-A-only). |
| `_FORWARD_BUFFER_ENABLED` / `_FWD_RISK_SHIFTS` | **ON / 1.0** | Forward-buffer (Phase C) + starvation-risk gate. `b2c_pipeline.py:197/206`. Idle machines pre-build about-to-starve SKUs up to `GT_SHELF_LIFE_SHIFTS = 9` ahead, bounded by `MAX_ENDOFDAY_GT_INVENTORY` (**8k**). Historically the +16k win (674k→690k on the pre-priority-score baseline). |
| `CO_CLASS_B_THRESHOLD` | **0.8** | CO fires if `current_days > H × 0.8`. Lower = more COs scheduled. |
| `GT_BUFFER_SHIFTS_VMI` / `GT_BUFFER_SHIFTS_OTHER` | **2 / 1** | Flat GT pre-build buffer depth per group (was a single `GT_BUFFER_SHIFTS=2` + hardcoded `1` for others). VMI banks 2 shifts, BJ/UNI/STAGE 1. **Lever D** tested a 3/2 bump — it HURT (front-loading, −8.6k May) → kept at 2/1. Env `GT_BUF_VMI` / `GT_BUF_OTHER`. |
| `MIN_INCH_DWELL_DAYS` | **5** | Building diff-size (inch-change) rule: a machine must stay on an inch size ≥5 days before changing to a DIFFERENT inch, UNLESS the current size's servable demand is done (deficit-done override → change early). See "Building inch rules". Toggle `_INCH_RULES_ENABLED` (default ON). |
| `MAX_BUILDING_SKUS_PER_DAY` | **4** | Max distinct SKUs a single building machine may produce per calendar day (overnight carryover counts as #1; both same/diff-size COs count). Per-machine. Toggle `_BLD_SKU_CAP_ENABLED` (env `BLD_SKU_CAP`, default ON). ~KPI-neutral at 4. **Value env-overridable via `BLD_SKU_MAX`**; tightening to **3** = −32,330 net (regression, see §3) → keep 4. |
| `INCH_PLUS3_CO_MINS` / `INCH_PLUS3_MIN_DAYS_LEFT` | **480 / 5** | EXPERIMENT: one-time +3/−3 inch escape per building machine per month at an 8h CO (a full shift). Only for a stranded machine with real +3/−3 demand, ≥5 days left (or in-band demand fully done). Toggle `_INCH_PLUS3_ENABLED` (env `INCH_PLUS3`, **default OFF**). Net +7,218 (July +6,692). |
| `MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT` | **2** | Cap on changeovers a single building machine may perform in one shift (rolling pipeline only). Plant averages 0.57 CO/shift/machine; upper bound of 2 lets one machine serve up to 3 SKU campaigns/shift. Must be `same_size_CO` to hold the 80% utilisation floor — 2× `diff_size_CO` (240 min VMI) would blow past it and is blocked. |
| `MIN_SHIFT_UTILISATION` | **0.80** | Target: each building machine should hit ≥80% production time per shift (384 of 480 min). **Defined in `bc_config.py` but not currently imported/referenced by `b2c_pipeline.py`, `building_b2c.py`, or `building.py`** — grep confirms no other file reads this constant. Treat it as an aspirational target documented in `bc.md` §19, not an enforced guard, until it is wired into the rolling-pipeline code. |
| `CURING_CO_DURATION_SHIFTS` | **1** | Shifts a curing press is idle during CO: Shift A only. Mould-clean removed from model. |
| `CURING_CO_CHANGEOVER_MINS` | **490** | Shift A duration for curing CO (full shift occupied). |
| `PRE_START_SHIFTS` | **2** | **LEGACY LP path only** (`building_b2c.py`). The **rolling pipeline does NOT pre-start** — building and curing both begin Day 1 Shift A 07:00 simultaneously (building runs before curing within each shift, so Day-1 GT is built and cured same shift; opening GT covers the rest). Day-1 starvation is negligible (~11 events). Do not assume pre-build in the rolling path. |
| `GT_SHELF_LIFE_DAYS` | 3 | GT cannot sit >3 days before curing. Must equal `TOPUP_LOOKAHEAD_DAYS_GT`. |
| `CARCASS_SHELF_LIFE_DAYS` | 1 | Stage-1 carcass shelf life: 1 day. |
| `MAX_ENDOFDAY_CARCASS_INVENTORY` | **500** | Hard overnight carcass-buffer cap. **`main.CLOUD_CONFIG` MUST pin the SAME 500** — it had drifted to 1200 and was fixed for local↔cloud byte-parity (§16). |
| `DEMAND_INT_NORMALIZE` (env) | **ON** | Integer-normalize demand at load (`round()`) to strip xlsx float dust (e.g. `13750.000000000002`) so `_demand_done`/demand-cap comparisons are exact and match the DB-int (cloud) path. Sites: `b2c_pipeline.py`~7015 + `connection.py load_demand`~311. Cloud (int) → no-op. `DEMAND_INT_NORMALIZE=0` reverts. See §16. |
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

> **The committed cap is `MAX_ENDOFDAY_GT_INVENTORY = 8000`** (bc_config, env `GT_CAP_MAX`) and
> **`main.CLOUD_CONFIG` pins the SAME 8000** — the earlier 7000-stale / 10000 / 6000 drift is
> RESOLVED; local and cloud are aligned (byte-parity verified June/July/August). The older "+16k"
> decomposition text below was measured at the historical 10k cap and illustrates the *mechanism*
> only — the adopted-approach KPIs are at 8k.

The residual gap was NOT curing-press/15"-tooling limited as previously believed — it was **mostly
building-side starvation**: late-month building machines sit 51–56% utilised (~15k idle machine-min/day)
while presses run (have demand) with zero GT. Three coupled changes close it: **674,422 → 690,180
cured (97.2% → 99.5%)**, starvation 1,508 → 911. All toggle-gated; OFF reproduces 674,422 bit-for-bit.

**Feature 1 — end-of-day GT-inventory cap** (`_ENDOFDAY_GT_CAP_ENABLED`, `b2c_pipeline.py:186`,
env `GT_CAP`; committed value `MAX_ENDOFDAY_GT_INVENTORY = 8000` — the 10k below is the historical
mechanism figure). Total GT held overnight
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

> **Now re-ranked by the IUkeep lever (§5, ADOPTED 2026-07-24).** The starvation-risk gate above is
> KEPT (still only pre-builds about-to-starve, curable-path SKUs — no waste GT), but among those
> candidates idle machines now pick the **biggest unmet-demand gap first** instead of the most-starving
> one. Net +13,512 (July +12,732 → 89.11%), lower writeoff + starvation. `IDLE_UNMET=0` reverts to the
> nearest-to-starve ranking documented above.

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
| **Local↔cloud KPI drift (carcass cap)** | `main.CLOUD_CONFIG` MAX_ENDOFDAY_CARCASS_INVENTORY drifted to 1200 vs `bc_config` 500 | **Fixed (§16):** pinned back to 500; cloud == local byte-parity restored. |
| **Local↔cloud tail drift (demand float dust)** | xlsx demand cells like `13750.000000000002` → `demand_remaining` never hits exact 0, so `_demand_done (<=0)` fires differently than the DB-int cloud path | **Fixed (§16):** `DEMAND_INT_NORMALIZE` (default ON) rounds demand at load; cloud (int) no-op, local converges. |
| **September holiday dip 2× other months** | earlier native `2026-09-01` running-moulds snapshot dropped roster presses (163 usable vs 167) → can't clear holiday GT backlog | **DB data issue, not code (§18):** load a full-roster snapshot for the month (the good August roster gives the normal ~1-day holiday cost). Proven NOT mould-life. |
| **Phase-0 CO budget used the WRONG horizon** (30-day months) | `run_dynamic_consumption` received `planning_days` but never forwarded it to `COScheduler.schedule()`, which fell back to its **import-time default** `planning_days = PLANNING_DAYS` (the `bc_config` constant). `simulate()` read the module global the same way. | **Fixed (commit `2b11cda`)** — both now take/forward `planning_days`. Symptom: June (30d) on the cloud path got `12 × 31 = 372` CO slots instead of `12 × 30 = 360` → 189 COs instead of 163 → a materially different plan. (The −8,508 figure quoted when this was found was measured on `demand_tomerji_june_normalized.xlsx`, later identified as the WRONG June input — the defect and its mechanism are real regardless.) Hidden because `bc_config.PLANNING_DAYS = 31` and May/July are both 31-day months, so the stale value coincidentally matched. **Local runs were never wrong in practice** (the constant is edited to match the month); it only bit when a caller passed a horizon differing from the constant — i.e. the cloud path. |

---

## Relevant source files

| File | Role |
|------|------|
| [bc_config.py](bc_config.py) | **SINGLE SOURCE OF TRUTH for ALL parameters**. Edit only this file. |
| [b2c_pipeline.py](b2c_pipeline.py) | **CORRECT ENTRY POINT** — `python b2c_pipeline.py`. Runs curing_consumption_dynamic → building_b2c → curing_b2c. |
| [building_b2c.py](building_b2c.py) | B2C building scheduler — Phase 1a/1b/2a/2b/3. |
| [curing_b2c.py](curing_b2c.py) | B2C curing simulation (Phase 4) — GT-balance shift-by-shift. Press IDs = `WCNAME_clean`. |
| [curing_consumption_dynamic.py](curing_consumption_dynamic.py) | **Phase 0 (Day-0 snapshot) + Phase 0 Extended (CO schedule) — MERGED HERE.** The standalone `curing_consumption.py` is **deleted** (its Day-0-snapshot code now lives in this module); the old `cbc_env.py` is likewise **deleted** (its contents live in `bc_config.py`); the DB ETL lives in `connection.py`. Class A + demand-done Class B, cap = `MAX_CHANGEOVERS_PER_DAY`. Reads Day-0 state from `bc_config.RUNNING_MOULDS_TABLE`; press-HOLD lever `_PRESS_STABLE` (§10) lives here; takes `initial_press_state` for mid-month Run-2 seeding (§11). |
| [building.py](building.py) | Base building machinery (LP engine + DemandHeuristicAssigner). |
| [approach/bc.md](approach/bc.md) | Full B2C architecture spec (authoritative). |

### Deployment layer (cloud)

| File | Role |
|------|------|
| [local_main.py](local_main.py) | **LOCAL entry point** — Excel in/out, reads `bc_config`. Parity anchor. |
| [main.py](main.py) | **CLOUD orchestrator** — `run_plan(plan_id)`: `read_db` → `_set_plan_month` → `_resolve_snapshot` → inject cfg → engine → `write_db`. Holds `CLOUD_CONFIG` (~36 pinned params incl `CURING_PRESS_COUNT=170`, GT cap 8000, **carcass cap 500** (§16), `DELIVERY_PRIORITY_ENABLED`). `_set_plan_month(plan_start)` sets RUNNING_MOULDS_MONTH/PLAN_MONTH/PLAN_DATE (start-of-month) per run; `connection._resolve_snapshot` then applies the 2026-07 fallback if the month's snapshot is missing (§17). Pins env `MIDMONTH_DEDUCT/ACTUAL_PROD/TODAY_START=0` (no SAP-deduction / no today-clamp; cloud runs purely from planStartDate/planEndDate). |
| [connection.py](connection.py) | DB adapter — `read_db()` reads **`jkt_plan_params` ONLY** (params-only; dates/CO/efficiency/impPriorityFlag/mouldAvailability — NOT the preset table) + `jkt_demand` (incl `priorityFlag`/`deliveryDate` when `impPriorityFlag=1`); `write_db()` → **6 output tables** (incl `jkt_plan_moulds`) + `now_ist()`. |
| [app.py](app.py) | **Flask API** — `POST /app/v1/jkt/planning-scheduling/plan/generate-plan {plan_id}`, `GET /health`. Synchronous. |
| [approach/deployment.md](approach/deployment.md) | Deployment spec — DB contract, config mapping, phases, parity-gate results. |
| [requirements.txt](requirements.txt) | Pinned runtime deps (Flask, SQLAlchemy, PyMySQL, pandas, numpy, scipy, openpyxl). |
| [Dockerfile](Dockerfile) / [.dockerignore](.dockerignore) | linux/amd64 image (gunicorn, port 5001). **`.dockerignore` MUST re-include 3 engine reference files** (`data/input/Cycle_time_Building.xlsx`, `data/analysis_aug/machine_inch_dominant_aug.xlsx`, `machine_inch_dominant_4months_Apr-Jul.xlsx`) — read from the filesystem every run; without them the cloud engine silently falls back and diverges from local. Secrets injected at runtime (`--env-file .env`, never baked). Published: `anmolsaini07/jkt-btp-planning:v2` (+ `:latest`), ~145 MB. |

---

## Deployment — local vs cloud (what drives what)

One engine, two I/O paths. **Only these cross the boundary: demand, per-run params, outputs.**
Masters + running-moulds are read from the DB by the engine's own ETL on both paths. **Cloud
`read_db` is PARAMS-ONLY** — every per-run knob comes from `jkt_plan_params`; the backend does
**not** read `jkt_plan_presets` (the FRONTEND copies the selected preset's values into the params
row on plan create/edit; there is no backend copy-back).

| Value | LOCAL source | CLOUD source (`jkt_plan_params` unless noted) | Editing `bc_config` affects cloud? |
|-------|--------------|--------------|-----------------------------------|
| `PLAN_START` / `PLANNING_DAYS` | `bc_config` | `planStartDate` / `planEndDate` | **No** |
| plan_month (Day-0 snapshot + opening GT) | env / `PLAN_START` | derived from `planStartDate` by `main._set_plan_month` (per run) | **No** |
| `DEMAND_FILE` | `bc_config` | `jkt_demand` (staged to a temp xlsx) | **No** |
| `MAX_CHANGEOVERS_PER_DAY` | `bc_config` | `noOfChangeOver` | **No** |
| `PRESS_EFFICIENCY` | `ConsumptionConfig` | `efficiency` (stored as %, ÷100) | **No** |
| **`DELIVERY_PRIORITY` gate** | Excel `Priority Flag`/`Delivery Date` cols + `DELIVERY_PRIORITY_ENABLED` | **`impPriorityFlag`** (=1 → read `jkt_demand.priorityFlag`/`deliveryDate`; =0 → baseline) | **No** |
| `mouldAvailability` (v2/dormant) | — | `mouldAvailability` | **No** |
| The ~20 tuning knobs (GT cap 8000, `CURING_PRESS_COUNT=170`, mould clean, campaign mins, `RUNNING_MOULDS_TABLE`, `INCH_HIST_LOCK`, …) | `bc_config` | **`main.CLOUD_CONFIG`** (pinned, applied before the engine imports) | **No — pinned** |

To change a **cloud** tuning value edit `main.CLOUD_CONFIG`; to change a cloud per-run value
edit the **`jkt_plan_params` row** (the frontend seeds it from the chosen preset). `bc_config`
drives the local run only. **Two BTP presets** exist for the frontend: `BTP Preset Main`
(impPriorityFlag=1 → delivery feature ON) / `BTP Preset` (impPriorityFlag=0 → OFF); CTP untouched.

**ConsolidatedPriorityScore** (the min-max-of-requirement urgency weight) is still computed in code
(`curing_consumption.load_demand` + `b2c_pipeline`) from `requirement` only — SEPARATE from the
committed-delivery feature. The delivery feature uses `jkt_demand.priorityFlag`/`deliveryDate`
(cloud) or the `Priority Flag`/`Delivery Date` Excel columns (local), gated as above.

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

## Current KPIs (ADOPTED approach 2026-08 — historical inch-lock + Lever A)

The rolling pipeline is **deterministic** (verified, fixed + random `PYTHONHASHSEED`).
Current committed stack: **historical inch-lock (INCH_HIST_LOCK, 27 fixed / 12 flexible, ±2 band
OFF) + Lever A flexible-inch scarcity targeting (FLEX_SCARCE_INCH) + hybrid curing-CO items 1+2
(PERSKU_FEED_V2 + HYBRID_CO_DEFER, +29,836 net) + mould gate + retarget + CO scorer + mould life v2
+ mould clean + 4-SKU/day cap + IUkeep + dynamic buffer (β=2 @ 10k GT cap)**, `MAX_CO=12`, carcass
lead 2. `FIXED_ESCAPE` (Lever B), `+3/−3 escape`, global mould optimiser, hybrid items 3/4, plant
holidays (`PLANT_HOLIDAYS=False`) are OFF/inert. Inputs: per-month demand files, month-keyed
`Daily_Running_Moulds` (`plan_month`), single `gt_inventory_manual`, building start-free.

**LOCAL, `MAX_CO=12`, all feasibility-clean, demand-cap 0-over, deterministic, local==cloud byte-parity:**

| Month | Demand file | Demand | Days | GT built | GT cured | Coverage | Curing COs | Starv | Expired GT | Expired carcass |
|-------|-------------|--------|------|----------|----------|----------|-----------|-------|-----------|----------------|
| June | `correct_june_plan.xlsx`     | 704,194 | 30 | 645,568 | **650,381** | **92.36%** | 256 | 2,654 | 666   | 10,008 |
| July | `july_correct_plan.xlsx`     | 727,779 | 31 | 679,223 | **685,342** | **94.17%** | 306 | 2,926 | 1,279 | 9,914  |
| Aug  | `august_demand_tomerji.xlsx` | 689,563 | 31 | 644,523 | **648,687** | **94.07%** | 284 | 3,234 | 1,420 | 10,264 |
| **Total** | | 2,121,536 | | | **1,984,410** | — | — | — | — | — |

> These SUPERSEDE the older 636,201 / 673,221 / 648,118 numbers (new hybrid-CO stack + new demand
> files `correct_june_plan.xlsx` / `july_correct_plan.xlsx` / `august_demand_tomerji.xlsx`).
> Measured with **per-month opening GT** (`gt_inventory_manual` `plan_month`-filtered: June 7,644 /
> July 8,989 / Aug 7,488) + the **Lever-A determinism fix** (inch-scarcity sort now tiebreaks on the
> inch string → identical across all `PYTHONHASHSEED`). Deterministic, mould-audit PASS, demand-cap
> 0-over. **Lever A adds ~+4k net over hist-lock-only** (measured; starvation ↓/flat); **Lever B
> (FIXED_ESCAPE) REJECTED** (regresses all 3); CO=12 best single value (July-only 16 = +2,677 per-month).
> **NOT comparable to pre-2026-08 numbers** (old June file / ±2-band; the `INCH_HIST_LOCK=0` baseline
> was Aug ~646k). Numbers move with the DB opening-GT snapshot — re-run after any `gt_inventory_manual` edit.

**Context — the rules are RESTRICTIONS that make the plan floor-realistic.** Mould-blind
baseline (physically infeasible) was May 688,873 / June 647,423 / July 704,831; the mould
gate + inch dwell + mould-life are real plant constraints, so the current KPI sits below
that by design (July most, because it is press/mould-constrained on 15"/13"). The
idle-recovery levers (inch B+C, IUkeep) and +3/−3 escape (+7,218, OFF) partly offset.

**July is the weak month (86.21%, ~107k unmet):** gap concentrated in the highest-demand inches
(15" and 13"). Under the hist-lock the diagnosis is **building-limited scarce inches** — the
per-inch check (`GT_Built ≈ GT_Cured`, ~0 closing balance, `Demand − Built ≈ gap`) shows the
presses could cure more but building doesn't produce enough 15"/13" GT because much of the spare
capacity is stranded on FIXED 14"/17" machines the lock can't redirect. Lever A recovers what the
12 flexible machines can (+1,860 on July); the rest is structural (the strict single-inch rule).
A global mould-allocation optimiser and the fixed-machine escape (Lever B) were both built +
measured + REJECTED. The remaining July lever is **more moulds/presses on 15"/13" (capacity)** or
**relaxing the fixed-machine rule** (which the user has chosen to keep strict).

**Per-SKU demand cap verified on all 3 months: 0 SKUs cured above demand.**

> ⚠️ **June demand file is now `correct_june_plan.xlsx` (704,194, 97 SKUs); July is
> `july_correct_plan.xlsx` (727,779, 102 SKUs); Aug `august_demand_tomerji.xlsx` (689,563, 91 SKUs).**
> The old `june_production_data.xlsx` (656,608), `demand_tomerji_june_normalized.xlsx` (742,094, has
> 3 stray NaN-`plan_id` rows), and the old July file are RETIRED — do not use them (KPIs differ accordingly).
> **Always sanity-check the demand total against the expected month total before trusting a run.**
> Per-SKU demand cap verified on all 3 current months: **0 SKUs cured above demand.**

> **May moved 690,319 → 687,028 (99.5% → 99.03%) — this is the priority-score v1 change, not a
> regression.** `demand_may.xlsx` carries a **weighted** `ConsolidatedPriorityScore`
> (market + target-date), which v1 **deliberately discards** in favour of pure min-max of
> `Requirement` (see "Known Calculation Pitfalls"). Measured cost on May: **−3,291 cured (−0.5pp)**.
> June and July are unaffected — their files' scores already equal min-max(requirement).
> If coverage matters more than scoring simplicity, restoring the weighted score (or implementing
> the `jkt_plan_params` weightages) is the lever — it is currently dormant by choice.

**Earlier history:** 690,180 → 690,319 came from (a) the 8k cap (was 10k), (b) mould clean (−~250),
(c) charging dynamic COs (was free), (d) CO shift-spread — all net ~flat, physically honest.

**Live scheduling toggles producing this baseline** (all in `b2c_pipeline.py` unless noted):
- `_FORWARD_BUFFER_ENABLED = True` / `_FWD_RISK_SHIFTS = 1.0` / `_ENDOFDAY_GT_CAP_ENABLED = True` — the +16k win (see "Forward-buffer + 10k GT cap" section). OFF (`GT_CAP=0 FWD_BUF=0`) = 674,422 / 97.2% bit-for-bit.
- `_GLOBAL_ASSIGN_ENABLED = True` / `_GLOBAL_CONSTRAINT_MODE = "below"` / `_CAPTIVE_MAX_ENABLED = True` — global (machine,SKU) pair scoring supersedes the sequential per-machine greedy (this was the prior +6.5k win to 681k; captive machine 7301 handled generally, no hardcoded rule).
- `_ROUND_TRIP_BUFFER_ENABLED = True`, `_BUILDING_RATIO_ENABLED = True`, `_CURING_RATIO_ENABLED = True` (`curing_consumption_dynamic.py`), `_INCH_FLEX_ENABLED = True` (VMI+BJ).
- `_PRESS_STABLE = True` (§10, `curing_consumption_dynamic.py`) — press HOLD; **supersedes the old
  `_SURPLUS_RELEASE_ENABLED` mid-life release** (surplus release is now suppressed under HOLD).
- `_MIDMONTH_DEDUCT = True` (§11, `b2c_pipeline.py`) — 2-pass mid-month start; INERT (single run,
  bit-for-bit) for a 1st-of-month start.

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
