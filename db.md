# Database map — MySQL `jkplanningV1`

Everything the B2C scheduler reads and writes. Full column-level contract for the
frontend is in `API.md`; deployment spec in `approach/deployment.md`.

---

## Per-run INPUT tables (written by the frontend before calling the API)

| Table | Contents |
|-------|----------|
| `jkt_demand` | one row per SKU: `plan_id`, `skuCode`, `requirement` (+ optional `skuDescription`). **No priority column needed** — the score is computed in code (min-max of `requirement`). |
| `jkt_plan_params` | one row per run (PK `plan_id`): `planStartDate`, `planEndDate`, `noOfChangeOver`, `efficiency` (**stored as a percentage, e.g. 94**), `plantName`, `productName`, `optimisationPreset`. Weightage/priority-flag columns exist but are **dormant in v1**. |
| `jkt_plan_presets` | reusable named parameter presets (PK `presetName`); used as fallback defaults. |

## Reference / master tables (read by the engine's ETL — not per-run)

| Table | Purpose |
|-------|---------|
| **`Daily_Running_Moulds`** | **Day-0 curing press state** — which SKU each press runs, mould life. **ALWAYS this table**, every month, local and cloud. The historical `testing_Daily_Running_Moulds` and `june_Daily_Running_Moulds` snapshots are **retired — do not use them.** |
| `Master_Curing_Design_CycleTime` | raw cure time per SKU (missing → default 17.0) |
| `Master_Curing_Allowable_Machines` / `_source` | SKU ↔ allowable curing press |
| `Master_Building_Allowable_Machines` | SKU ↔ allowable building machine (comma-separated `Machines`) |
| `Master_Building_ChangeoverTime`, `Master_Building_Machine_Design_cycleTime` | building CO times / design CTs |
| `Master_WC_Master`, `Master_Mapping_Mould_SKU` | work-centre and mould↔SKU mapping |
| `gt_inventory_manual`, `carcass_inventory_manual` | opening GT / carcass inventory |
| `jkt_sku_description` | SKU → description; fills output `skuDescription` when demand omits it |
| `Building_Stage1_Best_Machines`, `Building_Stage2_Best_Machines` | preferred machine lists |
| `TBMStage1_ProductionEventData`, `TBMStage2_ProductionEventData` | historical production events |

> Building cycle times live in **code** (`_BLD_CT_SEC` in `b2c_pipeline.py`), not in a table.

## OUTPUT tables (written by `connection.write_db`, all stamped `plan_id`, IST timestamps)

| Table | Rows per plan | Contents |
|-------|---------------|----------|
| `jkt_plan_building` | ~7k | building shift schedule |
| `jkt_plan_curing` | ~15k | curing shift schedule |
| `jkt_plan_Infeasibility` | few | **UNMET + missing-master + zero-production** SKUs only |
| `jkt_plan_kpis` | **1** (PK) | coverage, SKU counts, changeover counts, per-group utilisation |
| `jkt_plan_capacityUtilisation` | **1** (PK) | overall/monthly utilisation per machine group — **not per day** |

Re-running a `plan_id` deletes its existing rows and re-inserts (idempotent).

## Legacy / other-system tables — NOT used by this pipeline

`jkt_plan` (old curing-only planner's schedule output), `jkt_plan_history`,
`jkt_demand_history`, `jkt_sim_*` (simulation mode — **not implemented in v1**),
`jkt_plan_building_history`, `jkt_plan_curing_history` (frontend-owned archives).

> ⚠️ The **old planning system** (`/Users/anmolsaini/Documents/db_data_upload/`)
> also wrote `jkt_plan_kpis`, `jkt_plan_Infeasibility` and
> `jkt_plan_capacityUtilisation`. It is **retired**. If it is ever run it now
> fails on `jkt_plan_capacityUtilisation` (the `creatdBy` column was renamed to
> `createdBy`), but only *after* writing partial rows to the other tables.
> Sanity check: our pipeline writes exactly **1** row per plan to
> `jkt_plan_capacityUtilisation`; the old system wrote **30** (one per day).
