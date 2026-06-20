#!/usr/bin/env python3
"""Append a Synology DVA1622 price scrape and refresh its section in the markdown.

Runs ``scrape_synology_dva1622.scrape``, appends one row per site to
``data/synology_price_history.csv`` (long format), then rewrites a dedicated,
marker-delimited Synology section inside ``data/price_history.md``.

The section is delimited by HTML comment markers so it can be regenerated in
place: the camera recorder owns the top of ``price_history.md`` and rewrites the
whole file each run, so this recorder must run *after* it (see the workflow) and
re-append its own section every time.
"""

import csv
import os
import sys

from scrape_synology_dva1622 import PRODUCT, scrape_safe
from datetime import datetime, timezone

DATA_DIR = "data"
CSV_PATH = os.path.join(DATA_DIR, "synology_price_history.csv")
MD_PATH = os.path.join(DATA_DIR, "price_history.md")

SECTION_START = "<!-- SYNOLOGY-DVA1622:START -->"
SECTION_END = "<!-- SYNOLOGY-DVA1622:END -->"

CSV_FIELDS = [
    "scraped_at",
    "site",
    "product",
    "url",
    "condition",
    "availability",
    "currency",
    "item_price",
    "delivery_cost",
    "total_price",
    "exchange_rate_to_eur",
    "rate_date",
    "rate_source",
    "eur_item_price",
    "eur_delivery_cost",
    "eur_total_price",
]


def append_rows(listings: list[dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    is_new = not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if is_new:
            writer.writeheader()
        for row in listings:
            writer.writerow({"scraped_at": scraped_at, **{k: row.get(k, "") for k in CSV_FIELDS if k != "scraped_at"}})


def _cell(row: dict) -> str:
    # item / delivery / total, all in EUR; star marks a non-EUR listed currency.
    note = "" if row["currency"] == "EUR" else f" ({row['item_price']} {row['currency']})"
    return f"{row['eur_item_price']} / {row['eur_delivery_cost']} / {row['eur_total_price']}{note}"


def render_section() -> str:
    with open(CSV_PATH, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    sites = sorted({row["site"] for row in rows})
    by_time: dict[str, dict[str, str]] = {}
    for row in rows:
        by_time.setdefault(row["scraped_at"], {})[row["site"]] = _cell(row)

    lines = [
        SECTION_START,
        f"# {PRODUCT} price history",
        "",
        "Cheapest **new** listing per site. Each cell shows **item price / delivery to "
        "Cyprus / total** in EUR. Both sites quote in EUR; a non-EUR listed price would "
        "be shown in parentheses, and the EUR figures use the exchange rate recorded at "
        "scrape time (see the CSV). One row per scrape; timestamps in UTC.",
        "",
        "| Scraped (UTC) | " + " | ".join(sites) + " |",
        "| --- | " + " | ".join(["---"] * len(sites)) + " |",
    ]
    for scraped_at in sorted(by_time):
        cells = [by_time[scraped_at].get(site, "") for site in sites]
        lines.append(f"| {scraped_at} | " + " | ".join(cells) + " |")
    lines.append(SECTION_END)
    return "\n".join(lines) + "\n"


def write_section(section: str) -> None:
    existing = ""
    if os.path.exists(MD_PATH):
        with open(MD_PATH, encoding="utf-8") as handle:
            existing = handle.read()

    start = existing.find(SECTION_START)
    if start != -1:
        end = existing.find(SECTION_END, start)
        end = len(existing) if end == -1 else end + len(SECTION_END)
        existing = (existing[:start].rstrip() + "\n" + existing[end:].lstrip()).rstrip("\n")

    parts = [existing.rstrip("\n")] if existing.strip() else []
    parts.append(section.rstrip("\n"))
    with open(MD_PATH, "w", encoding="utf-8") as handle:
        handle.write("\n\n".join(parts) + "\n")


def main() -> int:
    listings, errors = scrape_safe()
    for site, message in errors.items():
        print(f"WARNING: skipped {site}: {message}", file=sys.stderr)
    if not listings:
        print("ERROR: no sites could be scraped", file=sys.stderr)
        return 1
    append_rows(listings)
    write_section(render_section())
    print(f"Recorded {len(listings)} listings to {CSV_PATH}; updated Synology section in {MD_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
