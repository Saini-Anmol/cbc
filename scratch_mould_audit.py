"""scratch_mould_audit.py <curing_b2c.xlsx>
EXACT feasibility audit of the mould-availability constraint.

For each shift, the set of simultaneously-running SKUs must be servable by a
DISJOINT assignment of 2 eligible moulds per running press (a mould serves one
press at a time). This is a bipartite b-matching: split each press into 2 slots,
connect each slot to its SKU's eligible moulds, and require a perfect matching of
all slots. Exact via scipy.maximum_bipartite_matching (greedy false-positives out).

Temporary verification harness (not part of the deployment).
"""
import sys
import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import maximum_bipartite_matching
import connection as conn
import curing_consumption as cc

path = sys.argv[1]
eng = conn.get_engine()
sku_moulds = {k: set(v) for k, v in
              cc.ConsumptionETL(eng).load_mould_eligibility()["sku_moulds"].items()}
# Fold Day-0 mounted (mould,SKU) pairs into eligibility — the scheduler does this
# too (orphan moulds mounted at Day 0 but not listed in the mapping), so the audit
# must use the SAME eligibility to be a fair feasibility test.
_rm = pd.read_sql("SELECT WCNAME, Sapcode, `Current MouldNo` AS mould "
                  "FROM jkplanningV1.Daily_Running_Moulds", eng)
for _s, _m in zip(_rm["Sapcode"].astype(str).str.strip(), _rm["mould"].astype(str).str.strip()):
    if _s and _m and _s.lower() != "nan" and _m.lower() != "nan":
        sku_moulds.setdefault(_s, set()).add(_m)

ss = pd.read_excel(path, sheet_name="Shift Schedule", header=0)
ss = ss[ss["SKUCode"].astype(str).str.match(r"^\d")].copy()
ss = ss[pd.to_numeric(ss["Qty"], errors="coerce").fillna(0) > 0]

infeasible_shifts = 0
struct = 0
shifts = 0
worst = []
for (date, shift), g in ss.groupby(["Date", "Shift"]):
    shifts += 1
    press_sku = dict(zip(g["Machine"].astype(str), g["SKUCode"].astype(str)))
    slots = []          # (press, sku) — 2 per press
    for p, s in press_sku.items():
        if len(sku_moulds.get(s, set())) < 2:
            struct += 1
        slots += [(p, s), (p, s)]
    moulds = sorted({m for _, s in slots for m in sku_moulds.get(s, set())})
    if not moulds:
        continue
    midx = {m: i for i, m in enumerate(moulds)}
    rows, cols = [], []
    for si, (p, s) in enumerate(slots):
        for m in sku_moulds.get(s, set()):
            rows.append(si); cols.append(midx[m])
    graph = csr_matrix((np.ones(len(rows), dtype=int), (rows, cols)),
                       shape=(len(slots), len(moulds)))
    match = maximum_bipartite_matching(graph, perm_type="column")
    matched = int((match != -1).sum())
    if matched < len(slots):
        infeasible_shifts += 1
        worst.append((str(date)[:10], shift, len(slots) // 2, (len(slots) - matched)))

print(f"shifts audited              : {shifts}")
print(f"SKU-with-<2-moulds instances: {struct}   (structural — must be 0)")
print(f"shifts NOT exactly feasible : {infeasible_shifts}   (exact bipartite matching failed)")
for d, s, npress, short in sorted(worst, key=lambda x: -x[3])[:6]:
    print(f"    {d} {s}: {npress} presses, {short} mould-slots unfillable")
print("RESULT:", "PASS (physically realizable)" if infeasible_shifts == 0 and struct == 0
      else "FAIL — plan needs more moulds than exist at some shift")
