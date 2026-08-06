# News Fetching Strategy: Financial Results & Market-Impacting Events

This document provides a verified, actionable strategy for fetching corporate news from both NSE and BSE, with a focus on capturing financial results at first publication and other market-moving events.

---

## 1. Financial Results — First Publication Detection

Financial results can appear under **multiple categories** on both exchanges. To guarantee you capture the first publication regardless of how the company files it, you must monitor **two channels** and suppress duplicates across them.

### Channel 1: Board Meeting Outcome (PRIMARY — Usually Published First)

Companies are required to disclose the outcome of a board meeting within 30 minutes of its conclusion. Since the board approves financial results during the meeting, this filing typically hits the exchange **before** the separate "Result" category filing.

| Exchange | Category | Subcategory | Field to Check |
| :--- | :--- | :--- | :--- |
| **BSE** | `Board Meeting` | `Outcome of Board Meeting` | `CategoryName == "Board Meeting"` AND `NEWSSUB` contains "Outcome" |
| **NSE** | `Board Meetings` | `Outcome of Board Meeting` | `desc` contains "Board Meeting" AND (`attchmntText` OR `desc`) contains result keywords |

> [!WARNING]
> **"Outcome of Board Meeting" is NOT always financial results.** Board meetings are held for many reasons (dividend declarations, fundraising approvals, director appointments, etc.). You **MUST** apply keyword filtering on the subject/attachment text to distinguish financial results from other outcomes.

### Channel 2: Direct Results Filing (SECONDARY — Explicit but Often Delayed)

| Exchange | Category | Subcategory | Field to Check |
| :--- | :--- | :--- | :--- |
| **BSE** | `Result` | `Financial Results` | `CategoryName == "Result"` AND `NEWSSUB` contains "Financial Results" |
| **NSE** | `Financial Results` | (no subcategory — it's a dedicated section) | `desc` contains "Financial Results" |

> [!NOTE]
> This is the **clean, explicit** channel. Subject lines clearly say "Financial Results for Q1/Q2/Q3/Q4" or "Annual Audited Results". However, by the time a company files here, the same data has usually already been published via Channel 1 (Board Meeting Outcome). **This channel serves as a safety net** to catch any filings that were NOT captured by Channel 1.

### Keyword Filter for Detecting Financial Results in Board Meeting Outcomes

Apply these regex patterns against the **subject line** (`NEWSSUB` on BSE / `attchmntText` + `desc` on NSE):

**Positive Match (IS financial results):**
```
financial result
unaudited financial
audited financial
quarterly result
half.?yearly result
annual result
standalone.*result
consolidated.*result
Q[1-4].*result
Q[1-4].*FY
statement of.*profit
profit.*loss
```

**Negative Match (is NOT financial results — skip even if under Outcome):**
```
fund.?raising
preferential.*issue
allotment
appointment
resignation
dividend.*only      (if subject ONLY mentions dividend, no results)
buyback
ESOP
ESPS
```

### Deduplication & Cross-Channel Suppression

The same financial result can appear in **4 places** (Channel 1 on BSE, Channel 1 on NSE, Channel 2 on BSE, Channel 2 on NSE). You must ensure a result is processed only **once**.

> [!CAUTION]
> **You CANNOT reliably extract the quarter/period from the API response fields.** The `NEWSSUB` (BSE) and `desc` (NSE) fields are free-text written by the company's compliance officer. Some say "Q1 FY27", others say "Quarter ended 30th June 2026", others just say "Financial Results" with no period mentioned. **Do not use quarter as a dedup key.**

#### Practical Deduplication Strategy (3 Layers):

**Layer 1 — PDF Filename Match (Most Reliable):**
The same company uploads the **exact same PDF file** to both BSE and NSE, and to both channels. The `ATTACHMENTNAME` (BSE) or `attchmntFile` (NSE) will often be identical or very similar.

```
Dedup Key = normalize(company_identifier) + normalize(pdf_filename)
```

Example:
- BSE files: `RELIANCE_05082026_Outcome.pdf`
- NSE files: `RELIANCE_05082026_Outcome.pdf`  → **Same file = duplicate, skip.**

**Layer 2 — Company + Date Window (Fallback):**
If the PDF filenames differ across exchanges (some companies rename), fall back to:

```
Dedup Key = ISIN (or Scrip Code) + Date (date-only, no time)
```

**Reasoning:** A company publishes its financial results **only once per quarter**. On any given day, if you already captured a financial result for ISIN `INE002A01018` at 5:30 PM from BSE Board Meeting Outcome, and at 5:35 PM the same company files under NSE Financial Results — it is the **same result**. A company will never publish two different quarter results on the same day.

Example:
- BSE Board Meeting Outcome at 17:30 → `INE002A01018_2026-08-05` → **NEW, process it.**
- NSE Financial Results at 17:35 → `INE002A01018_2026-08-05` → **Already exists, skip.**
- BSE Result at 17:40 → `INE002A01018_2026-08-05` → **Already exists, skip.**

**Layer 3 — Subject Similarity (Edge Case Safety Net):**
In rare cases where a company files a result AND a separate board meeting outcome on the same day for different matters (e.g., results + fundraising approval), Layer 2 alone could wrongly suppress the second filing. To handle this:
- Before suppressing, check if the **subject text** of the existing record and the new record both match the financial-results keyword filter.
- If the new record does NOT match financial-results keywords (e.g., it's about fundraising), do NOT suppress it — it's a different announcement.

#### Cross-Channel Processing Flow:

```
1. Fetch all "Board Meeting → Outcome" announcements (Channel 1)
   └─ For each:
      ├─ Apply financial-results keyword filter
      ├─ If MATCH → Generate dedup key → Check DB
      │   ├─ Key NOT found → Store as NEW financial result (source = Channel 1)
      │   └─ Key FOUND → Skip (already captured)
      └─ If NO MATCH → This is a non-results board outcome
         └─ Process under "Other News" categories (dividend, fundraising, etc.)

2. Fetch all "Result → Financial Results" announcements (Channel 2)
   └─ For each:
      ├─ Generate dedup key → Check DB
      │   ├─ Key NOT found → Store as NEW financial result (source = Channel 2)
      │   │   (This means Channel 1 missed it — rare but possible)
      │   └─ Key FOUND → Skip (already captured from Channel 1)
      └─ Done
```

> [!TIP]
> **Processing order matters.** Always process Channel 1 (Board Meeting Outcomes) **before** Channel 2 (Direct Results) in each polling cycle. This ensures Channel 1 gets priority as the first source, and Channel 2 only fills gaps.

---

## 2. Other Market-Impacting News to Monitor

Beyond financial results, the following news types have **proven, immediate stock price impact**. I recommend monitoring these categories in order of priority.

### 🚨 Priority 1 — High Impact (Monitor in Real-Time)

These should trigger immediate alerts and analysis.

| # | News Type | BSE Category → Subcategory | NSE Category | Keywords to Match |
| :--- | :--- | :--- | :--- | :--- |
| 1 | **Dividend Declaration** | `Corp. Action` → `Dividend` OR `Board Meeting` → `Outcome of Board Meeting` | `Corporate Actions` / `Board Meetings` | `dividend`, `interim dividend`, `final dividend`, `special dividend` |
| 2 | **Bonus / Stock Split** | `Corp. Action` → `Bonus` / `Sub-division / Stock Split` | `Corporate Actions` | `bonus`, `stock split`, `sub-division`, `subdivision` |
| 3 | **Buyback Announcement** | `Company Update` → `Buy back` / `Public Announcement-Buyback of Shares` | `Corporate Announcements` | `buyback`, `buy back`, `tender offer` |
| 4 | **Acquisition / Merger / Demerger** | `Company Update` → `Acquisition` / `Amalgamation/ Merger` / `De-merger` / `Slump Sale` | `Corporate Announcements` | `acquisition`, `acquire`, `merger`, `amalgamation`, `demerger`, `slump sale` |
| 5 | **Large Order / Contract Won** | `Company Update` → `Award of Order / Receipt of Order` / `Bagging/Receiving of orders/contracts` | `Corporate Announcements` | `order received`, `order won`, `contract awarded`, `bagged order`, `LOI received` |
| 6 | **Credit Rating Change** | `Company Update` → `Credit Rating` | `Corporate Announcements` | `credit rating`, `rating upgrade`, `rating downgrade`, `CRISIL`, `ICRA`, `CARE`, `India Ratings`, `Brickwork` |
| 7 | **Management Change (CEO/CFO/MD)** | `Company Update` → `Resignation of CEO` / `Appointment of MD` / `Change in Directorate` | `Corporate Announcements` | `resignation.*CEO`, `resignation.*CFO`, `resignation.*MD`, `appointment.*CEO`, `change in management` |
| 8 | **Insolvency / CIRP / Default** | `Company Update` → `Corporate Insolvency Resolution Process` / `Initiation of CIRP` | `Corporate Announcements` | `insolvency`, `CIRP`, `NCLT`, `default`, `NPA`, `winding-up`, `liquidation` |
| 9 | **Fundraising (QIP/Rights/Preferential)** | `Company Update` → `Qualified Institutional Placement` / `Preferential Issue` / `Raising of Funds` | `Corporate Announcements` | `QIP`, `qualified institutional`, `preferential issue`, `rights issue`, `raising of funds`, `fund raising` |
| 10 | **Open Offer / Takeover** | `Company Update` → `Open Offer` / `Public Announcement-Open Offer` | `Corporate Announcements` / `Shareholding` | `open offer`, `takeover`, `substantial acquisition` |

### ⚠️ Priority 2 — Medium Impact (Monitor Every 5–15 Minutes)

These are important but their price effect is more gradual.

| # | News Type | BSE Category → Subcategory | NSE Category | Keywords to Match |
| :--- | :--- | :--- | :--- | :--- |
| 11 | **Joint Venture** | `Company Update` → `Joint Venture` | `Corporate Announcements` | `joint venture`, `JV`, `collaboration agreement` |
| 12 | **Product Launch / Capacity** | `Company Update` → `Product launch` / `Capacity addition` | `Corporate Announcements` | `product launch`, `capacity addition`, `new plant`, `expansion`, `commissioning` |
| 13 | **Regulatory Approval / License** | `Company Update` → `Key licenses/regulatory approvals` | `Corporate Announcements` | `FDA approval`, `ANDA`, `regulatory approval`, `RBI approval`, `SEBI approval`, `license` |
| 14 | **Investor Presentation / Earnings Call** | `Company Update` → `Investor Presentation` / `Earnings Call Transcript` / `Analyst / Investor Meet` | `Corporate Announcements` | `investor presentation`, `earnings call`, `analyst meet`, `con call`, `conference call` |
| 15 | **Promoter Pledge / Insider Trading** | `Insider Trading / SAST` → Various SAST Reg disclosures | `Shareholding Patterns` / `Insider Trading` | `pledge`, `encumbrance`, `promoter`, `insider`, `SAST`, `Reg. 29`, `Reg. 31` |
| 16 | **Strike / Disruption** | `Company Update` → `Strike` / `Disruption of operations` | `Corporate Announcements` | `strike`, `lockout`, `disruption`, `force majeure`, `shutdown` |
| 17 | **Clarification of News** | `Company Update` → `Clarification` / `Clarification of News Item` | `Corporate Announcements` | `clarification`, `response to query`, `media report` |
| 18 | **Record Date / Book Closure** | `Corp. Action` → `Record Date` / `Book Closure` OR `Company Update` → `Declaration of Book Closure/ Record Date` | `Corporate Actions` | `record date`, `book closure`, `ex-date` |

### ℹ️ Priority 3 — Low Impact (Can Batch Process Daily)

These are routine compliance filings. Monitor once daily or ignore entirely.

| # | News Type | BSE Category → Subcategory | Keywords |
| :--- | :--- | :--- | :--- |
| 19 | Trading Window Closure | `Insider Trading / SAST` → `Closure of Trading Window` | `trading window` |
| 20 | Loss of Share Certificate | `Company Update` → `Loss of Share Certificate...` | `loss of certificate`, `duplicate` |
| 21 | Address / Name Change | `Company Update` → `Change of Name` / `Change in Registered Office` | `change of name`, `registered office` |
| 22 | Compliance Certificates | `Company Update` → Various `Reg. XX` filings | `compliance certificate`, `PCS certificate` |
| 23 | Annual Reports / BRSR | `Other` → `Reg. 34 (1) Annual Report` / `BRSR` | `annual report`, `sustainability` |
| 24 | Corporate Governance | `Other` → Various governance filings | `corporate governance`, `governance report` |

---

## 3. Recommended Fetching Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    POLLING SCHEDULER                     │
│  Result Season: Every 30s (3:30 PM – 11:59 PM)         │
│  Normal Days:   Every 5 min                             │
└────────────┬──────────────────────┬─────────────────────┘
             │                      │
     ┌───────▼───────┐     ┌───────▼───────┐
     │   BSE Fetcher  │     │  NSE Fetcher   │
     │  (API + Headers)│     │ (Session Cookies│
     │                │     │  + Headers)     │
     └───────┬───────┘     └───────┬────────┘
             │                      │
     ┌───────▼──────────────────────▼───────┐
     │         NORMALIZER & CLASSIFIER       │
     │  - Map fields to unified schema       │
     │  - Apply keyword regex classifier     │
     │  - Assign impact_level (H/M/L)        │
     │  - Generate dedup key (ISIN+Period)   │
     └───────────────┬──────────────────────┘
                     │
     ┌───────────────▼──────────────────────┐
     │         DEDUPLICATION LAYER           │
     │  - Check if ISIN+Period exists        │
     │  - Mark first_source (BSE or NSE)     │
     │  - Skip duplicate alerts              │
     └───────────────┬──────────────────────┘
                     │
     ┌───────────────▼──────────────────────┐
     │         ACTION / ALERT ENGINE         │
     │  Priority 1 → Immediate alert        │
     │  Priority 2 → Queue for analysis     │
     │  Priority 3 → Daily batch log        │
     └──────────────────────────────────────┘
```

---

## 4. BSE Category Values for API Filtering

When querying the BSE `AnnGetData` API, you can pass specific `strCat` values to filter by category. Based on the BSE website dropdown, the known values are:

| `strCat` Value | Category Name |
| :--- | :--- |
| `-1` | All Categories |
| `Insider Trading / SAST` | Insider Trading / SAST |
| `Result` | Result |
| `AGM/EGM` | AGM/EGM |
| `Board Meeting` | Board Meeting |
| `Company Update` | Company Update |
| `Corp. Action` | Corp. Action |
| `New Listing` | New Listing |
| `Other` | Other |

> [!TIP]
> For maximum coverage with minimum API calls, fetch with `strCat = -1` (all categories) and then classify locally using keyword matching. This ensures you never miss announcements that are miscategorized by the filing company.
