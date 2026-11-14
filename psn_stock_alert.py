"""
PSN Gift Card FAST Monitor (Railway / any always-on server)

- Every site is checked in its own loop, in parallel (default every ~5 s for normal
  sites, ~15 s for JavaScript sites that need a real browser).
- The moment something comes IN STOCK you get a loud, highlighted alert, repeated
  every REPEAT_ALERT seconds until it sells out.
- A status report (grouped by PRICE) goes out every REPORT_INTERVAL seconds (default 60).

Railway variables
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID   required (chat id can be comma-separated)
  PRODUCT_URLS      optional: extra product links separated by spaces/commas/new lines
  MAX_PRICE         optional: ignore listings above this price (scalper filter)
  FAST_INTERVAL     default 5     seconds between checks, normal sites
  BROWSER_INTERVAL  default 15    seconds between rounds, browser sites
  REPORT_INTERVAL   default 60    seconds between status reports
  REPEAT_ALERT      default 30    seconds between repeat "IN STOCK" alerts
"""

import html as H
import json
import os
import random
import re
import threading
import time

import requests
from bs4 import BeautifulSoup

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
FAST_INTERVAL = float(os.getenv("FAST_INTERVAL", "5"))
BROWSER_INTERVAL = float(os.getenv("BROWSER_INTERVAL", "15"))
REPORT_INTERVAL = int(os.getenv("REPORT_INTERVAL", "60"))
REPEAT_ALERT = int(os.getenv("REPEAT_ALERT", "30"))
RENDER_WAIT_MS = int(os.getenv("RENDER_WAIT_MS", "2500"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "0")) or None
PRIORITY_INTERVAL = float(os.getenv("PRIORITY_INTERVAL", "3"))  # seconds, priority links

# ---- Add product-page links here (or in the PRODUCT_URLS variable on Railway) ----
PRODUCTS = [
    "https://www.gamestheshop.com/product/2153a461-7203-4802-aac4-283be4c775d3?variant=09df672e-b598-4b2d-9b3e-e1ca1dd9d205",
    "https://www.gamestheshop.com/product/d117bc23-1c4b-4330-b7f3-f90c71d9a5b3?variant=cb05d659-beac-47d7-85ff-8bfed95f47cf",
    # "https://mdcomputers.in/product/sony-playstation-gift-card-inr-1000",
    # "https://www.amazon.in/dp/XXXXXXXXXX",
    # "https://www.flipkart.com/....",
]
PRODUCTS += [u for u in re.split(r"[\s,]+", os.getenv("PRODUCT_URLS", "")) if u.startswith("http")]
# Links you care about most: checked faster and shown first in every report.
PRIORITY_URLS = [u for u in re.split(r"[\s,]+", os.getenv("PRIORITY_URLS", "")) if u.startswith("http")]
PRODUCTS += PRIORITY_URLS
PRODUCTS = list(dict.fromkeys(PRODUCTS))  # remove duplicates, keep order

# Sites that load content with JavaScript -> opened in a real headless browser
BROWSER_SITES = ["gamestheshop.com"]
BROWSER_SITES += [d for d in re.split(r"[\s,]+", os.getenv("BROWSER_SITES", "")) if d]  # e.g. croma.com,vijaysales.com

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"}
OUT_WORDS = ["out of stock", "sold out", "currently unavailable", "notify me",
             "coming soon", "temporarily unavailable"]
IN_WORDS = ["add to cart", "buy now", "add to bag"]

state = {}
lock = threading.Lock()


# ---------------- parsing ----------------
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


def check_html(html, url):
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

    # What exactly does the page say? (used to spot "coming soon" / "notify me" changes)
    label = None
    if in_stock is not True:
        if "coming soon" in text:
            label = "COMING SOON"
        elif "sold out" in text:
            label = "SOLD OUT"
        elif any(w in text for w in ("out of stock", "currently unavailable",
                                     "temporarily unavailable")):
            label = "OUT OF STOCK"
        elif "notify me" in text:
            label = "NOTIFY ME"
    return {"name": name, "price": price, "in_stock": in_stock, "label": label}


# ---------------- telegram ----------------
def telegram(msg, html=True):
    for chat_id in [c.strip() for c in TELEGRAM_CHAT_ID.split(",") if c.strip()]:
        try:
            data = {"chat_id": chat_id, "text": msg[:4000], "disable_web_page_preview": True}
            if html:
                data["parse_mode"] = "HTML"
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          data=data, timeout=15)
        except Exception as e:
            print("Telegram error:", e)
        time.sleep(0.3)  # stay under Telegram rate limits


def site_of(url):
    return re.sub(r"^https?://(www\.)?", "", url).split("/")[0]


def fmt_price(p):
    return f"₹{p:,.0f}" if p else "price unknown"


def alert_text(url, s, repeat=False):
    tag = "STILL IN STOCK" if repeat else "IN STOCK — BUY NOW!"
    return (f"🚨🚨🚨 <b>{tag}</b> 🚨🚨🚨\n\n"
            f"🎮 <b>{H.escape(s['name'])}</b>\n"
            f"💰 <b>Price: {fmt_price(s['price'])}</b>\n"
            f"🏪 {H.escape(site_of(url))}\n\n"
            f"👉 <a href=\"{H.escape(url)}\"><b>OPEN &amp; BUY NOW</b></a> 👈\n"
            f"🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥")


# ---------------- state / alerts ----------------
def too_pricey(info):
    return bool(MAX_PRICE and info["price"] and info["price"] > MAX_PRICE)


OUT_LABELS = ("COMING SOON", "NOTIFY ME", "SOLD OUT", "OUT OF STOCK")
LABEL_TEXT = {"COMING SOON": "🕒 Coming soon", "NOTIFY ME": "❌ Out (Notify me)",
              "SOLD OUT": "❌ Sold out", "OUT OF STOCK": "❌ Out"}


def change_text(url, s, old_label, new_label):
    link = f"<a href=\"{H.escape(url)}\">OPEN PAGE</a>"
    if new_label == "UNKNOWN" and old_label in OUT_LABELS:
        return (f"🟡🟡 <b>POSSIBLE RESTOCK — CHECK NOW!</b> 🟡🟡\n\n"
                f"<b>{H.escape(s['name'])}</b>\n"
                f"The \"{old_label.title()}\" sign disappeared but I can't confirm stock.\n"
                f"💰 {fmt_price(s['price'])} | 🏪 {H.escape(site_of(url))}\n👉 {link}")
    if old_label == "COMING SOON":
        return (f"📢 <b>Status changed: COMING SOON → {new_label.title()}</b>\n\n"
                f"<b>{H.escape(s['name'])}</b>\n"
                f"💰 {fmt_price(s['price'])} | 🏪 {H.escape(site_of(url))}\n👉 {link}")
    return None


def record(url, info):
    send_alert = send_gone = False
    change_msg = None
    with lock:
        old = state.get(url, {})
        prev = old.get("in_stock")
        s = {**info, "error": None, "checked": time.time(),
             "last_alert": old.get("last_alert", 0)}

        # Track the page's status. A change must be seen twice in a row before we
        # report it as "changed / possible restock", to avoid false alarms from half-loaded pages.
        cur = "IN STOCK" if info["in_stock"] is True else (info.get("label") or "UNKNOWN")
        confirmed = old.get("confirmed_label")
        if confirmed is None or cur == confirmed:
            s["confirmed_label"], s["pending_label"], s["pending_count"] = (
                confirmed or cur, None, 0)
        else:
            cnt = old.get("pending_count", 0) + 1 if old.get("pending_label") == cur else 1
            if cnt >= 2:
                if cur != "IN STOCK":
                    change_msg = (confirmed, cur)
                s["confirmed_label"], s["pending_label"], s["pending_count"] = cur, None, 0
            else:
                s["confirmed_label"], s["pending_label"], s["pending_count"] = (
                    confirmed, cur, cnt)

        if info["in_stock"] and prev is not True and not too_pricey(info):
            send_alert = True
            s["last_alert"] = time.time()
        if prev is True and info["in_stock"] is False:
            send_gone = True
        state[url] = s
        snap = dict(s)
    print(f"[{time.strftime('%H:%M:%S')}] {site_of(url)} | {cur} | {fmt_price(info['price'])}")
    if send_alert:
        telegram(alert_text(url, snap))
    if send_gone:
        telegram(f"❌ Sold out again: <b>{H.escape(snap['name'])}</b> ({H.escape(site_of(url))})")
    if change_msg:
        msg = change_text(url, snap, *change_msg)
        if msg:
            telegram(msg)


def record_error(url, e):
    print(f"Error on {url}: {e}")
    with lock:
        old = state.get(url, {"name": site_of(url), "price": None,
                              "in_stock": None, "last_alert": 0})
        state[url] = {**old, "error": str(e)[:80], "checked": time.time()}


def repeater():
    """Re-sends the highlighted alert while an item stays in stock."""
    while True:
        time.sleep(3)
        due = []
        with lock:
            for url, s in state.items():
                if (s.get("in_stock") is True and not too_pricey(s)
                        and time.time() - s.get("last_alert", 0) >= REPEAT_ALERT):
                    s["last_alert"] = time.time()
                    due.append((url, dict(s)))
        for url, s in due:
            telegram(alert_text(url, s, repeat=True))


def status_icon(s):
    return ("✅ IN STOCK" if s["in_stock"] is True else
            LABEL_TEXT.get(s.get("label")) or
            ("❌ Out" if s["in_stock"] is False else "⚠️ Unknown"))


def report_line(url, s):
    site = H.escape(site_of(url))
    link = f"<a href=\"{H.escape(url)}\">{site}</a>"
    if not s:
        return f"  ⏳ {link} — not checked yet"
    err = " (error)" if s.get("error") else ""
    return f"  {status_icon(s)} — {link}{err}"


def report():
    with lock:
        snap = {u: dict(s) for u, s in state.items()}
    in_count = sum(1 for s in snap.values() if s.get("in_stock") is True)
    lines = [f"📊 <b>PSN status</b> {time.strftime('%H:%M:%S')}  |  "
             f"In stock: <b>{in_count}/{len(PRODUCTS)}</b>"]

    if PRIORITY_URLS:
        lines.append("\n🎯 <b>PRIORITY WATCH</b>")
        for url in PRIORITY_URLS:
            s = snap.get(url)
            price = fmt_price(s["price"]) if s else ""
            lines.append(f"  💰 {price}" if s else "")
            lines.append(report_line(url, s))

    groups = {}
    for url in PRODUCTS:
        if url in PRIORITY_URLS:
            continue
        s = snap.get(url)
        key = round(s["price"]) if s and s.get("price") else None
        groups.setdefault(key, []).append((url, s))
    for key in sorted(groups, key=lambda k: (k is None, k or 0)):
        lines.append(f"\n💰 <b>{'₹{:,}'.format(key) if key else 'Price unknown'}</b>")
        for url, s in groups[key]:
            lines.append(report_line(url, s))
    telegram("\n".join(l for l in lines if l != ""))


def reporter():
    time.sleep(25)
    while True:
        try:
            report()
        except Exception as e:
            print("report error:", e)
        time.sleep(REPORT_INTERVAL)


# ---------------- workers ----------------
def plain_worker(url):
    sess = requests.Session()
    sess.headers.update(HEADERS)
    base = PRIORITY_INTERVAL if url in PRIORITY_URLS else FAST_INTERVAL
    errors = 0
    while True:
        try:
            r = sess.get(url, timeout=20)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            record(url, check_html(r.text, url))
            errors = 0
        except Exception as e:
            record_error(url, e)
            errors += 1  # blocked? slow down automatically so we don't get banned
        wait = base * random.uniform(0.8, 1.3) * min(2 ** errors, 20) if errors else \
            base * random.uniform(0.8, 1.3)
        time.sleep(min(wait, 120))


def browser_worker(urls):
    pw = browser = None
    while True:
        for url in urls:
            try:
                if browser is None:
                    from playwright.sync_api import sync_playwright
                    pw = sync_playwright().start()
                    browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
                page = browser.new_page(user_agent=UA)
                try:
                    # skip images/fonts/video so pages load faster
                    page.route("**/*", lambda route: route.abort()
                               if route.request.resource_type in ("image", "media", "font")
                               else route.continue_())
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(RENDER_WAIT_MS)
                    html = page.content()
                finally:
                    page.close()
                record(url, check_html(html, url))
            except Exception as e:
                record_error(url, e)
                try:
                    if browser:
                        browser.close()
                    if pw:
                        pw.stop()
                except Exception:
                    pass
                pw = browser = None
            time.sleep(random.uniform(0.5, 1.5))
        time.sleep(BROWSER_INTERVAL)


def main():
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        raise SystemExit("Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID.")
    if not PRODUCTS:
        raise SystemExit("Add product links to PRODUCTS or PRODUCT_URLS.")

    browser_urls = [u for u in PRODUCTS if any(d in u for d in BROWSER_SITES)]
    plain_urls = [u for u in PRODUCTS if u not in browser_urls]

    telegram(f"✅ <b>Fast PSN monitor started</b>\n"
             f"Watching {len(PRODUCTS)} link(s): {len(plain_urls)} fast, "
             f"{len(browser_urls)} browser.\n"
             f"Report every {REPORT_INTERVAL}s. Stock alerts are instant.")

    for u in plain_urls:
        threading.Thread(target=plain_worker, args=(u,), daemon=True).start()
    if browser_urls:
        threading.Thread(target=browser_worker, args=(browser_urls,), daemon=True).start()
    threading.Thread(target=repeater, daemon=True).start()
    threading.Thread(target=reporter, daemon=True).start()
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
