"""
NSE/BSE announcement classification — implements news_fetching_strategy.md.

Two responsibilities:

1. Decide whether an announcement *is* a financial result. Results reach the
   exchanges through two channels and the same result can appear in up to four
   places (Channel 1 and Channel 2, on each of BSE and NSE):

     Channel 1  Board Meeting -> "Outcome of Board Meeting"   (usually first)
     Channel 2  Result        -> "Financial Results"          (explicit, later)

   Channel 1 is NOT automatically a result — board meetings are also held for
   fundraising, appointments, buybacks and so on — so a keyword filter decides.

2. Assign a priority tier and category to everything that is not a result, so
   the feed can be ranked without an LLM call.

Nothing in this module performs I/O; it is pure text classification so it can run
inline on the ingest path without adding latency.
"""
import re
from typing import Optional, Tuple

# ─── Financial-results keyword filter ───────────────────────────────────────

# Positive match — the announcement IS financial results.
_RESULT_POSITIVE = re.compile(
    r"(?:"
    r"financial\s+results?"
    r"|un\s?-?\s?audited\s+financial"
    r"|audited\s+financial"
    r"|quarterly\s+results?"
    r"|half[\s.-]?yearly\s+results?"
    r"|annual\s+results?"
    r"|standalone[^.]{0,40}results?"
    r"|consolidated[^.]{0,40}results?"
    r"|\bq[1-4]\b[^.]{0,40}results?"
    r"|\bq[1-4]\b[^.]{0,20}fy"
    r"|statement\s+of[^.]{0,30}profit"
    r"|profit\s+and\s+loss"
    r"|profit\s*&\s*loss"
    r")",
    re.IGNORECASE,
)

# Negative match — explicitly NOT financial results, even under a board-meeting
# outcome. Checked before the positive filter.
_RESULT_NEGATIVE = re.compile(
    r"(?:"
    r"fund[\s-]?raising"
    r"|raising\s+of\s+funds"
    r"|preferential[^.]{0,30}issue"
    r"|allotment"
    r"|appointment"
    r"|resignation"
    r"|buy[\s-]?back"
    r"|\besop\b"
    r"|\besps\b"
    r"|trading\s+window"
    # A company publishing its results in the press files this separately from
    # the results themselves, and it carries the same words. Matching only
    # "newspaper publication" missed both "News Paper Cutting" — the space is
    # how BSE writes it — and the reversed "Publication ... in Newspaper".
    # A company explaining why it has NOT filed is the opposite of a result, and
    # it says "financial results" twice while doing so.
    r"|non[\s-]?submission"
    r"|reasons?\s+for\s+delay"
    r"|delay(?:ed)?\s+(?:in\s+)?(?:submission|filing)"
    # Corrections and supplements to a result already filed. The original is
    # what a decision was made on; re-firing on the correction buys twice.
    r"|corrigendum"
    r"|inadvertently\s+omitted"
    # The call about the results is not the results.
    r"|audio\s+recording"
    r"|conference\s+call"
    r"|transcript"
    r"|news\s?paper"
    r"|paper\s+cutting"
    r"|press\s+cutting"
    r"|newspaper\s+advertisement"
    # A clarification, cancellation or postponement is about a result; it is not
    # one, and acting on it trades a filing that either restates something
    # already known or says the meeting is off.
    r"|clarification"
    r"|cancellation|cancelled"
    r"|withdraw"
    r"|postpone|reschedul"
    r"|re[\s-]?submission"
    r"|revised"
    r"|loss\s+of\s+share\s+certificate"
    r")",
    re.IGNORECASE,
)

# A subject that only declares a dividend is not a results filing.
# Quarterly results are unaudited; the audited ones are the year-end set, filed
# months after the quarter they cover. The lookbehinds matter: "Un-Audited"
# contains "audited", and a naive exclusion would reject exactly the filings
# this is meant to keep.
_UNAUDITED = re.compile(r"un[\s.-]?audited", re.I)
_AUDITED_ONLY = re.compile(r"(?<!un)(?<!un[\s.-])audited(?![a-z])", re.I)


_DIVIDEND_ONLY = re.compile(r"dividend", re.IGNORECASE)

_BOARD_OUTCOME = re.compile(r"outcome[^.]{0,20}board\s+meeting", re.IGNORECASE)

# A filing that merely *announces a future meeting* is never a result, however
# much results language its body carries ("...to consider the financial results
# for Q1"). Matched against the subject only: an actual outcome filing often
# repeats the agenda wording in its body, and checking the body would suppress
# the real result.
_FORWARD_LOOKING = re.compile(
    r"(?:"
    r"intimation"
    r"|notice\s+of\s+board\s+meeting"
    r"|board\s+meeting\s+intimation"
    r"|prior\s+intimation"
    r"|schedule\s+of\s+board\s+meeting"
    # NSE's board-meeting calendar publishes entries as
    # "SYMBOL: Board Meeting - Financial Results", announcing a meeting that will
    # consider results. The dash is what separates it from an actual filing:
    # a real one reads "Board Meeting Outcome for ...", with no dash.
    r"|^[^:]{1,20}:\s*board\s+meeting\s*[-–—]"
    r")",
    re.IGNORECASE,
)

CHANNEL_BOARD_OUTCOME = "board_meeting_outcome"
CHANNEL_DIRECT_RESULT = "direct_result"


# BSE subcategories that are never a results filing, however much results
# language their body carries. An investor presentation or an analyst-meet
# intimation routinely discusses "the financial results for the quarter", and
# treating that as the result itself both fires a false trade trigger and burns
# the cross-channel dedup key that the real filing needs.
_NON_RESULT_SUBCATEGORIES = (
    "investor presentation",
    "analyst",
    "investor meet",
    "press release",
    "media release",
    "newspaper",
    "news paper",
    "paper cutting",
    "earnings call",
    "transcript",
    "audio",
    "video",
    "annual report",
    "postal ballot",
    "trading window",
    "corporate governance",
    "shareholding",
)


def is_financial_result(title: str, description: str = "", category_name: str = "") -> Tuple[bool, Optional[str]]:
    """
    Decide whether an announcement is a financial-results filing.

    Returns (is_result, channel) where channel is CHANNEL_BOARD_OUTCOME,
    CHANNEL_DIRECT_RESULT, or None.

    The decision rests on the *subject*, not the body. BSE's subject is often a
    generic "Announcement under Regulation 30 (LODR)" with the real descriptor in
    SUBCATNAME, so the subcategory is treated as part of the subject. The body is
    consulted only to confirm a board-meeting outcome, because a body mentioning
    "financial results" is far too common to be evidence on its own.
    """
    subject = (title or "").strip()
    body = (description or "").strip()
    cat = (category_name or "").strip().lower()
    # On BSE the subcategory carries the real subject, so classify on both.
    subject_text = f"{subject} {cat}".strip()

    # A notice of an upcoming meeting is a calendar entry, not a result.
    if _FORWARD_LOOKING.search(subject_text):
        return False, None

    # Hard negatives win — a board outcome about fundraising is not a result.
    if _RESULT_NEGATIVE.search(subject_text):
        return False, None

    # Presentations, analyst meets and press releases discuss results without
    # being one.
    if any(nr in cat for nr in _NON_RESULT_SUBCATEGORIES):
        return False, None

    # Year-end audited results are a different animal from the quarterly print
    # this pipeline trades: filed long after the period, already largely known.
    full_text = f"{subject_text} {body}"
    if _AUDITED_ONLY.search(full_text) and not _UNAUDITED.search(full_text):
        return False, None

    # Channel 2 — explicit result filing, declared in the subject/subcategory.
    if cat.startswith("result") or _RESULT_POSITIVE.search(subject_text):
        return True, CHANNEL_DIRECT_RESULT

    # Channel 1 — board-meeting outcome whose subject or body confirms results.
    if _BOARD_OUTCOME.search(subject_text) or cat.startswith("board meeting"):
        if _RESULT_POSITIVE.search(f"{subject_text} {body}"):
            return True, CHANNEL_BOARD_OUTCOME
        # "Outcome ... dividend" with no results language is a corporate action.
        return False, None

    return False, None


# ─── Priority-1 / Priority-2 / Priority-3 classification ────────────────────

# (category, priority, compiled pattern) — evaluated in order, first match wins.
_CATEGORY_RULES = [
    ("corporate_action", 1, r"\bbonus\b|stock\s+split|sub[\s-]?division"),
    ("corporate_action", 1, r"buy[\s-]?back|tender\s+offer"),
    ("corporate_action", 1, r"\bdividend\b"),
    ("merger_acquisition", 1, r"acquisition|acquire|amalgamation|\bmerger\b|de[\s-]?merger|slump\s+sale"),
    ("order_win", 1, r"order\s+(?:received|won)|receipt\s+of\s+order|award\s+of\s+order|contract\s+award|bagg(?:ed|ing)|letter\s+of\s+intent|\bloi\b"),
    ("credit_rating", 1, r"credit\s+rating|rating\s+(?:upgrade|downgrade|revision)|crisil|\bicra\b|\bcare\s+ratings?\b|india\s+ratings|brickwork"),
    ("management_change", 1, r"(?:resignation|appointment|cessation)[^.]{0,60}(?:\bceo\b|\bcfo\b|chief\s+(?:executive|financial|operating)\s+officer|managing\s+director|whole[\s-]?time\s+director|\bmd\b|director|chairman)|change\s+in\s+management|change\s+in\s+directorate"),
    ("insolvency", 1, r"insolvency|\bcirp\b|\bnclt\b|winding[\s-]?up|liquidation|\bdefault\b"),
    ("fundraising", 1, r"\bqip\b|qualified\s+institutional|preferential\s+issue|rights\s+issue|raising\s+of\s+funds|fund[\s-]?raising"),
    ("open_offer", 1, r"open\s+offer|takeover|substantial\s+acquisition"),

    ("joint_venture", 2, r"joint\s+venture|\bjv\b|collaboration\s+agreement"),
    ("expansion", 2, r"product\s+launch|capacity\s+addition|new\s+plant|commissioning|expansion"),
    ("regulatory", 2, r"fda\s+approval|\banda\b|regulatory\s+approval|rbi\s+approval|sebi\s+approval|\blicen[cs]e\b"),
    ("investor_relations", 2, r"investor\s+presentation|earnings\s+call|analyst[\s/]+(?:meet|investor)|investor\s+meet|con\.?\s?call|conference\s+call|earnings\s+transcript"),
    ("insider_trade", 2, r"\bpledge\b|encumbrance|\bsast\b|reg\.?\s?2[39]\b|reg\.?\s?31\b|insider"),
    ("disruption", 2, r"\bstrike\b|lock[\s-]?out|disruption|force\s+majeure|shutdown"),
    ("clarification", 2, r"clarification|response\s+to\s+query|media\s+report"),
    ("corporate_action", 2, r"record\s+date|book\s+closure|ex[\s-]date"),

    ("compliance", 3, r"trading\s+window"),
    ("compliance", 3, r"loss\s+of\s+(?:share\s+)?certificate|duplicate\s+certificate"),
    ("compliance", 3, r"change\s+of\s+name|registered\s+office"),
    ("compliance", 3, r"compliance\s+certificate|pcs\s+certificate"),
    ("compliance", 3, r"annual\s+report|\bbrsr\b|sustainability"),
    ("compliance", 3, r"corporate\s+governance"),
    ("compliance", 3, r"newspaper\s+publication|press\s+release"),
]

_COMPILED_RULES = [(cat, pri, re.compile(pat, re.IGNORECASE)) for cat, pri, pat in _CATEGORY_RULES]


def classify(title: str, description: str = "", category_name: str = "") -> dict:
    """
    Classify an announcement into a category and priority tier.

    Returns:
        {
          "is_financial_result": bool,
          "channel": str | None,      # which results channel it arrived on
          "category": str,            # earnings, corporate_action, compliance, ...
          "priority": int,            # 1 = high impact, 2 = medium, 3 = routine
        }
    """
    subject = (title or "").strip()
    body = (description or "").strip()
    cat = (category_name or "").strip()

    is_result, channel = is_financial_result(subject, body, cat)
    if is_result:
        return {
            "is_financial_result": True,
            "channel": channel,
            "category": "earnings",
            "priority": 1,
        }

    # The subcategory leads: BSE's subject is frequently the boilerplate
    # "Announcement under Regulation 30 (LODR)" while SUBCATNAME holds the actual
    # descriptor ("Award of Order / Receipt of Order"), which is exactly the
    # column news_fetching_strategy.md keys its priority tiers off.
    haystack = f"{cat} {subject} {body}"
    for category, priority, pattern in _COMPILED_RULES:
        if pattern.search(haystack):
            return {
                "is_financial_result": False,
                "channel": None,
                "category": category,
                "priority": priority,
            }

    return {
        "is_financial_result": False,
        "channel": None,
        "category": "general",
        "priority": 3,
    }


# ─── Cross-channel deduplication keys ───────────────────────────────────────

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _norm(value: str) -> str:
    """Lowercase and strip everything that is not alphanumeric."""
    return _NON_ALNUM.sub("", (value or "").lower())


def _normalize_pdf_name(filename: str) -> str:
    """
    Normalise an attachment filename for cross-exchange matching.

    Companies upload the same PDF to both exchanges, but the exchanges prepend
    their own identifiers and extensions vary in case. Stripping the extension
    and all separators leaves a stable core.
    """
    if not filename:
        return ""
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    name = re.sub(r"\.(pdf|xml|zip|xlsx?|docx?)$", "", name, flags=re.IGNORECASE)
    return _norm(name)


def _identifiers(symbol: str, isin: str, scrip_code: str) -> list:
    """
    Every identifier this filing can be recognised by, most specific first.

    All of them are used, not just the best one. The two exchanges expose
    different fields — BSE carries ISIN and a numeric scrip code, NSE usually
    only the ticker — so keying on "the best available identifier" produces
    *different* keys for the same result and the duplicate slips through. Keying
    on all of them means a collision on any shared identifier is enough.
    """
    out = []
    for value in (isin, scrip_code, symbol):
        normalised = _norm(value)
        if normalised and normalised not in out:
            out.append(normalised)
    return out


def results_dedup_candidates(
    symbol: str,
    isin: str,
    scrip_code: str,
    pdf_filename: str,
    event_date: str,
) -> list:
    """
    Every key under which this filing might already have been recorded.

    Layer 1: identifier + normalised PDF filename. The same company uploads the
    identical file to both exchanges and both channels.

    Layer 2: identifier + calendar date. A company publishes results only once
    per quarter, so two results filings for one company on one day are the same
    result. This catches the case where the exchanges renamed the attachment.

    `event_date` must already be a date-only string (YYYY-MM-DD).
    """
    pdf_key = _normalize_pdf_name(pdf_filename)
    keys = []
    for identifier in _identifiers(symbol, isin, scrip_code):
        if pdf_key:
            keys.append(f"pdf:{identifier}:{pdf_key}")
        keys.append(f"date:{identifier}:{event_date}")
    return keys


def results_dedup_key(
    symbol: str,
    isin: str,
    scrip_code: str,
    pdf_filename: str,
    event_date: str,
) -> str:
    """The canonical key for a filing — the first of its candidates."""
    candidates = results_dedup_candidates(symbol, isin, scrip_code, pdf_filename, event_date)
    return candidates[0] if candidates else f"date::{event_date}"
