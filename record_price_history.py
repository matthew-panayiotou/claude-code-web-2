#!/usr/bin/env python3
"""Append a price scrape to the history CSV and regenerate the markdown pivot.

Runs the existing scraper (``scrape_hikvision_cameras.scrape``), appends one row
per product to ``data/price_history.csv`` (long format), then rebuilds
``data/price_history.md`` as a pivot: one row per scrape, one column per product,
each cell showing ``before / after / incl-VAT / availability``
(``A`` = in stock, ``O`` = out of stock).
"""

import csv
import os
from datetime import datetime, timezone

from scrape_hikvision_cameras import scrape

DATA_DIR = "data"
CSV_PATH = os.path.join(DATA_DIR, "price_history.csv")
MD_PATH = os.path.join(DATA_DIR, "price_history.md")

CSV_FIELDS = [
    "scraped_at",
    "model",
    "name",
    "price_before_discount",
    "price_after_discount",
    "price_with_vat",
    "availability",
]


def model_of(name: str) -> str:
    parts = name.split(" | ")
    return parts[1] if len(parts) > 1 else name


def avail_code(availability: str) -> str:
    return "O" if "out" in availability.lower() else "A"


def append_rows(products: list[dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    is_new = not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if is_new:
            writer.writeheader()
        for product in products:
            writer.writerow(
                {
                    "scraped_at": scraped_at,
                    "model": model_of(product["name"]),
                    "name": product["name"],
                    "price_before_discount": product["price_before_discount"],
                    "price_after_discount": product["price_after_discount"],
                    "price_with_vat": product["price_with_vat"],
                    "availability": product["availability"],
                }
            )


def column_label(name: str) -> str:
    # Drop the redundant 'Hikvision | ' prefix and replace inner '|' separators,
    # which would otherwise break the markdown table.
    return name.removeprefix("Hikvision | ").replace(" | ", " · ")


def render_markdown() -> str:
    with open(CSV_PATH, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    names = sorted({row["name"] for row in rows})

    by_time: dict[str, dict[str, str]] = {}
    for row in rows:
        cell = (
            f"{row['price_before_discount']} / {row['price_after_discount']} / "
            f"{row['price_with_vat']} / {avail_code(row['availability'])}"
        )
        by_time.setdefault(row["scraped_at"], {})[row["name"]] = cell

    lines = [
        "# Hikvision DS-2CD2087G3 price history",
        "",
        "Each cell shows **price before discount / price after discount / price incl. "
        "VAT / availability** (EUR; VAT computed at 19%). One row per scrape; "
        "timestamps in UTC.",
        "",
        "**Availability:** `A` = in stock, `O` = out of stock.",
        "",
        "| Scraped (UTC) | " + " | ".join(column_label(n) for n in names) + " |",
        "| --- | " + " | ".join(["---"] * len(names)) + " |",
    ]
    for scraped_at in sorted(by_time):
        cells = [by_time[scraped_at].get(name, "") for name in names]
        lines.append(f"| {scraped_at} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main() -> int:
    products = scrape()
    append_rows(products)
    with open(MD_PATH, "w", encoding="utf-8") as handle:
        handle.write(render_markdown())
    print(f"Recorded {len(products)} products to {CSV_PATH}; regenerated {MD_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
