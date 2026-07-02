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
2. **Curing press changeover cap** is configurable via `MAX_CHANGEOVERS_PER_DAY` in `bc_config.py` (currently **10/day**). Building machine changeovers have NO cap.
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

**LH / RH press labelling:** Each physical curing press appears as two rows in
`testing_Daily_Running_Moulds` — one with suffix `LH` (left-hand mould) and one
with `RH` (right-hand mould). They are the same press. `load_running_moulds()`
strips the suffix (`WCNAME_clean = WCNAME.replace(r"(LH|RH)$", "")`) and groups
both rows into a single press record keyed by the numeric press label (e.g.
`"75206"`). CO events and press_state always use this clean numeric key.

### Changeover timing — building MUST start simultaneously with curing CO

> **Key logic (confirmed update):** When a curing press starts a changeover
> (CO) to a new target SKU on Day D Shift A, the building machine(s) for that
> target SKU must ALSO start producing GT in **Shift A of Day D** — not Shift B,
> not Shift C.

Rationale: the curing press is idle for 1 shift (Shift A = CO only; mould clean
removed from scheduler model). Building starts in Shift A so at least 1 shift of
GT is in inventory by the time curing fires in Shift B. This eliminates any
starvation risk on Day-1 of the new SKU.

```
Day D  Shift A:  Curing press  → CHANGEOVER (490 min, OCCUPIED — full shift)
                 Building mach → START producing GT for new SKU   ← simultaneous
Day D  Shift B:  Curing press  → PRODUCTION begins (new SKU)      ← no mould-clean idle
                 Building mach → CONTINUE producing GT
                 GT inventory  → 1 full shift pre-built = immediate feed
Day D  Shift C:  Curing press  → PRODUCTION continues
                 Building mach → CONTINUE producing GT
```

**Mould-clean is NOT modelled as a scheduling event.** The physical mould-clean
(triggered after 3,000 cycles) is absorbed into the CO window at the plant level.
The scheduler treats every shift after CHANGEOVER as productive.
`bc_config.py`: `CURING_CO_DURATION_SHIFTS = 1`, `CURING_CO_CHANGEOVER_MINS = 490`.

This applies to ALL changeover types:
- Runner-Out press switching to a demand SKU (Phase 2a)
- Runner-In press freed after demand fulfilled, switching to NRI SKU (Phase 2b)
- Any NRI SKU assigned a curing press (Phase 1b)

**Implementation (rolling pipeline):** `co_target_skus_today = frozenset(co_press_map.values())`.
In **Shift A only**, `shift_cure_demand[new_sku] += _cure_qty_per_shift(new_ct)` is injected
for every CO target SKU. This creates a positive deficit that `_assign_building_shift` responds
to, starting GT production immediately. Shift B continues naturally (inventory now exists).
**Do not inject in Shift B** — double injection creates 2× demand signal for a single press,
diverting machines away from RI presses that have genuine Shift B curing demand.

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

## Inch Locking Policy — Two-tier approach

### Tier 1: Hard filter (`_MACHINE_HARD_INCH` in `building_b2c.py`, `_HARD` in `b2c_pipeline.py`)

Applied ONLY to BJ and UNI_NARROW groups where genuine physical constraints exist.
**VMIMAXX (6001–6004, 7001–7004) is NOT hard-filtered** — the group officially handles
14"–18" per plant policy, and the allowable table (`Master_Building_Allowable_Machines_source`)
is the authority for individual SKU assignments within that range. Using a hard filter for
VMIMAXX wrongly blocked legitimate assignments (e.g. all 8 VMIMAXX machines allowable for
certain 15" SKUs but the filter was only passing 6002 and 7003).

| Machine | Hard filter | Reason |
|---------|------------|--------|
| 7101 | 15" only | Dominant |
| 7102 | 14"/15" | 14" for 2 BJ-exclusive RI SKUs |
| 7103 | 13" only | Dominant |
| 7104 | 14"/15" | 14" for 2 BJ-exclusive RI SKUs |
| 7105 | 13" only | Dominant |
| 7106 | 13" only | Dominant |
| 7201 | 16" only | Dominant |
| 7501 | 12"/13" | Confirmed allowable for 13" SKUs (extended from 12"-only) |
| 7502 | 13" only | Hard |
| 7503 | 13" only | Hard |

### Tier 2: Dominant-inch preference (`_MACHINE_DOMINANT_INCH` in `b2c_pipeline.py`)

For VMIMAXX (and all other machines), CO candidates are sorted with `inch_penalty`:
- `inch_penalty = 0` if target SKU's inch == machine's dominant inch (preferred)
- `inch_penalty = 1` if different inch (still allowed if in allowable table)

This gives dominant-inch routing without hard-blocking legitimate assignments from the
allowable table. Sort key within each CO bucket: `(−deficit, inch_penalty, revisit_penalty, co_cost)`.

**SKU inch derivation:** if a SKU is missing from the size master, inch is derived from
`sku_code[8:10]` (characters 9–10, 1-indexed). E.g. `"1325216814085SURL0"[8:10] = "14"`.

**DB state (updated):**
- `1325215513073TUHL0` (13"): inserted with machines 7501, 7502, 7503
- `1325216814085SURL0` (14"): already in DB with VMIMAXX machines; was broken only because inch was unknown — now derived from SKU code
- `1325218415084TTMX0` (15"): inserted with all 8 VMIMAXX machines

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
CO fires instantly when Runner-In demand is fulfilled; counts toward the `MAX_CHANGEOVERS_PER_DAY` cap.
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
= 0.2` (already applied) prevents this. `TOPUP_LOOKAHEAD_DAYS_GT = 3` (correct
value) matches `GT_SHELF_LIFE_DAYS = 3`; setting it to 1 was too conservative and
unnecessarily reduced GT output by 7–10k. OVERBUILD_BUFFER_FRAC = 0.2 handles cap
collapse; TopUp = 3 fills idle tails aggressively.

**Case D — NRI SKU deferred past horizon**
The curing CO was deferred (daily CO cap hit) so the building machine was never
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
| **Mode C — NRI CO timing gap** | Building starts Shift A of CO day; curing Shift B → 1-shift buffer only. diff-size CO (180 min) eats Shift A → insufficient buffer | **Implemented:** NRI demand front-loading 70% pre-CO / 30% post-CO forces LP to pre-build buffer |

**Why small building CT doesn't fix Mode A or B:** Building CT ~2–3 min/tyre vs curing ~17 min —
building is fast enough. The problem is the machine is either not assigned (Mode A) or too few
machines assigned (Mode B). Speed only helps once a machine IS assigned.

**True fix for all modes:** Phase 4 — Curing Derivation. Implemented in `curing_b2c.py`.
Curing only runs when GT is available → `Cure_Qty ≤ GT_inventory` by construction → zero
starvation events. The curing output (520k May 2026) is now GT-limited, not press-limited.

---

## Current known issues (as of this design rev)

| Issue | Root cause | Current status |
|-------|-----------|----------------|
| Starvation events (Mode A) | Machine idle — LP doesn't assign some machines | **Fixed:** mould-constrained priority boost in `building_b2c.py` |
| Starvation events (Mode B) | Physical constraint — too few building machines for SKU's curing volume | **Structural:** true fix = Phase 4 (Curing Derivation; implemented in `curing_b2c.py`) |
| Starvation events (Mode C) | NRI CO timing — 2-shift buffer sometimes insufficient | **Fixed:** NRI front-loading 70/30 in `_make_synthetic_curing()` |
| 21 UNMET NRI SKUs | No CO scheduled in main loop (no free compatible press) | **Fixed:** CO Rescue pass (spare-press donation) in `curing_consumption_dynamic.py` |
| 7001/7002 utilisation (was 25–28%) | Cross-inch SKU assignment + history bias | **Fixed:** `_MACHINE_DOMINANT_INCH` preference in rolling pipeline + `_MACHINE_HARD_INCH` in legacy. New util: 7001≈45%, 7002≈46% |
| Stage-1 util <33% | Structural (15 machines for 11.5-equiv demand) | By design; not a bug |
| Demand skew: BJ oversubscribed | BJ demand vs ~184–200k BJ capacity | Structural — needs more BJ presses or VMI certification of BJ SKUs |
| NRI SKUs with zero production | No allowable machine, or CO deferred past horizon | 3 SKUs fixed (DB insert + inch derivation). Remainder logged per SKU. |
| Rolling pipeline KPI < 565k | RC1: `demand_remaining` check locked Shifts B/C → ~85% idle. RC2: double pre-build signal diverted machines from RI presses | **Fixed:** RC1 = `_deficit(cur_sku) <= 0` (this shift's gap, not horizon demand). RC2 = Shift A injection only. |
| 3 SKUs missing allowable building machines | Not in DB / inch unknown → filtered out | **Fixed:** inserted `1325215513073TUHL0` (7501–7503) and `1325218415084TTMX0` (VMIMAXX). Inch fallback = `sku[8:10]`. |
| VMIMAXX machines wrongly blocked for off-dominant-inch SKUs | Hard-inch filter removed 6001/6004/7001/7002/7004 from 15" SKU assignments | **Fixed:** VMIMAXX removed from `_HARD` filter. Allowable table governs; `inch_penalty` in CO sort provides dominant-inch preference. |
| 7501 wrongly blocked for 13" SKUs | Hard filter had `7501:{"12"}` only | **Fixed:** expanded to `{"12","13"}` — confirmed allowable per plant data. |
| Curing press IDs were 30 presses short | `_load_press_state` used `wcID` instead of `WCNAME_clean` | **Fixed:** `curing_b2c.py` uses `WCNAME_clean` (e.g. "75206"). LH/RH stripped in `load_running_moulds`. |
| Curing ~67k below building output | CO over-aggressiveness: RI presses CO'd before demand fulfilled | **Fixed:** guard added in main CO loop of `curing_consumption_dynamic.py` (line ~339): skip CO if `remaining_demand / ((n-1) × rate) > horizon_left`. Rescue pass already had this guard; main loop now consistent. |
| HURL0 zero production | Only machine 7102 eligible after `_HARD` filter (HURL0 is 14"; 7104 missing from DB allowable list). 7102 fully occupied by 15" primary SKU — diff_size_CO (90 min) exceeds 30% CO guard so 7102 can never reach HURL0 in Campaign 2. | **Data fix needed:** add machine 7104 to `Master_Building_Allowable_Machines_source` for SKU `1325218614088HURL0`. 7104 has `_HARD={"14","15"}` — would pass the filter once inserted. Both 7102 and 7104 are "the 2 BJ-exclusive 14" RI SKU machines" per CLAUDE.md; HURL0 is missing 7104 in DB. |
| TVECE partial production (3,945 remaining of 4,097 demand) | Only 7102 + 7104 eligible. BJ structural oversubscription (249k demand / 184k capacity = 136%) leaves little spare time for TVECE (14") after 15" primary SKU campaigns. | Structural — cannot close without more BJ capacity or VMI certification. Currently gets limited Shift B/C spillover. |
| Rolling pipeline starvation events (~4,800) | Pre-existing consequence of BJ oversubscription — building physically cannot supply all BJ presses at full rate. Not caused by any scheduling change. | Pre-existing baseline. Reduced as overall coverage improves. |
| Curing output lower than expected (old: ~587k) | MOULD_CLEAN held Shift B idle per CO event. 80 COs × ~56 tyres/shift = ~4,480 tyres lost per run. | **Fixed:** MOULD_CLEAN removed from scheduler. Shift B of CO day is now RUNNING. New cured total: **616,514** (May 2026). `bc_config.py`: `CURING_CO_DURATION_SHIFTS=1`. `curing_b2c.py` co_trans and press simulation updated. |

---

## Key config parameters (what's tunable)

**All parameters live in `bc_config.py` — single source of truth. Edit only that file.**

| Parameter | Current value | What it controls |
|-----------|--------------|-----------------|
| `MIN_CAMPAIGN_MINS` | **60 min** | Shortest allowed production run. Base value in `building.py` is 45; raised to 60 to allow 1–2 press SKUs (64–128 min/shift demand on fast VMI machines). Was 120 — too conservative, blocked any SKU with ≤2 presses from being served. |
| `MIN_CAMPAIGN_UNITS` | 40 | Minimum units per campaign. |
| `OVERBUILD_BUFFER_FRAC` | 0.2 | LP headroom above net demand per day (prevents cap collapse). |
| `TOPUP_LOOKAHEAD_DAYS_GT` | **3** | How many days ahead TopUp pre-builds GT. Must equal `GT_SHELF_LIFE_DAYS = 3`. Was incorrectly set to 1 (cost 7–10k GT); fixed. |
| `MAX_CHANGEOVERS_PER_DAY` | **10** | Curing CO cap per calendar day. **Single source of truth in `bc_config.py`** — propagates automatically to all pipeline files. Lower values (8) reduce NRI SKU activation rate; higher values (10) activate more NRI SKUs but increase building CO overhead. |
| `GT_BUFFER_SHIFTS` | **2** | Rolling pipeline: how many curing shifts of GT to pre-build as a buffer. 2 = build today's + 1 extra shift so sibling machines (e.g. 6004 + 7001 on 16") both see non-zero deficit and share the demand. VMI uses 2; BJ/UNI/STAGE use 1. |
| `CURING_CO_DURATION_SHIFTS` | **1** | Shifts a curing press is idle during CO: Shift A only (CHANGEOVER). Mould-clean removed from scheduler model — Shift B is productive. |
| `CURING_CO_CHANGEOVER_MINS` | **490** | Shift A duration for curing CO (full shift, press occupied). |
| `CO_CLASS_FILTER` | **Class A only** | COScheduler fires only Class A (critical: `current_days > horizon_left`) COs. Class B (helpful) skipped to avoid CO explosion from premature NRI activation. |
| `PRE_START_SHIFTS` | **2** | Building pre-starts N shifts before plan_start. 2 = Apr 30 Shift B (15:00). Pre-start GT credited to curing simulation's opening balance by `curing_b2c.py`. |
| `BUILD_LEAD_SHIFTS` | **3** (= 1 full day) | Legacy pipeline: building targets curing demand this many shifts ahead. Rolling pipeline: not applicable (simultaneous build + cure per shift). |
| `GT_SHELF_LIFE_DAYS` | 3 | GT cannot sit >3 days before curing (plant rule). Must equal `TOPUP_LOOKAHEAD_DAYS_GT`. |
| `CARCASS_SHELF_LIFE_DAYS` | 1 | Stage-1 carcass shelf life: 1 day. |
| `Stage-2 CO time multiplier` | **2.0×** (applied in `building.py`) | Stage-2 `co_time_map` uses `diff × 2.0` (88 min → 176 min) to discourage LP from overloading Stage-2 with SKU switches. |

---

## Pipeline execution order

> **ROLLING PIPELINE IS THE DEFAULT** (`python b2c_pipeline.py`). The 3-step legacy pipeline is available via `--legacy` flag.
> Full spec in `approach/bc.md` §19.

### Legacy pipeline (--legacy flag — run: `python b2c_pipeline.py --legacy`)

```
Step 1: curing_consumption_dynamic.py
  Phase 0  → Curing Consumption Table (classification + press counts + per-shift targets)
  Phase 0+ → CO Schedule: urgency-ranked Pass 1 (Class A ONLY, max MAX_CHANGEOVERS_PER_DAY/day)
                └─ CO Rescue pass: spare-press donation for NRI SKUs without any CO
             CO candidates sort: min-CT target first → exclusive press first (§19.5)

Step 2: building_b2c.py
  Phase 1a → Runner-In building (Stage-1, Stage-2, Unistage) — highest priority
                └─ Mould-constrained priority boost: priority × (1 + curr_days/31), capped 4×
                └─ RI SKUs with no eligible building machines SKIPPED in synthetic curing plan
  Phase 1b + 2a → Joint Priority Pool (NRI building + Runner-Out CO eligibility) — residual capacity
                └─ NRI synthetic demand: 70% pre-CO / 30% post-CO (co_day_map front-loading)
                └─ NRI CO priority floor: 0.05 (ensures NRI with CO always get machines)
  Phase 2b → Pending CO scheduling (max MAX_CHANGEOVERS_PER_DAY/day)
  Phase 3  → Dynamic target lock (per-shift building caps frozen)

Step 3: curing_b2c.py  [ACTIVE — Phase 4 implemented]
  → GT-balance shift-by-shift curing simulation
  → Press state from testing_Daily_Running_Moulds (167 presses, WCNAME format)
  → CO transitions from building Changeover Plan sheet
  → Pre-plan-start building GT credited to opening balance
  → Output: bc_curing_b2c.xlsx (6 sheets)
```

### Rolling pipeline (DEFAULT — run: `python b2c_pipeline.py`)

**Architecture:** Per-shift loop — building assignment runs once per shift (A, B, C), reacting to
that shift's actual curing press state. Curing and building are simulated simultaneously shift-by-shift.
No synthetic 31-day plan. Confirmed matches the plant's actual scheduling practice (plant schedule
09.04.2026 shows 17 of 24 GT machines run 2–3 campaigns per shift with intra-shift COs).

**Key function: `_assign_building_shift` in `b2c_pipeline.py`**
- Time budget: SHIFT_MINS (480 min) — NOT 3 × SHIFT_MINS
- Max COs: MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT (2 per shift)
- Deficit signal: `shift_cure_demand[sku]` — presses running THIS SHIFT only, plus pre-build signal for CO target SKUs in Shifts A/B
- machine_current_sku updated at END of EACH shift (carries over to next shift)
- Same-inch CO tried before diff-inch CO; dominant inch preferred within each bucket
- Shift B/C CO restriction: machine stays on its primary SKU unless demand is 0 or curing CO forces a switch

**Parameters added to `_assign_building_shift`:**
- `co_target_skus: frozenset` — SKUs that curing presses are switching to today (from `co_press_map`); building must pre-build these even in Shifts B/C
- `allow_new_co: bool` — `True` for Shift A (machine free to pick any eligible SKU), `False` for Shifts B/C (restricted to CO only when current demand done, target is in `co_target_skus`, or target is a starving RI)
- `ri_running_skus: frozenset` — SKUs with curing presses in RUNNING state this shift (not in CO). Used to compute `starving_ri` (running press + zero GT inventory + demand > 0). Starving RI SKUs bypass the Shift B/C CO restriction so machines can redirect to feed a starving press mid-shift.

**`_MACHINE_DOMINANT_INCH` (module-level dict in `b2c_pipeline.py`):** per-machine preferred inch used to sort CO candidates — dominant-inch SKUs (`inch_penalty=0`) sort before non-dominant (`inch_penalty=1`) within same_cands and diff_cands buckets. This replaces the old history-based TIER 4 bias.

```
Pre-computation (once):
  CO schedule    → COScheduler (which press COs on which day, Class A only)
  Allow map      → MACHINE_HARD_INCH filter applied
  CT map         → ConsumptionETL (curing), _BLD_CT_SEC (building)
  Press state    → testing_Daily_Running_Moulds (167 presses)
  GT inventory   → opening balance from DB
  Demand         → demand file

for Day D in 1..31:
  co_target_skus = {new_sku for each curing CO on Day D}

  for Shift S in [A, B, C]:

    Step 1 — Per-shift curing demand
      shift_cure_demand[sku] = sum over presses in RUNNING state for shift S
        (CO presses: Shift A=CHANGEOVER → contribute 0;
                     Shifts B and C: new_sku already RUNNING → contributes 1 shift demand)
      Pre-build signal (Shift A ONLY):
        for each new_sku in co_target_skus:
          shift_cure_demand[new_sku] += _cure_qty_per_shift(new_ct)
        → Building sees a positive deficit in Shift A and starts producing GT
          so inventory is ready before Shift B curing fires

    Step 2 — Greedy building assignment (_assign_building_shift)
      allow_new_co = (Shift == "A")
      For each machine M (VMI first, then BJ, Unistage, Stage2, Stage1):
        dom_inch = _MACHINE_DOMINANT_INCH[M]  (e.g. 7001→16", 7002→14")
        Campaign 1: serve current SKU (no CO cost)
        Campaign 2+: CO to deficit SKUs, same-inch first (dominant inch preferred):
          Shift A: any eligible SKU with deficit
          Shifts B/C: only if cur_sku demand == 0 OR target ∈ co_target_skus OR target ∈ starving_ri
          guard: CO_cost ≤ 30% remaining AND remaining_after_CO ≥ MIN_CAMPAIGN_MINS
          max 2 COs per shift (MAX_BUILDING_COS_PER_MACHINE_PER_SHIFT)
      Returns: {machine: [(sku, qty, co_type)]}

    Step 3 — Add building GT to inventory; record shift rows + CO events
      gt_inventory[sku] += qty_built
      machine_current_sku[machine] = last_sku_in_shift   ← updated per-shift

    Step 4 — Curing simulation
      for each press:
        RUNNING:    cured = min(capacity, gt_available); gt_inventory -= cured
        CHANGEOVER: press idle (Shift A of CO day only)

  End of day: apply CO transitions (press_state updated after all 3 shifts)
```

**Key differences from legacy:**

| Property | Legacy | Rolling |
|----------|--------|---------|
| Building plan | 31-day LP upfront | Greedy per-SHIFT from actual press state |
| Assignment granularity | Per-day (then distributed evenly × 3) | Per-shift (reacts to each shift's actual demand) |
| Curing target | Synthetic 31-day plan | Actual press_count × qty_per_shift per shift |
| GT buffer | TOPUP_LOOKAHEAD_DAYS_GT = 3 days | GT_BUFFER_SHIFTS = 1 shift |
| Building CO (Shift A) | LP penalty discourage | Free to pick any eligible SKU; same-inch + dominant inch preferred |
| Building CO (Shifts B/C) | Unrestricted | Restricted: only when cur demand=0 or curing CO forces it |
| Primary SKU per day | No concept | Machine locks to Shift A choice in Shifts B/C |
| CO target pre-build | Separate pre-start mechanism | Injected into shift_cure_demand in Shift A ONLY |
| machine_current_sku | Updated end of day | Updated end of EACH SHIFT |
| Starvation | Possible (synthetic plan mismatch) | Near-zero by construction |
| Plant match | N/A | Confirmed matches 09.04.2026 plant schedule pattern |

### In-shift building CO pattern (confirmed from plant data)

One primary SKU per machine per day. Shift A selects the anchor (dominant inch first). Shifts B/C continue on that anchor; COs in B/C only when the anchor's demand is 0 or a curing CO forces a switch:

```
Machine 7001 (dominant 16"):
  Shift A: picks 195/55 R16 (highest-deficit 16" SKU) as anchor
           ──[same_size_CO 20 min]──►  215/60 R16 (if 2nd 16" press group has deficit)
  Shift B: continues 195/55 R16 anchor (demand > 0, no CO target forcing switch)
  Shift C: continues 195/55 R16 anchor

Machine 7002 (dominant 14"):
  Shift A: 165/80 R14 TOURING ──[same_size_CO 20 min]──► 165/80 R14 TAXI MAX
  Shift B: continues 165/80 R14 TOURING (same logic)
```

CO math per shift for VMI same-inch (plant currently avg 0.57 CO/shift/machine):
  1 CO × 20 min = 4.2% overhead → 95.8% production  ✓ (typical today)
  2 CO × 20 min = 8.3% overhead → 91.7% production  ✓ (max — when 2 press groups need feeding)
  2 diff_size_CO × 120 min each → 50% overhead      ✗ BLOCKED (violates 80% floor)
Full KPI comparison (old vs new architecture): see `approach/bc.md` §20.
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
| [bc_config.py](bc_config.py) | **SINGLE SOURCE OF TRUTH for ALL parameters** — `MAX_CHANGEOVERS_PER_DAY`, `MIN_CAMPAIGN_MINS`, `BUILD_LEAD_SHIFTS`, `TOPUP_LOOKAHEAD_DAYS_GT`, `PLAN_START`, `PLANNING_DAYS`, output paths. Edit only this file; all pipeline files import from it. |
| [b2c_pipeline.py](b2c_pipeline.py) | **CORRECT ENTRY POINT** — runs all 3 steps in one command (`python b2c_pipeline.py`): curing_consumption_dynamic → building_b2c → curing_b2c. Do NOT run component files directly. |
| [building_b2c.py](building_b2c.py) | B2C building scheduler — Phase 1a/1b/2a/2b/3. Config params received as function arguments; not hardcoded. |
| [curing_b2c.py](curing_b2c.py) | **B2C curing simulation (Phase 4)** — GT-balance shift-by-shift simulation. Reads building output + CO plan. Press IDs use `WCNAME_clean` format (e.g. "75206") — MUST match `curing_consumption_dynamic.py` CO event format. Do NOT use `wcID` from WC Master as press key. |
| [curing_consumption.py](curing_consumption.py) | Phase 0 — Day 0 snapshot consumption table + press counts. Reads from `testing_Daily_Running_Moulds` (replaced `Daily_Running_Moulds` at commit b258a93). |
| [curing_consumption_dynamic.py](curing_consumption_dynamic.py) | Phase 0 Extended — 31-day pre-computed curing consumption + CO schedule. Pass 1 = CO schedule (Class A only, cap = `MAX_CHANGEOVERS_PER_DAY` from `bc_config.py`); Pass 2 = simulate 31 days. Press IDs = `WCNAME_clean` format. Output: `curing_consumption_31day.xlsx`. |
| [building.py](building.py) | Base building machinery (LP engine + DemandHeuristicAssigner) reused by B2C |
| [bc_config.py](bc_config.py) | All tunable params (see above) |
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

### Press ID format — WCNAME_clean, not wcID

`curing_consumption_dynamic.py` uses `str(r["Machine"])` from the Day 0 running moulds
output as press IDs. "Machine" = the `WCNAME` column after stripping "LH"/"RH" suffix
(e.g. "75206LH" → "75206"). These are numeric press labels used on the shop floor.

`curing_b2c.py`'s `_load_press_state` **must** use the same format — set
`press = WCNAME.str.replace(r"(LH|RH)$", "", regex=True)`. Never use `wcID` from
`Master_WC_Master` as the press key: 30 of 167 presses have no WC Master entry
(wcID = NaN) and would be silently dropped, and the remaining 137 would have IDs
(like "436") that never match CO event press IDs (like "75206").

**Rule:** Press key = `WCNAME_clean` everywhere (running moulds, CO events, press_state dict,
output sheets). This format is human-readable and matches the physical press labels.

### CO over-aggressiveness — RI presses CO'd before demand fulfilled

When `curing_consumption_dynamic.py` CO's a press from an RI SKU to an NRI SKU, it does not
currently verify that remaining presses can still fulfill the RI SKU's demand within the horizon.

Example: SKU with demand 18,913, 4 presses, 4×56×3 = 672/day capacity. CO'ing 3 presses
by Day 3 leaves 1 press (56×3×31 = 5,208 capacity) — far below 18,913. Result: 12,919 GT
sits uncured at month end.

**Fix implemented in `curing_consumption_dynamic.py` (main CO loop, line ~339):**
```python
if old_sku in ri_skus:
    n_remaining = press_count.get(old_sku, 0) - 1
    rem_old = updated_demand.get(old_sku, 0)
    if rem_old > 0 and n_remaining > 0:
        rate_old = _qty_per_press_per_day(ct_map.get(old_sku, DEFAULT_CT))
        if rate_old > 0 and rem_old / (n_remaining * rate_old) > horizon_left:
            continue  # remaining presses cannot cover old_sku demand
```
The rescue pass already had an equivalent guard using original `demand_map` values and
capacity accounting; the main loop now applies the same protection. Both guards together
ensure no RI press is CO'd unless remaining presses can still fulfill the RI SKU's demand.

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
| **TOTAL** | **39** | **89** | **653,138** | **615,438** | **37,700** | **94.4%** | |

**Structural ceilings:**
- BJ: demand/capacity ratio = 249,633 / ~184k = 136% — **physically oversubscribed**. Cannot close the 20k gap via scheduling. Fix = more BJ presses or VMI certification.
- VMIMAXX: demand/capacity = 239,156 / 357,120 = 67% ceiling — machines are **undersubscribed**. 27k gap is scheduling overhead (CO + idle tail); ~10–15k recoverable.
- UNI\_NARROW: demand/capacity = 56,717 / 89,280 = 63% ceiling — also undersubscribed. 12k gap is scheduling; ~5–8k recoverable.
- STAGE2: 95.7% coverage, near ceiling with Stage-1 feed constraints.
- No machine data: 51,501 permanently unbuilt until master data certifications added.

**Rolling pipeline actual (May 2026, current code): 615,438 GT built / 616,514 cured / 94.4% coverage**
**Note:** Curing exceeds building because opening GT inventory (6,199) from pre-start shifts is also consumed.
**Scheduler ceiling (without master data changes): ~620–625k / 653,138 ≈ 95–96%** (after data fix for HURL0 + TVECE)
**True ceiling (if all no-master-data SKUs certified): ~635k+ / 653,138 ≈ 97%+**

### BJ oversubscription — SKU-level breakdown (May 2026)

BJ machines built 181,539 units on 255,515 production minutes out of 312,480 available.
The 38,430 idle minutes (12.3%) occur at end-of-demand tails, not from lack of work.

**BJ-exclusive SKUs with unmet demand (no VMIMAXX/Stage-2 alternative):**

| SKU | Demand | Built | Gap | Root cause |
|-----|--------|-------|-----|------------|
| `1225221715115SSTL0` (RI) | 13,106 | 7,709 | **5,397** | Too few curing presses — building capped at curing throughput |
| `1225119015010QSTL0` (NRI) | 1,744 | 1,233 | 511 | CO Day 25 — only 6 days post-CO |
| `1325119015008SRBT0` (NRI) | 4,439 | 4,080 | 359 | CO Day 19 — late CO |
| `1225219015010QSTL0` (NRI) | 1,534 | 1,181 | 353 | CO Day 27 — only 4 days post-CO |

**Key rule:** When a Runner-In SKU's gap shows "demand cap applied", the root cause is **curing presses, not building capacity**. Building is already correct. The fix is a curing CO to add more presses. Do NOT try to fix these by changing building logic.

**NRI late-CO gap (~1,800 units):** Recoverable by scheduling COs earlier in `curing_consumption_dynamic.py` for the 3 NRI SKUs above. Full details in `approach/bc.md` §18.4.
