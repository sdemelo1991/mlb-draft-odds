# MLB Draft Odds Scraper — Local + Cloud Setup

This app is split into **two parts**: local scraping + cloud display.

## Architecture

```
Your Computer (Local)
├── fetch_competitor_odds_mlb.py  ← Runs Playwright scrapers
└── .comp_cache.json             ← Saves odds to this file
         ↓ (push to GitHub)
    Streamlit Cloud
    ├── kalshi_mlb_streamlit_app.py  ← Reads cache, no Playwright needed
    └── .comp_cache.json             ← Loads odds from here
```

## Setup

### 1. **Local Machine** (where you run the scraper)

Install with Playwright:
```bash
pip install -r requirements-local.txt
```

Run the scraper to update the cache:
```bash
python fetch_competitor_odds_mlb.py
```

This creates `.comp_cache.json` in the repo root.

**Commit and push** this file to GitHub:
```bash
git add .comp_cache.json
git commit -m "Update competitor odds cache"
git push
```

### 2. **Streamlit Cloud** (public web app)

- Uses lightweight `requirements.txt` (no Playwright)
- Reads odds from `.comp_cache.json` 
- No live scraping in the cloud — **only displays cached data**

## Workflow

1. **Locally**: Run `python fetch_competitor_odds_mlb.py` whenever you want fresh odds
2. **Locally**: Commit and push `.comp_cache.json` to GitHub
3. **Cloud**: Streamlit automatically redeploys and shows the latest odds

## Notes

- The `.gitignore` includes `.comp_cache.json` by default. **Remove that line** to track it:
  ```
  # Comment out or remove this line:
  # .comp_cache.json
  ```
- Cache is valid for 1 hour per session
- If you deploy without `.comp_cache.json`, the app will show: "No competitor odds available"
