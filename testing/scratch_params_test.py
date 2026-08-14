import os as _o, sys as _s; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))  # allow imports from repo root
"""scratch_params_test.py — prove the cloud backend reads EVERYTHING from jkt_plan_params only
(never the preset). Each plan's optimisationPreset is deliberately set to the OPPOSITE preset,
so if the output follows the params values the backend is genuinely params-only.
  cols: (demand_file, (Y,M,D), days, params.impPriorityFlag, params.noOfChangeOver,
         params.efficiency, optimisationPreset[mismatched], expected_gt_cured)."""
import sys, os, json
import pandas as pd
from datetime import datetime, timedelta
from sqlalchemy import text
import cbc_env, connection as conn

PLANS = {
    # impPriorityFlag from PARAMS drives the feature — preset is the OPPOSITE on purpose
    "PARAMS_ON_august":  ("august_demand_tomerji.xlsx", (2026,8,1),31, 1,12,94, "BTP Preset",      638594),
    "PARAMS_OFF_august": ("august_demand_tomerji.xlsx", (2026,8,1),31, 0,12,94, "BTP Preset Main", 649334),
    "PARAMS_ON_july":    ("july_demand_tomerJi1.xlsx",  (2026,7,1),31, 1,12,94, "BTP Preset",      671324),
    "PARAMS_OFF_july":   ("july_demand_tomerJi1.xlsx",  (2026,7,1),31, 0,12,94, "BTP Preset Main", 672696),
    # noOfChangeOver / efficiency from PARAMS change the plan
    "PARAMS_co20_aug":   ("august_demand_tomerji.xlsx", (2026,8,1),31, 0,20,94, "BTP Preset",      645579),
    "PARAMS_eff70_aug":  ("august_demand_tomerji.xlsx", (2026,8,1),31, 0,12,70, "BTP Preset",      556986),
}


def _find(cols, cands):
    low = {str(c).strip().lower().replace("_", " "): c for c in cols}
    return next((low[k] for k in cands if k in low), None)


def seed():
    eng = conn.get_engine()
    for pid, (fname,(Y,M,D),days,imp,co,eff,preset,_exp) in PLANS.items():
        with eng.begin() as c:
            for t in ["jkt_demand","jkt_plan_params"]+conn._OUTPUT_TABLES:
                c.execute(text(f"DELETE FROM {t} WHERE plan_id=:p"), {"p": pid})
        df = pd.read_excel(os.path.join(cbc_env.INPUT_DIR, fname))
        sku=_find(df.columns,["skucode","sku"]); qty=_find(df.columns,["requirement"])
        desc=_find(df.columns,["sku description","skudescription"])
        flag=_find(df.columns,["priority flag","priorityflag"]); date=_find(df.columns,["delivery date","deliverydate"])
        d = pd.DataFrame({"plan_id":pid,"skuCode":df[sku].astype(str).str.strip(),
                          "requirement":pd.to_numeric(df[qty],errors="coerce")})
        if desc: d["skuDescription"]=df[desc]
        if flag: d["priorityFlag"]=df[flag].where(df[flag].notna(),None).astype(object)
        if date:
            _dd=pd.to_datetime(df[date].astype(str).str.strip(),format="%d/%m/%y",errors="coerce")
            _dd=_dd.fillna(pd.to_datetime(df[date],dayfirst=True,errors="coerce"))
            d["deliveryDate"]=[None if pd.isna(x) else x.date() for x in _dd]
        d=d.dropna(subset=["requirement"]); d["requirement"]=d["requirement"].astype(int); d=d[d["requirement"]>0]
        d.to_sql("jkt_demand", eng, if_exists="append", index=False)
        start=datetime(Y,M,D); end=start+timedelta(days=days-1)
        pd.DataFrame([{"plan_id":pid,"plantName":"BTP","productName":"PCR",
                       "planStartDate":start.date(),"planEndDate":end.date(),
                       "noOfChangeOver":co,"efficiency":eff,"optimisationPreset":preset,
                       "impPriorityFlag":imp}]).to_sql("jkt_plan_params", eng, if_exists="append", index=False)
        print(f"seeded {pid}: params(imp={imp},CO={co},eff={eff}) preset={preset!r}[mismatched]")


def run(pid):
    import main
    s = main.run_plan(pid, created_by="params-test", running_moulds_table="Daily_Running_Moulds")
    cured=s["kpi"]["gt_cured"]; exp=PLANS[pid][7]
    print("PARAMS_RESULT " + json.dumps({"plan_id":pid,"gt_cured":cured,"expected":exp,
                                         "verdict":"MATCH" if cured==exp else f"MISMATCH(exp {exp})"}))


if __name__ == "__main__":
    seed() if sys.argv[1]=="seed" else run(sys.argv[1])
