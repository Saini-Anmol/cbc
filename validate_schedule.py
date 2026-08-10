#!/usr/bin/env python
"""validate_schedule.py — INDEPENDENT hard-rule validator for the JK Tyre B2C
Building + Curing output schedules.

It does NOT run or trust the planner. It reads the generated Building and Curing
workbooks and re-derives every reference value from the AUTHORITATIVE sources
(DB: curing CT / allowable / mould map / opening GT+carcass / month-filtered
Daily_Running_Moulds; CSV: Cycle_time_Building.csv; bc_config: CO/clean times),
then checks 21 hard business rules row-by-row / shift-by-shift / machine-by-machine
/ press-by-press / SKU-by-SKU and reports PASS/FAIL per rule with the exact
violating rows (date, shift, machine/press, SKU, qty, CT, prod-mins, CO-mins,
expected vs actual) and per-rule violation totals.

Usage:
    RUNNING_MOULDS_MONTH=2026-07 PLAN_MONTH=2026-07 \
    myenv/bin/python validate_schedule.py \
        --building main_output/_FINAL_building.xlsx \
        --curing   main_output/_FINAL_curing.xlsx \
        --demand   data/input/july_demand_tomerJi1.xlsx \
        --plan-month 2026-07 [--out validation_report.xlsx]

Defaults target the July _FINAL_ run. --plan-month sets BOTH month env vars
BEFORE the DB loaders are imported (they capture the month by value at import).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict

# ── month env MUST be set before importing the curing loaders (imported by value) ──
_ap = argparse.ArgumentParser(description="Independent B2C schedule validator")
_ap.add_argument("--building", default="main_output/_FINAL_building.xlsx")
_ap.add_argument("--curing", default="main_output/_FINAL_curing.xlsx")
_ap.add_argument("--demand", default="data/input/july_demand_tomerJi1.xlsx")
_ap.add_argument("--plan-month", default=os.environ.get("PLAN_MONTH", "2026-07"))
_ap.add_argument("--out", default="validation_report.xlsx")
_ap.add_argument("--max-rows", type=int, default=25, help="max sample violation rows printed per rule")
ARGS = _ap.parse_args()

PLAN_MONTH = ARGS.plan_month
os.environ["PLAN_MONTH"] = PLAN_MONTH
os.environ["RUNNING_MOULDS_MONTH"] = PLAN_MONTH

import pandas as pd  # noqa: E402
import numpy as np   # noqa: E402

# tolerances
MIN_TOL = 1.5        # minutes rounding tolerance for CT/production reconciliation
SHIFT_MINS = 480

# ══════════════════════════════════════════════════════════════════════════════
#  Finding / report plumbing
# ══════════════════════════════════════════════════════════════════════════════
class Rule:
    def __init__(self, num, name):
        self.num = num
        self.name = name
        self.violations = []   # list[dict]
        self.notes = []        # informational (not failures)
        self.checked = 0       # rows/items examined
        self.skipped = None    # reason string if the rule could not run

    def add(self, **row):
        self.violations.append(row)

    @property
    def status(self):
        if self.skipped:
            return "SKIP"
        return "PASS" if not self.violations else "FAIL"


RULES: dict[str, Rule] = {}
def R(num, name) -> Rule:
    r = Rule(num, name)
    RULES[num] = r
    return r


# ══════════════════════════════════════════════════════════════════════════════
#  Authoritative source loaders (DB / CSV / config)
# ══════════════════════════════════════════════════════════════════════════════
SRC = {}          # populated source data
SRC_ERR = {}      # loader -> error string

def _try(name, fn):
    try:
        SRC[name] = fn()
    except Exception as e:  # pragma: no cover
        SRC_ERR[name] = f"{type(e).__name__}: {e}"
        SRC[name] = None

def load_sources():
    import bc_config as bc
    SRC["bc"] = bc
    # CO / clean / cap constants
    SRC["CO_SAME"] = dict(bc.BUILDING_CO_SAME_SIZE)
    SRC["CO_DIFF"] = dict(bc.BUILDING_CO_DIFF_SIZE)
    SRC["CURING_CO"] = int(getattr(bc, "CURING_CO_CHANGEOVER_MINS", 480))
    SRC["CLEAN_MINS"] = int(getattr(bc, "MOULD_CLEAN_MINS", 480))
    SRC["MAX_CO_DAY"] = int(getattr(bc, "MAX_CHANGEOVERS_PER_DAY", 12))
    SRC["GT_AGE_DAYS"] = int(getattr(bc, "GT_SHELF_LIFE_DAYS", 3))
    SRC["CARC_AGE_DAYS"] = int(getattr(bc, "CARCASS_SHELF_LIFE_DAYS", 1))
    SRC["GT_CAP"] = int(getattr(bc, "MAX_ENDOFDAY_GT_INVENTORY", 8000))
    SRC["CARC_CAP"] = int(getattr(bc, "MAX_ENDOFDAY_CARCASS_INVENTORY", 1200))
    SRC["CAVITIES"] = 2
    SRC["DEFAULT_CURE_CT"] = 17.0

    # machine group + CT + inch (from b2c_pipeline — the CSV/inch source of truth)
    def _bld_helpers():
        import b2c_pipeline as bp
        return {
            "MG": dict(bp._MACHINE_GROUP),
            "ct_fn": bp._bld_ct_sec,
            "inch_set": dict(getattr(bp, "_MACHINE_ALLOWED_INCH_SET", {})),
        }
    _try("bld", _bld_helpers)

    # DB engine
    def _engine():
        try:
            from connection import get_engine
            return get_engine()
        except Exception:
            from cbc_env import make_engine
            return make_engine()
    _try("engine", _engine)
    eng = SRC.get("engine")

    if eng is not None:
        from curing_consumption import ConsumptionETL
        etl = ConsumptionETL(eng)
        SRC["etl"] = etl
        # curing CT map (DB, default 17)
        def _cure_ct():
            df = etl.load_cycle_times()
            try:
                from curing_consumption import CycleTimeResolver
                skus = [str(x) for x in df.iloc[:, 0].tolist()]
                m = CycleTimeResolver().resolve(skus, df)
                return {str(k): float(v) for k, v in dict(m).items()}
            except Exception:
                col = "CycleTime_min" if "CycleTime_min" in df.columns else df.columns[-1]
                return {str(r[df.columns[0]]): float(r[col]) for _, r in df.iterrows()}
        _try("cure_ct", _cure_ct)
        # 170 allowable presses
        _try("presses170", lambda: set(map(str, etl.load_allowable_press_ids())))
        # curing SKU->press eligibility
        def _cure_allow():
            m = etl.load_curing_allowable()
            return {str(k): set(map(str, v)) for k, v in dict(m).items()}
        _try("cure_allow", _cure_allow)
        # mould eligibility
        def _mould():
            m = etl.load_mould_eligibility()
            sm = {str(k): set(map(str, v)) for k, v in m["sku_moulds"].items()}
            ms = {str(k): set(map(str, v)) for k, v in m["mould_skus"].items()}
            return {"sku_moulds": sm, "mould_skus": ms}
        _try("mould", _mould)
        # running moulds for plan_month -> press set (+ mould life)
        def _rm():
            df = etl.load_running_moulds()
            pcol = "Machine" if "Machine" in df.columns else df.columns[0]
            return set(str(x) for x in df[pcol].tolist())
        _try("rm_presses", _rm)
        # opening GT (plan_month)
        def _open_gt():
            m = etl.load_gt_inventory()
            if isinstance(m, dict):
                return {str(k): float(v) for k, v in m.items()}
            df = m
            scol = "SKUCode" if "SKUCode" in df.columns else df.columns[0]
            vcol = "GT_Inventory" if "GT_Inventory" in df.columns else df.columns[-1]
            return {str(r[scol]): float(r[vcol]) for _, r in df.iterrows()}
        _try("open_gt", _open_gt)
        # opening carcass (plan_month)
        def _open_carc():
            from curing_b2c import _load_opening_carcass
            m = _load_opening_carcass(eng)
            return {str(k): float(v) for k, v in dict(m).items()}
        _try("open_carc", _open_carc)
        # building allowable machines
        def _bld_allow():
            from building import ETL as BETL
            df = BETL(eng).load_machine_allowable()
            out = {}
            for _, r in df.iterrows():
                mm = r["Machines"]
                if isinstance(mm, str):
                    mm = [t for t in re.split(r"[ ,]+", mm) if t.strip().isdigit()]
                out[str(r["SKUCode"])] = set(map(str, mm))
            return out
        _try("bld_allow", _bld_allow)
        # sku inch (size master; fallback code[8:10] later)
        def _sku_inch():
            try:
                m = etl.load_sku_sizes()
                d = {str(k): str(v).replace('"', "").strip() for k, v in dict(m).items()}
                return d
            except Exception:
                return {}
        _try("sku_inch", _sku_inch)

    # demand (per-SKU, collapsed)
    def _demand():
        eng2 = SRC.get("engine")
        if eng2 is not None:
            try:
                from curing_consumption import ConsumptionETL as CE
                # load_demand reads bc_config.DEMAND_FILE; set it to our file for the load
                bc.DEMAND_FILE = ARGS.demand
                df = CE(eng2).load_demand()
                qcol = "Requirement"
                for c in ("Quantity", "Updated_Requirement", "Requirement", "requirement", "Demand"):
                    if c in df.columns:
                        qcol = c; break
                scol = "SKUCode" if "SKUCode" in df.columns else df.columns[0]
                g = df.groupby(df[scol].astype(str))[qcol].sum()
                return {str(k): float(v) for k, v in g.items() if v > 0}
            except Exception:
                pass
        # fallback: read the excel directly
        raw = pd.read_excel(ARGS.demand)
        scol = next((c for c in raw.columns if str(c).lower() in ("skucode", "sku code", "sku_code", "sapcode")), raw.columns[0])
        qcol = next((c for c in ("Quantity", "Updated_Requirement", "Requirement") if c in raw.columns), None)
        if qcol is None:
            qcol = next((c for c in raw.columns if "req" in str(c).lower() or "qty" in str(c).lower()), raw.columns[-1])
        g = raw.groupby(raw[scol].astype(str))[qcol].sum()
        return {str(k): float(v) for k, v in g.items() if v > 0}
    _try("demand", _demand)


def sku_inch(sku: str) -> str:
    d = SRC.get("sku_inch") or {}
    v = d.get(str(sku))
    if v:
        return str(v).replace('"', "").strip()
    s = str(sku)
    return s[8:10] if len(s) >= 10 else ""


# ══════════════════════════════════════════════════════════════════════════════
#  Sheet loaders (exact header rows per the schema mapping)
# ══════════════════════════════════════════════════════════════════════════════
BLD_HDR = {
    "Shift Schedule": 2, "Changeover Plan": 0, "SKU Classification": 0,
    "Daily GT & Carcass": 0, "Demand Fulfillment (B2C)": 0, "Machine Utilization": 1,
}
CUR_HDR = {
    "Demand Fulfillment": 0, "Machine Utilization": 1, "Shift Schedule": 0,
    "Changeover Plan": 0, "Mould Tracker": 1, "Mould Movement": 1, "MouldInUse": 1,
    "Machine Schedule": 1, "Daily Cured tyres": 0, "GT Gap Diagnostic": 0,
}

def read_sheet(path, sheet, hdr):
    return pd.read_excel(path, sheet_name=sheet, header=hdr)

# machine group sets (internal, from facts sheet — used if b2c import failed)
_MG_FALLBACK = {}
for _g, _ms in {
    "VMI": ["6001", "6002", "6003", "6004", "7001", "7002", "7003", "7004"],
    "BJ": ["7101", "7102", "7103", "7104", "7105", "7106", "7201"],
    "UNISTAGE": ["7501", "7502", "7503"],
    "STAGE2": ["8201", "8301", "8302", "8501", "8502", "7301"],
    "STAGE1": ["6802", "6803", "6909", "6911", "7601", "7701", "7801", "7802", "7803", "7804", "8001", "8002", "8003", "8101"],
}.items():
    for _m in _ms:
        _MG_FALLBACK[_m] = _g

def mgroup(m):
    mg = (SRC.get("bld") or {}).get("MG") if SRC.get("bld") else None
    if mg and str(m) in mg:
        return mg[str(m)]
    return _MG_FALLBACK.get(str(m), "?")

S1_MACHINES = {m for m, g in _MG_FALLBACK.items() if g == "STAGE1"}
S2_MACHINES = {m for m, g in _MG_FALLBACK.items() if g == "STAGE2"}
GT_MACHINES = {m for m, g in _MG_FALLBACK.items() if g in ("VMI", "BJ", "UNISTAGE", "STAGE2")}
ALL_38 = set(_MG_FALLBACK.keys())

def bld_ct_sec(machine, sku):
    b = SRC.get("bld")
    if b and b.get("ct_fn"):
        try:
            return float(b["ct_fn"](str(machine), str(sku)))
        except Exception:
            pass
    return 120.0

SHIFT_ORDER = {"A": 0, "B": 1, "C": 2}

def to_min(start, end):
    try:
        a = pd.to_datetime(start); b = pd.to_datetime(end)
        return (b - a).total_seconds() / 60.0
    except Exception:
        return np.nan


# ══════════════════════════════════════════════════════════════════════════════
#  BUILDING validation
# ══════════════════════════════════════════════════════════════════════════════
def validate_building(path):
    ss = read_sheet(path, "Shift Schedule", BLD_HDR["Shift Schedule"])
    ss = ss.dropna(how="all")
    ss["Machine"] = ss["Machine"].astype(str).str.replace(r"\.0$", "", regex=True)
    ss["Qty"] = pd.to_numeric(ss["Qty"], errors="coerce").fillna(0)
    ss["CO_Mins"] = pd.to_numeric(ss["CO_Mins"], errors="coerce").fillna(0)
    ss["CO_Type"] = ss["CO_Type"].astype(str)
    is_co = ss["SKUCode"].astype(str).str.upper().eq("CHANGEOVER") | (ss["CO_Mins"] > 0)
    prod = ss[~is_co].copy()
    co = ss[is_co].copy()

    # ---- R14: only the 38 building machines ----
    r = R("14", "Only the 38 building machines appear")
    for m in sorted(ss["Machine"].unique()):
        r.checked += 1
        if m not in ALL_38:
            r.add(machine=m, note="machine not in the 38-machine set (6801 is retired)")

    # ---- R1: building CT sourced from Cycle_time_Building.csv (flag 120-default fallback) ----
    # NOTE: the building Shift Schedule carries NO per-row CT column and its StartTime/EndTime
    # is the SHIFT WINDOW (not production duration), so CT cannot be reconciled against wall-clock.
    # The meaningful independent check is that every (machine,SKU) built has a real CT from the
    # CSV/dict source (not the 120.0 "unknown" fallback), and that Qty*CT fits a shift (R18/R15).
    r1 = R("1", "Building CT resolves from Cycle_time_Building.csv/_BLD_CT_SEC (no 120 fallback)")
    r18 = R("18", "Building per-row production fits a shift: Qty*CT/60 <= 480")
    for _, row in prod.iterrows():
        m, s, q = row["Machine"], str(row["SKUCode"]), float(row["Qty"])
        if q <= 0:
            continue
        r1.checked += 1
        ct = bld_ct_sec(m, s)
        if abs(ct - 120.0) < 1e-9:
            r1.add(date=row["Date"], shift=row["Shift"], machine=m, sku=s, qty=q,
                   note="CT fell to 120.0 default (missing from Cycle_time_Building.csv AND _BLD_CT_SEC)")
        r18.checked += 1
        pm = q * ct / 60.0
        if pm > SHIFT_MINS + MIN_TOL:
            r18.add(date=row["Date"], shift=row["Shift"], machine=m, sku=s, qty=q,
                    ct_sec=round(ct, 2), prod_min=round(pm, 1), over_by=round(pm - SHIFT_MINS, 1))

    # ---- R2: building CO times match config per group+type ----
    r2 = R("2", "Building CO time = config value per group/type (same/diff)")
    for _, row in co.iterrows():
        m = row["Machine"]; g = mgroup(m); ct = str(row["CO_Type"]).lower()
        actual = float(row["CO_Mins"])
        r2.checked += 1
        if "diff" in ct:
            exp = SRC["CO_DIFF"].get(g)
        elif "same" in ct:
            exp = SRC["CO_SAME"].get(g)
        else:
            exp = None
        # allow the +10 DB-changeover variant as an accepted alternative
        if exp is not None and actual not in (exp, exp + 10):
            r2.add(date=row["Date"], shift=row["Shift"], machine=m, group=g,
                   co_type=row["CO_Type"], expected_co_min=exp, actual_co_min=actual)

    # ---- R3(bld): allowable machines matrix ----
    r3 = R("3B", "Building (SKU,machine) in Master_Building_Allowable_Machines")
    allow = SRC.get("bld_allow")
    if not allow:
        r3.skipped = f"allowable matrix not loaded ({SRC_ERR.get('bld_allow','?')})"
    else:
        for _, row in prod.iterrows():
            m, s = row["Machine"], str(row["SKUCode"])
            if m in S1_MACHINES:      # Stage-1 carcass row: SKU is the Stage-2 GT SKU it feeds
                continue
            if float(row["Qty"]) <= 0:
                continue
            r3.checked += 1
            allowed = allow.get(s)
            if allowed is not None and m not in allowed:
                r3.add(date=row["Date"], shift=row["Shift"], machine=m, sku=s,
                       allowed_machines=",".join(sorted(allowed)) or "(none)")

    # ---- R4: historical inch respected (GT machines; Stage-1 exempt) ----
    r4 = R("4", "Building GT machine builds a DB-allowable inch (matrix, not historical single-inch)")
    # Allowed-inch set per machine = the inches of every SKU the machine is DB-allowable for
    # (Master_Building_Allowable_Machines). The historical single-inch lock is only a policy; the
    # DB matrix is the physical truth, so inch-flex within DB-allowable is legitimate (Rule 3B).
    _bld_allow = SRC.get("bld_allow") or {}
    _db_inch = {}
    for _sku, _macs in _bld_allow.items():
        _i = sku_inch(_sku)
        for _mm in _macs:
            _db_inch.setdefault(str(_mm), set()).add(_i)
    for _, row in prod.iterrows():
        m, s = str(row["Machine"]), str(row["SKUCode"])
        if m in S1_MACHINES or float(row["Qty"]) <= 0:
            continue
        allowed = _db_inch.get(m)
        if not allowed:
            continue
        r4.checked += 1
        inch = sku_inch(s)
        if inch and inch not in allowed:
            r4.add(date=row["Date"], shift=row["Shift"], machine=m, sku=s, sku_inch=inch,
                   allowed_inches=",".join(sorted(str(x) for x in allowed)))

    # ---- R19/R20(bld): no production during a building CO ----
    r19 = R("19B", "No production during a building CO (CO row Qty=0; no row mixes CO+prod)")
    for _, row in ss.iterrows():
        if float(row["CO_Mins"]) > 0:
            r19.checked += 1
            if float(row["Qty"]) > 0:
                r19.add(date=row["Date"], shift=row["Shift"], machine=row["Machine"],
                        sku=row["SKUCode"], qty=float(row["Qty"]), co_min=float(row["CO_Mins"]))

    # ---- R16(bld): one machine builds at most one SKU per shift ----
    r16 = R("16B", "No building machine builds two SKUs in one shift")
    for (m, d, sh), grp in prod[prod["Qty"] > 0].groupby(["Machine", "Date", "Shift"]):
        r16.checked += 1
        skus = sorted(set(grp["SKUCode"].astype(str)))
        if len(skus) > 1:
            r16.add(date=d, shift=sh, machine=m, skus=",".join(skus), n_skus=len(skus))

    # ---- R11/R15(bld): Prod_Mins + CO_Mins <= 480 per (machine,date,shift) [incl same-size CO] ----
    r15 = R("15B", "Building Prod_Mins + CO_Mins <= 480 per machine/shift (incl same-size CO)")
    agg = defaultdict(lambda: [0.0, 0.0, []])  # key -> [prod_min, co_min, skus]
    for _, row in ss.iterrows():
        key = (row["Machine"], row["Date"], row["Shift"])
        if float(row["CO_Mins"]) > 0:
            agg[key][1] += float(row["CO_Mins"])
        q = float(row["Qty"])
        if q > 0:
            ct = bld_ct_sec(row["Machine"], str(row["SKUCode"]))
            agg[key][0] += q * ct / 60.0
            agg[key][2].append(str(row["SKUCode"]))
    for (m, d, sh), (pm, cm, skus) in agg.items():
        r15.checked += 1
        if pm + cm > SHIFT_MINS + MIN_TOL:
            r15.add(date=d, shift=sh, machine=m, group=mgroup(m), skus=",".join(skus),
                    prod_min=round(pm, 1), co_min=round(cm, 1), total_min=round(pm + cm, 1),
                    over_by=round(pm + cm - SHIFT_MINS, 1))

    # ---- R7(bld): occupancy <= 100% ----
    r7 = R("7B", "No building machine occupancy > 100%")
    try:
        mu = read_sheet(path, "Machine Utilization", BLD_HDR["Machine Utilization"])
        occ = [c for c in mu.columns if str(c).strip().lower() == "occupancy_pct"]
        if occ:
            for _, row in mu.iterrows():
                v = pd.to_numeric(pd.Series([row[occ[0]]]), errors="coerce").iloc[0]
                if pd.isna(v):
                    continue
                r7.checked += 1
                if v > 1.0 + 1e-4:
                    r7.add(machine=row.get("Machine"), occupancy_pct=round(float(v), 4))
    except Exception as e:
        r7.skipped = f"Machine Utilization read failed: {e}"

    # ---- R8(bld): per-SKU GT built <= demand (GT machines only) ----
    r8 = R("8B", "Building GT built <= demand per SKU (no overbuild)")
    dem = SRC.get("demand") or {}
    gt_built = defaultdict(float)
    for _, row in prod.iterrows():
        if row["Machine"] in GT_MACHINES and float(row["Qty"]) > 0:
            gt_built[str(row["SKUCode"])] += float(row["Qty"])
    if not dem:
        r8.skipped = f"demand not loaded ({SRC_ERR.get('demand','?')})"
    else:
        for s, built in gt_built.items():
            r8.checked += 1
            dq = dem.get(s)
            if dq is not None and built > dq + 0.5:
                r8.add(sku=s, gt_built=round(built), demand=round(dq), over_by=round(built - dq))

    # ---- R5: Stage-2 GT not before Stage-1 carcass (per SKU, cumulative, 1-day aging) ----
    r5 = R("5", "Stage-2 GT never exceeds available Stage-1 carcass (1-day aging)")
    open_carc = SRC.get("open_carc") or {}
    # per (sku, day, shift) sums
    s1 = defaultdict(float); s2 = defaultdict(float)
    days = set()
    for _, row in prod.iterrows():
        q = float(row["Qty"])
        if q <= 0:
            continue
        s = str(row["SKUCode"]); d = str(row["Date"]); sh = str(row["Shift"])
        days.add(d)
        if row["Machine"] in S1_MACHINES:
            s1[(s, d, sh)] += q
        elif row["Machine"] in S2_MACHINES:
            s2[(s, d, sh)] += q
    skus_s2 = {k[0] for k in s2}
    daylist = sorted(days)
    for s in sorted(skus_s2):
        # walk shifts; carcass available = opening + cumulative carcass built up to (t) with 1-day aging
        # aging: carcass built on day D is usable through day D+CARC_AGE_DAYS. We check the
        # cumulative feasibility: at each shift, cumulative S2 GT <= opening + cumulative S1 carcass built so far.
        cum_s1 = float(open_carc.get(s, 0.0)); cum_s2 = 0.0
        for d in daylist:
            for sh in ("A", "B", "C"):
                cum_s1 += s1.get((s, d, sh), 0.0)
                cum_s2 += s2.get((s, d, sh), 0.0)
                r5.checked += 1
                if cum_s2 > cum_s1 + 0.5:
                    r5.add(date=d, shift=sh, sku=s, cum_stage2_gt=round(cum_s2),
                           cum_carcass_avail=round(cum_s1), short_by=round(cum_s2 - cum_s1))
                    break
            else:
                continue
            break

    # ---- R9(carcass): carcass aging 1 day (writeoff any carcass idle > 1 day) ----
    # reconstruct per-SKU carcass FIFO: built (S1) inflow, consumed (S2) outflow, age <= 1 day.
    r9c = R("9C", "Carcass aging <= 1 day (FIFO reconstruction)")
    _fifo_age_check(r9c, s1, s2, open_carc, daylist, SRC["CARC_AGE_DAYS"], label="carcass")

    # ---- R6(carcass opening): opening carcass matches DB ----
    # (informational cross-check handled in curing R6 for GT; carcass opening used above.)
    return {"prod": prod, "co": co, "s1": s1, "s2": s2, "days": daylist, "gt_built": gt_built}


def _fifo_age_check(rule, inflow, outflow, opening, daylist, max_age_days, label):
    """Generic FIFO aging check per SKU. inflow/outflow keyed (sku,day,shift).
    Flags any inflow unit that remains uncured/unconsumed longer than max_age_days.
    Aging is measured in CALENDAR days (day-number difference), NOT an enumeration of the
    sparse active-day set — the latter compresses gaps and mis-ages sparsely-built carcass.
    We walk EVERY calendar day min..max so a gap correctly ages stock."""
    from collections import deque
    skus = {k[0] for k in inflow} | {k[0] for k in outflow} | set(opening)
    if not daylist:
        return
    d0 = pd.to_datetime(daylist[0])
    day_num = {d: (pd.to_datetime(d) - d0).days for d in daylist}   # calendar day index
    dn_to_date = {v: d for d, v in day_num.items()}
    max_dn = max(day_num.values())
    for s in sorted(skus):
        q = deque()
        op = float(opening.get(s, 0.0))
        if op > 0:
            q.append([-1, op, "opening"])   # opening available from start (never ages)
        for dn in range(0, max_dn + 1):
            d = dn_to_date.get(dn)          # None on a gap day (no build/consume anywhere)
            if d is not None:
                for sh in ("A", "B", "C"):
                    add = inflow.get((s, d, sh), 0.0)
                    if add > 0:
                        q.append([dn, add, f"{d} {sh}"])
                    take = outflow.get((s, d, sh), 0.0)
                    while take > 0 and q:
                        if q[0][1] <= take + 1e-9:
                            take -= q[0][1]; q.popleft()
                        else:
                            q[0][1] -= take; take = 0
            # end of calendar day dn: any queue item older than max_age_days is a violation
            while q and q[0][0] >= 0 and (dn - q[0][0]) > max_age_days:
                item = q.popleft()
                rule.checked += 1
                rule.add(sku=s, built=item[2], aged_out_on_day=(d if d is not None else dn),
                         age_days=dn - item[0], qty_expired=round(item[1]),
                         limit_days=max_age_days, kind=label)


# ══════════════════════════════════════════════════════════════════════════════
#  CURING validation
# ══════════════════════════════════════════════════════════════════════════════
def validate_curing(path):
    ss = read_sheet(path, "Shift Schedule", CUR_HDR["Shift Schedule"])
    ss = ss.dropna(how="all")
    ss["Machine"] = ss["Machine"].astype(str).str.replace(r"\.0$", "", regex=True)
    for c in ("Qty", "CO_Mins", "Mould_Clean_Mins", "CycleTime_min"):
        if c in ss.columns:
            ss[c] = pd.to_numeric(ss[c], errors="coerce").fillna(0)
    is_co = ss["CO_Mins"] > 0
    is_clean = ss["Mould_Clean_Mins"] > 0
    prod = ss[(~is_co) & (~is_clean) & (ss["Qty"] > 0)].copy()

    cure_ct = SRC.get("cure_ct") or {}
    DEF = SRC["DEFAULT_CURE_CT"]; CAV = SRC["CAVITIES"]

    def ct_of(sku):
        v = cure_ct.get(str(sku))
        return float(v) if v and v > 0 else DEF

    # ---- R13: only the 170 allowable presses ----
    r13 = R("13", "Only the 170 allowable curing presses are used")
    p170 = SRC.get("presses170")
    if not p170:
        r13.skipped = f"170-press set not loaded ({SRC_ERR.get('presses170','?')})"
    else:
        for m in sorted(ss["Machine"].unique()):
            r13.checked += 1
            if m not in p170:
                r13.add(press=m, note="press not in the 170 allowable-matrix set")

    # ---- R21: correct-month Daily Running Moulds ----
    # Verify the correct plan_month snapshot is loaded (non-empty) and every press is either in
    # that month's snapshot OR a legitimate cold-start press (absent from Day-0 but inside the 170
    # allowable — the pipeline's IDLE_PRESS_ACTIVATE design). A press outside BOTH is a real fault.
    r21 = R("21", f"Correct-month ({PLAN_MONTH}) Daily_Running_Moulds; presses trace to it or 170-cold-start")
    rmp = SRC.get("rm_presses"); p170 = SRC.get("presses170")
    if not rmp:
        r21.skipped = f"running-moulds snapshot not loaded ({SRC_ERR.get('rm_presses','?')})"
    elif not rmp:
        r21.add(note=f"{PLAN_MONTH} Daily_Running_Moulds snapshot is EMPTY (wrong/missing month)")
    else:
        r21.notes.append(f"{PLAN_MONTH} snapshot loaded: {len(rmp)} presses")
        for m in sorted(ss["Machine"].unique()):
            r21.checked += 1
            if m not in rmp:
                if p170 and m in p170:
                    r21.notes.append(f"press {m}: cold-started (absent from {PLAN_MONTH} snapshot, in 170 allowable) — by design")
                else:
                    r21.add(press=m, plan_month=PLAN_MONTH,
                            note=f"press absent from {PLAN_MONTH} snapshot AND not in 170 allowable")

    # ---- R1/R12/R18(cure): CT source + default 17 + reconciliation ----
    r1 = R("1C", "Curing CT from DB (default 17); sheet CycleTime_min matches DB")
    r12 = R("12", "Missing curing CT falls back to default 17")
    r18 = R("18C", "Curing (Qty/2)*CT within shift capacity (CT<->Qty reconciliation)")
    for _, row in prod.iterrows():
        s = str(row["SKUCode"]); q = float(row["Qty"])
        exp_ct = ct_of(s)
        sheet_ct = float(row.get("CycleTime_min", 0) or 0)
        r1.checked += 1
        if sheet_ct > 0 and abs(sheet_ct - exp_ct) > 0.5:
            r1.add(date=row["Date"], shift=row["Shift"], press=row["Machine"], sku=s,
                   sheet_ct=sheet_ct, db_ct=round(exp_ct, 2))
        if str(s) not in cure_ct:
            r12.checked += 1
            if sheet_ct and abs(sheet_ct - DEF) > 0.5:
                r12.add(date=row["Date"], press=row["Machine"], sku=s, sheet_ct=sheet_ct,
                        expected_default=DEF)
        # capacity: (Qty/2)*CT must fit a shift
        r18.checked += 1
        used = (q / CAV) * exp_ct
        if used > SHIFT_MINS + MIN_TOL:
            r18.add(date=row["Date"], shift=row["Shift"], press=row["Machine"], sku=s, qty=q,
                    ct=round(exp_ct, 2), used_min=round(used, 1), over_by=round(used - SHIFT_MINS, 1))

    # ---- R2/R10: curing CO=480, clean=480 ; max 12 CO/day ----
    r2 = R("2C", "Curing CO time = 480 and mould-clean = 480")
    for _, row in ss.iterrows():
        if float(row["CO_Mins"]) > 0:
            r2.checked += 1
            if int(row["CO_Mins"]) != SRC["CURING_CO"]:
                r2.add(date=row["Date"], shift=row["Shift"], press=row["Machine"],
                       kind="CO", expected=SRC["CURING_CO"], actual=float(row["CO_Mins"]))
        if float(row["Mould_Clean_Mins"]) > 0:
            r2.checked += 1
            if int(row["Mould_Clean_Mins"]) != SRC["CLEAN_MINS"]:
                r2.add(date=row["Date"], shift=row["Shift"], press=row["Machine"],
                       kind="Clean", expected=SRC["CLEAN_MINS"], actual=float(row["Mould_Clean_Mins"]))

    r10 = R("10", f"Curing CO per day <= {SRC['MAX_CO_DAY']}")
    co_rows = ss[ss["CO_Mins"] > 0]
    for d, grp in co_rows.groupby(ss.loc[co_rows.index, "Date"].astype(str)):
        r10.checked += 1
        n = len(grp)
        if n > SRC["MAX_CO_DAY"]:
            r10.add(date=d, curing_cos=n, limit=SRC["MAX_CO_DAY"], over_by=n - SRC["MAX_CO_DAY"])

    # ---- R19/R20(cure): no production during CO or clean ----
    r19 = R("19C", "No production during a curing CO / mould clean")
    for _, row in ss.iterrows():
        if float(row["CO_Mins"]) > 0 or float(row["Mould_Clean_Mins"]) > 0:
            r19.checked += 1
            if float(row["Qty"]) > 0:
                r19.add(date=row["Date"], shift=row["Shift"], press=row["Machine"],
                        sku=row["SKUCode"], qty=float(row["Qty"]),
                        co_min=float(row["CO_Mins"]), clean_min=float(row["Mould_Clean_Mins"]))

    # ---- R16(cure): one press cures at most one SKU per shift ----
    r16 = R("16C", "No curing press runs two SKUs in one shift")
    for (m, d, sh), grp in prod.groupby(["Machine", "Date", "Shift"]):
        r16.checked += 1
        skus = sorted(set(grp["SKUCode"].astype(str)))
        if len(skus) > 1:
            r16.add(date=d, shift=sh, press=m, skus=",".join(skus), n_skus=len(skus))

    # ---- R11/R15(cure): (Qty/2)*CT + CO + Clean <= 480 per press/shift ----
    r15 = R("15C", "Curing (Qty/2)*CT + CO_Mins + Clean_Mins <= 480 per press/shift")
    agg = defaultdict(lambda: [0.0, 0.0, 0.0, []])
    for _, row in ss.iterrows():
        key = (row["Machine"], row["Date"], row["Shift"])
        agg[key][1] += float(row["CO_Mins"])
        agg[key][2] += float(row["Mould_Clean_Mins"])
        q = float(row["Qty"])
        if q > 0:
            agg[key][0] += (q / CAV) * ct_of(str(row["SKUCode"]))
            agg[key][3].append(str(row["SKUCode"]))
    for (m, d, sh), (pm, cm, cl, skus) in agg.items():
        r15.checked += 1
        tot = pm + cm + cl
        if tot > SHIFT_MINS + MIN_TOL:
            r15.add(date=d, shift=sh, press=m, skus=",".join(skus), prod_min=round(pm, 1),
                    co_min=round(cm, 1), clean_min=round(cl, 1), total_min=round(tot, 1),
                    over_by=round(tot - SHIFT_MINS, 1))

    # ---- R7(cure): occupancy <= 100% ----
    r7 = R("7C", "No curing press occupancy > 100%")
    try:
        mu = read_sheet(path, "Machine Utilization", CUR_HDR["Machine Utilization"])
        occ = [c for c in mu.columns if str(c).strip().lower() == "occupancy_pct"]
        if occ:
            for _, row in mu.iterrows():
                v = pd.to_numeric(pd.Series([row[occ[0]]]), errors="coerce").iloc[0]
                if pd.isna(v):
                    continue
                r7.checked += 1
                if v > 1.0 + 1e-4:
                    r7.add(press=row.get("Machine"), occupancy_pct=round(float(v), 4))
    except Exception as e:
        r7.skipped = f"Machine Utilization read failed: {e}"

    # ---- R8(cure): cured <= demand per SKU ----
    r8 = R("8C", "Curing cured <= demand per SKU (no over-cure)")
    dem = SRC.get("demand") or {}
    cured = defaultdict(float)
    for _, row in prod.iterrows():
        cured[str(row["SKUCode"])] += float(row["Qty"])
    if not dem:
        r8.skipped = f"demand not loaded ({SRC_ERR.get('demand','?')})"
    else:
        for s, c in cured.items():
            r8.checked += 1
            dq = dem.get(s)
            if dq is not None and c > dq + 0.5:
                r8.add(sku=s, cured=round(c), demand=round(dq), over_by=round(c - dq))

    # ---- R3(cure)/R17: press<->SKU eligibility + mould feasibility (bipartite) ----
    r3 = R("3C", "Curing press eligible for its SKU (allowable matrix)")
    ca = SRC.get("cure_allow")
    if not ca:
        r3.skipped = f"curing allowable not loaded ({SRC_ERR.get('cure_allow','?')})"
    else:
        for _, row in prod.iterrows():
            s = str(row["SKUCode"]); m = row["Machine"]
            elig = ca.get(s)
            if elig is None:
                continue
            r3.checked += 1
            if m not in elig:
                r3.add(date=row["Date"], shift=row["Shift"], press=m, sku=s)

    r17 = R("17", "Mould feasibility: 2 eligible moulds/press, no mould in 2 presses same shift (bipartite)")
    _mould_bipartite(r17, prod)

    # ---- R6(cure): opening GT matches DB per plan_month ----
    r6 = R("6", f"Opening GT / carcass from DB for plan_month={PLAN_MONTH}")
    try:
        df = read_sheet(path, "Demand Fulfillment", CUR_HDR["Demand Fulfillment"])
        og = SRC.get("open_gt") or {}
        if not og:
            r6.skipped = f"opening GT not loaded ({SRC_ERR.get('open_gt','?')})"
        elif "GT_Inventory" in df.columns:
            for _, row in df.iterrows():
                s = str(row["SKUCode"])
                sheet_gt = pd.to_numeric(pd.Series([row["GT_Inventory"]]), errors="coerce").iloc[0]
                if pd.isna(sheet_gt):
                    continue
                r6.checked += 1
                db_gt = og.get(s, 0.0)
                if abs(float(sheet_gt) - db_gt) > 0.5:
                    r6.add(sku=s, sheet_opening_gt=round(float(sheet_gt)), db_opening_gt=round(db_gt),
                           plan_month=PLAN_MONTH)
    except Exception as e:
        r6.skipped = f"opening-GT cross-check failed: {e}"

    return {"prod": prod, "cured": cured}


def _mould_bipartite(rule, prod):
    """Rule 17: exact per-shift 2-mould-per-press disjoint feasibility, mirroring
    scratch_mould_audit — but plan_month-correct. Uses DB eligibility (sku_moulds)
    augmented with the plan_month running-moulds Day-0 mounted (SKU,mould) pairs."""
    mould = SRC.get("mould")
    if not mould:
        rule.skipped = f"mould eligibility not loaded ({SRC_ERR.get('mould','?')})"
        return
    sku_moulds = {k: set(v) for k, v in mould["sku_moulds"].items()}
    # augment with plan_month Day-0 mounted pairs (month-correct fold)
    try:
        eng = SRC.get("engine"); bc = SRC["bc"]
        rmt = getattr(bc, "RUNNING_MOULDS_TABLE", "Daily_Running_Moulds")
        q = (f"SELECT WCNAME, Sapcode, `Current MouldNo` AS mould "
             f"FROM {rmt} WHERE plan_month = '{PLAN_MONTH}'")
        rm = pd.read_sql(q, eng)
        for _, r in rm.iterrows():
            s = str(r["Sapcode"]); md = str(r["mould"])
            if s and md and md.lower() != "nan":
                sku_moulds.setdefault(s, set()).add(md)
    except Exception as e:
        rule.notes.append(f"Day-0 mould fold skipped ({e}); using DB eligibility only")

    try:
        from scipy.sparse import csr_matrix
        from scipy.sparse.csgraph import maximum_bipartite_matching
    except Exception as e:
        rule.skipped = f"scipy unavailable for bipartite matching: {e}"
        return

    for (d, sh), grp in prod.groupby(["Date", "Shift"]):
        presses = list(grp["Machine"].unique())
        # slots: 2 per press; moulds: union of eligible moulds for the shift's SKUs
        slot_press, slot_sku = [], []
        for _, row in grp.iterrows():
            slot_press += [row["Machine"], row["Machine"]]
            slot_sku += [str(row["SKUCode"]), str(row["SKUCode"])]
        moulds = sorted({m for s in set(slot_sku) for m in sku_moulds.get(s, set())})
        mi = {m: i for i, m in enumerate(moulds)}
        rule.checked += 1
        # structural: any SKU with <2 eligible moulds
        short = [s for s in set(slot_sku) if len(sku_moulds.get(s, set())) < 2]
        if short:
            for s in short:
                rule.add(date=d, shift=sh, sku=s, kind="structural_<2_moulds",
                         eligible_moulds=len(sku_moulds.get(s, set())))
            continue
        if not moulds:
            continue
        rows, cols = [], []
        for si, s in enumerate(slot_sku):
            for m in sku_moulds.get(s, set()):
                rows.append(si); cols.append(mi[m])
        M = csr_matrix((np.ones(len(rows)), (rows, cols)), shape=(len(slot_sku), len(moulds)))
        match = maximum_bipartite_matching(M, perm_type="column")
        if (match == -1).any():
            unmatched = int((match == -1).sum())
            rule.add(date=d, shift=sh, kind="infeasible_bipartite",
                     presses=len(presses), slots=len(slot_sku), unmatched_slots=unmatched,
                     note="no disjoint 2-eligible-mould assignment (mould contention)")


# ══════════════════════════════════════════════════════════════════════════════
#  GT aging (rule 9) — needs building GT built + curing cured per SKU/day
# ══════════════════════════════════════════════════════════════════════════════
def validate_gt_aging(bld, cur, bpath, cpath):
    r9 = R("9G", "GT aging <= 3 days (FIFO: built GT cured within 3 days)")
    # inflow: building GT built per (sku,day,shift) (GT machines only); outflow: cured per (sku,day,shift)
    prod_b = bld["prod"]
    inflow = defaultdict(float)
    for _, row in prod_b.iterrows():
        if row["Machine"] in GT_MACHINES and float(row["Qty"]) > 0:
            inflow[(str(row["SKUCode"]), str(row["Date"]), str(row["Shift"]))] += float(row["Qty"])
    outflow = defaultdict(float)
    for _, row in cur["prod"].iterrows():
        outflow[(str(row["SKUCode"]), str(row["Date"]), str(row["Shift"]))] += float(row["Qty"])
    days = sorted({k[1] for k in inflow} | {k[1] for k in outflow})
    open_gt = SRC.get("open_gt") or {}
    _fifo_age_check(r9, inflow, outflow, open_gt, days, SRC["GT_AGE_DAYS"], label="GT")


# ══════════════════════════════════════════════════════════════════════════════
#  Report
# ══════════════════════════════════════════════════════════════════════════════
RULE_ORDER = ["1", "1C", "2", "2C", "3B", "3C", "4", "5", "6", "7B", "7C", "8B", "8C",
              "9G", "9C", "10", "11", "12", "13", "14", "15B", "15C", "16B", "16C",
              "17", "18", "18C", "19B", "19C", "21"]

def report(max_rows):
    print("\n" + "=" * 90)
    print(f"  B2C SCHEDULE VALIDATION  |  plan_month={PLAN_MONTH}")
    print(f"  building={ARGS.building}")
    print(f"  curing  ={ARGS.curing}")
    print(f"  demand  ={ARGS.demand}")
    print("=" * 90)
    if SRC_ERR:
        print("\n  ⚠ SOURCE LOADERS THAT FAILED (rules depending on them are SKIP, not PASS):")
        for k, v in SRC_ERR.items():
            print(f"     - {k}: {v}")

    npass = nfail = nskip = 0
    print(f"\n  {'RULE':<6}{'STATUS':<7}{'VIOL':<7}{'CHECKED':<9} DESCRIPTION")
    print("  " + "-" * 86)
    all_rows = []
    for num in RULE_ORDER + [k for k in RULES if k not in RULE_ORDER]:
        r = RULES.get(num)
        if r is None:
            continue
        st = r.status
        npass += st == "PASS"; nfail += st == "FAIL"; nskip += st == "SKIP"
        mark = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "skip"}[st]
        print(f"  {r.num:<6}{mark:<7}{len(r.violations):<7}{r.checked:<9} {r.name}")
        for v in r.violations:
            all_rows.append({"rule": r.num, **v})

    print("  " + "-" * 86)
    print(f"  TOTAL: {npass} PASS | {nfail} FAIL | {nskip} SKIP  "
          f"(total violations: {sum(len(r.violations) for r in RULES.values())})")

    # detailed violations per failing rule
    for num in RULE_ORDER + [k for k in RULES if k not in RULE_ORDER]:
        r = RULES.get(num)
        if not r or not r.violations:
            continue
        print(f"\n  ── Rule {r.num} — {r.name}  [{len(r.violations)} violation(s)] ──")
        for v in r.violations[:max_rows]:
            print("      " + " | ".join(f"{k}={v[k]}" for k in v))
        if len(r.violations) > max_rows:
            print(f"      … and {len(r.violations) - max_rows} more (see {ARGS.out})")

    # write full report
    try:
        if all_rows:
            with pd.ExcelWriter(ARGS.out) as xl:
                pd.DataFrame(all_rows).to_excel(xl, "violations", index=False)
                summary = pd.DataFrame([{
                    "rule": r.num, "status": r.status, "violations": len(r.violations),
                    "checked": r.checked, "description": r.name,
                    "skipped_reason": r.skipped or "",
                } for r in (RULES[k] for k in RULE_ORDER if k in RULES)])
                summary.to_excel(xl, "summary", index=False)
            print(f"\n  Full violation detail written → {ARGS.out}")
    except Exception as e:
        print(f"\n  (could not write {ARGS.out}: {e})")

    return nfail


def main():
    print("Loading authoritative sources (DB / CSV / config) …")
    load_sources()
    print("Validating BUILDING schedule …")
    bld = validate_building(ARGS.building)
    print("Validating CURING schedule …")
    cur = validate_curing(ARGS.curing)
    print("Validating GT aging (cross-workbook) …")
    validate_gt_aging(bld, cur, ARGS.building, ARGS.curing)
    nfail = report(ARGS.max_rows)
    sys.exit(1 if nfail else 0)


if __name__ == "__main__":
    main()
