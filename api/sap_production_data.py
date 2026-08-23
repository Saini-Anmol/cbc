"""
SAP actual-production fetch — Plant 1300, Mtart = ZFGS (finished/cured tyres).

Two ways to use it:

  • As a library (the pipeline path — `local_main` / `main` call this live):
        from api.sap_production_data import production_by_sku, fetch_production
        prod = production_by_sku("2026-08-01", "2026-08-20")   # {SKUCode: cured_qty}
        df   = fetch_production("2026-08-01", "2026-08-20")     # per-(date, SKU) rows

  • As a script (writes a combined xlsx):
        python api/sap_production_data.py [START_DATE END_DATE] [OUT_DIR]

Requires connectivity to s4api.sap.jktyre.in:44305 (corporate network / VPN).
Credentials come from env (SAP_USER / SAP_PASS); the committed fallback keeps the
standalone script working. Move the real password to the repo .env for production.
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
# Credentials: env first (SAP_USER / SAP_PASS), fallback to the committed pair so the
# standalone script still runs. TODO: keep the real password only in .env.
AUTH = (os.environ.get("SAP_USER", "INFI_CON"),
        os.environ.get("SAP_PASS", "Welcome@12345678"))

PLANT = "1300"
MTART = "ZFGS"
OUT_COLS = ["ProdDate", "SKUCode", "ProdQty"]
DEFAULT_OUT = "pord_output"


def _date_range(start, end):
    """All dates start..end inclusive as 'YYYY-MM-DD' strings."""
    s = datetime.strptime(str(start)[:10], "%Y-%m-%d")
    e = datetime.strptime(str(end)[:10], "%Y-%m-%d")
    if e < s:
        raise ValueError(f"END_DATE ({end}) is before START_DATE ({start}).")
    return [(s + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range((e - s).days + 1)]


def _floor_qty(value):
    """SAP quantity (e.g. '560.000') → floored int; 0 on bad input."""
    try:
        return int(math.floor(float(value)))
    except (TypeError, ValueError):
        return 0


def _fetch_one(date_str, plant=PLANT, mtart=MTART, timeout=300):
    """Fetch one date's ZFGS records; return list of dicts. Raises on transport error."""
    sap_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y%m%d")
    params = {
        "$filter": (
            f"Plant eq '{plant}' "
            f"and PFrDt eq '{sap_date}' and PToDt eq '{sap_date}' "
            "and Matnr eq ' ' "
            f"and Mtart eq '{mtart}' "
            "and Matkl eq ' ' and StorgLoc eq ' ' and Arbpl eq ' ' and PType eq 'PD'"
        ),
        "$format": "json",
    }
    r = requests.get(URL, params=params, headers={"Accept": "application/json"},
                     auth=AUTH, verify=False, timeout=timeout)
    print(f"  [SAP] {date_str}: HTTP {r.status_code}")
    r.raise_for_status()
    return r.json().get("d", {}).get("results", [])


def fetch_production(start_date, end_date, plant=PLANT, mtart=MTART) -> pd.DataFrame:
    """Actual production for a date range → DataFrame[ProdDate, SKUCode, ProdQty]
    (ProdQty floored int, summed per (date, SKU)). SKUCode is stripped so it joins the
    demand file's SKUCode. Raises RuntimeError if SAP is unreachable."""
    rows = []
    for d in _date_range(start_date, end_date):
        try:
            results = _fetch_one(d, plant, mtart)
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(
                f"[SAP] could not reach {URL} for {d} ({exc.__class__.__name__}). "
                "Check corporate network / VPN to s4api.sap.jktyre.in:44305.") from exc
        for rec in results:
            if str(rec.get("Mtart", "")).strip().upper() != mtart:
                continue
            rows.append({"ProdDate": d,
                         "SKUCode": str(rec.get("Matnr", "")).strip(),
                         "ProdQty": _floor_qty(rec.get("ProdQty"))})
    if not rows:
        return pd.DataFrame(columns=OUT_COLS)
    df = pd.DataFrame(rows, columns=OUT_COLS)
    df["ProdQty"] = df["ProdQty"].astype(int)
    return (df.groupby(["ProdDate", "SKUCode"], as_index=False)["ProdQty"].sum()
              .sort_values(["ProdDate", "SKUCode"], ignore_index=True))


def production_by_sku(start_date, end_date) -> dict:
    """Actual CURED production summed over [start_date, end_date] per SKU → {SKUCode: qty}.
    This is what the mid-month demand-deduction consumes (original demand − this)."""
    df = fetch_production(start_date, end_date)
    if df.empty:
        return {}
    g = df.groupby("SKUCode")["ProdQty"].sum()
    return {str(k).strip(): int(v) for k, v in g.items() if str(k).strip()}


def write_production_file(df: pd.DataFrame, out_dir=DEFAULT_OUT) -> str:
    """Write the per-(date, SKU) production DataFrame to <out_dir>/sap_prod_<range>.xlsx."""
    os.makedirs(out_dir, exist_ok=True)
    _d = df["ProdDate"]
    first = datetime.strptime(_d.min(), "%Y-%m-%d").strftime("%d%b%Y")
    last = datetime.strptime(_d.max(), "%Y-%m-%d").strftime("%d%b%Y")
    path = os.path.join(out_dir, f"sap_prod_{first}-{last}.xlsx")
    df.to_excel(path, index=False)
    return path


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    start = argv[0] if len(argv) > 0 else "2026-08-01"
    end = argv[1] if len(argv) > 1 else "2026-08-20"
    out_dir = argv[2] if len(argv) > 2 else DEFAULT_OUT
    try:
        df = fetch_production(start, end)
    except RuntimeError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    if df.empty:
        print("\nNo data returned for the given date range.")
        return
    path = write_production_file(df, out_dir)
    print(f"\nCombined rows: {len(df)}  |  total ProdQty: {int(df['ProdQty'].sum()):,}")
    print(f"Saved: {path}")


if __name__ == "__main__":
    main()
