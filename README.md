# A/B Sales Analyzer

A Streamlit tool to **filter and read** A/B-test sales numbers from a CSV/Excel
export, then copy or download clean figures for your master sheet.

## What it does

- **One test at a time.** Pick a test id (e.g. `280`). The app resolves each
  row's variant within that test — including items that belong to multiple tests
  (`ab_test_name = "221|280"`, `ab_test_variant = "1|2"`) — so figures never
  double-count. Rows outside the test are excluded.
- **Filters:** test, variant, date window, exclude cancelled, final-only, item
  type, include/exclude delivery lines, payment, country, delivery type, item name.
- **Private brands** = the `PRIVATE_BRANDS` list (any product type), read from the
  authoritative `categoriesData-brand` column when present (fallback: accent-/case-
  insensitive item-name matching). The per-variant private split flags orders that
  contain a private brand regardless of type.
- **Product category** filter/dimension (Contact lenses / Solutions / Glasses / …)
  from `categoriesData-items-type` (fallback: lens detection by name). Combine it with
  the private split to get e.g. private *lenses* only. Brand list lives in
  `PRIVATE_BRANDS` in [`app.py`](app.py); `brand` and `category` dimensions are in the pivot.
- **Revenue basis:** net (`price_clean`) or gross (`price_vat`) × quantity.
- **Currency:** prices in the file are already in **CZK** and used as-is (the
  `currency` column is just an identifier); figures are labelled `Kč`.
- **Margins:** when the export includes profit columns, a **Margin basis** selector
  (Standard = `item_profit`, FIFO = `itemProfitByAccountingFifoPrice`) adds Margin,
  Margin/obj and Margin % to the views. *Note: the `item_profit` column is only valid
  when the export's sell prices and purchase prices share a currency (CZK).*
- **Three views:**
  - **Totals** — one summary row for the current filter.
  - **Per-variant** — one row per variant plus a **TOTAL** row, mirroring the manual
    evaluation sheet: Orders, **Storno** (cancelled) + % storno, Revenue, AOV, Margin,
    Margin/obj, Margin %, and % difference vs a chosen baseline. When **Private brands
    only** is checked, a **second table** appears below with the same columns computed
    over private-brand rows only.
  - **Custom pivot** — group by any columns × metrics.
- **Export:** every view has a Download CSV button (semicolon, UTF-8 BOM — Excel-friendly).

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

Upload your export, or try `sample_data/sample_sales.csv`.

## Deploy on Streamlit Cloud

1. Push to GitHub.
2. On [share.streamlit.io](https://share.streamlit.io), point a new app at this
   repo, branch `main`, file `app.py`.

## Data privacy

Real sales exports are **gitignored** and never committed. Only the synthetic
`sample_data/sample_sales.csv` lives in the repo.

## Roadmap

- Pricelist join for real margins / total prices (`compute_margin()` hook is in place).
- Adjustments per test as evaluation needs evolve.

See [`docs/superpowers/specs/`](docs/superpowers/specs/) for the design.
