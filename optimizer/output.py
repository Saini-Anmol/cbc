"""optimizer/output.py — write the CP-SAT optimizer's committed month plan to
inspectable Excel, matching the greedy engine's building/curing sheet shape so the
two plans can be compared side by side.

The rolling driver (optimizer/driver.py) accumulates the COMMITTED-days plan of
every window into two month-global row lists on the result dict:
    res['building_rows'] : {Day, Date, Shift, Machine, Group, Type, SKUCode, Inch, Qty}
        one row per (machine, sku, shift) with Qty>0.  Group S1 rows are carcass
        (from the plan's `gc`), S2/UNI rows are GT (from `g`).
    res['curing_rows']   : {Day, Date, Shift, Press, SKUCode, Inch, Qty}
        one row per PHYSICAL press (recovered from the CP-SAT count vector by
        optimizer.writer.recover_assignment), Qty = that press's share of the
        shift's cured units.

write_schedules() drops those into main_output/ as two date-stamped .xlsx files.
"""
from __future__ import annotations

import os

import pandas as pd

# ── sheet column order (mirrors the greedy building/curing Shift Schedule shape) ──
_BLD_COLS = ["Day", "Date", "Shift", "Machine", "Group", "Type", "SKUCode", "Inch", "Qty"]
_CUR_COLS = ["Day", "Date", "Shift", "Press", "SKUCode", "Inch", "Qty"]
_SHIFT_ORD = {"A": 0, "B": 1, "C": 2}


def write_schedules(res: dict, mi, out_dir: str = "main_output") -> dict:
    """Write the optimizer building + curing schedules to `out_dir` as two xlsx files.

    Args:
        res : the dict returned by driver.run_rolling — must carry 'building_rows'
              and 'curing_rows' (accumulated committed-days plan).
        mi  : ModelInputs (used for the plan-start date stamp).
        out_dir : output folder (created if absent). Default 'main_output'.

    Returns {'building': <path>, 'curing': <path>, 'n_building_rows', 'n_curing_rows'}.
    """
    os.makedirs(out_dir, exist_ok=True)
    stamp = mi.plan_start.strftime("%Y-%m-%d")

    bld_rows = list(res.get("building_rows", []) or [])
    cur_rows = list(res.get("curing_rows", []) or [])

    # ---- building schedule ----
    bld_df = pd.DataFrame(bld_rows, columns=_BLD_COLS)
    if not bld_df.empty:
        bld_df = bld_df.sort_values(
            by=["Day", "Shift", "Machine", "SKUCode"],
            key=lambda c: c.map(_SHIFT_ORD) if c.name == "Shift" else c,
        ).reset_index(drop=True)

    bld_path = os.path.join(out_dir, f"optimizer_building_schedule_{stamp}.xlsx")
    with pd.ExcelWriter(bld_path, engine="openpyxl") as w:
        bld_df.to_excel(w, sheet_name="Shift Schedule", index=False)
        _building_summary(bld_df, cur_rows).to_excel(w, sheet_name="Summary", index=False)

    # ---- curing schedule ----
    cur_df = pd.DataFrame(cur_rows, columns=_CUR_COLS)
    if not cur_df.empty:
        cur_df = cur_df.sort_values(
            by=["Day", "Shift", "Press", "SKUCode"],
            key=lambda c: c.map(_SHIFT_ORD) if c.name == "Shift" else c,
        ).reset_index(drop=True)

    cur_path = os.path.join(out_dir, f"optimizer_curing_schedule_{stamp}.xlsx")
    with pd.ExcelWriter(cur_path, engine="openpyxl") as w:
        cur_df.to_excel(w, sheet_name="Shift Schedule", index=False)
        _curing_summary(cur_df).to_excel(w, sheet_name="Summary", index=False)

    return {
        "building": bld_path,
        "curing": cur_path,
        "n_building_rows": int(len(bld_df)),
        "n_curing_rows": int(len(cur_df)),
    }


def _building_summary(bld_df: pd.DataFrame, cur_rows: list) -> pd.DataFrame:
    """Per-day GT-built / carcass-built + cured, plus a month TOTAL row."""
    cur_df = pd.DataFrame(cur_rows, columns=_CUR_COLS)
    rows: list = []
    days = sorted(set(bld_df["Day"]).union(cur_df["Day"])) if (not bld_df.empty or not cur_df.empty) else []
    for d in days:
        b = bld_df[bld_df["Day"] == d] if not bld_df.empty else bld_df
        gt = int(b[b["Type"] == "GT"]["Qty"].sum()) if not b.empty else 0
        carc = int(b[b["Type"] == "carcass"]["Qty"].sum()) if not b.empty else 0
        cured = int(cur_df[cur_df["Day"] == d]["Qty"].sum()) if not cur_df.empty else 0
        rows.append({"Day": d, "GT_Built": gt, "Carcass_Built": carc, "Cured": cured})
    tot_gt = sum(r["GT_Built"] for r in rows)
    tot_carc = sum(r["Carcass_Built"] for r in rows)
    tot_cured = sum(r["Cured"] for r in rows)
    rows.append({"Day": "TOTAL", "GT_Built": tot_gt, "Carcass_Built": tot_carc, "Cured": tot_cured})
    return pd.DataFrame(rows, columns=["Day", "GT_Built", "Carcass_Built", "Cured"])


def _curing_summary(cur_df: pd.DataFrame) -> pd.DataFrame:
    """Per-day cured units + active presses, plus a month TOTAL row."""
    rows: list = []
    if not cur_df.empty:
        for d in sorted(set(cur_df["Day"])):
            sub = cur_df[cur_df["Day"] == d]
            rows.append({"Day": d, "Cured": int(sub["Qty"].sum()),
                         "Press_Shifts": int(len(sub))})
    tot = sum(r["Cured"] for r in rows)
    tot_ps = sum(r["Press_Shifts"] for r in rows)
    rows.append({"Day": "TOTAL", "Cured": tot, "Press_Shifts": tot_ps})
    return pd.DataFrame(rows, columns=["Day", "Cured", "Press_Shifts"])
