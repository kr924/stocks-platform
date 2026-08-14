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

- **06:00** earnings sync into the watchlist
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

**BSE's `AnnGetData` endpoint is retired.** Use `AnnSubCategoryGetData`, needs
`pageno` and `subcategory`, and wants a **same-day** window — a multi-day range
returns zero rows rather than a superset. `SUBCATNAME` classifies far better
than `CATEGORYNAME`.

**Symbols come from `data/symbol_registry.csv`**, not fuzzy name matching. BSE's
current endpoint returns no ISIN and the NSE dump truncates company names, so
name similarity fails in both directions.

**Instrument keys must be resolved, never synthesised.** `NSE_EQ|<SYMBOL>` is
not a key Upstox accepts, and a BSE-only scrip has no NSE listing to fall back
on. Worse, one malformed key fails the *whole* batch, so a single unknown scrip
blanked every price on the screen. `main.py::resolve_instrument_keys` indexes
both exchange dumps — by trading symbol and by BSE scrip code — and returns NSE
first, BSE second. Unresolvable symbols are dropped from the request and read
"no live quote" in the UI.

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

**gemcall is not version controlled.** Several fixes live only on local disk.

---

## Open items

- OpenRouter API key is **empty** — the fallback cannot run, so most analyses
  return NA unless gemcall is up and logged in.
- gemcall needs `login-google.bat` then `start-all.bat` (login first, or it
  restarts into an unauthenticated session).
