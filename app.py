"""A/B Sales Analyzer — filter and export A/B test sales numbers.

One test at a time: pick a test, the app resolves each row's variant within that
test (handling pipe-delimited multi-test items), so figures never double-count.
"""
from __future__ import annotations

import io
import re
import unicodedata

import pandas as pd
import streamlit as st

# Columns we rely on. Missing ones degrade gracefully.
COL_ORDER = "orderId"
COL_TS = "ordertimestamp"
COL_PRICE_NET = "price_clean"
COL_PRICE_GROSS = "price_vat"
COL_AMOUNT = "amount"
COL_TEST = "ab_test_name"
COL_VARIANT = "ab_test_variant"
COL_CANCEL = "orderstatecancel"
COL_FINAL = "orderstatefinal"
COL_ITEMTYPE = "orderItemType"
COL_PROJITEM = "projectItemId"

# Per-item profit columns (CZK). "Standard" = sell − purchase price;
# "FIFO" = sell − accounting FIFO purchase price.
PROFIT_COLS = {"Standard": "item_profit", "FIFO": "itemProfitByAccountingFifoPrice"}

NO_TEST = "— None (all rows) —"

# Private brands. Matching is accent-/case-/separator-insensitive, so
# "Laim Care", "Laim-Care", "laimcare" all resolve to the same brand.
PRIVATE_BRANDS = [
    "Gelone", "TopVue", "AQ Pure", "Laim Care", "Crazy lens", "Laim premium",
    "Private label", "Válle", "Marisio", "Kimikado", "Beron", "Crullé",
]


def normalize_text(s: str) -> str:
    """Lowercase, strip accents, drop everything but a-z0-9 (collapses spaces/hyphens)."""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


_BRAND_KEYS = [(b, normalize_text(b)) for b in PRIVATE_BRANDS]


def detect_brand(*names: str) -> str:
    """Return the first private brand found in the given name fields, else ''."""
    norm = normalize_text(" ".join(n for n in names if n))
    for brand, key in _BRAND_KEYS:
        if key and key in norm:
            return brand
    return ""


# --------------------------------------------------------------------------- #
# Loading & preparation
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def load_data(raw: bytes, name: str) -> pd.DataFrame:
    """Parse the uploaded export into a typed DataFrame."""
    if name.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(raw), dtype=str)
    else:
        # Semicolon-delimited, UTF-8 BOM. Read as str, coerce numerics after.
        df = pd.read_csv(
            io.BytesIO(raw), sep=";", dtype=str,
            encoding="utf-8-sig", keep_default_na=False,
        )
    df.columns = [c.strip() for c in df.columns]

    for col in (COL_PRICE_NET, COL_PRICE_GROSS, COL_AMOUNT, *PROFIT_COLS.values()):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if COL_TS in df.columns:
        df["_order_dt"] = pd.to_datetime(df[COL_TS], errors="coerce")

    # Line revenue = unit price * quantity, for net and gross.
    amt = df[COL_AMOUNT].fillna(1) if COL_AMOUNT in df.columns else 1
    if COL_PRICE_NET in df.columns:
        df["_rev_net"] = df[COL_PRICE_NET].fillna(0) * amt
    if COL_PRICE_GROSS in df.columns:
        df["_rev_gross"] = df[COL_PRICE_GROSS].fillna(0) * amt

    # Profit columns are already line totals (quantity baked in) — use as-is, no * amt.
    # (Verified: for amount=2, item_profit == (price_clean - purchase_price_perItem) * 2.)
    for label, col in PROFIT_COLS.items():
        if col in df.columns:
            df[f"_profit_{label.lower()}"] = df[col].fillna(0)

    # Delivery / shipping lines: projectItemId contains "delivery".
    if COL_PROJITEM in df.columns:
        df["_is_delivery"] = df[COL_PROJITEM].str.contains("delivery", case=False, na=False)
    else:
        df["_is_delivery"] = False

    # Private-brand detection. Prefer the authoritative categoriesData-brand column;
    # fall back to matching the item/common name when the export lacks it.
    iname = df["itemname"] if "itemname" in df.columns else pd.Series("", index=df.index)
    cname = df["commonName"] if "commonName" in df.columns else pd.Series("", index=df.index)
    if "categoriesData-brand" in df.columns:
        norm_to_brand = {normalize_text(b): b for b in PRIVATE_BRANDS}
        df["_brand"] = df["categoriesData-brand"].fillna("").map(
            lambda b: norm_to_brand.get(normalize_text(b), ""))
    else:
        df["_brand"] = [detect_brand(a, b) for a, b in zip(iname, cname)]
    df["_is_private"] = df["_brand"] != ""

    # Contact-lens flag ("Pouze čočky"): prefer categoriesData-items-type == "Contact
    # lenses"; fall back to the Czech commonName containing "čoč". The private split
    # counts orders that contain a private-brand contact lens.
    if "categoriesData-items-type" in df.columns:
        df["_is_lens"] = df["categoriesData-items-type"].fillna("") == "Contact lenses"
    else:
        df["_is_lens"] = cname.str.contains("čoč", case=False, na=False)

    # Product category (Contact lenses / Solutions / Glasses / …) for filtering and
    # grouping — independent of the private-brand flag.
    if "categoriesData-items-type" in df.columns:
        df["_item_category"] = df["categoriesData-items-type"].fillna("").replace("", "(uncategorized)")
    else:
        df["_item_category"] = df["_is_lens"].map({True: "Contact lenses", False: "(uncategorized)"})

    df["_margin"] = pd.NA  # placeholder — wired in when a pricelist is added
    return df


def variant_in_test(name_field: str, variant_field: str, test: str) -> str | None:
    """Resolve this row's variant within `test`, handling pipe-delimited multi-test items.

    `ab_test_name="221|280"` paired positionally with `ab_test_variant="1|2"`
    means test 221 -> variant 1, test 280 -> variant 2.
    """
    names = (name_field or "").split("|")
    variants = (variant_field or "").split("|")
    for n, v in zip(names, variants):
        if n.strip() == test:
            return v.strip()
    return None


@st.cache_data(show_spinner=False)
def test_options(df: pd.DataFrame) -> list[str]:
    """Distinct individual test ids present (pipe-combos split out)."""
    if COL_TEST not in df.columns:
        return []
    seen: set[str] = set()
    for val in df[COL_TEST].dropna():
        for tok in str(val).split("|"):
            tok = tok.strip()
            if tok:
                seen.add(tok)
    return sorted(seen, key=lambda x: (len(x), x))


def compute_margin(df: pd.DataFrame, pricelist: pd.DataFrame | None = None) -> pd.Series:
    """Placeholder margin hook. Returns NA until a pricelist join is implemented."""
    return pd.Series(pd.NA, index=df.index)


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
MONEY_COLS = ("Revenue", "AOV", "Margin", "Margin/obj")


def metrics_for(df: pd.DataFrame, use_gross: bool, profit_col: str | None = None) -> dict[str, float]:
    """Aggregate metrics for a slice. Prices in the file are CZK and used as-is."""
    rev_col = "_rev_gross" if use_gross else "_rev_net"
    revenue = float(df[rev_col].sum()) if rev_col in df.columns else 0.0
    orders = int(df[COL_ORDER].nunique()) if COL_ORDER in df.columns else 0
    qty = float(df[COL_AMOUNT].sum()) if COL_AMOUNT in df.columns else float(len(df))
    # Column order: Orders, Revenue first (Conversion rate slots in here later),
    # then the supporting metrics. AOV = Average Order Value = Revenue / Orders.
    m = {
        "Orders": orders,
        "Revenue": round(revenue, 2),
        "AOV": round(revenue / orders, 2) if orders else 0.0,
    }
    if profit_col and profit_col in df.columns:
        margin = float(df[profit_col].sum())
        m["Margin"] = round(margin, 2)
        m["Margin/obj"] = round(margin / orders, 2) if orders else 0.0
        m["Margin %"] = round(margin / revenue * 100, 2) if revenue else 0.0
    m.update({
        "Quantity": round(qty, 2),
        "Line items": len(df),
        "Items / order": round(len(df) / orders, 2) if orders else 0.0,
    })
    return m


def style_money(df: pd.DataFrame) -> "pd.io.formats.style.Styler":
    """Format money columns as '1,000.58 Kč' and Δ% columns as '+12.34%' for display."""
    fmt: dict = {}
    for c in df.columns:
        if c in MONEY_COLS:
            fmt[c] = lambda v: "" if pd.isna(v) else f"{v:,.2f} Kč"
        elif isinstance(c, str) and c.endswith("Δ%"):
            fmt[c] = lambda v: "" if pd.isna(v) else f"{v:+.2f}%"
        elif isinstance(c, str) and (c.startswith("%") or c.endswith("%")):
            fmt[c] = lambda v: "" if pd.isna(v) else f"{v:.2f}%"
    return df.style.format(fmt)


def eval_row(g: pd.DataFrame, use_gross: bool, profit_col: str | None = None) -> dict:
    """Master-sheet-style metrics for a variant slice that still includes cancelled rows."""
    rev_col = "_rev_gross" if use_gross else "_rev_net"
    has_profit = bool(profit_col) and profit_col in g.columns
    live = g[g[COL_CANCEL] != "1"] if COL_CANCEL in g.columns else g
    canc = g[g[COL_CANCEL] == "1"] if COL_CANCEL in g.columns else g.iloc[0:0]
    # Revenue/orders/AOV from product lines; margin from ALL lines (incl. shipping/returns).
    if "_is_product" in live.columns:
        prod = live[live["_is_product"]]
    elif "_is_delivery" in live.columns:
        prod = live[~live["_is_delivery"]]
    else:
        prod = live
    orders = int(prod[COL_ORDER].nunique()) if COL_ORDER in prod.columns else 0
    storno = int(canc[COL_ORDER].nunique()) if COL_ORDER in canc.columns else 0
    revenue = float(prod[rev_col].sum()) if rev_col in prod.columns else 0.0
    margin = float(live[profit_col].sum()) if has_profit else 0.0

    row = {
        "Orders": orders,
        "Storno": storno,
        "% storno": round(storno / orders * 100, 2) if orders else 0.0,
        "Revenue": round(revenue, 2),
        "AOV": round(revenue / orders, 2) if orders else 0.0,
    }
    if has_profit:
        row["Margin"] = round(margin, 2)
        row["Margin/obj"] = round(margin / orders, 2) if orders else 0.0
        row["Margin %"] = round(margin / revenue * 100, 2) if revenue else 0.0
    return row


def download_button(df: pd.DataFrame, label: str, key: str) -> None:
    csv = df.to_csv(index=False, sep=";").encode("utf-8-sig")
    st.download_button(label, csv, file_name=f"{key}.csv", mime="text/csv", key=key)


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
def main() -> None:
    st.set_page_config(page_title="A/B Sales Analyzer", layout="wide")
    st.title("A/B Sales Analyzer")

    uploaded = st.file_uploader("Upload sales export (CSV or Excel)", type=["csv", "xlsx", "xls"])
    if uploaded is None:
        st.info("Upload a sales export to begin. Pick one test at a time for clean A/B figures.")
        st.stop()

    df = load_data(uploaded.getvalue(), uploaded.name)
    st.caption(f"Loaded **{len(df):,}** line items · **{df[COL_ORDER].nunique():,}** orders"
               if COL_ORDER in df.columns else f"Loaded {len(df):,} rows")

    # ---- Sidebar filters ----
    sb = st.sidebar
    sb.header("Filters")

    use_gross = sb.radio("Revenue basis", ["Gross", "Net"], horizontal=True) == "Gross"

    # Margin basis (only the bases whose source column exists in the file).
    available = {lbl: f"_profit_{lbl.lower()}" for lbl in PROFIT_COLS
                 if f"_profit_{lbl.lower()}" in df.columns}
    profit_col = None
    if available:
        basis = sb.radio("Margin basis", list(available), horizontal=True,
                         help="Standard = sell − purchase price (item_profit); "
                              "FIFO = sell − accounting FIFO price. Taken from the file as-is (CZK).")
        profit_col = available[basis]

    tests = test_options(df)
    test_choice = sb.selectbox("Test", [NO_TEST] + tests)
    selected_test = None if test_choice == NO_TEST else test_choice

    work = df.copy()

    # Resolve variant within the selected test (excludes non-participating rows).
    if selected_test is not None:
        work["_variant"] = [
            variant_in_test(n, v, selected_test)
            for n, v in zip(work.get(COL_TEST, ""), work.get(COL_VARIANT, ""))
        ]
        work = work[work["_variant"].notna()].copy()
        variants = sorted(work["_variant"].unique(), key=lambda x: (len(x), x))
        chosen = sb.multiselect("Variant", variants, default=variants)
        work = work[work["_variant"].isin(chosen)]
    else:
        work["_variant"] = work.get(COL_VARIANT, "").replace("", "(none)")

    # Date window
    if "_order_dt" in df.columns and df["_order_dt"].notna().any():
        dmin = df["_order_dt"].min().date()
        dmax = df["_order_dt"].max().date()
        start, end = sb.date_input("Date window", value=(dmin, dmax), min_value=dmin, max_value=dmax)
        mask = (work["_order_dt"].dt.date >= start) & (work["_order_dt"].dt.date <= end)
        work = work[mask]

    # Order state. Cancelled exclusion is applied last so the Per-variant tab can
    # still see cancelled rows to compute the Storno count.
    exclude_cancelled = (sb.checkbox("Exclude cancelled", value=True)
                         if COL_CANCEL in df.columns else False)
    if COL_FINAL in df.columns and sb.checkbox("Final orders only", value=False):
        work = work[work[COL_FINAL] == "1"]

    # Item type. Selection stored now and applied to work below. In Per-variant it
    # constrains revenue (via _is_product) but never the margin, which spans all lines.
    item_keep = None
    if COL_ITEMTYPE in df.columns:
        types = sorted(t for t in df[COL_ITEMTYPE].unique() if t)
        if types:
            item_keep = sb.multiselect("Item type", types, default=types)

    # Product category (Contact lenses / Solutions / Glasses / …). Empty = no filter.
    if "_item_category" in df.columns:
        cats = sorted(c for c in df["_item_category"].unique() if c and c != "(uncategorized)")
        sel_cat = sb.multiselect("Product category", cats,
                                 help="Filter to product categories (lenses, solutions, glasses…). "
                                      "Leave empty to include everything.")
        if sel_cat:
            work = work[work["_item_category"].isin(sel_cat)]

    # Delivery lines. Applied to work (Totals/Pivot) below; work_all keeps them so the
    # Per-variant margin can include shipping (which carries a negative margin).
    include_delivery = sb.checkbox("Include delivery / shipping lines", value=False)

    # Private brands. Gates the private split columns in Per-variant, and filters
    # the Totals/Pivot views to private-brand rows. The filter is applied below
    # (after work_all is captured) so the Per-variant split keeps both groups.
    show_private = sb.checkbox(
        "Private brands only", value=False,
        help="Show the private-brand split in Per-variant; filter Totals/Pivot to private-brand rows.")

    # Generic categorical filters (rendered inline to avoid an expander)
    sb.markdown("**More filters**")
    for col, label in [("payment", "Payment"),
                       ("orderDestinationCountryId", "Country"),
                       ("delivery_type", "Delivery type")]:
        if col in df.columns:
            opts = sorted(o for o in df[col].unique() if o)
            if opts:
                sel = sb.multiselect(label, opts)
                if sel:
                    work = work[work[col].isin(sel)]
    term = sb.text_input("Item name contains (Czech name)",
                         help="Searches the Czech commonName. Case-insensitive but "
                              "accent-sensitive: 'čoč' matches ČOČ/Čoč but not 'coc'.")
    if term and "commonName" in df.columns:
        work = work[work["commonName"].str.contains(term, case=False, na=False, regex=False)]

    # _is_product marks the lines that count as product revenue (not delivery, and an
    # included item type). Per-variant revenue uses it; margin spans every line.
    is_prod = ~work["_is_delivery"]
    if item_keep is not None:
        is_prod = is_prod & work[COL_ITEMTYPE].isin(item_keep)
    work["_is_product"] = is_prod

    # work_all keeps every line type + cancelled + both private/non-private rows, so
    # Per-variant can compute Storno, the private split, and a margin that includes
    # shipping and returns. work applies the visible filters for Totals/Pivot.
    work_all = work.copy()
    if not include_delivery:
        work = work[~work["_is_delivery"]]
    if item_keep is not None:
        work = work[work[COL_ITEMTYPE].isin(item_keep)]
    if show_private:
        work = work[work["_is_private"]]
    if exclude_cancelled:
        work = work[work[COL_CANCEL] != "1"]

    st.caption(f"**{len(work):,}** line items after filters"
               + (f" · test **{selected_test}**" if selected_test else ""))

    if work.empty:
        st.warning("No rows match the current filters.")
        st.stop()

    # ---- Views ----
    tab_totals, tab_variant, tab_pivot = st.tabs(["Totals", "Per-variant", "Custom pivot"])

    with tab_totals:
        m = metrics_for(work, use_gross, profit_col)
        cols = st.columns(len(m))
        for c, (k, v) in zip(cols, m.items()):
            c.metric(k, f"{v:,.2f} Kč" if k in MONEY_COLS else f"{v:,}")
        totals_df = pd.DataFrame([m])
        st.dataframe(style_money(totals_df), use_container_width=True, hide_index=True)
        download_button(totals_df, "Download totals", "totals")

    with tab_variant:
        # Computed from work_all so Storno (cancelled) is available per variant.
        def variant_table(src: pd.DataFrame, baseline_key: str) -> pd.DataFrame:
            rows = [{"Variant": var, **eval_row(grp, use_gross, profit_col)}
                    for var, grp in src.groupby("_variant")]
            t = pd.DataFrame(rows).sort_values("Variant").reset_index(drop=True)
            if len(t) > 1:
                base_var = st.selectbox("Baseline variant (for % diff)", t["Variant"].tolist(),
                                        key=baseline_key)
                base = t[t["Variant"] == base_var].iloc[0]
                for metric in ["Revenue", "Orders", "AOV"]:
                    b = base[metric]
                    t[f"{metric} Δ%"] = t[metric].apply(
                        lambda x: round((x - b) / b * 100, 2) if b else None)
            total = {"Variant": "TOTAL", **eval_row(src, use_gross, profit_col)}
            return pd.concat([t, pd.DataFrame([total])], ignore_index=True)

        st.caption("Storno is excluded. Revenue/AOV use product lines only; "
                   "Margin includes shipping lines (matching the manual sheet).")
        vdf = variant_table(work_all, "baseline_all")
        st.dataframe(style_money(vdf), use_container_width=True, hide_index=True)
        download_button(vdf, "Download per-variant", "per_variant")

        if show_private:
            st.markdown("#### Private brands only")
            st.caption("Same metrics, restricted to lines from private brands "
                       f"({', '.join(PRIVATE_BRANDS[:4])}…). "
                       "Use the Product category filter to narrow to e.g. lenses.")
            priv = work_all[work_all["_is_private"]]
            if priv.empty:
                st.info("No private-brand rows in the current selection.")
            else:
                pdf = variant_table(priv, "baseline_priv")
                st.dataframe(style_money(pdf), use_container_width=True, hide_index=True)
                download_button(pdf, "Download per-variant (private)", "per_variant_private")

    with tab_pivot:
        candidates = ["_variant", "_brand", "_item_category", "orderDestinationCountryId",
                      "payment", "delivery_type", "itemname", "commonName", "orderMonth", "orderDay"]
        group_opts = [c for c in candidates if c in work.columns]
        label_map = {"_variant": "variant", "_brand": "brand", "_item_category": "category"}
        group_by = st.multiselect(
            "Group by", group_opts, default=["_variant"] if "_variant" in group_opts else [],
            format_func=lambda c: label_map.get(c, c),
        )
        if group_by:
            recs = []
            for keys, grp in work.groupby(group_by):
                keys = keys if isinstance(keys, tuple) else (keys,)
                rec = {label_map.get(g, g): k for g, k in zip(group_by, keys)}
                rec.update(metrics_for(grp, use_gross, profit_col))
                recs.append(rec)
            pdf = pd.DataFrame(recs)
            st.dataframe(style_money(pdf), use_container_width=True, hide_index=True)
            download_button(pdf, "Download pivot", "pivot")
        else:
            st.info("Pick one or more columns to group by.")


if __name__ == "__main__":
    main()
