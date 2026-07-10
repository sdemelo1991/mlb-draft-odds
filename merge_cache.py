"""
Merge a freshly scraped comp file into the existing cache.

Guards against the markets-down / throttled failure mode (rejects the scrape
if no primary book is present), and carries forward recent data for any
primary book that is missing from the new scrape — so a single transient
scrape failure (e.g. a FanDuel timeout) doesn't blank that book's columns.

Carried-forward data is dropped once it exceeds CARRY_FORWARD_MAX_AGE, so a
book that stays genuinely down won't show stale odds indefinitely.

Also injects manually-entered odds from manual_odds.json (in the cache's
directory) on every run — for app-only books like Bookmaker that can't be
scraped. Manual entries store American odds and are converted to implied here,
so they persist across scraper runs and can be updated by editing that file.

Usage: python merge_cache.py <new_scrape.json> <current_cache.json> <out.json>
  exit 0 + "OK ..."     -> merged result written to <out.json>
  exit 1 + "REJECT ..." -> new scrape failed the guard; nothing written
"""
import json, sys, time, os

NEW, CUR, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
PRIMARY = ("FanDuel", "DraftKings", "BetMGM")
CARRY_FORWARD_MAX_AGE = 3600  # seconds (60 min) — covers transient failures, not overnight downtime

def load(path):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}

def american_to_implied(a):
    """American odds -> implied probability as a percentage (0-100)."""
    a = float(a)
    prob = 100.0 / (a + 100.0) if a > 0 else (-a) / ((-a) + 100.0)
    return round(prob * 100, 1)

new = load(NEW)
new_picks = new.get("picks", [])
new_books = set(p.get("book") for p in new_picks)

# Guard: reject markets-down / throttled scrapes (FanDuel-only or empty).
if not (len(new_picks) >= 30 and ("DraftKings" in new_books or "BetMGM" in new_books)):
    print(f"REJECT picks={len(new_picks)} books={sorted(new_books)}")
    sys.exit(1)

cur = load(CUR)
now = time.time()
book_ts = dict(cur.get("_book_ts", {}))

# Mark every book present in this fresh scrape as seen now.
for b in new_books:
    book_ts[b] = now

# Carry forward any primary book missing from the new scrape but seen recently.
carried = []
for b in PRIMARY:
    if b in new_books:
        continue
    age = now - book_ts.get(b, 0)
    if age <= CARRY_FORWARD_MAX_AGE:
        for key in ("picks", "ou", "h2h"):
            new.setdefault(key, [])
            new[key].extend([r for r in cur.get(key, []) if r.get("book") == b])
        carried.append(f"{b}(+{int(age/60)}m)")

new["_book_ts"] = book_ts
if "manual" not in new and "manual" in cur:
    new["manual"] = cur["manual"]

# Inject manually-entered odds (e.g. Bookmaker) from manual_odds.json, kept
# alongside the cache. Convert American -> implied and replace any existing
# entries for those books so they refresh cleanly each run.
manual_path = os.path.join(os.path.dirname(os.path.abspath(CUR)), "manual_odds.json")
manual = load(manual_path)
injected = {}
manual_books = {e.get("book") for k in ("picks", "ou", "h2h") for e in manual.get(k, [])}
if manual_books:
    for key in ("picks", "ou", "h2h"):
        new.setdefault(key, [])
        new[key] = [r for r in new[key] if r.get("book") not in manual_books]
        for e in manual.get(key, []):
            e = dict(e)
            if "american" in e:
                e["implied"] = american_to_implied(e.pop("american"))
            new[key].append(e)
            injected[e.get("book")] = injected.get(e.get("book"), 0) + 1

json.dump(new, open(OUT, "w", encoding="utf-8"))
merged_books = sorted(set(p.get("book") for p in new.get("picks", [])))
msg = f"OK picks={len(new.get('picks', []))} books={merged_books}"
if carried:
    msg += f" carried_forward={carried}"
if injected:
    msg += f" manual={injected}"
print(msg)
sys.exit(0)
