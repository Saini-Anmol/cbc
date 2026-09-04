import pandas as pd, numpy as np, sys
sys.path.insert(0,"/Users/anmolsaini/Documents/cbc/api")
OUT="/Users/anmolsaini/Documents/cbc/scratch_sap"

# ---------- curing log ----------
log = pd.read_excel("/Users/anmolsaini/Documents/cbc/data/input/curingPCR_4sep.xlsx")
log = log[log["isCured"]==True].copy()
log["dt"]=pd.to_datetime(log["dtandTime"])
log["pday"]=(log["dt"]-pd.Timedelta(hours=7)).dt.date.astype(str)
log["pshift"]=pd.cut(((log["dt"]-pd.Timedelta(hours=7)).dt.hour),
                     bins=[-1,7,15,24], labels=["A","B","C"])
print("log last event:", log["dt"].max())
rec = pd.read_csv("/Users/anmolsaini/Documents/cbc/data/input/RECIPE_MASTER.csv")
m = dict(zip(rec["iD"], rec["description"].astype(str).str.strip()))
log["SKU"]=log["recipeID"].map(m)
days=["2026-09-01","2026-09-02","2026-09-03"]
L=log[log["pday"].isin(days)]
print("raw rows/day:", L.groupby("pday").size().to_dict())
print("unmapped rows:", L["SKU"].isna().sum(), "recipeIDs:", L[L["SKU"].isna()]["recipeID"].value_counts().to_dict())
Lm=L.dropna(subset=["SKU"])
print("mapped total:", len(Lm), "SKUs:", Lm["SKU"].nunique())
print("mapped/day:", Lm.groupby("pday").size().to_dict())
log_sku = Lm.groupby("SKU").size().rename("log_qty")
log_day = Lm.groupby(["pday","pshift"],observed=True).size()
print("log per day/shift:\n", log_day)

# ---------- SAP ----------
raw = pd.read_pickle(f"{OUT}/raw_all.pkl")
z = raw[raw["Mtart"].astype(str).str.strip().str.upper()=="ZFGS"].copy()
z["SKUCode"]=z["Matnr"].astype(str).str.strip()
z["ProdQty"]=z["ProdQty"].astype(float).apply(np.floor).astype(int)
z["ProdDate"]=z["_ReqDate"]
z["Shift"]=z["Shift"].astype(str).str.strip().str.upper()
z=z[["ProdDate","SKUCode","Shift","ProdQty","Matkl","Arbpl","StorgLoc"]]
z.to_excel(f"{OUT}/sap_sep01-04.xlsx", index=False)
print("\nSAP zfgs rows saved:", len(z))
print("SAP per day/shift qty:\n", z.pivot_table(index="ProdDate",columns="Shift",values="ProdQty",aggfunc="sum",fill_value=0))
S=z[z["ProdDate"].isin(days)]
print("SAP total 01-03:", S["ProdQty"].sum(), "SKUs:", S["SKUCode"].nunique())
print("SAP per day:", S.groupby("ProdDate")["ProdQty"].sum().to_dict())
sap_sku=S.groupby("SKUCode")["ProdQty"].sum().rename("sap_qty")

# ---------- compare ----------
cmp=pd.concat([sap_sku, log_sku],axis=1).fillna(0).astype(int)
cmp["delta"]=cmp["sap_qty"]-cmp["log_qty"]
cmp=cmp.sort_values("delta",key=abs,ascending=False)
cmp.to_excel(f"{OUT}/sku_compare.xlsx")
print("\nTotals: SAP",cmp['sap_qty'].sum(),"LOG",cmp['log_qty'].sum(),"delta",cmp['delta'].sum())
print("SKUs: sap-only",(cmp['log_qty']==0).sum(),"log-only",(cmp['sap_qty']==0).sum(),"both",((cmp>0).all(axis=1)).sum())
print("\nTop 20 abs deltas:")
print(cmp.head(20).to_string())
# per-day comparison
sd=S.groupby("ProdDate")["ProdQty"].sum()
ld=Lm.groupby("pday").size()
print("\nper-day: \n", pd.DataFrame({"sap":sd,"log":ld}).assign(delta=lambda d:d.sap-d.log).to_string())
# SKU code length check
print("\nSAP SKU len:", S["SKUCode"].str.len().value_counts().to_dict())
print("LOG SKU len:", Lm["SKU"].str.len().value_counts().to_dict())
