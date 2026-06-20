#!/usr/bin/env python3
"""Scrape Hikvision DS-2CD2087G3 ColorVu camera listings from megateh.eu.

The listing page renders ex-VAT prices (after discount and before discount)
and an availability label for each product. The VAT-inclusive price shown on
the page depends on the visitor's geolocated country, so instead of scraping
that line we derive it deterministically from the after-discount price using a
fixed VAT rate (19%). Everything else is taken straight from the page.

Usage:
    python3 scrape_hikvision_cameras.py            # scrape and print JSON
    python3 scrape_hikvision_cameras.py --verify   # scrape and check expected data
"""

import argparse
import json
import re
import sys
import urllib.request
from decimal import ROUND_HALF_UP, Decimal

URL = (
    "https://www.megateh.eu/products/DS-2CD2087G3"
    "?part=1&noscroll=1&mod%5B7%5D%5B1%5D=1&order=1"
)

# Page prices exclude VAT; VAT-inclusive price is computed at this rate.
VAT_RATE = Decimal("0.19")

EXPECTED_PRODUCT_COUNT = 4

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Each product card starts with this marker; splitting on it isolates the cards.
_CARD_MARKER = '<div class="products product_'
_LISTING_ANCHOR = 'class="products-list list buttons3"'

_NAME_RE = re.compile(r"<h3>(.*?)</h3>", re.DOTALL)
# After-discount price: <b>306</b>.57  -> integer and decimal parts.
_AFTER_RE = re.compile(r"<b>(\d+)</b>\.(\d+)")
# Before-discount price sits in the only <span> of the card: <span>408.76</span>.
_BEFORE_RE = re.compile(r"<span>\s*([\d]+\.[\d]+)\s*</span>")
_AVAIL_RE = re.compile(r"Availability:\s*([^<]+)")


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", "replace")


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def parse(html: str) -> list[dict]:
    anchor = html.find(_LISTING_ANCHOR)
    if anchor == -1:
        raise ValueError("could not locate the product listing on the page")

    cards = html[anchor:].split(_CARD_MARKER)[1:]
    products = []
    for card in cards:
        name_match = _NAME_RE.search(card)
        after_match = _AFTER_RE.search(card)
        before_match = _BEFORE_RE.search(card)
        avail_match = _AVAIL_RE.search(card)
        if not (name_match and after_match and before_match and avail_match):
            continue

        after = Decimal(f"{after_match.group(1)}.{after_match.group(2)}")
        before = Decimal(before_match.group(1))
        with_vat = after * (Decimal("1") + VAT_RATE)

        products.append(
            {
                "name": " ".join(name_match.group(1).split()),
                "price_before_discount": _money(before),
                "price_after_discount": _money(after),
                "price_with_vat": _money(with_vat),
                "availability": avail_match.group(1).strip(),
            }
        )
    return products


def scrape() -> list[dict]:
    products = parse(fetch(URL))
    if len(products) != EXPECTED_PRODUCT_COUNT:
        raise SystemExit(
            f"expected {EXPECTED_PRODUCT_COUNT} products, found {len(products)}"
        )
    return products


EXPECTED = [
    {
        "name": "Hikvision | DS-2CD2087G3-LI2UY/SL | ColorVu 8MP Bullet IP Camera 2.8mm Fixed Lens",
        "price_before_discount": 408.76,
        "price_after_discount": 306.57,
        "price_with_vat": 364.82,
        "availability": "in stock",
    },
    {
        "name": "Hikvision | DS-2CD2087G3-LI2UY/SRB | ColorVu 8MP Bullet IP Camera 4mm Fixed Lens",
        "price_before_discount": 408.76,
        "price_after_discount": 306.57,
        "price_with_vat": 364.82,
        "availability": "in stock",
    },
    {
        "name": "Hikvision | DS-2CD2087G3-LIY | ColorVu 8MP Bullet IP Camera 2.8mm Fixed Lens",
        "price_before_discount": 405.09,
        "price_after_discount": 303.82,
        "price_with_vat": 361.55,
        "availability": "Temporarily out of stock",
    },
]


def verify(products: list[dict]) -> bool:
    by_name = {p["name"]: p for p in products}
    ok = True
    for want in EXPECTED:
        got = by_name.get(want["name"])
        if got is None:
            print(f"MISSING: {want['name']}")
            ok = False
            continue
        for field in (
            "price_before_discount",
            "price_after_discount",
            "price_with_vat",
        ):
            if got[field] != want[field]:
                print(f"MISMATCH [{want['name']}] {field}: got {got[field]} want {want[field]}")
                ok = False
        if got["availability"].lower() != want["availability"].lower():
            print(
                f"MISMATCH [{want['name']}] availability: "
                f"got {got['availability']!r} want {want['availability']!r}"
            )
            ok = False
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="check scraped data against the known-good expected values",
    )
    args = parser.parse_args()

    products = scrape()
    print(json.dumps(products, indent=2, ensure_ascii=False))

    if args.verify:
        if verify(products):
            print("\nVERIFY: PASS (3 products, all fields match expected data)")
            return 0
        print("\nVERIFY: FAIL")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
