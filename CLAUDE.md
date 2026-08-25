# Stocks Platform — working notes

Indian market intelligence + auto-trading. FastAPI backend, React frontend,
SQLite, deployed as one Docker container on an Oracle VM.

The commit history is the detailed record — every non-obvious decision is
explained in its commit message. `git log` is worth reading before changing
anything in the results pipeline.

---

## Deploy

There is no CI. Push to `main`, then rebuild on the VM:

```bash
ssh -i ~/.ssh/oci_key ubuntu@129.159.23.190
cd ~/stocks && git pull origin main
sudo docker build -t stocks-app . && sudo docker rm -f stocks-app
sudo docker run -d --name stocks-app --restart always --network host \
  --env-file ~/stocks/backend/.env \
  -v ~/stocks/backend/market_tracker.db:/app/backend/market_tracker.db stocks-app
```

`market_tracker.db` and `.env` are gitignored, so `git pull` never touches
production data or secrets. Schema changes are applied by `_safe_alter` calls in
`init_db()` — add one there for every new column.

Verify after deploy: `sudo docker logs stocks-app | grep "Exchange hub"` should
show cycles. Repeated `Exchange hub stale` means the hub is erroring and the
trade poller's watchdog has taken over.

---

## How results flow

`exchange_hub.py` is the **single owner** of the corporate-announcement
endpoints. One fetch per cycle (NSE and BSE concurrently), fanned out in latency
order:

1. **Trading engine** — `trade_nse_poller.handle_announcements()`, armed-target
   matching. Must stay first.
2. **Results routing** — `results_router.route_financial_result()`.
3. **Intelligence feed** — news-impact classification, persisted as MarketEvent.

Three loops used to poll these URLs independently. Do not reintroduce that.

`announcement_classifier.py` implements `news_fetching_strategy.md`: both
results channels, the keyword filters, priority tiers, and cross-channel dedup.
Dedup keys are claimed under **every** identifier (ISIN, scrip code, ticker) —
BSE and NSE expose different fields, so keying on "the best available" produces
different keys for the same filing and the duplicate slips through.

### What counts as a financial result

`announcement_classifier.is_financial_result()` decides on the **subject**
(title + BSE `SUBCATNAME`), never the body — a body mentioning "financial
results" is far too common to be evidence. Six gates, in order, first match wins:

1. **Forward-looking** → reject. `intimation`, `notice of board meeting`,
   `prior intimation`, and NSE's calendar form `SYMBOL: Board Meeting —`.
   Subject only: a real outcome repeats the agenda wording in its body.
2. **Hard negatives** → reject. Clarifications, corrigenda, revisions,
   re-submissions, cancellations, postponements, non-submission, press cuttings
   (`news paper`, `paper cutting`), transcripts, audio, conference calls, plus
   fundraising / allotment / appointment / resignation / buyback / ESOP.
3. **Non-result subcategories** → reject (BSE labels these precisely).
4. **Audited without un-audited** → reject. If either subject *or* details says
   "audited" and neither says "un-audited", it is the year-end set.
5. **Direct result** → accept (`direct_result`).
6. **Board outcome** → accept if results language appears in subject *or* body
   (`board_meeting_outcome`). The only place the body is trusted, and safe only
   because 1–4 have already run.

`"Un-Audited" contains "audited"`, so gate 4 uses lookbehinds; and the six
spellings seen in the wild (`unaudited`, `un-audited`, `un audited`,
`Un- Audited`, `Un - Audited`, `Un -audited`) are all read as unaudited.
**1,648 of 3,713 genuine results state no qualifier at all**, so the rule
excludes audited rather than requiring unaudited.

### Impact news

`is_impact_news()` catches good news that moves a price without carrying
numbers: order wins, bonus/split, buybacks, acquisitions, capacity additions,
regulatory approvals, rating upgrades, joint ventures. Routed by
`results_router.route_impact_news()` to the same alert and order screen as a
result, stored with `PendingResultOrder.kind` naming the event.

**No Screener lookup and no earnings AI run on these** — Screener publishes
quarterly figures and the 2-step extractor expects a results PDF. Deduped
against the same kind on the same day, not the 30-day results window: a company
can win several orders in a month and each is its own decision.

A results filing that also mentions an acquisition is a results filing —
`is_impact_news` returns False for anything `is_financial_result` accepts.

### One prompt per company per quarter

One earnings event produces the outcome, the results PDF, the newspaper
advertisement and the presentation, all genuinely results. `_already_prompted_this_quarter`
suppresses the second and later prompts within 30 days. It reads `market_events`,
not the prompts — prompts are purged after 72h and cannot answer a question
about the last month.

### NSE fills in the body after the subject

NSE publishes `Outcome of Board Meeting` with an empty description and fills it
minutes later, so a filing can *become* a result after we first saw it. 94 rows
sat in that state. `recheck_late_bodies()` re-asks the question every 2 minutes
for the last 6 hours. Do not backfill the classification instead — that claims
knowledge we did not have at the decision moment and inflates any backtest.

### The 09:00-15:30 window

Alerts and AI analysis run only while the market is open, **09:00 to 15:30 IST**
on a working day. Anything outside that — pre-open, post-close, or a weekend —
gets **no alert and no AI analysis**, because there is no session left to trade
into. Those are marked `deferred`, stay visible in the app, and are reported in
the 08:00 digest from Screener's figures only.

A filing between 08:00 and 09:00 therefore waits for the *next* morning's
digest: it is past the current digest's window but before the AI opens.

Nothing analyses them later. That is deliberate; a 07:00 batch job existed
briefly and was removed for contradicting the cutoff.

### Daily jobs (IST, guarded by last-run date)

- **01:00** purge result prompts older than 72h (rows that led to an order are
  kept — `config_id` is the only trail from a filing to a position)
- **02:00** retire target configs whose date has passed, **never** those in
  `bought`/`holding` — that would drop the stoploss watcher's subject
- **06:00** earnings sync into the watchlist
- **Sun 17:00** rebuild the symbol registry from both exchanges (below)
- **08:00** morning digest — HTML page + PDF + one Telegram alert. Covers a
  rolling **08:00-to-08:00 IST** window, so every filing lands in exactly one
  digest and the boundary falls where nobody is trading. Anything older that was
  never digested is swept in too, so a missed run or a weekend cannot lose rows.

Screener is keyed by **BSE scrip code** for BSE-only companies — `LIMECHM` 404s
where `507759` resolves — and answers 429 when called back to back, so requests
are paced. Both together took digest coverage from 4/14 to 6/6 on a sample.

The digest's mechanical `screener_signal` refuses any comparison against a base
under ₹1 crore. A micro-cap moving from ₹0.02 Cr profit to ₹0.06 Cr loss is
arithmetically -400% and means nothing; it reads NA, like every other
uncertainty in this codebase.

---

## Things that bit us, and why the code looks like it does

**Never poll without a single-flight guard.** `main.py::_single_flight` drops a
tick whose previous run is still going. Without it, slow NSE responses queue
workers without bound and the box saturates — that was the original 100% CPU.

**Logging must stay configured.** `basicConfig` is called in `main.py`. Nothing
called it before, so every `logger.error` was discarded and a dead BSE endpoint
stayed invisible for weeks.

**NSE names the ISIN field `sm_isin`.** Not `isin`, which does not exist on the
announcement feed — reading it meant every NSE announcement arrived with an
empty ISIN for as long as the hub has existed, throwing away the one identifier
that joins a filing to its BSE twin. `sm_isin` is populated on every row.

**BSE's `AnnGetData` endpoint is retired.** Use `AnnSubCategoryGetData`, needs
`pageno` and `subcategory`, and wants a **same-day** window — a multi-day range
returns zero rows rather than a superset. `SUBCATNAME` classifies far better
than `CATEGORYNAME`.

**Symbols come from the symbol registry**, not fuzzy name matching. BSE's
current endpoint returns no ISIN and the NSE dump truncates company names, so
name similarity fails in both directions.

The live registry is the **`symbol_registry` table**, not the CSV.
`data/symbol_registry.csv` is only the checked-in seed, loaded on the first
lookup against an empty table. The live copy has to be in the database because
the CSV is `COPY`'d into the image from git — a refresh written to the file is
reverted by the next `docker build`, exactly the way the baked `.env` overrides
the mounted one.

`services/registry_builder.py` rebuilds it **Sunday 17:00 IST** from three
lists, joined on ISIN — the only identifier both exchanges publish, and unique
within each list:

| | rows |
|---|---|
| `nsearchives…/content/equities/EQUITY_L.csv` | 2,557 |
| `nsearchives…/emerge/corporates/content/SME_EQUITY_L.csv` | 565 |
| `api.bseindia.com…/ListofScripData?segment=Equity&status=Active` | 4,976 |

One BSE call covers its SME platform too (groups M/MT/MS). The NSE SME file
spells its headers differently (`NAME_OF_COMPANY`, trailing comma per row), so
the reader normalises headers instead of indexing them by name. BSE is called
directly rather than through `BSESession` — the worker proxy's failure mode is a
success-shaped one and a weekly job has no latency to defend.

**A rebuild is all-or-nothing.** Applying a partial fetch would drop every
BSE-only listing — ~2,500 companies — and each would then resolve to a bare
scrip code, which is the exact failure the registry exists to prevent. Each
source has a row floor, and the join may not shrink the registry by more than
5% without `--force`. A short fetch is a run to retry, never a registry to
install.

After writing, the builder **must** call `symbol_registry.reload()`. `_load()`
sets `_loaded` once and returns early forever after, so without it the process
serves the map it read at startup and the fresh rows sit unread.

A missed Sunday — container down, or a failed fetch — is picked up by the daily
overdue check at 8 days. Last-run is kept in `system_settings`, not in the
scheduler's in-memory `last_run`: those guards reset on restart, which is
harmless daily and wrong weekly.

Run it by hand with `python ops/build_symbol_registry.py` (`--dry-run` reports
the diff and lists the dual-listed additions, `--export` refreshes the seed).

**A stale registry prompts the same result twice.** On 25 Aug 2026 Ardee
Industries filed to BSE at 14:20 and NSE at 14:35 and got two order screens. The
company had listed 13 days earlier and was in neither the CSV nor any lookup, so
`resolve` fell back to what each exchange sent — `544860` from BSE, `ARDEE` from
NSE. Those share **no** identifier, so `_claim_result_key` found no collision
and `_already_prompted_this_quarter`, which compares `MarketEvent.symbol`, saw
two different companies. Both nets are identifier-based; neither can work on a
listing the registry has never heard of. 613 listings were missing when this was
found, 18 of them dual-listed.

**Instrument keys must be resolved, never synthesised.** `NSE_EQ|<SYMBOL>` is
not a key Upstox accepts, and a BSE-only scrip has no NSE listing to fall back
on. Worse, one malformed key fails the *whole* batch, so a single unknown scrip
blanked every price on the screen. `main.py::resolve_instrument_keys` indexes
both exchange dumps — by trading symbol and by BSE scrip code — and returns NSE
first, BSE second. Unresolvable symbols are dropped from the request and read
"no live quote" in the UI.

**Quotes have a single owner.** `upstox_feed` runs one background loop that
refreshes every key anyone asked for, in one request (Upstox takes 500 keys), and
all panels read its cache. Before that, four panels fetched on their own timers
and blew the ~1000-per-30-min limit: prices vanished screen-wide with UDAPI10005,
and the result baselines — one call each, never retried — were starved by the
polling meant to display them. Request rate is now a property of the loop's
interval alone, not of how many panels or tabs are open.

**Only the pipe form of an instrument key is a request key.** `BSE_EQ:INE...` is
how quotes come *back* keyed; sending it returns UDAPI1087 and voids the whole
request, including the keys that were fine.

**Never write `` into a regex through a patch script.** Twice a word boundary
was written as a literal backspace (``), and both times the pattern silently
matched nothing while looking correct in the terminal, in grep, and in the
compiled pattern printed back — a backspace is invisible. Use a lookahead, or
check `repr(pattern)`. Grep the services directory for `` after any regex edit.

**A test that restates a rule tests its own spelling.** A verification script
that re-declared the unaudited regex reported five violations that did not exist,
because the code had moved on and the test had not. Import the live patterns.

**Screener figures live on the filing row** (`screener_json`), fetched once. A
published quarter cannot change, and fetching is paced at ~1 company/second, so
re-reading a past day must never mean re-fetching it. The digest file is named
for the morning it ran, which is a different set from "results announced on
<date>" — do not use it as a lookup key.

**A "success-shaped failure" is the dangerous kind.** The BSE proxy returned
HTTP 200 "No Record Found!" for every query and starved the feed silently. Empty
responses now re-check the origin.

---

## AI earnings analysis

Two flows, in order:

1. **gemcall** — `custom_api_url`, a local Playwright service driving the Gemini
   web UI. Lives at `C:\Users\koush\OneDrive\Desktop\gemcall`, **not in git**.
2. **OpenRouter** — `premium_openrouter_model`, currently
   `google/gemini-2.5-flash-lite` (reads PDFs natively, JSON mode, cheapest
   capable option ≈ $0.0011/filing).

Settings live in the SQLite `system_settings` table and override code defaults.

### Guards — do not weaken these

Output is a fixed 5×5 grid (revenue/expenses/other income/PAT/EBITDA ×
current qtr/YoY/YoY %/QoQ %/estimated). Three layers protect it:

- **`metrics_are_usable`** — no verdict without revenue AND PAT.
- **`validate_metrics`** — the model states both the values and the change
  between them, so those must reconcile. Catches numbers read off the wrong row
  when a filing's layout differs. Plus sign and magnitude checks.
- **`response_matches_company`** / **`response_indicates_missing_document`** —
  a scraped browser session once returned *another company's* analysis
  (EMMESSA's card held Carborundum's figures). Both are rejected to NA.

Uncertainty is always **NA**, never HOLD or NEUTRAL — a hedge reads as a real
call to anyone acting on it.

`_extract_json_from_llm` is deliberately tolerant: scraped output arrives with
literal newlines inside strings and sometimes **two concatenated renderings** of
the answer. It scores candidates and keeps the richest.

---

## Latency — measured, don't re-litigate

| | |
|---|---|
| Announced → ingested, median | **36.6 s** |
| Our controllable share | ~1.1 s |
| NSE / BSE round trip | 72 ms / 13 ms |

The ~36 s is the **exchange's own publication lag** and cannot be compressed.
Sub-second is not reachable by polling; 10 ms would need co-located direct
exchange feeds, which carry trades, not filing PDFs.

Poll interval stays at 2s. Halving it buys ~1.4% of total latency while doubling
the request rate against NSE, which blocks aggressive pollers.

---

## Known friction

**Cloudflare *quick* tunnels rotate their URL on every restart.** This has
repeatedly broken the gemcall endpoint and the Telegram webhook. Named tunnels
with fixed hostnames would end it.

- Re-register the webhook: `POST /api/telegram/register-webhook?public_url=...`
- Set `PUBLIC_BASE_URL` in `.env` so the digest alert carries a clickable link.

**`/buy` and `/sell` in Telegram place real orders with no confirmation step.**

**A stale `.env` is baked into the Docker image.** There is no `.dockerignore`,
so `COPY backend/ ./backend/` copies `backend/.env` — Upstox secret and Telegram
token included — into the image. Worse, `config.py` calls
`load_dotenv(override=True)`, so that baked copy *overrides* the `--env-file`
passed at runtime: `printenv` showed the right URL while the app served the old
one. The deploy now bind-mounts the live file over it
(`-v ~/stocks/backend/.env:/app/backend/.env:ro`), which papers over it. The
real fix is a `.dockerignore`, dropping `override=True`, and clearing the dead
tunnel hostname hardcoded as `UPSTOX_REDIRECT_URI`'s default in `config.py`.

**Rotating the tunnel is one command:** `bash ops/retunnel.sh` on the VM. It
restarts the tunnel, reads the new hostname from only the log written after the
restart, refuses to rewire unless the URL answers 200, rewrites `.env`,
recreates the container (`--env-file` is read at creation, so a restart keeps the
old environment), and re-registers the Telegram webhook. The Upstox developer
console redirect URI is left to the operator. `--current` prints the live URL and
changes nothing.

**gemcall is not version controlled.** Several fixes live only on local disk.

---

## Where the pipeline stands (measured 25 Aug 2026)

| | |
|---|---|
| filings ingested, 28 Jul–25 Aug | 50,219 |
| classified as results | 3,713 (7.4%) |
| impact news | 927 — 430 M&A, 347 order wins, 63 expansion, 44 buyback, 28 split |
| announced → ingested, median | 52 s |
| filings arriving outside 09:00-15:30 | 86% |
| AI verdicts that are NA | 88% — **both AI paths are down**, see Open items |

`ops/` holds operational scripts; exports are written to the scratchpad, not the
repo (a stray `results_announcements.csv` in the root is a leftover, safe to delete).

---

## Open items

- OpenRouter API key is **empty** — the fallback cannot run, so most analyses
  return NA unless gemcall is up and logged in.
- gemcall needs `login-google.bat` then `start-all.bat` (login first, or it
  restarts into an unauthenticated session), and its `custom_api_url` is a quick
  tunnel that has already died — a named tunnel would end this.
- **Both AI paths are therefore down**, which is why 1,510 of 1,531 analyses have
  no usable revenue+PAT. The triage in front of them works; nothing is reading
  the numbers. An OpenRouter key is the cheap fix (~$0.0011/filing).
- **Secrets are baked into the Docker image** — see the `.env` note above. Wants
  a `.dockerignore` and `load_dotenv(override=False)`.
- **The earnings calendar is NSE-heavy.** BSE has no board-meeting API we can
  reach (every path redirects), so BSE meetings are read from the announcement
  feed's "Board Meeting" subcategory, where the date exists only in prose and
  parses 85% of the time. Coverage of actual filers was 99% NSE / 25% BSE before
  this; the next results season is the real test.
- **A named Cloudflare tunnel** would end the URL rotation for good. Needs a
  Cloudflare account with a domain; the server side is quick once that exists.
