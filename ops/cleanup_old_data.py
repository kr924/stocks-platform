#!/usr/bin/env python3
"""
Apply the retention policy by hand.

The same sweep the scheduler runs at 18:00 IST on Sundays. Run it to reclaim
space now rather than waiting for the week, or with --dry-run to see exactly
what would go before anything does.

    python ops/cleanup_old_data.py --dry-run     # count only, delete nothing
    python ops/cleanup_old_data.py               # sweep
    python ops/cleanup_old_data.py --vacuum      # sweep, then shrink the file

On the VM, inside the container so it works on the mounted database:

    sudo docker exec -it stocks-app python ../ops/cleanup_old_data.py --dry-run

Two tiers: raw news goes at 5 days, anything the trading path or the earnings
calendar reads is held for 30. services/retention.py explains why that split is
a dependency and not a preference.

--vacuum is separate because VACUUM takes an exclusive lock and rewrites the
whole database, needing free disk equal to its size. Deleting alone already
stops the file growing — SQLite reuses the freed pages — so vacuuming is only
for handing space back to the filesystem. Do it when the market is shut.
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be deleted, delete nothing")
    parser.add_argument("--vacuum", action="store_true",
                        help="after sweeping, rewrite the file to return space to the disk")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from app.database import SessionLocal, init_db
    from app.services.retention import (
        NEWS_RETENTION_DAYS, TRADING_RETENTION_DAYS,
        database_size_mb, run_retention, vacuum,
    )

    init_db()
    db = SessionLocal()
    try:
        before = database_size_mb(db)
        print(f"database: {before} MB")
        print(f"policy  : raw news {NEWS_RETENTION_DAYS}d, "
              f"trading + earnings calendar {TRADING_RETENTION_DAYS}d\n")

        counts = run_retention(db, dry_run=args.dry_run)

        verb = "would delete" if args.dry_run else "deleted"
        width = max(len(k) for k in counts)
        total = 0
        for label, n in counts.items():
            print(f"  {label:<{width}}  {n:>8}  {verb}")
            if isinstance(n, int):
                total += n
        print(f"\n  {'total':<{width}}  {total:>8}")

        if args.dry_run:
            print("\nnothing was deleted. Re-run without --dry-run to apply.")
            print("Note: 'news_stories (orphaned)' is counted against the articles that "
                  "are still\n      present, so the real sweep orphans — and removes — "
                  "far more than shown here.")
            return 0

        after = database_size_mb(db)
        print(f"\ndatabase: {before} MB -> {after} MB")
        if args.vacuum:
            print("vacuuming (exclusive lock, rewrites the file)...")
            vacuum(db)
            print(f"database: {after} MB -> {database_size_mb(db)} MB")
        else:
            print("Pages are free for reuse but still allocated. "
                  "Re-run with --vacuum to hand the space back to the filesystem.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
