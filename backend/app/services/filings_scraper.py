"""
Company Filings Scraper — Quarterly Results, Investor Presentations,
Conference Call Transcripts, Annual Reports.

Fetches filing metadata from NSE APIs and Google News RSS,
extracts text from PDFs for AI analysis.
"""
import hashlib
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.database import CompanyFiling
from app.services.intel_config import get_intel_config
from app.services.deduplication import url_hash
from app.services.news_aggregator import fetch_google_news_rss

logger = logging.getLogger("app.filings_scraper")

# Try to import PyPDF2 for PDF extraction
try:
    from PyPDF2 import PdfReader
    import io
    HAS_PYPDF2 = True
except ImportError:
    HAS_PYPDF2 = False
    logger.info("PyPDF2 not installed. PDF text extraction will be disabled.")


def _extract_pdf_text(url: str, max_pages: int = 20, max_size_mb: int = 50) -> Optional[str]:
    """
    Download and extract text from a PDF URL.
    Returns extracted text or None on failure.
    """
    if not HAS_PYPDF2 or not url:
        return None
    
    import requests
    try:
        # Stream the PDF to check size before downloading
        resp = requests.get(url, stream=True, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        resp.raise_for_status()
        
        # Check content length
        content_length = int(resp.headers.get("Content-Length", 0))
        if content_length > max_size_mb * 1024 * 1024:
            logger.warning(f"PDF too large ({content_length / 1024 / 1024:.1f} MB): {url}")
            return None
        
        pdf_bytes = resp.content
        if len(pdf_bytes) > max_size_mb * 1024 * 1024:
            return None
        
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages_to_read = min(len(reader.pages), max_pages)
        
        text_parts = []
        for i in range(pages_to_read):
            try:
                page_text = reader.pages[i].extract_text()
                if page_text:
                    text_parts.append(page_text.strip())
            except Exception:
                continue
        
        full_text = "\n\n".join(text_parts)
        # Limit total text length
        return full_text[:50000] if full_text else None
        
    except Exception as e:
        logger.error(f"PDF extraction error for {url}: {e}")
        return None


def _is_duplicate_filing(db: Session, filing_url: str) -> bool:
    """Check if a filing with the same URL hash already exists."""
    if not filing_url:
        return False
    h = url_hash(filing_url)
    existing = db.query(CompanyFiling.id).filter(CompanyFiling.url_hash == h).first()
    return existing is not None


# ─── NSE Financial Results ────────────────────────────────────────────────

def fetch_quarterly_results(db: Session) -> int:
    """Fetch quarterly/annual financial results from NSE."""
    config = get_intel_config()
    if not config.is_source_enabled("filings", "quarterly_results"):
        return 0
    
    from app.services.nse_bse_scraper import _get_nse_session, _safe_datetime
    nse = _get_nse_session()
    
    nse_config = config.nse_bse.get("sources", {}).get("financial_results", {})
    url = nse_config.get("nse_url", "https://www.nseindia.com/api/corporates-financial-results?index=equities")
    
    data = nse.get(url)
    if not data:
        return 0
    
    results = data if isinstance(data, list) else data.get("data", data.get("results", []))
    if not isinstance(results, list):
        return 0
    
    pdf_config = config.filings.get("pdf", {})
    max_pages = pdf_config.get("max_pages", 20)
    max_size_mb = pdf_config.get("max_file_size_mb", 50)
    do_pdf = pdf_config.get("enabled", True)
    
    count = 0
    for result in results:
        try:
            symbol = result.get("symbol", result.get("sm_name", "")).strip().upper()
            period = result.get("period", result.get("re_forperiod", ""))
            broadcat_dt = result.get("broadcastDt", result.get("date", ""))
            xbrl_link = result.get("xbrl", result.get("re_xbrl", ""))
            
            if not symbol:
                continue
            
            filing_url = xbrl_link or ""
            if filing_url and _is_duplicate_filing(db, filing_url):
                continue
            
            # Even if no URL, check by symbol + period
            if not filing_url:
                existing = db.query(CompanyFiling.id).filter(
                    CompanyFiling.symbol == symbol,
                    CompanyFiling.period == period,
                    CompanyFiling.filing_type == "quarterly_result"
                ).first()
                if existing:
                    continue
            
            title = f"{symbol} — Financial Results for {period}"
            filed_at = _safe_datetime(broadcat_dt)
            
            # Extract PDF text if available
            extracted_text = None
            if do_pdf and filing_url and filing_url.lower().endswith(".pdf"):
                extracted_text = _extract_pdf_text(filing_url, max_pages, max_size_mb)
            
            filing = CompanyFiling(
                filing_type="quarterly_result",
                symbol=symbol,
                title=title,
                url=filing_url or None,
                url_hash=url_hash(filing_url) if filing_url else hashlib.sha256(f"{symbol}_{period}_qr".encode()).hexdigest(),
                extracted_text=extracted_text,
                period=period,
                filed_at=filed_at,
            )
            db.add(filing)
            count += 1
        except Exception as e:
            logger.error(f"Error processing quarterly result: {e}")
            continue
    
    if count > 0:
        db.commit()
        logger.info(f"Saved {count} new quarterly results")
    return count


# ─── Conference Call Transcripts (via Google News RSS) ─────────────────────

def fetch_conference_transcripts(db: Session) -> int:
    """Fetch conference call transcript links via Google News RSS."""
    config = get_intel_config()
    if not config.is_source_enabled("filings", "conference_calls"):
        return 0
    
    cc_config = config.filings.get("sources", {}).get("conference_calls", {})
    query = cc_config.get("query", "conference call transcript India stock")
    
    articles = fetch_google_news_rss(query, max_articles=15)
    
    count = 0
    for art in articles:
        try:
            article_url = art.get("url", "")
            headline = art.get("headline", "").strip()
            if not article_url or not headline:
                continue
            
            if _is_duplicate_filing(db, article_url):
                continue
            
            from app.services.deduplication import extract_primary_symbol
            symbol = extract_primary_symbol(headline) or "UNKNOWN"
            
            pub_time = art.get("published_at", datetime.utcnow())
            if isinstance(pub_time, str):
                try:
                    pub_time = datetime.fromisoformat(pub_time.replace("Z", "+00:00"))
                except Exception:
                    pub_time = datetime.utcnow()
            
            filing = CompanyFiling(
                filing_type="transcript",
                symbol=symbol,
                title=headline[:500],
                url=article_url,
                url_hash=url_hash(article_url),
                filed_at=pub_time,
            )
            db.add(filing)
            count += 1
        except Exception as e:
            logger.error(f"Error processing transcript: {e}")
            continue
    
    if count > 0:
        db.commit()
        logger.info(f"Saved {count} new conference call transcript links")
    return count


# ─── Investor Presentations (via BSE announcements filter) ─────────────────

def fetch_investor_presentations(db: Session) -> int:
    """Fetch investor presentation links via Google News RSS."""
    config = get_intel_config()
    if not config.is_source_enabled("filings", "investor_presentations"):
        return 0
    
    # Search for investor presentations
    articles = fetch_google_news_rss("investor presentation NSE BSE India company", max_articles=10)
    
    count = 0
    for art in articles:
        try:
            article_url = art.get("url", "")
            headline = art.get("headline", "").strip()
            if not article_url or not headline:
                continue
            
            if _is_duplicate_filing(db, article_url):
                continue
            
            from app.services.deduplication import extract_primary_symbol
            symbol = extract_primary_symbol(headline) or "UNKNOWN"
            
            pub_time = art.get("published_at", datetime.utcnow())
            if isinstance(pub_time, str):
                try:
                    pub_time = datetime.fromisoformat(pub_time.replace("Z", "+00:00"))
                except Exception:
                    pub_time = datetime.utcnow()
            
            filing = CompanyFiling(
                filing_type="investor_presentation",
                symbol=symbol,
                title=headline[:500],
                url=article_url,
                url_hash=url_hash(article_url),
                filed_at=pub_time,
            )
            db.add(filing)
            count += 1
        except Exception as e:
            logger.error(f"Error processing investor presentation: {e}")
            continue
    
    if count > 0:
        db.commit()
        logger.info(f"Saved {count} new investor presentation links")
    return count


# ─── Aggregate Fetch ───────────────────────────────────────────────────────

def fetch_all_filings(db: Session) -> Dict[str, int]:
    """
    Run all filing scrapers. Returns dict of {type: new_count}.
    Called by the background scheduler.
    """
    config = get_intel_config()
    if not config.filings.get("enabled", True):
        return {}
    
    results = {}
    
    try:
        results["quarterly_results"] = fetch_quarterly_results(db)
    except Exception as e:
        logger.error(f"Quarterly results fetch failed: {e}")
        results["quarterly_results"] = 0
    
    try:
        results["conference_transcripts"] = fetch_conference_transcripts(db)
    except Exception as e:
        logger.error(f"Conference transcripts fetch failed: {e}")
        results["conference_transcripts"] = 0
    
    try:
        results["investor_presentations"] = fetch_investor_presentations(db)
    except Exception as e:
        logger.error(f"Investor presentations fetch failed: {e}")
        results["investor_presentations"] = 0
    
    total = sum(results.values())
    if total > 0:
        logger.info(f"Filings scrape complete: {total} new filings — {results}")
    
    return results
