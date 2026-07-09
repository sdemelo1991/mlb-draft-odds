"""
Fetch MLB draft odds from multiple sportsbooks via web scraping.
Output: JSON with {"picks": [...], "ou": [...], "h2h": [...], "manual": {...}}
"""

import json
import sys
import asyncio
import re
from typing import Optional
import logging
import requests
import os
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.DEBUG, format='[%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

try:
    from playwright.sync_api import sync_playwright
    from playwright.async_api import async_playwright
except ImportError:
    logger.error("playwright not installed. Run: pip install playwright && playwright install")
    sys.exit(1)

# ── Data structures ────────────────────────────────────────────────────────

def normalize_player(name: str) -> str:
    """Normalize player name for consistent matching. Converts 'LastName, FirstName' to 'FirstName LastName'."""
    if not name:
        return ""
    ascii_name = name.encode("ascii", "ignore").decode("ascii").strip()
    if ascii_name.lower() in ("field", "the field", "any other", "any other player"):
        return "Field"

    # Convert "LastName, FirstName" to "FirstName LastName"
    if ',' in ascii_name:
        parts = ascii_name.split(',', 1)
        last_name = parts[0].strip()
        first_name = parts[1].strip() if len(parts) > 1 else ""
        if first_name:
            ascii_name = f"{first_name} {last_name}"
        else:
            ascii_name = last_name

    return ascii_name

def american_to_implied(american: str) -> Optional[float]:
    """Convert American odds to implied probability (0-1). E.g., '-167' -> 0.625, '+250' -> 0.286."""
    try:
        val = int(american.replace("+", "").strip())
        if val > 0:
            # Positive odds: implied = 100 / (odds + 100)
            return 100 / (val + 100)
        else:
            # Negative odds: implied = abs(odds) / (abs(odds) + 100)
            return abs(val) / (abs(val) + 100)
    except Exception:
        return None

# ── FanDuel Scraper ──────────────────────────────────────────────────────

def scrape_fanduel_sync(p):
    """Scrape FanDuel MLB draft odds using sync Playwright (NHL proven pattern)."""
    picks = []

    FD_URL = "https://on.sportsbook.fanduel.ca/baseball/mlb-draft"
    FD_PICK_MAP = {
        "1st overall": "#1 Overall",
        "2nd overall": "#2 Overall",
        "3rd overall": "#3 Overall",
        "4th overall": "#4 Overall",
        "5th overall": "#5 Overall",
    }

    STEALTH_JS = (
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
        "Object.defineProperty(navigator,'languages',{get:()=>['en-CA','en']});"
        "window.chrome={runtime:{}};"
    )
    STEALTH_ARGS = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    STEALTH_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    STEALTH_HEADERS = {
        "Accept-Language": "en-CA,en;q=0.9",
        "sec-ch-ua": '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }

    try:
        captured = []

        browser = p.chromium.launch(headless=True, args=STEALTH_ARGS)
        ctx = browser.new_context(user_agent=STEALTH_UA, extra_http_headers=STEALTH_HEADERS, locale="en-CA")
        ctx.add_init_script(STEALTH_JS)
        ctx.add_cookies([{
            "name": "osano_consentmanager",
            "value": "accepted",
            "domain": ".fanduel.ca",
            "path": "/",
        }])
        page = ctx.new_page()

        def capture_response(resp):
            if resp.status == 200 and "api" in resp.url.lower():
                logger.debug(f"FanDuel API call: {resp.url[:120]}")

            if "sbapi" in resp.url and resp.status == 200:
                try:
                    body = resp.json()
                    if isinstance(body, dict) and "attachments" in body:
                        captured.append(body)
                        market_count = len(body.get('attachments', {}).get('markets', {}))
                        logger.debug(f"FanDuel: captured sbapi response ({market_count} markets)")
                except Exception as e:
                    logger.debug(f"FanDuel: Failed to parse sbapi response: {e}")

        page.on("response", capture_response)

        try:
            # Use domcontentloaded (not networkidle) to avoid geolocation blocking
            # expect_response blocks until sbapi lands
            with page.expect_response(
                lambda r: "sbapi" in r.url and r.status == 200, timeout=45000
            ):
                page.goto(FD_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2000)
        except Exception as e:
            logger.debug(f"FanDuel: sbapi capture timeout or error: {e}")

        try:
            page.close()
        except Exception:
            pass

        try:
            ctx.close()
        except Exception:
            pass

        try:
            browser.close()
        except Exception:
            pass

        # Parse captured responses
        logger.info(f"FanDuel: captured {len(captured)} API responses")
        for payload in captured:
            try:
                markets = payload.get("attachments", {}).get("markets", {})
                if not isinstance(markets, dict):
                    continue

                for mkt_id, mkt in markets.items():
                    mkt_name = mkt.get("marketName", "") or mkt.get("name", "")
                    market_label = None
                    for i in range(1, 6):
                        if f"#{i}" in mkt_name.lower() or f"{i} overall" in mkt_name.lower():
                            market_label = f"#{i} Overall"
                            break
                    if not market_label:
                        market_label = "#1 Overall"

                    runners = mkt.get("runners", [])
                    for runner in runners:
                        if runner.get("runnerStatus", "").upper() not in ("", "ACTIVE", "OPEN"):
                            continue
                        player = runner.get("runnerName", "")
                        if not player or "any other" in player.lower():
                            continue

                        try:
                            dec = float(runner["winRunnerOdds"]["trueOdds"]["decimalOdds"]["decimalOdds"])
                        except Exception:
                            continue

                        if dec <= 0 or dec > 1000:
                            continue

                        implied = round(1 / dec * 100, 1)
                        picks.append({
                            "player": normalize_player(player),
                            "market": market_label,
                            "book": "FanDuel",
                            "implied": implied,
                        })
                        logger.debug(f"FanDuel: {player} {market_label} {dec:.2f} → {implied:.1f}%")
            except Exception as e:
                logger.debug(f"FanDuel parse error: {e}")

        logger.info(f"FanDuel: extracted {len(picks)} picks")
        return picks, [], []

    except Exception as e:
        logger.warning(f"FanDuel scrape failed: {e}")
        import traceback
        logger.debug(f"FanDuel traceback: {traceback.format_exc()}")
        return [], [], []

# ── Betano Scraper (WORKING!) ──────────────────────────────────────────────

async def scrape_betano(page):
    """Scrape Betano MLB draft odds — handle cookie consent."""
    picks = []

    try:
        await page.goto("https://www.betano.ca/sport/baseball/north-america/mlb-draft/199501/",
                       timeout=30000, wait_until="networkidle")

        # Accept cookie consent if it exists
        try:
            # Try to find "YES, I ACCEPT" button or similar
            buttons = await page.query_selector_all("button")
            for btn in buttons:
                try:
                    text = await btn.inner_text()
                    if "YES" in text.upper() or "ACCEPT" in text.upper() or "AGREE" in text.upper():
                        await btn.click()
                        logger.debug(f"Betano: clicked cookie button: {text}")
                        await asyncio.sleep(2)
                        break
                except:
                    pass
        except Exception as e:
            logger.debug(f"Betano: error handling cookies: {e}")

        # Wait for network and content to fully load
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except:
            pass

        # Scroll multiple times to trigger lazy loading
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(2)

        page_text = await page.inner_text("body")
        logger.info(f"Betano: page loaded, {len(page_text)} chars")

        # Debug: show first 500 chars
        logger.debug(f"Betano page text preview:\n{page_text[:500]}")

        # Parse text for draft odds
        # Betano puts player names and odds on separate lines
        lines = page_text.split('\n')
        logger.debug(f"Betano: {len(lines)} lines total")

        i = 0
        while i < len(lines):
            line = lines[i].strip()

            # Look for American odds pattern on current line (±digits only)
            if re.match(r'^[+-]\d{1,5}$', line):
                logger.debug(f"Found odds pattern at line {i}: '{line}', prev line: '{lines[i-1] if i > 0 else 'N/A'}'")

            if re.match(r'^[+-]\d{1,5}$', line):
                odds_str = line
                # Player name should be on previous line
                if i > 0:
                    player_text = lines[i-1].strip()
                    logger.debug(f"  Trying player: '{player_text}' with odds '{odds_str}'")

                    if player_text and len(player_text) > 2:
                        if player_text.lower() not in ('pick', 'closes', '07/11', '01:30', 'pm'):
                            try:
                                implied_prob = american_to_implied(odds_str)
                                logger.debug(f"    american_to_implied('{odds_str}') = {implied_prob}")
                                if implied_prob:
                                    implied = implied_prob * 100
                                    logger.debug(f"    implied = {implied:.1f}%")
                                    if 0.1 < implied < 99.9:  # Sanity check — allow long odds
                                        logger.debug(f"    ✓ Adding {player_text}")
                                        picks.append({
                                            "player": normalize_player(player_text),
                                            "market": "#1 Overall",
                                            "book": "Betano",
                                            "implied": implied,
                                        })
                                    else:
                                        logger.debug(f"    ✗ Implied {implied}% outside range")
                            except Exception as e:
                                logger.debug(f"  Betano parse error: {e}")
                        else:
                            logger.debug(f"    ✗ Filtered out as stop word")
            i += 1

        logger.info(f"Betano: extracted {len(picks)} picks")

    except Exception as e:
        logger.warning(f"Betano scrape failed: {e}")

    return picks, [], []

# ── Placeholder Scrapers (simplified, will improve) ────────────────────────

async def scrape_draftkings(page):
    """DraftKings — fetch all draft pick markets and their selections."""
    picks = []
    try:
        # Load DraftKings page to establish session
        await page.goto("https://sportsbook.draftkings.com/leagues/baseball/mlb?category=futures&subcategory=mlb-draft&nav_1=%231-pick",
                       timeout=30000, wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # Fetch all markets to discover available picks
        # Using a broad query to get all MLB draft-related markets
        url = "https://sportsbook-nash.draftkings.com/sites/CA-ON-SB/api/sportscontent/controldata/league/leagueSubcategory/v1/markets"

        # Individual overall picks 1-5 are now all under subcategory 20066
        # ("Pick Number"), returned as 5 markets: "... Number N Pick".
        # (Previously split across 11601/15723-15726, which DK retired.)
        PICK_SUBCATEGORY = "20066"

        # Alternative markets (Top 5, Top 10, O/U, H2H)
        other_markets = {
            "20048": "Top 5 Pick",    # Will be drafted in top 5
            "20049": "Top 10 Pick",   # Will be drafted in top 10
            "11602": "Draft Position O/U",  # Over/under pick position
            "11605": "1st to Be Drafted",   # Head-to-head matchups
        }

        params = {
            "isBatchable": "false",
            "templateVars": f"84240,{PICK_SUBCATEGORY}",
            "eventsQuery": f"$filter=leagueId eq '84240' AND clientMetadata/Subcategories/any(s: s/Id eq '{PICK_SUBCATEGORY}')",
            "marketsQuery": f"$filter=clientMetadata/subCategoryId eq '{PICK_SUBCATEGORY}' AND tags/all(t: t ne 'SportcastBetBuilder')",
            "include": "Events",
            "entity": "events"
        }

        try:
            resp = await page.request.get(url, params=params)
            if resp.status == 200:
                data = await resp.json()
                markets = data.get('markets', [])
                selections = data.get('selections', [])

                # Map each market id → "#N Overall" by parsing "... Number N Pick"
                market_label = {}
                for mkt in markets:
                    mname = mkt.get('name', '')
                    mnum = re.search(r'Number\s+(\d+)\s+Pick', mname, re.IGNORECASE)
                    if mnum:
                        market_label[mkt.get('id')] = f"#{mnum.group(1)} Overall"

                logger.debug(f"DraftKings: subcat {PICK_SUBCATEGORY} → {len(market_label)} pick markets, {len(selections)} selections")

                for selection in selections:
                    try:
                        label = market_label.get(selection.get('marketId'))
                        if not label:
                            continue
                        player_name = selection.get('label', '').strip()
                        american_odds = selection.get('displayOdds', {}).get('american', '').strip()
                        if not player_name or 'any other' in player_name.lower():
                            continue
                        if player_name and american_odds and len(player_name) > 2:
                            american_odds = american_odds.replace('−', '-').replace('–', '-')
                            implied_prob = american_to_implied(american_odds)
                            if implied_prob:
                                implied = implied_prob * 100
                                picks.append({
                                    "player": normalize_player(player_name),
                                    "market": label,
                                    "book": "DraftKings",
                                    "implied": implied,
                                })
                                logger.debug(f"DraftKings: {label} {player_name} {american_odds} = {implied:.1f}%")
                    except Exception as e:
                        logger.debug(f"DraftKings selection parse error: {e}")
            else:
                logger.warning(f"DraftKings: API returned {resp.status} for pick markets")
        except Exception as e:
            logger.warning(f"DraftKings: API request failed for pick markets: {e}")

        logger.info(f"DraftKings: extracted {len(picks)} overall picks (subcat {PICK_SUBCATEGORY})")

    except Exception as e:
        logger.warning(f"DraftKings picks scrape failed: {e}")

    # Fetch alternative markets: Top 5, Top 10, O/U, H2H
    ou_data = []
    h2h_data = []

    alternative_markets = {
        "20048": "Top 5 Pick",
        "20049": "Top 10 Pick",
        "20050": "R1",              # To Be Drafted in the First Round
        "11602": "Draft Position O/U",
        "11605": "1st to Be Drafted H2H",
    }

    for subcategory_id, market_type in alternative_markets.items():
        params = {
            "isBatchable": "false",
            "templateVars": f"84240,{subcategory_id}",
            "eventsQuery": f"$filter=leagueId eq '84240' AND clientMetadata/Subcategories/any(s: s/Id eq '{subcategory_id}')",
            "marketsQuery": f"$filter=clientMetadata/subCategoryId eq '{subcategory_id}' AND tags/all(t: t ne 'SportcastBetBuilder')",
            "include": "Events",
            "entity": "events"
        }

        try:
            resp = await page.request.get(url, params=params)
            if resp.status == 200:
                data = await resp.json()
                markets = data.get('markets', [])
                selections = data.get('selections', [])

                if market_type.startswith("Top") or market_type == "R1":
                    # Top 5 / Top 10 / R1 — player → implied %, one flat selection list
                    for selection in selections:
                        try:
                            player_name = selection.get('label', '').strip()
                            american_odds = selection.get('displayOdds', {}).get('american', '').strip()

                            if player_name and american_odds and len(player_name) > 2:
                                american_odds = american_odds.replace('−', '-').replace('−', '-')
                                implied_prob = american_to_implied(american_odds)
                                if implied_prob:
                                    implied = implied_prob * 100
                                    picks.append({
                                        "player": normalize_player(player_name),
                                        "market": market_type,
                                        "book": "DraftKings",
                                        "implied": implied,
                                    })
                        except Exception as e:
                            logger.debug(f"DraftKings {market_type} parse error: {e}")

                    logger.info(f"DraftKings: {market_type} extracted {len([s for s in selections])} selections")

                elif market_type == "Draft Position O/U":
                    # O/U markets grouped by player — one market per player with Under/Over
                    for market in markets:
                        market_name = market.get('name', '')
                        market_id = market.get('id')

                        # Extract player name from market name (e.g., "Drew Burress Draft Position")
                        player_name = market_name.replace(' Draft Position', '').strip()

                        # Get selections for this market
                        market_selections = [s for s in selections if s.get('marketId') == market_id]
                        for sel in market_selections:
                            try:
                                line_label = sel.get('label', '').strip()  # "Under 7.5" or "Over 7.5"
                                american_odds = sel.get('displayOdds', {}).get('american', '').strip()

                                if line_label and american_odds:
                                    american_odds = american_odds.replace('−', '-')
                                    implied_prob = american_to_implied(american_odds)
                                    if implied_prob:
                                        implied = implied_prob * 100
                                        ou_data.append({
                                            "player": normalize_player(player_name),
                                            "line": line_label,
                                            "book": "DraftKings",
                                            "implied": implied,
                                        })
                            except Exception as e:
                                logger.debug(f"DraftKings O/U parse error: {e}")

                    logger.info(f"DraftKings: Draft Position O/U extracted {len(ou_data)} entries")

                elif market_type == "1st to Be Drafted H2H":
                    # H2H matchups — extract player pair from market name and their odds
                    for market in markets:
                        market_name = market.get('name', '')
                        market_id = market.get('id')

                        # Extract player names from market name (e.g., "Tyler Bell vs Ryder Helfrick - 1st to Be Drafted")
                        matchup = market_name.replace(' - 1st to Be Drafted', '').strip()
                        if ' vs ' in matchup:
                            player_a, player_b = matchup.split(' vs ', 1)
                            player_a = player_a.strip()
                            player_b = player_b.strip()

                            # Get selections for this matchup
                            market_selections = [s for s in selections if s.get('marketId') == market_id]
                            for sel in market_selections:
                                try:
                                    player_name = sel.get('label', '').strip()
                                    american_odds = sel.get('displayOdds', {}).get('american', '').strip()

                                    if player_name and american_odds:
                                        american_odds = american_odds.replace('−', '-')
                                        implied_prob = american_to_implied(american_odds)
                                        if implied_prob:
                                            implied = implied_prob * 100
                                            h2h_data.append({
                                                "player": normalize_player(player_name),
                                                "vs": normalize_player(player_b if player_name.strip() == player_a.strip() else player_a),
                                                "book": "DraftKings",
                                                "implied": implied,
                                            })
                                except Exception as e:
                                    logger.debug(f"DraftKings H2H parse error: {e}")

                    logger.info(f"DraftKings: H2H extracted {len(h2h_data)} entries")

        except Exception as e:
            logger.warning(f"DraftKings {market_type} fetch failed: {e}")

    return picks, ou_data, h2h_data

async def scrape_bet365(page):
    """Bet365 — login and extract odds from MLB draft picks."""
    picks = []
    username = os.getenv("BET365_USERNAME")
    password = os.getenv("BET365_PASSWORD")

    if not username or not password:
        logger.warning("Bet365: credentials not provided (BET365_USERNAME, BET365_PASSWORD)")
        return picks, [], []

    try:
        # Navigate to homepage
        await page.goto("https://www.bet365.com/", timeout=30000, wait_until="domcontentloaded")
        await asyncio.sleep(2)

        # Look for and click the login button
        login_btn = await page.query_selector("[data-testid='gl-login-button'], button:has-text('Log In')")
        if login_btn:
            await login_btn.click()
            logger.debug("Bet365: clicked login button")
            await asyncio.sleep(2)
        else:
            logger.debug("Bet365: login button not found (may already be logged in?)")

        # Try to find and fill username field
        username_input = await page.query_selector("input[type='text'], input[autocomplete='email'], input[name='username']")
        if username_input:
            await username_input.fill(username)
            logger.debug("Bet365: entered username")
            await asyncio.sleep(1)
        else:
            logger.warning("Bet365: username input not found")
            return picks, [], []

        # Try to find and fill password field
        password_input = await page.query_selector("input[type='password']")
        if password_input:
            await password_input.fill(password)
            logger.debug("Bet365: entered password")
            await asyncio.sleep(1)
        else:
            logger.warning("Bet365: password input not found")
            return picks, [], []

        # Click submit/login button
        submit_btn = await page.query_selector("button[type='submit'], button:has-text('Log In')")
        if submit_btn:
            await submit_btn.click()
            logger.debug("Bet365: clicked submit button")
            await page.wait_for_load_state("networkidle", timeout=15000)
            await asyncio.sleep(3)
        else:
            logger.warning("Bet365: submit button not found")

        # Navigate to MLB draft picks page
        await page.goto("https://www.bet365.com/#/AC/B16/C21153839/D1/E135563270/F2/",
                       timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle", timeout=15000)
        await asyncio.sleep(3)

        page_text = await page.inner_text("body")
        logger.info(f"Bet365: page loaded, {len(page_text)} chars")

        # Look for player name + odds patterns
        pattern = r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+([-+]\d{3,4})'
        matches = re.findall(pattern, page_text)

        for player_name, american in matches:
            implied_prob = american_to_implied(american)
            if implied_prob and 0.001 < implied_prob < 0.9999:
                implied = implied_prob * 100
                picks.append({
                    "player": normalize_player(player_name),
                    "market": "#1 Overall",
                    "book": "Bet365",
                    "implied": implied
                })
                logger.debug(f"Bet365: {player_name} {american} = {implied:.1f}%")

        if len(picks) == 0:
            logger.warning(f"Bet365: no odds extracted")

        logger.info(f"Bet365: extracted {len(picks)} picks")

    except Exception as e:
        logger.warning(f"Bet365 scrape failed: {e}")
        import traceback
        logger.debug(traceback.format_exc())

    return picks, [], []

def scrape_bet99_graphql():
    """Bet99 — fetch odds via GraphQL API (no browser needed)."""
    picks = []
    ou = []
    h2h = []

    try:
        logger.info("Bet99: querying GraphQL API...")

        # Bet99 GraphQL endpoint
        url = "https://bet99.com/java-graphql/graphql"

        # GraphQL query to fetch MLB Draft markets
        query = {
            "query": """
            query GetFixture($fixtureId: Int!) {
              fixture(id: $fixtureId) {
                id
                name
                markets {
                  id
                  name
                  selections {
                    id
                    name
                    odds
                  }
                }
              }
            }
            """,
            "variables": {
                "fixtureId": 72720  # MLB Draft fixture ID
            }
        }

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/json",
        }

        import requests
        response = requests.post(url, json=query, headers=headers, timeout=15)

        if response.status_code != 200:
            logger.warning(f"Bet99: API returned {response.status_code}")
            return picks, ou, h2h

        payload = response.json()
        if "errors" in payload:
            logger.warning(f"Bet99: GraphQL errors: {payload['errors']}")
            return picks, ou, h2h

        fixture = payload.get("data", {}).get("fixture", {})
        markets = fixture.get("markets", [])
        logger.info(f"Bet99: found {len(markets)} markets")

        player_occurrence_count = {}

        for market in markets:
            market_name = market.get("name", "").lower()
            if not any(x in market_name for x in ["#1", "#2", "#3", "#4", "#5", "overall"]):
                continue

            # Extract pick number from market name
            pick_match = re.search(r'#(\d+)', market_name)
            if not pick_match:
                continue
            pick_num = pick_match.group(1)
            market_label = f"#{pick_num} Overall"

            selections = market.get("selections", [])
            logger.debug(f"Bet99: {market_label} with {len(selections)} selections")

            for selection in selections:
                try:
                    player_name = selection.get("name", "").strip()
                    odds_decimal = selection.get("odds")

                    if not player_name or not odds_decimal:
                        continue

                    normalized_name = normalize_player(player_name)
                    player_occurrence_count[normalized_name] = player_occurrence_count.get(normalized_name, 0) + 1
                    occurrence = player_occurrence_count[normalized_name]

                    if occurrence <= 5 and odds_decimal > 0:
                        implied_prob = 1.0 / odds_decimal
                        if 0.001 < implied_prob < 0.9999:
                            implied = implied_prob * 100
                            picks.append({
                                "player": normalized_name,
                                "market": market_label,
                                "book": "Bet99",
                                "implied": implied,
                            })
                            logger.debug(f"Bet99: {player_name} {market_label} {odds_decimal} → {implied:.1f}%")
                except Exception as e:
                    logger.debug(f"Bet99 parse error: {e}")

        logger.info(f"Bet99: extracted {len(picks)} picks")

    except Exception as e:
        logger.warning(f"Bet99 scrape failed: {e}")

    return picks, ou, h2h

BETMGM_FIXTURE_ID = "19771912"
BETMGM_EVENT_URL = "https://www.on.betmgm.ca/en/sports/events/2026-mlb-draft-19771912"
BETMGM_ORDINAL = {"1st": "#1 Overall", "2nd": "#2 Overall", "3rd": "#3 Overall",
                  "4th": "#4 Overall", "5th": "#5 Overall"}

def _bmgm_val(obj):
    """BetMGM names are {'value': '...'} dicts. Return the string."""
    if isinstance(obj, dict):
        return obj.get("value", "")
    return str(obj) if obj else ""

async def scrape_betmgm(page):
    """BetMGM — capture x-bwin-accessid from network, then call the fixture-offers
    CDS API directly with offerMapping=All to get every market (picks, Top 5/10,
    O/U draft position, and H2H). Returns (picks, ou, h2h)."""
    picks, ou, h2h = [], [], []
    access_id = None

    def on_response(response):
        nonlocal access_id
        url = response.url
        if access_id is None and "x-bwin-accessid=" in url and "cds-api" in url:
            start = url.find("x-bwin-accessid=") + len("x-bwin-accessid=")
            end = url.find("&", start)
            access_id = url[start:end] if end != -1 else url[start:]

    try:
        logger.debug("BetMGM: navigating to capture access id...")
        page.on("response", on_response)

        # BetMGM navigation is flaky (slow/heavy page). Retry the goto up to 3x.
        for attempt in range(1, 4):
            try:
                await page.goto(BETMGM_EVENT_URL, timeout=45000, wait_until="domcontentloaded")
                break
            except Exception as nav_err:
                logger.debug(f"BetMGM: goto attempt {attempt} failed: {nav_err}")
                if attempt == 3:
                    logger.warning("BetMGM: navigation timed out after 3 attempts")
                    return picks, ou, h2h
                await asyncio.sleep(3)

        # Poll for the access id to appear in network traffic
        for _ in range(30):
            if access_id:
                break
            await asyncio.sleep(0.5)

        if not access_id:
            logger.warning("BetMGM: could not capture x-bwin-accessid")
            return picks, ou, h2h

        logger.debug(f"BetMGM: captured access id {access_id[:16]}..., calling fixture-offers")
        resp = await page.request.get(
            "https://www.on.betmgm.ca/cds-api/bettingoffer/fixture-offers",
            params={
                "x-bwin-accessid": access_id,
                "lang": "en-us",
                "country": "CA",
                "userCountry": "CA",
                "subdivision": "CA-Ontario",
                "fixtureIds": BETMGM_FIXTURE_ID,
                "offerMapping": "All",
            },
        )
        if resp.status != 200:
            logger.warning(f"BetMGM: fixture-offers returned {resp.status}")
            return picks, ou, h2h

        body = await resp.json()
        offers = body.get("fixtureOffers", [])
        if not offers:
            logger.warning("BetMGM: no fixtureOffers in response")
            return picks, ou, h2h
        games = offers[0].get("games", [])
        logger.debug(f"BetMGM: {len(games)} games (markets) returned")

        for game in games:
            try:
                name = _bmgm_val(game.get("name"))
                low = name.lower()
                results = game.get("results", [])

                # ── Overall picks 1-5 ──
                m = re.match(r'^(1st|2nd|3rd|4th|5th) overall pick$', low)
                if m:
                    label = BETMGM_ORDINAL[m.group(1)]
                    for r in results:
                        player = _bmgm_val(r.get("name"))
                        odds = r.get("odds", 0) or 0
                        if player and odds > 0:
                            picks.append({"player": normalize_player(player), "market": label,
                                          "book": "BetMGM", "implied": 100.0 / odds})
                    continue

                # ── Top 5 / Top 10 ──
                if "top 5 draft pick" in low or "top 10 draft pick" in low:
                    label = "Top 5 Pick" if "top 5" in low else "Top 10 Pick"
                    for r in results:
                        player = _bmgm_val(r.get("name"))
                        odds = r.get("odds", 0) or 0
                        if player and odds > 0:
                            picks.append({"player": normalize_player(player), "market": label,
                                          "book": "BetMGM", "implied": 100.0 / odds})
                    continue

                # ── Drafted in Round 1 (R1) ──
                if "drafted in the 1st round" in low:
                    for r in results:
                        player = _bmgm_val(r.get("name"))
                        odds = r.get("odds", 0) or 0
                        if player and odds > 0:
                            picks.append({"player": normalize_player(player), "market": "R1",
                                          "book": "BetMGM", "implied": 100.0 / odds})
                    continue

                # ── O/U draft position ──
                if low.endswith("draft position"):
                    player = name[:-len(" Draft Position")].strip()
                    for r in results:
                        line = _bmgm_val(r.get("name"))  # "Over 21.5" / "Under 21.5"
                        odds = r.get("odds", 0) or 0
                        if player and odds > 0 and (line.startswith("Over") or line.startswith("Under")):
                            ou.append({"player": normalize_player(player), "line": line,
                                       "book": "BetMGM", "implied": 100.0 / odds})
                    continue

                # ── H2H (who gets drafted first) ──
                if low == "player to be drafted first" and len(results) == 2:
                    a, b = results[0], results[1]
                    an, bn = _bmgm_val(a.get("name")), _bmgm_val(b.get("name"))
                    ao, bo = a.get("odds", 0) or 0, b.get("odds", 0) or 0
                    if an and bn and ao > 0 and bo > 0:
                        h2h.append({"player": normalize_player(an), "vs": normalize_player(bn),
                                    "book": "BetMGM", "implied": 100.0 / ao})
                        h2h.append({"player": normalize_player(bn), "vs": normalize_player(an),
                                    "book": "BetMGM", "implied": 100.0 / bo})
                    continue
            except Exception as e:
                logger.debug(f"BetMGM game parse error: {e}")

        logger.info(f"BetMGM: extracted {len(picks)} picks, {len(ou)} O/U, {len(h2h)} H2H")

    except Exception as e:
        logger.warning(f"BetMGM scrape failed: {e}")
        import traceback
        logger.debug(traceback.format_exc())

    return picks, ou, h2h

# ── Placeholder books (app-only / not yet available) ────────────────────────

def add_placeholder_books():
    """Add placeholder entries for books not yet offering or app-only."""
    return {
        "Bet365": "Manual entry available",
        "Bookmaker": "App-only — manual entry required",
        "Caesars": "App-only — manual entry required",
        "Kambi": "Not yet offering — will scrape when available",
    }

# ── Main orchestrator ──────────────────────────────────────────────────────

async def fetch_all_books():
    """Fetch picks, O/Us, and H2Hs from all available sportsbooks."""
    all_picks = []
    all_ou = []
    all_h2h = []
    manual = add_placeholder_books()

    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=2)

    logger.info("Scraping FanDuel...")
    try:
        def run_fanduel():
            with sync_playwright() as p:
                return scrape_fanduel_sync(p)

        fd_picks, fd_ou, fd_h2h = await asyncio.wait_for(
            loop.run_in_executor(executor, run_fanduel),
            timeout=60
        )
        all_picks.extend(fd_picks)
        all_ou.extend(fd_ou)
        all_h2h.extend(fd_h2h)
        logger.info(f"  ✓ FanDuel: {len(fd_picks)} picks, {len(fd_ou)} O/U, {len(fd_h2h)} H2H")
    except Exception as e:
        logger.warning(f"FanDuel scraper error: {e}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,  # Run headless to prevent browser windows from opening
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            ignore_https_errors=True
        )
        page = await context.new_page()
        page.set_default_timeout(30000)

        scrapers = [
            ("DraftKings", scrape_draftkings),
            ("BetMGM", scrape_betmgm),
            # ("Betano", scrape_betano),  # No MLB draft markets available
            # Manual entry only:
            # Bet365, Bookmaker, Caesars, Kambi
        ]

        for book_name, scraper_func in scrapers:
            logger.info(f"Scraping {book_name}...")
            try:
                # BetMGM needs a fresh page due to headless=False browser setup
                if book_name == "BetMGM":
                    fresh_page = await context.new_page()
                    fresh_page.set_default_timeout(30000)
                    picks, ou, h2h = await scraper_func(fresh_page)
                    await fresh_page.close()
                else:
                    picks, ou, h2h = await scraper_func(page)
                all_picks.extend(picks)
                all_ou.extend(ou)
                all_h2h.extend(h2h)
                logger.info(f"  ✓ {book_name}: {len(picks)} picks, {len(ou)} O/U, {len(h2h)} H2H")
            except Exception as e:
                logger.warning(f"  ✗ {book_name} failed: {e}")

            await asyncio.sleep(1)

        await browser.close()

    return {
        "picks": all_picks,
        "ou": all_ou,
        "h2h": all_h2h,
        "manual": manual,
    }

# ── CLI ────────────────────────────────────────────────────────────────────

async def main():
    try:
        data = await fetch_all_books()
        print(json.dumps(data))
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        data = {"picks": [], "ou": [], "h2h": [], "manual": add_placeholder_books()}
        print(json.dumps(data))
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
