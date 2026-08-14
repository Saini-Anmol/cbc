import os as _o, sys as _s; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))  # allow imports from repo root
"""scratch_fulltest.py — seed June/July/August into the DB (jkt_demand + jkt_plan_params)
and run them through the CLOUD path (main.run_plan), auditing that all 5 output tables are
filled. Seeds the SAME PARITY_<month> plan_ids the parity harness (scratch_parity_run.py cloud)
uses, and — critically — carries the committed-delivery columns (priorityFlag + deliveryDate)
from the demand Excel into jkt_demand so the cloud DELIVERY_PRIORITY feature is exercised and
local↔cloud stay comparable. Temporary test harness (not part of the deployment)."""
import sys, os, json
import pandas as pd
from datetime import datetime, timedelta
from sqlalchemy import text
import cbc_env
import connection as conn

# Current per-month inputs. RUNNING MOULDS IS ALWAYS Daily_Running_Moulds (plan_month-keyed);
# the historical testing_/june_ variants are retired.
MONTHS = {
    "june":   ("june_demand_tomerji.xlsx",   (2026, 6, 1), 30, "Daily_Running_Moulds"),
    "july":   ("july_demand_tomerJi1.xlsx",  (2026, 7, 1), 31, "Daily_Running_Moulds"),
    "august": ("august_demand_tomerji.xlsx", (2026, 8, 1), 31, "Daily_Running_Moulds"),
}
PIDS = {m: f"PARITY_{m}" for m in MONTHS}   # align with scratch_parity_run.py cloud mode


def _find(cols, cands):
    low = {str(c).strip().lower().replace("_", " "): c for c in cols}
    for c in cands:
        if c in low:
            return low[c]
    return None


def seed():
    eng = conn.get_engine()
    for m, (fname, (Y, M, D), days, _) in MONTHS.items():
        pid = PIDS[m]
        with eng.begin() as c:
            for t in ["jkt_demand", "jkt_plan_params"] + conn._OUTPUT_TABLES:
                c.execute(text(f"DELETE FROM {t} WHERE plan_id=:p"), {"p": pid})
        df = pd.read_excel(os.path.join(cbc_env.INPUT_DIR, fname))
        sku  = _find(df.columns, ["skucode", "sku code", "sapcode", "sku"])
        qty  = _find(df.columns, ["requirement", "demand", "qty", "quantity"])
        desc = _find(df.columns, ["sku description", "skudescription", "description"])
        flag = _find(df.columns, ["priority flag", "priorityflag", "priority"])
        date = _find(df.columns, ["delivery date", "deliverydate"])
        mkt  = _find(df.columns, ["market"])

        d = pd.DataFrame({
            "plan_id":     pid,
            "skuCode":     df[sku].astype(str).str.strip(),
            "requirement": pd.to_numeric(df[qty], errors="coerce"),
        })
        if desc:
            d["skuDescription"] = df[desc]
        if mkt:
            d["market"] = df[mkt].astype(str).str.slice(0, 20).where(df[mkt].notna(), None)
        # committed-delivery columns (the whole point of this refresh)
        if flag:
            d["priorityFlag"] = df[flag].where(df[flag].notna(), None).astype(object)
        if date:
            _dd = pd.to_datetime(df[date].astype(str).str.strip(),
                                 format="%d/%m/%y", errors="coerce")
            _dd = _dd.fillna(pd.to_datetime(df[date], dayfirst=True, errors="coerce"))
            d["deliveryDate"] = [None if pd.isna(x) else x.date() for x in _dd]

        d = d.dropna(subset=["requirement"])
        d["requirement"] = d["requirement"].astype(int)
        d = d[d["requirement"] > 0]
        d.to_sql("jkt_demand", eng, if_exists="append", index=False)

        start = datetime(Y, M, D); end = start + timedelta(days=days - 1)
        pd.DataFrame([{
            "plan_id": pid, "plantName": "BTP", "productName": "PCR",
            "planStartDate": start.date(), "planEndDate": end.date(),
            "noOfChangeOver": 12, "efficiency": 94,
        }]).to_sql("jkt_plan_params", eng, if_exists="append", index=False)
        n_flag = int(d["priorityFlag"].notna().sum()) if flag else 0
        n_date = int(d["deliveryDate"].notna().sum()) if date else 0
        print(f"seeded {pid}: {len(d)} demand rows | priorityFlag set={n_flag} "
              f"deliveryDate set={n_date} | desc={'yes' if desc else 'NO'}")


def audit(month):
    """After a cloud run, report row counts + spot-checks for every output table."""
    eng = conn.get_engine(); pid = PIDS[month]
    days = MONTHS[month][2]
    with eng.begin() as c:
        counts = {t: c.execute(text(f"SELECT COUNT(*) FROM {t} WHERE plan_id=:p"),
                               {"p": pid}).scalar() for t in conn._OUTPUT_TABLES}
        kpi = c.execute(text("SELECT demandFulfillment, capacityUtilisation, "
                             "building_capacityUtilisation, curingChangeovers, "
                             "buildingChangeovers FROM jkt_plan_kpis WHERE plan_id=:p"),
                        {"p": pid}).mappings().first()
        capdays = c.execute(text("SELECT COUNT(*) FROM jkt_plan_capacityUtilisation "
                                 "WHERE plan_id=:p"), {"p": pid}).scalar()
    print("AUDIT " + json.dumps({
        "month": month, "plan_id": pid, "rows": counts,
        "kpi_row": (dict(kpi) if kpi else None),
        "capUtil_days": capdays, "expected_days": days,
        "capUtil_days_ok": (capdays == days),
        "all_5_tables_filled": all(v > 0 for k, v in counts.items()
                                   if k != "jkt_plan_Infeasibility"),
    }))


def run(month):
    import main
    _, _, _, rmt = MONTHS[month]
    s = main.run_plan(PIDS[month], created_by="fulltest", running_moulds_table=rmt)
    print("RESULT " + json.dumps({"month": month, "kpi": s["kpi"], "rows": s["rows_written"]}))
    audit(month)


if __name__ == "__main__":
    if sys.argv[1] == "seed":
        seed()
    elif sys.argv[1] == "audit":
        audit(sys.argv[2])
    else:
        run(sys.argv[1])
