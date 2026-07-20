"""scratch_fulltest.py — seed + run May/June/July through the CLOUD path and
audit that all 5 output tables are completely filled. Temporary test harness."""
import sys, os, json
import pandas as pd
from datetime import datetime, timedelta
from sqlalchemy import text
import cbc_env
import connection as conn

MONTHS = {
    "may":  ("demand_may.xlsx",                     (2026, 5, 1), 31, "Daily_Running_Moulds"),
    "june": ("june_production_data.xlsx", (2026, 6, 1), 30, "testing_Daily_Running_Moulds"),
    "july": ("july_demand_tomerJi1.xlsx",           (2026, 7, 1), 31, "june_Daily_Running_Moulds"),
}
PIDS = {m: f"FULLTEST_{m}" for m in MONTHS}


def seed():
    eng = conn.get_engine()
    for m, (fname, (Y, M, D), days, _) in MONTHS.items():
        pid = PIDS[m]
        with eng.begin() as c:
            for t in ["jkt_demand", "jkt_plan_params"] + conn._OUTPUT_TABLES:
                c.execute(text(f"DELETE FROM {t} WHERE plan_id=:p"), {"p": pid})
        df = pd.read_excel(os.path.join(cbc_env.INPUT_DIR, fname))
        sku = next(c for c in df.columns if "SKU" in str(c))
        qty = next(c for c in df.columns
                   if any(x in str(c) for x in ("Requirement", "Demand", "Qty", "Quantity")))
        desc = next((c for c in df.columns if "escription" in str(c)), None)
        d = pd.DataFrame({
            "plan_id": pid,
            "skuCode": df[sku].astype(str).str.strip(),
            "requirement": pd.to_numeric(df[qty], errors="coerce"),
        })
        if desc:
            d["skuDescription"] = df[desc]
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
        print(f"seeded {pid}: {len(d)} demand rows, desc_col={'yes' if desc else 'NO (master fallback)'}")


def run(month):
    import main
    _, _, _, rmt = MONTHS[month]
    s = main.run_plan(PIDS[month], created_by="fulltest", running_moulds_table=rmt)
    print("RESULT " + json.dumps({"month": month, "kpi": s["kpi"], "rows": s["rows_written"]}))


if __name__ == "__main__":
    if sys.argv[1] == "seed":
        seed()
    else:
        run(sys.argv[1])
