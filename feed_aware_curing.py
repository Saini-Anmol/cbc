"""
JK Tyre BTP — FEED-AWARE CURING FILTER  (Phase 1 / the C in C->B->C)
===================================================================
Adds building-feed awareness to the existing curing LP scheduler
(curing_lp.py) WITHOUT rewriting it.

The rule (Option A)
-------------------
For a curing press that needs a CHANGEOVER, only allow SKUs whose feeding
building machines (from the FEED map) can actually build them. Continuity
presses are exempt — they are already running, so their feed already exists.

How it plugs in
---------------
`install_feed_awareness()` monkeypatches
`MouldTracker.get_eligible_machines_with_moulds` in curing_lp.py. That method
already decides which presses a SKU may run on (mould + continuity logic); we
wrap it so the result is additionally intersected with "presses whose feeders
can build this SKU". Net effect: the LP can never assign a SKU to a press its
building machines can't feed -> the structural starvation cases disappear.

Usage
-----
    import curing_lp
    from feed_aware_curing import load_feed_map, load_building_allowable, install_feed_awareness

    feed_map = load_feed_map("feed_map.json")
    bld_allow = load_building_allowable("building_allowable.xlsx")  # SKU -> [machines]
    install_feed_awareness(curing_lp, feed_map, bld_allow)

    curing_lp.run_from_excel(...)        # now feed-aware
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import pandas as pd


# ══════════════════════════════════════════════════════════════════════════
# LOADERS
# ══════════════════════════════════════════════════════════════════════════
def load_feed_map(path: str) -> dict[str, set[str]]:
    """
    Load {press: [feeder building machines]} from feed_map.json (preferred)
    or feed_map.xlsx (columns: Press, Machine). Keys/values normalised to str.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"FEED map not found: {path} "
                                f"(run feed_map_builder.py first)")
    ext = os.path.splitext(path)[1].lower()
    fm: dict[str, set[str]] = {}
    if ext == ".json":
        with open(path) as f:
            raw = json.load(f)
        for p, machines in raw.items():
            fm[str(p).strip()] = {str(m).strip() for m in machines}
    else:
        df = pd.read_excel(path)
        pc = "Press" if "Press" in df.columns else df.columns[0]
        mc = "Machine" if "Machine" in df.columns else df.columns[1]
        if "Kept" in df.columns:
            df = df[df["Kept"].astype(bool)]
        for p, grp in df.groupby(pc):
            fm[str(p).strip()] = {str(m).strip() for m in grp[mc]}
    print(f"  [FeedAware] loaded FEED map: {len(fm)} presses, "
          f"{sum(len(v) for v in fm.values())} feeder links")
    return fm


def load_building_allowable(path: str,
                            sku_col_candidates=("SKUCode", "SKU Code", "Sapcode"),
                            mach_col_candidates=("Machines", "machines")) -> dict[str, set[str]]:
    """
    Load SKU -> set(building machines) from an Excel produced by the building
    ETL (load_machine_allowable). `Machines` may be a python-list string like
    "['6802', '7101']" or a real list.
    """
    import ast
    df = pd.read_excel(path)
    sku_col = next((c for c in sku_col_candidates if c in df.columns), df.columns[0])
    mach_col = next((c for c in mach_col_candidates if c in df.columns), None)
    out: dict[str, set[str]] = {}
    if mach_col is not None:
        for _, r in df.iterrows():
            raw = r[mach_col]
            if isinstance(raw, str):
                try:
                    raw = ast.literal_eval(raw)
                except (ValueError, SyntaxError):
                    raw = [x for x in raw.replace("[", "").replace("]", "").split(",") if x]
            machines = {str(m).strip().strip("'\"") for m in (raw or [])}
            out[str(r[sku_col]).strip()] = {m for m in machines if m}
    else:
        # wide matrix form: SKU column + one column per machine with Y/Yes/1
        mcols = [c for c in df.columns if str(c).strip().isdigit()]
        for _, r in df.iterrows():
            machines = {str(c).strip() for c in mcols
                        if str(r[c]).strip().lower() in {"y", "yes", "1", "true"}}
            out[str(r[sku_col]).strip()] = machines
    print(f"  [FeedAware] loaded building-allowable for {len(out)} SKUs")
    return out


# ══════════════════════════════════════════════════════════════════════════
# THE FILTER
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class FeedAwareFilter:
    feed_map: dict[str, set[str]]            # press -> feeder building machines
    building_allowable: dict[str, set[str]]  # SKU   -> building machines that can build it
    # If a press has no FEED entry we cannot prove infeasibility -> keep it.
    keep_unknown_press: bool = True
    # If a SKU has no building-allowable entry we cannot prove infeasibility -> keep it.
    keep_unknown_sku: bool = True

    @staticmethod
    def _norm(x) -> str:
        s = str(x).strip()
        # building/curing codes are sometimes 4401.0 from excel float coercion
        return s[:-2] if s.endswith(".0") else s

    def feeders(self, press) -> set[str]:
        # feed_map values may be list or set (build_feed_map returns lists);
        # coerce so set ops in can_feed() work regardless.
        return set(self.feed_map.get(self._norm(press), ()))

    def can_feed(self, sku, press) -> bool:
        """True if at least one feeder of `press` can build `sku`."""
        feeders = self.feeders(press)
        if not feeders:
            return self.keep_unknown_press
        builders = self.building_allowable.get(self._norm(sku))
        if builders is None:
            return self.keep_unknown_sku
        return bool(feeders & {self._norm(b) for b in builders})

    def filter_presses(self, sku, candidate_presses, continuity_presses=None) -> list:
        """
        Keep candidate presses that are either (a) continuity for this run, or
        (b) feed-feasible for `sku`. Preserves the input ordering/type.
        """
        cont = set()
        for c in (continuity_presses or []):
            cont.add(c)
            cont.add(self._norm(c))
        out = []
        for m in candidate_presses:
            if m in cont or self._norm(m) in cont:
                out.append(m)
            elif self.can_feed(sku, m):
                out.append(m)
        return out


# ══════════════════════════════════════════════════════════════════════════
# INTEGRATION — monkeypatch into curing_lp.MouldTracker
# ══════════════════════════════════════════════════════════════════════════
def install_feed_awareness(curing_module, feed_map, building_allowable,
                           keep_unknown_press: bool = True,
                           keep_unknown_sku: bool = True) -> FeedAwareFilter:
    """
    Wrap MouldTracker.get_eligible_machines_with_moulds so its result is
    additionally filtered to feed-feasible presses. Idempotent (safe to call
    once per process). Returns the installed FeedAwareFilter.
    """
    flt = FeedAwareFilter(
        feed_map=feed_map,
        building_allowable=building_allowable,
        keep_unknown_press=keep_unknown_press,
        keep_unknown_sku=keep_unknown_sku,
    )
    MouldTracker = curing_module.MouldTracker

    if getattr(MouldTracker, "_feed_aware_installed", False):
        # update the active filter in place and return
        MouldTracker._feed_filter = flt
        print("  [FeedAware] filter refreshed on already-patched MouldTracker")
        return flt

    original = MouldTracker.get_eligible_machines_with_moulds

    def patched(self, sku, candidate_machines, continuity_machines=None):
        base = original(self, sku, candidate_machines, continuity_machines)
        active = getattr(MouldTracker, "_feed_filter", flt)
        kept = active.filter_presses(sku, base, continuity_machines)
        # Safety net: never let the feed filter strip a continuity press to []
        if not kept and continuity_machines:
            cont = {active._norm(c) for c in continuity_machines}
            kept = [m for m in base if active._norm(m) in cont]
        return kept

    patched.__name__ = "get_eligible_machines_with_moulds"
    MouldTracker._feed_original = original
    MouldTracker.get_eligible_machines_with_moulds = patched
    MouldTracker._feed_aware_installed = True
    MouldTracker._feed_filter = flt
    print("  [FeedAware] installed feed-aware eligibility on MouldTracker "
          f"(unknown press={'keep' if keep_unknown_press else 'drop'}, "
          f"unknown sku={'keep' if keep_unknown_sku else 'drop'})")
    return flt


def uninstall_feed_awareness(curing_module) -> None:
    """Restore original eligibility (for A/B comparison runs)."""
    MouldTracker = curing_module.MouldTracker
    if getattr(MouldTracker, "_feed_aware_installed", False):
        orig = getattr(MouldTracker, "_feed_original", None)
        if orig is not None:
            MouldTracker.get_eligible_machines_with_moulds = orig
        MouldTracker._feed_aware_installed = False
        print("  [FeedAware] uninstalled")
