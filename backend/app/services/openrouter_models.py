"""
OpenRouter's model catalogue, and which account a results analysis runs on.

Two OpenRouter accounts:

  premium  a funded key and a model the operator picks. A switch in the app
           turns it on or off.
  free     a key with no credit behind it and a ":free" model. Free models are
           rate limited (about 20 a minute, 50 a day without credit on the
           account) and many cannot read a PDF natively.

With premium ON, every results filing is analysed on the premium account
first, ahead of gemcall. If it fails, gemcall runs, then the free account.
With premium OFF, the premium key is never used: gemcall runs, then the free
account. Switching premium off is a decision not to spend, so it never falls
back onto the paid key.

A re-analysis that names a model is the operator's explicit choice. A ":free"
model runs on the free account. Anything else runs on the premium key whatever
the switch says, because the switch governs the default, not a model picked
by hand.

The catalogue comes from https://openrouter.ai/api/v1/models, which needs no
key, and is cached for six hours. It lets the settings screen offer what
exists today rather than a hardcoded list. It also tells the free account
whether a model reads PDFs itself: one that cannot is sent through
OpenRouter's free "pdf-text" parser rather than the paid OCR engine.
"""
import logging
import os
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger("app.openrouter_models")

MODELS_URL = "https://openrouter.ai/api/v1/models"
CACHE_SECONDS = 6 * 3600

DEFAULT_PREMIUM_MODEL = "google/gemini-2.5-flash-lite"

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "models": []}


def _price_per_million(value) -> Optional[float]:
    try:
        return round(float(value) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None


def is_free_model(model_id: str, pricing: Optional[dict] = None) -> bool:
    if (model_id or "").endswith(":free"):
        return True
    if pricing:
        return str(pricing.get("prompt")) == "0" and str(pricing.get("completion")) == "0"
    return False


def _shape(m: dict) -> Optional[dict]:
    mid = m.get("id")
    if not mid:
        return None
    pricing = m.get("pricing") or {}
    arch = m.get("architecture") or {}
    inputs = [str(x).lower() for x in (arch.get("input_modalities") or [])]
    return {
        "id": mid,
        "name": m.get("name") or mid,
        "free": is_free_model(mid, pricing),
        "reads_pdf": "file" in inputs,
        "context": m.get("context_length"),
        "prompt_per_m": _price_per_million(pricing.get("prompt")),
        "completion_per_m": _price_per_million(pricing.get("completion")),
    }


def list_models(force: bool = False) -> list:
    """Every OpenRouter model, cached. Empty list when OpenRouter is unreachable."""
    with _lock:
        fresh = time.time() - _cache["at"] < CACHE_SECONDS
        if fresh and _cache["models"] and not force:
            return _cache["models"]
    try:
        r = requests.get(MODELS_URL, timeout=10)
        r.raise_for_status()
        shaped = [s for s in (_shape(m) for m in r.json().get("data", [])) if s]
        if shaped:
            with _lock:
                _cache["at"] = time.time()
                _cache["models"] = shaped
            return shaped
    except Exception as e:
        logger.warning(f"OpenRouter model list unavailable: {e}")
    with _lock:
        return _cache["models"]   # a stale list beats none


def models_for(tier: str) -> list:
    """
    The models one account can run. Free: ":free" or zero-priced models.
    Premium: everything else, cheapest first. Models that read PDFs natively
    come first in both lists, because that is what a results filing needs.
    """
    want_free = tier == "free"
    rows = [m for m in list_models() if m["free"] == want_free]
    rows.sort(key=lambda m: (not m["reads_pdf"], m["prompt_per_m"] or 0, m["name"].lower()))
    return rows


def reads_pdf(model_id: str) -> Optional[bool]:
    """True/False from the catalogue; None when the catalogue is unavailable."""
    for m in list_models():
        if m["id"] == model_id:
            return m["reads_pdf"]
    return None


def free_fallbacks(exclude: str = "", limit: int = 2) -> list:
    """A short chain of other free models to try if the chosen one fails."""
    out = [m["id"] for m in models_for("free") if m["id"] != exclude]
    return out[:limit]


def _clean_key(k) -> str:
    return str(k or "").strip().rstrip(">").strip('"').strip("'").strip()


def accounts(cfg: dict, model_override: Optional[str] = None) -> dict:
    """
    Which OpenRouter calls to make for one results analysis, and where they
    sit relative to gemcall.

    Returns {"before_gemcall": [...], "after_gemcall": [...]}; each attempt is
    {"tier", "key", "model"}. An attempt with no key is left out. If model is
    empty, the free tier picks from the catalogue.
    """
    premium_key = _clean_key(cfg.get("premium_openrouter_api_key")) \
        or _clean_key(os.getenv("OPENROUTER_PREMIUM_API_KEY"))
    free_key = _clean_key(cfg.get("free_openrouter_api_key")) \
        or _clean_key(os.getenv("OPENROUTER_API_KEY"))
    premium_model = (cfg.get("premium_openrouter_model") or "").strip() or DEFAULT_PREMIUM_MODEL
    free_model = (cfg.get("free_openrouter_model") or "").strip()
    premium_on = bool(cfg.get("premium_openrouter_enabled", True))

    override = (model_override or "").strip()
    if override:
        if is_free_model(override):
            key, tier = (free_key, "free") if free_key else (premium_key, "free")
        else:
            key, tier = premium_key, "premium"
        attempt = [{"tier": tier, "key": key, "model": override}] if key else []
        # The operator asked about this model in particular; gemcall's answer
        # would not be the answer to the question asked.
        return {"before_gemcall": attempt, "after_gemcall": [], "skip_gemcall": True,
                "missing": None if key else ("premium" if tier == "premium" else "free")}

    before, after = [], []
    if premium_on and premium_key:
        before.append({"tier": "premium", "key": premium_key, "model": premium_model})
    if free_key:
        after.append({"tier": "free", "key": free_key, "model": free_model})
    return {"before_gemcall": before, "after_gemcall": after, "skip_gemcall": False,
            "missing": None}


def mask(key: str) -> str:
    """What the settings screen shows for a stored key: enough to recognise it."""
    k = _clean_key(key)
    if not k:
        return ""
    return "••••" + k[-4:]


def is_masked(value) -> bool:
    return isinstance(value, str) and value.startswith("••••")
