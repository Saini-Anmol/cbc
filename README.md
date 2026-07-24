# CBC Scheduler — Curing → Building → Curing

> **CURRENT STATE (2026-07-24).** The live engine now models several plant rules (all in
> `b2c_pipeline.py`; see `CLAUDE.md` §"Rules & features added this cycle" for the authoritative
> detail): **mould→SKU availability gate + retarget + unified CO scorer + mould life v2**
> (a curing press needs 2 eligible moulds; opening mould life read from the DB); **building
> inch rules** (5-day minimum inch dwell + ±2 band + idle-recovery Levers B/C, replacing the
> old one-way rule); **max 4 distinct SKUs / building machine / day**; and an optional
> **+3/−3 inch escape** (default OFF). Current LOCAL KPIs (cap=12, all rules live):
> May 682,260/98.34% · June 634,038/96.56% · July 681,429/87.48%.

A feed-aware production scheduler for the **JK Tyre BTP PCR line**. It couples
the two existing stage schedulers — **curing** (press allocation) and
**building** (green-tyre manufacture) — so that curing only commits SKUs its
feeding building machines can actually supply. The result: curing presses no
longer **starve** for green tyres.

> For the full design rationale, data contracts, and per-module internals, see
> **[ARCHITECTURE_DETAILED.md](ARCHITECTURE_DETAILED.md)**. This README is the
> operational quick-start.

---

## The problem it solves

Tyre production is two linked stages:

```
        BUILDING                         CURING
   (makes the green tyre)          (cures the green tyre)
   Stage1 carcass ─► Stage2/        presses consume GT
   Unistage green tyre (GT)   ──►   produced upstream
```

Historically, curing planned against **curing capacity only**. It assigned
presses to SKUs whose feeding building machines couldn't supply them, so presses
ran dry — **starvation**. CBC inserts a **feed-awareness** layer: curing only
keeps a (press, SKU) pair if some building machine that feeds that press can
build that SKU.

---

## Pipeline overview

CBC runs three phases (plus a one-time, periodic Phase 0):

| Phase | Module | What it does |
|-------|--------|--------------|
| **0 — Feed map** | `feed_map_builder.py` | Learns the *press → feeder-machine* map from production history. Stable, rebuilt only when the line layout changes. |
| **C — Curing (feed-aware)** | `curing_lp.py` + `feed_aware_curing.py` | Linear-program press allocation over a 31-day horizon, with the feed filter installed so infeasible (press, SKU) pairs are barred. → `PCR_CBC_Curing_Initial.xlsx` |
| **B — Building** | `building.py` | Hybrid GA + LP, day-by-day, builds the green tyres the curing plan needs. → `PCR_CBC_Building.xlsx` |
| **C2 — Final curing** | `curing_lp.py` | Re-runs curing **capped by building's actual GT supply**, giving the achievable plan. → `PCR_CBC_Curing_Final.xlsx` |

`cbc.py` orchestrates all of it. The handoff between C and B is the bridge file
`curing_plan_for_building.xlsx`, which `cbc.py` writes automatically.

```
 feed_map.json ─┐
                ▼
  Book4.xlsx ─► [C] curing_lp + feed filter ─► curing_plan_for_building.xlsx
                                                        │
                                                        ▼
                                              [B] building.py (GA+LP)
                                                        │  GT supply
                                                        ▼
                                              [C2] curing_lp (capped) ─► Final plan
```

---

## Repository layout

```
cbc/
├── cbc.py                  # ORCHESTRATOR — entry point (python cbc.py)
├── cbc_env.py              # DB config + data paths, reads .env
├── curing_lp.py            # Curing scheduler (LP, scipy HiGHS)
├── building.py             # Building scheduler (GA + LP, day-by-day rolling)
├── feed_map_builder.py     # Phase 0 — learns press→feeder map from history
├── feed_aware_curing.py    # Monkeypatch that installs the feed filter into curing
├── map_events.py           # Helper to map raw production events → recipe codes
├── data_fetch.py           # Pulls raw curing-PCR events from the MES export API
├── ARCHITECTURE_DETAILED.md
├── data/
│   ├── input/              # (gitignored) inputs you drop in — Book4.xlsx, masters, events
│   └── output/             # (gitignored) generated schedules, feed map, logs
│       └── main_output/    # the three final schedules
└── *.xlsx                  # small reference/loader workbooks
```

`data/input/`, `data/output/`, and `.env` are **gitignored** — the large CSVs
(the production-event log is ~2.4 GB) and DB credentials never go into git.

---

## Requirements

- **Python 3.14** (project venv is `myenv/`)
- Python packages: `pandas`, `numpy`, `scipy`, `openpyxl`, `sqlalchemy`,
  `pymysql`, `requests`
- Network access to the planning database (MySQL `jkplanningV1`)

### Setup

```bash
# from the project root
python3 -m venv myenv
source myenv/bin/activate
pip install pandas numpy scipy openpyxl sqlalchemy pymysql requests
```

`cbc.py` auto-re-execs under `myenv/` if launched with a different interpreter,
so `python3 cbc.py` works even outside the activated venv.

### Configuration — `.env`

Create a `.env` file in the project root (it is gitignored):

```dotenv
JKT_DB_HOST=<host>
JKT_DB_PORT=3306
JKT_DB_USER=<user>
JKT_DB_PASSWORD=<password>
JKT_DB_DATABASE=jkplanningV1
```

`cbc_env.py` reads these; process environment variables override the file.

---

## Data inputs

Drop these into `data/input/`. Most reference data comes from the DB at run
time; the files below are what you supply:

| File | Used by | Purpose |
|------|---------|---------|
| `Book4.xlsx` | curing | Curing demand + priority per SKU |
| `building_allowable.xlsx` | feed-aware curing | SKU → eligible building machines (feasibility) |
| `CURING_PCR 1.csv` / curing events | feed map | Which press cured which recipe (history) |
| `productionevent_data.csv` | feed map | Which machine built which recipe (history, ~2.4 GB) |
| `RECIPE_MASTER.csv`, `WCMASTER.csv` | feed map | recipe-id↔code and wcID↔press bridges |

See [ARCHITECTURE_DETAILED.md §3](ARCHITECTURE_DETAILED.md) for the full table
of DB tables, columns, and file contracts.

---

## Running

| Goal | Command | Cadence |
|------|---------|---------|
| **Full CBC run** (C → B → C2) | `python cbc.py` | each planning cycle |
| Final curing only (re-cap against existing GT supply) | `python cbc.py final` | ad-hoc |
| Rebuild the feed map | `python feed_map_builder.py` | periodic (monthly / on layout change) |
| Refresh raw curing events from MES | `python data_fetch.py` | as needed |

Reuse an existing feed map (the default) by keeping `REBUILD_FEED_MAP=False`.

### Outputs (`data/output/main_output/`)

| File | Contents |
|------|----------|
| `PCR_CBC_Curing_Initial.xlsx` | Feed-aware curing plan (Phase C) |
| `PCR_CBC_Building.xlsx` | Building schedule (Phase B) |
| `PCR_CBC_Curing_Final.xlsx` | Final curing plan capped by GT supply (Phase C2) |

Each curing workbook includes Demand Fulfilment, Machine/Shift Schedule,
Utilisation, Mould Tracker, and a Daily Total Production sheet. Intermediate
files (`feed_map.json`, `curing_plan_for_building.xlsx`, logs) stay in
`data/output/`.

---

## Configuration knobs (`CBCConfig` in `cbc.py`)

| Field | Default | Meaning |
|-------|---------|---------|
| `REBUILD_FEED_MAP` | `False` | Rebuild the feed map from history vs. reuse `feed_map.json` |
| `PLAN_START` | `2026-05-01 07:00` | First shift of the horizon |
| `BUILDING_PLANNING_DAYS` | `31` | Building horizon (aligned to curing's 31 days) |
| `RUN_BUILDING` | `True` | Run Phase B |
| `RUN_FINAL_CURING` | `True` | Run Phase C2 (cap curing by GT supply) |
| `KEEP_UNKNOWN_PRESS` / `KEEP_UNKNOWN_SKU` | `True` | Permissive on missing feed data — keep the option rather than idle a press. Set `False` for strict mode once the feed map is trusted. |

---

## How feed-awareness works

`install_feed_awareness(curing_lp, feed_map, building_allowable)` **wraps**
curing's eligibility method (monkeypatch — no edits to `curing_lp.py`). A press
`P` is kept for SKU `S` only if:

```
P is a continuity press for this run         (already running → feed exists)
  OR  FEED[P] ∩ building_allowable[S] ≠ ∅     (a feeder can build it)
```

Infeasible pairs get a zero eligibility bound, so the LP cannot place them.
Continuity presses are always exempt and never stripped to empty. For an A/B
starvation comparison, `uninstall_feed_awareness(curing_lp)` reverts to the
original eligibility.

---

## Notes & caveats

- **Code alignment matters.** Press codes (`4401…`) must match across the WC
  master, feed map, and building-allowable; mismatches silently drop join rows.
  `feed_coverage.xlsx` surfaces the impact (aim for >80% coverage per press).
- **Data freshness.** Running-machine and inventory snapshots must be current at
  run time — both schedulers roll state forward from them.
- **What CBC fixes:** structural starvation (a press's feeders can't build the
  SKU). **What it doesn't yet:** fine shift-level timing of shared-feeder bursts.
- **Secrets:** `.env`, `data_fetch.py` (API key), and `cbc_env.py` (fallback DB
  values) contain credentials. Keep `.env` gitignored and avoid committing real
  keys; rotate any that have been shared.
</content>
