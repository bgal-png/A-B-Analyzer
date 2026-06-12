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
- **Private brands only.** A checkbox restricts results to our private brands,
  matched against the item name with accent-/case-/separator-insensitive logic
  (so `Laim Care`, `Laim-Care`, `laimcare` all match). A derived `brand` column is
  available in the Custom pivot to count/sum per brand. Brand list lives in
  `PRIVATE_BRANDS` in [`app.py`](app.py).
- **Revenue basis:** net (`price_clean`) or gross (`price_vat`) × quantity.
- **Three views:**
  - **Totals** — one summary row for the current filter.
  - **Per-variant** — one row per variant with absolute + % difference vs a chosen baseline.
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
