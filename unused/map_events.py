"""
JK Tyre BTP — CBC Phase-0 DATA PREP: map raw logs to a common vocabulary
========================================================================
Why this exists
---------------
The feed map (which building machines feed which curing press) is *learned*
from history: a press and a machine are linked when they repeatedly handle the
SAME recipe. To detect that, the two raw logs must share one vocabulary.

This script normalises:

    CURING log  (CURING_PCR 1.csv) :  wcID, recipeID, dtandTime
        wcID     --WCMASTER(iD->name)-->        curing press
        recipeID --RECIPE_MASTER(iD->desc)-->   recipe code   (== pde.RecipeCode)

    BUILDING log (pde.csv)         :  workcenter, RecipeCode, DtandTime
        workcenter  ==                          building machine
        RecipeCode  ==                          recipe code   (common key)

Confirmed join key: RECIPE_MASTER.description  ==  pde.RecipeCode  (100% match).
(tyreSize / RecipeName do NOT line up across the two systems.)

Outputs (data/output/):
    curing_events_mapped.csv          dt | curing_press | recipe_code | tyre_size | recipe_name | wcID | recipeID
    building_events_mapped.csv        dt | building_machine | recipe_code | recipe_name
    recipe_bridge.csv                 recipeID | recipe_code | tyre_size | recipe_name
    wc_bridge.csv                     wcID | curing_press   (presses actually seen in the curing log)
    press_machine_shared_recipes.csv  curing_press | building_machine | shared_recipe_codes | n_shared  (feed-map preview)
    mapping_report.txt                human-readable coverage summary

Run:  ./myenv/bin/python map_events.py
"""

from __future__ import annotations

import os
from collections import defaultdict

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
IN = os.path.join(HERE, "data", "input")
OUT = os.path.join(HERE, "data", "output")
os.makedirs(OUT, exist_ok=True)

CURING_FILE = os.path.join(IN, "CURING_PCR 1.csv")
WCM_FILE = os.path.join(IN, "WCMASTER.csv")
RM_FILE = os.path.join(IN, "RECIPE_MASTER.csv")
PDE_FILE = os.path.join(IN, "pde.csv")


def _clean_code(s: pd.Series) -> pd.Series:
    """Uppercase + strip; collapse excel '123.0' floats back to '123'."""
    s = s.astype(str).str.strip()
    s = s.str.replace(r"\.0$", "", regex=True)
    return s.str.upper()


def main() -> None:
    report: list[str] = []

    def log(msg: str = "") -> None:
        print(msg)
        report.append(msg)

    log("=" * 72)
    log("  CBC PHASE-0 DATA PREP — mapping curing + building logs")
    log("=" * 72)

    # ── reference masters ────────────────────────────────────────────────
    wcm = pd.read_csv(WCM_FILE)
    wcm["iD"] = pd.to_numeric(wcm["iD"], errors="coerce").astype("Int64")
    wcm["name"] = wcm["name"].astype(str).str.strip()
    # drop placeholder rows ('Unknown', blanks)
    wcm = wcm[(wcm["name"] != "") & (wcm["name"].str.lower() != "unknown")]
    wc2name = dict(zip(wcm["iD"], wcm["name"]))
    log(f"\n[WCMASTER]  {len(wc2name)} work-centre id->name entries")

    rm = pd.read_csv(RM_FILE)
    rm["iD"] = pd.to_numeric(rm["iD"], errors="coerce").astype("Int64")
    rm["recipe_code"] = _clean_code(rm["description"])   # the common key
    rm["tyre_size"] = rm["tyreSize"].astype(str).str.strip()
    rm["recipe_name"] = rm["name"].astype(str).str.strip()
    id2code = dict(zip(rm["iD"], rm["recipe_code"]))
    id2size = dict(zip(rm["iD"], rm["tyre_size"]))
    id2rname = dict(zip(rm["iD"], rm["recipe_name"]))
    log(f"[RECIPE_MASTER]  {len(id2code)} recipeID->recipe_code entries")

    # recipe bridge out
    rm_out = rm[["iD", "recipe_code", "tyre_size", "recipe_name"]].rename(
        columns={"iD": "recipeID"}).sort_values("recipeID")
    rm_out.to_csv(os.path.join(OUT, "recipe_bridge.csv"), index=False)

    # ── CURING events ────────────────────────────────────────────────────
    cur = pd.read_csv(CURING_FILE, usecols=["wcID", "recipeID", "dtandTime"])
    n0 = len(cur)
    cur["wcID"] = pd.to_numeric(cur["wcID"], errors="coerce").astype("Int64")
    cur["recipeID"] = pd.to_numeric(cur["recipeID"], errors="coerce").astype("Int64")
    cur["dt"] = pd.to_datetime(cur["dtandTime"], errors="coerce")
    cur["curing_press"] = cur["wcID"].map(wc2name)
    cur["recipe_code"] = cur["recipeID"].map(id2code)
    cur["tyre_size"] = cur["recipeID"].map(id2size)
    cur["recipe_name"] = cur["recipeID"].map(id2rname)

    mapped = cur.dropna(subset=["curing_press", "recipe_code", "dt"]).copy()
    cur_out = mapped[["dt", "curing_press", "recipe_code", "tyre_size",
                      "recipe_name", "wcID", "recipeID"]].sort_values("dt")
    cur_out.to_csv(os.path.join(OUT, "curing_events_mapped.csv"), index=False)
    log(f"\n[CURING events]  {n0:,} rows -> {len(cur_out):,} mapped "
        f"({100*len(cur_out)/n0:.1f}%)")
    log(f"    presses seen : {cur_out['curing_press'].nunique()}")
    log(f"    recipes seen : {cur_out['recipe_code'].nunique()}")
    log(f"    date range   : {cur_out['dt'].min()}  ->  {cur_out['dt'].max()}")

    # wc bridge (only presses actually present in the curing log)
    seen_ids = sorted(mapped["wcID"].dropna().unique().tolist())
    pd.DataFrame({"wcID": seen_ids,
                  "curing_press": [wc2name.get(i) for i in seen_ids]}
                 ).to_csv(os.path.join(OUT, "wc_bridge.csv"), index=False)

    # ── BUILDING events (pde) ────────────────────────────────────────────
    pde = pd.read_csv(PDE_FILE)
    nb0 = len(pde)
    pde["building_machine"] = pde["workcenter"].astype(str).str.strip()
    pde["recipe_code"] = _clean_code(pde["RecipeCode"])
    pde["recipe_name"] = pde["RecipeName"].astype(str).str.strip()
    pde["dt"] = pd.to_datetime(pde["DtandTime"], errors="coerce")
    # one row per physical tyre: dedupe on barcode where present, else on the
    # (machine, recipe, timestamp) triple. pde logs several message-rows/event.
    key = pde["Barcode"].astype(str)
    pde["_evt"] = key.where(key.str.lower() != "nan",
                            pde["building_machine"] + "|" + pde["recipe_code"]
                            + "|" + pde["dt"].astype(str))
    bld = pde.dropna(subset=["recipe_code", "dt"])
    bld = bld[bld["recipe_code"].str.lower() != "nan"]
    bld = bld.drop_duplicates(subset=["building_machine", "recipe_code", "_evt"])
    bld_out = bld[["dt", "building_machine", "recipe_code", "recipe_name"]
                  ].sort_values("dt")
    bld_out.to_csv(os.path.join(OUT, "building_events_mapped.csv"), index=False)
    log(f"\n[BUILDING events]  {nb0:,} log rows -> {len(bld_out):,} build events "
        f"(deduped)")
    log(f"    machines seen: {bld_out['building_machine'].nunique()}")
    log(f"    recipes seen : {bld_out['recipe_code'].nunique()}")
    log(f"    date range   : {bld_out['dt'].min()}  ->  {bld_out['dt'].max()}")

    # ── feed-map PREVIEW: presses & machines sharing a recipe code ───────
    cur_pr = cur_out.groupby("recipe_code")["curing_press"].apply(set)
    bld_mc = bld_out.groupby("recipe_code")["building_machine"].apply(set)
    common = set(cur_pr.index) & set(bld_mc.index)
    log(f"\n[FEED-MAP PREVIEW]  recipe codes common to both logs: {len(common)}")
    links: dict[tuple, set] = defaultdict(set)
    for r in common:
        for p in cur_pr[r]:
            for m in bld_mc[r]:
                links[(p, m)].add(r)
    link_rows = [{"curing_press": p, "building_machine": m,
                  "n_shared": len(rs),
                  "shared_recipe_codes": ", ".join(sorted(rs))}
                 for (p, m), rs in links.items()]
    link_df = pd.DataFrame(link_rows).sort_values(
        ["curing_press", "n_shared"], ascending=[True, False])
    link_df.to_csv(os.path.join(OUT, "press_machine_shared_recipes.csv"),
                   index=False)
    log(f"    candidate press<->machine feed links: {len(link_df)}")
    if not link_df.empty:
        log("    sample links:")
        for _, r in link_df.head(8).iterrows():
            log(f"      {r['curing_press']:>10s}  <-  {r['building_machine']:<12s}"
                f"  ({r['n_shared']} shared recipe)")

    # ── write report ─────────────────────────────────────────────────────
    log("\n[Outputs] written to data/output/:")
    for f in ("curing_events_mapped.csv", "building_events_mapped.csv",
              "recipe_bridge.csv", "wc_bridge.csv",
              "press_machine_shared_recipes.csv", "mapping_report.txt"):
        log(f"    - {f}")
    with open(os.path.join(OUT, "mapping_report.txt"), "w") as f:
        f.write("\n".join(report))


if __name__ == "__main__":
    main()
