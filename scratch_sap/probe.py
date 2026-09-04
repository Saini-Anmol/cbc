import sys, json
sys.path.insert(0, "/Users/anmolsaini/Documents/cbc/api")
import sap_production_data as sap
try:
    res = sap.fetch("2026-09-01")
except Exception as e:
    print("FETCH FAIL:", type(e).__name__, repr(e)[:500]); sys.exit(1)
print("records:", len(res))
if res:
    print(json.dumps(res[0], indent=1)[:1500])
