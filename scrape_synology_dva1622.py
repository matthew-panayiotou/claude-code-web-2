#!/usr/bin/env python3
"""Scrape the cheapest *new* Synology DVA1622 listing from skroutz.cy and amazon.de.

For each site we capture the item price, the delivery cost to Cyprus and the
resulting total. Both sites quote in EUR, but to satisfy the "store the listed
currency and an EUR conversion with the exchange rate at scrape time" requirement
the scraper detects the listed currency, looks up the live EUR rate (identity for
EUR) and stores the converted figures alongside the originals.

Both sites sit behind bot protection that rejects Python's urllib TLS handshake
(403/503), so we shell out to ``curl`` (HTTP/2 + brotli), which they accept.

skroutz.cy
    The SKU page exposes a clean ``filter_products.json`` endpoint listing every
    shop offer with ``raw_price``/``shipping_cost``/``final_price``. skroutz only
    aggregates new retail offers, so we simply take the offer with the lowest
    final price.

amazon.de
    amazon.de defaults to a USD price overlay; we force EUR with the
    ``i18n-prefs=EUR`` cookie so the listed currency is the store-native EUR.
    The featured offer (buy box) is Amazon's cheapest new offer; we read its
    price, delivery cost and availability. Product pages are served
    intermittently from data-center IPs, so the fetch retries until a full page
    (not a CAPTCHA stub) comes back.

Usage:
    python3 scrape_synology_dva1622.py            # scrape and print JSON
    python3 scrape_synology_dva1622.py --verify   # scrape and check expected prices
"""

import argparse
import html as htmllib
import json
import re
import subprocess
import sys
import time
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

DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)

# A real amazon.de product page is hundreds of KB; CAPTCHA / "503" stubs are ~1KB.
# amazon throttles per-IP, so we make a modest number of *spaced* attempts rather
# than hammering (rapid retries only deepen the block).
AMAZON_MIN_BYTES = 80_000
AMAZON_MAX_TRIES = 12
AMAZON_RETRY_DELAY = 4.0  # seconds between attempts

CURRENCY_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP"}


_STATUS_SEP = b"\n__HTTP_STATUS__:"


def _curl_once(url: str, headers: dict | None) -> tuple[int, bytes]:
    args = ["curl", "-s", "--http2", "--compressed", "-A", DESKTOP_UA,
            "-w", _STATUS_SEP.decode() + "%{http_code}"]
    for key, value in (headers or {}).items():
        args += ["-H", f"{key}: {value}"]
    args.append(url)
    result = subprocess.run(args, capture_output=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"curl failed for {url}: {result.stderr.decode('utf-8', 'replace')}")
    body, _, status = result.stdout.rpartition(_STATUS_SEP)
    return int(status or 0), body


def _curl(url: str, *, headers: dict | None = None, retries: int = 6) -> bytes:
    """Fetch a URL, retrying past the sites' intermittent anti-bot 403/503 stubs."""
    last = b""
    for attempt in range(retries):
        status, body = _curl_once(url, headers)
        if status == 200 and body:
            return body
        last = body
        if attempt < retries - 1:
            time.sleep(1.5)
    raise RuntimeError(f"could not fetch {url} (last status {status}, {len(last)} bytes)")


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
    data = json.loads(_curl(FX_URL.format(base=currency)))
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
        search = _curl(SKROUTZ_SEARCH, headers={"Accept-Language": "el-GR,el;q=0.9,en;q=0.8"})
        match = re.search(rb"/s/(\d+)/[^\"]*\.html", search)
        if match:
            return match.group(1).decode()
    except Exception:
        pass
    return SKROUTZ_FALLBACK_SKU


def scrape_skroutz() -> dict:
    sku = _skroutz_sku()
    url = f"{SKROUTZ_BASE}/s/{sku}/filter_products.json"
    data = json.loads(
        _curl(
            url,
            headers={
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Accept-Language": "el-GR,el;q=0.9",
            },
        )
    )
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
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        # Force EUR (the store-native currency) instead of the USD price overlay.
        "Cookie": "i18n-prefs=EUR; lc-acbde=de_DE",
    }
    for attempt in range(AMAZON_MAX_TRIES):
        status, body = _curl_once(AMAZON_URL, headers)
        # Amazon serves a ~1KB CAPTCHA/503 stub (sometimes with a 200) from
        # data-center IPs; a genuine product page is hundreds of KB.
        if status == 200 and len(body) >= AMAZON_MIN_BYTES:
            return body.decode("utf-8", "replace")
        if attempt < AMAZON_MAX_TRIES - 1:
            time.sleep(AMAZON_RETRY_DELAY)
    raise RuntimeError(
        f"amazon.de kept returning anti-bot stubs after {AMAZON_MAX_TRIES} tries"
    )


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
