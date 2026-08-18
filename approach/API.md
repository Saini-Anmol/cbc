# JKT BTP Planning (B2C Scheduler) — API & Integration Guide

For the frontend developer. Everything needed to call, test, and debug the
scheduling service.

> **The API takes only a `plan_id`.** All real inputs are read from the DB and
> all results are written back to the DB. So the frontend does three things:
> **(1) write the input rows → (2) POST the plan_id → (3) read the output tables.**

---

## 1. Endpoints

Base prefix: `/app/v1/jkt/planning-scheduling`
(local/dev container: `http://<host>:5001`)

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/health` | liveness check |
| `POST` | `/plan/generate-plan` | run the scheduler for one `plan_id` |
| `GET`  | `/plan/download/<plan_id>/building` | download the building schedule .xlsx |
| `GET`  | `/plan/download/<plan_id>/curing` | download the curing schedule .xlsx |

### `GET /health`
```json
{ "status": "ok", "mode": "planning" }        // HTTP 200
```

### `POST /plan/generate-plan`
Request body:
```json
{ "plan_id": "BTP_om_planning_new_776633" }
```
Validation: JSON **object**, `plan_id` = non-empty **string**, **≤ 100 chars**
(matches the `plan_id VARCHAR(100)` column in every table).

Success — **HTTP 200**:
```json
{
  "status": "success",
  "mode": "planning",
  "plan_id": "BTP_om_planning_new_776633",
  "elapsed_seconds": 67.9
}
```

Error — same envelope for every failure:
```json
{
  "status": "error",
  "stage": "validate|read|lock|schedule",
  "mode": "planning",
  "plan_id": "...",
  "message": "human readable reason"
}
```

| HTTP | `stage` | When |
|------|---------|------|
| 400 | `validate` | body is not a JSON object, or `plan_id` missing/empty/not a string |
| 422 | `validate` | `plan_id` longer than 100 chars |
| 404 | `read` | no rows in `jkt_demand` / `jkt_plan_params` for that `plan_id` |
| 409 | `lock` | **another plan is already running** (only one at a time) |
| 500 | `schedule` | engine or DB-write failure (see `message` + container logs) |

---

## 2. ⚠️ Four things that will bite you if you don't know them

1. **The call is SYNCHRONOUS and takes 1–4 minutes.** It returns only when the
   whole plan is generated (`elapsed_seconds` tells you how long). **Set your HTTP
   client timeout to ≥ 30 minutes.** Default `fetch`/`axios`/nginx timeouts (30–120 s)
   WILL kill the request mid-run — this is the single most common integration bug.
   Also raise any proxy/load-balancer read timeout.
2. **Only one run at a time.** A second POST while one is running returns **409**.
   Disable the "Generate" button while a run is in flight, or queue.
3. **`plan_id` must be unique per run.** Re-posting the same `plan_id`
   **overwrites** that plan's previous output rows (delete + re-insert).
4. **Write the input rows BEFORE calling.** If `jkt_demand` or `jkt_plan_params`
   has no row for the `plan_id`, you get **404**.

---

## 3. DB contract

### Inputs — frontend WRITES these before calling

**`jkt_demand`** (one row per SKU)

| Column | Required | Notes |
|--------|----------|-------|
| `plan_id` | ✅ | ties the demand to the run |
| `skuCode` | ✅ | |
| `requirement` | ✅ | integer units |
| `skuDescription` | optional | copied into the output tables for display |
| `market`, `deliveryDate` | optional | **not used in v1** |

> **No priority column needed.** The priority score is computed by the engine
> (min-max normalisation of `requirement`). Any priority column is ignored.

**`jkt_plan_params`** (exactly one row)

| Column | Required | Notes |
|--------|----------|-------|
| `plan_id` | ✅ | primary key |
| `planStartDate` | ✅ | first day of the plan (starts 07:00) |
| `planEndDate` | ✅ | **horizon = end − start + 1 days** |
| `noOfChangeOver` | ✅ | curing changeover cap **per day** (typical: 12) |
| `efficiency` | ✅ | **stored as a percentage, e.g. `94`** (not 0.94) |
| `plantName`, `productName` | optional | echoed into outputs (e.g. `BTP` / `PCR`) |
| `optimisationPreset` | optional | name from `jkt_plan_presets`, used for defaults |
| weightage / priority flag columns | — | **dormant in v1** |

### Outputs — frontend READS these after a 200

| Table | Contents |
|-------|----------|
| `jkt_plan_building` | building shift schedule (~7k rows/plan) |
| `jkt_plan_curing` | curing shift schedule (~15k rows/plan) |
| `jkt_plan_Infeasibility` | **only** UNMET SKUs + SKUs with missing master data |
| `jkt_plan_kpis` | one summary row (PK `plan_id`) |
| `jkt_plan_capacityUtilisation` | one **overall/monthly** row (PK `plan_id`) — machine-group utilisation |

All filtered by `plan_id`. Timestamps (`createdAt`) are **IST**.

### `jkt_plan_capacityUtilisation` (one row per plan — NOT per day)

| Column | Meaning | Example (May) |
|--------|---------|---------------|
| `plan_id` | PK | |
| `date` | plan **start** date (identifies the month) | 2026-05-01 |
| `capacityUtilisation` | curing press occupancy % | 87.76 |
| `building_capacityUtilisation` | all 39 building machines % | 65.67 |
| `vmi_capacityUtilisation` | VMI (8) | 80.16 |
| `bj_capacityUtilisation` | BJ (7) | 84.90 |
| `uniNarrow_capacityUtilisation` | US / UNI_NARROW, 7501-7503 (3) | 65.41 |
| `building_s2_capacityUtilisation` | **all GT-making machines** (VMI+BJ+UNI_NARROW+Stage-2 = 24) | 77.63 |
| `stage1_capacityUtilisation` | Stage-1 (15) | 46.54 |

These values are computed once and written to **both** this table and
`jkt_plan_kpis`, so the two can never disagree.

### `jkt_plan_kpis` columns (one row per plan)

| Column | Meaning | Example (May) |
|--------|---------|---------------|
| `demandFulfillment` | demand coverage % | 99.03 |
| `demandSKU` / `planSKU` | SKUs in demand / SKUs actually planned | 85 / 81 |
| `capacityUtilisation` | **curing press** occupancy % | 87.76 |
| `building_capacityUtilisation` | **all 39 building machines** occupancy % | 65.67 |
| `vmi_capacityUtilisation` | VMI group (8 machines) | 80.16 |
| `bj_capacityUtilisation` | BJ group (7 machines) | 84.90 |
| `uniNarrow_capacityUtilisation` | UNI_NARROW / "US" machines (7501-7503) | 65.41 |
| `building_s2_capacityUtilisation` | **all GT machines** (VMI+BJ+UNI_NARROW+Stage-2, 24) | 77.63 |
| `stage1_capacityUtilisation` | Stage-1 (15 machines) | 46.54 |
| `curingChangeovers` | curing COs, **total** = planned + dynamic | 200 |
| `buildingChangeovers_sameSize` | building same-size CO count (cheap, ~20 min) | 2048 |
| `buildingChangeovers_diffSize` | building diff-size CO count (costly, up to 180 min) | 208 |
| `buildingChangeovers` | total building COs = same + diff | 2256 |

> **Occupancy %** = (production + changeover + mould-clean) / available time —
> how *busy* the machines were, not how *productive*. Don't confuse it with
> `demandFulfillment`. Mould-clean applies to curing only.
>
> **Note on `building_s2_capacityUtilisation`:** despite the `s2` in the name, it is the
> occupancy of **all GT-making machines** (VMI + BJ + UNI_NARROW + Stage-2 = 24) — NOT
> Stage-2 alone. Stage-1 (carcass) is excluded; use `stage1_capacityUtilisation` for that.
>
> `building_capacityUtilisation` covers **all 39** building machines; the five
> group columns break that total down. **Stage-1 sitting ~46% is expected, by
> design** (it is structurally under-utilised) — it is what pulls the 39-machine
> total below the individual GT groups. Stage-1 changeovers are not modelled, so
> Stage-1 contributes 0 to the changeover counts.

Both `jkt_plan_kpis` and `jkt_plan_capacityUtilisation` are written on every run.

---

## 4. Excel outputs

Each run also writes two workbooks (same format as the local/desktop tool) to the
container's `/app/output` (mount a host volume there):

```
bc_building_schedule_<plan_id>_<planStartDate>.xlsx
bc_curing_b2c_<plan_id>_<planStartDate>.xlsx
```

Building sheets: `Shift Schedule`, `Changeover Plan`, `SKU Classification`,
`Daily GT & Carcass`, `Demand Fulfillment (B2C)`, `Machine Utilization`.
Curing sheets: `Demand Fulfillment`, `Machine Utilization`, `Shift Schedule`,
`Changeover Plan`, `Mould Tracker`, `Mould Movement`, `Machine Schedule`,
`Daily Cured tyres`, `GT Gap Diagnostic`.
(As of 2026-07: `Changeover Plan` also lists **mould-clean** rows (480 min) alongside COs;
`Mould Tracker` is now one row per press×mould×SKU-run and `Mould Movement` logs every mould
swap; curing `Machine Utilization` "Avg util" = occupancy `(Used+CO+Clean)/Available`.)

### Downloading them — `GET /plan/download/<plan_id>/<kind>`

`kind` = `building` or `curing`.

```
GET /app/v1/jkt/planning-scheduling/plan/download/BTP_June_Plan_R2_941998/building
GET /app/v1/jkt/planning-scheduling/plan/download/BTP_June_Plan_R2_941998/curing
```

- **200** → the `.xlsx` file as an attachment
  (`Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`,
  filename `bc_building_schedule_<plan_id>_<date>.xlsx`).
- **404** → no workbook for that `plan_id` (plan not generated yet, or the
  container was recreated **without** a volume mounted at `/app/output`).
- **400** → `kind` is not `building` / `curing`.

Frontend usage: after `POST /plan/generate-plan` returns **200**, enable two
download links pointing at these URLs. No request body, no auth headers — a
plain link or `window.open` is enough. The date in the filename is resolved
server-side, so you never need to know the plan start date.

> **Ops requirement:** the container must run with a volume at `/app/output`
> (`-v "${PWD}\output:/app/output"`). Without it the workbooks live only inside
> the container and disappear on restart, and downloads start returning 404.

---

## 5. Testing from the terminal

```bash
BASE=http://127.0.0.1:5001/app/v1/jkt/planning-scheduling

# 1. health
curl -i $BASE/health

# 2. run a plan — NOTE --max-time, the call blocks for minutes
curl -s -w "\n[HTTP %{http_code}] %{time_total}s\n" --max-time 3600 \
  -X POST $BASE/plan/generate-plan \
  -H 'Content-Type: application/json' \
  -d '{"plan_id":"YOUR_PLAN_ID"}'

# 3. error cases
curl -s -X POST $BASE/plan/generate-plan -H 'Content-Type: application/json' -d '"oops"'          # 400
curl -s -X POST $BASE/plan/generate-plan -H 'Content-Type: application/json' -d '{"plan_id":""}'  # 400
curl -s -X POST $BASE/plan/generate-plan -H 'Content-Type: application/json' -d '{"plan_id":"NOPE"}' # 404
```

Verified sample responses:
```
GET /health            -> 200 {"mode":"planning","status":"ok"}
POST '"oops"'          -> 400 {"stage":"validate","message":"request body must be a JSON object", ...}
POST {"plan_id":""}    -> 400 {"stage":"validate","message":"plan_id must be a non-empty string", ...}
POST 101-char id       -> 422 {"stage":"validate","message":"plan_id must be <= 100 chars", ...}
POST unknown id        -> 404 {"stage":"read","message":"jkt_demand has no rows for plan_id='NOPE_123'", ...}
POST valid id          -> 200 {"status":"success","elapsed_seconds":67.9, ...}
```

Check the results landed:
```sql
SELECT COUNT(*) FROM jkt_plan_building      WHERE plan_id='YOUR_PLAN_ID';
SELECT COUNT(*) FROM jkt_plan_curing        WHERE plan_id='YOUR_PLAN_ID';
SELECT * FROM jkt_plan_kpis                 WHERE plan_id='YOUR_PLAN_ID';
SELECT * FROM jkt_plan_Infeasibility        WHERE plan_id='YOUR_PLAN_ID';
```

---

## 6. Debugging checklist

| Symptom | Likely cause |
|---------|--------------|
| Client times out ~30–120 s, server still busy | client/proxy timeout too low — raise to ≥ 30 min (see §2.1) |
| `409` | a run is already in progress; wait and retry |
| `404` | input rows not written yet, or `plan_id` mismatch/typo |
| `500` at stage `schedule` | read `message`, then `docker logs <container>` |
| Output tables empty after 200 | wrong `plan_id` in your SELECT — a 200 means rows were written |
| Timestamps look 5:30 h off | container missing `tzdata` (IST falls back to UTC) |
| Container won't boot: "Missing required config 'JKT_DB_HOST'" | DB env vars not passed — use `--env-file .env` |

Useful:
```bash
docker ps                          # is it up / healthy?
docker logs -f jkt-btp-planning    # live engine logs (per-day progress + KPIs)
docker exec -it jkt-btp-planning ls -la /app/output   # generated workbooks
```

The container logs print full engine progress and the final KPI block
(GT built / cured / coverage / changeovers) for every run — the fastest way to
see what a run actually did.
