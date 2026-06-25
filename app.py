"""A/B Sales Analyzer — filter and export A/B test sales numbers.

One test at a time: pick a test, the app resolves each row's variant within that
test (handling pipe-delimited multi-test items), so figures never double-count.
"""
from __future__ import annotations

import io
import json
import re
import time
import unicodedata
import urllib.error
import urllib.request

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

VWO_CAMPAIGN_URL = "https://app.vwo.com/api/v2/accounts/{acc}/campaigns/{cid}"
# Post-segmentation endpoint (device splits etc.). The public host 307-redirects to
# the account's region path; account 717496 lives on eu01, so we hit it directly.
VWO_SEGMENT_URL = "https://app.vwo.com/eu01/api/v2/accounts/{acc}/campaigns/{cid}/segment"


def _vwo_get(url: str, token: str, timeout: int = 30, retries: int = 3) -> dict:
    """GET + JSON with retry/backoff on timeouts, 429s and 5xx (eases VWO API load)."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"token": token, "User-Agent": "Mozilla/5.0"})
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
    raise last_err if last_err else RuntimeError("request failed")


def _vwo_post(url: str, token: str, body: dict, timeout: int = 45, retries: int = 3) -> dict:
    """POST JSON + parse JSON, with the same retry/backoff as `_vwo_get`."""
    payload = json.dumps(body).encode("utf-8")
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, data=payload, method="POST",
                headers={"token": token, "Content-Type": "application/json",
                         "User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
    raise last_err if last_err else RuntimeError("request failed")


@st.cache_data(ttl=3600, show_spinner=False, max_entries=32)
def fetch_vwo_campaign(account_id: str, campaign_id: str, token: str) -> dict | None:
    """Fetch a VWO campaign report (the `_data` object), cached 1h.

    On failure returns {"_error": "..."} so the UI can show why.
    """
    try:
        url = VWO_CAMPAIGN_URL.format(acc=account_id, cid=campaign_id)
        data = _vwo_get(url, token, timeout=45).get("_data")
        return data if data else {"_error": "no report data in response"}
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code} {e.reason}"}
    except Exception as e:  # noqa: BLE001
        return {"_error": f"{type(e).__name__}: {e}"}


def vwo_primary_counts(data: dict) -> dict[str, dict]:
    """{variation_id: {visitors, conversions}} from a campaign's primary goal."""
    goals = data.get("goals", [])
    primary = next((g for g in goals if g.get("isPrimary")), goals[0] if goals else {})
    return {str(v): {"visitors": int(d.get("visitorCount", 0)),
                     "conversions": int(d.get("conversionCount", 0))}
            for v, d in primary.get("aggregatedData", {}).items()}


def vwo_primary_improvement(data: dict) -> dict[str, float]:
    """{variation_id: expected improvement % vs control} from the primary goal (control → None)."""
    goals = data.get("goals", [])
    pid = next((g.get("id") for g in goals if g.get("isPrimary")),
               goals[0].get("id") if goals else None)
    out: dict[str, float] = {}
    for vgd in data.get("variationGoalData", []):
        if vgd.get("goal") != pid:
            continue
        agg = vgd.get("aggregated", {})
        imp = agg.get("relativeImprovementRate") or agg.get("relativeExpectedImprovementRate")
        med = imp.get("median") if isinstance(imp, dict) else None
        out[str(vgd.get("variation"))] = round(med, 6) if med is not None else None  # fraction
    return out


def vwo_revenue_value(data: dict) -> dict[str, float]:
    """{variation_id: VWO-tracked total revenue} from the revenue goal."""
    rgoal = next((g for g in data.get("goals", []) if g.get("type") == "revenue"), None)
    if not rgoal:
        return {}
    return {str(v): round(float(d.get("totalRevenue", 0)), 2)
            for v, d in rgoal.get("aggregatedData", {}).items() if "totalRevenue" in d}


def vwo_primary_goal_id(data: dict):
    """The campaign's primary goal id (falls back to the first goal)."""
    goals = data.get("goals", [])
    return next((g.get("id") for g in goals if g.get("isPrimary")),
               goals[0].get("id") if goals else None)


@st.cache_data(ttl=3600, show_spinner=False, max_entries=64)
def fetch_vwo_segment(account_id: str, campaign_id: str, token: str, devices: tuple,
                      goal_ids: tuple, primary_id, revenue_id, start_ts, end_ts) -> dict:
    """{variation_id: {visitors, conversions, revenue, improvement}} for a device segment.

    `devices` is a tuple like ("desktop",) or ("mobile", "tablet"). Uses a *custom*
    segment so the device operand actually filters (a "predefined" id is server-side
    and ignores the operand). All goal ids are requested in one call: a single goal
    returns only raw counts, but multiple goals trigger full stats — so the primary
    goal then carries `relativeImprovementRate` (→ Improvement %) and the revenue goal
    carries `totalRevenue`. On failure returns {"_error": "..."}.
    """
    body = {
        "goals": ",".join(str(g) for g in goal_ids) or str(primary_id),
        "id": int(campaign_id),
        "startTime": int(start_ts),
        "endTime": int(end_ts),
        "segments": {"id": "dev", "type": "custom", "partialSegments": [
            {"operator": 11, "rOperandValue": list(devices),
             "id": "18-VwoPostWebsite", "queryElementType": "partialQuery"}]},
        "__preventNotifyOnError": True, "isGuardrail": False,
    }
    try:
        res = _vwo_post(VWO_SEGMENT_URL.format(acc=account_id, cid=campaign_id), token, body)
        d = res.get("_data", res)  # the regional endpoint wraps the report in `_data`
        out: dict[str, dict] = {}
        for vgd in d.get("variationGoalData", []):
            g = str(vgd.get("goal"))
            vid = str(vgd.get("variation"))
            agg = vgd.get("aggregated", {})
            rec = out.setdefault(vid, {"visitors": 0, "conversions": 0,
                                       "revenue": None, "improvement": None})
            if g == str(primary_id):
                rec["visitors"] = int(agg.get("visitorCount", 0))
                rec["conversions"] = int(agg.get("conversionCount", 0))
                ri = agg.get("relativeImprovementRate")
                med = ri.get("median") if isinstance(ri, dict) else None
                rec["improvement"] = round(med, 6) if med is not None else None  # fraction
            if revenue_id is not None and g == str(revenue_id) and "totalRevenue" in agg:
                rec["revenue"] = round(float(agg.get("totalRevenue", 0)), 2)
        return out or {"_error": "no segment data"}
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code} {e.reason}"}
    except Exception as e:  # noqa: BLE001
        return {"_error": f"{type(e).__name__}: {e}"}


def vwo_device_compare_table(data: dict, desk: dict, mob: dict) -> pd.DataFrame:
    """Desktop vs Mobile+Tablet per variation: Visitors, Conversions, CR%, VWO Impr% vs control."""
    names = {str(v["id"]): v.get("name", "") for v in data.get("variations", [])}
    ctrl = {str(v["id"]) for v in data.get("variations", []) if v.get("isControl")}
    vids = sorted(set(desk) | set(mob), key=lambda x: (len(x), x))

    def cr(seg, vid):
        s = seg.get(vid, {})
        vis = s.get("visitors", 0)
        return (s.get("conversions", 0) / vis) if vis else None  # fraction

    rows = []
    for vid in vids:
        dv, mv = desk.get(vid, {}), mob.get(vid, {})
        d_cr, m_cr = cr(desk, vid), cr(mob, vid)
        is_ctrl = vid in ctrl
        rows.append({
            "Variation": f"{vid} · {names.get(vid, '')}" + (" (ctrl)" if is_ctrl else ""),
            "Desktop vis": dv.get("visitors", 0),
            "Desktop conv": dv.get("conversions", 0),
            "Desktop CR%": round(d_cr, 6) if d_cr is not None else None,
            "Desktop Impr%": None if is_ctrl else dv.get("improvement"),
            "Mob+Tab vis": mv.get("visitors", 0),
            "Mob+Tab conv": mv.get("conversions", 0),
            "Mob+Tab CR%": round(m_cr, 6) if m_cr is not None else None,
            "Mob+Tab Impr%": None if is_ctrl else mv.get("improvement"),
        })
    return pd.DataFrame(rows)


def device_styler(tbl: pd.DataFrame):
    """Styler for the device table: ints, CR% plain, Impr% signed; best CR/Impr highlighted."""
    num_cols = [c for c in tbl.columns if c != "Variation"]

    def fmt_for(c):
        if "Impr" in c:  # stored as a fraction → render as signed percent
            return lambda v: "" if pd.isna(v) else f"{v * 100:+.2f}%"
        if c.endswith("%"):
            return lambda v: "" if pd.isna(v) else f"{v * 100:.2f}%"
        return lambda v: "" if pd.isna(v) else f"{int(v):,}"

    def highlight(col):
        s = pd.to_numeric(col, errors="coerce")
        uniq = sorted(s.dropna().unique(), reverse=True)  # higher = better
        best = uniq[0] if uniq else None
        second = uniq[1] if len(uniq) > 1 else None
        out = []
        for v in s:
            if best is not None and v == best:
                out.append("background-color: rgba(76,175,80,0.30)")
            elif second is not None and v == second:
                out.append("background-color: rgba(240,200,70,0.28)")
            else:
                out.append("")
        return out

    rate_cols = [c for c in num_cols if c.endswith("%")]  # highlight the comparison cols only
    return (tbl.style.format({c: fmt_for(c) for c in num_cols})
            .apply(highlight, subset=rate_cols))


def vwo_device_one_table(data: dict, seg: dict) -> pd.DataFrame:
    """VWO-only per-variant table for one device segment (Variant matches the main table).

    Columns: Variant, Visitors, VWO conv., Conv. rate %, Improvement %, VWO revenue, + TOTAL.
    Improvement % is VWO's relativeImprovementRate (median) vs control; VWO revenue is the
    revenue goal's totalRevenue for this device. The revenue column is dropped if the
    campaign has no revenue goal.
    """
    ctrl = {str(v["id"]) for v in data.get("variations", []) if v.get("isControl")}
    vids = sorted((k for k in seg if not k.startswith("_")), key=lambda x: (len(x), x))

    rows, tot_v, tot_c, tot_r, any_rev = [], 0, 0, 0.0, False
    for vid in vids:
        s = seg.get(vid, {})
        vis, con = s.get("visitors", 0), s.get("conversions", 0)
        cr = (con / vis) if vis else None  # fraction
        rev, is_ctrl = s.get("revenue"), vid in ctrl
        rows.append({
            "Variant": f"{vid} (ctrl)" if is_ctrl else vid,
            "Visitors": vis, "VWO conv.": con,
            "Conv. rate %": round(cr, 6) if cr is not None else None,
            "Improvement %": None if is_ctrl else s.get("improvement"),
            "VWO revenue": rev,
        })
        tot_v += vis
        tot_c += con
        if rev is not None:
            tot_r += rev
            any_rev = True
    rows.append({"Variant": "TOTAL", "Visitors": tot_v, "VWO conv.": tot_c,
                 "Conv. rate %": round(tot_c / tot_v, 6) if tot_v else None,
                 "Improvement %": None, "VWO revenue": tot_r if any_rev else None})
    df = pd.DataFrame(rows)
    if not any_rev:
        df = df.drop(columns=["VWO revenue"])  # campaign has no revenue goal
    return df


def device_variant_styler(tbl: pd.DataFrame):
    """Styler for a single-device VWO table: ranks best/2nd CR%, Improvement% & revenue,
    ignoring the TOTAL row."""
    num_cols = [c for c in tbl.columns if c != "Variant"]
    is_total = tbl["Variant"].astype(str).eq("TOTAL").to_numpy()

    def fmt_for(c):
        if "Improvement" in c or "lift" in c:  # stored as a fraction → signed percent
            return lambda v: "" if pd.isna(v) else f"{v * 100:+.2f}%"
        if c.endswith("%"):
            return lambda v: "" if pd.isna(v) else f"{v * 100:.2f}%"
        if "revenue" in c:
            return lambda v: "" if pd.isna(v) else f"{v:,.0f}"  # VWO revenue currency varies per test
        return lambda v: "" if pd.isna(v) else f"{int(v):,}"

    def highlight(col):
        s = pd.to_numeric(col, errors="coerce")
        ranked = s.where(~is_total)  # TOTAL never wins a highlight
        uniq = sorted(ranked.dropna().unique(), reverse=True)
        best = uniq[0] if uniq else None
        second = uniq[1] if len(uniq) > 1 else None
        out = []
        for v, t in zip(s, is_total):
            if t or pd.isna(v):
                out.append("")
            elif best is not None and v == best:
                out.append("background-color: rgba(76,175,80,0.30)")
            elif second is not None and v == second:
                out.append("background-color: rgba(240,200,70,0.28)")
            else:
                out.append("")
        return out

    hl_cols = [c for c in num_cols if c.endswith("%") or "revenue" in c]
    return (tbl.style.format({c: fmt_for(c) for c in num_cols})
            .apply(highlight, subset=hl_cols))


def _extract_campaign_list(payload) -> list:
    """Find the first list of campaign-like dicts anywhere in the response."""
    from collections import deque
    q = deque([payload])
    while q:
        x = q.popleft()
        if isinstance(x, list) and x and isinstance(x[0], dict) and "id" in x[0] and "name" in x[0]:
            return x
        if isinstance(x, dict):
            q.extend(x.values())
        elif isinstance(x, list):
            q.extend(x)
    return []


@st.cache_data(ttl=3600, show_spinner=False, max_entries=4)
def fetch_vwo_campaign_list(account_id: str, token: str) -> list[dict] | None:
    """All campaigns [{id,name,type,status}], paging through the API until exhausted."""
    try:
        out: dict[str, dict] = {}
        offset, page_size = 0, 1000
        for _ in range(40):  # safety cap
            url = (f"https://app.vwo.com/api/v2/accounts/{account_id}/campaigns"
                   f"?limit={page_size}&offset={offset}&showDetailedInfo=false")
            page = _extract_campaign_list(_vwo_get(url, token, timeout=30))
            if not page:
                break
            added = 0
            for c in page:
                cid = c.get("id")
                if cid is None:
                    continue
                k = str(cid)
                if k not in out:
                    out[k] = {"id": k, "name": c.get("name", ""),
                              "type": c.get("type", ""), "status": c.get("status", "")}
                    added += 1
            offset += len(page)
            if added == 0:  # no new campaigns -> done (handles capped limits too)
                break
        return list(out.values()) or None
    except Exception:
        return None


def _vwo_per_day(data: dict) -> dict[int, int]:
    """{day-start unix ts: total visitors} from the campaign's primary goal."""
    goals = data.get("goals", [])
    pid = next((g.get("id") for g in goals if g.get("isPrimary")),
               goals[0].get("id") if goals else None)
    per_day: dict[int, int] = {}
    for vgd in data.get("variationGoalData", []):
        if vgd.get("goal") != pid:
            continue
        for iw in vgd.get("intervalWise", []):
            ts = iw.get("interval")
            if ts is not None:
                per_day[ts] = per_day.get(ts, 0) + int(iw.get("visitorCount", 0))
    return per_day


def fmt_date(ts) -> str:
    """Unix ts → 'D.M.YYYY' (no leading zeros), cross-platform."""
    if not ts:
        return "?"
    t = time.gmtime(ts)
    return f"{t.tm_mday}.{t.tm_mon}.{t.tm_year}"


def vwo_active_days(data: dict) -> set:
    """Set of 'YYYY-MM-DD' day strings the test actually collected traffic."""
    return {time.strftime("%Y-%m-%d", time.gmtime(ts))
            for ts, v in _vwo_per_day(data).items() if v > 0}


def vwo_running_periods(data: dict) -> list[dict]:
    """Reconstruct active (running) date spans from per-day visitor counts.

    Days with no traffic (paused / out of quota) split the spans. Returns
    [{start, end, days}] using day-start unix timestamps.
    """
    active = sorted(t for t, v in _vwo_per_day(data).items() if v > 0)
    periods: list[dict] = []
    for ts in active:
        if periods and ts - periods[-1]["end"] <= 86400 * 1.5:  # contiguous day → same span
            periods[-1]["end"] = ts
            periods[-1]["days"] += 1
        else:
            periods.append({"start": ts, "end": ts, "days": 1})
    return periods


def vwo_styler(tbl: pd.DataFrame):
    """Styler for st.dataframe: formatted values, best value green, second-best yellow."""
    num_cols = [c for c in tbl.columns if c != "Variation"]

    def fmt_for(c):
        if c.endswith("%"):  # exp.impr% stored as a fraction → render as signed percent
            return lambda v: "" if pd.isna(v) else f"{v * 100:+.2f}%"
        if c.endswith("rev"):
            return lambda v: "" if pd.isna(v) else f"{v:,.0f} Kč"
        return lambda v: "" if pd.isna(v) else f"{int(v):,}"

    def highlight(col):
        s = pd.to_numeric(col, errors="coerce")
        uniq = sorted(s.dropna().unique(), reverse=True)  # higher = better
        best = uniq[0] if uniq else None
        second = uniq[1] if len(uniq) > 1 else None
        out = []
        for v in s:
            if best is not None and v == best:
                out.append("background-color: rgba(76,175,80,0.30)")
            elif second is not None and v == second:
                out.append("background-color: rgba(240,200,70,0.28)")
            else:
                out.append("")
        return out

    return tbl.style.format({c: fmt_for(c) for c in num_cols}).apply(highlight, subset=num_cols)


def vwo_all_goals_table(data: dict) -> pd.DataFrame:
    """Per-variation table with Visitors + every goal's conversions / CR% (+ revenue if present)."""
    goals = data.get("goals", [])
    names = {str(v["id"]): v.get("name", "") for v in data.get("variations", [])}
    ctrl = {str(v["id"]) for v in data.get("variations", []) if v.get("isControl")}
    primary = next((g for g in goals if g.get("isPrimary")), goals[0] if goals else {})
    vids = sorted(primary.get("aggregatedData", {}), key=lambda x: (len(x), x))
    # VWO's expected/relative improvement vs control, keyed by (goal id, variation id).
    vgd = {(str(i.get("goal")), str(i.get("variation"))): i.get("aggregated", {})
           for i in data.get("variationGoalData", [])}
    rows = []
    for vid in vids:
        vis = int(primary.get("aggregatedData", {}).get(vid, {}).get("visitorCount", 0))
        row = {"Variation": f"{vid} · {names.get(vid, '')}" + (" (ctrl)" if vid in ctrl else ""),
               "Visitors": vis}
        for g in goals:
            gname = g.get("name", f"Goal {g.get('id')}")
            ad = g.get("aggregatedData", {}).get(vid, {})
            if g.get("type") == "revenue" or "totalRevenue" in ad:
                row[f"{gname} · rev"] = round(float(ad.get("totalRevenue", 0)), 2)  # conv = same as Conversion
            else:
                row[f"{gname} · conv"] = int(ad.get("conversionCount", 0))
            if g is primary:  # expected improvement only for the conversion goal
                agg2 = vgd.get((str(g.get("id")), vid), {})
                imp = agg2.get("relativeImprovementRate") or agg2.get("relativeExpectedImprovementRate")
                med = imp.get("median") if isinstance(imp, dict) else None
                row[f"{gname} · exp.impr%"] = round(med, 6) if med is not None else None  # fraction
        rows.append(row)
    return pd.DataFrame(rows)

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
COL_PROJECT = "ref_projects"

# ref_projects id → eshop domain.
PROJECT_NAMES = {
    "49": "adrial.eu", "117": "adrialece.ba", "41": "adrialece.hr", "40": "adrialenti.it",
    "114": "alensa.ae", "64": "alensa.at", "71": "alensa.be", "72": "alensa.be.fr",
    "24": "alensa.bg", "104": "alensa.ch", "111": "alensa.co.il", "17": "alensa.co.uk",
    "115": "alensa.com", "92": "alensa.com.mt", "95": "alensa.cz", "81": "alensa.de",
    "67": "alensa.dk", "83": "alensa.ee", "96": "alensa.es", "73": "alensa.fi",
    "51": "alensa.fr", "50": "alensa.gr", "70": "alensa.hr", "76": "alensa.hu",
    "78": "alensa.ie", "75": "alensa.it", "93": "alensa.lt", "119": "alensa.lu",
    "94": "alensa.lv", "60": "alensa.nl", "69": "alensa.no", "77": "alensa.pl",
    "68": "alensa.pt", "85": "alensa.rs", "62": "alensa.ru", "82": "alensa.se",
    "74": "alensa.si", "8": "alensa.sk", "65": "alensa.ua", "2": "cocky-kontaktni.cz",
    "36": "cocky-online.cz", "7": "cocky-optika.cz", "20": "contact-lentile.ro",
    "112": "crulle.at", "102": "crulle.com", "106": "crulle.de", "113": "crulle.dk",
    "107": "crulle.es", "108": "crulle.hu", "109": "crulle.pl", "110": "crulle.pt",
    "38": "ihre-kontaktlinsen.de", "26": "kontaktlinsen-billig.at",
    "46": "kontaktlinsen-billig.ch", "35": "kontaktnesosovky.net", "12": "kontaktni.cz",
    "32": "kontaktnicocky.net", "101": "lecka.net", "39": "lencsebolt.hu",
    "33": "lensboss.ovh", "79": "lentekontakti.al", "91": "lentekontakti.com",
    "47": "lentes-de-contacto.es", "34": "lentes-shop.es", "18": "lenti-ottica.it",
    "15": "leshti.bg", "116": "mataki.gr", "53": "moje-lece.si", "103": "narocilabausch.si",
    "90": "objednavkybausch.cz", "97": "objednavkybausch.sk", "25": "sosovky-kontaktne.sk",
    "86": "vallismg.si", "87": "videt.ro", "28": "xlentile.ro",
}

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

# Internal / team test orders to drop from the report by default. Matched on the
# customer-email column (auto-detected: any column whose name contains "mail").
# Add exact addresses to TEAM_EMAILS, and/or whole domains to TEAM_EMAIL_DOMAINS
# (e.g. "alensa.eu" excludes every @alensa.eu order). Case-insensitive.
TEAM_EMAILS: list[str] = [
    # add specific addresses here if any test orders use a non-alensa.eu email
]
TEAM_EMAIL_DOMAINS: list[str] = [
    "alensa.eu",  # all internal/team test orders use the company domain
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
# Only these columns are read — keeps memory down on large exports (Streamlit Cloud).
USED_COLUMNS = {
    COL_ORDER, COL_TS, COL_PRICE_NET, COL_PRICE_GROSS, COL_AMOUNT, COL_TEST, COL_VARIANT,
    COL_CANCEL, COL_FINAL, COL_ITEMTYPE, COL_PROJITEM, COL_PROJECT,
    "itemname", "commonName", "payment", "orderDestinationCountryId", "delivery_type",
    "orderMonth", "orderDay", "categoriesData-brand", "categoriesData-items-type",
    *PROFIT_COLS.values(),
}
# Repeating-text columns read straight as category to cut peak memory on big CSVs.
CAT_COLUMNS = {
    COL_TEST, COL_VARIANT, COL_ITEMTYPE, "payment", "orderDestinationCountryId",
    "delivery_type", "itemname", "commonName", "orderMonth", "orderDay",
    "categoriesData-brand", "categoriesData-items-type",
}


@st.cache_data(show_spinner=False, max_entries=2)
def load_data(raw: bytes, name: str) -> pd.DataFrame:
    """Parse the uploaded export into a typed DataFrame (only the columns we use)."""
    # Read our known columns plus any email column (auto-detected by name) for team-order exclusion.
    wanted = lambda c: (str(c).strip() in USED_COLUMNS) or ("mail" in str(c).strip().lower())
    if name.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(raw), dtype=str, usecols=wanted)
    else:
        # Semicolon-delimited, UTF-8 BOM. Read repeating text as category (lower peak),
        # the rest as str; coerce numerics after.
        dtypes = {c: ("category" if c in CAT_COLUMNS else str) for c in USED_COLUMNS}
        df = pd.read_csv(
            io.BytesIO(raw), sep=";", dtype=dtypes, usecols=wanted,
            encoding="utf-8-sig", keep_default_na=False,
        )
    df.columns = [c.strip() for c in df.columns]
    # Excel cells come back as NaN for blanks; normalise to "" so string ops and
    # filter sorts behave like the CSV path (which uses keep_default_na=False).
    if df.isna().any().any():
        df = df.fillna("")

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
        df["_brand"] = df["categoriesData-brand"].astype(str).map(
            lambda b: norm_to_brand.get(normalize_text(b), ""))
    else:
        df["_brand"] = [detect_brand(a, b) for a, b in zip(iname.astype(str), cname.astype(str))]
    df["_is_private"] = df["_brand"] != ""

    # Contact-lens flag ("Pouze čočky"): prefer categoriesData-items-type == "Contact
    # lenses"; fall back to the Czech commonName containing "čoč". The private split
    # counts orders that contain a private-brand contact lens.
    if "categoriesData-items-type" in df.columns:
        itype = df["categoriesData-items-type"].astype(str)
        df["_is_lens"] = itype == "Contact lenses"
        df["_item_category"] = itype.where(itype.str.strip() != "", "(uncategorized)")
    else:
        df["_is_lens"] = cname.astype(str).str.contains("čoč", case=False, na=False)
        df["_item_category"] = df["_is_lens"].map({True: "Contact lenses", False: "(uncategorized)"})

    # Team / test-order flag from the customer email (auto-detected column).
    email_col = next((c for c in df.columns if "mail" in c.lower()), None)
    if email_col is not None:
        em = df[email_col].astype(str).str.strip().str.lower()
        team = {e.strip().lower() for e in TEAM_EMAILS if e.strip()}
        doms = tuple("@" + d.strip().lower().lstrip("@") for d in TEAM_EMAIL_DOMAINS if d.strip())
        df["_is_team"] = em.isin(team)
        if doms:
            df["_is_team"] = df["_is_team"] | em.str.endswith(doms)
        df = df.drop(columns=[email_col])  # raw email not needed downstream
    else:
        df["_is_team"] = False

    # Project / eshop label from ref_projects id.
    if COL_PROJECT in df.columns:
        df["_project"] = df[COL_PROJECT].astype(str).str.strip().map(
            lambda x: f"{PROJECT_NAMES.get(x, 'project ' + x)} ({x})")

    # Drop raw source columns now folded into derived ones — frees memory on big files.
    spent = [COL_PRICE_NET, COL_PRICE_GROSS, COL_TS, COL_PROJITEM, COL_PROJECT,
             "categoriesData-brand", "categoriesData-items-type", "_is_lens",
             *PROFIT_COLS.values()]
    df = df.drop(columns=[c for c in spent if c in df.columns])

    # Shrink memory on large files: store repeating text columns as category.
    # (Numerics kept as-is so revenue/margin sums stay exact to the cent.)
    for c in ("ab_test_name", "ab_test_variant", COL_ITEMTYPE, "payment",
              "orderDestinationCountryId", "delivery_type", "itemname", "commonName",
              "orderMonth", "orderDay", "_brand", "_item_category", "_project"):
        if c in df.columns:
            df[c] = df[c].astype("category")
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


@st.cache_data(show_spinner=False, max_entries=2)
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


def clean_test_name(name: str) -> str:
    """Strip the noise from a VWO test name for the selector.

    Removes bracketed segments — e.g. "(PDO, RHE)", country-code lists like
    "(.it, .si, .hr)" — then collapses leftover separators. The project/eshop domain
    in the name (e.g. "ihre-kontaktlinsen.de") is kept. Falls back to the original
    name if cleaning would empty it.
    """
    s = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", name)  # drop (…)/[…]/{…}
    s = re.sub(r"\s{2,}", " ", s).strip(" -–—·,|")
    return s or name.strip()


def build_test_labels(df: pd.DataFrame, tests: list[str], vwo_acc, vwo_token) -> dict[str, str]:
    """Map each test id → 'id — eshop(s) · VWO name' so the Test selector is self-describing.

    Eshop(s) come from the export's ref_projects; the test name from the VWO campaign
    list (one cached call). Either part is omitted if unavailable.
    """
    # eshop domain(s) each test appears in (handles pipe-combined test ids per row)
    test_projects: dict[str, set] = {}
    if COL_TEST in df.columns and COL_PROJECT in df.columns:
        for tname, proj in df[[COL_TEST, COL_PROJECT]].drop_duplicates().itertuples(index=False):
            proj = str(proj).strip()
            for tok in str(tname).split("|"):
                tok = tok.strip()
                if tok and proj:
                    test_projects.setdefault(tok, set()).add(proj)
    # VWO campaign names, keyed by campaign id (cached)
    names: dict[str, str] = {}
    if vwo_token:
        for c in (fetch_vwo_campaign_list(str(vwo_acc), vwo_token) or []):
            names[str(c.get("id"))] = c.get("name", "")
    labels: dict[str, str] = {}
    for t in tests:
        bits = []
        projs = test_projects.get(t)
        if projs:
            doms = sorted({PROJECT_NAMES.get(p, p) for p in projs})
            bits.append(doms[0] if len(doms) == 1 else f"{len(doms)} eshops")
        if names.get(t):
            bits.append(clean_test_name(names[t]))
        labels[t] = f"{t} — " + " · ".join(bits) if bits else t
    return labels


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
MONEY_COLS = ("Revenue", "Avg. Order Val.", "Margin", "Margin/obj")


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
        "Avg. Order Val.": round(revenue / orders, 2) if orders else 0.0,
    }
    if profit_col and profit_col in df.columns:
        margin = float(df[profit_col].sum())
        m["Margin"] = round(margin, 2)
        m["Margin/obj"] = round(margin / orders, 2) if orders else 0.0
        m["Margin %"] = round(margin / revenue, 6) if revenue else 0.0  # fraction
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
        elif isinstance(c, str) and (c.endswith("Δ%") or c == "Improvement %"):
            fmt[c] = lambda v: "" if pd.isna(v) else f"{v * 100:+.2f}%"  # stored as fraction
        elif isinstance(c, str) and (c.startswith("%") or c.endswith("%")):
            fmt[c] = lambda v: "" if pd.isna(v) else f"{v * 100:.2f}%"  # stored as fraction
        elif c == "VWO revenue":
            fmt[c] = lambda v: "" if pd.isna(v) else f"{v:,.0f}"
    return df.style.format(fmt)


def eval_row(g: pd.DataFrame, use_gross: bool, profit_col: str | None = None,
             exclude_cancelled: bool = True) -> dict:
    """Master-sheet-style metrics for a variant slice (which still includes cancelled rows).

    Storno is always reported; when exclude_cancelled is False the cancelled orders are
    also folded into Revenue/Orders/Margin.
    """
    rev_col = "_rev_gross" if use_gross else "_rev_net"
    has_profit = bool(profit_col) and profit_col in g.columns
    canc = g[g[COL_CANCEL] == "1"] if COL_CANCEL in g.columns else g.iloc[0:0]
    base = g[g[COL_CANCEL] != "1"] if (exclude_cancelled and COL_CANCEL in g.columns) else g
    # Revenue/orders/AOV from product lines; margin from ALL lines (incl. shipping/returns).
    if "_is_product" in base.columns:
        prod = base[base["_is_product"]]
    elif "_is_delivery" in base.columns:
        prod = base[~base["_is_delivery"]]
    else:
        prod = base
    orders = int(prod[COL_ORDER].nunique()) if COL_ORDER in prod.columns else 0
    storno = int(canc[COL_ORDER].nunique()) if COL_ORDER in canc.columns else 0
    revenue = float(prod[rev_col].sum()) if rev_col in prod.columns else 0.0
    margin = float(base[profit_col].sum()) if has_profit else 0.0

    row = {
        "Orders": orders,
        "Storno": storno,
        "% storno": round(storno / orders, 6) if orders else 0.0,  # fraction
        "Revenue": round(revenue, 2),
        "Avg. Order Val.": round(revenue / orders, 2) if orders else 0.0,
    }
    if has_profit:
        row["Margin"] = round(margin, 2)
        row["Margin/obj"] = round(margin / orders, 2) if orders else 0.0
        row["Margin %"] = round(margin / revenue, 6) if revenue else 0.0  # fraction
    return row


def download_button(df: pd.DataFrame, label: str, key: str) -> None:
    csv = df.to_csv(index=False, sep=";").encode("utf-8-sig")
    st.download_button(label, csv, file_name=f"{key}.csv", mime="text/csv", key=key)


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
COLUMNS_TO_TICK = [
    "AB test name",
    "AB test variant",
    "Is order cancelled",
    "Item amount",
    "Item price with vat",
    "Order item type",
    "Payment name",
    "Jméno položky",
    "Month of order",
]

RECOMMENDED_SETTINGS = """\
**Recommended export settings**

- **Convert currency**
- **CZK**
- **Owner** (pick one): Alensa s.r.o. · Adrial · Noavidet
- **Ignore showroom restock orders**
- **Export catalogue categories**: Brand, Items type
- **Group by**: Customers
"""


def render_copy_list(items: list[str]) -> None:
    """Compact list with a per-row copy-to-clipboard button."""
    rows = "".join(
        f'<li><span>{c}</span>'
        f'<button title="Copy" onclick="cp(this,\'{c}\')">📋</button></li>'
        for c in items
    )
    html = f"""
    <style>
      body {{ margin:0; background:transparent; }}
      ul.cl {{ list-style:none; padding:0; margin:0;
               font-family:ui-monospace,Menlo,Consolas,monospace; font-size:13px; }}
      ul.cl li {{ display:flex; align-items:center; justify-content:space-between;
                  gap:8px; padding:1px 4px; color:#e6e6e6; }}
      ul.cl li:hover {{ background:rgba(255,255,255,.06); border-radius:4px; }}
      ul.cl button {{ background:none; border:none; cursor:pointer; font-size:12px;
                      opacity:.55; padding:0 2px; }}
      ul.cl button:hover {{ opacity:1; }}
    </style>
    <ul class="cl">{rows}</ul>
    <script>
      function cp(btn, text) {{
        const done = () => {{ const o=btn.textContent; btn.textContent='✓';
                              setTimeout(()=>btn.textContent=o, 800); }};
        if (navigator.clipboard && navigator.clipboard.writeText) {{
          navigator.clipboard.writeText(text).then(done).catch(()=>fallback(text, done));
        }} else {{ fallback(text, done); }}
      }}
      function fallback(text, done) {{
        const t=document.createElement('textarea'); t.value=text;
        document.body.appendChild(t); t.select();
        try {{ document.execCommand('copy'); }} catch(e) {{}}
        document.body.removeChild(t); done();
      }}
    </script>
    """
    components.html(html, height=len(items) * 24 + 8)


def render_vwo_page() -> None:
    """Standalone view: pull live VWO campaign results (visitors/conversions) by id."""
    try:
        token = st.secrets.get("vwo_token")
        acc = st.secrets.get("vwo_account_id", "717496")
    except Exception:
        token, acc = None, "717496"

    st.subheader("VWO campaign results")
    if not token:
        st.info("Add `vwo_token` (and `vwo_account_id`) to Streamlit secrets to use this section.")
        return

    # Either browse the list by name, OR type IDs directly — one or the other.
    browse = st.toggle("Browse by name (otherwise enter IDs below)", value=False)
    dev_split = st.toggle("📱 Add device split (desktop vs mobile + tablet)", value=False,
                          help="Pulls VWO's post-segmentation device breakdown per campaign "
                               "(2 extra API calls each).")
    ids: list[str] = []
    if browse:
        with st.spinner("Loading campaign list from VWO…"):
            campaigns = fetch_vwo_campaign_list(str(acc), token)
        if campaigns:
            status_rank = {"PAUSED": 0, "RUNNING": 1, "STOPPED": 2, "FINISHED": 2, "ENDED": 2,
                           "ARCHIVED": 3}
            def label(c):
                s = f"  · {c['status']}" if c["status"] else ""
                return f"{c['id']} — {c['name']}{s}"
            ordered = sorted(campaigns, key=lambda c: (status_rank.get(str(c["status"]).upper(), 1.5),
                                                       c["name"].lower()))
            query = st.text_input("Search tests (name or id)", placeholder="e.g. invasive").lower().strip()
            shown = [c for c in ordered if not query
                     or query in c["name"].lower() or query in c["id"].lower()]
            by_label = {label(c): c["id"] for c in shown}
            chosen = st.multiselect("Select tests (pick 2+ to compare)", list(by_label))
            ids = [by_label[c] for c in chosen]
            st.caption(f"{len(shown)} of {len(campaigns)} campaigns shown · sorted Paused → Running → "
                       "Stopped → Archived. Compare device-specific campaigns side by side.")
        else:
            st.caption("Couldn't load the campaign list (token/plan) — turn this off and enter IDs.")
    else:
        tokens = [x.strip() for x in
                  st.text_input("Campaign / test IDs (numbers only)",
                                placeholder="e.g. 284, 283, 221").split(",") if x.strip()]
        ids = [t for t in tokens if t.isdigit()]
        not_ids = [t for t in tokens if not t.isdigit()]
        if not_ids:
            st.caption(f"“{', '.join(not_ids)}” aren't IDs — switch on **Browse by name** to search.")

    if not ids:
        st.info("Enter test IDs, or switch on **Browse by name** to pick from the list.")
        return

    # 2 campaigns → side by side; 3+ → stacked vertically (full width, more readable).
    vertical = len(ids) > 2
    cols = [st] * len(ids) if vertical else (st.columns(len(ids)) if len(ids) > 1 else [st])
    for cid, col in zip(ids, cols):
        if vertical:
            col.divider()
        data = fetch_vwo_campaign(str(acc), cid, token)
        if not data or data.get("_error"):
            col.warning(f"Campaign **{cid}**: {data.get('_error', 'no data') if data else 'no data'}")
            continue
        col.markdown(f"#### {cid} — {data.get('name', '')}")
        dr = data.get("dataIntervalRange", {})
        start = fmt_date(dr.get("limitingStartTime") or dr.get("startTime"))
        running = str(data.get("status", "")).upper() == "RUNNING"
        end = (f"{fmt_date(time.time())} (running)" if running
               else fmt_date(dr.get("limitingEndTime") or dr.get("endTime")))
        col.caption(f"status: {data.get('status', '—')} · device: {data.get('device', 'all')} · "
                    f"📅 {start} → {end}")
        tbl = vwo_all_goals_table(data)
        col.dataframe(vwo_styler(tbl), use_container_width=True, hide_index=True)
        download_button(tbl, f"Download VWO {cid}", f"vwo_{cid}")

        if dev_split:
            pid = vwo_primary_goal_id(data)
            goals = data.get("goals", [])
            goal_ids = tuple(str(g.get("id")) for g in goals if g.get("id") is not None)
            rev_id = next((g.get("id") for g in goals if g.get("type") == "revenue"), None)
            s_ts = dr.get("limitingStartTime") or dr.get("startTime")
            e_ts = dr.get("limitingEndTime") or dr.get("endTime")
            if not (pid and s_ts and e_ts):
                col.caption("Device split unavailable — no goal/date range on this campaign.")
            else:
                with st.spinner("Fetching device split from VWO…"):
                    desk = fetch_vwo_segment(str(acc), cid, token, ("desktop",), goal_ids, pid,
                                             rev_id, s_ts, e_ts)
                    mob = fetch_vwo_segment(str(acc), cid, token, ("mobile", "tablet"), goal_ids,
                                            pid, rev_id, s_ts, e_ts)
                err = desk.get("_error") or mob.get("_error")
                if err:
                    col.caption(f"Device split unavailable: {err}")
                else:
                    col.markdown("**Device split** · desktop vs mobile + tablet "
                                 "(primary goal · VWO Impr% vs control within each device)")
                    devt = vwo_device_compare_table(data, desk, mob)
                    col.dataframe(device_styler(devt), use_container_width=True, hide_index=True)
                    download_button(devt, f"Download device split {cid}", f"vwo_dev_{cid}")

        periods = vwo_running_periods(data)
        if periods:
            total = sum(p["days"] for p in periods)
            with col.expander(f"Running periods · {len(periods)} span(s), {total} active days"):
                st.caption("Reconstructed from daily traffic — gaps = paused / out of quota.")
                st.markdown("\n".join(
                    f"- {fmt_date(p['start'])} → {fmt_date(p['end'])}  ·  {p['days']} d" for p in periods))


# --------------------------------------------------------------------------- #
# Google Sheets export — fill the per-eshop template tab (VWO + device + Alensis)
# --------------------------------------------------------------------------- #
# Layout (from the TEMPLATE tab): merged section titles in column A locate each block.
#   "VWO"   title → +1 date · +2 header(eshop) · +3..+6 variants (B:G)
#   "Desktop"/"Mobile" title → +1..+4 variants (B:G)
#   "Alensis" title → +1 date · +2 header · +3..+6 variants (B:K)
GS_VARS = ("1", "2", "3", "4")  # variant ids → rows A, B, C, D

# Alensis block is HEADER-DRIVEN: normalized header label → (vdf column, number format).
# Add a column in the sheet with one of these labels and the app fills it; unknown
# headers are left untouched. normalize_text() lowercases, strips accents & punctuation.
ALENSIS_LABELS = {
    "pocetzobrazeni": ("Visitors", "int"), "zobrazeni": ("Visitors", "int"),
    "pocetkonverziobj": ("Orders", "int"), "konverzepocetobj": ("Orders", "int"),
    "pomerkonverze": ("Conv. rate %", "pct"), "konverznipomer": ("Conv. rate %", "pct"),
    "revenue": ("Revenue", "kc"), "revenuecelkem": ("Revenue", "kc"),
    "prumhodnotaobj": ("Avg. Order Val.", "kc"), "revenueobj": ("Avg. Order Val.", "kc"),
    "celkempocetstornoobj": ("Storno", "int"), "stornopocetobj": ("Storno", "int"),
    "storno": ("% storno", "pct"), "stornopomer": ("% storno", "pct"),
    "marze": ("Margin", "kc"), "prummarzeobj": ("Margin/obj", "kc"),
    # private-brand split
    "pocetobjednavekbezprivatky": ("orders_nonpriv", "int"),
    "pocetobjednaveksprivatkou": ("orders_priv", "int"),
    "objednaveksprivatkou": ("pct_priv", "pct"),
    "prumerhodnotaobjbezprivatky": ("aov_nonpriv", "kc"),
    "prumerhodnotaobjsprivatkou": ("aov_priv", "kc"),
    "prumermarzeobjbezprivatky": ("marginobj_nonpriv", "kc"),
    "prumermarzeobjsprivatkou": ("marginobj_priv", "kc"),
    "prumerhodnotaobjcelkem": ("Avg. Order Val.", "kc"),
    "prumermarzeobjcelkem": ("Margin/obj", "kc"),
}


def _col_letter(n: int) -> str:
    """1-based column index → spreadsheet letter (1→A, 27→AA)."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _private_split_metrics(g: pd.DataFrame, use_gross: bool, profit_col: str | None) -> dict:
    """Per-variant split of full orders into with/without a private brand (+ %, AOV, margin/obj)."""
    out = {"orders_priv": 0, "orders_nonpriv": 0, "pct_priv": None,
           "aov_priv": None, "aov_nonpriv": None,
           "marginobj_priv": None, "marginobj_nonpriv": None}
    if COL_ORDER not in g.columns or "_is_private" not in g.columns:
        return out
    priv_orders = set(g.loc[g["_is_private"], COL_ORDER])
    is_priv = g[COL_ORDER].isin(priv_orders)
    ep = eval_row(g[is_priv], use_gross, profit_col, True) if is_priv.any() else {}
    en = eval_row(g[~is_priv], use_gross, profit_col, True) if (~is_priv).any() else {}
    out["orders_priv"] = ep.get("Orders", 0)
    out["orders_nonpriv"] = en.get("Orders", 0)
    tot = out["orders_priv"] + out["orders_nonpriv"]
    out["pct_priv"] = (out["orders_priv"] / tot) if tot else None
    out["aov_priv"] = ep.get("Avg. Order Val.")
    out["aov_nonpriv"] = en.get("Avg. Order Val.")
    out["marginobj_priv"] = ep.get("Margin/obj")
    out["marginobj_nonpriv"] = en.get("Margin/obj")
    return out


@st.cache_resource(show_spinner=False)
def _gsheets_client():
    """Authorised gspread client from the service-account secret (None if not configured)."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        info = dict(st.secrets["gcp_service_account"])
        creds = Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        return gspread.authorize(creds)
    except Exception:
        return None


def gsheets_ready() -> bool:
    try:
        return ("gcp_service_account" in st.secrets) and _gsheets_client() is not None
    except Exception:
        return False


def _open_spreadsheet(client, id_or_url: str):
    """Open by spreadsheet id or a full edit URL."""
    m = re.search(r"/d/([A-Za-z0-9_-]+)", id_or_url or "")
    return client.open_by_key(m.group(1) if m else id_or_url)


def gsa_email() -> str:
    """The service account's email — the address that must be shared as Editor."""
    try:
        return st.secrets["gcp_service_account"].get("client_email", "")
    except Exception:
        return ""


def show_gsheets_error(e: Exception) -> None:
    """Render a friendly error; for access problems, say exactly who to share with."""
    s = str(e)
    access = (e.__class__.__name__ == "SpreadsheetNotFound"
              or any(k in s for k in ("PERMISSION_DENIED", "permission", "403", "404",
                                      "not found", "The caller does not have permission")))
    if access:
        st.error("⚠️ The app can't open or write to that spreadsheet — it isn't shared with the "
                 "service account (or the link/id is wrong). **Open the spreadsheet → Share → add "
                 "this address as an _Editor_:**")
        st.code(gsa_email() or "(no client_email found in secrets)", language=None)
    else:
        st.error(f"Failed: {type(e).__name__}: {e}")


def campaign_tab_map(sh) -> dict[str, object]:
    """{campaign_id: worksheet} read from each tab's B1 VWO link (e.g. .../ab/234/...)."""
    out: dict[str, object] = {}
    for ws in sh.worksheets():
        try:
            b1 = ws.acell("B1").value or ""
        except Exception:
            b1 = ""
        m = re.search(r"/ab/(\d+)", b1)
        if m:
            out[m.group(1)] = ws
    return out


def _clean(x):
    """Sheet-safe scalar: None/NaN → '' so empty cells clear instead of writing 'nan'."""
    if x is None:
        return ""
    try:
        if pd.isna(x):
            return ""
    except (TypeError, ValueError):
        pass
    return x


def _find_template_anchors(ws) -> dict[str, int]:
    """Row numbers of the merged section titles in column A: vwo / alensis / desktop / mobile."""
    pos: dict[str, int] = {}
    for i, v in enumerate(ws.col_values(1), 1):
        lv = (v or "").strip().lower()
        if lv == "vwo":
            pos.setdefault("vwo", i)
        elif lv == "alensis":
            pos.setdefault("alensis", i)
        elif lv == "desktop":
            pos.setdefault("desktop", i)
        elif lv.startswith("mobile"):
            pos.setdefault("mobile", i)
    return pos


def write_template_blocks(ws, eshop: str, vdf, vwo: dict) -> None:
    """Fill the VWO block (+ Desktop/Mobile) and the Alensis block on a template tab.

    Locates each block by its merged title (column A). Writes only the data grids
    (single cells) — never the merged titles/headers. Percentages go in as fractions;
    Alensis money is CZK; VWO money keeps the tab's own (as-is) cell format.
    """
    pos = _find_template_anchors(ws)
    if "vwo" not in pos or "alensis" not in pos:
        raise RuntimeError("Template anchors not found — the tab needs merged 'VWO' and "
                           "'Alensis' titles in column A (build it from the TEMPLATE tab).")
    vmap = {str(r["Variant"]): r for _, r in vdf.iterrows()} if vdf is not None else {}
    values, fmts = [], []
    pct = {"numberFormat": {"type": "PERCENT", "pattern": "0.00%"}}
    kc = {"numberFormat": {"type": "NUMBER", "pattern": '#,##0.00" Kč"'}}
    intf = {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}

    # ---- VWO block: B..G = Visitors, Conv, Improvement, CR%, Revenue, Avg/obj ----
    v0 = pos["vwo"]
    values += [{"range": f"A{v0 + 1}", "values": [[vwo.get("date", "")]]},
               {"range": f"A{v0 + 2}", "values": [[eshop]]}]
    main = []
    for v in GS_VARS:
        m = vwo.get("main", {}).get(v, {})
        d = "Baseline" if v == "1" else _clean(m.get("impr"))
        main.append([_clean(m.get("vis")), _clean(m.get("conv")), d,
                     _clean(m.get("cr")), _clean(m.get("rev")), _clean(m.get("avg"))])
    values.append({"range": f"B{v0 + 3}:G{v0 + 6}", "values": main})
    fmts.append({"range": f"D{v0 + 3}:E{v0 + 6}", "format": pct})  # Improvement + CR (VWO money as-is)

    # ---- Desktop / Mobile sub-blocks: B..G (no Improvement column) ----
    for key in ("desktop", "mobile"):
        if key in pos:
            d0 = pos[key]
            rows = []
            for v in GS_VARS:
                m = vwo.get(key, {}).get(v, {})
                rows.append([_clean(m.get("vis")), _clean(m.get("conv")), "",
                             _clean(m.get("cr")), _clean(m.get("rev")), _clean(m.get("avg"))])
            values.append({"range": f"B{d0 + 1}:G{d0 + 4}", "values": rows})
            fmts.append({"range": f"E{d0 + 1}:E{d0 + 4}", "format": pct})

    # ---- Alensis block: header-driven — fill each column by matching its header label ----
    a0 = pos["alensis"]
    values += [{"range": f"A{a0 + 1}", "values": [[vwo.get("date", "")]]},
               {"range": f"A{a0 + 2}", "values": [[eshop]]}]
    fmt_obj = {"int": intf, "pct": pct, "kc": kc}
    header = ws.row_values(a0 + 2)  # the template's labels are already there
    for idx, label in enumerate(header, start=1):
        spec = ALENSIS_LABELS.get(normalize_text(label))
        if not spec:
            continue
        vcol, fmt = spec
        col = _col_letter(idx)
        colvals = [[_clean(vmap[v][vcol]) if (v in vmap and vcol in vdf.columns) else ""]
                   for v in GS_VARS]
        values.append({"range": f"{col}{a0 + 3}:{col}{a0 + 6}", "values": colvals})
        fmts.append({"range": f"{col}{a0 + 3}:{col}{a0 + 6}", "format": fmt_obj[fmt]})

    ws.batch_update(values, value_input_option="USER_ENTERED")
    ws.batch_format(fmts)


def compute_variant_block(df: pd.DataFrame, test_id: str, use_gross: bool,
                          profit_col: str | None, vwo_acc, vwo_token):
    """Per-variant df + VWO variation names for one test id (no extra filters). For fill-all."""
    w = df.copy()
    w["_variant"] = [variant_in_test(n, v, str(test_id))
                     for n, v in zip(w[COL_TEST].astype(str), w[COL_VARIANT].astype(str))]
    w = w[w["_variant"].notna() & (w["_variant"] != "")]
    if w.empty:
        return None, {}
    rows = [{"Variant": var, **eval_row(g, use_gross, profit_col, True),
             **_private_split_metrics(g, use_gross, profit_col)}
            for var, g in w.groupby("_variant")]
    vdf = pd.DataFrame(rows).sort_values("Variant").reset_index(drop=True)
    var_names: dict[str, str] = {}
    if vwo_token:
        data = fetch_vwo_campaign(str(vwo_acc), str(test_id), vwo_token)
        if data and not data.get("_error"):
            counts = vwo_primary_counts(data)
            vis = pd.to_numeric(vdf["Variant"].map(lambda v: counts.get(str(v), {}).get("visitors")),
                                errors="coerce")
            vdf["Visitors"] = vis.astype("Int64")
            cr = vdf["Orders"] / vis
            vdf["Conv. rate %"] = cr.where(vis.notna() & (vis != 0)).round(6)  # fraction; ∞/NaN→NA
            var_names = {str(x["id"]): x.get("name", "") for x in data.get("variations", [])}
    return vdf, var_names


def compute_template_data(df: pd.DataFrame, test_id: str, use_gross: bool,
                          profit_col: str | None, vwo_acc, vwo_token):
    """Everything to fill a template tab for one test: (alensis vdf, VWO dict).

    VWO dict: {date, main, desktop, mobile} where each variant maps to
    {vis, conv, impr, cr, rev, avg} (fractions for cr/impr; rev as VWO returns it).
    """
    vdf, _ = compute_variant_block(df, test_id, use_gross, profit_col, vwo_acc, vwo_token)
    vwo = {"date": "", "main": {}, "desktop": {}, "mobile": {}}
    if not vwo_token:
        return vdf, vwo
    data = fetch_vwo_campaign(str(vwo_acc), str(test_id), vwo_token)
    if not data or data.get("_error"):
        return vdf, vwo
    counts = vwo_primary_counts(data)
    impr = vwo_primary_improvement(data)
    revv = vwo_revenue_value(data)
    for v in GS_VARS:
        c = counts.get(v, {})
        vis, conv, rev = c.get("visitors"), c.get("conversions"), revv.get(v)
        vwo["main"][v] = {"vis": vis, "conv": conv, "impr": impr.get(v),
                          "cr": (conv / vis) if vis else None,
                          "rev": rev, "avg": (rev / conv) if (rev is not None and conv) else None}
    dr = data.get("dataIntervalRange", {})
    s = dr.get("limitingStartTime") or dr.get("startTime")
    e = dr.get("limitingEndTime") or dr.get("endTime")
    if s:
        running = str(data.get("status", "")).upper() == "RUNNING"
        vwo["date"] = f"{fmt_date(s)} - {fmt_date(time.time()) if running else fmt_date(e)}"
    pid = vwo_primary_goal_id(data)
    goals = data.get("goals", [])
    gids = tuple(str(g.get("id")) for g in goals if g.get("id") is not None)
    rid = next((g.get("id") for g in goals if g.get("type") == "revenue"), None)
    if pid and s and e:
        for key, devs in (("desktop", ("desktop",)), ("mobile", ("mobile", "tablet"))):
            seg = fetch_vwo_segment(str(vwo_acc), str(test_id), vwo_token, devs, gids, pid, rid, s, e)
            if isinstance(seg, dict) and not seg.get("_error"):
                for v in GS_VARS:
                    sd = seg.get(v, {})
                    vis, conv, rev = sd.get("visitors"), sd.get("conversions"), sd.get("revenue")
                    vwo[key][v] = {"vis": vis, "conv": conv,
                                   "cr": (conv / vis) if vis else None, "rev": rev,
                                   "avg": (rev / conv) if (rev is not None and conv) else None}
    return vdf, vwo


def disable_clear_cache_shortcut() -> None:
    """Disable Streamlit's bare-'c' "Clear cache" hotkey.

    That shortcut wipes the cache and forces a full reload of large exports (costly /
    OOM-prone here). A listener attached from the component iframe dies when Streamlit
    reruns and tears the iframe down — leaving a gap where a quick 'c' still fires. So
    we instead inject a <script> into the *parent* document, so the capture-phase
    keydown listener lives in the app's own realm and persists across reruns. It also
    blocks keypress/keyup as a backstop. Ctrl/Cmd+C (copy) and typing in inputs are
    untouched. Guarded by element id so it's injected only once.
    """
    components.html(
        """
        <script>
        (function () {
          var p = window.parent, d = p && p.document;
          if (!d || d.getElementById('ab-no-cache-clear')) return;
          var sc = d.createElement('script');
          sc.id = 'ab-no-cache-clear';
          sc.textContent =
            "(function(){" +
            "  function block(e){" +
            "    if((e.key==='c'||e.key==='C')&&!e.ctrlKey&&!e.metaKey&&!e.altKey){" +
            "      var t=e.target||{}, tag=(t.tagName||'').toLowerCase();" +
            "      if(tag!=='input'&&tag!=='textarea'&&!t.isContentEditable){" +
            "        e.stopImmediatePropagation(); e.preventDefault();" +
            "      }" +
            "    }" +
            "  }" +
            "  ['keydown','keypress','keyup'].forEach(function(ev){" +
            "    window.addEventListener(ev, block, true);" +     // capture → before Streamlit
            "    document.addEventListener(ev, block, true);" +
            "  });" +
            "})();";
          (d.head || d.documentElement).appendChild(sc);
        })();
        </script>
        """,
        height=0,
    )


def main() -> None:
    st.set_page_config(page_title="A/B Sales Analyzer", layout="wide")
    disable_clear_cache_shortcut()
    head_l, head_r = st.columns([4, 1], vertical_alignment="center")
    head_l.title("A/B Sales Analyzer")
    with head_r:
        with st.popover("⚙️ Default export settings", use_container_width=True):
            st.markdown("**Tick these columns** — click 📋 to copy")
            render_copy_list(COLUMNS_TO_TICK)
            st.markdown(RECOMMENDED_SETTINGS)

    section = st.sidebar.radio("Section", ["📊 Sales analyzer", "🧪 VWO campaigns"])
    st.sidebar.divider()
    if section.endswith("VWO campaigns"):
        render_vwo_page()
        return

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

    use_gross = sb.radio("Revenue basis", ["Gross", "Net"], horizontal=True,
                         format_func=lambda x: f"{x} (default)" if x == "Gross" else x) == "Gross"

    # Margin basis (only the bases whose source column exists in the file).
    available = {lbl: f"_profit_{lbl.lower()}" for lbl in PROFIT_COLS
                 if f"_profit_{lbl.lower()}" in df.columns}
    profit_col = None
    if available:
        basis = sb.radio("Margin basis", list(available), horizontal=True,
                         format_func=lambda x: f"{x} (default)" if x == "Standard" else x,
                         help="Standard = sell − purchase price (item_profit); "
                              "FIFO = sell − accounting FIFO price. Taken from the file as-is (CZK).")
        profit_col = available[basis]

    try:
        vwo_token, vwo_acc = st.secrets.get("vwo_token"), st.secrets.get("vwo_account_id", "717496")
    except Exception:
        vwo_token, vwo_acc = None, "717496"

    tests = test_options(df)
    test_labels = build_test_labels(df, tests, vwo_acc, vwo_token)
    test_choice = sb.selectbox("Test", [NO_TEST] + tests,
                               format_func=lambda t: test_labels.get(t, t))
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
        v = work[COL_VARIANT].astype(str) if COL_VARIANT in work.columns else pd.Series("", index=work.index)
        work["_variant"] = v.where(v != "", "(none)")

    # Project / eshop (only when the export spans more than one). Empty = all.
    if "_project" in df.columns and df["_project"].nunique() > 1:
        projs = sorted(df["_project"].unique())
        sel_proj = sb.multiselect("Project / eshop", projs)
        if sel_proj:
            work = work[work["_project"].isin(sel_proj)]

    # Date window
    if "_order_dt" in df.columns and df["_order_dt"].notna().any():
        dmin = df["_order_dt"].min().date()
        dmax = df["_order_dt"].max().date()
        start, end = sb.date_input("Date window", value=(dmin, dmax), min_value=dmin, max_value=dmax)
        mask = (work["_order_dt"].dt.date >= start) & (work["_order_dt"].dt.date <= end)
        work = work[mask]

    # Restrict to the days the VWO test actually ran (excludes paused / out-of-quota gaps).
    if vwo_token and selected_test and "_order_dt" in work.columns:
        if sb.checkbox("Only dates the test was running (VWO)", value=False,
                       help="Keep only orders from days the VWO test collected traffic — "
                            "skips paused / out-of-quota gaps."):
            cdata = fetch_vwo_campaign(str(vwo_acc), str(selected_test), vwo_token)
            if cdata and not cdata.get("_error"):
                active = vwo_active_days(cdata)
                if active:
                    work = work[work["_order_dt"].dt.strftime("%Y-%m-%d").isin(active)]
                    sb.caption(f"Test ran on {len(active)} day(s).")
                else:
                    sb.warning("VWO returned no traffic days for this test.")
            else:
                sb.warning("Couldn't fetch VWO running dates.")

    # Order state. Cancelled exclusion is applied last so the Per-variant tab can
    # still see cancelled rows to compute the Storno count.
    exclude_cancelled = (sb.checkbox(
        "Exclude cancelled", value=True,
        help="When on, cancelled orders are removed from Revenue/Orders/Margin everywhere "
             "(Storno still counts them). Untick to fold cancelled orders back into the totals.")
        if COL_CANCEL in df.columns else False)
    if COL_FINAL in df.columns and sb.checkbox("Final orders only", value=False):
        work = work[work[COL_FINAL] == "1"]

    # Showroom orders (paid via a Showroom payment method) — offer to exclude.
    if "payment" in df.columns:
        is_showroom = df["payment"].astype(str).str.contains("showroom", case=False, na=False)
        if is_showroom.any():
            n_sr = df.loc[is_showroom, COL_ORDER].nunique() if COL_ORDER in df.columns else int(is_showroom.sum())
            if sb.checkbox(f"Exclude showroom orders ({n_sr:,})", value=False,
                           help="Drops orders paid via a Showroom payment method."):
                work = work[~work["payment"].astype(str).str.contains("showroom", case=False, na=False)]

    # Team / internal test orders (matched on the customer email) — excluded by default.
    exclude_team = False
    if "_is_team" in df.columns and df["_is_team"].any():
        n_team = (df.loc[df["_is_team"], COL_ORDER].nunique()
                  if COL_ORDER in df.columns else int(df["_is_team"].sum()))
        exclude_team = sb.checkbox(f"Exclude team / test orders ({n_team:,})", value=True,
                                   help="Drops orders placed with an internal/team email "
                                        "(TEAM_EMAILS / TEAM_EMAIL_DOMAINS) — your A/B test orders. "
                                        "Applies everywhere, including the Google Sheets export.")
        if exclude_team:
            work = work[~work["_is_team"]]
    # df the Google Sheets export uses (team orders dropped to match the on-screen report).
    report_df = df[~df["_is_team"]] if (exclude_team and "_is_team" in df.columns) else df

    # Item type. Selection stored now and applied to work below. In Per-variant it
    # constrains revenue (via _is_product) but never the margin, which spans all lines.
    item_keep = None
    if COL_ITEMTYPE in df.columns:
        types = sorted(t for t in df[COL_ITEMTYPE].unique() if t)
        if types:
            item_keep = sb.multiselect("Item type", types, default=types)

    # Product category (Contact lenses / Solutions / Glasses / …). Used to *qualify*
    # which orders count as private in the Private-brands table — it does NOT shrink
    # the basket (full orders are still measured). Empty = any category qualifies.
    sel_cat = []
    if "_item_category" in df.columns:
        cats = sorted(c for c in df["_item_category"].unique() if c and c != "(uncategorized)")
        sel_cat = sb.multiselect("Product category (private cohort)", cats,
                                 help="In the Private-brands table, only orders containing a "
                                      "private brand in these categories count. Leave empty for any.")

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
        work = work[work["commonName"].astype(str).str.contains(term, case=False, na=False, regex=False)]

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
        def variant_table(src: pd.DataFrame) -> pd.DataFrame:
            rows = [{"Variant": var, **eval_row(grp, use_gross, profit_col, exclude_cancelled)}
                    for var, grp in src.groupby("_variant")]
            t = pd.DataFrame(rows).sort_values("Variant").reset_index(drop=True)
            total = {"Variant": "TOTAL", **eval_row(src, use_gross, profit_col, exclude_cancelled)}
            return pd.concat([t, pd.DataFrame([total])], ignore_index=True)

        cancel_note = ("Storno is excluded from the money columns"
                       if exclude_cancelled else "Storno is INCLUDED in the money columns")
        st.caption(f"{cancel_note}. Revenue/Avg. Order Val. use product lines only; "
                   "Margin includes shipping lines (matching the manual sheet).")
        vdf = variant_table(work_all)

        # VWO visitors + conversion rate (orders ÷ visitors), if a token is configured.
        try:
            vwo_token = st.secrets.get("vwo_token")
            acc = st.secrets.get("vwo_account_id", "717496")
        except Exception:
            vwo_token, acc = None, "717496"
        vwo_cols: list[str] = []
        if vwo_token and selected_test:
            data = fetch_vwo_campaign(str(acc), str(selected_test), vwo_token)
            counts = vwo_primary_counts(data) if data and not data.get("_error") else None
            if counts:
                def col(metric):
                    def f(variant):
                        if variant == "TOTAL":
                            return sum(c[metric] for c in counts.values())
                        return counts.get(str(variant), {}).get(metric)
                    return pd.to_numeric(vdf["Variant"].map(f), errors="coerce").astype("Int64")
                vdf.insert(1, "Visitors", col("visitors"))
                vdf.insert(2, "VWO conv.", col("conversions"))
                vdf.insert(3, "Conv. rate %", (vdf["Orders"] / vdf["Visitors"]).round(6))  # fraction
                impr = vwo_primary_improvement(data)
                vdf.insert(4, "Improvement %",
                           vdf["Variant"].map(lambda v: impr.get(str(v))))  # control/TOTAL → blank
                vwo_cols = ["Visitors", "VWO conv.", "Conv. rate %", "Improvement %"]
                rev = vwo_revenue_value(data)
                if rev:
                    vdf.insert(5, "VWO revenue", pd.to_numeric(
                        vdf["Variant"].map(lambda v: sum(rev.values()) if v == "TOTAL" else rev.get(str(v))),
                        errors="coerce"))
                    vwo_cols.append("VWO revenue")
                st.caption("🔵 tinted columns are from **VWO** (Visitors, VWO conv., Improvement % vs "
                           "control, VWO revenue); the rest is from the **sales export**. Conv. rate % = "
                           "Orders ÷ Visitors. VWO counts are campaign-wide — don't filter by a single project.")
            else:
                st.caption("⚠️ Couldn't fetch VWO data for this campaign (token/plan/campaign id).")

        styler = style_money(vdf)
        if vwo_cols:
            styler = styler.set_properties(subset=vwo_cols,
                                           **{"background-color": "rgba(70,130,255,0.13)"})
        st.dataframe(styler, use_container_width=True, hide_index=True)
        download_button(vdf, "Download per-variant", "per_variant")

        # 📤 Push the per-variant numbers into the finalization Google Sheet's
        # "Pro porovnání z Analyzeru (BG)" block (routed by each tab's B1 campaign link).
        if selected_test:
            with st.expander("📤 Send to Google Sheets (Analyzer comparison block)"):
                try:
                    default_sid = st.secrets.get("gsheets_spreadsheet_id", "")
                except Exception:
                    default_sid = ""
                sid = st.text_input("Target spreadsheet — paste the link or id", value=default_sid,
                                    placeholder="https://docs.google.com/spreadsheets/d/…/edit")
                if not gsheets_ready():
                    st.info(
                        "**One-time setup needed to write.** The spreadsheet above you just paste — "
                        "but to *write* into it the app needs a Google **service-account key** (its "
                        "login). A link alone can't grant write access. Add a `[gcp_service_account]` "
                        "block to the app's secrets once (see README → *Send to Google Sheets*); then "
                        "the buttons appear here. Until then, use **Download per-variant** above and "
                        "paste manually.")
                else:
                    st.caption("✍️ The app writes as the address below — it must be an **Editor** on "
                               "the spreadsheet (Share → add as Editor):")
                    st.code(gsa_email(), language=None)
                c1, c2 = st.columns(2) if gsheets_ready() else (None, None)
                if gsheets_ready() and c1.button(f"Send test {selected_test} → its tab", use_container_width=True):
                    try:
                        sh = _open_spreadsheet(_gsheets_client(), sid)
                        ws = campaign_tab_map(sh).get(str(selected_test))
                        if ws is None:
                            st.error(f"No tab references campaign {selected_test} in its B1 link.")
                        else:
                            tvdf, tvwo = compute_template_data(report_df, selected_test, use_gross,
                                                               profit_col, acc, vwo_token)
                            write_template_blocks(ws, ws.title, tvdf, tvwo)
                            st.success(f"Filled VWO + Alensis blocks on “{ws.title}”.")
                    except Exception as e:  # noqa: BLE001
                        show_gsheets_error(e)
                if gsheets_ready() and c2.button("Fill all tabs from this export", use_container_width=True):
                    try:
                        sh = _open_spreadsheet(_gsheets_client(), sid)
                        tabs = campaign_tab_map(sh)
                        if not tabs:
                            st.warning("No tabs with a VWO campaign link in B1.")
                        else:
                            done, skipped = [], []
                            for cid, ws in tabs.items():
                                tvdf, tvwo = compute_template_data(report_df, cid, use_gross,
                                                                   profit_col, acc, vwo_token)
                                if tvdf is None or tvdf.empty:
                                    skipped.append(f"{ws.title} ({cid})")
                                    continue
                                try:
                                    write_template_blocks(ws, ws.title, tvdf, tvwo)
                                    done.append(ws.title)
                                except Exception as we:  # noqa: BLE001
                                    skipped.append(f"{ws.title} ({we})")
                            st.success(f"Filled {len(done)} tab(s): {', '.join(done) or '—'}")
                            if skipped:
                                st.caption("Skipped: " + "; ".join(skipped))
                    except Exception as e:  # noqa: BLE001
                        show_gsheets_error(e)
                if gsheets_ready():
                    st.caption("Tabs are matched by the VWO campaign id in each tab's **B1** link, and "
                               "blocks by their merged **VWO**/**Desktop**/**Mobile**/**Alensis** titles. "
                               "Both buttons write each campaign's full data (VWO block + device split "
                               "from VWO, Alensis block from the sales export in CZK). Re-sending "
                               "overwrites the data cells in place.")

        # Two VWO-only device tables (mobile+tablet, desktop) under the main per-variant table.
        if vwo_token and selected_test:
            ddata = fetch_vwo_campaign(str(acc), str(selected_test), vwo_token)
            if ddata and not ddata.get("_error"):
                pid = vwo_primary_goal_id(ddata)
                goals = ddata.get("goals", [])
                goal_ids = tuple(str(g.get("id")) for g in goals if g.get("id") is not None)
                rev_id = next((g.get("id") for g in goals if g.get("type") == "revenue"), None)
                dr = ddata.get("dataIntervalRange", {})
                s_ts = dr.get("limitingStartTime") or dr.get("startTime")
                e_ts = dr.get("limitingEndTime") or dr.get("endTime")
                if pid and s_ts and e_ts:
                    with st.spinner("Fetching device split from VWO…"):
                        segs = {
                            "mobile": fetch_vwo_segment(str(acc), str(selected_test), vwo_token,
                                                        ("mobile", "tablet"), goal_ids, pid,
                                                        rev_id, s_ts, e_ts),
                            "desktop": fetch_vwo_segment(str(acc), str(selected_test), vwo_token,
                                                         ("desktop",), goal_ids, pid,
                                                         rev_id, s_ts, e_ts),
                        }
                    st.markdown("##### Device split — VWO only")
                    st.caption("VWO visitors / conversions / revenue for each device segment "
                               "(campaign-wide, all eshops). CR% = conversions ÷ visitors; "
                               "Improvement % is VWO's relative improvement vs control.")
                    # Two half-width tables side by side (mobile left, desktop right).
                    dev_cols = st.columns(2)
                    for (label, key), dcol in zip(
                            (("📱 Mobile + tablet", "mobile"), ("🖥️ Desktop", "desktop")), dev_cols):
                        seg = segs[key]
                        dcol.markdown(f"**{label}**")
                        if seg.get("_error"):
                            dcol.caption(f"Unavailable: {seg['_error']}")
                            continue
                        dt = vwo_device_one_table(ddata, seg)
                        dcol.dataframe(device_variant_styler(dt), use_container_width=True,
                                       hide_index=True)
                        with dcol:
                            download_button(dt, f"Download {key} split", f"per_variant_{key}")

        if show_private:
            cat_note = (f" in {', '.join(sel_cat)}" if sel_cat else "")
            st.markdown("#### Private brands only")
            st.caption(f"Full orders that contain a private brand{cat_note} "
                       "(whole basket measured, like the manual sheet).")
            # Qualify orders: contain a private-brand line (in the chosen categories).
            qual = work_all["_is_private"]
            if sel_cat:
                qual = qual & work_all["_item_category"].isin(sel_cat)
            priv_orders = set(work_all.loc[qual, COL_ORDER])
            priv = work_all[work_all[COL_ORDER].isin(priv_orders)]
            if priv.empty:
                st.info("No orders with a private brand in the current selection.")
            else:
                pdf = variant_table(priv)
                st.dataframe(style_money(pdf), use_container_width=True, hide_index=True)
                download_button(pdf, "Download per-variant (private)", "per_variant_private")

            st.markdown("#### Without private brands")
            st.caption(f"Full orders that do NOT contain a private brand{cat_note} "
                       "(complement of the table above).")
            nonpriv = work_all[~work_all[COL_ORDER].isin(priv_orders)]
            if nonpriv.empty:
                st.info("No non-private orders in the current selection.")
            else:
                ndf = variant_table(nonpriv)
                st.dataframe(style_money(ndf), use_container_width=True, hide_index=True)
                download_button(ndf, "Download per-variant (non-private)", "per_variant_nonpriv")

    with tab_pivot:
        candidates = ["_variant", "_brand", "_item_category", "_project",
                      "orderDestinationCountryId", "payment", "delivery_type",
                      "itemname", "commonName", "orderMonth", "orderDay"]
        group_opts = [c for c in candidates if c in work.columns]
        label_map = {"_variant": "variant", "_brand": "brand", "_item_category": "category",
                     "_project": "project"}
        group_by = st.multiselect(
            "Group by", group_opts, default=["_variant"] if "_variant" in group_opts else [],
            format_func=lambda c: label_map.get(c, c),
        )
        if group_by:
            recs = []
            for keys, grp in work.groupby(group_by, observed=True):
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
