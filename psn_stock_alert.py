"""
PSN Gift Card Stock Monitor (for Render / Railway / any server)

- Checks every product every CHECK_INTERVAL seconds.
- INSTANT Telegram alert when an item goes from not-in-stock to in-stock.
- Hourly Telegram status report listing every product: name, price, status, link.

Secrets are read from environment variables (do NOT hard-code them on GitHub):
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
Optional: CHECK_INTERVAL (default 90), REPORT_INTERVAL (default 3600), MAX_PRICE
"""

import json
import os
import random
import re
import time
import requests
from bs4 import BeautifulSoup

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "90"))
REPORT_INTERVAL = int(os.getenv("REPORT_INTERVAL", "3600"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "0")) or None  # 0 = no limit

# Paste exact product-page links here (add Amazon, Flipkart, JioStore, PlayStation Direct...)
PRODUCTS = [
    "https://www.gamestheshop.com/product/2153a461-7203-4802-aac4-283be4c775d3?variant=09df672e-b598-4b2d-9b3e-e1ca1dd9d205",
    "https://www.gamestheshop.com/product/d117bc23-1c4b-4330-b7f3-f90c71d9a5b3?variant=cb05d659-beac-47d7-85ff-8bfed95f47cf",
    # "https://mdcomputers.in/product/sony-playstation-gift-card-inr-1000",
]

# Sites that load content with JavaScript -> opened in a real headless browser
BROWSER_SITES = ["gamestheshop.com"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"}

OUT_WORDS = ["out of stock", "sold out", "currently unavailable", "notify me",
             "coming soon", "temporarily unavailable"]
IN_WORDS = ["add to cart", "buy now", "add to bag"]

_pw = None
_browser = None


def get_browser():
    global _pw, _browser
    if _browser is None:
        from playwright.sync_api import sync_playwright
        _pw = sync_playwright().start()
        _browser = _pw.chromium.launch(headless=True, args=["--no-sandbox"])
    return _browser


def reset_browser():
    global _pw, _browser
    try:
        if _browser:
            _browser.close()
        if _pw:
            _pw.stop()
    except Exception:
        pass
    _pw = _browser = None


def fetch_rendered(url):
    page = get_browser().new_page(user_agent=UA)
    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(2000)
        return page.content()
    finally:
        page.close()


def parse_price(text):
    m = re.search(r"[\d,]+(?:\.\d+)?", text or "")
    return float(m.group(0).replace(",", "")) if m else None


def jsonld_offers(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for it in list(items):
            if isinstance(it, dict) and "@graph" in it:
                items.extend(it["@graph"])
        for it in items:
            if isinstance(it, dict) and it.get("@type") in ("Product", ["Product"]):
                offers = it.get("offers") or {}
                if isinstance(offers, list) and offers:
                    offers = offers[0]
                return {
                    "name": it.get("name"),
                    "price": offers.get("price") or offers.get("lowPrice"),
                    "in_stock": "instock" in str(offers.get("availability", "")).lower(),
                    "has_avail": "availability" in offers,
                }
    return None


def check(url):
    if any(d in url for d in BROWSER_SITES):
        html = fetch_rendered(url)
    else:
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        html = r.text
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True).lower()

    name = (soup.find("title").get_text(strip=True) if soup.find("title") else url)[:100]
    price, in_stock = None, None

    ld = jsonld_offers(soup)
    if ld:
        name = ld["name"] or name
        price = parse_price(str(ld["price"])) if ld["price"] else None
        if ld["has_avail"]:
            in_stock = ld["in_stock"]

    if "amazon." in url:
        av = soup.select_one("#availability")
        if av:
            in_stock = "in stock" in av.get_text().lower()
        p = soup.select_one(".a-price .a-offscreen") or soup.select_one(".a-price-whole")
        if p:
            price = parse_price(p.get_text())
        if soup.select_one("#add-to-cart-button"):
            in_stock = True

    if in_stock is None:
        if any(w in text for w in OUT_WORDS):
            in_stock = False
        elif any(w in text for w in IN_WORDS):
            in_stock = True

    if price is None:
        m = re.search(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)", text)
        if m:
            price = parse_price(m.group(1))

    return {"name": name, "price": price, "in_stock": in_stock}


def telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg[:4000],
                  "disable_web_page_preview": True},
            timeout=15,
        )
    except Exception as e:
        print("Telegram error:", e)


def site_of(url):
    return re.sub(r"^https?://(www\.)?", "", url).split("/")[0]


def fmt_price(p):
    return f"₹{p:,.0f}" if p else "price not found"


def status_label(s):
    return "✅ IN STOCK" if s is True else ("❌ Out of stock" if s is False else "⚠️ Unknown")


def hourly_report(state):
    lines = [f"🕐 Hourly PSN status ({time.strftime('%d %b, %H:%M')} server time)\n"]
    for url in PRODUCTS:
        s = state.get(url)
        if not s:
            lines.append(f"• Not checked yet\n  {url}\n")
            continue
        err = f"\n  ⚠️ Last error: {s['error']}" if s.get("error") else ""
        lines.append(
            f"• {s['name']}\n"
            f"  {status_label(s['in_stock'])} | {fmt_price(s['price'])} | {site_of(url)}\n"
            f"  🔗 {url}{err}\n"
        )
    telegram("\n".join(lines))


def main():
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        raise SystemExit("Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID environment variables.")
    if not PRODUCTS:
        raise SystemExit("Add product links to PRODUCTS.")

    state = {}
    telegram(f"✅ PSN monitor started. Watching {len(PRODUCTS)} link(s). "
             f"Alerts are instant; status report every {REPORT_INTERVAL // 60} min.")
    last_report = 0

    while True:
        for url in PRODUCTS:
            prev = state.get(url, {}).get("in_stock")
            try:
                info = check(url)
                state[url] = {**info, "error": None}
                print(f"[{time.strftime('%H:%M:%S')}] {site_of(url)} | "
                      f"{status_label(info['in_stock'])} | {fmt_price(info['price'])}")

                too_pricey = MAX_PRICE and info["price"] and info["price"] > MAX_PRICE
                if info["in_stock"] and prev is not True and not too_pricey:
                    telegram(
                        f"🎮🚨 IN STOCK NOW!\n\n{info['name']}\n"
                        f"💰 Price: {fmt_price(info['price'])}\n"
                        f"🏪 Site: {site_of(url)}\n\n🔗 {url}"
                    )
            except Exception as e:
                print(f"Error on {url}: {e}")
                old = state.get(url, {"name": url, "price": None, "in_stock": None})
                state[url] = {**old, "error": str(e)[:120]}
                if any(d in url for d in BROWSER_SITES):
                    reset_browser()
            time.sleep(random.uniform(2, 5))

        if time.time() - last_report >= REPORT_INTERVAL:
            hourly_report(state)
            last_report = time.time()

        time.sleep(CHECK_INTERVAL + random.uniform(0, 15))


if __name__ == "__main__":
    main()
