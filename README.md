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
- **Cap orders per IP** (default on when the export has `customerIpAddress`): counts at most
  **N orders per IP** (default 3) and drops the rest — removes store/terminal/bot over-counting
  (e.g. a shop till placing dozens of orders that all land on one A/B variant). Ranks *distinct
  orders* (not the duplicate line rows), keeping each IP's earliest N. Applies to the sales
  figures and the Sheets export; VWO visitor counts are unaffected.
- **Exclude team / test orders** (default on): orders placed with an internal email are
  dropped from every view and the Sheets export. The email column is auto-detected (any
  column whose name contains "mail"); the team is defined by `TEAM_EMAILS` (exact addresses),
  `TEAM_EMAIL_DOMAINS` (whole domains, e.g. `videt.ro`), and `TEAM_EMAIL_DOMAIN_PREFIXES`
  (domain prefixes — `alensa.` catches every Alensa country domain: alensa.eu, alensa.cz,
  alensa.sk, …) in [`app.py`](app.py). The raw email is used
  only for this flag and then discarded (only the domain is kept, for the audit view below).
- **Dropped-orders audit (per variant).** Below the Per-variant table, a **Dropped orders (per
  variant)** table shows how many orders each exclusion filter removed, per variant (+ TOTAL):
  **By team email**, **By IP cap** (beyond N/IP), **By showroom** (showroom-payment orders, when
  that filter is on), and **Total dropped** (an order hit by more than one is counted in each
  column but once in Total). Scoped to the selected test and the active filters.
  Two **Download** buttons give the order-level detail — email-dropped (variant, order, domain)
  and IP-dropped (variant, order, IP). The table is hidden when nothing is dropped.
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
- **Percentages are Sheets-friendly:** all `%` columns (% storno, Margin %, Conv. rate %,
  Improvement %, …) are stored as **fractions** (e.g. `0.0459`) and only *displayed* as
  `4.59%`. So when you copy a cell into Google Sheets and apply Percent formatting you get
  `4.59%`, not `459%`. (Downloaded CSVs therefore also hold the fraction.)
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

### Send to Google Sheets (finalization)

The Per-variant view has a **📤 Send to Google Sheets** panel that fills your per-eshop
finalization tabs (built from the `TEMPLATE` tab) with two blocks:

- **VWO block** (+ **Desktop** / **Mobile** sub-blocks): Visitors, Conversions, Improvement,
  CR%, Revenue, Avg/obj — all from the VWO API, including the device split. VWO money is
  written as-is (the tab's own currency format is kept).
- **Alensis block:** Visitors, Orders, % Konverze, Revenue, Prům./obj., Storno, % Storno,
  Marže, Prům. marže/obj. — from the sales export, **in CZK**.

How it finds where to write:
- **Tab routing:** each eshop tab declares its VWO campaign id in cell **B1** (the
  `.../ab/<id>/` link). The app matches the selected test id to that tab.
- **Block anchoring:** each block is located by its merged section title in column A
  (`VWO`, `Desktop`, `Mobile`, `Alensis`). Only the single-cell data grids are written —
  merged titles/headers are never touched.
- **Alensis is header-driven:** the app fills each Alensis column by **matching its header
  label** (accent-/case-insensitive), so you can add, rename, or reorder columns in the
  template and the app keeps up — unknown headers are left alone. Recognised labels include
  the private-brand split: *Počet objednávek bez/s privátky*, *% objednávek s privátkou*,
  *Průměr. hodnota obj. bez/s privátky*, *Průměr. Marže/obj. bez/s privátky*, plus the
  *… CELKEM* totals. (Private = full orders containing a private brand; "bez" = the rest.)
- **Send test → its tab** writes the selected campaign; **Fill all tabs from this export**
  loops every tab's B1 id and fills them all. Percentages go in as fractions into
  percent-formatted cells; re-sending overwrites the data cells in place.
- **VWO-only update (no export needed):** on the **VWO campaigns** page, after selecting one or
  more campaigns, an *Update VWO blocks in Google Sheets* panel fills just the VWO +
  Desktop/Mobile blocks (and dates) for **those selected campaigns** — each routed to its tab by
  the B1 link, Alensis left untouched. Use it to refresh VWO numbers before you have the export.
- **Custom date range:** the **VWO campaigns** page has a *📅 Custom date range* toggle. Set a
  From/To window (e.g. test start → 25.6.2026) and the displayed numbers **and** the sheet update
  are computed for that interval only — visitors, conversions, CR%, improvement, revenue, the
  device split, and custom goals all respect the window (via VWO's ranged segment endpoint).
- **VWO block is header-driven too**, with **per-test custom goals.** Standard columns (Počet
  zobrazení, Počet konverzí, Improvement, Konverzní poměr, Revenue, Průměr/obj.) fill by header
  label. Test-specific goal columns (e.g. popup "výměn za privátku", "přidání do košíku") fill
  from `VWO_CUSTOM_GOALS` in [`app.py`](app.py) — a map of *campaign id → {header label: VWO goal
  id}*; add an entry per test. Derived columns (rates, "closes") are left for **sheet formulas**
  — the app skips any header it doesn't recognise.

**Setup:** a Google **service account** with the Sheets API enabled; share the spreadsheet
with its email (Editor); put the key + spreadsheet id in secrets:

```toml
gsheets_spreadsheet_id = "your-spreadsheet-id"
[gcp_service_account]   # the fields from the downloaded JSON key
type = "service_account"
project_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "...@....iam.gserviceaccount.com"
# token_uri, private_key_id, client_id …
```

## Very large exports — stream from Google Drive (no local step)

On free Streamlit Cloud (~1 GB RAM) the file-uploader buffers the whole file in memory
before the app runs, so an ~800 MB+ CSV crashes on upload. The **Google Drive** data source
avoids that entirely — no local trimming, no paid tier:

1. **Enable the Google Drive API** in the same Google Cloud project (you already enabled Sheets).
2. **Share a Drive folder** (Viewer) with the service account (`…@….iam.gserviceaccount.com`)
   and drop the full export CSV in it. (Optional: set `gdrive_folder_id` in secrets to prefill it.)
3. In the app, pick **Data source → Google Drive**, choose the file, and enter the **test id**.

The app then **streams** the CSV straight from Drive and keeps only that test's rows — it never
holds the whole file, so peak memory stays a few hundred MB regardless of file size. You pick the
test up front (from the VWO campaigns page or your notes); everything else works as usual on that
test's slice. The VWO campaigns page needs no data source at all.

## Very large exports (trim locally — alternative)

Streamlit Community Cloud (~1 GB RAM) buffers the whole upload in memory before the app
runs, so a full ~800 MB+ export can crash it on load. Use the local **`trim_export.py`**
helper on your own machine first — it streams the file (low memory) and writes a much
smaller CSV, then upload that:

```bash
python trim_export.py sells-29513.csv --split     # one file per project (eshop)
python trim_export.py sells-29513.csv --project 87 # just videt.ro
python trim_export.py sells-29513.csv              # keep only the analyzer's columns
```

- `--split` (recommended) writes one file per `ref_projects` id — usually tens of MB each,
  which load comfortably. You analyse one eshop/test at a time, so grab the project file you need.
- `--outdir "…/AB trims"` collects the trimmed files in one folder. The `trim.bat` launcher
  (double-click / drag a CSV onto it) runs `--split --outdir` for you.
- Column-trimming alone (~halves the size) helps but a multi-million-row export can still be
  too big for the free tier; splitting by project is the reliable path.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

Upload your export, or try `sample_data/sample_sales.csv`.

## Run as a desktop app (own window, no browser, uses your PC's resources)

Double-click **`Run A-B Analyzer.bat`**. It launches the analyzer in its **own
native window** (via pywebview) — no browser tab — using **this computer's RAM**
instead of Streamlit Cloud's ~1 GB cap, so large exports load without the Drive
workaround. On launch it does a fast-forward `git pull`, so you always run the
latest pushed version — **no rebuild/redeploy step**. Close the window to quit
(the background server stops automatically).

- First run installs `pywebview` (and any missing deps); later runs are instant.
- Under the hood it's still Streamlit (a hidden `127.0.0.1` server); the window
  is [`desktop_app.py`](desktop_app.py). Requires Python + git installed.

**Pin it to the taskbar.** A `.bat` can't be pinned directly, so run
[`make_desktop_shortcut.ps1`](make_desktop_shortcut.ps1) once (right-click → Run
with PowerShell). It creates **A-B Analyzer** on your Desktop targeting
`pythonw.exe desktop_app.py` (a real program, so Windows allows pinning) with the
[`assets/analyzer.ico`](assets/analyzer.ico) icon. Then right-click that shortcut
→ *Show more options* → **Pin to taskbar**. The shortcut still runs from the repo
folder, so the launch-time `git pull` auto-update keeps working.

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
