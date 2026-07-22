"""Trim a big sales export down to what the A/B Sales Analyzer actually needs.

Run this LOCALLY (on your own machine) on a large export before uploading to the
Streamlit Cloud app. It streams the file in chunks (low memory) and writes a much
smaller CSV keeping only the ~two dozen columns the analyzer uses — and can also
split the export into one file per project (eshop), which is usually tiny.

Usage:
    python trim_export.py sells-29513.csv                 # → sells-29513-trimmed.csv (columns only)
    python trim_export.py sells-29513.csv --project 87    # keep only ref_projects 87 (videt.ro)
    python trim_export.py sells-29513.csv --split         # one file per project: sells-29513-<pid>.csv
    python trim_export.py sells-29513.csv --out small.csv  # custom output name

The trimmed file loads on Streamlit Cloud where the full one won't.
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

# Keep in sync with USED_COLUMNS in app.py (+ customerIpAddress). Any column whose
# name contains "mail" (the customer email) is also kept, for the team-order filter.
KEEP = {
    "ref_projects", "orderId", "ordertimestamp", "price_clean", "price_vat", "amount",
    "ab_test_name", "ab_test_variant", "orderstatecancel", "orderstatefinal",
    "orderItemType", "projectItemId", "itemname", "commonName", "payment",
    "orderDestinationCountryId", "delivery_type", "orderMonth", "orderDay",
    "categoriesData-brand", "categoriesData-items-type", "customerIpAddress",
    "item_profit", "itemProfitByAccountingFifoPrice",
}
CHUNK = 200_000


def wanted(col: str) -> bool:
    c = str(col).strip()
    return c in KEEP or "mail" in c.lower()


def main() -> None:
    ap = argparse.ArgumentParser(description="Trim a sales export to the analyzer's columns.")
    ap.add_argument("input", help="path to the full export CSV (semicolon-delimited)")
    ap.add_argument("--project", help="keep only rows with this ref_projects id (e.g. 87)")
    ap.add_argument("--split", action="store_true", help="write one file per ref_projects id")
    ap.add_argument("--out", help="output path (default: <input>-trimmed.csv)")
    a = ap.parse_args()

    if not os.path.exists(a.input):
        sys.exit(f"File not found: {a.input}")
    base, _ = os.path.splitext(a.input)
    reader = pd.read_csv(a.input, sep=";", dtype=str, usecols=wanted,
                         encoding="utf-8-sig", keep_default_na=False, chunksize=CHUNK)

    if a.split:
        written: set[str] = set()
        rows = 0
        for chunk in reader:
            for pid, grp in chunk.groupby("ref_projects"):
                path = f"{base}-{pid}.csv"
                grp.to_csv(path, sep=";", index=False, mode="a" if path in written else "w",
                           header=path not in written, encoding="utf-8-sig")
                written.add(path)
            rows += len(chunk)
        print(f"Split {rows:,} rows into {len(written)} project file(s):")
        for p in sorted(written):
            print(f"  {p}  ({os.path.getsize(p) / 1e6:.0f} MB)")
        return

    out = a.out or f"{base}-trimmed.csv"
    rows_in = rows_out = 0
    first = True
    for chunk in reader:
        rows_in += len(chunk)
        if a.project:
            chunk = chunk[chunk["ref_projects"] == a.project]
        if chunk.empty:
            continue
        chunk.to_csv(out, sep=";", index=False, mode="w" if first else "a",
                     header=first, encoding="utf-8-sig")
        rows_out += len(chunk)
        first = False
    if first:
        sys.exit("No rows matched — check the --project id.")
    src_mb, out_mb = os.path.getsize(a.input) / 1e6, os.path.getsize(out) / 1e6
    print(f"Wrote {out}")
    print(f"  rows: {rows_in:,} -> {rows_out:,} | size: {src_mb:.0f} MB -> {out_mb:.0f} MB "
          f"({out_mb / src_mb:.0%} of original)")


if __name__ == "__main__":
    main()
