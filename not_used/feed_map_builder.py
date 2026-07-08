"""
JK Tyre BTP — CBC FEED-MAP BUILDER  (Phase 0)
=============================================
Derives the curing-press -> building-machine "feed map" from history.

Why this exists
---------------
A curing press consumes green tyres (GT) that are physically supplied by a
specific set of building machines. The plant has no documented press->machine
mapping, but it DOES log:

    * curing events    : which RECIPE was cured on which CURING PRESS, when
    * building events  : which RECIPE was built on which BUILDING MACHINE, when

A green tyre of recipe R built on machine M flows to whichever press cures R,
so co-production of the SAME recipe is evidence of a feeder link. Aggregated
over history (with exclusivity + recency weighting) this reveals, for every
curing press, the building machines that feed it.

Output
------
    feed_map.xlsx          long table: Press | Machine | Score | Rank
    feed_map_inverse.xlsx  long table: Machine | Press  (contention view)
    feed_map.json          {press: [feeder machines]}  (loaded by curing)
    feed_coverage.xlsx     validation: % of each press's cured recipes that
                           at least one chosen feeder historically built

Drop the five input files in INPUT_DIR (see FeedMapConfig), set the column
names to match your exports if they differ from the defaults, then run:

    python feed_map_builder.py
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


# ══════════════════════════════════════════════════════════════════════════
# CONFIGURATION  — edit paths / column names to match your exports
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class FeedMapConfig:
    # Folder where the input Excel/CSV files live and outputs are written.
    INPUT_DIR: str = os.path.dirname(os.path.abspath(__file__))

    # ── input file names ────────────────────────────────────────────────
    CURING_EVENTS_FILE: str = "curing_events.xlsx"      # wcID, recipe id, dt
    # One filename, or a list/tuple of filenames to concatenate (e.g. monthly
    # building-pde exports). Each must expose workcenter / recipecode / timestamp.
    BUILDING_EVENTS_FILE: "str | list | tuple" = "building_events.xlsx"
    RECIPE_MASTER_FILE: str = "recipe_master.xlsx"      # recipe id -> recipecode
    WC_MASTER_FILE: str = "wc_master.xlsx"              # wcID -> curing press code
    NAME_MAP_FILE: str | None = "building_name_map.xlsx"  # workcenter -> machine code
    #   If NAME_MAP_FILE is None the built-in S1/S2 name maps below are used.

    # ── column names (override to match your files) ─────────────────────
    #   Candidate names are tried in order; first match wins.
    CUR_WCID_COLS: tuple = ("wcID", "WCID", "WcId", "wcid", "WorkCenter")
    CUR_RECIPE_COLS: tuple = ("recipe id", "recipeId", "RecipeID", "recipe_id", "recipeID")
    CUR_TIME_COLS: tuple = ("dt", "DtAndTime", "dtAndTime", "DateTime", "Date", "Time")

    BLD_WC_COLS: tuple = ("workcenter", "WorkCenter", "Workcenter", "wc")
    BLD_RECIPE_COLS: tuple = ("recipecode", "RecipeCode", "recipeCode", "recipe code")
    BLD_TIME_COLS: tuple = ("dt", "DtAndTime", "dtAndTime", "DateTime", "Date", "Time")

    RM_RECIPE_ID_COLS: tuple = ("recipe id", "recipeId", "RecipeID", "recipe_id", "recipeID")
    RM_RECIPE_CODE_COLS: tuple = ("recipecode", "RecipeCode", "recipeCode", "recipe code")

    WCM_WCID_COLS: tuple = ("wcID", "WCID", "wcid", "WorkCenter")
    WCM_PRESS_COLS: tuple = ("curing_machine", "press", "MachineCode", "Press", "WCNAME")

    NM_WC_COLS: tuple = ("workcenter", "WorkCenter", "name", "WCNAME")
    NM_MACHINE_COLS: tuple = ("machine", "MachineCode", "machine_code", "code")

    # ── scoring parameters ──────────────────────────────────────────────
    USE_RECENCY: bool = True          # weight recent events more heavily
    RECENCY_HALFLIFE_DAYS: float = 90.0   # event weight halves every N days
    TOP_N_FEEDERS: int | None = None  # keep at most N feeders per press; None = use cutoff
    SCORE_CUTOFF_FRAC: float = 0.10   # keep feeders with score >= frac * press's best score
    MIN_SHARED_RECIPES: int = 1       # ignore press/machine pairs sharing < this many recipes

    # Output file names (written into INPUT_DIR)
    OUT_MAP: str = "feed_map.xlsx"
    OUT_INVERSE: str = "feed_map_inverse.xlsx"
    OUT_JSON: str = "feed_map.json"
    OUT_COVERAGE: str = "feed_coverage.xlsx"

    def path(self, name: str) -> str:
        return os.path.join(self.INPUT_DIR, name)


# Built-in building workcenter -> machine-code map (from the building scheduler).
# Used only when NAME_MAP_FILE is None.
BUILTIN_NAME_MAP = {
    # Stage 1
    "midland4stage1": "7804", "midland2stage1": "7802", "bj2stage1": "6802",
    "bj8stage1": "7201", "sai3stage1": "8003", "bj7stage1": "7104",
    "bj9stage1": "7105", "bj3stage1": "6803", "bj4stage1": "7101",
    "bj5stage1": "7102", "88d1stage1": "8101", "ltmstage1": "7601",
    "midland5stage1": "7701", "midland3stage1": "7803", "bj10stage1": "7106",
    "sai1stage1": "8001", "sai2stage1": "8002", "midland1stage1": "7801",
    "nrm11stage1": "6911", "nrm9stage1": "6909", "bj1stage1": "6801",
    "bj6stage1": "7103",
    # Stage 2
    "bj8": "7201", "bj7": "7104", "bj9": "7105", "vmi1": "8501", "vmi2": "8502",
    "bj4": "7101", "bj5": "7102", "newirm": "7301", "bj6": "7103", "oldirm": "8201",
    "vmi2Maxx": "7002", "gtic1": "8301", "vmi3Maxx": "7003", "gtic2": "8302",
    "us1": "7501", "us2": "7502", "bj10": "7106", "us3": "7503",
    "vmi4Maxx": "7004", "vmi1Maxx": "7001",
    "VMIExxium01": "6001", "VMIExxium02": "6002",
    "VMIExxium03": "6003", "VMIExxium04": "6004",
}


# ══════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════
def _read_any(path: str, usecols=None) -> pd.DataFrame:
    """Read an .xlsx / .xls / .csv into a DataFrame. `usecols` restricts the
    columns read — essential for very large event logs (e.g. a 2.5 GB CSV where
    only workcenter/RecipeCode/DtandTime are needed)."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Input file not found: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xls", ".xlsm"):
        return pd.read_excel(path, usecols=usecols)
    return pd.read_csv(path, usecols=usecols, low_memory=False)


def _read_header(path: str) -> pd.DataFrame:
    """Read only the header row (no data) to resolve column names cheaply."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xls", ".xlsm"):
        return pd.read_excel(path, nrows=0)
    return pd.read_csv(path, nrows=0)


def _pick_col(df: pd.DataFrame, candidates: tuple, what: str) -> str:
    """Return the first column in `candidates` present in df (case-insensitive)."""
    lower = {str(c).strip().lower(): c for c in df.columns}
    for cand in candidates:
        key = str(cand).strip().lower()
        if key in lower:
            return lower[key]
    raise KeyError(
        f"Could not find a column for {what}. Tried {candidates}; "
        f"file has {list(df.columns)}. Update the *_COLS setting in FeedMapConfig."
    )


def _recency_weights(times: pd.Series, halflife_days: float) -> np.ndarray:
    """Weight = 0.5 ** (age_in_days / halflife). Newest event ~ 1.0."""
    t = pd.to_datetime(times, errors="coerce")
    if t.notna().sum() == 0:
        return np.ones(len(times))
    newest = t.max()
    age_days = (newest - t).dt.total_seconds() / 86400.0
    age_days = age_days.fillna(age_days.max() if age_days.notna().any() else 0.0)
    return np.power(0.5, age_days.to_numpy() / max(halflife_days, 1e-6))


# ══════════════════════════════════════════════════════════════════════════
# FEED-MAP BUILDER
# ══════════════════════════════════════════════════════════════════════════
class FeedMapBuilder:
    def __init__(self, cfg: FeedMapConfig | None = None):
        self.cfg = cfg or FeedMapConfig()

    # ── load + normalise the two event logs to (recipecode, node, weight) ──
    def _load_curing(self) -> pd.DataFrame:
        cfg = self.cfg
        cpath = cfg.path(cfg.CURING_EVENTS_FILE)
        hdr = _read_header(cpath)
        wc = _pick_col(hdr, cfg.CUR_WCID_COLS, "curing wcID")
        rid = _pick_col(hdr, cfg.CUR_RECIPE_COLS, "curing recipe id")
        tcol = _pick_col(hdr, cfg.CUR_TIME_COLS, "curing timestamp")
        df = _read_any(cpath, usecols=[wc, rid, tcol])

        # recipe id -> recipecode
        rm = _read_any(cfg.path(cfg.RECIPE_MASTER_FILE))
        rm_id = _pick_col(rm, cfg.RM_RECIPE_ID_COLS, "recipe-master recipe id")
        rm_code = _pick_col(rm, cfg.RM_RECIPE_CODE_COLS, "recipe-master recipecode")
        id2code = dict(zip(rm[rm_id].astype(str).str.strip(),
                           rm[rm_code].astype(str).str.strip()))

        # wcID -> curing press code
        wcm = _read_any(cfg.path(cfg.WC_MASTER_FILE))
        wcm_id = _pick_col(wcm, cfg.WCM_WCID_COLS, "wc-master wcID")
        wcm_press = _pick_col(wcm, cfg.WCM_PRESS_COLS, "wc-master press code")
        wc2press = dict(zip(wcm[wcm_id].astype(str).str.strip(),
                            wcm[wcm_press].astype(str).str.strip()))

        out = pd.DataFrame({
            "recipecode": df[rid].astype(str).str.strip().map(id2code),
            "node": df[wc].astype(str).str.strip().map(wc2press),
            "time": df[tcol],
        })
        before = len(out)
        out = out.dropna(subset=["recipecode", "node"])
        out = out[(out["recipecode"] != "") & (out["node"] != "")]
        out["weight"] = (_recency_weights(out["time"], cfg.RECENCY_HALFLIFE_DAYS)
                         if cfg.USE_RECENCY else 1.0)
        print(f"  [Curing] {before} events -> {len(out)} after mapping "
              f"| presses={out['node'].nunique()} | recipes={out['recipecode'].nunique()}")
        return out

    def _load_building(self) -> pd.DataFrame:
        cfg = self.cfg
        # Accept a single filename or a list/tuple of files (monthly exports).
        files = cfg.BUILDING_EVENTS_FILE
        if isinstance(files, (str, bytes)):
            files = [files]
        parts = []
        for fname in files:
            bpath = cfg.path(fname)
            hdr = _read_header(bpath)
            wc = _pick_col(hdr, cfg.BLD_WC_COLS, "building workcenter")
            rc = _pick_col(hdr, cfg.BLD_RECIPE_COLS, "building recipecode")
            tcol = _pick_col(hdr, cfg.BLD_TIME_COLS, "building timestamp")
            d = _read_any(bpath, usecols=[wc, rc, tcol])
            # standardise to fixed names so files with differing headers concat cleanly
            d = d.rename(columns={wc: "_wc", rc: "_rc", tcol: "_t"})
            parts.append(d[["_wc", "_rc", "_t"]])
            print(f"  [Building] {os.path.basename(bpath)}: {len(d):,} rows")
        df = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]
        wc, rc, tcol = "_wc", "_rc", "_t"

        # workcenter -> machine code
        if cfg.NAME_MAP_FILE and os.path.exists(cfg.path(cfg.NAME_MAP_FILE)):
            nm = _read_any(cfg.path(cfg.NAME_MAP_FILE))
            nm_wc = _pick_col(nm, cfg.NM_WC_COLS, "name-map workcenter")
            nm_m = _pick_col(nm, cfg.NM_MACHINE_COLS, "name-map machine code")
            name_map = dict(zip(nm[nm_wc].astype(str).str.strip(),
                                nm[nm_m].astype(str).str.strip()))
            print(f"  [NameMap] loaded {len(name_map)} workcenter->machine entries from file")
        else:
            name_map = BUILTIN_NAME_MAP
            print(f"  [NameMap] using built-in S1/S2 map ({len(name_map)} entries)")

        def resolve(w):
            w = str(w).strip()
            # try direct, then mapped
            return name_map.get(w, w if w.isdigit() else None)

        out = pd.DataFrame({
            "recipecode": df[rc].astype(str).str.strip(),
            "node": df[wc].map(resolve),
            "time": df[tcol],
        })
        before = len(out)
        out = out.dropna(subset=["recipecode", "node"])
        out = out[(out["recipecode"] != "") & (out["node"] != "")]
        out["weight"] = (_recency_weights(out["time"], cfg.RECENCY_HALFLIFE_DAYS)
                         if cfg.USE_RECENCY else 1.0)
        print(f"  [Building] {before} events -> {len(out)} after mapping "
              f"| machines={out['node'].nunique()} | recipes={out['recipecode'].nunique()}")
        return out

    # ── core scoring ────────────────────────────────────────────────────
    def build(self) -> dict:
        print("\n[Phase 0] Building FEED map from event history...")
        cur = self._load_curing()
        bld = self._load_building()

        # weighted volume per (recipe, press) and (recipe, machine)
        cure_vol = (cur.groupby(["recipecode", "node"])["weight"].sum()
                       .to_dict())   # {(recipe, press): w}
        bld_vol = (bld.groupby(["recipecode", "node"])["weight"].sum()
                      .to_dict())    # {(recipe, machine): w}

        presses_by_recipe = defaultdict(dict)   # recipe -> {press: w}
        for (r, p), w in cure_vol.items():
            presses_by_recipe[r][p] = w
        machines_by_recipe = defaultdict(dict)  # recipe -> {machine: w}
        for (r, m), w in bld_vol.items():
            machines_by_recipe[r][m] = w

        # exclusivity-weighted co-occurrence score
        score = defaultdict(lambda: defaultdict(float))   # press -> machine -> score
        shared = defaultdict(lambda: defaultdict(int))    # press -> machine -> #recipes
        shared_recipes = set(presses_by_recipe) & set(machines_by_recipe)
        for r in shared_recipes:
            presses = presses_by_recipe[r]
            machines = machines_by_recipe[r]
            excl = 1.0 / (len(presses) * len(machines))   # rare recipe => strong signal
            for p, pw in presses.items():
                for m, mw in machines.items():
                    score[p][m] += excl * min(pw, mw)
                    shared[p][m] += 1

        # select feeders per press
        feed_map: dict[str, list[str]] = {}
        rows = []
        for p in sorted(score):
            ranked = sorted(score[p].items(), key=lambda kv: -kv[1])
            ranked = [(m, s) for m, s in ranked
                      if shared[p][m] >= self.cfg.MIN_SHARED_RECIPES]
            if not ranked:
                feed_map[p] = []
                continue
            best = ranked[0][1]
            kept = []
            for rank, (m, s) in enumerate(ranked, 1):
                keep = (s >= self.cfg.SCORE_CUTOFF_FRAC * best)
                if self.cfg.TOP_N_FEEDERS is not None:
                    keep = keep and rank <= self.cfg.TOP_N_FEEDERS
                rows.append({
                    "Press": p, "Machine": m,
                    "Score": round(s, 4),
                    "Shared_Recipes": shared[p][m],
                    "Rank": rank,
                    "Kept": bool(keep),
                })
                if keep:
                    kept.append(m)
            feed_map[p] = kept

        df_map = pd.DataFrame(rows)
        self._export(feed_map, df_map, presses_by_recipe, machines_by_recipe)
        return feed_map

    # ── outputs + validation ──────────────────────────────────────────────
    def _export(self, feed_map, df_map, presses_by_recipe, machines_by_recipe):
        cfg = self.cfg

        # kept-only map file
        kept = df_map[df_map["Kept"]].copy() if not df_map.empty else df_map
        kept.to_excel(cfg.path(cfg.OUT_MAP), index=False)

        # inverse map (machine -> presses) for contention
        inv = defaultdict(set)
        for p, machines in feed_map.items():
            for m in machines:
                inv[m].add(p)
        inv_rows = [{"Machine": m, "Press": p, "Feeds_N_Presses": len(ps)}
                    for m, ps in inv.items() for p in sorted(ps)]
        pd.DataFrame(inv_rows).to_excel(cfg.path(cfg.OUT_INVERSE), index=False)

        # json for the curing scheduler
        with open(cfg.path(cfg.OUT_JSON), "w") as f:
            json.dump({p: sorted(ms) for p, ms in feed_map.items()}, f, indent=2)

        # coverage validation: of each press's cured recipes, what fraction
        # has >=1 chosen feeder that historically built that recipe?
        cov_rows = []
        for p, machines in feed_map.items():
            recipes_here = [r for r, pr in presses_by_recipe.items() if p in pr]
            if not recipes_here:
                continue
            covered = 0
            for r in recipes_here:
                builders = set(machines_by_recipe.get(r, {}))
                if builders & set(machines):
                    covered += 1
            cov_rows.append({
                "Press": p,
                "Feeders": len(machines),
                "Cured_Recipes": len(recipes_here),
                "Recipes_Covered": covered,
                "Coverage_Pct": round(100.0 * covered / len(recipes_here), 1),
            })
        df_cov = pd.DataFrame(cov_rows).sort_values("Coverage_Pct")
        df_cov.to_excel(cfg.path(cfg.OUT_COVERAGE), index=False)

        n_press = len(feed_map)
        avg_feeders = np.mean([len(m) for m in feed_map.values()]) if feed_map else 0
        avg_cov = df_cov["Coverage_Pct"].mean() if not df_cov.empty else 0
        low_cov = int((df_cov["Coverage_Pct"] < 80).sum()) if not df_cov.empty else 0
        contended = sum(1 for ps in inv.values() if len(ps) > 1)
        print(f"\n  [FEED map] presses={n_press} | avg feeders/press={avg_feeders:.1f} "
              f"| shared(contended) machines={contended}")
        print(f"  [Validate] avg coverage={avg_cov:.1f}% | presses < 80% coverage={low_cov}")
        if low_cov:
            print("    -> low-coverage presses may need a higher feeder count "
                  "(raise TOP_N_FEEDERS / lower SCORE_CUTOFF_FRAC) or the FEED map "
                  "is genuinely missing a feeder. Check feed_coverage.xlsx.")
        print(f"  [Export] {cfg.OUT_MAP} | {cfg.OUT_INVERSE} | {cfg.OUT_JSON} | {cfg.OUT_COVERAGE}")


def build_feed_map(cfg: FeedMapConfig | None = None) -> dict:
    """Convenience entry point. Returns {press: [feeder machines]}."""
    return FeedMapBuilder(cfg).build()


if __name__ == "__main__":
    build_feed_map()
