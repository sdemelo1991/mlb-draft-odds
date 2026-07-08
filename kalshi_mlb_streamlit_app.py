# -*- coding: utf-8 -*-
"""
Kalshi MLB Draft Market Viewer
Fetches live data from the Kalshi API — refreshes on every page load / button click.
"""

import streamlit as st
import requests
import pandas as pd
import urllib3
import subprocess
import sys
import json
from collections import defaultdict
from datetime import datetime
import matplotlib.colors as mcolors
import unicodedata
import uuid
import time
import os

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Active-user heartbeat ──────────────────────────────────────────────────────
_HEARTBEAT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".active_sessions.json")
_ACTIVE_WINDOW = 90  # seconds — a session is "active" if it pinged within this window

def _heartbeat():
    """Record this session's timestamp and return count of active sessions."""
    if "session_id" not in st.session_state:
        st.session_state["session_id"] = str(uuid.uuid4())
    sid = st.session_state["session_id"]
    now = time.time()
    try:
        sessions = json.loads(open(_HEARTBEAT_FILE).read()) if os.path.exists(_HEARTBEAT_FILE) else {}
    except Exception:
        sessions = {}
    sessions[sid] = now
    # Prune stale sessions
    sessions = {k: v for k, v in sessions.items() if now - v < _ACTIVE_WINDOW}
    try:
        with open(_HEARTBEAT_FILE, "w") as f:
            json.dump(sessions, f)
    except Exception:
        pass
    return len(sessions)

_active_users = _heartbeat()

st.set_page_config(
    page_title="MLB Draft Tool",
    page_icon="⚾",
    layout="wide",
)

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

MLB_DRAFT_SERIES = [
    ("KXMLBDRAFTPICK", "MLB Draft Pick (exact pick)"),
    ("KXMLBDRAFTTOP",  "MLB Draft Top N"),
]

# ── helpers ────────────────────────────────────────────────────────────────────

def _headers(api_key):
    return {"Authorization": api_key}

def fmt_cents(val):
    try:
        return f"{float(val) * 100:.1f}¢"
    except Exception:
        return "—"

def fmt_vol(val):
    try:
        return f"${float(val):,.0f}"
    except Exception:
        return "—"

def player_name(title):
    if title.startswith("Will ") and " be " in title:
        return title[5 : title.index(" be ")]
    return title

def price_at_depth(ladder, depth_dollars):
    # Walk best bids first (highest price → lowest), accumulate until depth hit
    sorted_ladder = sorted(ladder, key=lambda x: float(x[0]), reverse=True)
    cumulative = 0.0
    for price_str, qty_str in sorted_ladder:
        cumulative += float(qty_str)
        if cumulative >= depth_dollars:
            return float(price_str), cumulative
    if sorted_ladder:
        return float(sorted_ladder[-1][0]), cumulative
    return None, 0.0

# ── API calls ──────────────────────────────────────────────────────────────────

@st.cache_data(ttl=0)
def get_events_for_series(api_key, series_ticker):
    r = requests.get(f"{API_BASE}/events", headers=_headers(api_key),
                     params={"series_ticker": series_ticker, "limit": 100},
                     timeout=30, verify=False)
    r.raise_for_status()
    return r.json().get("events", [])

@st.cache_data(ttl=0)
def get_markets_for_event(api_key, event_ticker):
    r = requests.get(f"{API_BASE}/markets", headers=_headers(api_key),
                     params={"event_ticker": event_ticker, "limit": 200},
                     timeout=30, verify=False)
    r.raise_for_status()
    return r.json().get("markets", [])

def fetch_competitor_odds():
    """Load competitor odds from cache. On Streamlit Cloud, only reads cache (doesn't scrape)."""
    import time

    # Check session cache first (current Streamlit session)
    if "comp_data_cached" in st.session_state and "comp_data_timestamp" in st.session_state:
        cache_age = time.time() - st.session_state.comp_data_timestamp
        if cache_age < 3600:  # 1 hour
            return st.session_state.comp_data_cached, f"(session cached {int(cache_age/60)}m ago)"

    cache_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".comp_cache.json")

    # Check file-based cache (persists across sessions)
    if os.path.exists(cache_file):
        try:
            cache_age = time.time() - os.path.getmtime(cache_file)
            with open(cache_file) as f:
                cached = json.load(f)
                # Store in session cache
                st.session_state.comp_data_cached = cached
                st.session_state.comp_data_timestamp = time.time()
                return cached, f"(cached {int(cache_age/60)}m ago)"
        except:
            pass

    # Only try to scrape if we're NOT on Streamlit Cloud and script exists
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fetch_competitor_odds_mlb.py")
    is_streamlit_cloud = "STREAMLIT_SERVER_HEADLESS" in os.environ

    if not is_streamlit_cloud and os.path.exists(script):
        try:
            result = subprocess.run(
                [sys.executable, script],
                capture_output=True, text=True, timeout=300
            )
            if result.returncode == 0:
                raw = json.loads(result.stdout.strip())
                # Support both old flat list and new dict format
                if isinstance(raw, list):
                    raw = {"picks": raw, "ou": [], "h2h": []}

                # Save to cache
                st.session_state.comp_data_cached = raw
                st.session_state.comp_data_timestamp = time.time()
                try:
                    with open(cache_file, 'w') as f:
                        json.dump(raw, f)
                except:
                    pass

                return raw, result.stderr.strip() or None
        except Exception as e:
            pass  # Fall through to "no cache" error

    # No cache and can't scrape
    return None, "No competitor odds available. Run scraper locally: python fetch_competitor_odds_mlb.py"
    except Exception as e:
        return None, str(e)

@st.cache_data(ttl=3600)
def get_orderbook(api_key, ticker):
    try:
        r = requests.get(f"{API_BASE}/markets/{ticker}/orderbook",
                         headers=_headers(api_key), timeout=15, verify=False,
                         params={"depth": 100})
        if r.status_code == 200:
            return r.json().get("orderbook_fp", {})
    except (requests.exceptions.Timeout, requests.exceptions.ConnectTimeout):
        return {}
    except Exception:
        return {}
    return {}

# ── sidebar ────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("⚾ MLB Draft Tool")
    st.metric("Active Users", _active_users)
    st.divider()
    api_key = st.text_input(
        "Kalshi API Key",
        value=st.session_state.get("api_key", ""),
        type="password",
        help="kalshi.com → Settings → API",
    )
    if api_key:
        st.session_state["api_key"] = api_key

    st.divider()
    st.subheader("Depth Filter")
    yes_depth = st.number_input("YES depth ($)", min_value=1, value=500, step=50)
    no_depth  = st.number_input("NO depth ($)",  min_value=1, value=500, step=50)

    st.divider()
    show_ladders  = st.toggle("Show full liquidity ladders", value=False)
    sort_by_price = st.toggle("Sort by YES bid ↓", value=True)

    st.divider()
    if st.button("🔄 Refresh", type="primary", use_container_width=True):
        get_events_for_series.clear()
        get_markets_for_event.clear()
        get_orderbook.clear()

    st.caption(f"Last loaded: {datetime.now().strftime('%H:%M:%S')}")
    st.caption("Auto-refreshes every 60 s")

st.title("⚾ MLB Draft Tool")

if not api_key:
    st.info("Enter your Kalshi API key in the sidebar to load markets.")
    st.stop()

# ── load all data upfront (shared between tabs) ────────────────────────────────

# all_markets: list of dicts with extra keys injected for context
all_markets = []          # flat list of every active market
all_orderbooks = {}       # ticker → orderbook dict

for series_ticker, series_label in MLB_DRAFT_SERIES:
    try:
        events = get_events_for_series(api_key, series_ticker)
    except Exception:
        continue
    events = sorted(events, key=lambda e: e.get("event_ticker", ""))
    for event in events:
        event_ticker = event.get("event_ticker", "")
        event_title  = event.get("title", event_ticker)
        try:
            markets = get_markets_for_event(api_key, event_ticker)
        except Exception:
            continue
        for m in markets:
            if m.get("status") != "active":
                continue
            m["_event_title"]  = event_title
            m["_series_label"] = series_label
            m["_player"]       = player_name(m.get("title", m.get("ticker", "")))
            all_markets.append(m)

# Fetch orderbooks (with timeout to prevent hanging)
import concurrent.futures
def fetch_orderbook_safe(ticker):
    try:
        return ticker, get_orderbook(api_key, ticker)
    except Exception as e:
        st.debug(f"Orderbook fetch error for {ticker}: {str(e)[:50]}")
        return ticker, {}

with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
    futures = {executor.submit(fetch_orderbook_safe, m["ticker"]): m["ticker"] for m in all_markets[:50]}  # Limit to first 50
    for future in concurrent.futures.as_completed(futures, timeout=30):
        try:
            ticker, ob = future.result()
            all_orderbooks[ticker] = ob
        except Exception:
            pass

# ── Alert detection (runs every refresh before tabs render) ───────────────────
ALERT_THRESHOLD = 0.0099  # >0.99¢ move triggers an alert

if "price_snapshot" not in st.session_state:
    st.session_state["price_snapshot"] = {}
if "alert_log" not in st.session_state:
    st.session_state["alert_log"] = []

# Clear snapshot if it's in the old format (plain floats instead of dicts)
if st.session_state["price_snapshot"]:
    sample = next(iter(st.session_state["price_snapshot"].values()))
    if not isinstance(sample, dict):
        st.session_state["price_snapshot"] = {}

prev_snapshot = st.session_state["price_snapshot"]
new_snapshot  = {}

for m in all_markets:
    ticker   = m["ticker"]
    yes_bid  = float(m.get("yes_bid_dollars") or 0)
    no_bid   = float(m.get("no_bid_dollars")  or 0)

    new_snapshot[ticker] = {"yes": yes_bid, "no": no_bid}

    if ticker in prev_snapshot:
        for side, current, key in [
            ("YES", yes_bid, "yes"),
            ("NO",  no_bid,  "no"),
        ]:
            prev = prev_snapshot[ticker].get(key, 0)
            change = current - prev
            if abs(change) > ALERT_THRESHOLD:
                direction = "🔺" if change > 0 else "🔻"
                st.session_state["alert_log"].insert(0, {
                    "Time":    datetime.now().strftime("%H:%M:%S"),
                    "Player":  m["_player"],
                    "Market":  m["_event_title"],
                    "Side":    side,
                    "From":    f"{prev*100:.1f}¢",
                    "To":      f"{current*100:.1f}¢",
                    "Change":  f"{direction} {change*100:+.1f}¢",
                })

st.session_state["price_snapshot"] = new_snapshot

tab_market, tab_player, tab_grid, tab_alerts, tab_sportsbook, tab_mock_consensus = st.tabs(["📋 Kalshi By Market", "👤 Kalshi By Player", "🔢 Kalshi Grid", "🔔 Kalshi Alerts", "📊 Comps", "🎯 Mocks"])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — By Market (original view)
# ══════════════════════════════════════════════════════════════════════════════

with tab_market:
    # Group markets back into series → event structure for display
    from itertools import groupby

    series_groups = defaultdict(lambda: defaultdict(list))
    for m in all_markets:
        series_groups[m["_series_label"]][m["_event_title"]].append(m)

    for series_label, events_dict in series_groups.items():
        st.header(series_label)
        for event_title, markets in events_dict.items():
            if sort_by_price:
                markets = sorted(markets, key=lambda m: float(m.get("yes_bid_dollars") or 0), reverse=True)

            with st.expander(f"**{event_title}**", expanded=True):
                rows = []
                for m in markets:
                    ob         = all_orderbooks.get(m["ticker"], {})
                    yes_ladder = ob.get("yes_dollars", [])
                    no_ladder  = ob.get("no_dollars",  [])
                    yes_price, yes_avail = price_at_depth(yes_ladder, yes_depth)
                    no_price,  no_avail  = price_at_depth(no_ladder,  no_depth)
                    overround = (yes_price + no_price) * 100 if yes_price and no_price else None
                    rows.append({
                        "Player":              m["_player"],
                        f"YES @ ${yes_depth}": f"{yes_price*100:.1f}¢" if yes_price else "—",
                        "YES Avail":           f"${yes_avail:,.0f}" if yes_avail else "—",
                        f"NO @ ${no_depth}":   f"{no_price*100:.1f}¢"  if no_price  else "—",
                        "NO Avail":            f"${no_avail:,.0f}"  if no_avail  else "—",
                        "Overround":           f"{overround:.0f}¢"  if overround else "—",
                        "Volume":              fmt_vol(m.get("volume_fp")),
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

                if show_ladders:
                    st.markdown("##### Liquidity Ladders (top 5)")
                    top5 = sorted(markets, key=lambda m: float(m.get("yes_bid_dollars") or 0), reverse=True)[:5]
                    cols = st.columns(min(len(top5), 5))
                    for col, m in zip(cols, top5):
                        ob       = all_orderbooks.get(m["ticker"], {})
                        yes_side = ob.get("yes_dollars", [])
                        no_side  = ob.get("no_dollars",  [])
                        with col:
                            st.markdown(f"**{m['_player']}**  \n{fmt_cents(m.get('yes_bid_dollars'))} bid / {fmt_cents(m.get('yes_ask_dollars'))} ask")
                            if yes_side:
                                df = pd.DataFrame(yes_side, columns=["Price", "Qty ($)"])
                                df = df.sort_values("Price", key=lambda s: s.apply(float), ascending=False)
                                df["Price"]   = df["Price"].apply(lambda x: f"{float(x)*100:.1f}¢")
                                df["Qty ($)"] = df["Qty ($)"].apply(lambda x: f"${float(x):,.0f}")
                                st.caption("YES bids")
                                st.dataframe(df, use_container_width=True, hide_index=True, height=200)
                            if no_side:
                                df = pd.DataFrame(no_side, columns=["Price", "Qty ($)"])
                                df = df.sort_values("Price", key=lambda s: s.apply(float), ascending=False)
                                df["Price"]   = df["Price"].apply(lambda x: f"{float(x)*100:.1f}¢")
                                df["Qty ($)"] = df["Qty ($)"].apply(lambda x: f"${float(x):,.0f}")
                                st.caption("NO bids")
                                st.dataframe(df, use_container_width=True, hide_index=True, height=200)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — By Player
# ══════════════════════════════════════════════════════════════════════════════

with tab_player:
    st.header("Player View — odds across all markets")

    # Desired event order
    EVENT_ORDER = [
        "Pro Baseball #1 Overall Pick",
        "Pro Baseball #2 Overall Pick",
        "Pro Baseball #3 Overall Pick",
        "Pro Baseball #4 Overall Pick",
        "Pro Baseball #5 Overall Pick",
        "Pro Baseball Players Drafted Top 3",
        "Pro Baseball Players Drafted Top 5",
        "Pro Baseball Drafted Top 10",
    ]

    def event_sort_key(m):
        t = m["_event_title"]
        try:
            return EVENT_ORDER.index(t)
        except ValueError:
            return len(EVENT_ORDER)  # unknown events go last

    # Group all markets by player name
    player_markets = defaultdict(list)
    for m in all_markets:
        player_markets[m["_player"]].append(m)

    # Sort players: by their best YES bid across any market, descending
    def best_bid(markets):
        return max((float(m.get("yes_bid_dollars") or 0) for m in markets), default=0)

    sorted_players = sorted(player_markets.items(), key=lambda x: best_bid(x[1]), reverse=True)

    if not sorted_players:
        st.info("No data loaded yet.")
    else:
        # Build a wide summary table: one row per player, columns = each market/event
        # Use the same fixed order as the per-player detail
        present = {m["_event_title"] for m in all_markets}
        event_order = [e for e in EVENT_ORDER if e in present]
        # Append any unexpected events not in the list
        for m in all_markets:
            if m["_event_title"] not in event_order:
                event_order.append(m["_event_title"])

        # Summary table rows
        summary_rows = []
        for pname, markets in sorted_players:
            row = {"Player": pname}
            mkt_by_event = {m["_event_title"]: m for m in markets}
            for evt in event_order:
                m = mkt_by_event.get(evt)
                if m:
                    ob         = all_orderbooks.get(m["ticker"], {})
                    yes_ladder = ob.get("yes_dollars", [])
                    yes_price, _ = price_at_depth(yes_ladder, yes_depth)
                    row[evt] = f"{yes_price*100:.1f}¢" if yes_price else fmt_cents(m.get("yes_bid_dollars"))
                else:
                    row[evt] = "—"
            summary_rows.append(row)

        st.markdown(f"##### YES price at ${yes_depth} depth across all markets")
        st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

        st.divider()

        # Per-player expanders with full detail
        st.markdown("##### Per-player detail")
        for pname, markets in sorted_players:
            best = best_bid(markets)
            with st.expander(f"**{pname}**  —  best YES bid {fmt_cents(best)}", expanded=False):
                rows = []
                for m in sorted(markets, key=event_sort_key):
                    ob         = all_orderbooks.get(m["ticker"], {})
                    yes_ladder = ob.get("yes_dollars", [])
                    no_ladder  = ob.get("no_dollars",  [])
                    yes_price, yes_avail = price_at_depth(yes_ladder, yes_depth)
                    no_price,  no_avail  = price_at_depth(no_ladder,  no_depth)
                    overround = (yes_price + no_price) * 100 if yes_price and no_price else None
                    rows.append({
                        "Market":              m["_event_title"],
                        "YES Bid":             fmt_cents(m.get("yes_bid_dollars")),
                        "YES Ask":             fmt_cents(m.get("yes_ask_dollars")),
                        f"YES @ ${yes_depth}": f"{yes_price*100:.1f}¢" if yes_price else "—",
                        f"NO @ ${no_depth}":   f"{no_price*100:.1f}¢"  if no_price  else "—",
                        "Overround":           f"{overround:.0f}¢"      if overround else "—",
                        "Volume":              fmt_vol(m.get("volume_fp")),
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

                if show_ladders:
                    cols = st.columns(min(len(markets), 4))
                    for col, m in zip(cols, sorted(markets, key=event_sort_key)):
                        ob       = all_orderbooks.get(m["ticker"], {})
                        yes_side = ob.get("yes_dollars", [])
                        no_side  = ob.get("no_dollars",  [])
                        with col:
                            st.markdown(f"**{m['_event_title']}**")
                            if yes_side:
                                df = pd.DataFrame(yes_side, columns=["Price", "Qty ($)"])
                                df = df.sort_values("Price", key=lambda s: s.apply(float), ascending=False)
                                df["Price"]   = df["Price"].apply(lambda x: f"{float(x)*100:.1f}¢")
                                df["Qty ($)"] = df["Qty ($)"].apply(lambda x: f"${float(x):,.0f}")
                                st.caption("YES bids")
                                st.dataframe(df, use_container_width=True, hide_index=True, height=180)
                            if no_side:
                                df = pd.DataFrame(no_side, columns=["Price", "Qty ($)"])
                                df = df.sort_values("Price", key=lambda s: s.apply(float), ascending=False)
                                df["Price"]   = df["Price"].apply(lambda x: f"{float(x)*100:.1f}¢")
                                df["Qty ($)"] = df["Qty ($)"].apply(lambda x: f"${float(x):,.0f}")
                                st.caption("NO bids")
                                st.dataframe(df, use_container_width=True, hide_index=True, height=180)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — Grid
# ══════════════════════════════════════════════════════════════════════════════

GRID_EVENTS = [
    "Pro Baseball #1 Overall Pick",
    "Pro Baseball #2 Overall Pick",
    "Pro Baseball #3 Overall Pick",
    "Pro Baseball #4 Overall Pick",
    "Pro Baseball #5 Overall Pick",
]

with tab_grid:
    st.header("Grid — YES Bid / Total Liquidity by Pick")

    # Collect players and their markets for picks 1-5
    player_markets_grid = defaultdict(dict)
    for m in all_markets:
        if m["_event_title"] in GRID_EVENTS:
            player_markets_grid[m["_player"]][m["_event_title"]] = m

    if not player_markets_grid:
        st.info("No pick markets loaded yet.")
    else:
        # Sort players by best YES bid across the 5 pick markets
        def best_bid_grid(mkt_dict):
            return max((float(m.get("yes_bid_dollars") or 0) for m in mkt_dict.values()), default=0)

        sorted_grid_players = sorted(
            player_markets_grid.items(), key=lambda x: best_bid_grid(x[1]), reverse=True
        )

        # Only include events present in the data, in order
        present_grid = {m["_event_title"] for m in all_markets if m["_event_title"] in GRID_EVENTS}
        grid_cols = [e for e in GRID_EVENTS if e in present_grid]

        # Shorten column headers
        col_labels = {
            "Pro Baseball #1 Overall Pick": "#1 Overall",
            "Pro Baseball #2 Overall Pick": "#2 Overall",
            "Pro Baseball #3 Overall Pick": "#3 Overall",
            "Pro Baseball #4 Overall Pick": "#4 Overall",
            "Pro Baseball #5 Overall Pick": "#5 Overall",
        }

        rows = []
        for pname, mkt_dict in sorted_grid_players:
            row = {"Player": pname}
            for evt in grid_cols:
                m = mkt_dict.get(evt)
                if m:
                    bid = float(m.get("yes_bid_dollars") or 0)
                    ob  = all_orderbooks.get(m["ticker"], {})
                    total_liq = sum(float(qty) for _, qty in ob.get("yes_dollars", []))
                    bid_str = f"{bid*100:.1f}¢"
                    liq_str = f"${total_liq:,.0f}"
                    row[col_labels[evt]] = f"{bid_str} / {liq_str}"
                else:
                    row[col_labels[evt]] = "—"
            rows.append(row)

        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — Alerts
# ══════════════════════════════════════════════════════════════════════════════

with tab_alerts:
    st.header("🔔 Price Movement Alerts")
    st.caption(f"Triggers when YES bid moves more than 0.99¢ between refreshes")

    col1, col2 = st.columns([1, 5])
    with col1:
        if st.button("🗑️ Clear Alerts"):
            st.session_state["alert_log"] = []
            st.rerun()
    with col2:
        st.caption(f"{len(st.session_state['alert_log'])} alert(s) logged this session")

    if not st.session_state["alert_log"]:
        st.info("No alerts yet — watching for moves > 0.99¢ on every refresh.")
    else:
        df_alerts = pd.DataFrame(st.session_state["alert_log"])
        st.dataframe(df_alerts, use_container_width=True, hide_index=True)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — Sportsbook (Comps)
# ══════════════════════════════════════════════════════════════════════════════

PICK_ORDER = ["#1 Overall", "#2 Overall", "#3 Overall", "#4 Overall", "#5 Overall", "Top 3 Pick", "Top 5 Pick", "Top 10 Pick"]
OVERALL_PICKS = ["#1 Overall", "#2 Overall", "#3 Overall", "#4 Overall", "#5 Overall"]
TOP_PICKS = ["Top 3 Pick", "Top 5 Pick", "Top 10 Pick"]
BOOKS_ORDER = ["Kalshi", "FanDuel", "Bookmaker", "DraftKings", "Caesars", "Bet365", "Kambi", "Betano", "Bet99", "BetMGM"]

# Maps Kalshi event titles → PICK_ORDER labels
KALSHI_EVENT_TO_PICK = {
    "Pro Baseball #1 Overall Pick": "#1 Overall",
    "Pro Baseball #2 Overall Pick": "#2 Overall",
    "Pro Baseball #3 Overall Pick": "#3 Overall",
    "Pro Baseball #4 Overall Pick": "#4 Overall",
    "Pro Baseball #5 Overall Pick": "#5 Overall",
    "Pro Baseball Players Drafted Top 3": "Top 3 Pick",
    "Pro Baseball Players Drafted Top 3 in 2026?": "Top 3 Pick",
    "Pro Baseball Drafted Top 3": "Top 3 Pick",  # Fallback for title variation
    "Pro Baseball Players Drafted Top 5": "Top 5 Pick",
    "Pro Baseball Players Drafted Top 5 in 2026?": "Top 5 Pick",
    "Pro Baseball Drafted Top 5": "Top 5 Pick",  # Fallback for title variation
    "Pro Baseball Players Drafted Top 10": "Top 10 Pick",
    "Pro Baseball Players Drafted Top 10 in 2026?": "Top 10 Pick",
    "Pro Baseball Drafted Top 10": "Top 10 Pick",  # Fallback for title variation
}

def _brand_cmap(name, light, dark):
    return mcolors.LinearSegmentedColormap.from_list(name, [light, dark])

# Per-book color maps — light tint → brand primary
BOOK_CMAPS = {
    "Kalshi":        _brand_cmap("kalshi",  "#e6f9ef", "#00b84a"),  # Kalshi green
    "FanDuel":       _brand_cmap("fd",      "#e5f2ff", "#1493ff"),  # FanDuel blue
    "DraftKings":    _brand_cmap("dk",      "#edfae4", "#53d337"),  # DraftKings green
    "BetMGM":        _brand_cmap("mgm",     "#fff8e1", "#c8922a"),  # BetMGM gold
    "Bet365":        _brand_cmap("b365",    "#ffe0e0", "#fbb034"),  # Bet365 orange
    "Betano":        _brand_cmap("btn",     "#fff3e0", "#e65100"),  # Betano orange
    "Bet99":         _brand_cmap("b99",     "#f3e5f5", "#7b1fa2"),  # Bet99 purple
    "Bookmaker":     _brand_cmap("bm",      "#fdecea", "#e53935"),  # Bookmaker red
    "Caesars":       _brand_cmap("caesars", "#f0e6ff", "#9c27b0"),  # Caesars purple
    "Kambi":         _brand_cmap("ns",      "#f5f5f5", "#424242"),  # Kambi grey
}
# Solid accent for the Best Odds cell (slightly darker for white-text contrast)
BOOK_ACCENT = {
    "Kalshi":        "#008c38",  # Kalshi dark green
    "FanDuel":       "#0070d1",  # FanDuel dark blue
    "DraftKings":    "#3aaa1e",  # DraftKings dark green
    "BetMGM":        "#a67420",  # BetMGM dark gold
    "Bet365":        "#d4791e",  # Bet365 dark orange
    "Betano":        "#bf360c",  # Betano dark orange
    "Bet99":         "#4a148c",  # Bet99 dark purple
    "Bookmaker":     "#b71c1c",  # Bookmaker dark red
    "Caesars":       "#6a1b9a",  # Caesars dark purple
    "Kambi":         "#212121",  # near-black
}

with tab_sportsbook:
    st.header("📊 MLB Draft Comps")
    st.caption("Sources: Kalshi (YES ask) · Betano · DraftKings · BetMGM · (Others — coming soon). Takes ~60–90 s.")

    if st.button("🔄 Refresh Competitor Odds", key="comp_refresh"):
        # Clear competitor odds cache
        st.session_state.pop("comp_data_cached", None)
        st.session_state.pop("comp_data_timestamp", None)

    with st.spinner("Fetching odds from all books… (60–90 s)"):
        comp_data, comp_err = fetch_competitor_odds()

    if comp_err and not comp_data:
        st.error(f"Could not fetch competitor odds: {comp_err}")
    elif comp_err:
        with st.expander("Fetch warnings", expanded=False):
            st.code(comp_err)

    comp_rows     = (comp_data or {}).get("picks", [])
    comp_ou       = (comp_data or {}).get("ou", [])
    comp_h2h      = (comp_data or {}).get("h2h", [])
    manual_books  = (comp_data or {}).get("manual", {})

    if not comp_rows and not comp_ou and not comp_h2h:
        st.warning("No odds data returned.")
        if manual_books:
            st.info("**Manual/placeholder books:**")
            for book, status in manual_books.items():
                st.caption(f"• {book}: {status}")
    else:
        def _normalize_player(name):
            # Handle non-string inputs (floats, NaN, etc.)
            if not isinstance(name, str):
                return str(name) if name is not None else ""
            ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
            if ascii_name.lower() in ("field", "the field", "any other", "any other player"):
                return "Field"
            # Remove periods and extra spaces to handle variations like "A.J. Gracia" vs "AJ Gracia"
            ascii_name = ascii_name.replace(".", "").replace("  ", " ").strip()
            return ascii_name

        # Load mock consensus data for reference
        mock_lookup = {}
        try:
            mock_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_draft_data.json")
            if os.path.exists(mock_file):
                with open(mock_file) as f:
                    mock_data = json.load(f)
                    for player_data in mock_data.get("players", []):
                        player_name = _normalize_player(player_data["name"])  # Normalize for consistent matching
                        avg = player_data.get("avg", 0)
                        min_pick = player_data.get("min", 0)
                        max_pick = player_data.get("max", 0)
                        range_str = f"{min_pick}/{max_pick}" if min_pick and max_pick else "—"
                        mock_lookup[player_name] = {
                            "avg": f"{avg:.1f}",
                            "range": range_str
                        }
        except Exception as e:
            logger.debug(f"Could not load mock data: {e}")

        df_all = pd.DataFrame(comp_rows) if comp_rows else pd.DataFrame()
        if not df_all.empty:
            df_all["player"] = df_all["player"].apply(_normalize_player)

        books_present = [b for b in BOOKS_ORDER if not df_all.empty and b in df_all["book"].values]

        # Build Kalshi YES-ask lookup: {pick_label: {player: implied%}}
        # Uses yes_ask (cost to buy a Yes contract) as the implied probability.
        kalshi_lookup: dict[str, dict[str, float]] = defaultdict(dict)
        for _m in all_markets:
            _event_title = _m["_event_title"]
            _pick = KALSHI_EVENT_TO_PICK.get(_event_title)

            # Fallback: flexible matching for title variations
            if not _pick:
                title_lower = _event_title.lower()
                if "top 3" in title_lower or "#3 in 2026" in title_lower or "drafted in the 3" in title_lower:
                    _pick = "Top 3 Pick"
                elif "top 5" in title_lower or "#5 in 2026" in title_lower or "drafted in the 5" in title_lower:
                    _pick = "Top 5 Pick"
                elif "top 10" in title_lower or "#10 in 2026" in title_lower or "drafted in the 10" in title_lower:
                    _pick = "Top 10 Pick"

            if not _pick:
                continue
            _ask = _m.get("yes_ask_dollars")
            if _ask is None:
                continue
            try:
                _imp = round(float(_ask) * 100, 1)  # dollars → cents = implied %
            except Exception:
                continue
            if _imp > 0:
                kalshi_lookup[_pick][_m["_player"]] = _imp

        if kalshi_lookup:
            books_present = ["Kalshi"] + [b for b in books_present if b != "Kalshi"]
        st.caption(f"Books loaded: {', '.join(books_present)}")

        # Debug: show available markets
        with st.expander("🔍 Debug: Available Kalshi Markets", expanded=False):
            st.write("Markets found:")
            for pick_type, players_dict in kalshi_lookup.items():
                st.write(f"- {pick_type}: {len(players_dict)} players")

        if manual_books:
            with st.expander("ℹ️ Manual/placeholder books", expanded=False):
                for book, status in manual_books.items():
                    st.caption(f"**{book}:** {status}")

        # 2026 MLB Draft order (https://www.mlb.com/draft/2026/order)
        PICK_TEAM_LOGO = {
            "#1 Overall":  ("CWS", "https://a.espncdn.com/i/teamlogos/mlb/500/cws.png"),
            "#2 Overall":  ("TB",  "https://a.espncdn.com/i/teamlogos/mlb/500/tb.png"),
            "#3 Overall":  ("MIN", "https://a.espncdn.com/i/teamlogos/mlb/500/min.png"),
            "#4 Overall":  ("SF",  "https://a.espncdn.com/i/teamlogos/mlb/500/sf.png"),
            "#5 Overall":  ("PIT", "https://a.espncdn.com/i/teamlogos/mlb/500/pit.png"),
        }

        # ── Comps subtabs ─────────────────────────────────────────────────
        st.markdown("""<style>
        .stTabs [data-baseweb="tab"] { font-size: 1.4rem !important; padding: 12px 24px !important; }
        .stTabs [data-baseweb="tab"] p { font-size: 1.4rem !important; }
        .stTabs [data-baseweb="tab-list"] button { font-size: 1.4rem !important; }
        </style>""", unsafe_allow_html=True)
        _ctab_picks, _ctab_top, _ctab_ou, _ctab_h2h = st.tabs(["🎯 Overall 1–5", "🏆 Top 3/5/10", "📈 O/U", "⚔️ H2H"])

        with _ctab_picks:
            # ── Overall picks 1-5 ────────────────────────────────────────────────
            for pick in OVERALL_PICKS:
                df_pick = df_all[df_all["market"] == pick].copy() if not df_all.empty else pd.DataFrame()
                # Skip only if both sportsbooks AND Kalshi have no data
                if df_pick.empty and (pick not in kalshi_lookup or not kalshi_lookup[pick]):
                    continue

                # Styled pick header with team logo
                _team_info = PICK_TEAM_LOGO.get(pick)
                _logo_html = (
                    f"<img src='{_team_info[1]}' height='28' "
                    f"style='vertical-align:middle;margin-right:8px;border-radius:3px;'>"
                    if _team_info else ""
                )
                st.markdown(
                    f"<div style='background:#1e3a5f;color:white;padding:6px 12px;"
                    f"border-radius:4px;font-weight:600;font-size:1.05rem;margin-top:16px;"
                    f"display:flex;align-items:center;'>"
                    f"{_logo_html}{pick}</div>",
                    unsafe_allow_html=True,
                )

                # Pivot implied % — rows = players, columns = ALL books (including placeholders)
                # Include all books from BOOKS_ORDER, not just those with data
                pivot = df_pick.pivot_table(
                    index="player", columns="book", values="implied", aggfunc="max"
                )
                if pivot is not None and not pivot.empty:
                    pivot.index = pivot.index.map(_normalize_player)  # Ensure normalization
                # Reindex to include all books, filling missing with NaN (shows as N/A)
                all_books = [b for b in BOOKS_ORDER if b != "Kalshi"]
                if pivot is not None and not pivot.empty:
                    pivot = pivot.reindex(columns=all_books)
                else:
                    pivot = pd.DataFrame()

                # Merge Kalshi YES-ask column (join on player name index)
                if kalshi_lookup.get(pick) and kalshi_lookup[pick]:
                    normalized_pick = {_normalize_player(p): v for p, v in kalshi_lookup[pick].items()}
                    if pivot.empty:
                        # If no sportsbook data, create pivot from Kalshi data only
                        pivot = pd.DataFrame({
                            "Kalshi": list(kalshi_lookup[pick].values())
                        }, index=pd.Index([_normalize_player(p) for p in kalshi_lookup[pick].keys()]))
                    else:
                        pivot["Kalshi"] = pivot.index.map(normalized_pick)
                        # Reorder so Kalshi is first
                        pivot = pivot[["Kalshi"] + [c for c in pivot.columns if c not in ["Kalshi", "Average", "Range", "Best Odds", "Best Book"]]]

                # Get list of book columns (excluding helper columns)
                imp_cols = [col for col in pivot.columns if col not in ['Best Odds', 'Best Book', 'Average', 'Range']]

                # Best Odds = lowest implied % (best value) across books with data
                if imp_cols:
                    pivot["Best Odds"] = pivot[imp_cols].min(axis=1)
                # idxmin raises on all-NA rows; mask those rows first
                has_any = pivot[imp_cols].notna().any(axis=1)
                pivot["Best Book"] = None
                if has_any.any():
                    pivot.loc[has_any, "Best Book"] = pivot.loc[has_any, imp_cols].idxmin(axis=1)

                # Add Mock Average and Mock Range
                pivot["Mock Avg"] = pivot.index.map(lambda p: mock_lookup.get(p, {}).get("avg", "—"))
                pivot["Mock Range"] = pivot.index.map(lambda p: mock_lookup.get(p, {}).get("range", "—"))

                # Sort by Best Odds, highest to lowest
                pivot = pivot.sort_values("Best Odds", ascending=False)
                pivot.index.name = "Player"

                # Reorder columns: Mock Avg/Mock Range first (after player), then books, then Best Odds/Best Book
                final_cols = ["Mock Avg", "Mock Range"] + [c for c in pivot.columns if c in imp_cols] + ["Best Odds", "Best Book"]
                pivot = pivot[[c for c in final_cols if c in pivot.columns]]

                # Per-book gradient + Best Odds cell colored to match best book
                styled = pivot.style.format(
                    "{:.1f}", subset=imp_cols + ["Best Odds"], na_rep="N/A"
                ).format("{}", subset=["Mock Avg", "Mock Range", "Best Book"], na_rep="—")
                for _b in imp_cols:
                    _cmap = BOOK_CMAPS.get(_b, "Blues")
                    styled = styled.background_gradient(cmap=_cmap, subset=[_b], axis=0)

                def _color_cells(row, _accent=BOOK_ACCENT, _books=imp_cols):
                    result = pd.Series("", index=row.index)
                    # White out NaN book cells (background_gradient renders them black)
                    for b in _books:
                        if pd.isna(row.get(b)):
                            result[b] = "background-color:white;color:#888;font-style:italic"
                    # Accent the Best Odds cell
                    book = row.get("Best Book", "")
                    color = _accent.get(book, "#1565c0")
                    result["Best Odds"] = f"background-color:{color};color:white;font-weight:600"
                    return result

                styled = styled.apply(_color_cells, axis=1)
                # Center-align all data cells; index (Player) stays left via its <th> element
                styled = styled.map(lambda _: "text-align:center")
                styled = styled.set_table_styles([
                    {"selector": "", "props": [("border-collapse", "collapse"), ("width", "100%"), ("font-size", "0.83rem")]},
                    {"selector": "th", "props": [("padding", "6px 10px"), ("background-color", "#f0f2f6"), ("border", "1px solid #ddd"), ("text-align", "center"), ("white-space", "nowrap")]},
                    {"selector": "th.row_heading", "props": [("text-align", "left"), ("min-width", "140px")]},
                    {"selector": "td", "props": [("padding", "5px 8px"), ("border", "1px solid #ddd"), ("white-space", "nowrap")]},
                    {"selector": "tr:hover td", "props": [("filter", "brightness(0.96)")]},
                ])
                st.markdown(
                    f"<div style='overflow-x:auto;margin-bottom:8px;'>{styled.to_html()}</div>",
                    unsafe_allow_html=True,
                )

            def _to_american(dec):
                if dec is None or dec <= 1:
                    return "N/A"
                if dec >= 2.0:
                    return f"+{int(round((dec - 1) * 100))}"
                return f"{int(round(-100 / (dec - 1)))}"

            def _implied_to_american(implied):
                """Convert implied probability (0-100) to American odds."""
                if implied is None or implied <= 0 or implied >= 100:
                    return "N/A"
                prob = implied / 100.0
                if prob >= 0.5:
                    return f"{int(round(-100 * prob / (1 - prob)))}"
                else:
                    return f"+{int(round(100 * (1 - prob) / prob))}"

        with _ctab_top:
            # ── Top 3/5/10 picks ────────────────────────────────────────────────
            picks_to_show = []
            for pick in TOP_PICKS:
                if not df_all.empty and pick in df_all["market"].values:
                    picks_to_show.append(pick)
                elif pick in kalshi_lookup and kalshi_lookup[pick]:
                    picks_to_show.append(pick)

            for pick in picks_to_show:
                df_pick = df_all[df_all["market"] == pick].copy() if not df_all.empty else pd.DataFrame()

                # Styled pick header
                st.markdown(
                    f"<div style='background:#1e3a5f;color:white;padding:6px 12px;"
                    f"border-radius:4px;font-weight:600;font-size:1.05rem;margin-top:16px;'>"
                    f"{pick}</div>",
                    unsafe_allow_html=True,
                )

                # Pivot implied % — rows = players, columns = ALL books
                pivot = df_pick.pivot_table(
                    index="player", columns="book", values="implied", aggfunc="max"
                )
                if pivot is not None and not pivot.empty:
                    pivot.index = pivot.index.map(_normalize_player)  # Ensure normalization
                all_books = [b for b in BOOKS_ORDER if b != "Kalshi"]
                if pivot is not None and not pivot.empty:
                    pivot = pivot.reindex(columns=all_books)
                else:
                    pivot = pd.DataFrame()

                # Merge Kalshi YES-ask column
                if kalshi_lookup.get(pick) and kalshi_lookup[pick]:
                    normalized_pick = {_normalize_player(p): v for p, v in kalshi_lookup[pick].items()}
                    if pivot.empty:
                        # If no sportsbook data, create pivot from Kalshi data only
                        pivot = pd.DataFrame({
                            "Kalshi": list(kalshi_lookup[pick].values())
                        }, index=pd.Index([_normalize_player(p) for p in kalshi_lookup[pick].keys()]))
                    else:
                        pivot["Kalshi"] = pivot.index.map(normalized_pick)
                        pivot = pivot[["Kalshi"] + [c for c in pivot.columns if c != "Kalshi"]]

                # Best Odds and Best Book columns
                imp_cols = [col for col in pivot.columns if col not in ['Best Odds', 'Best Book', 'Average', 'Range', 'Mock Avg', 'Mock Range']]
                if imp_cols:
                    pivot["Best Odds"] = pivot[imp_cols].min(axis=1)  # Show min (best value)

                    # Add Mock Average and Mock Range
                    pivot["Mock Avg"] = pivot.index.map(lambda p: mock_lookup.get(p, {}).get("avg", "—"))
                    pivot["Mock Range"] = pivot.index.map(lambda p: mock_lookup.get(p, {}).get("range", "—"))

                has_any = pivot[imp_cols].notna().any(axis=1)
                pivot["Best Book"] = None
                if has_any.any():
                    pivot.loc[has_any, "Best Book"] = pivot.loc[has_any, imp_cols].idxmin(axis=1)  # Find book with lowest implied %

                # Sort by Best Odds, highest to lowest
                pivot = pivot.sort_values("Best Odds", ascending=False)
                pivot.index.name = "Player"

                # Reorder columns: Mock Avg/Mock Range first (after player), then books, then Best Odds/Best Book
                final_cols = ["Mock Avg", "Mock Range"] + [c for c in pivot.columns if c in imp_cols] + ["Best Odds", "Best Book"]
                pivot = pivot[[c for c in final_cols if c in pivot.columns]]

                # Styling
                styled = pivot.style.format(
                    "{:.1f}", subset=imp_cols + ["Best Odds"], na_rep="N/A"
                ).format("{}", subset=["Mock Avg", "Mock Range", "Best Book"], na_rep="—")
                for _b in imp_cols:
                    _cmap = BOOK_CMAPS.get(_b, "Blues")
                    styled = styled.background_gradient(cmap=_cmap, subset=[_b], axis=0)

                def _color_cells(row, _accent=BOOK_ACCENT, _books=imp_cols):
                    result = pd.Series("", index=row.index)
                    for b in _books:
                        if pd.isna(row.get(b)):
                            result[b] = "background-color:white;color:#888;font-style:italic"
                    book = row.get("Best Book", "")
                    color = _accent.get(book, "#1565c0")
                    result["Best Odds"] = f"background-color:{color};color:white;font-weight:600"
                    return result

                styled = styled.apply(_color_cells, axis=1)
                styled = styled.map(lambda _: "text-align:center")
                styled = styled.set_table_styles([
                    {"selector": "", "props": [("border-collapse", "collapse"), ("width", "100%"), ("font-size", "0.83rem")]},
                    {"selector": "th", "props": [("padding", "6px 10px"), ("background-color", "#f0f2f6"), ("border", "1px solid #ddd"), ("text-align", "center"), ("white-space", "nowrap")]},
                    {"selector": "th.row_heading", "props": [("text-align", "left"), ("min-width", "140px")]},
                    {"selector": "td", "props": [("padding", "5px 8px"), ("border", "1px solid #ddd"), ("white-space", "nowrap")]},
                    {"selector": "tr:hover td", "props": [("filter", "brightness(0.96)")]},
                ])
                st.markdown(
                    f"<div style='overflow-x:auto;margin-bottom:8px;'>{styled.to_html()}</div>",
                    unsafe_allow_html=True,
                )

        with _ctab_ou:
            # ── O/U section ──────────────────────────────────────────────────────
            if comp_ou:
                st.markdown(
                    "<div style='background:#1a3a2a;color:white;padding:8px 14px;"
                    "border-radius:4px;font-weight:700;font-size:1.1rem;margin-top:28px'>"
                    "O/Us (Draft Position)</div>",
                    unsafe_allow_html=True,
                )
                # Normalize player names and group by player + book + line
                rows_ou = {}
                for row in comp_ou:
                    player = _normalize_player(row["player"])
                    book = row["book"]
                    line = row["line"]  # e.g., "Under 7.5" or "Over 7.5"
                    implied = row["implied"]
                    # Convert implied % to American odds
                    american = _implied_to_american(implied)

                    # Extract line number and direction
                    if line.startswith("Under"):
                        line_num = line.replace("Under ", "")
                        direction = "u"
                    elif line.startswith("Over"):
                        line_num = line.replace("Over ", "")
                        direction = "o"
                    else:
                        continue

                    if player not in rows_ou:
                        rows_ou[player] = {}
                    if book not in rows_ou[player]:
                        rows_ou[player][book] = {}
                    if line_num not in rows_ou[player][book]:
                        rows_ou[player][book][line_num] = {}

                    rows_ou[player][book][line_num][direction] = american

                # Convert to dataframe: pair U/O for each line
                df_ou_data = {}
                for player in sorted(rows_ou.keys()):
                    row_data = {}
                    for book in rows_ou[player]:
                        pairs = []
                        for line_num in sorted(rows_ou[player][book].keys(), key=float):
                            u_odds = rows_ou[player][book][line_num].get("u", "—")
                            o_odds = rows_ou[player][book][line_num].get("o", "—")
                            pairs.append(f"u{line_num} {u_odds} / o{line_num} {o_odds}")
                        row_data[book] = " | ".join(pairs)
                    df_ou_data[player] = row_data

                df_ou_pivot = pd.DataFrame.from_dict(df_ou_data, orient="index")
                df_ou_pivot.index = df_ou_pivot.index.map(_normalize_player)  # Normalize for mock_lookup matching
                df_ou_pivot.index.name = "Player"
                ou_book_order = [b for b in BOOKS_ORDER if b in df_ou_pivot.columns]
                df_ou_pivot = df_ou_pivot.reindex(columns=ou_book_order)

                # Add Mock Average and Mock Range columns
                df_ou_pivot.insert(0, "Mock Avg", df_ou_pivot.index.map(lambda p: mock_lookup.get(p, {}).get("avg", "—")))
                df_ou_pivot.insert(1, "Mock Range", df_ou_pivot.index.map(lambda p: mock_lookup.get(p, {}).get("range", "—")))

                _ou_styled = df_ou_pivot.style.format(
                    "{}", subset=["Mock Avg", "Mock Range"], na_rep="—"
                ).map(lambda _: "text-align:center;font-size:0.9rem").set_table_styles([
                    {"selector": "", "props": [("border-collapse", "collapse"), ("width", "100%"), ("font-size", "0.83rem")]},
                    {"selector": "th", "props": [("padding", "6px 10px"), ("background-color", "#f0f2f6"), ("border", "1px solid #ddd"), ("text-align", "center"), ("white-space", "nowrap")]},
                    {"selector": "th.row_heading", "props": [("text-align", "left"), ("min-width", "140px")]},
                    {"selector": "td", "props": [("padding", "5px 8px"), ("border", "1px solid #ddd"), ("white-space", "normal")]},
                ])
                st.markdown(
                    f"<div style='overflow-x:auto;margin-bottom:8px;'>{_ou_styled.to_html()}</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.info("No O/U data loaded yet.")

        with _ctab_h2h:
            # ── H2H section ──────────────────────────────────────────────────────
            if comp_h2h:
                st.markdown(
                    "<div style='background:#2a1a3a;color:white;padding:8px 14px;"
                    "border-radius:4px;font-weight:700;font-size:1.1rem;margin-top:28px'>"
                    "Head-to-Head (Who Gets Drafted First)</div>",
                    unsafe_allow_html=True,
                )
                # Extract matchups and odds from H2H data
                matchup_odds = {}
                for row in comp_h2h:
                    player = _normalize_player(row["player"])
                    opponent = _normalize_player(row["vs"])
                    book = row["book"]
                    implied = row["implied"]

                    # Normalize matchup key: always use sorted names to avoid duplicates
                    key = " vs ".join(sorted([player, opponent]))
                    if key not in matchup_odds:
                        matchup_odds[key] = {}
                    if book not in matchup_odds[key]:
                        matchup_odds[key][book] = {}

                    # Store probability for this player in this matchup
                    matchup_odds[key][book][player] = implied

                # Build display table: matchups as rows, books as columns
                rows_h2h_display = {}
                for matchup in sorted(matchup_odds.keys()):
                    row_data = {}
                    for book in sorted(matchup_odds[matchup].keys()):
                        odds_for_book = matchup_odds[matchup][book]
                        # Display both sides of the matchup with American odds
                        parts = []
                        for player in sorted(odds_for_book.keys()):
                            prob = odds_for_book[player]
                            american = _implied_to_american(prob)
                            parts.append(f"{player} {american}")
                        row_data[book] = " / ".join(parts)
                    rows_h2h_display[matchup] = row_data

                df_h2h_pivot = pd.DataFrame.from_dict(rows_h2h_display, orient="index")
                df_h2h_pivot.index.name = "Matchup"
                h2h_book_order = [b for b in BOOKS_ORDER if b in df_h2h_pivot.columns]
                df_h2h_pivot = df_h2h_pivot.reindex(columns=h2h_book_order)

                # Add Mock data columns for both players in matchup
                def get_mock_data_for_matchup(matchup_str, data_type="avg"):
                    players = matchup_str.split(" vs ")
                    if len(players) == 2:
                        p1, p2 = _normalize_player(players[0]), _normalize_player(players[1])
                        mock_p1 = mock_lookup.get(p1, {})
                        mock_p2 = mock_lookup.get(p2, {})
                        if mock_p1 or mock_p2:
                            p1_val = mock_p1.get(data_type, "—")
                            p2_val = mock_p2.get(data_type, "—")
                            return f"{p1_val} / {p2_val}"
                    return "—"

                df_h2h_pivot.insert(0, "Mock Avg", df_h2h_pivot.index.map(lambda x: get_mock_data_for_matchup(x, "avg")))
                df_h2h_pivot.insert(1, "Mock Range", df_h2h_pivot.index.map(lambda x: get_mock_data_for_matchup(x, "range")))

                _h2h_styled = df_h2h_pivot.style.format(
                    "{}", subset=["Mock Avg", "Mock Range"], na_rep="—"
                ).map(lambda _: "text-align:center").set_table_styles([
                    {"selector": "", "props": [("border-collapse", "collapse"), ("width", "100%"), ("font-size", "0.83rem")]},
                    {"selector": "th", "props": [("padding", "6px 10px"), ("background-color", "#f0f2f6"), ("border", "1px solid #ddd"), ("text-align", "center"), ("white-space", "nowrap")]},
                    {"selector": "th.row_heading", "props": [("text-align", "left"), ("min-width", "200px")]},
                    {"selector": "td", "props": [("padding", "5px 8px"), ("border", "1px solid #ddd"), ("white-space", "nowrap")]},
                ])
                st.markdown(
                    f"<div style='overflow-x:auto;margin-bottom:8px;'>{_h2h_styled.to_html()}</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.info("No H2H data loaded yet.")


# ═══════════════════════════════════════════════════════════════════════════════
# MOCK CONSENSUS (Separate Section)
# ═══════════════════════════════════════════════════════════════════════════════

st.divider()
with tab_mock_consensus:
    st.header("📋 Mocks")
    try:
        mock_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_draft_data.json")
        if os.path.exists(mock_file):
            with open(mock_file) as f:
                mock_data = json.load(f)

            mocks_info = mock_data.get("mocks", {})
            players_data = mock_data.get("players", [])
            mock_order = ["Law", "Kiley", "Callis", "Doyle", "Collazo", "Mayo"]

            # Prepare data with average and range (min/max format)
            mock_rows = []
            for player_data in players_data:
                player_name = player_data["name"]
                picks = player_data["picks"]
                avg_pick = player_data.get("avg", 0)
                min_pick = player_data.get("min", 0)
                max_pick = player_data.get("max", 0)
                range_str = f"{min_pick}/{max_pick}" if min_pick and max_pick else "—"

                mock_rows.append({
                    "player": player_name,
                    "avg": avg_pick,  # Store as float for sorting
                    "avg_str": f"{avg_pick:.1f}",
                    "range": range_str,
                    "min": min_pick,
                    **{mock: int(picks.get(mock, "—")) if picks.get(mock, "—") and picks.get(mock, "—") != "—" else "—" for mock in mock_order}
                })

            # Default sort by Avg (will be sorted client-side on header click)
            mock_rows.sort(key=lambda x: x["avg"])

            # Build dataframe
            df_mocks = pd.DataFrame(mock_rows)
            df_mocks.index = range(1, len(df_mocks) + 1)

            # Reorder columns (use avg_str for display)
            col_order = ["player", "avg_str", "range"] + mock_order
            df_mocks = df_mocks[col_order]
            df_mocks.columns = ["Player", "Avg", "Range"] + mock_order

            # Initialize session state for mock sorting
            if "mock_sort_col" not in st.session_state:
                st.session_state.mock_sort_col = "Avg"

            # Build column headers with logos and dates
            header_info = []
            for m in mock_order:
                mock_info = mocks_info[m]
                logo_url = mock_info.get("logo_url", "") if isinstance(mock_info, dict) else ""
                date = mock_info.get("date", "—") if isinstance(mock_info, dict) else "—"
                header_info.append((m, logo_url, date))

            # Sort the data based on session state
            sort_col = st.session_state.mock_sort_col
            if sort_col == "Player":
                mock_rows.sort(key=lambda x: x["player"])
            elif sort_col in mock_order:
                mock_rows.sort(key=lambda x: (x[sort_col], x["avg"]))
            else:  # Avg
                mock_rows.sort(key=lambda x: x["avg"])

            # Get sort column from URL
            sort_col = st.query_params.get("mock_sort", "Avg") if st.query_params else "Avg"

            # Re-sort data based on current sort column
            if sort_col == "Player":
                mock_rows.sort(key=lambda x: x["player"])
            elif sort_col in mock_order:
                mock_rows.sort(key=lambda x: (x[sort_col], x["avg"]))
            else:  # Avg (default)
                mock_rows.sort(key=lambda x: x["avg"])

            # Build HTML table with clickable headers
            th_style = "padding:6px 10px;text-align:center;vertical-align:middle;border:1px solid #ddd;white-space:nowrap;cursor:pointer;"
            th_no_sort = "padding:6px 10px;text-align:center;vertical-align:middle;border:1px solid #ddd;white-space:nowrap;"
            td_style = "padding:8px 12px;border:1px solid #ddd;text-align:center;"
            td_name = "padding:8px 12px;border:1px solid #ddd;text-align:left;"

            html_table = "<table style='border-collapse:collapse; width:100%; font-size:0.85rem;'>"
            html_table += "<thead><tr style='background-color:#f0f2f6;'>"
            html_table += f"<th style='{th_no_sort}'>#</th>"
            html_table += f"<th style='{th_style}text-align:left;' onclick=\"window.history.replaceState(null, '', '?mock_sort=Player'); location.reload();\"  ><a href='#' style='color:inherit;text-decoration:none;display:block;' onclick='return false;'>Player</a></th>"
            html_table += f"<th style='{th_style}' onclick=\"window.history.replaceState(null, '', '?mock_sort=Avg'); location.reload();\"><a href='#' style='color:inherit;text-decoration:none;display:block;' onclick='return false;'>Avg</a></th>"
            html_table += f"<th style='{th_no_sort}'>Range</th>"

            for mocker, logo_url, date in header_info:
                logo_html = f"<img src='{logo_url}' height='16' style='display:block;margin:0 auto 2px;'>" if logo_url else ""
                html_table += f"<th style='{th_style}' onclick=\"window.location.search='mock_sort={mocker}'; return false;\"><a href='#' style='color:inherit;text-decoration:none;display:block;' onclick='return false;'>{logo_html}{mocker}<div style='font-size:0.7rem;margin-top:2px;'>({date})</div></a></th>"

            html_table += "</tr></thead><tbody>"

            for idx, row in enumerate(mock_rows, 1):
                html_table += f"<tr><td style='{td_style}'>{idx}</td>"
                html_table += f"<td style='{td_name}'>{row['player']}</td>"
                html_table += f"<td style='{td_style}'>{row['avg_str']}</td>"
                html_table += f"<td style='{td_style}'>{row['range']}</td>"

                for m in mock_order:
                    value = row.get(m, "—")
                    html_table += f"<td style='{td_style}'>{value}</td>"

                html_table += "</tr>"

            html_table += "</tbody></table>"
            st.markdown(
                f"<div style='overflow-x:auto;'>{html_table}</div>",
                unsafe_allow_html=True,
            )
        else:
            st.warning("Mock consensus data not found.")
    except Exception as e:
        st.error(f"Error loading mock data: {e}")
