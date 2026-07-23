"""scratch_inch_audit.py <building_schedule.xlsx>
Audit the client inch rules against a produced building schedule.

Rule 1a  no machine may return to an inch it has already left
Rule 2   every inch a machine runs must be within anchor +/- BAND
         (anchor = the inch of the machine's FIRST assignment)

Also reports the diff-size CO count, which is what the client wanted reduced.
Temporary verification harness (not part of the deployment).
"""
import sys
import pandas as pd

BAND = 2
path = sys.argv[1]
band = int(sys.argv[2]) if len(sys.argv) > 2 else BAND

bs = pd.read_excel(path, sheet_name="Shift Schedule", header=2)

# Real production/CO rows only — CHANGEOVER sentinel rows carry no SKU, and
# Stage-1 carcass rows are not GT inch work.
bs = bs[bs["SKUCode"].astype(str).str.match(r"^\d")].copy()
bs["inch"] = bs["SKUCode"].astype(str).str[8:10]
bs = bs[bs["inch"].str.isdigit()]
bs["Date"] = pd.to_datetime(bs["Date"])
order = {"A": 0, "B": 1, "C": 2}
bs["_o"] = bs["Shift"].map(order).fillna(0)
bs = bs.sort_values(["Machine", "Date", "_o"])

viol_band, viol_revisit, machines = [], [], 0
for m, g in bs.groupby("Machine"):
    machines += 1
    seq = []
    for i in g["inch"]:
        if not seq or seq[-1] != i:
            seq.append(i)          # collapse consecutive repeats -> inch runs
    anchor = int(seq[0])
    seen = set()
    for idx, i in enumerate(seq):
        n = int(i)
        if abs(n - anchor) > band:
            viol_band.append((m, anchor, i))
        if i in seen:
            viol_revisit.append((m, i, "->".join(seq)))
        seen.add(i)

co = pd.read_excel(path, sheet_name="Shift Schedule", header=2)["CO_Type"].value_counts()
print(f"machines audited      : {machines}")
print(f"Rule 2 band violations: {len(viol_band)}   (anchor +/- {band})")
for v in viol_band[:8]:
    print(f"    machine {v[0]}: anchor {v[1]}\" ran {v[2]}\"")
print(f"Rule 1a revisits      : {len(viol_revisit)}")
for v in viol_revisit[:8]:
    print(f"    machine {v[0]}: returned to {v[1]}\"  seq={v[2]}")
print(f"same_size_CO={int(co.get('same_size_CO',0))}  diff_size_CO={int(co.get('diff_size_CO',0))}")
print("RESULT:", "PASS" if not viol_band and not viol_revisit else "FAIL")
