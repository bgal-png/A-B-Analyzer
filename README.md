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
- **Private brands only.** A checkbox restricts results to our private brands.
  Brand is read from the authoritative `categoriesData-brand` column when the export
  has it (falling back to accent-/case-insensitive item-name matching otherwise), and
  the "Pouze čočky" private split uses `categoriesData-items-type == "Contact lenses"`
  (fallback: commonName contains `čoč`). Brand list lives in `PRIVATE_BRANDS` in
  [`app.py`](app.py); a `brand` dimension is available in the Custom pivot.
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
    evaluation sheet: Orders, **Storno** (cancelled) + % storno, Revenue, AOV, and a
    private-brand split (orders with/without a private-brand contact lens, % and AOV
    each). % difference vs a chosen baseline. *Private split uses the "Pouze čočky"
    rule and is pending calibration against the exact privátka definition.*
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
