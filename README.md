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
- **Product category (private cohort)** (Contact lenses / Solutions / Glasses / …)
  from `categoriesData-items-type` (fallback: lens by name). It selects *which orders*
  qualify as private in the Private-brands table (e.g. orders with a private contact
  lens) — it does **not** shrink the basket. Brand list lives in `PRIVATE_BRANDS` in
  [`app.py`](app.py); `brand` and `category` dimensions are in the pivot.
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
    Margin/obj, Margin %. When **Private brands only** is checked, a **second table**
    appears below with the same columns computed over the **full orders that contain a
    private brand** (whole basket, matching the department's "jen privátní čočky").
  - **Custom pivot** — group by any columns × metrics.
- **Export:** every view has a Download CSV button (semicolon, UTF-8 BOM — Excel-friendly).
- **VWO visitors / conversion rate (optional):** if a VWO API token is configured in
  Streamlit secrets (`vwo_token`, plus `vwo_account_id`), the Per-variant table adds
  **Visitors** and **Conv. rate %** (orders ÷ visitors) for the selected test, pulled
  from the VWO Campaign API. Campaign id = `ab_test_name`, variation id = `ab_test_variant`.
  Note: VWO visitors are campaign-wide (not split per eshop), so don't filter by a single
  project when reading conversion rate.

### VWO campaigns section

A sidebar **Section** switch toggles between the **Sales analyzer** and a **VWO campaigns**
view that works without uploading a file. It lists all campaigns (searchable by name/id),
and selecting one shows **every goal's** per-variation Visitors / Conversions / CR% (+ revenue).
Selecting **2+** campaigns puts them **side by side to compare**. The campaign list is one
cached call; each opened campaign is one more.

**Device split (desktop vs mobile + tablet).** Toggle **📱 Add device split** to pull VWO's
post-segmentation device breakdown per campaign — per variation it shows Desktop and
Mobile+Tablet Visitors, Conversions, CR% and lift% vs control (within each device). This uses
VWO's `/segment` post-segmentation endpoint with a *custom* device segment (`operator 11`,
`rOperandValue` = device list), authenticated by the same API token; the date range and primary
goal come from the campaign report. Two extra API calls per campaign (cached 1h).

## VWO secrets

Add to **Streamlit Cloud → Settings → Secrets** (and a local `.streamlit/secrets.toml`,
which is gitignored):

```toml
vwo_token = "your-vwo-api-token"   # generate at app.vwo.com/#/developers/tokens (Browse is enough)
vwo_account_id = "717496"
```

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
