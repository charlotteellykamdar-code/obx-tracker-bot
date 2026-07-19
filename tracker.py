"""
OBX Season 5 Ticket Tracker
Polls GoFobo and Tudum, then hits your website API to:
  - Flip the site to LIVE
  - Blast email + text alerts to all subscribers

Requirements:
    pip install requests beautifulsoup4 playwright
    playwright install chromium
"""

import json
import os
import time
import hashlib
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from playwright.sync_api import sync_playwright

# ── CONFIG ────────────────────────────────────────────────────────────────────
SITE_URL = "https://obx-tracker.vercel.app"
ALERT_SECRET = "OBX2026"                # ← same as ALERT_SECRET env var
POLL_INTERVAL_SECONDS = 300                      # check every 5 min
SEEN_FILE = "seen_listings.json"

SEARCH_TERMS = ["outer banks", "OBX", "outer banks season 5"]

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# ── HELPERS ───────────────────────────────────────────────────────────────────

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


def make_id(title: str, url: str) -> str:
    return hashlib.md5(f"{title}|{url}".encode()).hexdigest()


def fire_alert(title: str, url: str, source: str):
    """Tell the website tickets are live — it handles emails + texts."""
    try:
        r = requests.post(
            f"{SITE_URL}/api/alert",
            json={"title": title, "url": url, "source": source},
            headers={"x-alert-secret": ALERT_SECRET},
            timeout=15,
        )
        if r.ok:
            data = r.json()
            print(f"  ✅ Alert fired! {data.get('emailSent', 0)} emails, {data.get('textsSent', 0)} texts sent.")
        else:
            print(f"  ⚠️  Alert API error: {r.status_code} {r.text}")
    except Exception as e:
        print(f"  ⚠️  Could not reach alert API: {e}")


def ping_last_checked():
    """Update the 'last checked' timestamp on the site."""
    try:
        requests.post(
            f"{SITE_URL}/api/ping",
            headers={"x-alert-secret": ALERT_SECRET},
            timeout=5,
        )
    except Exception:
        pass  # Non-critical


# ── SCRAPERS ──────────────────────────────────────────────────────────────────

def check_gofobo() -> list[dict]:
    results = []
    for term in SEARCH_TERMS:
        url = f"https://www.gofobo.com/search?q={requests.utils.quote(term)}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            cards = soup.select(".screening-card, .event-card, article.card, .search-result")
            for card in cards:
                title_el = card.select_one("h2, h3, .title, .event-title")
                link_el = card.select_one("a[href]")
                desc_el = card.select_one("p, .description")
                if not title_el or not link_el:
                    continue
                title = title_el.get_text(strip=True)
                href = link_el["href"]
                full_url = href if href.startswith("http") else f"https://www.gofobo.com{href}"
                desc = desc_el.get_text(strip=True) if desc_el else ""
                if any(t.lower() in title.lower() or t.lower() in desc.lower() for t in SEARCH_TERMS):
                    results.append({"title": title, "url": full_url, "source": "GoFobo"})
        except Exception as e:
            print(f"  [GoFobo] Error for '{term}': {e}")
    return results


def check_tudum() -> list[dict]:
    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            for term in ["outer banks", "OBX"]:
                search_url = f"https://www.netflix.com/tudum/events?q={requests.utils.quote(term)}"
                page.goto(search_url, timeout=30000)
                page.wait_for_load_state("networkidle", timeout=20000)

                links = page.query_selector_all("a[href]")
                for link in links:
                    href = link.get_attribute("href") or ""
                    text = link.inner_text().strip()
                    if any(t.lower() in text.lower() for t in SEARCH_TERMS):
                        full_url = href if href.startswith("http") else f"https://www.netflix.com{href}"
                        results.append({
                            "title": text[:120],
                            "url": full_url,
                            "source": "Tudum",
                        })
            browser.close()
    except Exception as e:
        print(f"  [Tudum] Error: {e}")
    return results


def check_direct_gofobo_urls() -> list[dict]:
    """Check common short GoFobo URLs Netflix might use for OBX."""
    results = []
    candidates = [
        "https://www.gofobo.com/OBX",
        "https://www.gofobo.com/OBX5",
        "https://www.gofobo.com/OuterBanks",
        "https://www.gofobo.com/OuterBanks5",
        "https://www.gofobo.com/outerbanks",
        "https://www.gofobo.com/outerbanks5",
    ]
    for url in candidates:
        try:
            r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
            if r.status_code == 200 and any(t.lower() in r.text.lower() for t in SEARCH_TERMS):
                soup = BeautifulSoup(r.text, "html.parser")
                title_el = soup.select_one("h1, h2, title")
                title = title_el.get_text(strip=True) if title_el else "Outer Banks Screening"
                results.append({"title": title, "url": url, "source": "GoFobo (direct)"})
        except Exception as e:
            print(f"  [Direct] Error {url}: {e}")
    return results


# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

def run():
    print(f"🏄 OBX S5 Tracker started — polling every {POLL_INTERVAL_SECONDS // 60} min")
    print(f"   Site: {SITE_URL}\n")
    seen = load_seen()

    while True:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking...")

        all_results = []
        all_results += check_gofobo()
        all_results += check_tudum()
        all_results += check_direct_gofobo_urls()

        # Deduplicate within this batch
        seen_this_run = set()
        new_count = 0

        for item in all_results:
            listing_id = make_id(item["title"], item["url"])
            if listing_id not in seen and listing_id not in seen_this_run:
                print(f"  🆕 NEW: [{item['source']}] {item['title']}")
                fire_alert(item["title"], item["url"], item["source"])
                seen.add(listing_id)
                seen_this_run.add(listing_id)
                new_count += 1

        save_seen(seen)
        ping_last_checked()

        if new_count == 0:
            print("  No new listings.\n")
        else:
            print(f"  {new_count} new listing(s) found and alerted!\n")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
