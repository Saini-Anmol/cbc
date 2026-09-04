import pandas as pd, numpy as np
OUT="/Users/anmolsaini/Documents/cbc/scratch_sap"
dem=pd.read_excel("/Users/anmolsaini/Documents/cbc/data/input/BTP_SEPT26_DEMAND.xlsx")
dem["SKUCode"]=dem["SKUCode"].astype(str).str.strip()
d=dem.groupby("SKUCode")["Requirement"].sum()
print("demand total:", int(dem['Requirement'].sum()), "SKUs:", d.size)

log = pd.read_excel("/Users/anmolsaini/Documents/cbc/data/input/curingPCR_4sep.xlsx")
log=log[log["isCured"]==True].copy(); log["dt"]=pd.to_datetime(log["dtandTime"])
log["pday"]=(log["dt"]-pd.Timedelta(hours=7)).dt.date.astype(str)
rec=pd.read_csv("/Users/anmolsaini/Documents/cbc/data/input/RECIPE_MASTER.csv")
log["SKU"]=log["recipeID"].map(dict(zip(rec["iD"],rec["description"].astype(str).str.strip())))
days=["2026-09-01","2026-09-02","2026-09-03"]
LOG=log[log["pday"].isin(days)].dropna(subset=["SKU"]).groupby("SKU").size()

raw=pd.read_pickle(f"{OUT}/raw_all.pkl")
z=raw[raw["Mtart"].astype(str).str.strip().str.upper()=="ZFGS"].copy()
z["SKUCode"]=z["Matnr"].astype(str).str.strip(); z["ProdQty"]=z["ProdQty"].astype(float).apply(np.floor).astype(int)
SAP=z[z["_ReqDate"].isin(days)].groupby("SKUCode")["ProdQty"].sum()
# hybrid: SAP days1-2 + log day3
SAP12=z[z["_ReqDate"].isin(days[:2])].groupby("SKUCode")["ProdQty"].sum()
LOG3=log[log["pday"]=="2026-09-03"].dropna(subset=["SKU"]).groupby("SKU").size()
HYB=SAP12.add(LOG3,fill_value=0)

for name,prod in [("LOG (days1-3)",LOG),("SAP (days1-3, day3 C missing)",SAP),("HYBRID SAP d1-2 + LOG d3",HYB)]:
    p=prod.reindex(d.index).fillna(0)
    resid=(d-p).clip(lower=0)
    absent=prod[~prod.index.isin(d.index)]
    floored=(p-d).clip(lower=0)
    print(f"\n{name}: prod_total {int(prod.sum())} -> remaining demand {int(resid.sum())} (deducted {int(dem['Requirement'].sum()-resid.sum())})")
    print("  SKUs absent from demand:", len(absent), "units", int(absent.sum()), dict(absent[absent>0].astype(int)) if len(absent)<8 else "")
    print("  floored-at-0 excess:", int(floored.sum()), dict(floored[floored>0].astype(int)))
