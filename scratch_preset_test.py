"""scratch_preset_test.py — verify the impPriorityFlag preset-gate on the CLOUD path.

Seeds 4 plans (July/August × {BTP Preset Main = impPriorityFlag 1, BTP Preset = impPriorityFlag 0}),
with jkt_plan_params.impPriorityFlag = 0 in ALL of them (so the test proves the PRESET, not the
params row, is what gates the delivery feature), then runs each via the cloud path and checks:
  • BTP Preset Main (impPriorityFlag=1) → feature ON  → July 671,324 / August 638,594
  • BTP Preset      (impPriorityFlag=0) → feature OFF → July 672,696 / August 649,334 (baseline)
Temporary test harness (not part of the deployment)."""
import sys, os, json
import pandas as pd
from datetime import datetime, timedelta
from sqlalchemy import text
import cbc_env
import connection as conn

PLANS = {
    "PRESET_MAIN_july":   ("july_demand_tomerJi1.xlsx",  (2026, 7, 1), 31, "BTP Preset Main", 671324),
    "PRESET_OFF_july":    ("july_demand_tomerJi1.xlsx",  (2026, 7, 1), 31, "BTP Preset",      672696),
    "PRESET_MAIN_august": ("august_demand_tomerji.xlsx", (2026, 8, 1), 31, "BTP Preset Main", 638594),
    "PRESET_OFF_august":  ("august_demand_tomerji.xlsx", (2026, 8, 1), 31, "BTP Preset",      649334),
}


def _find(cols, cands):
    low = {str(c).strip().lower().replace("_", " "): c for c in cols}
    for c in cands:
        if c in low:
            return low[c]
    return None


def seed():
    eng = conn.get_engine()
    for pid, (fname, (Y, M, D), days, preset, _exp) in PLANS.items():
        with eng.begin() as c:
            for t in ["jkt_demand", "jkt_plan_params"] + conn._OUTPUT_TABLES:
                c.execute(text(f"DELETE FROM {t} WHERE plan_id=:p"), {"p": pid})
        df = pd.read_excel(os.path.join(cbc_env.INPUT_DIR, fname))
        sku  = _find(df.columns, ["skucode", "sku code", "sapcode", "sku"])
        qty  = _find(df.columns, ["requirement", "demand", "qty", "quantity"])
        desc = _find(df.columns, ["sku description", "skudescription", "description"])
        flag = _find(df.columns, ["priority flag", "priorityflag", "priority"])
        date = _find(df.columns, ["delivery date", "deliverydate"])
        d = pd.DataFrame({"plan_id": pid,
                          "skuCode": df[sku].astype(str).str.strip(),
                          "requirement": pd.to_numeric(df[qty], errors="coerce")})
        if desc:
            d["skuDescription"] = df[desc]
        if flag:
            d["priorityFlag"] = df[flag].where(df[flag].notna(), None).astype(object)
        if date:
            _dd = pd.to_datetime(df[date].astype(str).str.strip(), format="%d/%m/%y", errors="coerce")
            _dd = _dd.fillna(pd.to_datetime(df[date], dayfirst=True, errors="coerce"))
            d["deliveryDate"] = [None if pd.isna(x) else x.date() for x in _dd]
        d = d.dropna(subset=["requirement"]); d["requirement"] = d["requirement"].astype(int)
        d = d[d["requirement"] > 0]
        d.to_sql("jkt_demand", eng, if_exists="append", index=False)
        start = datetime(Y, M, D); end = start + timedelta(days=days - 1)
        pd.DataFrame([{
            "plan_id": pid, "plantName": "BTP", "productName": "PCR",
            "planStartDate": start.date(), "planEndDate": end.date(),
            "noOfChangeOver": 12, "efficiency": 94,
            "optimisationPreset": preset,
            "impPriorityFlag": 0,   # deliberately 0 → proves the PRESET is the gate
        }]).to_sql("jkt_plan_params", eng, if_exists="append", index=False)
        print(f"seeded {pid}: preset={preset!r} params.impPriorityFlag=0 rows={len(d)}")


def run(pid):
    import main
    s = main.run_plan(pid, created_by="preset-test", running_moulds_table="Daily_Running_Moulds")
    exp = PLANS[pid][4]
    cured = s["kpi"]["gt_cured"]
    ok = "MATCH" if cured == exp else f"MISMATCH (exp {exp})"
    print("PRESET_RESULT " + json.dumps({"plan_id": pid, "preset": PLANS[pid][3],
                                         "gt_cured": cured, "expected": exp, "verdict": ok}))


if __name__ == "__main__":
    if sys.argv[1] == "seed":
        seed()
    else:
        run(sys.argv[1])
