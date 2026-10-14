"""
HTTP to BSE that BSE will answer.

From 23 Sep 2026 every request from the VM to api.bseindia.com came back
403 "Access Denied" (Akamai) — the announcements feed, the results calendar,
the scrip list — and the BSE half of the platform went silent for three weeks
while NSE kept flowing. Browser headers did not help: `curl` with a full
Chrome User-Agent, Referer and Origin was still 403. The same request through
curl_cffi impersonating Chrome's TLS handshake was 200. Akamai is reading the
TLS / HTTP2 fingerprint, and python-requests' fingerprint is on its list.

So every BSE call goes through here. When curl_cffi is installed the session
impersonates Chrome and the User-Agent is left to the impersonation — a
hand-set UA that disagrees with the handshake is itself a bot signal. Without
curl_cffi it falls back to plain requests, which is what failed, so the
fallback exists only to keep the app importable.

The Cloudflare-worker proxy in BSESession does not help either: it forwards to
the retired AnnGetData endpoint whatever path it is given, and BSE denies that
too.
"""
import logging
from typing import Optional

logger = logging.getLogger("app.bse_http")

try:
    from curl_cffi import requests as _cffi_requests
    HAVE_CFFI = True
except Exception:  # pragma: no cover - only when the dependency is missing
    _cffi_requests = None
    HAVE_CFFI = False
    logger.warning("curl_cffi is not installed: BSE requests will use python-requests "
                   "and are likely to be refused (403). Add curl_cffi to the image.")

import requests as _requests

IMPERSONATE = "chrome"


def is_bse_url(url: str) -> bool:
    return "bseindia.com" in (url or "").lower()


def _clean(headers: Optional[dict]) -> dict:
    h = dict(headers or {})
    if HAVE_CFFI:
        for k in list(h):
            if k.lower() == "user-agent":
                h.pop(k)
    return h


def new_session(headers: Optional[dict] = None):
    """A session BSE accepts. Same .get/.headers interface as requests.Session."""
    if HAVE_CFFI:
        s = _cffi_requests.Session(impersonate=IMPERSONATE)
    else:
        s = _requests.Session()
    s.headers.update(_clean(headers))
    return s


def get(url: str, headers: Optional[dict] = None, params: Optional[dict] = None, timeout: float = 15):
    """One-shot GET to BSE."""
    if HAVE_CFFI:
        return _cffi_requests.get(url, headers=_clean(headers), params=params,
                                  timeout=timeout, impersonate=IMPERSONATE)
    return _requests.get(url, headers=headers, params=params, timeout=timeout)


def download(url: str, max_bytes: int, timeout: float = 45) -> Optional[bytes]:
    """
    Fetch a BSE attachment (a filing PDF). None on any failure or if larger
    than max_bytes. Not streamed: the size cap is checked after the fact,
    which is acceptable for filings that are tens of megabytes at most.
    """
    try:
        r = get(url, headers={"Referer": "https://www.bseindia.com/",
                              "Accept": "application/pdf,*/*"}, timeout=timeout)
        r.raise_for_status()
        data = r.content
        if len(data) > max_bytes:
            logger.info(f"BSE attachment over {max_bytes // 1048576} MB, skipped: {url}")
            return None
        return data
    except Exception as e:
        logger.info(f"BSE download failed for {url}: {e}")
        return None
