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

    for col in (COL_PRICE_NET, COL_PRICE_GROSS, COL_AMOUNT):
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

    # Delivery / shipping lines: projectItemId contains "delivery".
    if COL_PROJITEM in df.columns:
        df["_is_delivery"] = df[COL_PROJITEM].str.contains("delivery", case=False, na=False)
    else:
        df["_is_delivery"] = False

    # Private-brand detection from item name (+ commonName when present).
    iname = df["itemname"] if "itemname" in df.columns else pd.Series("", index=df.index)
    cname = df["commonName"] if "commonName" in df.columns else pd.Series("", index=df.index)
    df["_brand"] = [detect_brand(a, b) for a, b in zip(iname, cname)]
    df["_is_private"] = df["_brand"] != ""

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
MONEY_COLS = ("Revenue", "AOV")


def metrics_for(df: pd.DataFrame, use_gross: bool, rate: float = 1.0) -> dict[str, float]:
    """Aggregate metrics for a slice. `rate` converts revenue from EUR to CZK."""
    rev_col = "_rev_gross" if use_gross else "_rev_net"
    revenue = float(df[rev_col].sum()) * rate if rev_col in df.columns else 0.0
    orders = int(df[COL_ORDER].nunique()) if COL_ORDER in df.columns else 0
    qty = float(df[COL_AMOUNT].sum()) if COL_AMOUNT in df.columns else float(len(df))
    # Column order: Orders, Revenue first (Conversion rate slots in here later),
    # then the supporting metrics. AOV = Average Order Value = Revenue / Orders.
    return {
        "Orders": orders,
        "Revenue": round(revenue, 2),
        "AOV": round(revenue / orders, 2) if orders else 0.0,
        "Quantity": round(qty, 2),
        "Line items": len(df),
        "Items / order": round(len(df) / orders, 2) if orders else 0.0,
    }


def style_money(df: pd.DataFrame) -> "pd.io.formats.style.Styler":
    """Format money columns as '1,000.58 Kč' and Δ% columns as '+12.34%' for display."""
    fmt: dict = {}
    for c in df.columns:
        if c in MONEY_COLS:
            fmt[c] = lambda v: "" if pd.isna(v) else f"{v:,.2f} Kč"
        elif isinstance(c, str) and c.endswith("Δ%"):
            fmt[c] = lambda v: "" if pd.isna(v) else f"{v:+.2f}%"
    return df.style.format(fmt)


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

    use_gross = sb.radio("Revenue basis", ["Net", "Gross"], horizontal=True) == "Gross"
    czk_rate = sb.number_input("EUR → CZK rate", min_value=0.0, value=25.00, step=0.10,
                               format="%.2f",
                               help="Revenue (in EUR) is multiplied by this rate and shown in Kč. "
                                    "Set to 1 to keep EUR values.")

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

    # Order state
    if COL_CANCEL in df.columns and sb.checkbox("Exclude cancelled", value=True):
        work = work[work[COL_CANCEL] != "1"]
    if COL_FINAL in df.columns and sb.checkbox("Final orders only", value=False):
        work = work[work[COL_FINAL] == "1"]

    # Item type
    if COL_ITEMTYPE in df.columns:
        types = sorted(t for t in df[COL_ITEMTYPE].unique() if t)
        if types:
            keep = sb.multiselect("Item type", types, default=types)
            work = work[work[COL_ITEMTYPE].isin(keep)]

    # Delivery lines
    if not sb.checkbox("Include delivery / shipping lines", value=False):
        work = work[~work["_is_delivery"]]

    # Private brands
    if sb.checkbox("Private brands only", value=False,
                   help="Match item name against our private brands (accent/case/separator-insensitive)."):
        work = work[work["_is_private"]]

    # Generic categorical filters
    with sb.expander("More filters"):
        for col, label in [("payment", "Payment"),
                           ("orderDestinationCountryId", "Country"),
                           ("delivery_type", "Delivery type")]:
            if col in df.columns:
                opts = sorted(o for o in df[col].unique() if o)
                if opts:
                    sel = st.multiselect(label, opts)
                    if sel:
                        work = work[work[col].isin(sel)]
        term = st.text_input("Item name contains (Czech name)",
                             help="Searches the Czech commonName. Case-insensitive but "
                                  "accent-sensitive: 'čoč' matches ČOČ/Čoč but not 'coc'.")
        if term and "commonName" in df.columns:
            work = work[work["commonName"].str.contains(term, case=False, na=False, regex=False)]

    st.caption(f"**{len(work):,}** line items after filters"
               + (f" · test **{selected_test}**" if selected_test else ""))

    if work.empty:
        st.warning("No rows match the current filters.")
        st.stop()

    # ---- Views ----
    tab_totals, tab_variant, tab_pivot = st.tabs(["Totals", "Per-variant", "Custom pivot"])

    with tab_totals:
        m = metrics_for(work, use_gross, czk_rate)
        cols = st.columns(len(m))
        for c, (k, v) in zip(cols, m.items()):
            c.metric(k, f"{v:,.2f} Kč" if k in MONEY_COLS else f"{v:,}")
        totals_df = pd.DataFrame([m])
        st.dataframe(style_money(totals_df), use_container_width=True, hide_index=True)
        download_button(totals_df, "Download totals", "totals")

    with tab_variant:
        rows = []
        for var, grp in work.groupby("_variant"):
            m = metrics_for(grp, use_gross, czk_rate)
            m = {"Variant": var, **m}
            rows.append(m)
        vdf = pd.DataFrame(rows).sort_values("Variant").reset_index(drop=True)

        if len(vdf) > 1:
            baseline = st.selectbox("Baseline variant (for % diff)", vdf["Variant"].tolist())
            base = vdf[vdf["Variant"] == baseline].iloc[0]
            for metric in ["Revenue", "Orders", "AOV"]:
                b = base[metric]
                vdf[f"{metric} Δ%"] = vdf[metric].apply(
                    lambda x: round((x - b) / b * 100, 2) if b else None
                )
        st.dataframe(style_money(vdf), use_container_width=True, hide_index=True)
        download_button(vdf, "Download per-variant", "per_variant")

    with tab_pivot:
        candidates = ["_variant", "_brand", "orderDestinationCountryId", "payment",
                      "delivery_type", "itemname", "commonName", "orderMonth", "orderDay"]
        group_opts = [c for c in candidates if c in work.columns]
        label_map = {"_variant": "variant", "_brand": "brand"}
        group_by = st.multiselect(
            "Group by", group_opts, default=["_variant"] if "_variant" in group_opts else [],
            format_func=lambda c: label_map.get(c, c),
        )
        if group_by:
            recs = []
            for keys, grp in work.groupby(group_by):
                keys = keys if isinstance(keys, tuple) else (keys,)
                rec = {label_map.get(g, g): k for g, k in zip(group_by, keys)}
                rec.update(metrics_for(grp, use_gross, czk_rate))
                recs.append(rec)
            pdf = pd.DataFrame(recs)
            st.dataframe(style_money(pdf), use_container_width=True, hide_index=True)
            download_button(pdf, "Download pivot", "pivot")
        else:
            st.info("Pick one or more columns to group by.")


if __name__ == "__main__":
    main()
