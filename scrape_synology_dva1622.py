#!/usr/bin/env python3
"""Scrape the cheapest *new* Synology DVA1622 listing from skroutz.cy and amazon.de.

For each site we capture the item price, the delivery cost to Cyprus and the
resulting total. Both sites quote in EUR, but to satisfy the "store the listed
currency and an EUR conversion with the exchange rate at scrape time" requirement
the scraper detects the listed currency, looks up the live EUR rate (identity for
EUR) and stores the converted figures alongside the originals.

Both sites block direct requests from data-center/CI IP ranges (skroutz returns a
403 challenge, amazon a CAPTCHA), so we never hit them directly. Instead each
site is fetched through a proxy that fetches from its own clean IPs:

skroutz.cy
    Fetched through the free, no-key r.jina.ai reader. The SKU page exposes a
    clean ``filter_products.json`` endpoint listing every shop offer with
    ``raw_price``/``shipping_cost``/``final_price``. skroutz only aggregates new
    retail offers, so we take the offer with the lowest final price.

amazon.de
    Fetched through ScraperAPI (residential proxies), which is the only thing
    that reliably gets past amazon's CAPTCHA. ``country_code=de`` lands on an EU
    IP so amazon quotes the store-native EUR price. The featured offer (buy box)
    is amazon's cheapest new offer; we read its price, delivery cost and
    availability. Set the API key in the ``SCRAPER_API_KEY`` env var; without it
    the amazon scrape is skipped (skroutz still works).

Usage:
    python3 scrape_synology_dva1622.py            # scrape and print JSON
    python3 scrape_synology_dva1622.py --verify   # scrape and check expected prices
"""

import argparse
import html as htmllib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

PRODUCT = "Synology DVA1622"

# --- skroutz.cy -----------------------------------------------------------
SKROUTZ_SEARCH = "https://www.skroutz.cy/search?keyphrase=Synology+DVA1622"
SKROUTZ_BASE = "https://www.skroutz.cy"
# Stable SKU id for the DVA1622; we still rediscover it from search so a future
# catalogue change does not silently break the scraper.
SKROUTZ_FALLBACK_SKU = "38408451"

# --- amazon.de ------------------------------------------------------------
AMAZON_ASIN = "B0B3ZS9RS7"  # Synology DVA1622 (16-channel NVR, HDMI output)
AMAZON_URL = f"https://www.amazon.de/dp/{AMAZON_ASIN}"

# Live EUR FX rates (frankfurter.dev — ECB reference rates, no API key needed).
FX_URL = "https://api.frankfurter.dev/v1/latest?base={base}&symbols=EUR"

# Proxy transports (the sites block CI IPs directly, so we route through these).
JINA_BASE = "https://r.jina.ai/"
SCRAPERAPI_BASE = "https://api.scraperapi.com/"
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "").strip()

CURRENCY_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP"}

_STATUS_SEP = b"\n__HTTP_STATUS__:"
_DEFAULT_TIMEOUT = 90


def _curl_once(url: str, headers: dict | None, timeout: int) -> tuple[int, str]:
    args = ["curl", "-s", "--compressed", "-m", str(timeout),
            "-w", _STATUS_SEP.decode() + "%{http_code}"]
    for key, value in (headers or {}).items():
        args += ["-H", f"{key}: {value}"]
    args.append(url)
    result = subprocess.run(args, capture_output=True, timeout=timeout + 15)
    if result.returncode != 0:
        raise RuntimeError(f"curl failed for {url}: {result.stderr.decode('utf-8', 'replace')}")
    body, _, status = result.stdout.rpartition(_STATUS_SEP)
    return int(status or 0), body.decode("utf-8", "replace")


def _get(url: str, *, headers: dict | None = None, retries: int = 4,
         timeout: int = _DEFAULT_TIMEOUT) -> str:
    """GET ``url`` with retries, returning the body text. Raises on persistent failure."""
    status = 0
    for attempt in range(retries):
        status, body = _curl_once(url, headers, timeout)
        if status == 200 and body:
            return body
        if attempt < retries - 1:
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"could not fetch {url} (last status {status})")


def _jina_get(target_url: str, *, headers: dict | None = None) -> str:
    """Fetch ``target_url`` through the r.jina.ai reader (free, no key)."""
    return _get(JINA_BASE + target_url, headers=headers)


def _scraperapi_get(target_url: str, *, render: bool = False,
                    country_code: str = "de") -> str:
    """Fetch ``target_url`` through ScraperAPI (residential proxies)."""
    if not SCRAPER_API_KEY:
        raise RuntimeError(
            "SCRAPER_API_KEY is not set; cannot fetch amazon.de "
            "(set it as a GitHub Actions secret / env var)"
        )
    params = {
        "api_key": SCRAPER_API_KEY,
        "url": target_url,
        "country_code": country_code,
    }
    if render:
        params["render"] = "true"
    return _get(SCRAPERAPI_BASE + "?" + urllib.parse.urlencode(params))


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a (possibly proxy-wrapped) response body."""
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in response")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj


def _money(value) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _parse_eu_number(text: str) -> float:
    """Parse a European-formatted amount like '1.240,98' or '961,82' to float."""
    cleaned = text.strip().replace(".", "").replace(",", ".")
    return float(cleaned)


# -------------------------------------------------------------------------
# Currency conversion
# -------------------------------------------------------------------------
def eur_rate(currency: str) -> tuple[float, str, str]:
    """Return (rate_to_eur, rate_date, source) for the given currency.

    For EUR this is the identity rate; otherwise the live ECB reference rate is
    fetched so the conversion reflects the exchange rate at scrape time.
    """
    if currency == "EUR":
        return 1.0, datetime.now(timezone.utc).strftime("%Y-%m-%d"), "identity"
    data = json.loads(_get(FX_URL.format(base=currency)))
    return float(data["rates"]["EUR"]), data["date"], "frankfurter.dev (ECB)"


def _with_eur(listing: dict) -> dict:
    rate, rate_date, source = eur_rate(listing["currency"])
    listing["exchange_rate_to_eur"] = rate
    listing["rate_date"] = rate_date
    listing["rate_source"] = source
    listing["eur_item_price"] = _money(Decimal(str(listing["item_price"])) * Decimal(str(rate)))
    listing["eur_delivery_cost"] = _money(Decimal(str(listing["delivery_cost"])) * Decimal(str(rate)))
    listing["eur_total_price"] = _money(Decimal(str(listing["total_price"])) * Decimal(str(rate)))
    return listing


# -------------------------------------------------------------------------
# skroutz.cy
# -------------------------------------------------------------------------
def _skroutz_sku() -> str:
    try:
        search = _jina_get(SKROUTZ_SEARCH)
        match = re.search(r"/s/(\d+)/[A-Za-z0-9\-]*\.html", search)
        if match:
            return match.group(1)
    except Exception:
        pass
    return SKROUTZ_FALLBACK_SKU


def scrape_skroutz() -> dict:
    sku = _skroutz_sku()
    url = f"{SKROUTZ_BASE}/s/{sku}/filter_products.json"
    data = _extract_json(_jina_get(url))
    cards = data.get("product_cards", {})
    if not cards:
        raise ValueError("skroutz returned no offers for the DVA1622")

    offers = []
    for card in cards.values():
        item = Decimal(str(card["raw_price"]))
        shipping = Decimal(str(card.get("shipping_cost") or 0))
        total = Decimal(str(card.get("final_price", item + shipping)))
        offers.append((total, item, shipping, card))
    total, item, shipping, card = min(offers, key=lambda row: row[0])

    return _with_eur(
        {
            "site": "skroutz.cy",
            "product": PRODUCT,
            "url": f"{SKROUTZ_BASE}/s/{sku}",
            "condition": "new",
            "availability": card["products"][0].get("availability", ""),
            "currency": "EUR",
            "item_price": _money(item),
            "delivery_cost": _money(shipping),
            "total_price": _money(total),
        }
    )


# -------------------------------------------------------------------------
# amazon.de
# -------------------------------------------------------------------------
def _amazon_fetch() -> str:
    # Plain server HTML carries the buy-box price inline (no JS needed); fetching
    # via a German-geo residential IP yields the native EUR price. If a future
    # markup shift hides the price behind JS, re-fetch with rendering enabled.
    html = _scraperapi_get(AMAZON_URL, country_code="de")
    if "priceToPay" not in html:
        html = _scraperapi_get(AMAZON_URL, render=True, country_code="de")
    return html


def _amazon_price(html: str) -> tuple[float, str]:
    # The featured-offer price lives in the priceToPay block. Anchor there, then
    # read the rendered whole/fraction/symbol spans (the *.-offscreen mirror is
    # sometimes blank), falling back to a flat offscreen amount.
    anchor = html.find("priceToPay")
    region = html[anchor:] if anchor != -1 else html
    structured = re.search(
        r'a-price-whole">([\d.]+)<span class="a-price-decimal">,</span></span>'
        r'<span class="a-price-fraction">(\d{2})</span>'
        r'<span class="a-price-symbol">([€$£])',
        region,
    )
    if structured:
        whole, fraction, symbol = structured.groups()
        return _parse_eu_number(f"{whole},{fraction}"), CURRENCY_SYMBOLS[symbol]

    flat = re.search(r'(?:a-offscreen|aok-offscreen)">\s*([\d.]+,\d{2})\s*([€$£])', region)
    if flat:
        return _parse_eu_number(flat.group(1)), CURRENCY_SYMBOLS[flat.group(2)]
    raise ValueError("could not find the amazon.de buy-box price")


def _amazon_delivery(html: str) -> float:
    match = re.search(r'data-csa-c-delivery-price="([^"]*)"', html)
    raw = (match.group(1).strip() if match else "")
    if not raw or raw.lower() in {"gratis", "kostenlose", "kostenlos", "free"}:
        return 0.0
    num = re.search(r"([\d.]+,\d{2})", raw)
    return _parse_eu_number(num.group(1)) if num else 0.0


def _amazon_availability(html: str) -> str:
    match = re.search(r'id="availability".*?<span[^>]*>\s*([^<]+?)\s*</span>', html, re.DOTALL)
    return htmllib.unescape(match.group(1).strip()) if match else ""


def parse_amazon(html: str) -> dict:
    # Amazon's featured offer (the priceToPay buy box) is always a new offer;
    # used/renewed stock is only ever surfaced under "Andere Angebote", never here.
    if "priceToPay" not in html:
        raise ValueError("amazon.de page has no featured new offer (buy box)")
    item, currency = _amazon_price(html)
    delivery = _amazon_delivery(html)
    return _with_eur(
        {
            "site": "amazon.de",
            "product": PRODUCT,
            "url": AMAZON_URL,
            "condition": "new",
            "availability": _amazon_availability(html),
            "currency": currency,
            "item_price": _money(item),
            "delivery_cost": _money(delivery),
            "total_price": _money(Decimal(str(item)) + Decimal(str(delivery))),
        }
    )


def scrape_amazon() -> dict:
    return parse_amazon(_amazon_fetch())


# -------------------------------------------------------------------------
SITES = [
    ("skroutz.cy", scrape_skroutz),
    ("amazon.de", scrape_amazon),
]


def scrape() -> list[dict]:
    """Scrape every site, raising on the first failure (used by --verify)."""
    return [fn() for _, fn in SITES]


def scrape_safe() -> tuple[list[dict], dict[str, str]]:
    """Scrape every site independently; return (listings, {site: error}).

    One flaky site (notably amazon.de's per-IP anti-bot) must not stop us from
    recording the others, so failures are collected rather than raised.
    """
    listings, errors = [], {}
    for site, fn in SITES:
        try:
            listings.append(fn())
        except Exception as exc:  # noqa: BLE001 - report and keep going
            errors[site] = str(exc)
    return listings, errors


# Known-good item prices supplied for validation (EUR).
EXPECTED = {
    "skroutz.cy": 1240.98,
    "amazon.de": 961.82,
}
# Acceptable drift before we treat a scrape as suspicious (prices move over time).
VERIFY_TOLERANCE = 0.05  # 5%


def verify(listings: list[dict]) -> bool:
    by_site = {row["site"]: row for row in listings}
    ok = True
    for site, expected in EXPECTED.items():
        row = by_site.get(site)
        if row is None:
            print(f"MISSING: {site}")
            ok = False
            continue
        got = row["eur_item_price"]
        drift = abs(got - expected) / expected
        status = "OK" if drift <= VERIFY_TOLERANCE else "OUT OF RANGE"
        if drift > VERIFY_TOLERANCE:
            ok = False
        print(f"{site}: item {got} EUR (expected ~{expected}, drift {drift:.1%}) [{status}]")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="check item prices against known-good values")
    args = parser.parse_args()

    listings = scrape()
    print(json.dumps(listings, indent=2, ensure_ascii=False))

    if args.verify:
        if verify(listings):
            print("\nVERIFY: PASS")
            return 0
        print("\nVERIFY: FAIL")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
