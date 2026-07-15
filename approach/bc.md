# B2C Scheduler — Technical Architecture (current)

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
GT inventory is loaded from `gt_inventory_manual` at plan start (~**6,530 units** total).
Pre-plan-start building output is credited to the curing sim's opening balance by
`curing_b2c.py`. Opening carcass inventory = **0** (Stage-2 waits for Stage-1 output;
Unistage is unaffected). Opening GT acts as a Day-1 buffer; it does not change the
steady-state rate.

### 2.2 Building pre-start (`PRE_START_SHIFTS = 2`)
Building starts **2 shifts before** curing consumption begins.
```
Curing starts   May 1  Shift A (07:00)
Building starts Apr 30 Shift B (15:00)  ← 2 shifts prior (Apr 30 B, Apr 30 C)
```
Pre-start GT is credited to the curing opening balance, preventing zero-inventory
Day-1 starvation for Runner-In SKUs.

### 2.3 Demand file
`data/input/demand_may.xlsx` — **85 SKUs**, total demand **693,748**.
Columns: `SKUCode`, `Requirement` (the per-SKU **demand cap**), `ConsolidatedPriorityScore`
(static input; higher = higher priority; not computed here).

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
- CO takes effect same day: Shift A = CHANGEOVER (idle), Shift B onward = producing new SKU.
- **CO Rescue pass:** NRI SKUs unscheduled in the main loop can be given a press donated
  from an RI press with `n>1` that can still meet demand with `n−1`.

**Output:** `curing_consumption_31day.xlsx` (Day_01…Day_31 + CO_Schedule + Day0_Summary + Summary).

### 3.5 Consumption-table schema (per SKU)
`SKUCode` · `Category` (RI/RO/NRI) · `Running_Press_Count` (0 for NRI) · `Allowable_Moulds_Count`
· `Effective_CT_Min` · `Qty_Per_Press_Per_Shift` (`floor(480/CT)×2`) · `Total_GT_Per_Shift`
· `Demand_Qty` (`Requirement`) · `Priority_Score` (`ConsolidatedPriorityScore`).

---

## 4. Machines, groups & inch policy

### 4.1 Building machine groups (39 machines)
```
Stage-1  (15): 6801,6802,6803,6909,6911,7601,7701,7801–7804,8001–8003,8101
   → Carcass (semi-finished). Feeds Stage-2 only.
Stage-2  (6):  8201,8301,8302,8501,8502,7301
   → GT, requires Stage-1 carcass as input. BOTTLENECK (6 vs 15 Stage-1).
Unistage (18): 6001–6004,7001–7004 (VMI) · 7101–7106,7201 (BJ) · 7501–7503 (UNI_NARROW)
   → GT, independent (no Stage-1 dependency).
```
**Stage-1 → carcass → Stage-2** dependency is a hard constraint; Unistage independent.
**167 active curing presses** (as of May 2026, from `testing_Daily_Running_Moulds`).
Planning horizon: 31 days × 3 shifts (A 07:00 / B 15:00 / C 23:00) × 480 min.

**Stage-1 carcass capacity fact:** Stage-1 ≈ 2,459 carcass/shift (15 machines) vs Stage-2
demand ≈ 2,215 carcass/shift at full output → ~11% headroom, no sustained bottleneck. Stage-1
util is structurally ~47% (15 machines for ~11.5 machine-equivalents of Stage-2 demand) — by
design, not a scheduling bug. It rises only if more SKUs are certified onto Stage-2.

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

### 4.5 Inch-locking — four tiers

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

Each press holds **2 moulds × 2 cavities = 4 tyre slots** per cycle. Mould clean fires after
3,000 cycles = 6,000 tyres. **Mould-clean is NOT modelled as a scheduling event** — absorbed
into the CO window at plant level.

**LH/RH labelling:** each physical press appears twice in `testing_Daily_Running_Moulds`
(suffix `LH`, `RH`). `load_running_moulds()` strips the suffix → single record keyed by the
numeric label (e.g. `"75206"`). All press IDs everywhere use this `WCNAME_clean` key.

**Changeover timing — building starts simultaneously with the curing CO:**
```
Day D Shift A: Curing press → CHANGEOVER (490 min, OCCUPIED full shift)
               Building     → START producing GT for new SKU  ← simultaneous
Day D Shift B: Curing press → PRODUCTION begins (new SKU; no mould-clean idle)
               Building     → CONTINUE
Day D Shift C: Curing press → PRODUCTION continues
```
`bc_config.py`: `CURING_CO_DURATION_SHIFTS = 1`, `CURING_CO_CHANGEOVER_MINS = 490`.
**Implementation:** in **Shift A only**, `shift_cure_demand[new_sku] += _cure_qty_per_shift(new_ct)`
is injected for each CO target SKU. Never inject in Shift B — double injection = 2× demand signal.

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
      CO presses: Shift A = CHANGEOVER (no demand); Shifts B/C RUNNING = full demand
      Pre-build signal (Shift A ONLY): shift_cure_demand[new_sku] += _cure_qty_per_shift

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

    Step 4 — Curing simulation (same shift S)
      for each press:
        RUNNING:    cured = min(capacity, gt_inventory[sku], demand_left); gt_inventory -= cured
        CHANGEOVER: idle (Shift A of CO day only)

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

**Utilisation floor (target, not an override of the demand cap):** `MIN_SHIFT_UTILISATION = 0.77`
is documented but yields to the demand cap and the CO-cost guard. GT machines only — Stage-1 is
structurally ~47% by design (15 machines for ~11.5-equiv demand).

---

## 8. NEW LOGIC — forward buffer + 10k cap + risk gate

Three buffers now cover three distinct needs:
- **Flat buffer** (`GT_BUFFER_SHIFTS`): survive steady curing (VMI = 2, others = 1).
- **Round-trip buffer** (§9.2): survive a machine rotating to a partner SKU and back.
- **Forward buffer** (this section): bank GT for a live SKU's *own* future need using idle
  building time, up to 9 shifts ahead, no rotation partner required.

### 8.1 End-of-day 10k GT-inventory cap (hard plant constraint)
- `bc_config.py`: **`MAX_ENDOFDAY_GT_INVENTORY = 10000`.** Total GT held overnight (summed
  over all SKUs, after curing + stale write-off) ≤ 10,000. Physical plant storage limit.
- Toggle `_ENDOFDAY_GT_CAP_ENABLED` (`b2c_pipeline.py:186`, default ON; env `GT_CAP=0` disables).
- **Enforced proactively**, not by reactive write-off: the forward buffer is bounded so
  overnight carry never exceeds 10k. Audit column `EndDay_GT_Inventory` added to the building
  "Daily GT & Carcass" sheet. Verified: max ~4,900, **0 days over**.

### 8.2 Forward buffer / Phase C (the KPI lever)
Toggles: `_FORWARD_BUFFER_ENABLED` (`b2c_pipeline.py:197`, default ON, env `FWD_BUF`);
`_FWD_RISK_SHIFTS = 1.0` (`:206`, env `FWD_RISK`);
`GT_SHELF_LIFE_SHIFTS = GT_SHELF_LIFE_DAYS × 3 = 9` (`:211`).

Runs as **Phase C** in `_assign_building_shift`, after Phase A (continuation) and Phase B
(global CO pairing). For each machine with idle shift-minutes, while total projected inventory
< 10k, pick a candidate SKU that satisfies **all** of:
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

**10k bound:** `entry_carry_gt + forward_added ≤ 10,000`. Base Phase A/B build is cure-neutral
(consumed same/next shift) so it is excluded; only the forward buffer adds net overnight carry.
Hard demand-cap clamp applies; respects the flex/soft-lock off-inch gate and CO budget.

**Why it works:** it uses idle late-month building capacity (machines 51–56% utilised on days
24–31) to feed presses that were STARVED (running, have demand, no GT). The **risk gate** stops
early-month front-loading and keeps the 10k buffer unclogged so it can always respond to the
next SKU that runs dry.
```
Ungated (risk=0): buffer clogs, mean inventory 7,877 → 681k cured
Gated (risk=1.0): mean inventory 3,664           → 690k cured
```

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
CO fires instantly when RI demand is fulfilled; counts toward `MAX_CHANGEOVERS_PER_DAY`.

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
   from building. Overnight carry ≤ `MAX_ENDOFDAY_GT_INVENTORY = 10,000`.

---

## 12. Config parameters (all in `bc_config.py` — single source of truth)

| Parameter | Value | Controls |
|-----------|-------|----------|
| `SHIFT_DURATION_MIN` / `SHIFTS_PER_DAY` | 480 / 3 | Shift length; A/B/C |
| `MAX_CHANGEOVERS_PER_DAY` | **12** | Curing CO cap/day (was 18; set 12 for surplus-release). Building COs uncapped by day. |
| `CO_CLASS_B_THRESHOLD` | 0.8 | Class A fires if `current_days > H × 0.8` |
| `MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT` | 2 | Building COs/machine/shift (4 for flex machines) |
| `MIN_CAMPAIGN_MINS` | 60 | Shortest production run (was 120 — blocked ≤2-press SKUs) |
| `MIN_CAMPAIGN_UNITS` | 40 | Minimum units per campaign |
| `OVERBUILD_BUFFER_FRAC` | 0.2 | LP headroom (legacy path) |
| `MIN_SHIFT_UTILISATION` | 0.77 | Aspirational GT-machine floor; yields to demand cap |
| `PRE_START_SHIFTS` | 2 | Building pre-starts N shifts before plan start |
| `GT_BUFFER_SHIFTS` | 2 | Flat pre-build buffer (VMI=2, others=1) |
| `GT_SHELF_LIFE_DAYS` | 3 | GT cannot sit > 3 days; = `TOPUP_LOOKAHEAD_DAYS_GT` |
| `GT_SHELF_LIFE_SHIFTS` | 9 | `GT_SHELF_LIFE_DAYS × 3` — forward-buffer horizon |
| `CARCASS_SHELF_LIFE_DAYS` | 1 | Stage-1 carcass shelf life |
| `TOPUP_LOOKAHEAD_DAYS_GT` | 3 | TopUp pre-build days (legacy); must equal shelf life |
| `MAX_ENDOFDAY_GT_INVENTORY` | **10000** | Overnight GT-hold cap (hard plant constraint) |
| `CURING_CO_DURATION_SHIFTS` | 1 | Curing press idle during CO: Shift A only |
| `CURING_CO_CHANGEOVER_MINS` | 490 | Shift A curing CO duration (full shift) |
| `DEFAULT_CYCLE_TIME_MIN` | 17.0 | Effective CT when SKU missing from DB |
| `LOAD_UNLOAD_BUFFER_MIN` / `PRESS_EFFICIENCY` | 2.3 / 0.94 | Effective-CT formula |
| `CAVITIES_PER_MOULD` | 2 | Tyres per curing cycle |
| Stage-2 CO multiplier | 2.0× | `co_time_map` Stage-2 diff × 2 (88→176) to discourage LP overload (legacy) |

**Live scheduling toggles (`b2c_pipeline.py`):** `_GLOBAL_ASSIGN_ENABLED`,
`_GLOBAL_CONSTRAINT_MODE="below"`, `_ROUND_TRIP_BUFFER_ENABLED`, `_BUILDING_RATIO_ENABLED`,
`_INCH_FLEX_ENABLED`, `_ENDOFDAY_GT_CAP_ENABLED`, `_FORWARD_BUFFER_ENABLED`, `_FWD_RISK_SHIFTS=1.0`
— all ON by default. `_CURING_RATIO_ENABLED` in `curing_consumption_dynamic.py`.

---

## 13. Building CT (per-machine, real — not a proxy)

`b2c_pipeline.py` has a real per-machine building-CT dict `_BLD_CT_SEC` (seconds/unit),
corrected against plant norms in commit `f86f70b` (every value changed). Samples:
```
VMI:      7001=51.6s  7002=52.6s  7003=56.0s  7004=53.0s
BJ/UNI:   7101=83.0s  7104=87.0s  7501=90.0s  7502=90.0s
Stage-1:  6801=127s   8101=230s   7601=186s   (slower — carcass)
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

**Structural note superseded — see §16.** The old "~3% residual gap is curing-press /
15"-tooling limited" claim is now largely **incorrect**; the forward buffer proved most of it was
building-side starvation.

---

## 16. Current KPIs (committed default)

Committed default: forward buffer `risk=1.0` ON, 10k cap ON, `MAX_CHANGEOVERS_PER_DAY = 12`.
Fully deterministic (every sort/min/max ends in an explicit tiebreak; bit-for-bit reproducible).
`result_checker`-audited.

| KPI | Value |
|-----|-------|
| Total demand (`demand_may.xlsx`) | **693,748** |
| GT built | **684,028** |
| GT cured (total units) | **690,180** |
| Demand coverage | **99.5%** (690,180 / 693,748) |
| Starvation events | **911** |
| GT written off | **803** |
| End-of-day GT inventory (max) | **~4,900** (10k cap held; 0 days over) |
| Overbuild | ≤ 162 units / 0.02% (rounding only) |

**OFF baseline** (`GT_CAP=0 FWD_BUF=0`) reproduces **674,422 / 97.2%** bit-for-bit.

**CO-cap sweep (forward buffer live):** all caps 8–14 land 98.9–99.9%. **cap=14 best**
(692,988 / 99.89%); **cap=12 lowest starvation** (911). Total curing COs scale 174→250 across the
sweep.

**KPI progression (context).** Before the forward buffer, the deterministic baseline at CO=18 was
~670,744 cured / 96.7% (round-trip + building-ratio + curing-ratio + overbuild fix + inch-flex,
brute-forced machine set). Adding the forward buffer + 10k cap + risk gate at CO=12 lifts this to
**690,180 / 99.5%**. Plant benchmark (`Plant_vs_Scheduler_Report.pdf`): the AI plan (99.5% cured,
~0 overbuild) beats the plant's demand-capped effective fulfilment (95.9%, with ~61k wasted
overbuild). Re-run `python b2c_pipeline.py` for the exact current figure.

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
| `testing_Daily_Running_Moulds` | Which SKU each press runs today; mould life; press state (LH/RH → `WCNAME_clean`) |
| `gt_inventory_manual` | Opening GT inventory per SKU (`sizeCode`, `gtInventory`) |
| `Master_Curing_Design_CycleTime` | Raw cure time per SKU; missing → default 17.0 |
| `Master_Curing_Allowable_Machines_source` | SKU ↔ allowable curing press |
| `Master_Building_Allowable_Machines` | SKU ↔ building machines — single comma-separated `Machines` string (renamed from `_source`, reshaped from per-machine Y/Yes cols; commit `63d193f`); parsed via `_parse()` |
| `Building_Stage1/2_Best_Machines` | 3-month history — loaded but **NOT** used for eligibility (union removed) |
| `Master_Building_ChangeoverTime` | Building CO times (`Same Size(min)`, `Diff Size(min)`) |
| `Master_WC_Master` | Press code normalisation (`wcID`, `WCNAME`) — 30 presses have NaN `wcID` |
| `Master_Mapping_Mould_SKU` | Mould ↔ SKU compatibility for CO target check |
| `TBMStage1/2_ProductionEventData` | Currently running building machines at plan start |

**File inputs:** `data/input/demand_may.xlsx`; `feed_map.json` (press → feeder machines, pre-built).

### 17.2 Output files
| File | Contents |
|------|----------|
| `curing_consumption_31day.xlsx` | 31 day sheets + CO_Schedule + Day0_Summary + Summary |
| `bc_building_schedule_YYYY-MM-DD.xlsx` | Per machine/shift: SKU, qty, CO_Type; Machine Utilization; Daily GT & Carcass (`EndDay_GT_Inventory` audit col); Demand Fulfillment (B2C) |
| `bc_curing_b2c.xlsx` | Per press/shift: SKU, status, qty; Changeover Plan (all curing COs); Machine Utilization (Prod_Mins / CO_Mins) |

---

## 18. Output-metric definitions & edge cases

### 18.1 Metrics
- **Utilisation(machine, shift)** = committed production minutes / 480.
- **Total_Units** = GT (Stage-2 + Unistage, the "Built" figure) + Carcass (Stage-1, internal
  intermediate). Keep the two separate when reporting demand fulfilment.
- **Starvation event** = a RUNNING curing press with demand remaining but zero GT that shift.
  Distinct from IDLE (demand met). Curing output "Remarks" distinguishes `STARVED (no GT)` vs
  `IDLE (demand met)`; the sheet's STARVED count reconciles with the printed KPI.

### 18.2 Edge cases
| Edge case | Handling |
|-----------|----------|
| Building capacity < RI consumption | Produce at max; press GT-limited (throughput gap, not negative inventory) |
| No viable CO target for a Runner-Out press | Press stays idle; logged |
| Daily curing CO budget exhausted | Defer lower-priority CO to next day; re-sort each day |
| RI demand fulfilled mid-cycle | Cap building to 0; freed press enters CO queue (`demand_done_free`) |
| Stage-1 machine unavailable for a Stage-2 assignment | Stage-2 removed that shift; carcass shortfall logged |
| Machine spare minutes < CO cost + one campaign | Machine treated as occupied; no new SKU |
| End-of-day GT would exceed 10k | Forward buffer bounded proactively so carry ≤ 10,000 |

---

## 19. Source files

| File | Role |
|------|------|
| `bc_config.py` | **SINGLE SOURCE OF TRUTH for all parameters.** Edit only this file. |
| `b2c_pipeline.py` | **ENTRY POINT** — `python b2c_pipeline.py`. Rolling pipeline; `_assign_building_shift`. |
| `curing_consumption_dynamic.py` | Phase 0 — 31-day CO schedule (COScheduler/DaySimulator); reused by rolling loop. |
| `curing_consumption.py` | Phase 0 — Day-0 snapshot + eligibility filter. |
| `curing_b2c.py` | Curing GT-balance simulation (legacy path); press IDs = `WCNAME_clean`. |
| `building_b2c.py` / `building.py` | Legacy LP building scheduler (`--legacy` only). |
| `approach/bc.md` | This file — architecture spec. |

---

## 20. Framing-

When answering "should we / what if / what's wrong":
1. **Which invariant does it touch?** (demand cap, CO caps, Stage-1/2 dependency, no-waste / 10k cap)
2. **Which SKU category?** (RI / RO / NRI)
3. **Which machines?** (Stage-1 under-utilised by design; VMI 7001/7003 soft-locked; flex = VMI+BJ)
4. **Config, logic, or data change?**
5. **Trade-off or strict improvement?** Most "never go idle" changes are trade-offs; the forward
   buffer's front-loading is one (throughput up, evenness down).
6. **Building vs curing bottleneck?** Since §16.1, do not assume a residual gap is
   curing-limited — check whether idle building machines are simply not feeding running presses.
