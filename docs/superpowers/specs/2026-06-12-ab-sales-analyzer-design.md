# A/B Sales Analyzer — Design

**Date:** 2026-06-12
**Repo:** bgal-png/A-B-Analyzer
**Status:** Approved (build first, refine on the go)

## Purpose

A Streamlit tool to **filter and read** A/B test sales numbers from a CSV/Excel
export, then copy/export clean figures into a separate master sheet. Not a
significance-testing tool — it slices, aggregates, and exports.

## Input data

Sales export at **line-item level** (semicolon-delimited, UTF-8 BOM, quoted
timestamps). One order = several rows (products + one delivery line). Key columns:

| Column | Meaning |
|---|---|
| `orderId` | order identifier (multiple rows per order) |
| `ordertimestamp` | order datetime (date window filter) |
| `price_clean` / `price_vat` | unit price net / gross |
| `amount` | quantity → line revenue = price × amount |
| `ab_test_name` | test id(s); pipe-delimited if item is in multiple tests, e.g. `221\|280` |
| `ab_test_variant` | variant(s); positionally paired with `ab_test_name`, e.g. `1\|2` |
| `itemname` / `commonName` | product name |
| `orderstatecancel` | 1 = cancelled |
| `orderstatefinal` | 1 = final |
| `orderItemType` | Normal / Gift |
| `payment`, `orderDestinationCountryId`, `delivery_type`, `currency` | filterable dims |
| `projectItemId` | contains `delivery` for shipping lines |

## Core model — one test at a time

The user selects **a single test** (e.g. `280`). For each row the app splits
`ab_test_name` and `ab_test_variant` on `|`, pairs them positionally, and resolves
**that row's variant within the selected test**. Rows not in the test are excluded.

This guarantees **no double-counting**: an item in two tests contributes its
selected-test variant exactly once. A "None" option disables the test filter for
raw exploration.

## Filters (sidebar)

- Test (single-select) + Variant (multi-select, populated from chosen test)
- Date window (range on `ordertimestamp`)
- Exclude cancelled (default on); final-only toggle
- Item type (Normal/Gift)
- Include delivery/shipping lines (default off)
- Generic: payment, country, delivery_type, item-name search

## Metrics

Net/gross toggle. Revenue = price × amount. Derived: revenue, distinct orders,
quantity, AOV (rev ÷ orders), items per order. **Margin = placeholder**, wired in
later via a `compute_margin()` hook when a pricelist file is added.

## Views (tabs)

1. **Totals** — one summary row for the current filter.
2. **Per-variant** — one row per variant, with absolute + % difference vs a chosen
   baseline variant.
3. **Custom pivot** — user picks group-by × metrics.

Each view: Download CSV + copy-friendly table.

## Stack & deployment

Streamlit + pandas. `app.py`, `requirements.txt`, `README.md`, `.gitignore`,
`sample_data/` (synthetic). Deployable on Streamlit Cloud. **Real sales data is
gitignored** — never committed.

## Future (out of scope now)

- Pricelist join for real margins / total prices (`compute_margin()` seam).
- Excel (`.xlsx`) upload alongside CSV.
