"""
Download SAP production data for a date range, Plant 1300, filtered on
Mtart = ZFGS. Produces a single combined XLSX with columns:
    ProdDate, SKUCode, Shift, Timestamp, ProdQty
ProdQty is a floored integer, summed per SKUCode per Shift for each date.
Timestamp is ESTIMATED from the shift (A=07:00, B=15:00, C=23:00) because the
SAP API does not return a real per-record production time.

Run:  python sap_production_data.py
Requires connectivity to s4api.sap.jktyre.in:44305 (corporate network / VPN).
"""

import math
import os
import sys
import urllib3
from datetime import datetime, timedelta

import pandas as pd
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

URL = ("https://s4api.sap.jktyre.in:44305/"
       "sap/opu/odata/sap/ZINFI_PLT_API_SRV/InfiProdDtlSet")
AUTH = ("INFI_CON", "Welcome@12345678")

# Output goes into a "pord_output" sub-folder next to wherever the script is run.
OUT = "pord_output"
os.makedirs(OUT, exist_ok=True)
# Date range to pull (both inclusive). Set these to cover the full month.
START_DATE = "2026-07-01"
END_DATE = "2026-07-31"

OUT_COLS = ["ProdDate", "SKUCode", "Shift", "Timestamp", "ProdQty"]

# Estimated shift-start time (the 7AM-7AM production day: A 07-15, B 15-23, C 23-07).
# NOTE: this is an ESTIMATE from the shift, not the real production timestamp,
# because the SAP API does not return a per-record time.
SHIFT_START = {"A": "07:00:00", "B": "15:00:00", "C": "23:00:00"}


def date_range(start, end):
    """Return all dates from start to end (inclusive) as 'YYYY-MM-DD' strings."""
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    if e < s:
        raise ValueError(f"END_DATE ({end}) is before START_DATE ({start}).")
    return [(s + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range((e - s).days + 1)]


DATES = date_range(START_DATE, END_DATE)


def floor_qty(value):
    """Convert a SAP quantity (e.g. '560.000') to a floored integer."""
    try:
        return int(math.floor(float(value)))
    except (TypeError, ValueError):
        return 0


def fetch(date_str):
    """Fetch ZFGS production records for a single date; return list of dicts."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    sap_date = dt.strftime("%Y%m%d")  # 20260801

    params = {
        "$filter": (
            "Plant eq '1300' "
            f"and PFrDt eq '{sap_date}' "
            f"and PToDt eq '{sap_date}' "
            "and Matnr eq ' ' "
            "and Mtart eq 'ZFGS' "
            "and Matkl eq ' ' "
            "and StorgLoc eq ' ' "
            "and Arbpl eq ' ' "
            "and PType eq 'PD'"
        ),
        "$format": "json",
    }

    r = requests.get(URL, params=params, headers={"Accept": "application/json"},
                     auth=AUTH, verify=False, timeout=300)
    print(f"{date_str}: HTTP {r.status_code}")
    r.raise_for_status()

    results = r.json().get("d", {}).get("results", [])
    print(f"{date_str}: {len(results)} raw records")
    return results


def to_rows(date_str, results):
    """Filter to ZFGS and reduce each record to ProdDate, SKUCode, Shift, ProdQty."""
    rows = []
    for rec in results:
        if str(rec.get("Mtart", "")).strip().upper() != "ZFGS":
            continue
        rows.append({
            "ProdDate": date_str,
            "SKUCode": rec.get("Matnr"),
            "Shift": str(rec.get("Shift", "")).strip().upper(),
            "ProdQty": floor_qty(rec.get("ProdQty")),
        })
    return rows


def main():
    all_rows = []
    for d in DATES:
        try:
            results = fetch(d)
        except requests.exceptions.RequestException as exc:
            print(
                f"\nERROR: could not reach SAP for {d} -> {exc.__class__.__name__}.\n"
                "Check that you are on the corporate network / VPN and that "
                "s4api.sap.jktyre.in:44305 is reachable.",
                file=sys.stderr,
            )
            sys.exit(1)
        rows = to_rows(d, results)
        print(f"{d}: {len(rows)} rows after ZFGS filter")
        all_rows.extend(rows)

    if not all_rows:
        print("\nNo data returned for the given date range.")
        return

    df = pd.DataFrame(all_rows)
    df["ProdQty"] = df["ProdQty"].astype(int)

    # Sum ProdQty per SKUCode per Shift for each date.
    df = (df.groupby(["ProdDate", "SKUCode", "Shift"], as_index=False)["ProdQty"]
            .sum())

    # Estimated production timestamp = ProdDate + shift-start time.
    df["Timestamp"] = pd.to_datetime(
        df["ProdDate"] + " " + df["Shift"].map(SHIFT_START).fillna("00:00:00"),
        errors="coerce")

    df = df[OUT_COLS].sort_values(["ProdDate", "Shift", "SKUCode"], ignore_index=True)

    first = datetime.strptime(DATES[0], "%Y-%m-%d").strftime("%d%b%Y")
    last = datetime.strptime(DATES[-1], "%Y-%m-%d").strftime("%d%b%Y")
    out_path = os.path.join(OUT, f"sap_prod_{first}-{last}.xlsx")
    df.to_excel(out_path, index=False)

    print(f"\nCombined rows: {len(df)}")
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
