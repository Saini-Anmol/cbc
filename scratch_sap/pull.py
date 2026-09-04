import sys, json, os
sys.path.insert(0, "/Users/anmolsaini/Documents/cbc/api")
import pandas as pd
import sap_production_data as sap

OUT="/Users/anmolsaini/Documents/cbc/scratch_sap"
allrec=[]
for d in ["2026-09-01","2026-09-02","2026-09-03","2026-09-04"]:
    try:
        res = sap.fetch(d)
    except Exception as e:
        print("FETCH FAIL", d, type(e).__name__, repr(e)[:300]); continue
    for r in res:
        r.pop("__metadata", None)
        r["_ReqDate"]=d
    allrec.extend(res)
    json.dump(res, open(f"{OUT}/raw_{d}.json","w"))
df=pd.DataFrame(allrec)
df.to_pickle(f"{OUT}/raw_all.pkl")
print("total raw rows:", len(df))
print(df.groupby(["_ReqDate","Mtart"]).size())
print("\nYdate vs _ReqDate mismatch:", (df["Ydate"]!=df["_ReqDate"]).sum())
z=df[df["Mtart"].str.strip().str.upper()=="ZFGS"]
print("\nZFGS rows:", len(z))
print(z.groupby(["_ReqDate","Shift"]).size())
print("\nPType values in ZFGS:", z["PType"].value_counts().to_dict())
