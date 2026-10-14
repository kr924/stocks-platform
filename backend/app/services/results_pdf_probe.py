"""
Read the first page of a filing's PDF to see whether it is the quarterly result.

Only asked of filings from companies on today's results calendar whose subject
and body say nothing useful — see `announcement_classifier.could_carry_result`.
The case that motivated it: on 14 Oct 2026 Elecon filed on BSE at 13:16:30 with
the subject "Board Meeting Outcome for Outcome Of Board Meeting" and the body
"Outcome of the board meeting is attached." Page one of that PDF reads "...
approved: 1. the Unaudited Financial Results (Standalone & Consolidated) ...".
NSE's copy, three minutes later, said "financial results" in its body, so that
is the one the platform caught.

Runs on its own small pool so a slow exchange download never holds up the
2-second announcement cycle. Outcome PDFs bundle the full results, so they are
large (Elecon's was 11 MB, 20 pages) — only page one is read, but the whole
file has to arrive first.
"""
import io
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple

import requests

logger = logging.getLogger("app.results_pdf_probe")

# Two downloads at once: enough that one slow file does not queue the rest of a
# results afternoon behind it, few enough not to starve the 2-core VM.
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pdf-probe")

_MAX_PDF_BYTES = 40 * 1024 * 1024
_CONNECT_TIMEOUT = 5
_READ_TIMEOUT = 15
# Past this the other exchange has usually published a wordier copy anyway.
_TOTAL_BUDGET_SECONDS = 45
# Below this a page has no usable text layer and is OCR'd instead.
_MIN_TEXT_CHARS = 80

_seen_lock = threading.Lock()
_seen: "OrderedDict[str, float]" = OrderedDict()
_SEEN_MAX = 5000

_stats = {"queued": 0, "confirmed": 0, "rejected": 0, "failed": 0, "last": None}


def get_probe_stats() -> dict:
    return dict(_stats)


def _claim(key: str) -> bool:
    """One probe per filing per process."""
    with _seen_lock:
        if key in _seen:
            return False
        _seen[key] = time.time()
        while len(_seen) > _SEEN_MAX:
            _seen.popitem(last=False)
        return True


def _headers_for(url: str) -> dict:
    h = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
        "Accept": "application/pdf,*/*",
    }
    if "bseindia.com" in url:
        h["Referer"] = "https://www.bseindia.com/"
    elif "nseindia.com" in url:
        h["Referer"] = "https://www.nseindia.com/"
    return h


def _download(url: str) -> Optional[bytes]:
    started = time.monotonic()
    try:
        with requests.get(url, headers=_headers_for(url), stream=True,
                          timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT)) as r:
            r.raise_for_status()
            buf = io.BytesIO()
            for chunk in r.iter_content(256 * 1024):
                buf.write(chunk)
                if buf.tell() > _MAX_PDF_BYTES:
                    logger.info(f"PDF probe: {url} exceeds {_MAX_PDF_BYTES // 1048576} MB, skipped")
                    return None
                if time.monotonic() - started > _TOTAL_BUDGET_SECONDS:
                    logger.info(f"PDF probe: {url} took over {_TOTAL_BUDGET_SECONDS}s, abandoned")
                    return None
            data = buf.getvalue()
            return data if data[:5] == b"%PDF-" or b"%PDF-" in data[:1024] else None
    except Exception as e:
        logger.info(f"PDF probe: download failed for {url}: {e}")
        return None


def _ocr_first_page(doc) -> str:
    """OCR page one with Tesseract, the fast engine the extractor already ships."""
    try:
        from app.services.pdf_extract import ocr_words
        got = ocr_words.get_engine("tesseract")
        if not got:
            return ""
        eng, _ = got
        from PIL import Image
        pix = doc[0].get_pixmap(dpi=150)
        return eng.image_to_string(Image.open(io.BytesIO(pix.tobytes("png")))) or ""
    except Exception as e:
        logger.debug(f"PDF probe: OCR of page one failed: {e}")
        return ""


def first_page_text(data: bytes) -> Tuple[str, str]:
    """Text of page one, and how it was read: text_layer | ocr | none."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        fitz = None

    if fitz is not None:
        try:
            doc = fitz.open(stream=data, filetype="pdf")
            try:
                if doc.page_count < 1:
                    return "", "none"
                text = doc[0].get_text() or ""
                if len(text.strip()) >= _MIN_TEXT_CHARS:
                    return text, "text_layer"
                ocr = _ocr_first_page(doc)
                if ocr.strip():
                    return ocr, "ocr"
                return text, "text_layer" if text.strip() else "none"
            finally:
                doc.close()
        except Exception as e:
            logger.debug(f"PDF probe: PyMuPDF could not read the file: {e}")

    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        text = reader.pages[0].extract_text() if reader.pages else ""
        return text or "", "text_layer" if text else "none"
    except Exception as e:
        logger.debug(f"PDF probe: pypdf could not read the file: {e}")
        return "", "none"


def schedule_probe(ann, event_id: int) -> bool:
    """Queue a first-page read for this filing. False if already probed or no PDF."""
    url = (getattr(ann, "url", "") or "").strip()
    if not url.lower().startswith(("http://", "https://")) or not event_id:
        return False
    if not _claim(f"{ann.exchange}:{url}"):
        return False
    _stats["queued"] += 1
    _pool.submit(_probe_job, ann, event_id, time.monotonic())
    logger.info(f"🔎 [PDF PROBE] {ann.symbol} on {ann.exchange.upper()} reports today and filed "
                f"'{(ann.title or '')[:60]}' — reading page one")
    return True


def _probe_job(ann, event_id: int, queued_at: float):
    from app.database import SessionLocal, MarketEvent
    from app.services.announcement_classifier import (
        CHANNEL_BOARD_OUTCOME, pdf_text_confirms_result,
    )

    data = _download(ann.url)
    if not data:
        _stats["failed"] += 1
        return
    text, how = first_page_text(data)
    took = time.monotonic() - queued_at
    _stats["last"] = {"symbol": ann.symbol, "exchange": ann.exchange, "how": how,
                      "seconds": round(took, 1), "bytes": len(data)}

    if not pdf_text_confirms_result(text):
        _stats["rejected"] += 1
        logger.info(f"[PDF PROBE] {ann.symbol} ({ann.exchange.upper()}): page one is not a "
                    f"result ({how}, {took:.1f}s)")
        return

    _stats["confirmed"] += 1
    db = SessionLocal()
    try:
        event = db.query(MarketEvent).filter(MarketEvent.id == event_id).first()
        if event is None:
            return
        # The page itself becomes the body the result was judged on, so the
        # order prompt and the AI see the evidence, not "outcome attached".
        excerpt = " ".join((text or "").split())[:1500]
        ann.description = f"{ann.description or ''}\n[Page 1 of filing] {excerpt}".strip()

        from app.services.exchange_hub import promote_to_result
        captured = promote_to_result(db, ann, event, CHANNEL_BOARD_OUTCOME,
                                     evidence=f"pdf_first_page_{how}")
        if captured:
            # Stored only once it is the routed result. Written onto a filing
            # that lost the race, the exchange's own body would differ from it
            # on every cycle and the hub would keep "refreshing" it back.
            try:
                event.description = ann.description
                db.commit()
            except Exception:
                db.rollback()
        logger.info(
            f"[PDF PROBE] {ann.symbol} ({ann.exchange.upper()}): page one confirms results "
            f"in {took:.1f}s — {'captured first' if captured else 'already captured by another filing'}"
        )
    except Exception as e:
        db.rollback()
        logger.error(f"[PDF PROBE] routing failed for {ann.symbol}: {e}")
    finally:
        db.close()
