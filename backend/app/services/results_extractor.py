"""
Read a filed results PDF and say how much of it to believe.

The pipeline this comes from settled on a shape worth keeping: **Screener is the
spine, the PDF is the check.** Screener carries every quarter already normalised
to ₹ crore, so the platform's figures come from there. The filing is then read
independently — different source, no shared code — and agreement between the two
is evidence. One source repeated is not.

That is also why the confidence here is not a number invented for the occasion.
It comes from two things the extraction actually knows:

  1. the extractor's own validation flags — EBITDA derived two ways must agree,
     total income must equal revenue plus other income, PAT cannot exceed PBT,
     revenue cannot be negative, the unit must be stated rather than assumed;
  2. whether each figure matches Screener's published one.

Measured over 1,926 filings in the reference run: a clean read is right about
94% of the time (median error 0.01%), a flagged read about 67%. Those two
numbers are the priors the tiers below are pinned to, so a percentage on screen
means something that was counted rather than felt.

One rule worth knowing before reading a disagreement: **EBITDA always spreads.**
Screener's Operating Profit is its own construct, not a P&L-derived EBITDA, so a
gap there is not a correctness signal and is excluded from the score. Revenue,
PBT and PAT are the anchors.
"""
import json
import logging
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import requests

logger = logging.getLogger("app.results_extractor")

# One at a time, like the earnings AI. OCR is CPU-bound and each RapidOCR
# worker loads its own ONNX session at 400-800 MB; two of them on a 2-core box
# would starve the announcement path, which is the one thing here that is
# actually time-critical.
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdf-extract")

# Metrics scored against Screener. EBITDA is deliberately absent — see above.
# Revenue and PAT are the anchors the reference pipeline verifies on.
_SCORED = ("revenue", "pat", "pbt", "expenses", "other_income")
_ANCHORS = ("revenue", "pat")

# A figure within this of Screener's is treated as the same number. The
# reference run measured a median error of 0.01% on clean reads, so 2% is
# generous — it is set to absorb rounding and presentation differences, not to
# flatter the extractor.
_MATCH_TOL_PCT = 2.0

# Below this (₹ crore) a percentage comparison stops discriminating: two figures
# that differ by a few lakh rupees can be 50% apart.
_MIN_BASE = 1.0

_DOWNLOAD_TIMEOUT = 45
_MAX_PDF_BYTES = 40 * 1024 * 1024


class ExtractionUnavailable(RuntimeError):
    """The extractor's dependencies are not installed in this image."""


# What each engine needs beyond its own package to actually return words.
# ocr_words imports these lazily, inside the per-page call, where the failure is
# caught and the page skipped — so an engine can load, report itself available,
# and then produce nothing on every page it is given.
_ENGINE_DEPS = {
    "tesseract": ("pytesseract", "pandas", "PIL"),
    "rapidocr": ("rapidocr_onnxruntime",),
}


def available() -> dict:
    """
    What this deployment can actually do, for the UI to say so honestly.

    An engine counts as available only when the modules its word-extraction
    path imports are all present. Loading is not enough: pytesseract returns its
    boxes as a DataFrame, so without pandas the engine loads cleanly and then
    fails on every page — and because that failure is swallowed per page, the
    only visible symptom was scanned filings reporting "no OCR engine reached
    it" while the capability list showed two engines ready.
    """
    import importlib

    out = {"text_layer": False, "ocr_engines": [], "degraded": [], "error": ""}
    try:
        import fitz  # noqa: F401
        out["text_layer"] = True
    except Exception as e:
        out["error"] = f"PyMuPDF not installed ({e})"
        return out
    try:
        from app.services.pdf_extract import ocr_words
        loaded = list(ocr_words.available())
    except Exception as e:
        logger.debug(f"OCR engines unavailable: {e}")
        return out

    for name in loaded:
        missing = [m for m in _ENGINE_DEPS.get(name, ())
                   if not importlib.util.find_spec(m)]
        if missing:
            out["degraded"].append({"engine": name, "missing": missing})
            logger.warning(
                f"OCR engine '{name}' loads but cannot read a page: missing {missing}")
        else:
            out["ocr_engines"].append(name)
    return out


def _download(url: str) -> Optional[str]:
    """Fetch the filing to a temp file, or None. Never raises."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    try:
        with requests.get(url, timeout=_DOWNLOAD_TIMEOUT, stream=True, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        }) as r:
            r.raise_for_status()
            fd, path = tempfile.mkstemp(suffix=".pdf", prefix="filing_")
            size = 0
            with os.fdopen(fd, "wb") as f:
                for chunk in r.iter_content(65536):
                    size += len(chunk)
                    if size > _MAX_PDF_BYTES:
                        # A results PDF is single-digit megabytes. Anything this
                        # large is a bundle we cannot use and would spend
                        # minutes of OCR discovering that.
                        f.close()
                        os.unlink(path)
                        logger.warning(f"Filing exceeded {_MAX_PDF_BYTES // 1048576}MB, not extracted: {url}")
                        return None
                    f.write(chunk)
            return path
    except Exception as e:
        logger.warning(f"Could not download filing {url}: {e}")
        return None


def _pct_gap(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """Signed difference of `a` from `b`, as a percentage of `b`."""
    if a is None or b is None or abs(b) < _MIN_BASE:
        return None
    return round((a - b) / abs(b) * 100, 2)


def _confidence(row: dict, comparisons: list) -> dict:
    """
    Turn the extractor's verdict and the Screener agreement into one figure.

    The tiers are the reference pipeline's, and the percentages are the
    accuracy actually measured for each: a clean read that a second source
    confirms is a different claim from a flagged read that nothing corroborates,
    and they should not both be called "extracted".
    """
    flags = list(row.get("flags") or [])
    status = row.get("status") or "FAIL"
    clean = status == "OK"

    scored = [c for c in comparisons if c["gap_pct"] is not None]
    agree = [c for c in scored if abs(c["gap_pct"]) <= _MATCH_TOL_PCT]
    anchors = [c for c in scored if c["metric"] in _ANCHORS]
    anchors_agree = [c for c in anchors if abs(c["gap_pct"]) <= _MATCH_TOL_PCT]
    corroborated = len(anchors) >= 1 and len(anchors_agree) == len(anchors)

    if status in ("NEEDS_OCR", "INVALID_PDF", "FAIL"):
        tier, pct, why = "NOT_READ", None, {
            "NEEDS_OCR": "The filing is a scan with no text to read and no OCR engine reached it.",
            "INVALID_PDF": "The download is not a results filing — the exchange served a "
                           "'page has moved' notice, which saves as a valid PDF.",
        }.get(status, "The statement page could not be located in this filing.")
    elif not scored:
        # Read, but nothing to check it against. The extractor's own verdict is
        # all there is, and on its own it is worth about two thirds.
        tier = "UNVERIFIED" if clean else "UNVERIFIED_FLAGGED"
        pct = 70 if clean else 40
        why = ("Read cleanly, but Screener has no figures for this quarter to check it against."
               if clean else
               "Read with validation warnings and nothing to check it against.")
    elif corroborated and clean:
        tier, pct = "VERIFIED", 95
        why = "Read cleanly and agrees with Screener on revenue and PAT."
    elif corroborated:
        tier, pct = "VERIFIED_WEAK", 80
        why = "Agrees with Screener on revenue and PAT, but the extractor raised a warning."
    elif clean:
        tier, pct = "CHECK", 50
        why = "Read cleanly but disagrees with Screener. Worth a look before acting on it."
    else:
        tier, pct = "FLAGGED", 25
        why = "Flagged by the extractor and disagrees with Screener."

    # A standalone filing against consolidated Screener figures is a real
    # difference, not an error, and must not read as one.
    basis = (row.get("basis") or "").lower()
    if tier in ("CHECK", "FLAGGED") and basis == "standalone":
        why += " The filing is standalone where Screener is consolidated, which explains a gap on its own."

    return {
        "tier": tier,
        "pct": pct,
        "reason": why,
        "clean": clean,
        "status": status,
        "flags": flags,
        "metrics_agreeing": len(agree),
        "metrics_compared": len(scored),
    }


def run_extraction_async(pending_id: int) -> bool:
    """
    Read one filing in the background and store the result on its row.

    Not synchronous, because the honest worst case is not a click's worth of
    waiting. A text-layer read is a tenth of a second, but a filing whose
    statement page has to be *found* by OCR costs about a minute — the engine
    reads pages until it recognises a table. Blocking a request for that long
    ties up a worker on a 2-core box and times out in front of the user anyway.

    The panel already polls every few seconds, so the result appears the same
    way an AI verdict does.
    """
    _pool.submit(_extract_job, pending_id)
    return True


def _extract_job(pending_id: int) -> None:
    from app.database import SessionLocal, PendingResultOrder

    db = SessionLocal()
    try:
        pending = db.query(PendingResultOrder).filter(
            PendingResultOrder.id == pending_id).first()
        if not pending:
            return
        screener = None
        if getattr(pending, "screener_json", None):
            try:
                screener = json.loads(pending.screener_json)
            except Exception:
                screener = None
        result = extract_from_filing(pending.attachment_url, pending.symbol,
                                     screener=screener)
        pending = db.query(PendingResultOrder).filter(
            PendingResultOrder.id == pending_id).first()
        if pending:
            pending.extraction_json = json.dumps(result, default=str)
            db.commit()
            logger.info(f"[EXTRACT] {pending.symbol}: {result['confidence']['tier']} "
                        f"({result['confidence']['pct']}%)")
    except Exception as e:
        db.rollback()
        logger.error(f"[EXTRACT] job failed for #{pending_id}: {e}")
        try:
            from app.database import PendingResultOrder as _P
            row = db.query(_P).filter(_P.id == pending_id).first()
            if row:
                row.extraction_json = json.dumps({
                    "ok": False, "error": str(e)[:200],
                    "confidence": {"tier": "NOT_READ", "pct": None, "flags": [],
                                   "reason": f"The extractor errored: {str(e)[:120]}"},
                })
                db.commit()
        except Exception:
            db.rollback()
    finally:
        db.close()


def extract_from_filing(attachment_url: str, symbol: str,
                        use_ocr: bool = True,
                        screener: Optional[dict] = None) -> dict:
    """
    Run the cascade over one filing and score it against Screener.

    Never raises: a filing that cannot be read is a result in itself, and the
    caller is a background worker whose failure would otherwise be invisible.
    """
    caps = available()
    if not caps["text_layer"]:
        return {"ok": False, "error": caps["error"] or "extractor unavailable",
                "confidence": {"tier": "UNAVAILABLE", "pct": None,
                               "reason": caps["error"], "flags": []}}

    path = _download(attachment_url)
    if not path:
        return {"ok": False, "error": "could not download the filing",
                "confidence": {"tier": "NOT_READ", "pct": None,
                               "reason": "The filing could not be downloaded.", "flags": []}}

    try:
        from app.services.pdf_extract.quarterly_extract import extract as _extract
        row = _extract(path, symbol=symbol, ocr_fallback=bool(use_ocr and caps["ocr_engines"]))
    except Exception as e:
        logger.error(f"[EXTRACT] {symbol} failed: {e}")
        return {"ok": False, "error": str(e)[:200],
                "confidence": {"tier": "NOT_READ", "pct": None,
                               "reason": f"The extractor errored: {str(e)[:120]}", "flags": []}}
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass

    # Screener's published figures, to check the reading against. The caller
    # normally has them already — the panel stores them on the filing row — and
    # passing them in avoids re-fetching a quarter that cannot have changed.
    #
    # Without them, fetch live rather than cache-only. This runs because someone
    # asked for the check, and a check with nothing to check against is the one
    # outcome not worth returning; one company on demand is well inside the
    # pacing Screener expects.
    if screener is None:
        screener = {}
        try:
            from app.services.screener_quarters import fetch_latest_quarter
            screener = fetch_latest_quarter(symbol, cache_only=False)
        except Exception as e:
            logger.debug(f"[EXTRACT] Screener unavailable for {symbol}: {e}")
    screener = screener or {}

    s_metrics = (screener or {}).get("metrics") or {}
    comparisons = []
    for metric in _SCORED:
        pdf_v = row.get(metric)
        scr_v = (s_metrics.get(metric) or {}).get("current_qtr")
        gap = _pct_gap(pdf_v, scr_v)
        comparisons.append({
            "metric": metric,
            "pdf": pdf_v,
            "screener": scr_v,
            "gap_pct": gap,
            "agrees": None if gap is None else abs(gap) <= _MATCH_TOL_PCT,
        })
    # Reported but not scored — Screener's Operating Profit is not a P&L EBITDA.
    comparisons.append({
        "metric": "ebitda", "pdf": row.get("ebitda"),
        "screener": (s_metrics.get("ebitda") or {}).get("current_qtr"),
        "gap_pct": None, "agrees": None,
        "note": "not scored — Screener's Operating Profit is its own construct, "
                "not a P&L-derived EBITDA",
    })

    confidence = _confidence(row, comparisons)
    covers_latest = bool(row.get("quarter") and screener.get("quarter")
                         and row["quarter"].lower() == (screener["quarter"] or "").lower())
    if not covers_latest and row.get("quarter") and screener.get("quarter"):
        confidence["reason"] += (f" Note the filing covers {row['quarter']} where Screener's "
                                 f"latest is {screener['quarter']}.")

    return {
        "ok": row.get("status") in ("OK", "OK_WARN", "REVIEW"),
        "symbol": symbol.upper(),
        "engine": row.get("engine") or ("ocr" if any("via_ocr" in f for f in (row.get("flags") or []))
                                        else "text layer"),
        "page": row.get("page"),
        "basis": row.get("basis") or "",
        "unit": row.get("unit") or "",
        "quarter": row.get("quarter") or "",
        "qoq_quarter": row.get("qoq_quarter") or "",
        "yoy_quarter": row.get("yoy_quarter") or "",
        "covers_screener_quarter": covers_latest,
        "screener_quarter": screener.get("quarter") or "",
        "metrics": {m: {
            "current_qtr": row.get(m),
            "prev_qtr": row.get(m + "_prev_q"),
            "year_ago": row.get(m + "_year_ago"),
            "yoy_change_pct": row.get(m + "_yoy_pct"),
            "qoq_change_pct": row.get(m + "_qoq_pct"),
        } for m in ("revenue", "expenses", "other_income", "ebitda", "pbt", "pat")},
        "comparisons": comparisons,
        "confidence": confidence,
        "error": "",
    }
