#!/usr/bin/env python3
"""Build a demo SQLite database for exercising QC end-to-end.

Every table targets a specific QC code path so profile/sanitize/generate
runs are informative rather than trivially green:

  customers   layered sensitivity detection: name heuristics, Luhn-valid
              cards, JWT/hex tokens, IPs, government ids, zero-padded refs
  orders      financial + free text + ambiguous dates + FK relationship
  events      type-inference ambiguity: untyped mixed column, ambiguous
              day/month dates, mixed boolean encodings, NaN/Inf, sentinels
  event_log   WITHOUT ROWID table (exercises the rowid sampling fallback)
  metrics_daily  a view, so discovery reports view vs table correctly
  empty_audit    zero rows (aggregate edge case)

All values are fabricated. Emails use RFC 2606 example domains, phones use
the 555-01xx fictional block, IPs use RFC 5737 documentation ranges, and
card numbers are Luhn-valid but issued by nobody.

Deterministic: seeded, so reruns produce identical databases.

    python3 make_demo_db.py [--out PATH] [--reset]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
from pathlib import Path

SEED = 20251012

FIRST = ["ada", "brook", "cass", "dara", "emil", "fern", "gale", "hana", "iris", "jules"]
LAST = ["vale", "quill", "merrow", "sable", "thorne", "ashby", "wren", "frost", "dune", "kestrel"]
COUNTRIES = ["US", "GB", "CA", "DE", "JP", "BR"]
CURRENCIES = ["USD", "EUR", "GBP", "JPY"]
STATUSES = ["pending", "paid", "shipped", "cancelled", "refunded"]
CATEGORIES = ["click", "view", "purchase", "signup", "error"]
STATUS_TEXT = ["ok", "degraded", "down"]

# RFC 5737 documentation ranges; never routable.
DOC_IPS = ["192.0.2.", "198.51.100.", "203.0.113."]


def luhn_complete(prefix: str) -> str:
    """Append a Luhn check digit so detectors see a genuinely card-shaped value."""
    total = 0
    parity = len(prefix) % 2
    for i, ch in enumerate(prefix):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return prefix + str((10 - total % 10) % 10)


def build(out: Path, reset: bool) -> None:
    if out.exists():
        if not reset:
            raise SystemExit(f"{out} already exists (pass --reset to overwrite)")
        out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)

    conn = sqlite3.connect(out)
    try:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE customers (
                id             INTEGER PRIMARY KEY,
                customer_ref   TEXT,          -- zero-padded: must stay a string/identifier
                full_name      TEXT,
                email          TEXT,
                phone          TEXT,
                birth_date     TEXT,          -- ambiguous dd/mm vs mm/dd
                ssn            TEXT,          -- 123-45-6789 shape
                credit_card    TEXT,          -- Luhn-valid
                country_code   TEXT,
                postal_code    TEXT,
                last_ip        TEXT,          -- documentation ranges only
                api_token      TEXT,          -- JWT shape
                session_secret TEXT,          -- high-entropy hex
                loyalty_points INTEGER,       -- real zeros and the -9999 sentinel
                is_active      INTEGER        -- boolean-as-int, with strays
            );

            CREATE TABLE orders (
                id            INTEGER PRIMARY KEY,
                customer_id   INTEGER REFERENCES customers(id),
                order_uuid    TEXT,            -- UUID upgrade from string
                total_amount  REAL,            -- -9999 sentinel and genuine 0.00
                currency_code TEXT,
                status        TEXT,            -- low cardinality -> categorical
                discount_code TEXT,            -- NULL / '' / '   ' / 'N/A' / real codes
                created_at    TEXT,            -- mixed + ambiguous formats
                notes         TEXT,            -- long free text (>120 chars) sometimes
                ip_address    TEXT
            );

            -- deliberately untyped column: BLOB affinity, so mixed types survive
            CREATE TABLE events (
                id            INTEGER PRIMARY KEY,
                event_id      TEXT,
                value,                          -- int / float / text / NULL mixed
                occurred_on   TEXT,            -- ambiguous day/month ordering
                flag_value,                     -- 1 / 0 / 'true' / 'false' / NULL
                ratio         REAL,            -- NaN and +/-Infinity
                payload       TEXT,            -- JSON documents
                category      TEXT,
                metric_bignum TEXT             -- integer-like strings beyond float64
            );

            CREATE TABLE event_log (
                at        TEXT,
                level     TEXT,
                message   TEXT,
                PRIMARY KEY (at, level)
            ) WITHOUT ROWID;

            CREATE VIEW metrics_daily AS
                SELECT category, count(*) AS events, min(occurred_on) AS first_seen
                FROM events GROUP BY category;

            CREATE TABLE empty_audit (
                id     INTEGER PRIMARY KEY,
                detail TEXT
            );
            """
        )

        # ---------------------------------------------------------- customers
        customers = []
        for i in range(1, 201):
            name = f"{FIRST[i % len(FIRST)].capitalize()} {LAST[(i * 3) % len(LAST)].capitalize()}"
            email = None if i % 17 == 0 else f"{FIRST[i % 10]}.{LAST[(i*3)%10]}@example.com"
            phone = f"+1-555-01{i % 100:02d}"
            # ambiguous ordering on purpose: both readings are valid dates
            birth = f"{(i % 28) + 1:02d}/{(i % 12) + 1:02d}/19{60 + (i % 40)}"
            card = luhn_complete("411111111111111"[:15])
            token = f"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1{i}In0.{'a1b2c3d4e5f6' * 2}"
            secret = "".join(rng.choice("0123456789abcdef") for _ in range(48))
            customers.append(
                (
                    i,
                    f"{i:06d}",                       # leading zeros -> identifier
                    name,
                    email,
                    phone,
                    birth,
                    f"{100 + i % 800:03d}-{10 + i % 80:02d}-{1000 + i % 9000:04d}",
                    card,
                    COUNTRIES[i % len(COUNTRIES)],
                    f"{10000 + i}",
                    f"{DOC_IPS[i % 3]}{i % 254 + 1}",
                    token,
                    secret,
                    -9999 if i % 37 == 0 else (0 if i % 11 == 0 else rng.randint(1, 900)),
                    1 if i % 3 else 0,
                )
            )
        # a couple of genuinely messy rows
        customers.append((201, "000201", "Ada  Vale", "", "+1-555-0100", "01/02/1990", "", "", "US", "10001", "", "", "", 0, "yes"))
        customers.append((202, "000202", None, None, None, None, None, None, "GB", "", "", "", "", -9999, 0))
        conn.executemany(
            "INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", customers
        )

        # ------------------------------------------------------------- orders
        orders = []
        for i in range(1, 601):
            cid = rng.randint(1, 202)
            # -9999 sentinel, a real 0.00, and ordinary amounts
            if i % 97 == 0:
                amount = -9999.0
            elif i % 31 == 0:
                amount = 0.0
            else:
                amount = round(rng.uniform(5, 999), 2)
            # every falsy variant the analyzer distinguishes
            disc = rng.choice([None, "", "   ", "N/A", "unknown", f"SAVE{rng.randint(1, 20)}"])
            created = rng.choice(
                [
                    f"2025-0{(i % 9) + 1}-1{i % 9}",
                    f"{(i % 28) + 1:02d}/{(i % 12) + 1:02d}/2025",   # ambiguous
                    f"2025-0{(i % 9) + 1}-1{i % 9} 1{i % 9}:30:00",
                ]
            )
            notes = None
            if i % 4 == 0:
                notes = (
                    f"Customer asked for expedited handling on order {i}; "
                    "the warehouse confirmed stock but flagged a damaged outer carton "
                    "and requested photographic evidence before dispatch. Follow up "
                    "with the account manager before the end of the week."
                )
            elif i % 4 == 1:
                notes = ""
            orders.append(
                (
                    i,
                    cid,
                    f"{i:08x}-0000-4000-8000-{i:012x}",
                    amount,
                    CURRENCIES[i % len(CURRENCIES)],
                    STATUSES[i % len(STATUSES)],
                    disc,
                    created,
                    notes,
                    f"{DOC_IPS[(i + 1) % 3]}{(i * 3) % 254 + 1}",
                )
            )
        conn.executemany(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)", orders
        )

        # ------------------------------------------------------------- events
        events = []
        for i in range(1, 501):
            # mixed types in one untyped column
            value = rng.choice(
                [i, None, f"text-{i}", round(rng.uniform(0, 1), 3), 0, "", True, False]
            )
            # ambiguous dates + a couple of unambiguous ISO ones
            occurred = (
                f"2025-03-{(i % 28) + 1:02d}" if i % 5 == 0
                else f"{(i % 12) + 1:02d}/{(i % 28) + 1:02d}/2025"
            )
            flag = rng.choice([1, 0, "true", "false", None])
            r = rng.random()
            ratio = math.nan if i % 97 == 0 else (math.inf if i % 89 == 0 else round(r, 4))
            payload = json.dumps({"user": i % 25, "ms": rng.randint(1, 900), "ok": r > 0.2})
            bignum = str(10**25 + i) if i % 3 == 0 else str(i)
            events.append(
                (
                    i,
                    f"{i:07d}" if i % 2 else f"E{i}",
                    value,
                    occurred,
                    flag,
                    ratio,
                    payload,
                    CATEGORIES[i % len(CATEGORIES)],
                    bignum,
                )
            )
        conn.executemany(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", events
        )

        # ---------------------------------------------------------- event_log
        log_rows = []
        for i in range(120):
            log_rows.append(
                (
                    f"2025-06-{i % 28 + 1:02d}T0{i % 10}:15:00",
                    STATUS_TEXT[i % len(STATUS_TEXT)],
                    f"event {i}: " + ("detail " * (i % 9 + 1)),
                )
            )
        conn.executemany("INSERT INTO event_log VALUES (?,?,?)", log_rows)

        conn.commit()
    finally:
        conn.close()


EXPECTATIONS = """\
customers       202  layered sensitivity: name heuristics (email/phone/ssn/credit_card/
               last_ip/api_token), Luhn-valid cards, JWT + high-entropy hex tokens,
               zero-padded customer_ref (must stay identifier), birth_date ambiguity,
               loyalty_points with real 0 and the -9999 sentinel
orders          600  free-text notes (>120 chars), FK -> customers, UUID upgrade,
               total_amount -9999/0.00/normal, discount_code covering NULL vs ''
               vs '   ' vs 'N/A', created_at with ambiguous day/month
events          500  untyped `value` column mixing int/float/text/NULL/bool,
               ambiguous occurred_on, flag_value 1/0/'true'/'false'/NULL,
               ratio NaN/+Inf, metric_bignum beyond float64
event_log       120  WITHOUT ROWID (exercises the rowid sampling fallback)
metrics_daily       view, so discovery reports view vs table
empty_audit         0  zero-row aggregate edge case
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="demo.db", type=Path)
    ap.add_argument("--reset", action="store_true", help="overwrite an existing file")
    args = ap.parse_args()
    build(args.out, args.reset)
    size = args.out.stat().st_size
    print(f"wrote {args.out} ({size:,} bytes)")
    print(EXPECTATIONS)


if __name__ == "__main__":
    main()
