import pandas as pd, numpy as np
OUT="/Users/anmolsaini/Documents/cbc/scratch_sap"
log = pd.read_excel("/Users/anmolsaini/Documents/cbc/data/input/curingPCR_4sep.xlsx")
log = log[log["isCured"]==True].copy()
log["dt"]=pd.to_datetime(log["dtandTime"]); log["pday"]=(log["dt"]-pd.Timedelta(hours=7)).dt.date.astype(str)
rec = pd.read_csv("/Users/anmolsaini/Documents/cbc/data/input/RECIPE_MASTER.csv")
log["SKU"]=log["recipeID"].map(dict(zip(rec["iD"], rec["description"].astype(str).str.strip())))
raw = pd.read_pickle(f"{OUT}/raw_all.pkl")
z = raw[raw["Mtart"].astype(str).str.strip().str.upper()=="ZFGS"].copy()
z["SKUCode"]=z["Matnr"].astype(str).str.strip(); z["ProdQty"]=z["ProdQty"].astype(float).apply(np.floor).astype(int)

for days,label in [(["2026-09-01","2026-09-02"],"DAYS 1-2 (both complete)"),
                   (["2026-09-01","2026-09-02","2026-09-03"],"DAYS 1-3")]:
    L=log[log["pday"].isin(days)].dropna(subset=["SKU"]).groupby("SKU").size().rename("log")
    S=z[z["_ReqDate"].isin(days)].groupby("SKUCode")["ProdQty"].sum().rename("sap")
    c=pd.concat([S,L],axis=1).fillna(0).astype(int); c["delta"]=c["sap"]-c["log"]
    print(f"\n=== {label}: SAP {c['sap'].sum()} LOG {c['log'].sum()} delta {c['delta'].sum()} | SKUs sap {(c['sap']>0).sum()} log {(c['log']>0).sum()}")
    print(c.reindex(c["delta"].abs().sort_values(ascending=False).index).head(20).to_string())
    print("SAP-only SKUs:", c[(c['log']==0)].to_dict()['sap'])
    print("LOG-only SKUs:", c[(c['sap']==0)].to_dict()['log'])

# deduction impact
dem=pd.read_excel("/Users/anmolsaini/Documents/cbc/data/input/BTP_SEPT26_DEMAND.xlsx")
print("\ndemand cols:", list(dem.columns))
