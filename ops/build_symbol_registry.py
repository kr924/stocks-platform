#!/usr/bin/env python3
"""
Rebuild the NSE/BSE symbol registry by hand.

The same job the scheduler runs at 17:00 IST on Sundays. Run it directly to
bootstrap a fresh database, to recover from a failed weekly run without waiting
a week, or to see what a rebuild would change before it happens.

    python ops/build_symbol_registry.py                # rebuild and apply
    python ops/build_symbol_registry.py --dry-run      # report, change nothing
    python ops/build_symbol_registry.py --force        # accept a big shrink
    python ops/build_symbol_registry.py --export out.csv

On the VM, inside the container so it writes to the mounted database:

    sudo docker exec -it stocks-app python ops/build_symbol_registry.py

--export writes the joined registry as CSV in the same column order as
data/symbol_registry.csv, which is how the checked-in seed is refreshed. The
seed only matters for a database that has never been built, so refreshing it is
housekeeping, not deployment — the running platform reads the table.
"""
import argparse
import csv
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

CSV_COLUMNS = ["ISIN", "COMPANY_NAME", "EXCHANGE_LISTING",
               "NSE_SYMBOL", "BSE_SCRIP_CD", "BSE_SCRIP_ID"]


def _export(rows, path):
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for r in rows:
            writer.writerow([r["isin"], r["company_name"], r["exchange_listing"],
                             r["nse_symbol"], r["bse_scrip_cd"], r["bse_scrip_id"]])
    print(f"wrote {len(rows)} rows to {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and join, report the diff, write nothing")
    parser.add_argument("--force", action="store_true",
                        help="apply even if the registry would shrink by more than 5%%")
    parser.add_argument("--export", metavar="PATH",
                        help="also write the joined registry to a CSV")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from app.database import SessionLocal, SymbolRegistryEntry, init_db
    from app.services import registry_builder as rb
    from app.services import symbol_registry

    init_db()
    # Seed an empty table from the checked-in CSV before diffing against it,
    # exactly as the app does on its first lookup. Without this a fresh database
    # reports every listing as an addition and the diff says nothing.
    symbol_registry._load()

    db = SessionLocal()
    try:
        if args.dry_run:
            try:
                rows, dropped = rb.build_rows(rb.fetch_nse_main(),
                                              rb.fetch_nse_sme(),
                                              rb.fetch_bse_scrips())
            except Exception as e:
                print(f"FAILED: {e}")
                return 1

            current = {r.isin for r in db.query(SymbolRegistryEntry).all()}
            new = {r["isin"] for r in rows}
            added = new - current
            gone = current - new
            dual = [r for r in rows
                    if r["isin"] in added and r["exchange_listing"] == "both nse/bse"]

            print(f"\njoined       {len(rows)} listings  (dropped without ISIN: {dropped})")
            print(f"in database  {len(current)}")
            print(f"would add    {len(added)}   of which dual-listed NSE+BSE: {len(dual)}")
            print(f"would drop   {len(gone)}")
            if dual:
                # ASCII only: this runs on a Windows console and inside a
                # container, and neither reliably encodes a dash.
                print("\ndual-listed additions - these are the ones that would otherwise "
                      "raise two prompts for one result:")
                for r in sorted(dual, key=lambda r: r["nse_symbol"])[:25]:
                    print(f"  {r['nse_symbol']:<12} {r['bse_scrip_cd']:<8} {r['company_name']}")
                if len(dual) > 25:
                    print(f"  ... and {len(dual) - 25} more")
            if args.export:
                _export(rows, args.export)
            return 0

        summary = rb.rebuild_symbol_registry(db, force=args.force)
        if not summary.get("ok"):
            print(f"\nFAILED: {summary.get('error')}\nThe previous registry is untouched.")
            return 1
        print(f"\nrebuilt {summary['previous_rows']} -> {summary['rows']} listings "
              f"({summary['added']:+d}), {summary['indexed']} indexed")
        if args.export:
            rows = [{"isin": r.isin, "company_name": r.company_name,
                     "exchange_listing": r.exchange_listing, "nse_symbol": r.nse_symbol,
                     "bse_scrip_cd": r.bse_scrip_cd, "bse_scrip_id": r.bse_scrip_id}
                    for r in db.query(SymbolRegistryEntry).order_by(SymbolRegistryEntry.isin).all()]
            _export(rows, args.export)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
