"""
Weekly data retention.

The platform ingests ~1,700 announcements a day and keeps every one. After a
month that was 51,000 rows and 80 MB of a 140 MB database, almost all of it raw
feed JSON nobody reads again. This bounds it.

Two tiers, because "old" means different things to different rows:

    30 days   anything the trading path or the earnings calendar still reads
    5 days    raw news — the intelligence feed's own ingest, read once and
              superseded by the next poll

The 30-day tier is not a preference, it is a dependency. `_already_prompted_this_quarter`
answers "has this company already reported this season" from `market_events`
over 30 days, precisely because the prompts themselves are purged at 72h and
cannot answer it. Delete a `financial_results` row inside that window and the
guard silently forgets the company reported, so its next filing — the results
PDF, the newspaper advertisement — raises a second order prompt for one earnings
event. That is the duplicate-prompt bug, reintroduced from the other end.

Board meetings are protected for the same class of reason. The earnings calendar
reads the meeting date out of `raw_data`, and for BSE out of prose, to show
*upcoming* meetings. A meeting announced two weeks ahead would vanish from the
calendar before it happened if its row were dropped at 5 days.

So `raw_data` is never stripped from a retained row either — it is the only
place a BSE board-meeting date exists.

What is left over is genuinely disposable: compliance notices, investor-relations
posts, management changes, and the long tail classified `general`. 34,644 rows
and 42 MB of the sample this was written against.

Deletes free pages for SQLite to reuse, which stops the file growing, but do not
return space to the filesystem. `ops/cleanup_old_data.py --vacuum` does that, as
a deliberate manual step: VACUUM takes an exclusive lock and rewrites the whole
database, which is not something to do from a background thread while the
announcement path is polling.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import or_, and_, text
from sqlalchemy.orm import Session

from app.database import (
    AIAlert, CompanyFiling, MarketEvent, NewsItem, NewsStory,
    ResultDedupKey, TradeAILog,
)

logger = logging.getLogger("app.retention")

# Rows the trading path and the earnings calendar still read.
TRADING_RETENTION_DAYS = 30
# Raw intelligence-feed news.
NEWS_RETENTION_DAYS = 5

SETTING_LAST_RUN = "retention_last_run"


def _protected_market_events():
    """
    The `market_events` rows that survive the 5-day sweep.

    `financial_results` and `impact_news` are the auto-trading path's own rows.
    `board_meeting` and `earnings`, plus BSE rows whose SUBCATNAME says Board
    Meeting, are what the earnings calendar is built from — BSE has no
    board-meeting API we can reach, so the announcement feed is the only source
    and the date lives in `raw_data`.
    """
    return or_(
        MarketEvent.category.in_(
            ["financial_results", "impact_news", "board_meeting", "earnings"]),
        MarketEvent.event_type == "board_meeting",
        and_(MarketEvent.source == "bse",
             MarketEvent.raw_data.like('%"SUBCATNAME": "Board Meeting%')),
    )


def _cutoff(days: int) -> datetime:
    return datetime.utcnow() - timedelta(days=days)


def run_retention(db: Session, dry_run: bool = False) -> dict:
    """
    Apply both tiers. Returns a count per rule.

    Each rule commits on its own. A sweep that fails halfway has still done real
    work, and re-running it is harmless — every rule is defined by an age, not by
    a cursor, so the second run simply finds less to do.
    """
    news_cut = _cutoff(NEWS_RETENTION_DAYS)
    trade_cut = _cutoff(TRADING_RETENTION_DAYS)
    counts = {}

    def sweep(label, query):
        try:
            if dry_run:
                counts[label] = query.count()
                return
            n = query.delete(synchronize_session=False)
            db.commit()
            counts[label] = n
            if n:
                logger.info(f"[RETENTION] {label}: {n} rows")
        except Exception as e:
            db.rollback()
            counts[label] = f"failed: {e}"
            logger.error(f"[RETENTION] {label} failed: {e}")

    # ── 5-day tier: raw news ──
    sweep("market_events (news)",
          db.query(MarketEvent).filter(
              MarketEvent.created_at < news_cut,
              ~_protected_market_events(),
          ))
    # NewsItem is dated by publication, not ingest: an article fetched today but
    # published a fortnight ago is old news, and that is what the existing
    # aggregator purge keys on too.
    sweep("news_items",
          db.query(NewsItem).filter(NewsItem.published_at < news_cut))
    sweep("ai_alerts",
          db.query(AIAlert).filter(AIAlert.created_at < news_cut))
    sweep("company_filings",
          db.query(CompanyFiling).filter(CompanyFiling.created_at < news_cut))

    # ── 30-day tier: the trading path ──
    sweep("market_events (trading)",
          db.query(MarketEvent).filter(
              MarketEvent.created_at < trade_cut,
              _protected_market_events(),
          ))
    sweep("result_dedup_keys",
          db.query(ResultDedupKey).filter(ResultDedupKey.created_at < trade_cut))
    # A log carrying a config_id is the only trail from a filing to a position
    # that may still be open, so it is kept regardless of age — the same rule
    # `purge_old_pending` applies to the prompts.
    sweep("trade_ai_logs (unlinked)",
          db.query(TradeAILog).filter(
              TradeAILog.created_at < trade_cut,
              TradeAILog.config_id.is_(None),
          ))

    # ── Stories left with no articles ──
    # Done last: the sweeps above are what orphan them.
    try:
        q = db.query(NewsStory).filter(
            ~NewsStory.id.in_(
                db.query(NewsItem.story_id).filter(NewsItem.story_id.isnot(None)).distinct()
            )
        )
        if dry_run:
            counts["news_stories (orphaned)"] = q.count()
        else:
            n = q.delete(synchronize_session="fetch")
            db.commit()
            counts["news_stories (orphaned)"] = n
            if n:
                logger.info(f"[RETENTION] news_stories (orphaned): {n} rows")
    except Exception as e:
        db.rollback()
        counts["news_stories (orphaned)"] = f"failed: {e}"
        logger.error(f"[RETENTION] orphaned stories failed: {e}")

    if not dry_run:
        try:
            from app.services.registry_builder import _set_setting
            _set_setting(db, SETTING_LAST_RUN, datetime.utcnow().strftime("%Y-%m-%d"))
            db.commit()
        except Exception:
            db.rollback()
        total = sum(v for v in counts.values() if isinstance(v, int))
        logger.info(f"[RETENTION] sweep complete: {total} rows removed.")

    return counts


def database_size_mb(db: Session) -> float:
    """Size of the SQLite file, as the database itself reports it."""
    try:
        row = db.execute(text(
            "select page_count * page_size from pragma_page_count(), pragma_page_size()"
        )).fetchone()
        return round(row[0] / 1e6, 1) if row else 0.0
    except Exception:
        return 0.0


def vacuum(db: Session) -> None:
    """
    Return freed pages to the filesystem.

    Separate from the sweep and never called from the scheduler: VACUUM takes an
    exclusive lock and rewrites the entire database, needing free disk equal to
    its size. Deleting rows alone already stops the file growing, because SQLite
    reuses the pages; this is only for actually shrinking it.
    """
    db.commit()
    db.execute(text("VACUUM"))
    db.commit()
