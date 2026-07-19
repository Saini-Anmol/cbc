# B2C Scheduler — Deployment DB Contract & Config Mapping (v1)

Authoritative spec for the cloud deployment. Two halves:
1. **DDL changes** to apply to the schema (items 1–3 below) — runnable SQL.
2. **ETL rules** the deployment code (`connection.py` / `main.py`) must follow
   when it reads inputs and writes outputs (items 4–5, and the config mapping).

`plan_id` is the run-identity key — every input/output row for one scheduler run
carries the same `plan_id`. One run = one `jkt_plan_params` row + one `jkt_demand`
set → the 5 output tables, all stamped with that `plan_id`.

---

## Config mapping — input DB columns → `bc_config.py`

| DB column (`jkt_plan_params` / `jkt_plan_presets`) | Maps to | Notes |
|---|---|---|
| `planStartDate` | `PLAN_START` | date @ 07:00 (first shift A) |
| `planStartDate` + `planEndDate` | `PLANNING_DAYS` | `(planEndDate − planStartDate).days + 1` |
| `noOfChangeOver` | `MAX_CHANGEOVERS_PER_DAY` | curing CO cap/day. v1 = **12** |
| `efficiency` | `PRESS_EFFICIENCY` (=0.94) | **curing CT only**: `CT = (rawCureTime + 2.3) / efficiency`. Building CT (in code) has no efficiency factor. |
| `mouldAvailability` | — (v2 only) | v2: load real per-press opening mould life (SKU→mould mapping). **v1 ignores it** — every press starts fresh at 3000 cycles, so 0/1 has no effect. Distinct from the always-on 3000-cycle mould-clean rule. |
| market/target-date weightages, priority flags (`impPriorityFlag`, etc.) | ConsolidatedPriorityScore inputs | **v1 = requirement only** → these columns are **dormant** (reserved for v2 weighted scoring). |
| `optimisationPreset` | (traceability) | points to `jkt_plan_presets.presetName` — which saved preset this run was built from. Plain column (optional FK constraint). |

**Running-moulds (Day-0 press state):** on cloud, always
`jkplanningV1.Daily_Running_Moulds` (fixed). Masters (allowable machines,
curing CT, WC master, size master) read from existing DB tables. Building CT
stays in code (`_BLD_CT_SEC`).

**Priority score:** computed in code (`curing_consumption.load_demand` +
`b2c_pipeline`), min-max of requirement — `jkt_demand` needs only `skuCode` +
`requirement`, **no priority column**.

---

## 1–3. Schema DDL changes — ✅ APPLIED to `jkplanningV1` (verified)

> Applied live. Actual state at apply time differed from the pasted schema:
> `jkt_demand`, `jkt_plan_capacityUtilisation`, and both `_history` tables were
> **already `VARCHAR(100)`**; only `jkt_plan_params`, `jkt_plan_curing`,
> `jkt_plan_building`, `jkt_plan_Infeasibility`, `jkt_plan_kpis` needed widening.
> DATETIME/DATE types were already correct. All 9 tables now `plan_id VARCHAR(100)`;
> `kpis` & `params` have PK on `plan_id`; all multi-row tables have `idx_plan`.
> The block below is the full set as-run (idempotent to re-check).

```sql
-- 1. Fix typo: creatdBy -> createdBy  (jkt_plan_capacityUtilisation)
ALTER TABLE jkt_plan_capacityUtilisation RENAME COLUMN creatdBy TO createdBy;

-- 2. Make plan_id VARCHAR(100) everywhere (join key must match type+collation).
--    Already 100: jkt_demand, jkt_plan_params, jkt_plan_capacityUtilisation.
ALTER TABLE jkt_plan_curing            MODIFY plan_id VARCHAR(100) NOT NULL;
ALTER TABLE jkt_plan_building          MODIFY plan_id VARCHAR(100) NOT NULL;
ALTER TABLE jkt_plan_Infeasibility     MODIFY plan_id VARCHAR(100) NOT NULL;
ALTER TABLE jkt_plan_kpis              MODIFY plan_id VARCHAR(100) NOT NULL;
ALTER TABLE jkt_plan_building_history  MODIFY plan_id VARCHAR(100) NOT NULL;
ALTER TABLE jkt_plan_curing_history    MODIFY plan_id VARCHAR(100) NOT NULL;

-- 3. Indexes / PK on plan_id.
--    kpis: one row per run -> plan_id is the PRIMARY KEY.
ALTER TABLE jkt_plan_kpis ADD PRIMARY KEY (plan_id);
--    Multi-row tables: plan_id is NOT unique -> non-unique index
--    (every read/delete is WHERE plan_id = ?; idempotent re-runs need it).
ALTER TABLE jkt_demand                 ADD INDEX idx_plan (plan_id, skuCode);
ALTER TABLE jkt_plan_curing            ADD INDEX idx_plan (plan_id);
ALTER TABLE jkt_plan_building          ADD INDEX idx_plan (plan_id);
ALTER TABLE jkt_plan_Infeasibility     ADD INDEX idx_plan (plan_id, skuCode);
ALTER TABLE jkt_plan_capacityUtilisation ADD INDEX idx_plan (plan_id);
ALTER TABLE jkt_plan_building_history  ADD INDEX idx_plan (plan_id);
ALTER TABLE jkt_plan_curing_history    ADD INDEX idx_plan (plan_id);
-- jkt_plan_params: plan_id already PRIMARY KEY. jkt_plan_presets PK = presetName.
-- Best practice on multi-row tables: also add an auto-increment surrogate
--   `id BIGINT AUTO_INCREMENT PRIMARY KEY` and keep the plan_id index for lookups.
```

---

## 4–5. ETL write-rules (implement in the deployment code, not DDL)

**4. `jkt_plan_kpis.curingChangeovers` = TOTAL curing COs (static + dynamic).**
Write `_n_co_total` (= `_n_co_planned + _n_co_dynamic`; currently 225 = 147 + 78),
which the pipeline already returns. Do **not** write the planned-only 147.

**5. `jkt_plan_Infeasibility` — insert a row ONLY when the SKU is infeasible.**
Filter the Demand Fulfillment result before insert; insert if either:
- `status == 'UNMET'`, **or**
- SKU has **missing master data** — no eligible building machine
  (`Eligible_Machines == 0` / absent from `Master_Building_Allowable_Machines`);
  set `skipReason = 'NO_ELIGIBLE_MACHINE'`.

Fully-met SKUs are **not** inserted. Per instruction, **PARTIAL** SKUs are
excluded (only UNMET + missing-master). Flip to `status IN ('UNMET','PARTIAL')`
later if you want every shortfall surfaced.

---
---

# Deployment phases (Phase 0 = DB, done. Phases 1–4 below.)

**Goal:** one source-agnostic engine; two orchestrators sharing it —
`local_main.py` (Excel I/O, today's behaviour) and `main.py` (DB I/O, cloud) —
behind an async API a deploy dev runs from an env file and a frontend dev calls.

**Guiding principle — the seam is thin.** The engine (`b2c_pipeline.py` +
`building_b2c.py` + `curing_b2c.py`) **already reads masters + running-moulds
from the DB** via its ETL objects. Only **three things** cross the local↔cloud
boundary: **(1) demand**, **(2) run config/params**, **(3) the 5 outputs**.
Do NOT rewrite the engine — wrap its I/O.

---

## Phase 1 — Carve the I/O seam (the foundation)

**Objective:** the engine takes inputs from an injected *reader* and hands
outputs to an injected *writer*, so Excel and DB are two implementations of one
contract. Ends at a **parity gate**: Excel run and DB run produce identical KPIs.

### 1.1 Define the contract (do this first, on paper → top of `io_contract.py`)

**Inputs the engine needs (the reader must supply):**
| Name | Shape | Local source | Cloud source |
|---|---|---|---|
| `demand_df` | `DataFrame[SKUCode, Requirement]` | `DEMAND_FILE` xlsx | `jkt_demand` (WHERE plan_id) |
| `run_cfg` | dict: `plan_start, planning_days, max_co_per_day, press_efficiency, plan_id` | `bc_config` constants | `jkt_plan_params` (WHERE plan_id) + preset |
| masters, running-moulds | (unchanged) | DB (existing ETL) | DB (existing ETL) |

Priority score is **computed in the engine** (min-max of Requirement) — reader
supplies only SKUCode + Requirement, both paths identical.

**Outputs the engine produces (the writer consumes) — expose these as DataFrames:**
| Frame | Built from | → Excel sheet | → DB table |
|---|---|---|---|
| `building_schedule` | `bld_shift_rows` | Building "Shift Schedule" | `jkt_plan_building` |
| `curing_schedule` | `cure_shift_rows` | Curing "Shift Schedule" | `jkt_plan_curing` |
| `infeasibility` | Demand-Fulfillment rows | "Demand Fulfillment" | `jkt_plan_Infeasibility` (rule 5 filter) |
| `kpis` | return dict + util aggregates | console | `jkt_plan_kpis` (rule 4) |
| `capacity_daily` *(optional/deferred)* | daily util | — | `jkt_plan_capacityUtilisation` |

### 1.2 Refactor the engine to RETURN the output frames

Today `_write_rolling_building_excel` / `_write_rolling_curing_excel` build the
row lists **and** write Excel in one step. Split that:
- Have `run_rolling_pipeline` collect `bld_shift_rows`, `cure_shift_rows`, the
  demand-fulfillment rows, and the machine-util aggregates into DataFrames and
  add them to its **return dict** (it already returns KPIs at line ~3536).
- The Excel writers become one *writer implementation* that takes those frames.
- **No scheduling logic changes** — pure plumbing. This is the only engine edit.

### 1.3 Two writer/reader implementations

- `local_io.py` — `read_local()` (Excel/`bc_config`) + `write_local()` (the
  current `_write_rolling_*_excel`, now fed the frames).
- `connection.py` — `read_db(plan_id)` + `write_db(plan_id, frames)` — **stub only
  in Phase 1** (real body in Phase 2). Stub can raise `NotImplementedError`.

### 1.4 Parity gate (the checkpoint)

`local_main.py` runs the engine through `local_io` and prints KPIs. Freeze those
numbers (current committed baseline: built 684,165 / cured 690,319 / 99.5% /
COs 225 / cleans 4). Phase 2's DB path must reproduce them **exactly** on the
same demand. If they differ, the seam leaked — stop and fix before Phase 3.

**Phase 1 done when:** `local_main.py` reproduces today's Excel output bit-for-bit
via the new seam, and `connection.py` exists as a stub implementing the contract.

---

## Phase 2 — DB read/write adapter (`connection.py`) — ✅ IMPLEMENTED & TESTED

> **Built in `connection.py` and verified against live `jkplanningV1`.**
> Design chosen: instead of refactoring the 3,600-line engine to return frames,
> `write_db` ingests the **freshly written workbooks** the engine already
> produces (DB↔Excel parity by construction) and takes CO count + coverage from
> the engine **return dict** (one dynamic CO spans two segment rows, so Excel
> row-counting would overcount). Two bugs found & fixed during testing:
> (1) `efficiency` is stored as a **percentage** (94.0) → normalise `>1 → /100`;
> (2) the Demand Fulfillment sheet appends a **KPI-summary footer block** (blank
> Status) — filter to `Status.notna()` before counting SKUs / flagging
> missing-master, else `demandSKU` inflates and footer rows get mis-flagged.

**Objective:** fill the stub so the cloud path reads inputs from the 3 input
tables and writes the 5 output tables, applying rules 4 & 5.

### 2.1 `read_db(plan_id)`
- `demand_df` ← `SELECT skuCode, requirement FROM jkt_demand WHERE plan_id=?`.
- `run_cfg` ← `SELECT * FROM jkt_plan_params WHERE plan_id=?`, then map:
  - `planStartDate` → `plan_start` (@07:00); `(planEndDate−planStartDate).days+1` → `planning_days`
  - `noOfChangeOver` → `max_co_per_day`; `efficiency` → `press_efficiency`
  - `mouldAvailability` → ignored (v2); weightages/flags → ignored (v1)
  - fall back to the chosen `optimisationPreset` row in `jkt_plan_presets` for any null.
- masters + running-moulds: unchanged (existing ETL; cloud running-moulds table
  is fixed = `jkplanningV1.Daily_Running_Moulds`).

### 2.2 `write_db(plan_id, frames)` — column mapping (verified against live schema)
- **`jkt_plan_building`** ← building_schedule → `Date, Shift, Machine, SKUCode,
  skuDescription, StartTime(DATETIME), EndTime(DATETIME), Qty, Machine_Group, CO_Type`.
  (No CO_Mins column — CO duration = EndTime−StartTime.)
- **`jkt_plan_curing`** ← curing_schedule → `Date, Shift, Machine, SKUCode,
  skuDescription, StartTime, EndTime, Qty, CycleTime_min, GT_Inventory, Remarks`.
  (No CO_Mins/Mould_Clean_Mins — recoverable from Start/End + Remarks.)
- **`jkt_plan_kpis`** (one row) ← `demandFulfillment=coverage%, demandSKU, planSKU,
  curingChangeovers=_n_co_total` (**rule 4**), and the 3 **occupancy** metrics:
  `capacityUtilisation` = curing Σ(prod+CO+clean)/Σ(avail);
  `building_capacityUtilisation` = building Σ(prod+CO)/Σ(avail);
  `building_s2_capacityUtilisation` = same over Stage-2 only.
- **`jkt_plan_Infeasibility`** ← infeasibility filtered to UNMET + missing-master
  (**rule 5**) → `plantName, productName, skuCode, skuDescription, priority, demand,
  plannedUnits, status, skipReason`.
- **`jkt_plan_capacityUtilisation`** — deferred (leave empty for now).
- Every row stamped `plan_id` + `createdAt`/`createdBy`. Plain insert (unique
  plan_id per run → no pre-delete needed).

**Phase 2 done when:** `main.py` on the same demand reproduces the Phase-1 frozen
KPIs and the 5 tables populate correctly (spot-check row counts + a few rows).

---

## Phase 3 — Orchestrators + runtime config injection — ✅ IMPLEMENTED & TESTED

> **Built as `local_main.py` (Excel) + `main.py` (DB), verified end-to-end.**
> Config injection = monkeypatch the two per-run module globals before the run:
> `b2c_pipeline.MAX_CHANGEOVERS_PER_DAY` and
> `curing_consumption.ConsumptionConfig.PRESS_EFFICIENCY` (plan_start / days /
> demand are passed as call args). `main.py` stages DB demand to a temp xlsx,
> runs the engine to a temp workdir, then `write_db`. End-to-end test on a
> cloned plan (`start=2026-06-01, days=30, max_co=8`): DB KPIs reconciled exactly
> with the engine console (coverage 95.5%, curing COs 171, 5 UNMET) — 4 tables
> populated, then test rows cleaned up.

**Objective:** two thin entry points; make the `bc_config` constants overridable
per-run so DB params actually drive the engine (today they're module constants).

- **Config injection:** wrap the ~6 run knobs (`PLAN_START, PLANNING_DAYS,
  MAX_CHANGEOVERS_PER_DAY, PRESS_EFFICIENCY, DEMAND source, plan_id`) into a
  `RunConfig` object passed into `run_rolling_pipeline`, instead of reading module
  globals. `bc_config` stays the default; DB params override per call. (Masters/
  toggles stay as constants — only per-run knobs are injected.)
- **`local_main.py`:** `read_local` → `run_rolling_pipeline(run_cfg)` → `write_local`.
  Reproduces today's `python b2c_pipeline.py` exactly.
- **`main.py`:** `plan_id` in → `read_db(plan_id)` → engine → `write_db(plan_id,…)`.
  Wrap in try/except that writes a run status (success/fail + message) so the API
  and frontend can poll state.

**Phase 3 done when:** both orchestrators run end-to-end from their own source and
pass the parity gate; a `plan_id` round-trips DB→engine→DB.

---

## Phase 4 — Flask API + handoff — ✅ IMPLEMENTED & TESTED

> **Corrected from the original "async" idea.** The reference contract
> (`approach/JKT_API_and_DataFlow.pdf`) is **synchronous Flask**: the run happens
> inside the request and the response returns when done with `elapsed_seconds`.
> Built as `app.py`, matching that contract exactly.

**Contract (identical to the existing JKT planning page, so the frontend calls
it the same way):**
- Prefix `/app/v1/jkt/planning-scheduling`.
- `POST /plan/generate-plan` body `{"plan_id":"<id>"}` — validate: JSON object,
  non-empty string, ≤ 50 chars.
- `GET /health`.
- Success 200: `{status:"success", mode:"planning", plan_id, elapsed_seconds}`.
- Error: `{status:"error", stage, mode, plan_id, message}` + HTTP
  400 (bad body / empty) · 422 (>50 chars) · 404 (no input rows) ·
  409 (a run already in progress) · 500 (engine/write failure).

**v1 decisions:** planning mode only (no simulation / no `jkt_sim_*`);
re-run **overwrites** (`write_db overwrite=True`) — 409 is used ONLY for a
concurrent run (`_RUN_LOCK`, process-wide serialisation). Timestamps in **IST**
(`connection.now_ist()`).

**Handler flow** (`app.generate_plan` → `main.run_plan`):
`validate → run_plan(plan_id) [read_db → inject cfg → engine → write_db] → elapsed_seconds`.
`read_db` raising ValueError (no input rows) maps to 404.

**Run it:**
```
pip install flask                       # the one new runtime dep
API_PORT=8000 python app.py             # dev server; API_HOST/API_PORT from env
# production: gunicorn -w 1 -t 600 app:app   (long timeout — runs block ~1-4 min;
#                                             1 worker + _RUN_LOCK = one run at a time)
```

**`.env`** (already present): DB creds `JKT_DB_*` (read via `cbc_env`). Add
`API_HOST` / `API_PORT` for the server. Nothing hardcoded.

**Frontend contract (hand off):** frontend mints a unique `plan_id` (≤50 chars),
writes the `jkt_demand` + `jkt_plan_params` rows, then `POST /plan/generate-plan
{plan_id}` and **waits** (synchronous, ~1-4 min) for the 200 + `elapsed_seconds`;
on success reads the 4 output tables (`jkt_plan_building`, `jkt_plan_curing`,
`jkt_plan_Infeasibility`, `jkt_plan_kpis`) by `plan_id` to render the report.

**Tested (live, port 8077):** `/health`→200; bad body→400; empty→400; >50→422;
unknown plan_id→404; real plan (cloned)→**200 `elapsed_seconds` 63.3**, all 4
tables populated; concurrent POST→409; re-run→overwrite (kpis stayed 1 row, no
duplication); `createdAt` in IST. Test rows cleaned up afterwards.

**Phase 4 done.** Full chain works: `POST {plan_id}` → engine → 4 output tables,
synchronous, contract-compliant.

---

### Critical-path order & the one gate that matters

`Phase 1 (seam) → Phase 2 (DB adapter) → Phase 3 (orchestrators) → Phase 4 (API)`.
The **parity gate** (Phase 1, re-checked in Phase 2) is the single most important
checkpoint: if the DB path ever diverges from the Excel KPIs on identical demand,
the seam changed the math — fix before proceeding. Everything else is plumbing.
