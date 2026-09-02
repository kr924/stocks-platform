import React, { useState, useEffect, useMemo, useRef, useCallback } from "react";
import { Chart } from "./Chart";
import {
  Zap,
  ShieldAlert,
  Play,
  Square,
  Plus,
  Trash2,
  TrendingUp,
  TrendingDown,
  RefreshCw,
  CheckCircle,
  XCircle,
  Clock,
  Cpu,
  ShoppingBag,
  DollarSign,
  AlertTriangle,
  Settings,
  ExternalLink,
  Calendar,
  Sparkles,
  ChevronRight,
  Pause,
  Bell,
  BellOff
} from "lucide-react";

const API_BASE = import.meta.env.VITE_API_BASE || (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1" ? "http://localhost:8000" : "");

/**
 * A financial result that landed for a stock with no armed config.
 * The user is prompted to place an order; AI analysis follows.
 */
interface DigestCompany {
  symbol: string;
  company_name: string | null;
  exchange: string;
  tracking_ref: string | null;
  announced_at: string | null;
  ai_requested_at: string | null;
  ai_completed_at: string | null;
  deferred: boolean;
  analysed: boolean;
  verdict: string;
  title: string;
  attachment_url: string | null;
  screener: { ok: boolean; quarter: string | null; source_url: string | null; error: string | null };
  comparison: {
    label: string; ai_text: string; actual: number | null;
    ai_yoy: string; actual_yoy: number | null; actual_qoq: number | null;
    match: boolean | null; diff_pct: number | null;
  }[];
  ai_summary: string | null;
  future_growth_outlook: string | null;
  future_projected_numbers: string | null;
  broker_estimates: string | null;
  screener_signal?: { label: string; tone: string; reason: string } | null;
  all_positive?: boolean;
}

interface MetricCell {
  current_qtr: string;
  last_year_same_qtr: string;
  yoy_change_pct: string;
  qoq_change_pct: string;
  estimated: string;
}

type MetricGrid = Record<string, MetricCell>;

interface TradePosition {
  config_id: number;
  status: string;
  quantity: number | null;
  buy_price: number | null;
  sell_price: number | null;
  pnl: number | null;
  can_sell: boolean;
}

interface PendingResult {
  id: number;
  position: TradePosition | null;
  symbol: string;
  company_name: string | null;
  tracking_ref: string | null;
  trade_date: string | null;
  deferred?: boolean;
  price_at_announcement: number | null;
  kind?: string;
  announced_at: string | null;
  ingested_at: string | null;
  alert_sent_at: string | null;
  ai_requested_at: string | null;
  ai_completed_at: string | null;
  instrument_key: string | null;
  exchange: string;
  title: string;
  description: string | null;
  attachment_url: string | null;
  event_time: string | null;
  status: string;
  ai_status: string;
  ai_log_id: number | null;
  config_id: number | null;
  created_at: string | null;
  ai_analysis: TradeAILog | null;
  // Price refreshing is suspended for this row. It stays listed and keeps the
  // last price it had; it is simply skipped when quotes are collected.
  paused?: boolean;
  // Screener's published figures, folded onto the prompt so the panel carries
  // what the Results Digest used to show in a second list of the same rows.
  // Null on impact news, which has no quarter to publish.
  screener?: { ok: boolean; quarter: string | null; prev_quarter?: string | null;
               source_url: string | null; error: string | null } | null;
  screener_signal?: { label: string; tone: string; reason: string } | null;
  all_positive?: boolean;
  comparison?: DigestCompany["comparison"] | null;
  // The filed PDF read independently of the AI, with its confidence.
  extraction?: Extraction | null;
}

/**
 * Which tab a prompt belongs under.
 *
 * Results are the quarterly filings; the rest is impact news, split by what
 * actually happened. "other" is not a dumping ground for the unclassifiable —
 * it is buybacks, expansions, approvals and joint ventures, each a real kind
 * with too few rows on a normal day to deserve a tab of its own. Anything new
 * the classifier learns to emit lands there rather than disappearing.
 */
type PromptCategory = "results" | "order" | "merger" | "other";

const CATEGORY_OF: Record<string, PromptCategory> = {
  result: "results",
  order_win: "order",
  merger_acquisition: "merger",
};

function categoryOf(p: PendingResult): PromptCategory {
  return CATEGORY_OF[(p.kind || "result")] || "other";
}

const CATEGORY_LABEL: Record<PromptCategory, string> = {
  results: "Results",
  order: "Orders",
  merger: "Mergers",
  other: "Other",
};

/**
 * Models offered for a re-analysis, most economical first.
 *
 * A starting list, not a catalogue: OpenRouter adds models faster than a
 * hardcoded list can follow, which is why "Other" takes a free-text id. A model
 * id that does not exist fails on the one filing it was tried on and leaves the
 * configured default untouched.
 */
const AI_MODELS: { id: string; label: string }[] = [
  { id: "", label: "Configured default" },
  { id: "google/gemini-2.5-flash-lite", label: "Gemini 2.5 Flash Lite — cheapest capable" },
  { id: "google/gemini-2.5-flash", label: "Gemini 2.5 Flash — stronger" },
  { id: "anthropic/claude-haiku-4.5", label: "Claude Haiku 4.5" },
  { id: "openai/gpt-4o-mini", label: "GPT-4o mini" },
  { id: "__custom__", label: "Other — type an id…" },
];

interface TradeConfig {
  id: number;
  symbol: string;
  instrument_key: string | null;
  purchase_date: string;
  quantity: number;
  stoploss_pct: number;
  stoploss_type: string;
  broker: string;
  order_type: string;
  limit_price: number | null;
  ai_provider: string;
  status: string;
  is_active: boolean;
  trigger_subject: string;
  buy_price: number | null;
  sell_price: number | null;
  pnl: number | null;
  notes: string | null;
  created_at: string;
  triggered_at: string | null;
  bought_at: string | null;
  sold_at: string | null;
}

interface TradeOrder {
  id: number;
  config_id: number;
  symbol: string;
  side: string;
  quantity: number;
  order_type: string;
  limit_price: number | null;
  price: number | null;
  stoploss_price: number | null;
  broker: string;
  broker_order_id: string | null;
  broker_response: string | null;
  status: string;
  error_message: string | null;
  created_at: string;
  filled_at: string | null;
}

/** Order-book quantity in Indian notation, as the tracker writes it. */
const fmtQty = (qty: number): string => {
  if (!qty || qty <= 0) return "0";
  if (qty >= 10000000) return `${(qty / 10000000).toFixed(2)}Cr`;
  if (qty >= 100000) return `${(qty / 100000).toFixed(2)}L`;
  if (qty >= 1000) return `${(qty / 1000).toFixed(1)}K`;
  return qty.toString();
};

// Row order is fixed so the grid reads the same everywhere it appears.
const METRIC_ROWS: { key: string; label: string }[] = [
  { key: "revenue", label: "Revenue" },
  { key: "expenses", label: "Expenses" },
  { key: "other_income", label: "Other Income" },
  { key: "pat", label: "Profit (PAT)" },
  { key: "ebitda", label: "EBITDA" },
];

// Column order mirrors the backend grid: the current print, the year-ago figure
// it is measured against, the two changes, then the broker estimate. Estimates
// exist only when a research house published one, so they usually read NA.
const METRIC_COLS: { key: keyof MetricCell; label: string }[] = [
  { key: "current_qtr", label: "Current Qtr" },
  { key: "last_year_same_qtr", label: "YoY" },
  { key: "yoy_change_pct", label: "YoY %" },
  { key: "qoq_change_pct", label: "QoQ %" },
  { key: "estimated", label: "Estimated" },
];

interface TradeAILog {
  id: number;
  config_id: number | null;
  symbol: string;
  provider: string;
  prompt_summary: string | null;
  ai_sentiment: string | null;
  ai_impact_score: number | null;
  ai_summary: string | null;
  nse_event_title: string | null;
  created_at: string;
  revenue?: string;
  expenses?: string;
  operating_profit?: string;
  pbt?: string;
  other_income?: string;
  pat_yoy?: string;
  growth_projection?: string;
  broker_estimates?: string;
  ai_suggestion?: string;
  attachment_url?: string;
  flow_used?: string;
  company_name?: string | null;
  tracking_ref?: string | null;
  ai_requested_at?: string | null;
  ai_completed_at?: string | null;
  metrics?: MetricGrid | null;
  future_growth_outlook?: string | null;
  future_projected_numbers?: string | null;
  extraction_ok?: boolean;
  validation?: { issues: string[]; hard_failures: number; reconciled: number; trustworthy: boolean } | null;
}

/** One labelled readout in the quote strip, matching the tracker's column head. */
function QuoteCell({ label, title, children }: { label: string; title?: string; children: React.ReactNode }) {
  return (
    <span title={title} style={{ display: "flex", flexDirection: "column", lineHeight: 1.15, gap: "2px" }}>
      <span style={{ fontSize: "9px", color: "var(--text-faint)", letterSpacing: "0.3px", whiteSpace: "nowrap" }}>{label}</span>
      {children}
    </span>
  );
}

const pctTone = (v: number | null | undefined) =>
  v == null ? "var(--text-faint)" : v > 0 ? "var(--positive)" : v < 0 ? "var(--negative)" : "var(--text-secondary)";
const pctText = (v: number | null | undefined) =>
  v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;
const rupees = (v: number | null | undefined) =>
  !v || v <= 0 ? "—" : `₹${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

/**
 * The stock tracker's watchlist readout, on a result prompt.
 *
 * Same columns and same arithmetic as the watchlist table — LTP, day change,
 * previous close, day high, the composite buy/sell split, order-book
 * quantities and the 2-minute signal — so a stock reads identically wherever
 * it appears, and the order decision does not need a second tab open.
 *
 * The one addition is the move since announcement, which is what decides
 * whether there is still a trade here: by the time a result reaches this panel
 * the market has often already repriced, and the day's change does not show it.
 */
function ResultQuoteStrip({ p, quote, signal, flash }: {
  p: PendingResult;
  quote: any;
  signal: { recommendation: string; confidence: string; tone: "up" | "down" | "neutral"; explanation: string };
  flash?: "up" | "down";
}) {
  const ltp: number = quote?.last_price || 0;
  const base = p.price_at_announcement;
  const dayChange: number | undefined = quote?.change;
  const prevClose: number = quote?.close || 0;
  const dayHigh: number = quote?.high || 0;
  const hasQuote = ltp > 0;

  const sinceAnnounce = hasQuote && base ? ((ltp - base) / base) * 100 : null;

  const buyPct = quote?.depth_buy_pct !== undefined ? Math.round(quote.depth_buy_pct) : null;
  const buyQty: number = quote?.total_buy_qty || 0;
  const sellQty: number = quote?.total_sell_qty || 0;
  const totalQty = buyQty + sellQty;

  const sigColor = signal.tone === "up" ? "var(--positive-strong)"
    : signal.tone === "down" ? "var(--negative-strong)" : "var(--warning)";
  const sigBg = signal.tone === "up" ? "rgba(63, 191, 135, 0.12)"
    : signal.tone === "down" ? "rgba(240, 115, 111, 0.12)" : "rgba(224, 163, 62, 0.12)";
  const sigBorder = signal.tone === "up" ? "rgba(63, 191, 135, 0.3)"
    : signal.tone === "down" ? "rgba(240, 115, 111, 0.3)" : "rgba(224, 163, 62, 0.3)";

  const num = { fontSize: "13px", fontWeight: 700, fontVariantNumeric: "tabular-nums" as const };

  return (
    <div style={{
      display: "flex", alignItems: "center", gap: "16px", flexWrap: "wrap",
      padding: "8px 10px", borderRadius: "8px",
      background: "rgba(10, 13, 18, 0.45)", border: "1px solid rgba(255,255,255,0.06)",
    }}>
      <span style={{
        fontSize: "16px", fontWeight: 800, fontVariantNumeric: "tabular-nums",
        color: flash === "up" ? "var(--positive-strong)" : flash === "down" ? "var(--negative-strong)" : "var(--text-primary)",
        transition: "color 0.3s",
      }}>
        {rupees(ltp)}
      </span>

      <QuoteCell label="SINCE RESULT" title="Move since the result was announced — what is left of the reaction">
        <span style={{ ...num, fontWeight: 800, color: pctTone(sinceAnnounce) }}>
          {pctText(sinceAnnounce)}
        </span>
      </QuoteCell>

      <QuoteCell label="CHANGE">
        <span style={{ ...num, color: pctTone(dayChange) }}>{pctText(dayChange)}</span>
      </QuoteCell>

      <QuoteCell label="PREV CLOSE">
        <span style={{ ...num, fontWeight: 600, color: "var(--text-secondary)" }}>{rupees(prevClose)}</span>
      </QuoteCell>

      <QuoteCell label="DAY HIGH">
        <span style={{ ...num, fontWeight: 600, color: "var(--positive-strong)" }}>{rupees(dayHigh)}</span>
      </QuoteCell>

      <QuoteCell label="SENTIMENT (B/S)" title="85% order-book depth, 15% where the price sits in the day's range — the tracker's composite">
        {buyPct == null ? (
          <span style={{ ...num, color: "var(--text-faint)" }}>—</span>
        ) : (
          <span style={{ display: "flex", flexDirection: "column", gap: "2px", width: "92px" }}>
            <span style={{ display: "flex", justifyContent: "space-between", fontSize: "9px", fontWeight: 800 }}>
              <span style={{ color: "var(--positive)" }}>{buyPct}% B</span>
              <span style={{ color: "var(--negative)" }}>{100 - buyPct}% S</span>
            </span>
            <span style={{ height: "5px", background: "var(--negative)", borderRadius: "3px", overflow: "hidden", display: "flex" }}>
              <span style={{ width: `${buyPct}%`, background: "var(--positive)" }} />
            </span>
          </span>
        )}
      </QuoteCell>

      <QuoteCell label="BUY/SELL QTY">
        {totalQty === 0 ? (
          <span style={{ ...num, color: "var(--text-faint)" }}>—</span>
        ) : (
          <span style={{ display: "flex", flexDirection: "column", gap: "2px", width: "104px" }}>
            <span style={{ display: "flex", justifyContent: "space-between", fontSize: "9px", fontWeight: 700 }}>
              <span style={{ color: "var(--positive)" }}>{fmtQty(buyQty)}</span>
              <span style={{ color: "var(--negative)" }}>{fmtQty(sellQty)}</span>
            </span>
            <span style={{ height: "4px", background: "var(--negative)", borderRadius: "2px", overflow: "hidden", display: "flex" }}>
              <span style={{ width: `${(buyQty / totalQty) * 100}%`, background: "var(--positive)" }} />
            </span>
            <span style={{ fontSize: "8px", color: "var(--text-faint)", fontWeight: 600 }}>Σ {fmtQty(totalQty)}</span>
          </span>
        )}
      </QuoteCell>

      <QuoteCell label="2M SIGNAL">
        <span
          title={`${signal.explanation}\n\nReliability Warning:\nThis micro-trend is based on high-frequency order-book data. Useful for near-term momentum, but vulnerable to spoofing. Use as helper, not standalone decision.`}
          style={{
            display: "inline-flex", flexDirection: "column", alignItems: "center",
            padding: "3px 8px", borderRadius: "6px", cursor: "help",
            background: sigBg, border: `1px solid ${sigBorder}`, color: sigColor,
            fontSize: "10px", fontWeight: 800, lineHeight: 1.2,
          }}>
          <span>{signal.recommendation}</span>
          <span style={{ fontSize: "7px", opacity: 0.8, fontWeight: 400 }}>CONF: {signal.confidence}</span>
        </span>
      </QuoteCell>

      {base != null && (
        <span style={{ fontSize: "10px", color: "var(--text-faint)", whiteSpace: "nowrap" }}>
          at result {rupees(base)}
        </span>
      )}

      {!hasQuote && (
        <span title="No listing matched this symbol on either exchange, so there is no live quote to show"
          style={{ fontSize: "10px", color: "var(--text-faint)", whiteSpace: "nowrap" }}>
          no live quote
        </span>
      )}
    </div>
  );
}

/** Local time-of-day, or an em dash when the stage has not happened yet. */
function clockTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  return isNaN(d.getTime())
    ? "—"
    : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

/** Elapsed time between two stages, so latency is visible at a glance. */
function elapsed(from: string | null | undefined, to: string | null | undefined): string {
  if (!from || !to) return "";
  const norm = (s: string) => new Date(s.endsWith("Z") || s.includes("+") ? s : s + "Z").getTime();
  const ms = norm(to) - norm(from);
  if (!isFinite(ms) || ms < 0) return "";
  return ms < 90000 ? `+${Math.round(ms / 1000)}s` : `+${(ms / 60000).toFixed(1)}m`;
}

/**
 * The five stages a result passes through. Rendered as a single strip so a slow
 * step is obvious — the gap between "Loaded" and "AI sent" is where a backlog
 * shows up, and between "AI sent" and "AI received" is the model's own latency.
 */
function LifecycleStrip({ p }: { p: PendingResult }) {
  const stages: { label: string; at: string | null; prev: string | null }[] = [
    { label: "Announced", at: p.announced_at, prev: null },
    { label: "Loaded", at: p.ingested_at, prev: p.announced_at },
    { label: "Alerted", at: p.alert_sent_at, prev: p.ingested_at },
    { label: "AI sent", at: p.ai_requested_at, prev: p.alert_sent_at || p.ingested_at },
    { label: "AI received", at: p.ai_completed_at, prev: p.ai_requested_at },
  ];
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: "14px", marginTop: "8px", fontSize: "10px", color: "var(--text-muted)" }}>
      {stages.map(s => {
        const lag = elapsed(s.prev, s.at);
        return (
          <span key={s.label} style={{ whiteSpace: "nowrap" }}>
            <b style={{ color: "var(--text-secondary)", fontWeight: 600 }}>{s.label}</b>{" "}
            <span style={{ color: s.at ? "var(--text-primary)" : "var(--text-faint)", fontVariantNumeric: "tabular-nums" }}>
              {clockTime(s.at)}
            </span>
            {lag && <span style={{ color: "var(--text-faint)" }}> {lag}</span>}
          </span>
        );
      })}
    </div>
  );
}

/**
 * The fixed earnings grid. Cells the model could not extract read "NA" rather
 * than being blank, so a gap is visibly a gap rather than a rendering glitch.
 */
function MetricsTable({ metrics }: { metrics: MetricGrid | null | undefined }) {
  if (!metrics) return null;
  const na = (v: string | undefined) => !v || v.toUpperCase() === "NA";
  return (
    <div style={{ overflowX: "auto", marginTop: "8px" }}>
      <table style={{ borderCollapse: "collapse", fontSize: "11px", minWidth: "480px", width: "100%" }}>
        <thead>
          <tr>
            <th style={{ textAlign: "left", padding: "5px 8px", color: "var(--text-muted)", fontWeight: 600, borderBottom: "1px solid rgba(255,255,255,0.12)" }}></th>
            {METRIC_COLS.map(c => (
              <th key={c.key} style={{ textAlign: "right", padding: "5px 8px", color: "var(--text-muted)", fontWeight: 600, borderBottom: "1px solid rgba(255,255,255,0.12)", whiteSpace: "nowrap" }}>
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {METRIC_ROWS.map(r => (
            <tr key={r.key}>
              <td style={{ padding: "5px 8px", color: "var(--text-primary)", fontWeight: 600, whiteSpace: "nowrap", borderBottom: "1px solid rgba(255,255,255,0.04)" }}>
                {r.label}
              </td>
              {METRIC_COLS.map(c => {
                const val = metrics[r.key]?.[c.key];
                return (
                  <td key={c.key} style={{
                    padding: "5px 8px", textAlign: "right", whiteSpace: "nowrap",
                    borderBottom: "1px solid rgba(255,255,255,0.04)",
                    color: na(val) ? "var(--text-faint)" : "var(--text-primary)",
                    fontStyle: na(val) ? "italic" : "normal",
                  }}>
                    {val || "NA"}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}


/**
 * Consistency failures found in an extraction. Shown rather than merely acted
 * on, so a suspect filing can be checked against the source instead of just
 * being distrusted silently.
 */
function ValidationNotice({ v }: { v: TradeAILog["validation"] }) {
  if (!v || !v.issues || v.issues.length === 0) return null;
  return (
    <div style={{
      marginTop: "8px", padding: "8px 10px", borderRadius: "6px",
      background: "var(--warning-bg)", border: "1px solid var(--warning-border)",
    }}>
      <div style={{ fontSize: "10px", fontWeight: 700, color: "var(--warning)", marginBottom: "4px" }}>
        ⚠ FIGURES FAILED CONSISTENCY CHECKS — treat as unverified
      </div>
      {v.issues.slice(0, 4).map((issue, i) => (
        <div key={i} style={{ fontSize: "10px", color: "var(--text-secondary)", lineHeight: 1.5 }}>• {issue}</div>
      ))}
    </div>
  );
}

/** Verdict pill. "NA" is styled neutrally so it never reads as a call to act. */
/**
 * Our AI's figures against Screener's published ones.
 *
 * Lifted out of the Results Digest rather than written again: the two panels
 * were rendering the same PendingResultOrder rows, and a second copy of this
 * table is a second place for the columns to drift.
 */
function ScreenerComparison({ rows, quarter, analysed }: {
  rows: DigestCompany["comparison"]; quarter?: string | null; analysed: boolean;
}) {
  if (!rows || rows.length === 0) return null;
  const na = (v: any) => !v || String(v).toUpperCase() === "NA";
  const head = ["", "Our AI", `Screener${quarter ? ` (${quarter})` : ""}`, "Variance",
                "AI YoY", "Screener YoY", "Screener QoQ"];
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ borderCollapse: "collapse", fontSize: "11px", width: "100%", minWidth: "560px" }}>
        <thead>
          <tr>
            {head.map((h, i) => (
              <th key={i} style={{ textAlign: i === 0 ? "left" : "right", padding: "4px 8px", color: "var(--text-muted)", fontWeight: 600, fontSize: "10px", borderBottom: "1px solid var(--border-subtle)", whiteSpace: "nowrap" }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, ri) => (
            <tr key={ri}>
              <td style={{ padding: "4px 8px", fontWeight: 600, color: "var(--text-primary)", whiteSpace: "nowrap" }}>{row.label}</td>
              <td style={{ padding: "4px 8px", textAlign: "right", color: !analysed || na(row.ai_text) ? "var(--text-faint)" : "var(--text-primary)", fontStyle: !analysed || na(row.ai_text) ? "italic" : "normal", whiteSpace: "nowrap" }}>
                {!analysed ? "not analysed" : (row.ai_text || "NA")}
              </td>
              <td style={{ padding: "4px 8px", textAlign: "right", color: row.actual === null ? "var(--text-faint)" : "var(--text-primary)", whiteSpace: "nowrap" }}>
                {row.actual === null ? "—" : row.actual.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
              </td>
              <td style={{ padding: "4px 8px", textAlign: "right", whiteSpace: "nowrap", color: row.match === true ? "var(--positive)" : row.match === false ? "var(--negative)" : "var(--text-faint)" }}>
                {row.match === true ? "match" : row.match === false ? `${row.diff_pct! > 0 ? "+" : ""}${row.diff_pct}%` : "—"}
              </td>
              <td style={{ padding: "4px 8px", textAlign: "right", color: "var(--text-secondary)", whiteSpace: "nowrap" }}>{analysed ? (row.ai_yoy || "NA") : "—"}</td>
              <td style={{ padding: "4px 8px", textAlign: "right", whiteSpace: "nowrap", color: row.actual_yoy === null ? "var(--text-faint)" : row.actual_yoy > 0 ? "var(--positive)" : row.actual_yoy < 0 ? "var(--negative)" : "var(--text-secondary)" }}>
                {row.actual_yoy === null ? "—" : `${row.actual_yoy > 0 ? "+" : ""}${row.actual_yoy.toFixed(2)}%`}
              </td>
              <td style={{ padding: "4px 8px", textAlign: "right", whiteSpace: "nowrap", color: row.actual_qoq == null ? "var(--text-faint)" : row.actual_qoq > 0 ? "var(--positive)" : row.actual_qoq < 0 ? "var(--negative)" : "var(--text-secondary)" }}>
                {row.actual_qoq == null ? "—" : `${row.actual_qoq > 0 ? "+" : ""}${row.actual_qoq.toFixed(2)}%`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}


interface QuarterPoint {
  quarter: string;
  year_ago_quarter: string | null;
  [metric: string]: string | number | null;
}

interface QuarterHistory {
  ok: boolean; symbol: string; source_url: string; unit: string;
  metrics: { key: string; label: string }[];
  points: QuarterPoint[];
  error: string;
}

/** Compact ₹ crore, so eight of them fit on one line without wrapping. */
function crore(v: number | null): string {
  if (v == null) return "—";
  const a = Math.abs(v);
  if (a >= 100000) return `${(v / 1000).toFixed(0)}k`;
  if (a >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (a >= 10) return v.toFixed(0);
  return v.toFixed(2);
}

/**
 * One metric across the reported quarters: a bar per quarter, the figure under
 * it, and the year-on-year change under that.
 *
 * The year-earlier bar is gone. It doubled the ink to say something the
 * percentage already says, and with eight metrics on screen the comparison that
 * matters is down a column — how this quarter's revenue, margin and profit move
 * together — not between two bars of one metric.
 *
 * Bars are scaled per metric, never across metrics: interest and revenue differ
 * by three orders of magnitude, so a shared scale would flatten every line
 * except the top one into nothing.
 */
function MetricStrip({ points, metric, label }: {
  points: QuarterPoint[]; metric: string; label: string;
}) {
  const val = (p: QuarterPoint) => p[metric] as number | null;
  const pct = (p: QuarterPoint) => p[`${metric}_yoy_pct`] as number | null;

  const values = points.map(val).filter((v): v is number => v != null);
  if (!values.length) return null;

  // Expenses, interest and depreciation rising is not good news, so the bars
  // for those are neutral: only the lines where up is genuinely better carry
  // the green/red reading.
  const directional = !["expenses", "interest", "depreciation"].includes(metric);

  const hi = Math.max(...values, 0);
  const lo = Math.min(...values, 0);
  const span = hi - lo || 1;
  const H = 44;
  const zero = ((hi - 0) / span) * H;

  return (
    <div style={{ marginBottom: "14px" }}>
      <div style={{ fontSize: "10px", fontWeight: 700, color: "var(--text-secondary)",
                    letterSpacing: "0.3px", marginBottom: "4px" }}>
        {label}
      </div>
      <div style={{ display: "flex", gap: "3px" }}>
        {points.map(p => {
          const v = val(p), c = pct(p);
          const colour = !directional ? "var(--text-muted)"
            : c == null ? "var(--text-faint)"
            : c > 0 ? "var(--positive)" : c < 0 ? "var(--negative)" : "var(--text-muted)";
          const h = v == null ? 0 : Math.max(Math.abs(((hi - v) / span) * H - zero), 1.5);
          const top = v == null ? zero : Math.min(((hi - v) / span) * H, zero);
          return (
            <div key={p.quarter} style={{ flex: 1, minWidth: 0, textAlign: "center" }}
                 title={`${p.quarter}: ${v == null ? "not reported" : v.toLocaleString()} ₹ Cr`}>
              <div style={{ height: `${H}px`, position: "relative" }}>
                <div style={{
                  position: "absolute", left: "18%", right: "18%",
                  top: `${top}px`, height: `${h}px`,
                  background: colour, opacity: v == null ? 0.25 : 0.85, borderRadius: "1.5px",
                }} />
                {/* Zero line, drawn only where the series actually crosses it. */}
                {lo < 0 && (
                  <div style={{ position: "absolute", left: 0, right: 0, top: `${zero}px`,
                                borderTop: "1px solid var(--text-faint)", opacity: 0.5 }} />
                )}
              </div>
              <div style={{ fontSize: "10px", fontWeight: 700, color: "var(--text-primary)",
                            fontVariantNumeric: "tabular-nums", marginTop: "3px",
                            whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                {crore(v)}
              </div>
              <div style={{ fontSize: "9px", fontWeight: 700, fontVariantNumeric: "tabular-nums",
                            whiteSpace: "nowrap",
                            color: c == null ? "var(--text-faint)"
                              : !directional ? "var(--text-muted)"
                              : c > 0 ? "var(--positive)" : c < 0 ? "var(--negative)" : "var(--text-muted)" }}>
                {c == null ? "—" : `${c > 0 ? "+" : ""}${c.toFixed(0)}%`}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}


/**
 * Contain a render error to the card that caused it.
 *
 * React unmounts the whole tree when a render throws, so one bad row took the
 * entire dashboard to a blank screen — which is what a missing component
 * reference did here, and what any future shape mismatch in a stored payload
 * would do again. A prompt panel reads from JSON written by a background job;
 * that payload will not always match what this file expects.
 *
 * The failing card shows what went wrong. Everything around it keeps working,
 * including the order forms, which is the part that must never disappear
 * because of a display bug.
 */
class CardBoundary extends React.Component<
  { label: string; children: React.ReactNode },
  { message: string | null }
> {
  constructor(props: { label: string; children: React.ReactNode }) {
    super(props);
    this.state = { message: null };
  }
  static getDerivedStateFromError(err: unknown) {
    return { message: err instanceof Error ? err.message : String(err) };
  }
  componentDidCatch(err: unknown) {
    console.error("Card render failed:", err);
  }
  render() {
    if (this.state.message === null) return this.props.children;
    return (
      <div style={{
        padding: "12px 14px", borderRadius: "8px", fontSize: "11px", lineHeight: 1.55,
        background: "rgba(240, 115, 111, 0.08)", border: "1px solid rgba(240, 115, 111, 0.3)",
        color: "var(--text-secondary)",
      }}>
        <b style={{ color: "var(--negative-strong)" }}>{this.props.label} could not be displayed.</b>
        <div style={{ marginTop: "4px", color: "var(--text-muted)" }}>{this.state.message}</div>
        <div style={{ marginTop: "4px", color: "var(--text-faint)" }}>
          The rest of the panel is unaffected — the filing and its order form are still above.
        </div>
      </div>
    );
  }
}


interface Extraction {
  ok: boolean;
  engine?: string; page?: number | null; basis?: string; unit?: string;
  quarter?: string; screener_quarter?: string; covers_screener_quarter?: boolean;
  metrics?: Record<string, {
    current_qtr: number | null; prev_qtr: number | null; year_ago: number | null;
    yoy_change_pct: number | null; qoq_change_pct: number | null;
  }>;
  comparisons?: {
    metric: string; pdf: number | null; screener: number | null;
    gap_pct: number | null; agrees: boolean | null; note?: string;
  }[];
  confidence: {
    tier: string; pct: number | null; reason: string; clean?: boolean;
    status?: string; flags?: string[];
    metrics_agreeing?: number; metrics_compared?: number;
  };
  error?: string;
}

/**
 * How much of the extraction to believe, as a tier and a percentage.
 *
 * The percentages are the accuracy measured for each tier over 1,926 filings,
 * not a feeling: a clean read confirmed by a second source is a different claim
 * from a flagged read nothing corroborates, and showing both as "extracted"
 * would be the lie worth avoiding.
 */
function ConfidenceBadge({ c }: { c: Extraction["confidence"] }) {
  // Defensive: this renders from a stored payload, and an older or truncated
  // row must degrade to "unknown" rather than take the whole panel down with a
  // ReferenceError — which is exactly what a blank screen is.
  const tier = c?.tier || "UNKNOWN";
  const pct = c?.pct ?? null;
  const tone =
    pct == null ? "na"
      : pct >= 80 ? "good"
      : pct >= 50 ? "warn"
      : "bad";
  const colour = tone === "good" ? "var(--positive)"
    : tone === "warn" ? "var(--warning)"
    : tone === "bad" ? "var(--negative)" : "var(--text-muted)";
  const bg = tone === "good" ? "rgba(63, 191, 135, 0.14)"
    : tone === "warn" ? "rgba(224, 163, 62, 0.14)"
    : tone === "bad" ? "rgba(240, 115, 111, 0.14)" : "var(--surface-2)";
  return (
    <span title={c?.reason || ""} style={{
      display: "inline-flex", alignItems: "baseline", gap: "6px",
      fontSize: "10px", fontWeight: 700, letterSpacing: "0.3px",
      padding: "3px 9px", borderRadius: "5px", color: colour, background: bg,
      border: `1px solid ${colour}33`,
    }}>
      {tier.replace(/_/g, " ")}
      <b style={{ fontSize: "12px", fontVariantNumeric: "tabular-nums" }}>
        {pct == null ? "—" : `${pct}%`}
      </b>
    </span>
  );
}


function VerdictBadge({ verdict }: { verdict?: string | null }) {
  const v = (verdict || "NA").toUpperCase();
  const isNA = v === "NA";
  const positive = /BEAT|^BUY$/.test(v);
  const negative = /MISS|^SELL$/.test(v);
  const color = isNA ? "var(--text-muted)" : positive ? "var(--positive-strong)" : negative ? "var(--negative-strong)" : "var(--warning)";
  const bg = isNA ? "rgba(125, 135, 153,0.12)" : positive ? "rgba(63, 191, 135,0.15)"
    : negative ? "rgba(240, 115, 111,0.15)" : "rgba(224, 163, 62,0.15)";
  return (
    <span
      title={isNA ? "Figures could not be extracted — no directional call is given" : undefined}
      style={{ fontSize: "10px", fontWeight: 800, padding: "2px 8px", borderRadius: "4px", color, background: bg, whiteSpace: "nowrap" }}
    >
      {v}
    </span>
  );
}

interface UpcomingEarningsItem {
  id: number;
  symbol: string;
  title?: string;
  meeting_date: string;
  display_date?: string;
  purpose: string;
  created_at?: string;
  return_1y?: string;
  returns_1y?: string;
}

interface AutoTradingSettings {
  custom_api_url: string;
  premium_openrouter_api_key: string;
  premium_openrouter_model: string;
}

interface PollerStatus {
  running: boolean;
  armed_count: number;
  last_poll_at: string | null;
  polls_total: number;
  triggers_total: number;
  last_error: string | null;
}


export function TradingDashboard() {
  const [configs, setConfigs] = useState<TradeConfig[]>([]);
  const [orders, setOrders] = useState<TradeOrder[]>([]);
  const [aiLogs, setAiLogs] = useState<TradeAILog[]>([]);
  const [nseAnnouncements, setNseAnnouncements] = useState<any[]>([]);
  const [pendingResults, setPendingResults] = useState<PendingResult[]>([]);
  const [pendingSearch, setPendingSearch] = useState("");
  // AI detail is collapsed by default: on a heavy results day this panel carries
  // 170+ cards, and a five-row metric table on each buries the order controls.
  const [expandedResults, setExpandedResults] = useState<Record<number, boolean>>({});
  const [pendingCategory, setPendingCategory] = useState<PromptCategory>("results");
  // Per-card: which extraction tab is showing, and which model a re-analysis
  // would use. Keyed by prompt id so opening one card does not reset another.
  const [resultTab, setResultTab] = useState<Record<number, "ai" | "ocr">>({});
  const [reanalyseModel, setReanalyseModel] = useState<Record<number, string>>({});
  const [customModel, setCustomModel] = useState<Record<number, string>>({});
  const [aiLogSearch, setAiLogSearch] = useState("");
  const [aiLogDateFrom, setAiLogDateFrom] = useState("");
  const [aiLogDateTo, setAiLogDateTo] = useState("");
  // How many rows on the shown day are still waiting for Screener figures.
  // Screener is paced at roughly a company a second, so a heavy day arrives
  // over several minutes; the panel says so rather than showing gaps that look
  // like missing data.
  const [pendingBuilding, setPendingBuilding] = useState(0);
  // Per-prompt order form state, keyed by pending-result id
  const [resultOrderForm, setResultOrderForm] = useState<Record<number, {
    quantity: number; order_type: string; limit_price: string;
    stoploss_pct: number; stoploss_type: string; broker: string;
  }>>({});
  const [pollerStatus, setPollerStatus] = useState<PollerStatus | null>(null);
  const [upcomingEarnings, setUpcomingEarnings] = useState<UpcomingEarningsItem[]>([]);
  const [aiSettings, setAiSettings] = useState<AutoTradingSettings>({
    custom_api_url: "http://localhost:11434/api/generate",
    premium_openrouter_api_key: "",
    premium_openrouter_model: "anthropic/claude-3.5-sonnet",
  });
  const [showSettingsModal, setShowSettingsModal] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);

  const [loading, setLoading] = useState(true);
  const [showAddForm, setShowAddForm] = useState(false);

  // Direct buy: no target, no arming, no filing to wait for.
  const [showBuyForm, setShowBuyForm] = useState(false);
  const [buyBusy, setBuyBusy] = useState(false);
  const [buyForm, setBuyForm] = useState({
    symbol: "", quantity: 1, order_type: "MARKET", limit_price: "",
    stoploss_pct: 2, stoploss_type: "software", broker: "upstox",
  });

  /**
   * NSE continuous trading, 09:15-15:30 IST on a weekday.
   *
   * Tighter than the 09:00 the results panel uses: the pre-open auction runs
   * 09:00-09:15 and does not take the plain market orders placed here. This
   * only decides what the button *says* — the backend decides what happens.
   */
  const canTradeNow = () => {
    try {
      const ist = new Date(new Date().toLocaleString("en-US", { timeZone: "Asia/Kolkata" }));
      if (ist.getDay() === 0 || ist.getDay() === 6) return false;
      const mins = ist.getHours() * 60 + ist.getMinutes();
      return mins >= 555 && mins <= 930;   // 09:15 to 15:30
    } catch {
      return true;
    }
  };

  const handleDirectBuy = async (e: React.FormEvent) => {
    e.preventDefault();
    const symbol = buyForm.symbol.trim().toUpperCase();
    if (!symbol) return;

    const when = canTradeNow()
      ? "This places a real order now."
      : "The market is closed, so this will be queued for the next open at 09:15 IST.";
    if (!window.confirm(`Buy ${buyForm.quantity} ${symbol} (${buyForm.order_type})?\n\n${when}`)) return;

    setBuyBusy(true);
    try {
      const res = await fetch(`${API_BASE}/api/trading/buy-now`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol,
          quantity: buyForm.quantity,
          order_type: buyForm.order_type,
          limit_price: buyForm.order_type === "LIMIT" && buyForm.limit_price
            ? parseFloat(buyForm.limit_price) : null,
          stoploss_pct: buyForm.stoploss_pct,
          stoploss_type: buyForm.stoploss_type,
          broker: buyForm.broker,
        }),
      });
      const data = await res.json();
      if (!res.ok || data.success === false) {
        alert(`Buy failed for ${symbol}: ${data.message || data.detail || "Unknown error"}`);
      } else {
        alert(data.message || `Order placed for ${symbol}.`);
        setShowBuyForm(false);
        setBuyForm(prev => ({ ...prev, symbol: "", limit_price: "" }));
      }
      await fetchData();
    } catch (err) {
      console.error("Direct buy failed:", err);
      alert(`Buy failed for ${symbol}. See console for details.`);
    } finally {
      setBuyBusy(false);
    }
  };
  const [autoArmOnSave, setAutoArmOnSave] = useState(true);
  const [hoveredOrder, setHoveredOrder] = useState<{ order: TradeOrder; x: number; y: number } | null>(null);

  // Chart Modal State
  const [chartSymbol, setChartSymbol] = useState<string | null>(null);
  const [chartInstrumentKey, setChartInstrumentKey] = useState<string | null>(null);
  const [chartPeriod, setChartPeriod] = useState<string>("1D");
  const [chartCandles, setChartCandles] = useState<any[]>([]);
  const [chartLoading, setChartLoading] = useState(false);
  const [actionLoading, setActionLoading] = useState<Record<string, boolean>>({});

  // Form State
  const todayStr = new Date().toISOString().split("T")[0];
  const [formData, setFormData] = useState({
    symbol: "",
    instrument_key: "",
    purchase_date: todayStr,
    quantity: 1,
    stoploss_pct: 2.0,
    stoploss_type: "software",
    broker: "upstox",
    order_type: "MARKET",
    limit_price: "",
    ai_provider: "groq",
    trigger_subject: "Outcome of Board Meeting",
    notes: ""
  });

  // Stock search state for form
  const [searchResults, setSearchResults] = useState<any[]>([]);
  const [searching, setSearching] = useState(false);

  // Live Stock Tracker Market Quotes Map State
  const [marketQuotesMap, setMarketQuotesMap] = useState<Record<string, any>>({});

  // Rolling Sentiment History Ref (Last 15 updates per symbol)
  const sentimentHistoryRef = useRef<Record<string, number[]>>({});
  // Order-book quantities over the same window — the 2m signal reads both.
  const quantityHistoryRef = useRef<Record<string, { buy: number[]; sell: number[] }>>({});
  // Last seen price per symbol, so a tick can be flashed green or red.
  const prevPricesRef = useRef<Record<string, number>>({});
  const [priceFlash, setPriceFlash] = useState<Record<string, "up" | "down">>({});

  // The quote poll runs from a single interval registered once. Reading the
  // lists through refs keeps it from fetching quotes for whatever was on screen
  // at mount — which was nothing, so no result prompt ever had a price.
  const pendingResultsRef = useRef<PendingResult[]>([]);
  const upcomingEarningsRef = useRef<UpcomingEarningsItem[]>([]);

  // Which trade date the results panel is showing. Quotes are only fetched for
  // today: a past day's prices are whatever they are now, which says nothing
  // about the decision that was in front of you then, and spends metered
  // requests to say it.
  // Prompts are kept for 30 days now that this panel is the only place they can
  // be read back, so the picker reaches as far as the retention does.
  const PANEL_RETENTION_DAYS = 30;
  const oldestStr = useMemo(() => {
    const d = new Date();
    d.setDate(d.getDate() - PANEL_RETENTION_DAYS);
    return d.toISOString().slice(0, 10);
  }, []);
  const [resultsDate, setResultsDate] = useState<string>(todayStr);
  const resultsDateRef = useRef<string>(todayStr);
  const isToday = resultsDate === todayStr;

  useEffect(() => {
    resultsDateRef.current = resultsDate;
    fetchData();
  }, [resultsDate]);

  /**
   * Desktop notification with a short tone when a result lands.
   *
   * A filing is only tradeable for as long as the session lasts, and this panel
   * is not always the focused tab. Both the permission prompt and the audio
   * context need a user gesture to start, which is what the toggle is for —
   * browsers ignore either if a background poll tries to open them.
   */
  const [alertsOn, setAlertsOn] = useState<boolean>(
    () => localStorage.getItem("resultAlertsOn") === "1"
  );
  const alertsOnRef = useRef(alertsOn);
  const seenResultIdsRef = useRef<Set<number> | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);

  useEffect(() => {
    alertsOnRef.current = alertsOn;
    localStorage.setItem("resultAlertsOn", alertsOn ? "1" : "0");
  }, [alertsOn]);

  const enableAlerts = async () => {
    if (alertsOn) {
      setAlertsOn(false);
      return;
    }
    try {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") {
        alert("Browser notifications are blocked for this site — enable them in the address-bar site settings.");
        return;
      }
      // Created inside the click so the browser lets it make sound later.
      audioCtxRef.current = audioCtxRef.current || new AudioContext();
      await audioCtxRef.current.resume();
      setAlertsOn(true);
    } catch (err) {
      console.error("Could not enable result alerts:", err);
    }
  };

  /** Two short notes — audible without being alarming on a heavy results day. */
  const playAlertTone = () => {
    const ctx = audioCtxRef.current;
    if (!ctx) return;
    try {
      [0, 0.18].forEach((offset, i) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.value = i === 0 ? 880 : 1174;
        gain.gain.setValueAtTime(0.0001, ctx.currentTime + offset);
        gain.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + offset + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + offset + 0.16);
        osc.connect(gain).connect(ctx.destination);
        osc.start(ctx.currentTime + offset);
        osc.stop(ctx.currentTime + offset + 0.18);
      });
    } catch (err) {
      console.error("Alert tone failed:", err);
    }
  };

  /**
   * Fire for filings that appeared since the last poll.
   *
   * The first poll after a reload only seeds the set — otherwise opening the
   * page on a day with 300 filings would fire 300 notifications.
   */
  const notifyNewResults = (rows: PendingResult[]) => {
    if (seenResultIdsRef.current === null) {
      seenResultIdsRef.current = new Set(rows.map(r => r.id));
      return;
    }
    const seen = seenResultIdsRef.current;
    const fresh = rows.filter(r => !seen.has(r.id));
    rows.forEach(r => seen.add(r.id));
    if (!fresh.length || !alertsOnRef.current) return;

    playAlertTone();
    // One notification for a batch: a results burst can carry dozens at once,
    // and dozens of toasts bury the thing they are announcing.
    if (fresh.length === 1) {
      const r = fresh[0];
      new Notification(`${r.symbol} — financial result`, {
        body: (r.company_name ? `${r.company_name}\n` : "") + (r.title || "").slice(0, 120),
        tag: `result-${r.id}`,
      });
    } else {
      new Notification(`${fresh.length} financial results just landed`, {
        body: fresh.slice(0, 8).map(r => r.symbol).join(", ") + (fresh.length > 8 ? "…" : ""),
        tag: "result-batch",
      });
    }
  };

  /**
   * Cash on hand, so a card can say what an order costs and what is left.
   * Kept in the browser: it is a planning figure the user types, not a balance
   * fetched from the broker, and it should not look like one.
   */
  const [availableFunds, setAvailableFunds] = useState<string>(
    () => localStorage.getItem("availableFunds") || ""
  );
  useEffect(() => {
    localStorage.setItem("availableFunds", availableFunds);
  }, [availableFunds]);

  // Upcoming earnings quotes can be paused independently, same reasoning as the
  // tracker's tables: the allowance is shared across every panel.
  const [earningsPaused, setEarningsPaused] = useState<boolean>(
    () => localStorage.getItem("earningsQuotesPaused") === "1"
  );
  const earningsPausedRef = useRef(earningsPaused);
  useEffect(() => {
    earningsPausedRef.current = earningsPaused;
    localStorage.setItem("earningsQuotesPaused", earningsPaused ? "1" : "0");
  }, [earningsPaused]);

  // Hover Sentiment Popover State
  const [hoveredSentiment, setHoveredSentiment] = useState<{
    symbol: string;
    name: string;
    buyPct: number;
    sellPct: number;
    buyQty: number;
    sellQty: number;
    changePct: number;
    ltp: number;
    prevClose: number;
    dayHigh: number;
    dayLow: number;
    x: number;
    y: number;
  } | null>(null);

  const handleSentimentMouseEnter = (e: React.MouseEvent, item: any, buyPct: number, buyQty: number, sellQty: number, changePct: number) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const width = 360;
    const height = 240;

    let left = rect.left - 100;
    if (left + width > window.innerWidth) left = window.innerWidth - width - 16;
    if (left < 16) left = 16;

    let top = rect.bottom + 8;
    if (top + height > window.innerHeight) top = rect.top - height - 8;

    const sym = item.symbol.toUpperCase();
    const q = marketQuotesMap[sym] || {};
    const ltp = q.last_price || 0;
    const prevClose = q.close || 0;
    const dayHigh = q.high || 0;
    const dayLow = q.low || 0;

    // The sparkline series is written by the quote poll, once per tick. Adding
    // a point on hover too would bend the trend the popover is there to show.

    setHoveredSentiment({
      symbol: sym,
      name: item.title || item.symbol,
      buyPct,
      sellPct: 100 - buyPct,
      buyQty,
      sellQty,
      changePct,
      ltp,
      prevClose,
      dayHigh,
      dayLow,
      x: left,
      y: top
    });
  };

  const handleSentimentMouseLeave = () => {
    setHoveredSentiment(null);
  };

  // Upcoming Earnings Sorting, Search, and Filtering state
  const [earningsSortBy, setEarningsSortBy] = useState<"date" | "return_desc" | "return_asc" | "change_desc" | "change_asc">("change_desc");
  const [earningsSearch, setEarningsSearch] = useState<string>("");
  const [earningsDateFilter, setEarningsDateFilter] = useState<string>(todayStr);

  const processedUpcomingEarnings = useMemo(() => {
    let list = [...upcomingEarnings];

    // Calendar Date Filter (default TODAY)
    if (earningsDateFilter.trim()) {
      list = list.filter(item => {
        const itemDate = item.meeting_date || item.date;
        return itemDate === earningsDateFilter;
      });
    }

    // Symbol / Text Search
    if (earningsSearch.trim()) {
      const q = earningsSearch.toLowerCase().trim();
      list = list.filter(item =>
        item.symbol.toLowerCase().includes(q) ||
        (item.purpose && item.purpose.toLowerCase().includes(q))
      );
    }

    // Sort
    if (earningsSortBy === "change_desc") {
      // Sort by live change % (uses marketQuotesMap for real-time data)
      list.sort((a, b) => {
        const qA = marketQuotesMap[a.symbol.toUpperCase()] || {};
        const qB = marketQuotesMap[b.symbol.toUpperCase()] || {};
        const cA = qA.change ?? a.change_pct ?? 0;
        const cB = qB.change ?? b.change_pct ?? 0;
        return cB - cA;
      });
    } else if (earningsSortBy === "change_asc") {
      list.sort((a, b) => {
        const qA = marketQuotesMap[a.symbol.toUpperCase()] || {};
        const qB = marketQuotesMap[b.symbol.toUpperCase()] || {};
        const cA = qA.change ?? a.change_pct ?? 0;
        const cB = qB.change ?? b.change_pct ?? 0;
        return cA - cB;
      });
    } else if (earningsSortBy === "return_desc") {
      list.sort((a, b) => (b.change_pct ?? 0) - (a.change_pct ?? 0));
    } else if (earningsSortBy === "return_asc") {
      list.sort((a, b) => (a.change_pct ?? 0) - (b.change_pct ?? 0));
    } else {
      // Date ascending (nearest meeting first)
      list.sort((a, b) => new Date(a.date).getTime() - new Date(b.date).getTime());
    }

    return list;
  }, [upcomingEarnings, earningsSortBy, earningsSearch, earningsDateFilter, marketQuotesMap]);

  // Fetch chart candles for earnings stock chart modal
  const fetchChartCandles = useCallback(async (key: string, period: string) => {
    setChartLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/market/stock/${encodeURIComponent(key)}/candles?period=${period}`);
      if (res.ok) {
        const data = await res.json();
        setChartCandles(data.candles || []);
      }
    } catch (err) {
      console.error("Error fetching chart candles:", err);
    } finally {
      setChartLoading(false);
    }
  }, []);

  /**
   * Open the price chart for a symbol.
   *
   * A missing key is resolved, never invented. `NSE_EQ|<SYMBOL>` looks like a
   * key and is accepted by nothing, so a BSE-only scrip opened an empty chart
   * reading "chart data unavailable" — which says the stock has no data, when
   * really we asked about a stock that does not exist.
   */
  const [quarterHistory, setQuarterHistory] = useState<QuarterHistory | null>(null);
  const [quarterLoading, setQuarterLoading] = useState(false);

  const fetchQuarterHistory = async (symbol: string) => {
    setQuarterLoading(true);
    setQuarterHistory(null);
    try {
      const res = await fetch(`${API_BASE}/api/trading/quarterly-history/${encodeURIComponent(symbol)}?quarters=8`);
      setQuarterHistory(res.ok ? await res.json() : null);
    } catch {
      setQuarterHistory(null);
    } finally {
      setQuarterLoading(false);
    }
  };

  const openEarningsChart = async (symbol: string, instrumentKey?: string) => {
    setChartSymbol(symbol);
    setChartPeriod("1D");
    fetchQuarterHistory(symbol);

    const looksReal = instrumentKey && /\|[A-Z]{2}[A-Z0-9]{9}[0-9]$/.test(instrumentKey);
    let key = looksReal ? instrumentKey! : "";
    if (!key) {
      try {
        const res = await fetch(`${API_BASE}/api/market/resolve-symbol?symbol=${encodeURIComponent(symbol)}`);
        if (res.ok) key = (await res.json()).key || "";
      } catch (err) {
        console.error("Symbol resolution failed:", err);
      }
    }
    if (!key) {
      // Neither exchange lists it, so there is nothing to chart. Say that,
      // rather than showing an empty chart the user will retry.
      setChartInstrumentKey(null);
      setChartCandles([]);
      return;
    }
    setChartInstrumentKey(key);
    fetchChartCandles(key, "1D");
  };

  useEffect(() => {
    if (chartInstrumentKey) {
      fetchChartCandles(chartInstrumentKey, chartPeriod);
    }
  }, [chartPeriod, chartInstrumentKey, fetchChartCandles]);

  // AI-log filters are applied server-side so the date range spans all history,
  // not just the page currently loaded.
  const aiLogParams = () => {
    const p = new URLSearchParams();
    if (aiLogSearch.trim()) p.set("search", aiLogSearch.trim());
    if (aiLogDateFrom) p.set("date_from", aiLogDateFrom);
    if (aiLogDateTo) p.set("date_to", aiLogDateTo);
    return p.toString();
  };

  // Pending results are filtered client-side: the list is small and already
  // scoped to today, so this keeps typing instant.
  // Live rows before the category filter, so each tab can show its own count
  // and an empty tab is visibly empty rather than missing.
  const livePendingResults = useMemo(() => {
    // Today shows what can still be acted on. A past day is history, and its
    // rows were all marked 'expired' at the daily reset — filtering those out
    // is what made every earlier date look empty. Dismissed rows stay hidden
    // either way: dismissing is the one status that means "I do not want to
    // see this".
    if (resultsDate === todayStr) {
      return pendingResults.filter(p => p.status === "pending" || p.status === "ordered");
    }
    return pendingResults.filter(p => p.status !== "dismissed");
  }, [pendingResults, resultsDate, todayStr]);

  const categoryCounts = useMemo(() => {
    const counts: Record<PromptCategory, number> = { results: 0, order: 0, merger: 0, other: 0 };
    for (const p of livePendingResults) counts[categoryOf(p)]++;
    return counts;
  }, [livePendingResults]);

  const visiblePendingResults = useMemo(() => {
    // Which statuses survive is decided in livePendingResults, and depends on
    // whether the day being shown is today. This only narrows to the open tab.
    const live = livePendingResults.filter(p => categoryOf(p) === pendingCategory);
    const q = pendingSearch.trim().toLowerCase();
    if (!q) return live;
    return live.filter(p =>
      p.symbol.toLowerCase().includes(q) ||
      (p.company_name || "").toLowerCase().includes(q) ||
      (p.title || "").toLowerCase().includes(q) ||
      // The ref is the chip beside the symbol on every card and the code on the
      // Telegram alert, so it is the obvious thing to paste in here. The box
      // has always offered it; only the server-side filter implemented it.
      (p.tracking_ref || "").toLowerCase().includes(q)
    );
  }, [livePendingResults, pendingCategory, pendingSearch]);

  /**
   * Today's filings grouped by the hour they were announced, each symbol
   * carrying its move since the result.
   *
   * A hundred-plus cards is too many to read one at a time. Bucketing by hour
   * says which part of the day is still worth attention — an 11:00 batch that
   * has already repriced needs nothing, a 15:00 one may still be live — and the
   * colour is the same "since result" number the card shows, so scanning the
   * strip and reading a card cannot disagree.
   */
  const resultsByHour = useMemo(() => {
    const buckets = new Map<string, {
      hour: string;
      items: { symbol: string; since: number | null; day: number | null }[];
    }>();
    for (const p of visiblePendingResults) {
      const iso = p.announced_at || p.event_time || p.ingested_at;
      if (!iso) continue;
      const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
      if (isNaN(d.getTime())) continue;
      const hour = `${String(d.getHours()).padStart(2, "0")}:00`;

      const quote = marketQuotesMap[p.symbol.toUpperCase()];
      const base = p.price_at_announcement;
      const ltp = quote?.last_price;
      const since = ltp && base ? ((ltp - base) / base) * 100 : null;
      const day = quote?.change ?? null;

      if (!buckets.has(hour)) buckets.set(hour, { hour, items: [] });
      buckets.get(hour)!.items.push({ symbol: p.symbol, since, day });
    }
    return [...buckets.values()]
      .map(b => ({
        ...b,
        // Biggest movers first: the reason to look at an hour is what moved in it.
        items: b.items.sort((a, z) => Math.abs(z.since ?? -1) - Math.abs(a.since ?? -1)),
        up: b.items.filter(i => (i.since ?? 0) > 0).length,
        down: b.items.filter(i => (i.since ?? 0) < 0).length,
      }))
      .sort((a, z) => z.hour.localeCompare(a.hour));
  }, [visiblePendingResults, marketQuotesMap]);

  // Fetch initial & fast status data
  const fetchData = async () => {
    try {
      const [configsRes, ordersRes, aiLogsRes, pollerRes, settingsRes, feedRes, pendingRes] = await Promise.all([
        fetch(`${API_BASE}/api/trading/configs`),
        fetch(`${API_BASE}/api/trading/orders`),
        fetch(`${API_BASE}/api/trading/ai-logs?${aiLogParams()}`),
        fetch(`${API_BASE}/api/trading/poller/status`),
        fetch(`${API_BASE}/api/trading/settings`),
        // Financial results live here, not in the AI Intelligence feed
        fetch(`${API_BASE}/api/intelligence/feed?hours=24&category=financial_results`),
        // status=all so a filing that has been ordered stays on screen with a
        // sell control, instead of vanishing the moment the buy goes through.
        fetch(`${API_BASE}/api/trading/pending-results?status=all&trade_date=${resultsDateRef.current}`),
      ]);

      if (configsRes.ok) {
        const data = await configsRes.json();
        setConfigs(data.configs || []);
      }
      if (ordersRes.ok) {
        const data = await ordersRes.json();
        setOrders(data.orders || []);
      }
      if (aiLogsRes.ok) {
        const data = await aiLogsRes.json();
        setAiLogs(data.logs || []);
      }
      if (pollerRes.ok) {
        const data = await pollerRes.json();
        setPollerStatus(data);
      }
      if (settingsRes.ok) {
        const data = await settingsRes.json();
        if (data.settings && !showSettingsModal) {
          setAiSettings(prev => ({
            ...prev,
            ...data.settings
          }));
        }
      }
      if (feedRes.ok) {
        const data = await feedRes.json();
        const filtered = (data.items || []).filter((item: any) =>
          (item.source === "nse" || item.source === "bse") && item.event_type === "announcement"
        );
        setNseAnnouncements(filtered);
      }
      if (pendingRes.ok) {
        const data = await pendingRes.json();
        const rows: PendingResult[] = data.pending || [];
        // Only today's view announces arrivals; browsing an earlier date is
        // reading history, not watching for something to act on.
        if (resultsDateRef.current === todayStr) {
          notifyNewResults(rows.filter(r => r.status === "pending"));
        }
        setPendingResults(rows);
        // Screener figures arrive behind the request, so the panel reports how
        // many are still outstanding rather than rendering gaps.
        setPendingBuilding(data.pending_screener || 0);
      }
    } catch (err) {
      console.error("Error loading trading dashboard data:", err);
    } finally {
      setLoading(false);
    }
  };

  // ── Financial-result order prompts ──

  const getResultForm = (id: number) =>
    resultOrderForm[id] || {
      quantity: 1, order_type: "MARKET", limit_price: "",
      stoploss_pct: 2, stoploss_type: "software", broker: "upstox",
    };

  const updateResultForm = (id: number, patch: Partial<ReturnType<typeof getResultForm>>) => {
    setResultOrderForm(prev => ({ ...prev, [id]: { ...getResultForm(id), ...patch } }));
  };

  const handlePlaceResultOrder = async (pending: PendingResult) => {
    const form = getResultForm(pending.id);
    setActionLoading(prev => ({ ...prev, [`result_${pending.id}`]: true }));
    try {
      const res = await fetch(`${API_BASE}/api/trading/pending-results/${pending.id}/order`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          quantity: form.quantity,
          order_type: form.order_type,
          limit_price: form.order_type === "LIMIT" && form.limit_price ? parseFloat(form.limit_price) : null,
          stoploss_pct: form.stoploss_pct,
          stoploss_type: form.stoploss_type,
          broker: form.broker,
        }),
      });
      const data = await res.json();
      if (!res.ok || !data.success) {
        alert(`Order failed for ${pending.symbol}: ${data.message || data.detail || "Unknown error"}`);
      }
      await fetchData();
    } catch (err) {
      console.error("Error placing result order:", err);
      alert(`Order failed for ${pending.symbol}. See console for details.`);
    } finally {
      setActionLoading(prev => ({ ...prev, [`result_${pending.id}`]: false }));
    }
  };

  /**
   * Sell the position a result prompt opened.
   *
   * Confirmed first: this places a real market order, and the card sits in a
   * list of hundreds where a misplaced click is easy.
   */
  const handleSellResultPosition = async (pending: PendingResult) => {
    const pos = pending.position;
    if (!pos?.config_id) return;
    const qty = pos.quantity ?? 0;
    const at = pos.buy_price ? ` bought at ₹${pos.buy_price}` : "";
    if (!window.confirm(`Sell ${qty} ${pending.symbol}${at}?\n\nThis places a real market order.`)) return;

    setActionLoading(prev => ({ ...prev, [`sell_${pending.id}`]: true }));
    try {
      const res = await fetch(`${API_BASE}/api/trading/configs/${pos.config_id}/sell`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ side: "SELL", quantity: qty }),
      });
      const data = await res.json();
      if (!res.ok || data.success === false) {
        alert(`Sell failed for ${pending.symbol}: ${data.message || data.detail || "Unknown error"}`);
      }
      await fetchData();
    } catch (err) {
      console.error("Error selling result position:", err);
      alert(`Sell failed for ${pending.symbol}. See console for details.`);
    } finally {
      setActionLoading(prev => ({ ...prev, [`sell_${pending.id}`]: false }));
    }
  };

  /**
   * Suspend or resume live pricing for one row.
   *
   * The row stays exactly where it is with the last price it had; only its
   * symbol leaves the quote batch. That batch is where the metered Upstox
   * allowance is spent, so pausing the rows you are not watching is what keeps
   * prices flowing on the ones you are.
   */
  const handleTogglePause = async (id: number) => {
    setActionLoading(prev => ({ ...prev, [`pause_${id}`]: true }));
    try {
      const res = await fetch(`${API_BASE}/api/trading/pending-results/${id}/pause`, { method: "POST" });
      if (res.ok) {
        const data = await res.json();
        // Patched in place rather than refetching the whole panel: a pause is a
        // one-field change and a full reload would scroll a long list back.
        setPendingResults(prev => prev.map(p =>
          p.id === id ? { ...p, paused: !!data.paused } : p));
      }
    } catch (err) {
      console.error("Error pausing result:", err);
    } finally {
      setActionLoading(prev => ({ ...prev, [`pause_${id}`]: false }));
    }
  };

  /**
   * Read the filing's own PDF and score it against Screener.
   *
   * Deliberately a separate action from the AI: this is a second opinion from a
   * different source, and running it automatically alongside every analysis
   * would spend a download and an OCR pass on filings nobody is looking at.
   */
  const handleExtract = async (pending: PendingResult, refresh = false) => {
    setActionLoading(prev => ({ ...prev, [`extract_${pending.id}`]: true }));
    try {
      const res = await fetch(
        `${API_BASE}/api/trading/pending-results/${pending.id}/extract${refresh ? "?refresh=true" : ""}`,
        { method: "POST" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert(err.detail || "Could not read the filing.");
        return;
      }
      const data: Extraction = await res.json();
      // The endpoint queues the read and returns immediately. Its placeholder
      // goes on the row so the card says what is happening; the finished
      // extraction arrives on the panel's next poll, like an AI verdict.
      setPendingResults(prev => prev.map(p =>
        p.id === pending.id ? { ...p, extraction: data } : p));
    } catch (err) {
      console.error("Extraction failed:", err);
    } finally {
      setActionLoading(prev => ({ ...prev, [`extract_${pending.id}`]: false }));
    }
  };

  /** Re-run the earnings AI over one filing, optionally with another model. */
  const handleReanalyse = async (pending: PendingResult) => {
    const choice = reanalyseModel[pending.id] ?? "";
    const model = choice === "__custom__" ? (customModel[pending.id] || "").trim() : choice;
    if (choice === "__custom__" && !model) {
      alert("Type a model id, or pick one from the list.");
      return;
    }
    setActionLoading(prev => ({ ...prev, [`reanalyse_${pending.id}`]: true }));
    try {
      const res = await fetch(`${API_BASE}/api/trading/pending-results/${pending.id}/reanalyse`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model, flow: "ai" }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert(err.detail || "Could not start the analysis.");
        return;
      }
      // The verdict already on screen is left alone until the new one lands:
      // a re-run that fails would otherwise wipe a good answer.
      setPendingResults(prev => prev.map(p =>
        p.id === pending.id ? { ...p, ai_status: "running" } : p));
    } catch (err) {
      console.error("Error re-analysing:", err);
    } finally {
      setActionLoading(prev => ({ ...prev, [`reanalyse_${pending.id}`]: false }));
    }
  };

  const [syncingQuotes, setSyncingQuotes] = useState(false);

  const handleSyncQuotesNow = async () => {
    setSyncingQuotes(true);
    try {
      const res = await fetch(`${API_BASE}/api/trading/upcoming-earnings/sync`, { method: "POST" });
      if (res.ok) {
        await fetchUpcomingEarnings();
        await fetchMarketQuotes();
      }
    } catch (err) {
      console.error("Error syncing quotes:", err);
    } finally {
      setSyncingQuotes(false);
    }
  };

  const fetchUpcomingEarnings = async () => {
    try {
      const earningsRes = await fetch(`${API_BASE}/api/trading/upcoming-earnings`);
      if (earningsRes.ok) {
        const data = await earningsRes.json();
        setUpcomingEarnings(data.upcoming_earnings || []);
      }
    } catch (err) {
      console.error("Error loading upcoming earnings:", err);
    }
  };

  const handleClearAiLogs = async () => {
    if (!window.confirm("Are you sure you want to clear AI analysis logs and reset to latest?")) return;
    try {
      const res = await fetch(`${API_BASE}/api/trading/ai-logs/clear`, { method: "DELETE" });
      if (res.ok) {
        setAiLogs([]);
      }
    } catch (err) {
      console.error("Failed to clear AI logs:", err);
    }
  };

  const handleSaveSettings = async (e: React.FormEvent) => {
    e.preventDefault();
    setSavingSettings(true);
    try {
      const res = await fetch(`${API_BASE}/api/trading/settings`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(aiSettings),
      });
      if (res.ok) {
        setShowSettingsModal(false);
        fetchData();
      }
    } catch (err) {
      console.error("Failed to save auto-trading settings:", err);
    } finally {
      setSavingSettings(false);
    }
  };




  /**
   * Roll one tick of quotes into the per-symbol history the tracker's readouts
   * are built from: the composite buy/sell split, the order-book quantities
   * behind it, and a flash when the price moves.
   *
   * The composite weighting (85% order-book depth, 15% where the price sits in
   * the day's range) is the same one the stock tracker uses, so a stock reads
   * identically in both places.
   */
  const recordQuoteTick = (qmap: Record<string, any>) => {
    const flashes: Record<string, "up" | "down"> = {};

    Object.entries(qmap).forEach(([sym, q]: [string, any]) => {
      const ltp = q?.last_price || 0;
      if (ltp <= 0) return;

      const prev = prevPricesRef.current[sym];
      if (prev !== undefined && prev !== ltp) {
        flashes[sym] = ltp > prev ? "up" : "down";
      }
      prevPricesRef.current[sym] = ltp;

      const high = q.high || 0;
      const low = q.low || 0;
      const range = high - low;
      const priceBuyPct = range > 0 ? ((ltp - low) / range) * 100 : 50;
      const depthBuyPct = q.depth_buy_pct !== undefined ? q.depth_buy_pct : 50;
      const compositeBuyPct = Math.round((priceBuyPct * 0.15) + (depthBuyPct * 0.85));

      const hist = sentimentHistoryRef.current[sym] || [];
      sentimentHistoryRef.current[sym] = [...hist, compositeBuyPct].slice(-15);

      const qHist = quantityHistoryRef.current[sym] || { buy: [], sell: [] };
      quantityHistoryRef.current[sym] = {
        buy: [...qHist.buy, q.total_buy_qty || 0].slice(-15),
        sell: [...qHist.sell, q.total_sell_qty || 0].slice(-15),
      };
    });

    if (Object.keys(flashes).length > 0) {
      setPriceFlash(flashes);
      setTimeout(() => setPriceFlash({}), 800);
    }
  };

  /**
   * The tracker's 2-minute order-book signal, scored from the rolling history
   * above. It reads the same as the watchlist column because it is the same
   * calculation — level, drift, quantity dominance and quantity accumulation.
   *
   * Under three ticks there is no trend to read, so it says so rather than
   * showing a confident-looking HOLD.
   */
  const get2MinSignal = (symbol: string, quote: any) => {
    const sym = symbol.toUpperCase();
    const sentHistory = sentimentHistoryRef.current[sym] || [];
    const qtyHistory = quantityHistoryRef.current[sym] || { buy: [], sell: [] };

    if (sentHistory.length < 3) {
      return {
        recommendation: "HOLD", confidence: "Insuff. Data", tone: "neutral" as const,
        explanation: "Collecting real-time order book ticks to establish trend baseline (takes ~30s).",
      };
    }

    const currentSent = sentHistory[sentHistory.length - 1];
    const buyQty = quote?.total_buy_qty || 0;
    const sellQty = quote?.total_sell_qty || 0;
    const totalQty = buyQty + sellQty;

    let score = 0;

    if (currentSent >= 75) score += 1.5;
    else if (currentSent >= 60) score += 1.0;
    else if (currentSent <= 25) score -= 1.5;
    else if (currentSent <= 40) score -= 1.0;

    const sentDelta = currentSent - sentHistory[0];
    if (sentDelta >= 15) score += 2.0;
    else if (sentDelta >= 5) score += 1.0;
    else if (sentDelta <= -15) score -= 2.0;
    else if (sentDelta <= -5) score -= 1.0;

    if (totalQty > 0) {
      const buyRatio = buyQty / totalQty;
      if (buyRatio >= 0.75) score += 1.5;
      else if (buyRatio >= 0.60) score += 1.0;
      else if (buyRatio <= 0.25) score -= 1.5;
      else if (buyRatio <= 0.40) score -= 1.0;
    }

    if (qtyHistory.buy.length >= 3) {
      const buyQtyChange = qtyHistory.buy[qtyHistory.buy.length - 1] - qtyHistory.buy[0];
      const sellQtyChange = qtyHistory.sell[qtyHistory.sell.length - 1] - qtyHistory.sell[0];
      if (buyQtyChange > 0 && buyQtyChange > sellQtyChange) score += 1.0;
      else if (sellQtyChange > 0 && sellQtyChange > buyQtyChange) score -= 1.0;
    }

    let recommendation = "HOLD";
    let confidence = "Low";
    let tone: "up" | "down" | "neutral" = "neutral";
    if (score >= 3.5) { recommendation = "STRONG BUY"; confidence = "High"; tone = "up"; }
    else if (score >= 1.5) { recommendation = "BUY"; confidence = "Medium"; tone = "up"; }
    else if (score <= -3.5) { recommendation = "STRONG SELL"; confidence = "High"; tone = "down"; }
    else if (score <= -1.5) { recommendation = "SELL"; confidence = "Medium"; tone = "down"; }

    const sentTrendText = sentDelta > 0 ? "improving" : sentDelta < 0 ? "weakening" : "stable";
    const qtyDominance = buyQty > sellQty ? "Buyer volume dominance"
      : sellQty > buyQty ? "Seller volume dominance" : "Balanced volume";

    return {
      recommendation, confidence, tone,
      explanation: `Sentiment is ${sentTrendText} (${sentDelta >= 0 ? "+" : ""}${sentDelta.toFixed(0)}% delta). ${qtyDominance} with total interest of ${fmtQty(totalQty)}.`,
    };
  };

  const fetchMarketQuotes = useCallback(async () => {
    try {
      const qmap: Record<string, any> = {};

      // 1. Fetch Watchlist items
      const resWl = await fetch(`${API_BASE}/api/watchlist?period=today`);
      if (resWl.ok) {
        const data = await resWl.json();
        const items = Array.isArray(data) ? data : (data.watchlist || data.gainers || []);
        if (Array.isArray(items)) {
          items.forEach((item: any) => {
            if (item.symbol) {
              qmap[item.symbol.toUpperCase()] = item;
            }
          });
        }
      }

      // 2. Batch fetch live Upstox market quotes for earnings + pending results
      const quoteSymbols = [
        ...(earningsPausedRef.current ? [] : upcomingEarningsRef.current.map(item => item.symbol)),
        // Only today's results carry live prices — see resultsDate. A paused row
        // is skipped here, which is the whole of what pausing does: on a heavy
        // results day the symbol list is what the Upstox budget is spent on.
        ...(resultsDateRef.current === todayStr
          ? pendingResultsRef.current.filter(p => !p.paused).map(p => p.symbol)
          : []),
      ].filter(Boolean);
      if (quoteSymbols.length > 0) {
        const uniqueSyms = Array.from(new Set(quoteSymbols.map(s => s.toUpperCase())));
        // Chunked because a heavy results day carries well past 100 symbols and
        // the whole panel, not just the overflow, goes priceless if the URL is
        // rejected for length. 250 keeps a full day inside a single request —
        // at 80 the list split into three, tripling the metered calls per tick.
        for (let i = 0; i < uniqueSyms.length; i += 250) {
          const symStr = uniqueSyms.slice(i, i + 250).join(",");
          const resBatch = await fetch(`${API_BASE}/api/market/quotes-by-symbols?symbols=${encodeURIComponent(symStr)}`);
          if (!resBatch.ok) continue;
          const batchQuotes = await resBatch.json();
          Object.entries(batchQuotes).forEach(([sym, q]: [string, any]) => {
            const symKey = sym.toUpperCase();
            const existing = qmap[symKey] || {};
            const validChange = (q.change !== undefined && q.change !== 0.0) ? q.change : existing.change;
            const validLtp = (q.last_price && q.last_price > 0) ? q.last_price : existing.last_price;

            qmap[symKey] = {
              ...existing,
              ...q,
              change: validChange,
              last_price: validLtp
            };
          });
        }
      }

      recordQuoteTick(qmap);
      setMarketQuotesMap(qmap);
    } catch (err) {
      console.error("Error loading live market quotes:", err);
    }
  }, []);

  useEffect(() => {
    pendingResultsRef.current = pendingResults;
  }, [pendingResults]);

  useEffect(() => {
    upcomingEarningsRef.current = upcomingEarnings;
  }, [upcomingEarnings]);

  /**
   * Mon–Fri, 09:00–15:35 IST — the window quotes are worth spending on.
   * Read in Asia/Kolkata rather than the browser's zone, matching the tracker.
   */
  const isMarketHours = () => {
    try {
      const ist = new Date(new Date().toLocaleString("en-US", { timeZone: "Asia/Kolkata" }));
      if (ist.getDay() === 0 || ist.getDay() === 6) return false;
      const mins = ist.getHours() * 60 + ist.getMinutes();
      return mins >= 540 && mins <= 935; // 9:00 AM to 3:35 PM
    } catch {
      return true;
    }
  };

  useEffect(() => {
    fetchData();
    fetchUpcomingEarnings();
    fetchMarketQuotes();

    // Card data is a SQLite read and costs nothing upstream, so a new filing
    // still appears within 3s.
    const intervalCards = setInterval(() => {
      fetchData();
      fetchUpcomingEarnings();
    }, 3000);

    // Quotes come from the backend's shared cache, which a single background
    // loop keeps ~3s fresh for every panel at once. Polling here no longer
    // costs an Upstox request, so this tracks that cache closely instead of
    // rationing itself the way it had to when each panel fetched its own.
    let lastQuoteAt = 0;
    const intervalQuotes = setInterval(() => {
      const gap = isMarketHours() ? 3000 : 60000;
      if (Date.now() - lastQuoteAt < gap) return;
      lastQuoteAt = Date.now();
      fetchMarketQuotes();
    }, 1000);

    return () => {
      clearInterval(intervalCards);
      clearInterval(intervalQuotes);
    };
  }, []);

  // Symbol Search Autocomplete
  const handleSymbolSearch = async (query: string) => {
    setFormData(prev => ({ ...prev, symbol: query }));
    if (query.trim().length < 2) {
      setSearchResults([]);
      return;
    }
    setSearching(true);
    try {
      const res = await fetch(`${API_BASE}/api/watchlist/search?q=${encodeURIComponent(query)}`);
      if (res.ok) {
        const data = await res.json();
        setSearchResults(data || []);
      }
    } catch (err) {
      console.error("Symbol search failed:", err);
    } finally {
      setSearching(false);
    }
  };

  const selectSymbol = (item: any) => {
    setFormData(prev => ({
      ...prev,
      symbol: item.symbol,
      instrument_key: item.key || item.instrument_key || ""
    }));
    setSearchResults([]);
  };

  // Add new config
  const handleAddConfig = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!formData.symbol.trim()) return;

    try {
      const res = await fetch(`${API_BASE}/api/trading/configs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...formData,
          limit_price: formData.order_type === "LIMIT" && formData.limit_price ? parseFloat(formData.limit_price) : null
        })
      });

      if (res.ok) {
        const newConfig = await res.json();
        // If autoArmOnSave flag is set, arm the config immediately after creation
        if (autoArmOnSave && newConfig.id) {
          await fetch(`${API_BASE}/api/trading/configs/${newConfig.id}/arm`, { method: "POST" });
        }
        setShowAddForm(false);
        setAutoArmOnSave(false);
        setFormData({
          symbol: "",
          instrument_key: "",
          purchase_date: todayStr,
          quantity: 1,
          stoploss_pct: 2.0,
          stoploss_type: "software",
          broker: "upstox",
          order_type: "MARKET",
          limit_price: "",
          ai_provider: "groq",
          trigger_subject: "Outcome of Board Meeting",
          notes: ""
        });
        fetchData();
      }
    } catch (err) {
      console.error("Failed to add config:", err);
    }
  };

  // Arm config
  const handleArm = async (id: number) => {
    setActionLoading(prev => ({ ...prev, [`arm-${id}`]: true }));
    try {
      await fetch(`${API_BASE}/api/trading/configs/${id}/arm`, { method: "POST" });
      fetchData();
    } catch (err) {
      console.error("Failed to arm config:", err);
    } finally {
      setActionLoading(prev => ({ ...prev, [`arm-${id}`]: false }));
    }
  };

  // Disarm config
  const handleDisarm = async (id: number) => {
    setActionLoading(prev => ({ ...prev, [`disarm-${id}`]: true }));
    try {
      await fetch(`${API_BASE}/api/trading/configs/${id}/disarm`, { method: "POST" });
      fetchData();
    } catch (err) {
      console.error("Failed to disarm config:", err);
    } finally {
      setActionLoading(prev => ({ ...prev, [`disarm-${id}`]: false }));
    }
  };

  // Open target config form pre-filled from upcoming earnings calendar
  const selectUpcomingStock = (item: any, autoArm: boolean = false) => {
    setFormData({
      symbol: item.symbol,
      instrument_key: item.instrument_key || `NSE_EQ|${item.symbol}`,
      purchase_date: item.meeting_date || item.date || todayStr,
      quantity: 1,
      stoploss_pct: 2.0,
      stoploss_type: "software",
      broker: "upstox",
      order_type: "MARKET",
      limit_price: "",
      ai_provider: "groq",
      trigger_subject: item.purpose || "Outcome of Board Meeting",
      notes: ""
    });
    setAutoArmOnSave(autoArm);
    setShowAddForm(true);

    setTimeout(() => {
      const configEl = document.getElementById("auto-trading-target-configs");
      if (configEl) {
        configEl.scrollIntoView({ behavior: "smooth" });
      }
    }, 100);
  };

  // Manual Buy
  const handleManualBuy = async (id: number) => {
    if (!window.confirm("Place live BUY order via broker now?")) return;
    setActionLoading(prev => ({ ...prev, [`buy-${id}`]: true }));
    try {
      const res = await fetch(`${API_BASE}/api/trading/configs/${id}/buy`, { method: "POST" });
      const data = await res.json();
      if (!data.success) {
        alert(`Order Failed: ${data.message}`);
      }
      fetchData();
    } catch (err) {
      console.error("Manual buy failed:", err);
    } finally {
      setActionLoading(prev => ({ ...prev, [`buy-${id}`]: false }));
    }
  };

  // Manual Sell
  const handleManualSell = async (id: number) => {
    if (!window.confirm("Place live SELL order via broker now?")) return;
    setActionLoading(prev => ({ ...prev, [`sell-${id}`]: true }));
    try {
      const res = await fetch(`${API_BASE}/api/trading/configs/${id}/sell`, { method: "POST" });
      const data = await res.json();
      if (!data.success) {
        alert(`Sell Failed: ${data.message}`);
      }
      fetchData();
    } catch (err) {
      console.error("Manual sell failed:", err);
    } finally {
      setActionLoading(prev => ({ ...prev, [`sell-${id}`]: false }));
    }
  };

  // Manual On-Demand Poll
  const [manualPolling, setManualPolling] = useState(false);
  const handlePollNow = async () => {
    setManualPolling(true);
    try {
      const res = await fetch(`${API_BASE}/api/trading/poller/poll-now`, { method: "POST" });
      if (res.ok) {
        const data = await res.json();
        alert(`⚡ Manual Poll Complete!\n• Announcements Fetched: ${data.announcements_fetched}\n• Armed Targets Checked: ${data.armed_configs_checked}\n• Triggers Found: ${data.triggers_found}`);
      }
      fetchData();
    } catch (err) {
      console.error("Manual poll failed:", err);
    } finally {
      setManualPolling(false);
    }
  };

  // Delete config
  const handleDeleteConfig = async (id: number) => {
    if (!window.confirm("Delete this trade target configuration?")) return;
    try {
      await fetch(`${API_BASE}/api/trading/configs/${id}`, { method: "DELETE" });
      fetchData();
    } catch (err) {
      console.error("Delete failed:", err);
    }
  };

  // Status Badge Helper
  const getStatusBadge = (status: string) => {
    switch (status) {
      case "armed":
        return (
          <span style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "5px",
            padding: "3px 10px",
            borderRadius: "12px",
            fontSize: "11px",
            fontWeight: "700",
            backgroundColor: "rgba(63, 191, 135, 0.15)",
            color: "var(--positive)",
            border: "1px solid rgba(63, 191, 135, 0.3)"
          }}>
            <span style={{ width: "6px", height: "6px", borderRadius: "50%", backgroundColor: "var(--positive)" }} className="animate-pulse" />
            ARMED & POLLING
          </span>
        );
      case "triggered":
        return (
          <span style={{
            padding: "3px 10px",
            borderRadius: "12px",
            fontSize: "11px",
            fontWeight: "700",
            backgroundColor: "rgba(164, 138, 224, 0.2)",
            color: "var(--ai)",
            border: "1px solid rgba(164, 138, 224, 0.3)"
          }}>
            ⚡ TRIGGERED
          </span>
        );
      case "bought":
        return (
          <span style={{
            padding: "3px 10px",
            borderRadius: "12px",
            fontSize: "11px",
            fontWeight: "700",
            backgroundColor: "rgba(91, 157, 255, 0.2)",
            color: "var(--accent)",
            border: "1px solid rgba(91, 157, 255, 0.3)"
          }}>
            🛒 BOUGHT / HOLDING
          </span>
        );
      case "sold":
        return (
          <span style={{
            padding: "3px 10px",
            borderRadius: "12px",
            fontSize: "11px",
            fontWeight: "700",
            backgroundColor: "rgba(6, 182, 212, 0.2)",
            color: "var(--info)",
            border: "1px solid rgba(6, 182, 212, 0.3)"
          }}>
            💰 SOLD
          </span>
        );
      case "failed":
        return (
          <span style={{
            padding: "3px 10px",
            borderRadius: "12px",
            fontSize: "11px",
            fontWeight: "700",
            backgroundColor: "rgba(240, 115, 111, 0.2)",
            color: "var(--negative-strong)",
            border: "1px solid rgba(240, 115, 111, 0.3)"
          }}>
            ❌ FAILED
          </span>
        );
      case "disarmed":
        return (
          <span style={{
            padding: "3px 10px",
            borderRadius: "12px",
            fontSize: "11px",
            fontWeight: "600",
            backgroundColor: "rgba(125, 135, 153, 0.15)",
            color: "var(--text-muted)",
            border: "1px solid rgba(125, 135, 153, 0.2)"
          }}>
            DISARMED
          </span>
        );
      default:
        return (
          <span style={{
            padding: "3px 10px",
            borderRadius: "12px",
            fontSize: "11px",
            fontWeight: "600",
            backgroundColor: "rgba(234, 179, 8, 0.15)",
            color: "var(--warning)",
            border: "1px solid rgba(234, 179, 8, 0.2)"
          }}>
            PENDING
          </span>
        );
    }
  };

  return (
    <div style={{
      padding: "20px",
      backgroundColor: "var(--bg-base)",
      minHeight: "calc(100vh - 80px)",
      color: "var(--text-primary)",
      fontFamily: "system-ui, sans-serif"
    }}>
      {/* TOP POLLER STATUS HEADER */}
      <div style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
        gap: "12px",
        marginBottom: "20px"
      }}>
        <div style={{
          background: "var(--surface-2)",
          border: "1px solid rgba(255, 255, 255, 0.12)",
          boxShadow: "0 4px 16px rgba(0, 0, 0, 0.2)",
          borderRadius: "12px",
          padding: "14px 18px",
          display: "flex",
          alignItems: "center",
          gap: "14px"
        }}>
          <div style={{
            padding: "10px",
            borderRadius: "10px",
            backgroundColor: pollerStatus?.running ? "rgba(63, 191, 135, 0.2)" : "rgba(240, 115, 111, 0.2)",
            color: pollerStatus?.running ? "var(--positive-strong)" : "var(--negative)"
          }}>
            <Zap size={22} />
          </div>
          <div>
            <div style={{ fontSize: "11px", color: "var(--text-secondary)", fontWeight: "600" }}>NSE REAL-TIME POLLER</div>
            <div style={{ fontSize: "15px", fontWeight: "800", color: pollerStatus?.running ? "var(--positive-strong)" : "var(--negative-strong)", marginTop: "2px" }}>
              {pollerStatus?.running ? `🟢 ACTIVE (${pollerStatus?.mode || 'Adaptive'})` : "🔴 IDLE"}
            </div>
          </div>
        </div>

        <div style={{
          background: "var(--surface-2)",
          border: "1px solid rgba(255, 255, 255, 0.12)",
          boxShadow: "0 4px 16px rgba(0, 0, 0, 0.2)",
          borderRadius: "12px",
          padding: "14px 18px",
          display: "flex",
          alignItems: "center",
          gap: "14px"
        }}>
          <div style={{ padding: "10px", borderRadius: "10px", backgroundColor: "rgba(91, 157, 255, 0.15)", color: "var(--accent)" }}>
            <Play size={22} />
          </div>
          <div>
            <div style={{ fontSize: "11px", color: "var(--text-muted)", fontWeight: "600" }}>ARMED CONFIGS</div>
            <div style={{ fontSize: "20px", fontWeight: "800", color: "var(--text-primary)" }}>
              {pollerStatus?.armed_count || 0} Targets Active
            </div>
          </div>
        </div>

        <div style={{
          background: "rgba(22, 27, 36, 0.6)",
          border: "1px solid rgba(255, 255, 255, 0.08)",
          borderRadius: "12px",
          padding: "14px 18px",
          display: "flex",
          alignItems: "center",
          gap: "14px"
        }}>
          <div style={{ padding: "10px", borderRadius: "10px", backgroundColor: "rgba(164, 138, 224, 0.15)", color: "var(--ai)" }}>
            <Cpu size={22} />
          </div>
          <div>
            <div style={{ fontSize: "11px", color: "var(--text-muted)", fontWeight: "600" }}>POLLS & TRIGGERS</div>
            <div style={{ fontSize: "14px", fontWeight: "700", color: "var(--text-primary)" }}>
              {pollerStatus?.polls_total || 0} Polls | ⚡ {pollerStatus?.triggers_total || 0} Triggers
            </div>
          </div>
        </div>

        <div style={{
          background: "rgba(22, 27, 36, 0.6)",
          border: "1px solid rgba(255, 255, 255, 0.08)",
          borderRadius: "12px",
          padding: "14px 18px",
          display: "flex",
          alignItems: "center",
          gap: "14px"
        }}>
          <div style={{ padding: "10px", borderRadius: "10px", backgroundColor: "rgba(224, 163, 62, 0.15)", color: "var(--warning)" }}>
            <ShieldAlert size={22} />
          </div>
          <div>
            <div style={{ fontSize: "11px", color: "var(--text-muted)", fontWeight: "600" }}>STOPLOSS WATCHER</div>
            <div style={{ fontSize: "14px", fontWeight: "700", color: "var(--warning)" }}>
              Active (Software + Bracket)
            </div>
          </div>
        </div>
      </div>

      {/* FINANCIAL RESULT ORDER PROMPTS — results that arrived on unarmed stocks */}
      {pendingResults.length > 0 && (
        <div style={{
          background: "rgba(120, 53, 15, 0.25)",
          border: "1px solid rgba(224, 163, 62, 0.45)",
          borderRadius: "14px",
          padding: "18px",
          marginBottom: "20px"
        }}>
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: "10px", marginBottom: "12px" }}>
            <h3 style={{ fontSize: "15px", fontWeight: 700, color: "var(--warning)", display: "flex", alignItems: "center", gap: "8px", margin: 0 }}>
              <AlertTriangle size={18} /> Order Decisions
              {" "}({visiblePendingResults.length}{pendingSearch.trim() ? ` of ${categoryCounts[pendingCategory]}` : ""})
            </h3>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <input
                value={pendingSearch}
                onChange={e => setPendingSearch(e.target.value)}
                placeholder="Search symbol, company, filing or ref…"
                style={{
                  padding: "6px 10px", minWidth: "240px", fontSize: "12px",
                  background: "rgba(10, 13, 18,0.8)", border: "1px solid rgba(224, 163, 62,0.3)",
                  borderRadius: "6px", color: "var(--text-primary)",
                }}
              />
              {pendingSearch && (
                <button onClick={() => setPendingSearch("")}
                  style={{ padding: "6px 10px", background: "transparent", border: "1px solid rgba(125, 135, 153,0.3)", borderRadius: "6px", color: "var(--text-muted)", fontSize: "11px", cursor: "pointer" }}>
                  Clear
                </button>
              )}

              <button
                onClick={enableAlerts}
                title={alertsOn
                  ? "Desktop notification and a short tone when a result lands. Click to turn off."
                  : "Notify me when a result lands (asks for browser permission)"}
                style={{
                  display: "inline-flex", alignItems: "center", gap: "5px",
                  padding: "6px 10px", borderRadius: "6px", cursor: "pointer",
                  fontSize: "11px", fontWeight: 700,
                  background: alertsOn ? "rgba(63, 191, 135, 0.12)" : "transparent",
                  border: `1px solid ${alertsOn ? "rgba(63, 191, 135, 0.35)" : "rgba(125, 135, 153,0.3)"}`,
                  color: alertsOn ? "var(--positive)" : "var(--text-muted)",
                }}>
                {alertsOn ? <Bell size={12} /> : <BellOff size={12} />}
                {alertsOn ? "ALERTS ON" : "Alerts off"}
              </button>

              <label title="Cash you have available. Each card shows what its order costs and what would be left."
                style={{ display: "inline-flex", alignItems: "center", gap: "5px", fontSize: "11px", color: "var(--text-muted)" }}>
                ₹
                <input
                  type="number"
                  min={0}
                  value={availableFunds}
                  onChange={e => setAvailableFunds(e.target.value)}
                  placeholder="funds"
                  style={{
                    width: "96px", padding: "6px 8px", fontSize: "12px",
                    background: "rgba(10, 13, 18,0.8)", border: "1px solid rgba(125, 135, 153,0.3)",
                    borderRadius: "6px", color: "var(--text-primary)",
                  }}
                />
              </label>

              <input
                type="date"
                value={resultsDate}
                max={todayStr}
                min={oldestStr}
                onChange={e => setResultsDate(e.target.value || todayStr)}
                title="Trade date to show. Only today carries live prices; earlier days are kept for 30 days."
                style={{
                  padding: "6px 10px", fontSize: "12px",
                  background: "rgba(10, 13, 18,0.8)",
                  border: `1px solid ${isToday ? "rgba(224, 163, 62,0.3)" : "var(--accent-border)"}`,
                  borderRadius: "6px", color: "var(--text-primary)",
                }}
              />
              {!isToday && (
                <button onClick={() => setResultsDate(todayStr)}
                  style={{ padding: "6px 10px", background: "var(--accent-bg)", border: "1px solid var(--accent-border)", borderRadius: "6px", color: "var(--accent)", fontSize: "11px", fontWeight: 700, cursor: "pointer" }}>
                  Today
                </button>
              )}

              {/* The same rows as the panel, with the price baseline and the
                  move since the filing that the 08:00 digest cannot carry —
                  it runs before there are any prices. */}
              <a href={`${API_BASE}/api/trading/pending-results/pdf?trade_date=${resultsDate}`}
                 target="_blank" rel="noreferrer"
                 title="Export this day's decisions, with Screener figures and price movement"
                 style={{ padding: "6px 12px", background: "transparent", border: "1px solid var(--border-default)", borderRadius: "6px", color: "var(--text-secondary)", fontSize: "11px", fontWeight: 600, textDecoration: "none" }}>
                PDF
              </a>

              <span style={{
                fontSize: "11px", padding: "4px 10px", borderRadius: "20px", whiteSpace: "nowrap",
                color: isToday ? "var(--warning)" : "var(--text-muted)",
                background: isToday ? "rgba(224, 163, 62,0.12)" : "rgba(125, 135, 153,0.12)",
              }}>
                {isToday ? "Today · not armed" : "Past date · prices not fetched"}
              </span>
            </div>
          </div>

          {/* One tab per kind of filing. Results are the only ones with figures
              to read, so they are the only ones carrying a Screener table or an
              AI section; an order win has no quarter in it. Counts are of the
              whole day, so an empty tab reads as empty rather than missing. */}
          <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", marginBottom: "12px" }}>
            {(["results", "order", "merger", "other"] as PromptCategory[]).map(cat => {
              const on = pendingCategory === cat;
              const n = categoryCounts[cat];
              return (
                <button key={cat} onClick={() => setPendingCategory(cat)}
                  style={{
                    padding: "6px 12px", borderRadius: "6px", cursor: "pointer",
                    fontSize: "11px", fontWeight: 700, letterSpacing: "0.3px",
                    background: on ? "rgba(224, 163, 62, 0.16)" : "transparent",
                    border: `1px solid ${on ? "rgba(224, 163, 62, 0.5)" : "rgba(125, 135, 153,0.25)"}`,
                    color: on ? "var(--warning)" : n ? "var(--text-secondary)" : "var(--text-faint)",
                  }}>
                  {CATEGORY_LABEL[cat]}
                  <span style={{ marginLeft: "6px", opacity: 0.8, fontWeight: 600 }}>{n}</span>
                </button>
              );
            })}
            {pendingBuilding > 0 && pendingCategory === "results" && (
              <span title="Screener is fetched one company at a time to stay inside its rate limit. Refresh in a few minutes."
                style={{ fontSize: "11px", color: "var(--warning)", background: "var(--warning-bg)", padding: "5px 10px", borderRadius: "20px", alignSelf: "center" }}>
                Screener figures still arriving — {pendingBuilding} to go
              </span>
            )}
          </div>

          {/* Hour-by-hour overview. Click a symbol to filter the list to it. */}
          {resultsByHour.length > 0 && (
            <div style={{
              marginBottom: "14px", padding: "10px 12px", borderRadius: "10px",
              background: "rgba(10, 13, 18, 0.45)", border: "1px solid rgba(255,255,255,0.06)",
              maxHeight: "190px", overflowY: "auto",
            }}>
              {resultsByHour.map(bucket => (
                <div key={bucket.hour} style={{ display: "flex", gap: "10px", alignItems: "flex-start", padding: "4px 0" }}>
                  <span style={{
                    minWidth: "96px", fontSize: "11px", fontWeight: 700, color: "var(--text-secondary)",
                    fontVariantNumeric: "tabular-nums", paddingTop: "3px", whiteSpace: "nowrap",
                  }}>
                    {bucket.hour}
                    <span style={{ color: "var(--text-faint)", fontWeight: 500 }}> · {bucket.items.length}</span>
                    {bucket.up > 0 && <span style={{ color: "var(--positive)", fontWeight: 700 }}> ↑{bucket.up}</span>}
                    {bucket.down > 0 && <span style={{ color: "var(--negative)", fontWeight: 700 }}> ↓{bucket.down}</span>}
                  </span>
                  <span style={{ display: "flex", flexWrap: "wrap", gap: "4px", flex: 1 }}>
                    {bucket.items.map(item => {
                      const tone = item.since == null ? "faint" : item.since > 0 ? "up" : item.since < 0 ? "down" : "flat";
                      const color = tone === "up" ? "var(--positive)"
                        : tone === "down" ? "var(--negative)" : "var(--text-faint)";
                      const bg = tone === "up" ? "rgba(63, 191, 135, 0.12)"
                        : tone === "down" ? "rgba(240, 115, 111, 0.12)" : "rgba(125, 135, 153, 0.10)";
                      const selected = pendingSearch.trim().toLowerCase() === item.symbol.toLowerCase();
                      return (
                        <button
                          key={item.symbol}
                          onClick={() => setPendingSearch(selected ? "" : item.symbol)}
                          title={`${item.symbol}\n`
                            + `Since result: ${item.since == null ? "not recorded (no baseline or no live quote)" : `${item.since > 0 ? "+" : ""}${item.since.toFixed(2)}%`}\n`
                            + `Day: ${item.day == null ? "no quote" : `${item.day > 0 ? "+" : ""}${item.day.toFixed(2)}%`}`}
                          style={{
                            display: "inline-flex", alignItems: "baseline", gap: "4px",
                            padding: "2px 7px", borderRadius: "5px", cursor: "pointer",
                            background: bg, color,
                            border: `1px solid ${selected ? color : "transparent"}`,
                            fontSize: "10px", fontWeight: 700, fontVariantNumeric: "tabular-nums",
                          }}>
                          {item.symbol}
                          <span style={{ fontSize: "9px", opacity: 0.9 }}>
                            {item.since == null ? "—" : `${item.since > 0 ? "+" : ""}${item.since.toFixed(1)}%`}
                          </span>
                          {/* Day change, dimmed: the move since the result is the
                              decision, the day's change is the context it sits in. */}
                          <span style={{
                            fontSize: "9px", fontWeight: 600, opacity: 0.75,
                            color: item.day == null ? "var(--text-faint)"
                              : item.day > 0 ? "var(--positive)" : item.day < 0 ? "var(--negative)" : "var(--text-faint)",
                          }}>
                            {item.day == null ? "· —" : `· ${item.day > 0 ? "+" : ""}${item.day.toFixed(1)}%d`}
                          </span>
                        </button>
                      );
                    })}
                  </span>
                </div>
              ))}
            </div>
          )}

          {/* Capped height: a heavy results day can produce hundreds of prompts,
              and the panel must not push the rest of the dashboard off-screen. */}
          <div style={{
            display: "flex", flexDirection: "column", gap: "12px",
            maxHeight: "560px", overflowY: "auto", paddingRight: "6px",
          }}>
            {visiblePendingResults.length === 0 && (
              <div style={{ padding: "18px", textAlign: "center", color: "var(--text-muted)", fontSize: "12px" }}>
                {pendingSearch.trim()
                  ? `No ${CATEGORY_LABEL[pendingCategory].toLowerCase()} match "${pendingSearch}".`
                  : `No ${CATEGORY_LABEL[pendingCategory].toLowerCase()} awaiting a decision on this date.`}
              </div>
            )}
            {visiblePendingResults.map(pending => {
              const form = getResultForm(pending.id);
              const busy = !!actionLoading[`result_${pending.id}`];
              const ai = pending.ai_analysis;
              const quote = marketQuotesMap[pending.symbol.toUpperCase()] || {};
              const isOpen = !!expandedResults[pending.id];
              // Only a quarterly filing has figures to read. Impact news gets no
              // Screener lookup and no earnings AI — asking would spend a
              // premium call to be told there are no numbers in an order win.
              const isResult = categoryOf(pending) === "results";
              const tab = resultTab[pending.id] || "ai";
              const modelChoice = reanalyseModel[pending.id] ?? "";
              const reanalysing = !!actionLoading[`reanalyse_${pending.id}`];
              return (
                <div key={pending.id} style={{
                  background: "rgba(15, 19, 25, 0.6)",
                  border: "1px solid rgba(224, 163, 62, 0.25)",
                  borderRadius: "10px",
                  padding: "14px"
                }}>
                  {/* Header line */}
                  <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "10px", marginBottom: "10px" }}>
                    {/* Blue means an order was placed against this filing, so the
                        row can be picked out of a list of hundreds at a glance. */}
                    <span style={{
                      fontSize: "16px", fontWeight: 800,
                      color: pending.position ? "var(--accent)" : "var(--text-primary)",
                    }}>
                      {pending.symbol}
                    </span>
                    {pending.position && (
                      <span title={`Order placed from this filing · config #${pending.position.config_id}`}
                        style={{
                          fontSize: "10px", fontWeight: 700, letterSpacing: "0.4px",
                          color: "var(--accent)", background: "var(--accent-bg)",
                          border: "1px solid var(--accent-border)",
                          padding: "2px 7px", borderRadius: "4px",
                        }}>
                        {pending.position.status.toUpperCase()}
                        {pending.position.quantity ? ` · ${pending.position.quantity}` : ""}
                        {pending.position.buy_price ? ` @ ₹${pending.position.buy_price}` : ""}
                        {pending.position.pnl != null ? ` · P&L ₹${pending.position.pnl}` : ""}
                      </span>
                    )}
                    <span style={{
                      fontSize: "10px", fontWeight: 700, letterSpacing: "0.5px",
                      color: pending.exchange === "nse" ? "var(--accent)" : "var(--ai)",
                      background: pending.exchange === "nse" ? "rgba(91, 157, 255,0.15)" : "rgba(164, 138, 224,0.15)",
                      padding: "3px 8px", borderRadius: "4px"
                    }}>{pending.exchange.toUpperCase()}</span>
                    {pending.company_name && (
                      <span style={{ fontSize: "12px", color: "var(--text-muted)", fontWeight: 500 }}>
                        {pending.company_name}
                      </span>
                    )}
                    {pending.tracking_ref && (
                      <span title="Tracking reference — the same code appears on the Telegram alert and the AI analysis"
                        style={{ fontSize: "10px", fontFamily: "ui-monospace, Menlo, monospace", color: "var(--text-muted)",
                                 background: "var(--surface-2)", padding: "2px 6px", borderRadius: "4px" }}>
                        {pending.tracking_ref}
                      </span>
                    )}
                    {pending.kind && pending.kind !== "result" && (
                      <span title="Impact news — good news worth acting on, with no quarterly figures to analyse. No Screener lookup and no earnings AI run on these."
                        style={{
                          fontSize: "10px", fontWeight: 700, letterSpacing: "0.4px",
                          color: "var(--positive)", background: "rgba(63, 191, 135, 0.14)",
                          border: "1px solid rgba(63, 191, 135, 0.3)",
                          padding: "2px 7px", borderRadius: "4px",
                        }}>
                        {pending.kind.replace(/_/g, " ").toUpperCase()}
                      </span>
                    )}
                    {isResult && pending.screener_signal && (
                      <span title={`Screener read: ${pending.screener_signal.reason}`}
                        style={{
                          fontSize: "10px", fontWeight: 700, padding: "2px 7px", borderRadius: "4px",
                          color: pending.screener_signal.tone === "pos" ? "var(--positive)"
                            : pending.screener_signal.tone === "neg" ? "var(--negative)" : "var(--text-muted)",
                          background: pending.screener_signal.tone === "pos" ? "rgba(63, 191, 135, 0.12)"
                            : pending.screener_signal.tone === "neg" ? "rgba(240, 115, 111, 0.12)" : "var(--surface-2)",
                        }}>
                        SCREENER {pending.screener_signal.label}
                      </span>
                    )}
                    {isResult && pending.all_positive && (
                      <span title="Revenue and profit both up year-on-year and quarter-on-quarter"
                        style={{ fontSize: "10px", fontWeight: 800, padding: "2px 7px", borderRadius: "4px", color: "var(--positive-strong)", background: "rgba(63, 191, 135, 0.18)" }}>
                        ALL POSITIVE
                      </span>
                    )}
                    {pending.paused && (
                      <span title="Live pricing is suspended for this row. The price shown is the last one fetched."
                        style={{ fontSize: "10px", fontWeight: 700, color: "var(--text-muted)",
                                 background: "var(--surface-2)", border: "1px solid rgba(125, 135, 153,0.3)",
                                 padding: "2px 7px", borderRadius: "4px" }}>
                        PAUSED
                      </span>
                    )}
                    {pending.deferred && (
                      <span title="Filed after the 15:20 IST cutoff — held for the 08:00 digest, no alert or AI overnight"
                        style={{ fontSize: "10px", fontWeight: 700, color: "var(--warning)",
                                 background: "var(--warning-bg)", padding: "2px 6px", borderRadius: "4px" }}>
                        DEFERRED
                      </span>
                    )}
                    <span style={{ fontSize: "12px", color: "var(--text-secondary)", flex: 1, minWidth: "200px" }}>{pending.title}</span>
                    {pending.event_time && (
                      <span style={{ fontSize: "11px", color: "var(--text-muted)" }}>
                        {new Date(pending.event_time).toLocaleString()}
                      </span>
                    )}
                    <button
                      onClick={() => openEarningsChart(pending.symbol, pending.instrument_key || undefined)}
                      title="Open price chart"
                      style={{
                        padding: "3px 9px", background: "var(--accent-bg)",
                        border: "1px solid var(--accent-border)", borderRadius: "5px",
                        color: "var(--accent)", fontSize: "10px", fontWeight: 700,
                        cursor: "pointer", display: "inline-flex", alignItems: "center", gap: "4px",
                      }}>
                      <TrendingUp size={11} /> Chart
                    </button>
                    {pending.attachment_url && (
                      <a href={pending.attachment_url} target="_blank" rel="noreferrer"
                         style={{ fontSize: "11px", color: "var(--accent)", display: "flex", alignItems: "center", gap: "4px" }}>
                        <ExternalLink size={12} /> Filing
                      </a>
                    )}
                  </div>

                  {/* Quote and timing belong to the stock, not to the AI verdict:
                      how long ago the filing landed decides whether the move is
                      still tradeable, and it has to be readable without opening
                      anything. */}
                  <div style={{ marginBottom: "10px" }}>
                    <ResultQuoteStrip
                      p={pending}
                      quote={quote}
                      signal={get2MinSignal(pending.symbol, quote)}
                      flash={priceFlash[pending.symbol.toUpperCase()]}
                    />
                    <LifecycleStrip p={pending} />
                  </div>

                  {/* Order form */}
                  <div style={{ display: "flex", flexWrap: "wrap", alignItems: "flex-end", gap: "10px" }}>
                    <label style={{ fontSize: "10px", color: "var(--text-muted)" }}>
                      QTY
                      <input type="number" min={1} value={form.quantity}
                        onChange={e => updateResultForm(pending.id, { quantity: parseInt(e.target.value) || 1 })}
                        style={{ display: "block", width: "70px", marginTop: "3px", padding: "6px 8px", background: "rgba(10, 13, 18,0.8)", border: "1px solid rgba(255,255,255,0.12)", borderRadius: "6px", color: "var(--text-primary)", fontSize: "12px" }} />
                    </label>
                    <label style={{ fontSize: "10px", color: "var(--text-muted)" }}>
                      TYPE
                      <select value={form.order_type}
                        onChange={e => updateResultForm(pending.id, { order_type: e.target.value })}
                        style={{ display: "block", marginTop: "3px", padding: "6px 8px", background: "rgba(10, 13, 18,0.8)", border: "1px solid rgba(255,255,255,0.12)", borderRadius: "6px", color: "var(--text-primary)", fontSize: "12px" }}>
                        <option value="MARKET">MARKET</option>
                        <option value="LIMIT">LIMIT</option>
                      </select>
                    </label>
                    {form.order_type === "LIMIT" && (
                      <label style={{ fontSize: "10px", color: "var(--text-muted)" }}>
                        LIMIT ₹
                        <input type="number" step="0.05" value={form.limit_price}
                          onChange={e => updateResultForm(pending.id, { limit_price: e.target.value })}
                          style={{ display: "block", width: "90px", marginTop: "3px", padding: "6px 8px", background: "rgba(10, 13, 18,0.8)", border: "1px solid rgba(255,255,255,0.12)", borderRadius: "6px", color: "var(--text-primary)", fontSize: "12px" }} />
                      </label>
                    )}
                    <label style={{ fontSize: "10px", color: "var(--text-muted)" }}>
                      SL %
                      <input type="number" step="0.5" min={0.5} value={form.stoploss_pct}
                        onChange={e => updateResultForm(pending.id, { stoploss_pct: parseFloat(e.target.value) || 2 })}
                        style={{ display: "block", width: "70px", marginTop: "3px", padding: "6px 8px", background: "rgba(10, 13, 18,0.8)", border: "1px solid rgba(255,255,255,0.12)", borderRadius: "6px", color: "var(--text-primary)", fontSize: "12px" }} />
                    </label>
                    <label style={{ fontSize: "10px", color: "var(--text-muted)" }}>
                      BROKER
                      <select value={form.broker}
                        onChange={e => updateResultForm(pending.id, { broker: e.target.value })}
                        style={{ display: "block", marginTop: "3px", padding: "6px 8px", background: "rgba(10, 13, 18,0.8)", border: "1px solid rgba(255,255,255,0.12)", borderRadius: "6px", color: "var(--text-primary)", fontSize: "12px" }}>
                        <option value="upstox">Upstox</option>
                        <option value="zerodha">Zerodha</option>
                      </select>
                    </label>

                    {/* Once a position exists the buy is done; the only action
                        left on this filing is getting out of it. */}
                    {!pending.position && (
                      <button onClick={() => handlePlaceResultOrder(pending)} disabled={busy}
                        style={{
                          padding: "8px 16px", background: busy ? "rgba(63, 191, 135,0.4)" : "var(--positive)",
                          border: "none", borderRadius: "6px", color: "var(--on-accent)", fontSize: "12px", fontWeight: 700,
                          cursor: busy ? "not-allowed" : "pointer", display: "flex", alignItems: "center", gap: "6px"
                        }}>
                        <ShoppingBag size={14} />
                        {busy ? "Placing…" : canTradeNow() ? "Place Buy Order" : "Queue Buy for 09:15"}
                      </button>
                    )}
                    {pending.position?.can_sell && (
                      <button onClick={() => handleSellResultPosition(pending)}
                        disabled={!!actionLoading[`sell_${pending.id}`]}
                        style={{
                          padding: "8px 16px",
                          background: actionLoading[`sell_${pending.id}`] ? "rgba(240, 115, 111,0.4)" : "var(--negative)",
                          border: "none", borderRadius: "6px", color: "var(--on-accent)", fontSize: "12px", fontWeight: 700,
                          cursor: actionLoading[`sell_${pending.id}`] ? "not-allowed" : "pointer",
                          display: "flex", alignItems: "center", gap: "6px"
                        }}>
                        <DollarSign size={14} /> {actionLoading[`sell_${pending.id}`] ? "Selling…" : "Sell Now"}
                      </button>
                    )}
                    {pending.position && !pending.position.can_sell && (
                      <span style={{ fontSize: "11px", color: "var(--text-muted)" }}>
                        {pending.position.status === "sold"
                          ? `Sold${pending.position.sell_price ? ` at ₹${pending.position.sell_price}` : ""}`
                          : `Position is '${pending.position.status}' — sell available once the buy fills`}
                      </span>
                    )}
                    {/* What this order costs, against what you said you have.
                        Priced at the live LTP, so it moves with the quote and is
                        an estimate for a MARKET order, not a quotation. */}
                    {(() => {
                      const ltp = quote?.last_price || 0;
                      if (!ltp || !form.quantity) return null;
                      const cost = ltp * form.quantity;
                      const funds = parseFloat(availableFunds);
                      const haveFunds = isFinite(funds) && funds > 0;
                      const left = haveFunds ? funds - cost : null;
                      const short = left != null && left < 0;
                      const money = (v: number) =>
                        `₹${Math.abs(v).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
                      return (
                        <span style={{ fontSize: "11px", lineHeight: 1.35, whiteSpace: "nowrap" }}
                          title={`${form.quantity} × ₹${ltp.toFixed(2)} at the current price`}>
                          <span style={{ color: "var(--text-muted)" }}>Needs </span>
                          <b style={{ color: "var(--text-primary)", fontVariantNumeric: "tabular-nums" }}>{money(cost)}</b>
                          {left != null && (
                            <>
                              <span style={{ color: "var(--text-muted)" }}>{short ? " · short by " : " · leaves "}</span>
                              <b style={{ color: short ? "var(--negative)" : "var(--positive)", fontVariantNumeric: "tabular-nums" }}>
                                {money(left)}
                              </b>
                            </>
                          )}
                        </span>
                      );
                    })()}

                    {/* The only per-card action besides ordering. Dismiss used
                        to sit beside it and removed the card outright, which
                        was the wrong shape for a panel that is now the record
                        of the last 30 days — it is still reachable from the
                        Telegram alert's inline buttons, where hiding a prompt
                        you have decided against is what you actually want. */}
                    <button onClick={() => handleTogglePause(pending.id)}
                      disabled={!!actionLoading[`pause_${pending.id}`]}
                      title={pending.paused
                        ? "Resume refreshing this row's live price"
                        : "Stop refreshing this row's live price. The card stays; the last price is kept."}
                      style={{
                        padding: "8px 14px", background: "transparent",
                        border: `1px solid ${pending.paused ? "rgba(224, 163, 62,0.5)" : "rgba(125, 135, 153,0.35)"}`,
                        borderRadius: "6px",
                        color: pending.paused ? "var(--warning)" : "var(--text-muted)",
                        fontSize: "12px", fontWeight: 600,
                        cursor: actionLoading[`pause_${pending.id}`] ? "not-allowed" : "pointer"
                      }}>
                      {pending.paused ? "Resume prices" : "Pause prices"}
                    </button>

                  </div>

                  {/* One collapsed row carries the AI state, so the verdict is
                      still visible without the five-row table pushing the next
                      order card off screen. Impact news has no AI section at
                      all: there are no quarterly figures to extract. */}
                  <div style={{ marginTop: "10px", paddingTop: "9px", borderTop: "1px solid var(--border-subtle)",
                                display: isResult ? undefined : "none" }}>
                    <button
                      onClick={() => setExpandedResults(prev => ({ ...prev, [pending.id]: !prev[pending.id] }))}
                      style={{
                        display: "flex", alignItems: "center", gap: "8px", width: "100%",
                        background: "transparent", border: "none", padding: 0,
                        cursor: "pointer", textAlign: "left", flexWrap: "wrap",
                      }}>
                      <ChevronRight
                        size={13}
                        style={{
                          color: "var(--ai)", flexShrink: 0,
                          transform: isOpen ? "rotate(90deg)" : "none",
                          transition: "transform 0.15s",
                        }} />
                      <span style={{ fontSize: "11px", fontWeight: 700, color: "var(--ai)" }}>EARNINGS ANALYSIS</span>

                      {pending.ai_status === "done" && ai ? (
                        <>
                          <VerdictBadge verdict={ai.ai_suggestion} />
                          {ai.extraction_ok === false && (
                            <span style={{ fontSize: "10px", color: "var(--text-muted)", fontStyle: "italic" }}>
                              figures not extractable
                            </span>
                          )}
                          {ai.validation?.issues?.length ? (
                            <span style={{ fontSize: "10px", color: "var(--warning)", fontWeight: 700 }}>
                              ⚠ {ai.validation.issues.length} consistency issue{ai.validation.issues.length > 1 ? "s" : ""}
                            </span>
                          ) : null}
                        </>
                      ) : pending.ai_status === "failed" ? (
                        <span style={{ fontSize: "10px", color: "var(--negative-strong)" }}>failed</span>
                      ) : pending.deferred ? (
                        <span style={{ fontSize: "10px", color: "var(--text-muted)" }}>not analysed — after cutoff</span>
                      ) : (
                        <span style={{ fontSize: "10px", color: "var(--text-muted)", display: "inline-flex", alignItems: "center", gap: "5px" }}>
                          <RefreshCw size={11} className="spin" /> running…
                        </span>
                      )}

                      <span style={{ marginLeft: "auto", fontSize: "10px", color: "var(--text-faint)" }}>
                        {isOpen ? "hide" : "details"}
                      </span>
                    </button>

                    {isOpen && (
                      <CardBoundary label="This analysis">
                      <div style={{ marginTop: "10px" }}>
                        {/* Two ways of getting figures out of the same filing.
                            OCR reads the PDF directly; AI asks a model. They are
                            tabs rather than a setting because the interesting
                            question is where two readings disagree. */}
                        <div style={{ display: "flex", gap: "6px", marginBottom: "10px" }}>
                          {(["ai", "ocr"] as const).map(t => (
                            <button key={t}
                              onClick={() => setResultTab(prev => ({ ...prev, [pending.id]: t }))}
                              style={{
                                padding: "4px 12px", borderRadius: "5px", cursor: "pointer",
                                fontSize: "10px", fontWeight: 700, letterSpacing: "0.4px",
                                background: tab === t ? "var(--ai-bg)" : "transparent",
                                border: `1px solid ${tab === t ? "rgba(164, 138, 224,0.5)" : "rgba(125, 135, 153,0.25)"}`,
                                color: tab === t ? "var(--ai)" : "var(--text-muted)",
                              }}>
                              {t.toUpperCase()}
                            </button>
                          ))}
                        </div>

                        {tab === "ocr" ? (() => {
                          const rawAny = pending.extraction as Extraction | null | undefined;
                          // Shape-check what came out of the database rather
                          // than trusting it. This expression is an inline IIFE
                          // in the card's JSX, so React evaluates it during the
                          // *parent's* render — before the error boundary that
                          // wraps it ever renders. A throw here escapes the
                          // boundary and unmounts the dashboard, which is what
                          // a blank screen is. Components rendered inside the
                          // boundary are protected; this is not.
                          const raw: Extraction | null =
                            rawAny && typeof rawAny === "object" && rawAny.confidence
                              ? rawAny : null;
                          // "READING" is the placeholder the endpoint returns
                          // while the job runs; it carries no figures, so the
                          // card shows progress rather than an empty table.
                          const reading = raw?.confidence?.tier === "READING";
                          const ex = reading ? null : raw;
                          const busy = !!actionLoading[`extract_${pending.id}`] || reading;
                          const LABEL: Record<string, string> = {
                            revenue: "Revenue", expenses: "Expenses",
                            other_income: "Other income", ebitda: "EBITDA",
                            pbt: "PBT", pat: "Profit after tax",
                          };
                          return (
                            <div>
                              <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "10px", marginBottom: "10px" }}>
                                <button onClick={() => handleExtract(pending, !!ex)} disabled={busy}
                                  title="Read the filing's PDF and check the figures against Screener"
                                  style={{
                                    display: "inline-flex", alignItems: "center", gap: "5px",
                                    padding: "5px 12px", borderRadius: "6px",
                                    background: "var(--ai-bg)", border: "1px solid rgba(164, 138, 224,0.45)",
                                    color: "var(--ai)", fontSize: "11px", fontWeight: 700,
                                    cursor: busy ? "not-allowed" : "pointer", opacity: busy ? 0.6 : 1,
                                  }}>
                                  <RefreshCw size={11} className={busy ? "spin" : undefined} />
                                  {busy ? "Reading the filing…" : ex ? "Read again" : "Read the filing"}
                                </button>
                                {ex && <ConfidenceBadge c={ex.confidence} />}
                                {ex?.engine && (
                                  <span style={{ fontSize: "10px", color: "var(--text-muted)" }}>
                                    via {ex.engine}{ex.page ? ` · page ${ex.page}` : ""}
                                    {ex.basis ? ` · ${ex.basis}` : ""}{ex.unit ? ` · ${ex.unit}` : ""}
                                  </span>
                                )}
                              </div>

                              {!ex ? (
                                <div style={{ padding: "16px", borderRadius: "8px", textAlign: "center",
                                              background: "var(--surface-2)", border: "1px dashed rgba(125, 135, 153,0.3)",
                                              fontSize: "11px", color: "var(--text-muted)", lineHeight: 1.6 }}>
                                  {reading
                                    ? raw?.confidence?.reason
                                    : "Reads the figures out of the filing itself, then checks each one against Screener. Two independent sources agreeing is evidence; the model's reading on its own is not."}
                                </div>
                              ) : (
                                <>
                                  <div style={{ fontSize: "11px", color: "var(--text-secondary)", lineHeight: 1.55, marginBottom: "10px" }}>
                                    {ex.confidence.reason}
                                  </div>

                                  {Array.isArray(ex.confidence.flags) && ex.confidence.flags.length > 0 && (
                                    <div style={{ display: "flex", flexWrap: "wrap", gap: "5px", marginBottom: "10px" }}>
                                      {ex.confidence.flags.map(f => (
                                        <span key={f} title="A validation check the extractor raised on this reading"
                                          style={{ fontSize: "9px", fontFamily: "ui-monospace, Menlo, monospace",
                                                   color: "var(--warning)", background: "var(--warning-bg)",
                                                   padding: "2px 6px", borderRadius: "4px" }}>
                                          {f}
                                        </span>
                                      ))}
                                    </div>
                                  )}

                                  {ex.quarter && !ex.covers_screener_quarter && ex.screener_quarter && (
                                    <div style={{ fontSize: "10px", color: "var(--warning)", marginBottom: "8px" }}>
                                      The filing covers {ex.quarter}; Screener's latest is {ex.screener_quarter}.
                                      The two describe different periods, so the gaps below are not errors.
                                    </div>
                                  )}

                                  {Array.isArray(ex.comparisons) && ex.comparisons.length > 0 && (
                                    <div style={{ overflowX: "auto" }}>
                                      <table style={{ borderCollapse: "collapse", fontSize: "11px", width: "100%", minWidth: "440px" }}>
                                        <thead>
                                          <tr>
                                            {["", `From the filing${ex.quarter ? ` (${ex.quarter})` : ""}`, "Screener", "Gap", ""].map((h, i) => (
                                              <th key={i} style={{ textAlign: i === 0 || i === 4 ? "left" : "right",
                                                                   padding: "4px 8px", color: "var(--text-muted)", fontWeight: 600,
                                                                   fontSize: "10px", borderBottom: "1px solid var(--border-subtle)",
                                                                   whiteSpace: "nowrap" }}>{h}</th>
                                            ))}
                                          </tr>
                                        </thead>
                                        <tbody>
                                          {ex.comparisons.map(c => (
                                            <tr key={c.metric}>
                                              <td style={{ padding: "4px 8px", fontWeight: 600, color: "var(--text-primary)", whiteSpace: "nowrap" }}>
                                                {LABEL[c.metric] || c.metric}
                                              </td>
                                              <td style={{ padding: "4px 8px", textAlign: "right", whiteSpace: "nowrap",
                                                           color: c.pdf == null ? "var(--text-faint)" : "var(--text-primary)" }}>
                                                {c.pdf == null ? "—" : c.pdf.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                                              </td>
                                              <td style={{ padding: "4px 8px", textAlign: "right", whiteSpace: "nowrap",
                                                           color: c.screener == null ? "var(--text-faint)" : "var(--text-secondary)" }}>
                                                {c.screener == null ? "—" : c.screener.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                                              </td>
                                              <td style={{ padding: "4px 8px", textAlign: "right", whiteSpace: "nowrap",
                                                           color: c.agrees === true ? "var(--positive)"
                                                             : c.agrees === false ? "var(--negative)" : "var(--text-faint)" }}>
                                                {c.gap_pct == null ? "—" : `${c.gap_pct > 0 ? "+" : ""}${c.gap_pct.toFixed(2)}%`}
                                              </td>
                                              <td style={{ padding: "4px 8px", fontSize: "9px", color: "var(--text-faint)", lineHeight: 1.4 }}>
                                                {c.note || (c.agrees === true ? "agrees" : c.agrees === false ? "differs" : "")}
                                              </td>
                                            </tr>
                                          ))}
                                        </tbody>
                                      </table>
                                    </div>
                                  )}
                                </>
                              )}
                            </div>
                          );
                        })() : (
                          <>
                            {/* Model choice applies to this run only; it does not
                                change the configured default for future filings. */}
                            <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "8px", marginBottom: "10px" }}>
                              <span style={{ fontSize: "10px", color: "var(--text-muted)", fontWeight: 600 }}>MODEL</span>
                              <select
                                value={modelChoice}
                                onChange={e => setReanalyseModel(prev => ({ ...prev, [pending.id]: e.target.value }))}
                                style={{ padding: "5px 8px", fontSize: "11px", background: "var(--bg-sunken)", border: "1px solid var(--border-default)", borderRadius: "6px", color: "var(--text-primary)", maxWidth: "280px" }}>
                                {AI_MODELS.map(m => <option key={m.id} value={m.id}>{m.label}</option>)}
                              </select>
                              {modelChoice === "__custom__" && (
                                <input
                                  value={customModel[pending.id] || ""}
                                  onChange={e => setCustomModel(prev => ({ ...prev, [pending.id]: e.target.value }))}
                                  placeholder="provider/model-id"
                                  style={{ padding: "5px 8px", fontSize: "11px", minWidth: "200px", background: "var(--bg-sunken)", border: "1px solid var(--border-default)", borderRadius: "6px", color: "var(--text-primary)" }}
                                />
                              )}
                              <button
                                onClick={() => handleReanalyse(pending)}
                                disabled={reanalysing || pending.ai_status === "running"}
                                title="Run the earnings analysis again. The verdict on screen is kept until the new one lands."
                                style={{
                                  display: "inline-flex", alignItems: "center", gap: "5px",
                                  padding: "5px 12px", borderRadius: "6px",
                                  background: "var(--ai-bg)", border: "1px solid rgba(164, 138, 224,0.45)",
                                  color: "var(--ai)", fontSize: "11px", fontWeight: 700,
                                  cursor: (reanalysing || pending.ai_status === "running") ? "not-allowed" : "pointer",
                                  opacity: (reanalysing || pending.ai_status === "running") ? 0.6 : 1,
                                }}>
                                <RefreshCw size={11} className={pending.ai_status === "running" ? "spin" : undefined} />
                                {pending.ai_status === "running" ? "Analysing…" : "Re-analyse"}
                              </button>
                            </div>

                            {pending.ai_status === "done" && ai ? (
                              <div style={{ marginTop: "8px" }}>
                                <MetricsTable metrics={ai.metrics} />
                                <ValidationNotice v={ai.validation} />
                                <div style={{ fontSize: "11px", color: "var(--text-secondary)", lineHeight: 1.55, marginTop: "8px" }}>{ai.ai_summary}</div>
                                {(ai.future_growth_outlook || ai.future_projected_numbers) && (
                                  <div style={{ display: "flex", flexDirection: "column", gap: "3px", marginTop: "8px", fontSize: "10px", color: "var(--text-muted)" }}>
                                    {ai.future_growth_outlook && <span><b style={{ color: "var(--text-primary)" }}>Outlook:</b> {ai.future_growth_outlook}</span>}
                                    {ai.future_projected_numbers && <span><b style={{ color: "var(--text-primary)" }}>Projected:</b> {ai.future_projected_numbers}</span>}
                                  </div>
                                )}
                              </div>
                            ) : pending.ai_status === "failed" ? (
                              <div style={{ fontSize: "11px", color: "var(--negative-strong)", marginTop: "8px" }}>
                                AI analysis failed — place the order on the filing itself, or try another model above.
                              </div>
                            ) : null}

                            {/* Screener's own figures for the quarter, beside
                                ours. This is what the Results Digest showed in a
                                second list of these same rows. */}
                            {Array.isArray(pending.comparison) && pending.comparison.length > 0 && (
                              <div style={{ marginTop: "12px", paddingTop: "10px", borderTop: "1px solid var(--border-subtle)" }}>
                                <div style={{ fontSize: "10px", fontWeight: 700, color: "var(--info)", marginBottom: "6px", letterSpacing: "0.4px" }}>
                                  SCREENER COMPARISON
                                </div>
                                {!pending.screener?.ok && (
                                  <div style={{ fontSize: "10px", color: "var(--text-muted)", marginBottom: "6px" }}>
                                    {pending.screener?.error === "not fetched yet"
                                      ? "Screener figures still being fetched — refresh shortly."
                                      : `Screener unavailable — ${pending.screener?.error || "no data"}`}
                                  </div>
                                )}
                                <ScreenerComparison
                                  rows={pending.comparison}
                                  quarter={pending.screener?.quarter}
                                  analysed={pending.ai_status === "done" && !!ai}
                                />
                                {pending.screener?.source_url && (
                                  <div style={{ fontSize: "10px", color: "var(--text-faint)", marginTop: "6px" }}>
                                    <a href={pending.screener.source_url} target="_blank" rel="noreferrer" style={{ color: "var(--text-muted)" }}>screener.in</a>
                                  </div>
                                )}
                              </div>
                            )}
                          </>
                        )}
                      </div>
                      </CardBoundary>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* The Results Digest panel used to live here. It was a second list
          of the same PendingResultOrder rows the panel above renders — same
          query, same day, one carrying the order form and one the figures.
          Screener's comparison, the any-date picker and the PDF export have
          moved onto Order Decisions; the 08:00 HTML page and Telegram alert
          are unchanged, still served by /api/trading/digest/<date>. */}

      {/* UPCOMING EARNINGS CALENDAR SECTION */}
      <div style={{
        background: "rgba(22, 27, 36, 0.6)",
        border: "1px solid rgba(91, 157, 255, 0.2)",
        borderRadius: "14px",
        padding: "18px",
        marginBottom: "20px"
      }}>
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: "10px", marginBottom: "14px" }}>
          <h3 style={{ fontSize: "15px", fontWeight: "700", color: "var(--accent)", display: "flex", alignItems: "center", gap: "8px", margin: 0 }}>
            <Calendar size={18} /> 📊 Upcoming Earnings Calendar ({processedUpcomingEarnings.length})
          </h3>
          <span style={{ fontSize: "11px", color: "var(--text-muted)", background: "rgba(91, 157, 255,0.1)", padding: "4px 10px", borderRadius: "20px" }}>
            Real-Time Corporate Earnings Disclosures
          </span>
        </div>

        {/* Toolbar: Search, Date Filter, Quick Filter Pills & Sort */}
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: "10px", marginBottom: "14px", backgroundColor: "rgba(15, 19, 25, 0.5)", padding: "10px 12px", borderRadius: "10px", border: "1px solid rgba(255, 255, 255, 0.05)" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
            {/* Search Input */}
            <input
              type="text"
              placeholder="Search symbol..."
              value={earningsSearch}
              onChange={(e) => setEarningsSearch(e.target.value)}
              style={{
                backgroundColor: "rgba(15, 19, 25, 0.9)",
                border: "1px solid rgba(91, 157, 255, 0.3)",
                borderRadius: "8px",
                color: "var(--text-primary)",
                fontSize: "12px",
                padding: "6px 12px",
                width: "130px",
                outline: "none"
              }}
            />

            {/* Calendar Date Picker Filter */}
            <div style={{ display: "flex", alignItems: "center", gap: "4px" }}>
              <span style={{ fontSize: "11px", color: "var(--text-muted)", fontWeight: "600" }}>Date:</span>
              <input
                type="date"
                value={earningsDateFilter}
                onChange={(e) => setEarningsDateFilter(e.target.value)}
                style={{
                  backgroundColor: "var(--bg-base)",
                  border: "1px solid rgba(91, 157, 255, 0.4)",
                  borderRadius: "8px",
                  color: "var(--info)",
                  fontSize: "11px",
                  fontWeight: "700",
                  padding: "5px 8px",
                  outline: "none",
                  cursor: "pointer"
                }}
              />
            </div>

            {/* Date Quick Pills */}
            <button
              onClick={() => setEarningsDateFilter(todayStr)}
              style={{
                backgroundColor: earningsDateFilter === todayStr ? "rgba(79, 184, 217, 0.25)" : "rgba(15, 19, 25, 0.6)",
                border: earningsDateFilter === todayStr ? "1px solid var(--info)" : "1px solid rgba(255, 255, 255, 0.1)",
                color: earningsDateFilter === todayStr ? "var(--info)" : "var(--text-muted)",
                fontSize: "11px",
                fontWeight: "700",
                padding: "5px 10px",
                borderRadius: "6px",
                cursor: "pointer",
                transition: "all 0.15s"
              }}
            >
              📅 Today
            </button>

            <button
              onClick={() => setEarningsDateFilter("")}
              style={{
                backgroundColor: earningsDateFilter === "" ? "rgba(91, 157, 255, 0.25)" : "rgba(15, 19, 25, 0.6)",
                border: earningsDateFilter === "" ? "1px solid var(--accent)" : "1px solid rgba(255, 255, 255, 0.1)",
                color: earningsDateFilter === "" ? "var(--accent)" : "var(--text-muted)",
                fontSize: "11px",
                fontWeight: "700",
                padding: "5px 10px",
                borderRadius: "6px",
                cursor: "pointer",
                transition: "all 0.15s"
              }}
            >
              🌐 All Dates
            </button>

            <button
              onClick={handleSyncQuotesNow}
              disabled={syncingQuotes}
              title="Click to manually register all earnings stocks into Upstox live streaming quote feed"
              style={{
                backgroundColor: "rgba(63, 191, 135, 0.2)",
                border: "1px solid var(--positive)",
                color: "var(--positive-strong)",
                fontSize: "11px",
                fontWeight: "700",
                padding: "5px 12px",
                borderRadius: "6px",
                cursor: "pointer",
                display: "flex",
                alignItems: "center",
                gap: "5px",
                transition: "all 0.15s",
                boxShadow: "0 2px 8px rgba(63, 191, 135, 0.25)"
              }}
            >
              <RefreshCw size={12} className={syncingQuotes ? "animate-spin" : ""} />
              {syncingQuotes ? "Syncing Quotes..." : "🔄 Sync Live Quotes"}
            </button>

            {/* Same lever as the tracker's tables: this calendar can run to 139
                symbols, and the quote allowance is shared with the results panel. */}
            <button
              onClick={() => setEarningsPaused(v => !v)}
              title={earningsPaused
                ? "Automatic price updates are off for this calendar. Click to resume."
                : "Prices update with the other panels. Click to pause and free up Upstox quota."}
              style={{
                display: "inline-flex", alignItems: "center", gap: "5px",
                padding: "5px 10px", borderRadius: "8px", cursor: "pointer",
                fontSize: "11px", fontWeight: 700,
                background: earningsPaused ? "rgba(224, 163, 62, 0.12)" : "rgba(63, 191, 135, 0.12)",
                border: `1px solid ${earningsPaused ? "rgba(224, 163, 62, 0.35)" : "rgba(63, 191, 135, 0.35)"}`,
                color: earningsPaused ? "var(--warning)" : "var(--positive)",
              }}
            >
              {earningsPaused ? <Play size={12} /> : <Pause size={12} />}
              {earningsPaused ? "PAUSED" : "LIVE"}
            </button>
          </div>

          {/* Sort By Dropdown */}
          <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
            <span style={{ fontSize: "11px", color: "var(--text-muted)", fontWeight: "600" }}>Sort By:</span>
            <select
              value={earningsSortBy}
              onChange={(e: any) => setEarningsSortBy(e.target.value)}
              style={{
                backgroundColor: "var(--bg-base)",
                border: "1px solid rgba(91, 157, 255, 0.3)",
                color: "var(--text-primary)",
                fontSize: "11px",
                fontWeight: "700",
                padding: "5px 10px",
                borderRadius: "8px",
                cursor: "pointer",
                outline: "none"
              }}
            >
              <option value="change_desc">📊 Change % (Highest First)</option>
              <option value="change_asc">📉 Change % (Lowest First)</option>
              <option value="date">📅 Meeting Date (Earliest First)</option>
              <option value="return_desc">📈 1Y Return % (Highest Gainers First)</option>
              <option value="return_asc">📉 1Y Return % (Lowest / Worst First)</option>
            </select>
          </div>
        </div>

        {loading && upcomingEarnings.length === 0 ? (
          <div style={{ color: "var(--accent)", fontSize: "12px", textAlign: "center", padding: "20px", display: "flex", alignItems: "center", justifyContent: "center", gap: "8px" }}>
            <RefreshCw size={14} className="animate-spin" /> Loading upcoming earnings calendar...
          </div>
        ) : processedUpcomingEarnings.length === 0 ? (
          <div style={{ color: "var(--text-faint)", fontSize: "12px", textAlign: "center", padding: "16px" }}>
            No earnings disclosures match the selected date ({earningsDateFilter || 'All Dates'}) / search filter.
          </div>
        ) : (
          <div style={{ maxHeight: "420px", overflowY: "auto", border: "1px solid rgba(255, 255, 255, 0.08)", borderRadius: "10px" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", textAlign: "left", fontSize: "11px" }}>
              <thead>
                <tr style={{ background: "rgba(15, 19, 25, 0.95)", borderBottom: "1px solid rgba(255, 255, 255, 0.1)", color: "var(--text-muted)" }}>
                  <th style={{ padding: "10px 12px" }}>SYMBOL</th>
                  <th style={{ padding: "10px 12px" }}>COMPANY</th>
                  <th style={{ padding: "10px 12px", textAlign: "right" }}>LTP</th>
                  <th style={{ padding: "10px 12px", textAlign: "right" }}>CHANGE</th>
                  <th style={{ padding: "10px 12px", textAlign: "right" }}>PREV CLOSE</th>
                  <th style={{ padding: "10px 12px", textAlign: "right" }}>DAY HIGH</th>
                  <th style={{ padding: "10px 12px", textAlign: "center", width: "100px" }}>SENTIMENT (B/S)</th>
                  <th style={{ padding: "10px 12px", textAlign: "center", width: "110px" }}>BUY/SELL QTY</th>
                  <th style={{ padding: "10px 12px", textAlign: "center", width: "115px" }}>
                    2M SIGNAL
                    <span title="Short-term (2 min) trend calculated from order book depth sentiment and quantity dynamics." style={{ cursor: "help", fontSize: "10px", color: "var(--text-faint)", marginLeft: "4px" }}>ⓘ</span>
                  </th>
                  <th style={{ padding: "10px 12px", textAlign: "center" }}>MEETING DATE</th>
                  <th style={{ padding: "10px 12px", textAlign: "right" }}>ACTIONS</th>
                </tr>
              </thead>
              <tbody>
                {processedUpcomingEarnings.map((item) => {
                  const ret1y = item.return_1y || item.returns_1y || "N/A";
                  const sym = (item.symbol || "").toUpperCase();
                  const q = marketQuotesMap[sym] || {};

                  // Live quote only. These readouts used to fall back to a hash
                  // of the symbol when no quote arrived — a plausible-looking
                  // price, a plausible-looking depth split and a plausible-looking
                  // signal, none of them real. A blank is the honest answer.
                  const ltp: number = (q.last_price && q.last_price > 0) ? q.last_price : 0;
                  const prevClose: number = (q.close && q.close > 0) ? q.close : 0;
                  const dayHigh: number = (q.high && q.high > 0) ? q.high : 0;

                  let changePct: number | null = null;
                  if (q.change !== undefined && q.change !== 0.0) {
                    changePct = q.change;
                  } else if (ltp > 0 && prevClose > 0) {
                    changePct = ((ltp - prevClose) / prevClose) * 100;
                  }
                  const isUp = (changePct ?? 0) >= 0;

                  // Micro Depth & Quantities for Stock View
                  const buyPct: number | null = q.depth_buy_pct !== undefined ? Math.round(q.depth_buy_pct) : null;
                  const sellPct = buyPct == null ? null : 100 - buyPct;

                  const buyQty: number = q.total_buy_qty || 0;
                  const sellQty: number = q.total_sell_qty || 0;
                  const totalQty = buyQty + sellQty;

                  // Same 2m order-book signal as the watchlist and the result prompts
                  const sig = get2MinSignal(sym, q);
                  const sigColor = sig.tone === "up" ? "var(--positive-strong)" : sig.tone === "down" ? "var(--negative-strong)" : "var(--warning)";
                  const sigBg = sig.tone === "up" ? "rgba(63, 191, 135, 0.12)" : sig.tone === "down" ? "rgba(240, 115, 111, 0.12)" : "rgba(224, 163, 62, 0.12)";
                  const sigBorder = sig.tone === "up" ? "rgba(63, 191, 135, 0.3)" : sig.tone === "down" ? "rgba(240, 115, 111, 0.3)" : "rgba(224, 163, 62, 0.3)";

                  return (
                    <tr key={item.id || item.symbol} style={{ borderBottom: "1px solid rgba(255, 255, 255, 0.05)", background: "var(--surface-1)" }}>
                      {/* 1. SYMBOL */}
                      <td style={{ padding: "10px 12px", fontWeight: "800", color: "var(--text-primary)", fontSize: "12px" }}>
                        <span
                          onClick={() => openEarningsChart(item.symbol, item.instrument_key)}
                          style={{ cursor: "pointer", color: "var(--accent)", textDecoration: "none", transition: "color 0.2s" }}
                          onMouseEnter={(e) => (e.currentTarget.style.color = "var(--accent-strong)")}
                          onMouseLeave={(e) => (e.currentTarget.style.color = "var(--accent)")}
                          title={`Click to view ${item.symbol} chart`}
                        >
                          {item.symbol}
                        </span>
                      </td>

                      {/* 2. COMPANY */}
                      <td style={{ padding: "10px 12px", color: "var(--text-muted)", fontSize: "11px", maxWidth: "160px" }}>
                        <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {item.title || item.symbol}
                        </div>
                      </td>

                      {/* 3. LTP */}
                      <td style={{ padding: "10px 12px", textAlign: "right", fontWeight: "700", color: "var(--text-primary)", fontSize: "12px" }}>
                        {ltp > 0 ? `₹${ltp.toFixed(2)}` : "—"}
                      </td>

                      {/* 4. CHANGE */}
                      <td style={{ padding: "10px 12px", textAlign: "right", fontWeight: "700", fontSize: "11px", color: changePct == null ? "var(--text-faint)" : isUp ? "var(--positive-strong)" : "var(--negative-strong)" }}>
                        {changePct == null ? "—" : `${isUp ? "+" : ""}${changePct.toFixed(2)}%`}
                      </td>

                      {/* 5. PREV CLOSE */}
                      <td style={{ padding: "10px 12px", textAlign: "right", color: "var(--text-muted)", fontSize: "11px" }}>
                        {prevClose > 0 ? `₹${prevClose.toFixed(2)}` : "—"}
                      </td>

                      {/* 6. DAY HIGH */}
                      <td style={{ padding: "10px 12px", textAlign: "right", color: "var(--positive-strong)", fontWeight: "600", fontSize: "11px" }}>
                        {dayHigh > 0 ? `₹${dayHigh.toFixed(2)}` : "—"}
                      </td>

                      {/* 7. SENTIMENT (B/S) */}
                      <td
                        style={{ verticalAlign: "middle", padding: "8px 10px", textAlign: "center", cursor: "pointer" }}
                        onMouseEnter={(e) => handleSentimentMouseEnter(e, item, buyPct ?? 50, buyQty, sellQty, changePct ?? 0)}
                        onMouseLeave={handleSentimentMouseLeave}
                      >
                        {buyPct == null ? (
                          <span style={{ color: "var(--text-faint)", fontSize: "10px" }}>—</span>
                        ) : (
                          <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "2px", width: "85px", margin: "0 auto" }}>
                            <div style={{ display: "flex", justifyContent: "space-between", width: "100%", fontSize: "9px", fontWeight: "800" }}>
                              <span style={{ color: "var(--positive)", display: "flex", alignItems: "center", gap: "2px" }}>
                                {buyPct}% B {buyPct >= 50 ? "▲" : "▼"}
                              </span>
                              <span style={{ color: "var(--negative)" }}>{sellPct}% S</span>
                            </div>
                            <div style={{ width: "100%", height: "4px", backgroundColor: "var(--negative)", borderRadius: "2px", overflow: "hidden", display: "flex" }}>
                              <div style={{ width: `${buyPct}%`, height: "100%", backgroundColor: "var(--positive)" }} />
                            </div>
                          </div>
                        )}
                      </td>

                      {/* 8. BUY/SELL QTY */}
                      <td
                        style={{ textAlign: "center", padding: "8px 10px", cursor: "pointer" }}
                        onMouseEnter={(e) => handleSentimentMouseEnter(e, item, buyPct ?? 50, buyQty, sellQty, changePct ?? 0)}
                        onMouseLeave={handleSentimentMouseLeave}
                      >
                        {totalQty === 0 ? (
                          <span style={{ color: "var(--text-faint)", fontSize: "10px" }}>—</span>
                        ) : (
                          <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "2px", width: "95px", margin: "0 auto" }}>
                            <div style={{ display: "flex", justifyContent: "space-between", width: "100%", fontSize: "9px", fontWeight: "700" }}>
                              <span style={{ color: "var(--positive)" }}>{fmtQty(buyQty)}</span>
                              <span style={{ color: "var(--negative)" }}>{fmtQty(sellQty)}</span>
                            </div>
                            <div style={{ width: "100%", height: "4px", backgroundColor: "var(--negative)", borderRadius: "2px", overflow: "hidden", display: "flex" }}>
                              <div style={{ width: `${(buyQty / totalQty) * 100}%`, height: "100%", backgroundColor: "var(--positive)" }} />
                            </div>
                            <div style={{ fontSize: "8px", color: "var(--text-faint)", fontWeight: "600" }}>
                              Σ {fmtQty(totalQty)}
                            </div>
                          </div>
                        )}
                      </td>

                      {/* 9. 2M SIGNAL */}
                      <td
                        style={{ textAlign: "center", padding: "8px 6px", cursor: "pointer" }}
                        onMouseEnter={(e) => handleSentimentMouseEnter(e, item, buyPct ?? 50, buyQty, sellQty, changePct ?? 0)}
                        onMouseLeave={handleSentimentMouseLeave}
                      >
                        <div
                          title={`${sig.explanation}\n\nReliability Warning:\nThis micro-trend is based on high-frequency order-book data. Useful for near-term momentum, but vulnerable to spoofing. Use as helper, not standalone decision.`}
                          style={{
                            display: "inline-flex",
                            flexDirection: "column",
                            alignItems: "center",
                            padding: "3px 8px",
                            borderRadius: "6px",
                            backgroundColor: sigBg,
                            border: `1px solid ${sigBorder}`,
                            color: sigColor,
                            fontSize: "10px",
                            fontWeight: "800",
                            lineHeight: "1.2"
                          }}>
                          <span>{sig.recommendation}</span>
                          <span style={{ fontSize: "7px", opacity: 0.8, fontWeight: "normal" }}>CONF: {sig.confidence}</span>
                        </div>
                      </td>

                      {/* 10. MEETING DATE */}
                      <td style={{ padding: "10px 12px", textAlign: "center" }}>
                        <span style={{ fontSize: "11px", fontWeight: "700", color: "var(--info)", background: "rgba(79, 184, 217, 0.12)", padding: "3px 8px", borderRadius: "6px" }}>
                          📅 {item.display_date || item.meeting_date}
                        </span>
                      </td>

                      {/* 11. ACTIONS */}
                      <td style={{ padding: "10px 12px", textAlign: "right" }}>
                        <div style={{ display: "flex", gap: "5px", justifyContent: "flex-end" }}>
                          <button
                            onClick={() => selectUpcomingStock(item, false)}
                            style={{
                              padding: "4px 8px",
                              fontSize: "10px",
                              fontWeight: "700",
                              borderRadius: "6px",
                              backgroundColor: "rgba(91, 157, 255, 0.15)",
                              color: "var(--accent)",
                              border: "1px solid rgba(91, 157, 255, 0.3)",
                              cursor: "pointer",
                              display: "flex",
                              alignItems: "center",
                              gap: "3px"
                            }}
                          >
                            <Plus size={11} /> Add Target
                          </button>
                          <button
                            onClick={() => selectUpcomingStock(item, true)}
                            style={{
                              padding: "4px 8px",
                              fontSize: "10px",
                              fontWeight: "700",
                              borderRadius: "6px",
                              backgroundColor: "var(--positive)",
                              color: "var(--on-accent)",
                              border: "none",
                              cursor: "pointer",
                              display: "flex",
                              alignItems: "center",
                              gap: "3px",
                              boxShadow: "0 2px 6px rgba(63, 191, 135, 0.3)"
                            }}
                          >
                            <Zap size={11} /> Add &amp; Arm
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {/* Floating Sentiment Trend Analyzer Popover */}
        {hoveredSentiment && (() => {
          const range = hoveredSentiment.dayHigh - hoveredSentiment.dayLow;
          const priceBuyPct = range > 0 ? Math.round(((hoveredSentiment.ltp - hoveredSentiment.dayLow) / range) * 100) : 50;
          const depthBuyPct = Math.round(hoveredSentiment.buyPct);
          const compositeBuyPct = Math.round((priceBuyPct * 0.15) + (depthBuyPct * 0.85));
          const compositeSellPct = 100 - compositeBuyPct;

          const history = sentimentHistoryRef.current[hoveredSentiment.symbol] || [compositeBuyPct];

          const drawSparkline = (hist: number[]) => {
            const displayHist = hist.length === 1 ? [hist[0], hist[0]] : hist;
            const width = 328;
            const height = 40;
            const padding = 5;
            const maxX = displayHist.length - 1;
            const maxY = 100;

            const points = displayHist.map((val, index) => {
              const x = padding + (index / (maxX || 1)) * (width - 2 * padding);
              const y = height - (padding + (val / maxY) * (height - 2 * padding));
              return `${x},${y}`;
            }).join(" ");

            const isUp = displayHist[displayHist.length - 1] >= displayHist[0];
            const strokeColor = isUp ? "var(--positive)" : "var(--negative)";

            return (
              <svg width={width} height={height} style={{ overflow: "visible" }}>
                <line x1={0} y1={height / 2} x2={width} y2={height / 2} stroke="rgba(255,255,255,0.06)" strokeDasharray="3 3" />
                <polyline fill="none" stroke={strokeColor} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" points={points} />
              </svg>
            );
          };

          return (
            <div style={{
              position: "fixed",
              left: `${hoveredSentiment.x}px`,
              top: `${hoveredSentiment.y}px`,
              width: "360px",
              zIndex: 99999,
              background: "linear-gradient(135deg, rgba(28, 34, 45, 0.98) 0%, rgba(15, 19, 25, 0.99) 100%)",
              border: "1px solid rgba(255, 255, 255, 0.15)",
              borderRadius: "12px",
              boxShadow: "0 20px 50px rgba(0, 0, 0, 0.9), 0 0 30px rgba(79, 184, 217, 0.15)",
              padding: "16px",
              pointerEvents: "none",
              backdropFilter: "blur(20px)"
            }}>
              {/* Popover Header */}
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", borderBottom: "1px solid rgba(255, 255, 255, 0.08)", paddingBottom: "10px", marginBottom: "12px" }}>
                <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                  <span style={{ padding: "3px 8px", borderRadius: "6px", background: "var(--info)", color: "var(--bg-base)", fontWeight: "800", fontSize: "12px" }}>
                    {hoveredSentiment.symbol}
                  </span>
                  <span style={{ fontSize: "13px", fontWeight: "700", color: "var(--text-primary)" }}>
                    Sentiment &amp; Trend Analyzer
                  </span>
                </div>
              </div>

              {/* Big Buyers vs Sellers percentage */}
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: "6px" }}>
                <span style={{ fontSize: "20px", fontWeight: "900", color: "var(--positive)" }}>
                  {compositeBuyPct}% <span style={{ fontSize: "11px", fontWeight: "700", opacity: 0.8 }}>BUYERS</span>
                </span>
                <span style={{ fontSize: "20px", fontWeight: "900", color: "var(--negative)" }}>
                  {compositeSellPct}% <span style={{ fontSize: "11px", fontWeight: "700", opacity: 0.8 }}>SELLERS</span>
                </span>
              </div>

              {/* Visual sentiment bar */}
              <div style={{ width: "100%", height: "6px", backgroundColor: "var(--negative)", borderRadius: "3px", overflow: "hidden", display: "flex", marginBottom: "14px" }}>
                <div style={{ width: `${compositeBuyPct}%`, height: "100%", backgroundColor: "var(--positive)", transition: "width 0.3s ease" }} />
              </div>

              {/* Metrics Grid: Daily Range vs Order Book */}
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "10px", marginBottom: "14px" }}>
                <div style={{ backgroundColor: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.06)", borderRadius: "8px", padding: "8px 10px" }}>
                  <div style={{ fontSize: "9px", color: "var(--text-muted)", fontWeight: "700", marginBottom: "2px" }}>DAILY RANGE (15% WT)</div>
                  <div style={{ fontSize: "12px", fontWeight: "800", color: "var(--text-primary)" }}>{priceBuyPct}% B / {100 - priceBuyPct}% S</div>
                </div>
                <div style={{ backgroundColor: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.06)", borderRadius: "8px", padding: "8px 10px" }}>
                  <div style={{ fontSize: "9px", color: "var(--text-muted)", fontWeight: "700", marginBottom: "2px" }}>ORDER BOOK (85% WT)</div>
                  <div style={{ fontSize: "12px", fontWeight: "800", color: "var(--text-primary)" }}>{depthBuyPct}% B / {100 - depthBuyPct}% S</div>
                </div>
              </div>

              {/* Buyer Sentiment Sparkline Chart */}
              <div style={{ borderTop: "1px solid rgba(255,255,255,0.08)", paddingTop: "10px" }}>
                <div style={{ fontSize: "9px", color: "var(--text-faint)", fontWeight: "700", letterSpacing: "0.5px", marginBottom: "6px" }}>
                  BUYER SENTIMENT SPARKLINE (LAST 15 UPDATES)
                </div>
                {drawSparkline(history)}
              </div>
            </div>
          );
        })()}
      </div>

      {/* AI SETTINGS MODAL */}
      {showSettingsModal && (
        <div style={{
          position: "fixed",
          top: 0, left: 0, right: 0, bottom: 0,
          backgroundColor: "rgba(0, 0, 0, 0.75)",
          backdropFilter: "blur(4px)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          zIndex: 1000,
          padding: "20px"
        }}>
          <div style={{
            backgroundColor: "var(--bg-base)",
            border: "1px solid rgba(164, 138, 224, 0.3)",
            borderRadius: "16px",
            width: "100%",
            maxWidth: "540px",
            padding: "24px",
            boxShadow: "0 20px 25px -5px rgba(0, 0, 0, 0.5)"
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "18px" }}>
              <h3 style={{ fontSize: "16px", fontWeight: "800", color: "var(--text-primary)", display: "flex", alignItems: "center", gap: "8px" }}>
                <Settings size={18} className="text-purple-400" /> 2-Step AI Earnings Pipeline Settings
              </h3>
              <button
                onClick={() => setShowSettingsModal(false)}
                style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", fontSize: "18px" }}
              >
                ✕
              </button>
            </div>

            <form onSubmit={handleSaveSettings} style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
              <div>
                <label style={{ display: "block", fontSize: "12px", fontWeight: "700", color: "var(--text-secondary)", marginBottom: "6px" }}>
                  Flow 1: Custom REST API Endpoint URL (POST /api/generate)
                </label>
                <input
                  type="text"
                  value={aiSettings.custom_api_url}
                  onChange={(e) => setAiSettings({ ...aiSettings, custom_api_url: e.target.value })}
                  placeholder="http://localhost:11434/api/generate"
                  style={{
                    width: "100%",
                    padding: "10px 12px",
                    borderRadius: "8px",
                    backgroundColor: "var(--surface-2)",
                    border: "1px solid rgba(255, 255, 255, 0.1)",
                    color: "var(--text-primary)",
                    fontSize: "12px",
                    fontFamily: "monospace"
                  }}
                />
                <span style={{ fontSize: "11px", color: "var(--text-faint)", marginTop: "4px", display: "block" }}>
                  Primary REST endpoint called dynamically when an announcement is detected.
                </span>
              </div>

              <div>
                <label style={{ display: "block", fontSize: "12px", fontWeight: "700", color: "var(--text-secondary)", marginBottom: "6px" }}>
                  Flow 2 (Fallback): Premium OpenRouter API Key
                </label>
                <input
                  type="password"
                  value={aiSettings.premium_openrouter_api_key}
                  onChange={(e) => setAiSettings({ ...aiSettings, premium_openrouter_api_key: e.target.value })}
                  placeholder="sk-or-v1-..."
                  style={{
                    width: "100%",
                    padding: "10px 12px",
                    borderRadius: "8px",
                    backgroundColor: "var(--surface-2)",
                    border: "1px solid rgba(255, 255, 255, 0.1)",
                    color: "var(--text-primary)",
                    fontSize: "12px",
                    fontFamily: "monospace"
                  }}
                />
                <span style={{ fontSize: "11px", color: "var(--text-faint)", marginTop: "4px", display: "block" }}>
                  Dedicated key used if Custom REST API is unreachable or returns an error.
                </span>
              </div>

              <div>
                <label style={{ display: "block", fontSize: "12px", fontWeight: "700", color: "var(--text-secondary)", marginBottom: "6px" }}>
                  Flow 2 Model Selector (Premium Models)
                </label>
                <select
                  value={aiSettings.premium_openrouter_model}
                  onChange={(e) => setAiSettings({ ...aiSettings, premium_openrouter_model: e.target.value })}
                  style={{
                    width: "100%",
                    padding: "10px 12px",
                    borderRadius: "8px",
                    backgroundColor: "var(--surface-2)",
                    border: "1px solid rgba(255, 255, 255, 0.1)",
                    color: "var(--text-primary)",
                    fontSize: "12px"
                  }}
                >
                  <option value="anthropic/claude-3.5-sonnet">Claude 3.5 Sonnet (Anthropic)</option>
                  <option value="deepseek/deepseek-r1">DeepSeek R1 (Reasoning Model)</option>
                  <option value="openai/gpt-4o">GPT-4o (OpenAI)</option>
                  <option value="google/gemini-2.5-flash">Gemini 2.5 Flash (Google)</option>
                  <option value="meta-llama/llama-3.3-70b-instruct">Llama 3.3 70B (Meta)</option>
                </select>
              </div>

              <div style={{ display: "flex", justifyContent: "flex-end", gap: "10px", marginTop: "10px" }}>
                <button
                  type="button"
                  onClick={() => setShowSettingsModal(false)}
                  style={{
                    padding: "8px 16px",
                    borderRadius: "8px",
                    backgroundColor: "var(--surface-3)",
                    color: "var(--text-primary)",
                    border: "none",
                    cursor: "pointer",
                    fontSize: "12px"
                  }}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={savingSettings}
                  style={{
                    padding: "8px 20px",
                    borderRadius: "8px",
                    backgroundColor: "var(--ai)",
                    color: "var(--on-accent)",
                    border: "none",
                    cursor: "pointer",
                    fontSize: "12px",
                    fontWeight: "700",
                    boxShadow: "0 2px 8px rgba(147, 51, 234, 0.4)"
                  }}
                >
                  {savingSettings ? "Saving..." : "Save AI Settings"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* TOOLBAR */}
      <div
        id="auto-trading-target-configs"
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: "16px",
          background: "rgba(22, 27, 36, 0.4)",
          padding: "12px 18px",
          borderRadius: "12px",
          border: "1px solid rgba(255, 255, 255, 0.05)"
        }}
      >
        <h2 id="auto-trading-configs" style={{ fontSize: "16px", fontWeight: "700", color: "var(--text-primary)", display: "flex", alignItems: "center", gap: "8px", scrollMarginTop: "16px" }}>
          ⚡ Auto-Trading Target Stock Configurations
        </h2>
        <div style={{ display: "flex", gap: "10px", alignItems: "center" }}>
          <button
            onClick={() => setShowSettingsModal(true)}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "6px",
              padding: "8px 14px",
              fontSize: "12px",
              fontWeight: "700",
              borderRadius: "8px",
              backgroundColor: "rgba(164, 138, 224, 0.15)",
              color: "var(--ai)",
              border: "1px solid rgba(164, 138, 224, 0.3)",
              cursor: "pointer"
            }}
          >
            <Settings size={15} />
            ⚙️ AI Pipeline Settings
          </button>
          <button
            onClick={handlePollNow}
            disabled={manualPolling}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "6px",
              padding: "8px 16px",
              fontSize: "12px",
              fontWeight: "700",
              borderRadius: "8px",
              backgroundColor: "rgba(91, 157, 255, 0.2)",
              color: "var(--accent)",
              border: "1px solid rgba(91, 157, 255, 0.3)",
              cursor: "pointer",
              transition: "all 0.2s"
            }}
          >
            <Zap size={15} className={manualPolling ? "animate-spin" : ""} />
            {manualPolling ? "Polling NSE..." : "⚡ Poll Now (On-Demand)"}
          </button>
          <button
            onClick={() => setShowAddForm(!showAddForm)}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "6px",
              padding: "8px 16px",
              fontSize: "12px",
              fontWeight: "700",
              borderRadius: "8px",
              backgroundColor: showAddForm ? "var(--text-faint)" : "var(--accent)",
              color: "var(--on-accent)",
              border: "none",
              cursor: "pointer",
              boxShadow: "0 2px 8px rgba(91, 157, 255,0.3)"
            }}
          >
            <Plus size={15} />
            {showAddForm ? "Cancel" : "Add Target Stock"}
          </button>
          <button
            onClick={() => setShowBuyForm(!showBuyForm)}
            title="Buy a stock outright — no target, no arming, no filing to wait for"
            style={{
              display: "flex", alignItems: "center", gap: "6px",
              padding: "8px 16px", fontSize: "12px", fontWeight: 700, borderRadius: "8px",
              backgroundColor: showBuyForm ? "var(--text-faint)" : "var(--positive)",
              color: "var(--on-accent)", border: "none", cursor: "pointer",
              boxShadow: "0 2px 8px rgba(63, 191, 135,0.3)",
            }}
          >
            <ShoppingBag size={15} />
            {showBuyForm ? "Cancel" : "Buy Stock"}
          </button>
          <button
            onClick={fetchData}
            style={{
              padding: "8px 12px",
              borderRadius: "8px",
              backgroundColor: "rgba(255, 255, 255, 0.05)",
              color: "var(--text-muted)",
              border: "1px solid rgba(255, 255, 255, 0.08)",
              cursor: "pointer"
            }}
          >
            <RefreshCw size={14} />
          </button>
        </div>
      </div>


      {/* ADD TARGET STOCK FORM */}
      {showBuyForm && (
        <form onSubmit={handleDirectBuy} style={{
          background: "var(--surface-1)", border: "1px solid rgba(63, 191, 135,0.3)",
          borderRadius: "12px", padding: "16px", marginBottom: "16px",
          display: "flex", flexWrap: "wrap", alignItems: "flex-end", gap: "12px",
        }}>
          <label style={{ fontSize: "10px", color: "var(--text-muted)", fontWeight: 700 }}>
            SYMBOL
            <input
              autoFocus
              value={buyForm.symbol}
              onChange={e => setBuyForm(p => ({ ...p, symbol: e.target.value.toUpperCase() }))}
              placeholder="e.g. RELIANCE"
              style={{ display: "block", width: "160px", marginTop: "4px", padding: "8px 10px", background: "var(--bg-sunken)", border: "1px solid var(--border-default)", borderRadius: "6px", color: "var(--text-primary)", fontSize: "13px", fontWeight: 700 }}
            />
          </label>
          <label style={{ fontSize: "10px", color: "var(--text-muted)", fontWeight: 700 }}>
            QTY
            <input type="number" min={1} value={buyForm.quantity}
              onChange={e => setBuyForm(p => ({ ...p, quantity: parseInt(e.target.value) || 1 }))}
              style={{ display: "block", width: "80px", marginTop: "4px", padding: "8px 10px", background: "var(--bg-sunken)", border: "1px solid var(--border-default)", borderRadius: "6px", color: "var(--text-primary)", fontSize: "13px" }} />
          </label>
          <label style={{ fontSize: "10px", color: "var(--text-muted)", fontWeight: 700 }}>
            TYPE
            <select value={buyForm.order_type}
              onChange={e => setBuyForm(p => ({ ...p, order_type: e.target.value }))}
              style={{ display: "block", marginTop: "4px", padding: "8px 10px", background: "var(--bg-sunken)", border: "1px solid var(--border-default)", borderRadius: "6px", color: "var(--text-primary)", fontSize: "13px" }}>
              <option value="MARKET">MARKET</option>
              <option value="LIMIT">LIMIT</option>
            </select>
          </label>
          {buyForm.order_type === "LIMIT" && (
            <label style={{ fontSize: "10px", color: "var(--text-muted)", fontWeight: 700 }}>
              LIMIT ₹
              <input type="number" step="0.05" value={buyForm.limit_price}
                onChange={e => setBuyForm(p => ({ ...p, limit_price: e.target.value }))}
                style={{ display: "block", width: "110px", marginTop: "4px", padding: "8px 10px", background: "var(--bg-sunken)", border: "1px solid var(--border-default)", borderRadius: "6px", color: "var(--text-primary)", fontSize: "13px" }} />
            </label>
          )}
          <label style={{ fontSize: "10px", color: "var(--text-muted)", fontWeight: 700 }}>
            SL %
            <input type="number" step="0.5" min={0.5} value={buyForm.stoploss_pct}
              onChange={e => setBuyForm(p => ({ ...p, stoploss_pct: parseFloat(e.target.value) || 2 }))}
              style={{ display: "block", width: "80px", marginTop: "4px", padding: "8px 10px", background: "var(--bg-sunken)", border: "1px solid var(--border-default)", borderRadius: "6px", color: "var(--text-primary)", fontSize: "13px" }} />
          </label>
          <label style={{ fontSize: "10px", color: "var(--text-muted)", fontWeight: 700 }}>
            BROKER
            <select value={buyForm.broker}
              onChange={e => setBuyForm(p => ({ ...p, broker: e.target.value }))}
              style={{ display: "block", marginTop: "4px", padding: "8px 10px", background: "var(--bg-sunken)", border: "1px solid var(--border-default)", borderRadius: "6px", color: "var(--text-primary)", fontSize: "13px" }}>
              <option value="upstox">Upstox</option>
              <option value="zerodha">Zerodha</option>
            </select>
          </label>

          <button type="submit" disabled={buyBusy || !buyForm.symbol.trim()}
            style={{
              padding: "9px 18px", borderRadius: "8px", border: "none",
              background: buyBusy ? "rgba(63, 191, 135,0.4)" : "var(--positive)",
              color: "var(--on-accent)", fontSize: "12px", fontWeight: 700,
              cursor: buyBusy ? "not-allowed" : "pointer", display: "flex", alignItems: "center", gap: "6px",
            }}>
            <ShoppingBag size={14} />
            {buyBusy ? "Placing…" : canTradeNow() ? "Buy Now" : "Queue for 09:15"}
          </button>

          {/* Says which of the two will happen before the click, not after. */}
          <span style={{ fontSize: "11px", color: canTradeNow() ? "var(--positive)" : "var(--warning)" }}>
            {canTradeNow()
              ? "Market is open — this order goes to the broker immediately."
              : "Market is closed — this will be queued for the next trading day at 09:15 IST."}
          </span>
        </form>
      )}

      {showAddForm && (
        <form onSubmit={handleAddConfig} style={{
          background: "rgba(22, 27, 36, 0.8)",
          border: "1px solid rgba(91, 157, 255, 0.3)",
          borderRadius: "12px",
          padding: "20px",
          marginBottom: "20px",
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
          gap: "14px"
        }}>
          {/* Symbol Search */}
          <div style={{ position: "relative" }}>
            <label style={{ fontSize: "11px", color: "var(--text-muted)", fontWeight: "600", display: "block", marginBottom: "4px" }}>Stock Symbol *</label>
            <input
              type="text"
              placeholder="e.g. RELIANCE, TCS"
              value={formData.symbol}
              onChange={(e) => handleSymbolSearch(e.target.value)}
              required
              style={{
                width: "100%",
                padding: "8px 12px",
                fontSize: "12px",
                borderRadius: "6px",
                border: "1px solid rgba(255, 255, 255, 0.1)",
                backgroundColor: "var(--surface-1)",
                color: "var(--text-primary)"
              }}
            />
            {searchResults.length > 0 && (
              <div style={{
                position: "absolute",
                top: "100%",
                left: 0,
                right: 0,
                backgroundColor: "var(--bg-base)",
                border: "1px solid rgba(255, 255, 255, 0.1)",
                borderRadius: "6px",
                maxHeight: "150px",
                overflowY: "auto",
                zIndex: 100,
                marginTop: "4px"
              }}>
                {searchResults.map((item, i) => (
                  <div
                    key={i}
                    onClick={() => selectSymbol(item)}
                    style={{
                      padding: "8px 12px",
                      fontSize: "12px",
                      cursor: "pointer",
                      borderBottom: "1px solid rgba(255, 255, 255, 0.05)",
                      color: "var(--text-primary)"
                    }}
                    onMouseOver={(e) => e.currentTarget.style.backgroundColor = "rgba(91, 157, 255, 0.2)"}
                    onMouseOut={(e) => e.currentTarget.style.backgroundColor = "transparent"}
                  >
                    <strong>{item.symbol}</strong> — {item.name}
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Purchase Date */}
          <div>
            <label style={{ fontSize: "11px", color: "var(--text-muted)", fontWeight: "600", display: "block", marginBottom: "4px" }}>Purchase Date *</label>
            <input
              type="date"
              value={formData.purchase_date}
              onChange={(e) => setFormData(prev => ({ ...prev, purchase_date: e.target.value }))}
              required
              style={{
                width: "100%",
                padding: "8px 12px",
                fontSize: "12px",
                borderRadius: "6px",
                border: "1px solid rgba(255, 255, 255, 0.1)",
                backgroundColor: "var(--surface-1)",
                color: "var(--text-primary)"
              }}
            />
          </div>

          {/* Quantity */}
          <div>
            <label style={{ fontSize: "11px", color: "var(--text-muted)", fontWeight: "600", display: "block", marginBottom: "4px" }}>Quantity *</label>
            <input
              type="number"
              min="1"
              value={formData.quantity}
              onChange={(e) => setFormData(prev => ({ ...prev, quantity: parseInt(e.target.value) || 1 }))}
              required
              style={{
                width: "100%",
                padding: "8px 12px",
                fontSize: "12px",
                borderRadius: "6px",
                border: "1px solid rgba(255, 255, 255, 0.1)",
                backgroundColor: "var(--surface-1)",
                color: "var(--text-primary)"
              }}
            />
          </div>

          {/* Stoploss % */}
          <div>
            <label style={{ fontSize: "11px", color: "var(--text-muted)", fontWeight: "600", display: "block", marginBottom: "4px" }}>Stoploss % *</label>
            <input
              type="number"
              step="0.5"
              min="0.5"
              max="20"
              value={formData.stoploss_pct}
              onChange={(e) => setFormData(prev => ({ ...prev, stoploss_pct: parseFloat(e.target.value) || 2.0 }))}
              required
              style={{
                width: "100%",
                padding: "8px 12px",
                fontSize: "12px",
                borderRadius: "6px",
                border: "1px solid rgba(255, 255, 255, 0.1)",
                backgroundColor: "var(--surface-1)",
                color: "var(--text-primary)"
              }}
            />
          </div>

          {/* Broker Choice */}
          <div>
            <label style={{ fontSize: "11px", color: "var(--text-muted)", fontWeight: "600", display: "block", marginBottom: "4px" }}>Broker Account *</label>
            <select
              value={formData.broker}
              onChange={(e) => setFormData(prev => ({ ...prev, broker: e.target.value }))}
              style={{
                width: "100%",
                padding: "8px 12px",
                fontSize: "12px",
                borderRadius: "6px",
                border: "1px solid rgba(255, 255, 255, 0.1)",
                backgroundColor: "var(--surface-1)",
                color: "var(--text-primary)"
              }}
            >
              <option value="upstox">Upstox (Active OAuth)</option>
              <option value="zerodha">Zerodha / Kite</option>
            </select>
          </div>

          {/* Order Type */}
          <div>
            <label style={{ fontSize: "11px", color: "var(--text-muted)", fontWeight: "600", display: "block", marginBottom: "4px" }}>Order Type *</label>
            <select
              value={formData.order_type}
              onChange={(e) => setFormData(prev => ({ ...prev, order_type: e.target.value }))}
              style={{
                width: "100%",
                padding: "8px 12px",
                fontSize: "12px",
                borderRadius: "6px",
                border: "1px solid rgba(255, 255, 255, 0.1)",
                backgroundColor: "var(--surface-1)",
                color: "var(--text-primary)"
              }}
            >
              <option value="MARKET">Market Order (Instant Fill)</option>
              <option value="LIMIT">Limit Order</option>
            </select>
          </div>

          {/* Limit Price (if order_type === LIMIT) */}
          {formData.order_type === "LIMIT" && (
            <div>
              <label style={{ fontSize: "11px", color: "var(--text-muted)", fontWeight: "600", display: "block", marginBottom: "4px" }}>Limit Price (₹)</label>
              <input
                type="number"
                step="0.05"
                placeholder="Max price"
                value={formData.limit_price}
                onChange={(e) => setFormData(prev => ({ ...prev, limit_price: e.target.value }))}
                style={{
                  width: "100%",
                  padding: "8px 12px",
                  fontSize: "12px",
                  borderRadius: "6px",
                  border: "1px solid rgba(255, 255, 255, 0.1)",
                  backgroundColor: "var(--surface-1)",
                  color: "var(--text-primary)"
                }}
              />
            </div>
          )}

          {/* Premium AI Provider */}
          <div>
            <label style={{ fontSize: "11px", color: "var(--text-muted)", fontWeight: "600", display: "block", marginBottom: "4px" }}>Premium AI Engine *</label>
            <select
              value={formData.ai_provider}
              onChange={(e) => setFormData(prev => ({ ...prev, ai_provider: e.target.value }))}
              style={{
                width: "100%",
                padding: "8px 12px",
                fontSize: "12px",
                borderRadius: "6px",
                border: "1px solid rgba(255, 255, 255, 0.1)",
                backgroundColor: "var(--surface-1)",
                color: "var(--text-primary)"
              }}
            >
              <option value="groq">Groq (Llama 3.3 70B - Fastest)</option>
              <option value="gemini">Gemini 2.5 Flash</option>
              <option value="openrouter">OpenRouter Free Pool</option>
              <option value="openai">OpenAI GPT-4o</option>
              <option value="anthropic">Anthropic Claude 3.5</option>
            </select>
          </div>

          {/* Submit Button with Auto-Arm Toggle */}
          <div style={{ gridColumn: "1 / -1", display: "flex", justifyContent: "flex-end", alignItems: "center", marginTop: "10px", gap: "16px" }}>
            <label style={{ display: "flex", alignItems: "center", gap: "8px", cursor: "pointer", fontSize: "12px", color: "var(--text-muted)" }}>
              <input
                type="checkbox"
                checked={autoArmOnSave}
                onChange={(e) => setAutoArmOnSave(e.target.checked)}
                style={{ width: "16px", height: "16px", accentColor: "var(--positive)", cursor: "pointer" }}
              />
              <span style={{ fontWeight: "600", color: autoArmOnSave ? "var(--positive-strong)" : "var(--text-muted)" }}>
                <Zap size={12} style={{ display: "inline", verticalAlign: "middle", marginRight: "4px" }} />
                Auto-Arm on Save
              </span>
            </label>
            <button
              type="submit"
              style={{
                padding: "10px 24px",
                fontSize: "13px",
                fontWeight: "700",
                borderRadius: "8px",
                backgroundColor: "var(--positive)",
                color: "var(--on-accent)",
                border: "none",
                cursor: "pointer",
                boxShadow: "0 2px 10px rgba(63, 191, 135, 0.3)"
              }}
            >
              Save Target Config
            </button>
          </div>
        </form>
      )}

      {/* CONFIGS TABLE */}
      <div style={{
        background: "rgba(22, 27, 36, 0.6)",
        border: "1px solid rgba(255, 255, 255, 0.08)",
        borderRadius: "12px",
        overflow: "hidden",
        marginBottom: "30px"
      }}>
        <table style={{ width: "100%", borderCollapse: "collapse", textAlign: "left", fontSize: "12px" }}>
          <thead>
            <tr style={{ background: "rgba(15, 19, 25, 0.8)", borderBottom: "1px solid rgba(255, 255, 255, 0.08)", color: "var(--text-muted)" }}>
              <th style={{ padding: "12px 16px" }}>Stock Symbol</th>
              <th style={{ padding: "12px 16px" }}>Target Date</th>
              <th style={{ padding: "12px 16px" }}>Qty</th>
              <th style={{ padding: "12px 16px" }}>SL %</th>
              <th style={{ padding: "12px 16px" }}>Broker</th>
              <th style={{ padding: "12px 16px" }}>Order Type</th>
              <th style={{ padding: "12px 16px" }}>Premium AI</th>
              <th style={{ padding: "12px 16px" }}>Status</th>
              <th style={{ padding: "12px 16px", textAlign: "right" }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {configs.length === 0 ? (
              <tr>
                <td colSpan={9} style={{ padding: "30px", textAlign: "center", color: "var(--text-faint)" }}>
                  No auto-trade targets configured yet. Click <strong>Add Target Stock</strong> to configure one.
                </td>
              </tr>
            ) : (
              configs.map((c) => (
                <tr key={c.id} style={{ borderBottom: "1px solid rgba(255, 255, 255, 0.05)" }}>
                  <td style={{ padding: "14px 16px", fontWeight: "700", color: "var(--text-primary)" }}>
                    {c.symbol}
                    {c.buy_price && <div style={{ fontSize: "10px", color: "var(--positive-strong)" }}>Bought @ ₹{c.buy_price}</div>}
                  </td>
                  <td style={{ padding: "14px 16px", color: "var(--text-secondary)" }}>{c.purchase_date}</td>
                  <td style={{ padding: "14px 16px", color: "var(--text-secondary)" }}>{c.quantity}</td>
                  <td style={{ padding: "14px 16px", color: "var(--negative-strong)" }}>{c.stoploss_pct}%</td>
                  <td style={{ padding: "14px 16px", textTransform: "uppercase", fontWeight: "600", color: "var(--text-muted)" }}>{c.broker}</td>
                  <td style={{ padding: "14px 16px", color: "var(--text-secondary)" }}>{c.order_type}</td>
                  <td style={{ padding: "14px 16px", textTransform: "uppercase", color: "var(--ai)", fontWeight: "600" }}>{c.ai_provider}</td>
                  <td style={{ padding: "14px 16px" }}>{getStatusBadge(c.status)}</td>
                  <td style={{ padding: "14px 16px", textAlign: "right" }}>
                    <div style={{ display: "flex", gap: "6px", justifyContent: "flex-end" }}>
                      {c.status === "pending" || c.status === "disarmed" ? (
                        <button
                          onClick={() => handleArm(c.id)}
                          disabled={actionLoading[`arm-${c.id}`]}
                          style={{
                            padding: "5px 10px",
                            fontSize: "11px",
                            fontWeight: "700",
                            borderRadius: "6px",
                            backgroundColor: "var(--positive)",
                            color: "var(--on-accent)",
                            border: "none",
                            cursor: "pointer"
                          }}
                        >
                          ⚡ Arm
                        </button>
                      ) : c.status === "armed" ? (
                        <button
                          onClick={() => handleDisarm(c.id)}
                          disabled={actionLoading[`disarmed-${c.id}`]}
                          style={{
                            padding: "5px 10px",
                            fontSize: "11px",
                            fontWeight: "600",
                            borderRadius: "6px",
                            backgroundColor: "var(--text-faint)",
                            color: "var(--text-primary)",
                            border: "none",
                            cursor: "pointer"
                          }}
                        >
                          🛑 Disarm
                        </button>
                      ) : null}

                      {c.status !== "bought" && c.status !== "sold" && (
                        <button
                          onClick={() => handleManualBuy(c.id)}
                          disabled={actionLoading[`buy-${c.id}`]}
                          style={{
                            padding: "5px 10px",
                            fontSize: "11px",
                            fontWeight: "700",
                            borderRadius: "6px",
                            backgroundColor: "var(--accent)",
                            color: "var(--on-accent)",
                            border: "none",
                            cursor: "pointer"
                          }}
                        >
                          🛒 Buy Now
                        </button>
                      )}

                      {c.status === "bought" && (
                        <button
                          onClick={() => handleManualSell(c.id)}
                          disabled={actionLoading[`sell-${c.id}`]}
                          style={{
                            padding: "5px 10px",
                            fontSize: "11px",
                            fontWeight: "700",
                            borderRadius: "6px",
                            backgroundColor: "var(--negative)",
                            color: "var(--on-accent)",
                            border: "none",
                            cursor: "pointer"
                          }}
                        >
                          💰 Sell Now
                        </button>
                      )}

                      <button
                        onClick={() => handleDeleteConfig(c.id)}
                        style={{
                          padding: "5px 8px",
                          borderRadius: "6px",
                          backgroundColor: "rgba(240, 115, 111, 0.1)",
                          color: "var(--negative-strong)",
                          border: "none",
                          cursor: "pointer"
                        }}
                      >
                        <Trash2 size={13} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* EXECUTED ORDERS & AI LOGS GRID */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "20px" }}>
        {/* Executed Broker Orders */}
        <div style={{
          background: "rgba(22, 27, 36, 0.6)",
          border: "1px solid rgba(255, 255, 255, 0.08)",
          borderRadius: "12px",
          padding: "16px"
        }}>
          <h3 style={{ fontSize: "14px", fontWeight: "700", color: "var(--text-primary)", marginBottom: "14px", display: "flex", alignItems: "center", gap: "8px" }}>
            <ShoppingBag size={16} className="text-blue-400" /> Executed Broker Orders ({orders.length})
          </h3>
          <div style={{ maxHeight: "350px", overflowY: "auto" }}>
            {orders.length === 0 ? (
              <div style={{ color: "var(--text-faint)", fontSize: "12px", textAlign: "center", padding: "20px" }}>
                No broker orders executed yet.
              </div>
            ) : (
              orders.map((o) => {
                const matchingAiLog = aiLogs.find(l => l.symbol?.toUpperCase() === o.symbol?.toUpperCase());

                return (
                  <div
                    key={o.id}
                    onClick={() => {
                      const el = document.getElementById(`ai-card-${o.symbol.toUpperCase()}`);
                      if (el) {
                        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        el.style.border = "1px solid var(--ai)";
                        el.style.boxShadow = "0 0 20px rgba(164, 138, 224, 0.8)";
                        setTimeout(() => {
                          el.style.border = "1px solid rgba(255, 255, 255, 0.08)";
                          el.style.boxShadow = "";
                        }, 2500);
                      }
                    }}
                    style={{
                      padding: "10px 12px",
                      borderRadius: "8px",
                      backgroundColor: "var(--surface-1)",
                      border: "1px solid rgba(255, 255, 255, 0.08)",
                      marginBottom: "8px",
                      fontSize: "12px",
                      cursor: "pointer",
                      transition: "all 0.2s",
                      position: "relative" as const
                    }}
                    onMouseEnter={(e) => {
                      e.currentTarget.style.borderColor = "rgba(91, 157, 255, 0.5)";
                      const rect = e.currentTarget.getBoundingClientRect();
                      setHoveredOrder({ order: o, x: rect.right + 8, y: rect.top });
                    }}
                    onMouseLeave={(e) => {
                      e.currentTarget.style.borderColor = "rgba(255, 255, 255, 0.08)";
                      setHoveredOrder(null);
                    }}
                  >
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <span style={{ fontSize: "14px", fontWeight: "800", color: "var(--text-primary)" }}>#{o.symbol}</span>
                      <span style={{
                        fontSize: "10px",
                        fontWeight: "700",
                        padding: "2px 8px",
                        borderRadius: "4px",
                        backgroundColor: o.status === "filled" || o.status === "placed" ? "rgba(63, 191, 135, 0.15)" : "rgba(240, 115, 111, 0.15)",
                        color: o.status === "filled" || o.status === "placed" ? "var(--positive-strong)" : "var(--negative-strong)",
                        border: o.status === "filled" || o.status === "placed" ? "1px solid rgba(63, 191, 135, 0.3)" : "1px solid rgba(240, 115, 111, 0.3)"
                      }}>
                        {o.status.toUpperCase()}
                      </span>
                    </div>

                    <div style={{ fontSize: "11px", color: "var(--text-muted)", marginTop: "4px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <span>{o.side} {o.quantity}x @ {o.price ? `₹${o.price}` : "Market"} ({o.broker.toUpperCase()})</span>
                      <span style={{ color: "var(--text-faint)", fontSize: "10px" }}>
                        {o.created_at ? new Date(o.created_at).toLocaleTimeString() : ""}
                      </span>
                    </div>

                    <div style={{ fontSize: "10px", color: "var(--accent)", marginTop: "4px", display: "flex", alignItems: "center", gap: "4px", fontWeight: "600" }}>
                      <ChevronRight size={12} /> Hover for execution log | Click to view AI analysis
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>

        {/* Executed Order Hover Popover */}
        {hoveredOrder && (() => {
          const o = hoveredOrder.order;
          const matchingAiLog = aiLogs.find(l => l.symbol?.toUpperCase() === o.symbol?.toUpperCase());
          let brokerResp: any = null;
          try { if (o.broker_response) brokerResp = JSON.parse(o.broker_response); } catch {}

          let popX = hoveredOrder.x;
          let popY = hoveredOrder.y;
          const popWidth = 380;
          const popHeight = 320;
          if (popX + popWidth > window.innerWidth) popX = hoveredOrder.x - popWidth - 16;
          if (popX < 8) popX = 8;
          if (popY + popHeight > window.innerHeight) popY = window.innerHeight - popHeight - 8;
          if (popY < 8) popY = 8;

          return (
            <div style={{
              position: "fixed",
              left: `${popX}px`,
              top: `${popY}px`,
              width: `${popWidth}px`,
              zIndex: 99999,
              background: "linear-gradient(135deg, rgba(28, 34, 45, 0.98) 0%, rgba(15, 19, 25, 0.99) 100%)",
              border: "1px solid rgba(91, 157, 255, 0.3)",
              borderRadius: "12px",
              boxShadow: "0 20px 50px rgba(0, 0, 0, 0.9), 0 0 30px rgba(91, 157, 255, 0.15)",
              padding: "16px",
              pointerEvents: "none" as const,
              backdropFilter: "blur(20px)",
              maxHeight: `${popHeight}px`,
              overflowY: "auto" as const
            }}>
              {/* Header */}
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", borderBottom: "1px solid rgba(255,255,255,0.08)", paddingBottom: "10px", marginBottom: "10px" }}>
                <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                  <span style={{ padding: "3px 8px", borderRadius: "6px", background: o.status === "filled" || o.status === "placed" ? "var(--positive)" : "var(--negative)", color: "var(--on-accent)", fontWeight: "800", fontSize: "11px" }}>
                    {o.symbol}
                  </span>
                  <span style={{ fontSize: "12px", fontWeight: "700", color: "var(--text-primary)" }}>
                    Execution Log
                  </span>
                </div>
                <span style={{ fontSize: "10px", color: "var(--text-faint)" }}>
                  {o.created_at ? new Date(o.created_at).toLocaleString() : ""}
                </span>
              </div>

              {/* Order Details */}
              <div style={{ fontSize: "11px", color: "var(--text-muted)", marginBottom: "8px" }}>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px 12px" }}>
                  <span>Side: <b style={{ color: o.side === "BUY" ? "var(--positive-strong)" : "var(--negative-strong)" }}>{o.side}</b></span>
                  <span>Qty: <b style={{ color: "var(--text-primary)" }}>{o.quantity}</b></span>
                  <span>Price: <b style={{ color: "var(--text-primary)" }}>{o.price ? `₹${o.price}` : "Market"}</b></span>
                  <span>Type: <b style={{ color: "var(--text-primary)" }}>{o.order_type}</b></span>
                  <span>Broker: <b style={{ color: "var(--info)" }}>{o.broker.toUpperCase()}</b></span>
                  <span>Status: <b style={{ color: o.status === "filled" || o.status === "placed" ? "var(--positive-strong)" : "var(--negative-strong)" }}>{o.status.toUpperCase()}</b></span>
                </div>
                {o.broker_order_id && (
                  <div style={{ marginTop: "4px", fontSize: "10px", color: "var(--text-faint)" }}>
                    Order ID: {o.broker_order_id}
                  </div>
                )}
              </div>

              {/* Error Message */}
              {o.error_message && (
                <div style={{ background: "rgba(240, 115, 111, 0.1)", border: "1px solid rgba(240, 115, 111, 0.3)", borderRadius: "6px", padding: "8px", marginBottom: "8px", fontSize: "10px", color: "var(--negative-strong)", fontWeight: "600" }}>
                  ⚠️ {o.error_message}
                </div>
              )}

              {/* AI Analysis Summary (if available) */}
              {matchingAiLog && (
                <div style={{ borderTop: "1px solid rgba(255,255,255,0.08)", paddingTop: "8px" }}>
                  <div style={{ fontSize: "10px", fontWeight: "800", color: "var(--ai)", marginBottom: "6px", letterSpacing: "0.5px" }}>
                    🤖 AI ANALYSIS
                  </div>
                  {matchingAiLog.ai_sentiment && (
                    <div style={{ fontSize: "11px", marginBottom: "4px" }}>
                      Sentiment: <b style={{ color: matchingAiLog.ai_sentiment === "positive" ? "var(--positive-strong)" : matchingAiLog.ai_sentiment === "negative" ? "var(--negative-strong)" : "var(--warning)" }}>
                        {matchingAiLog.ai_sentiment.toUpperCase()}
                      </b>
                      {matchingAiLog.ai_impact_score !== null && matchingAiLog.ai_impact_score !== undefined && (
                        <span style={{ marginLeft: "8px", color: "var(--text-muted)" }}>Impact: <b style={{ color: "var(--text-primary)" }}>{matchingAiLog.ai_impact_score}/10</b></span>
                      )}
                    </div>
                  )}
                  {matchingAiLog.ai_suggestion && (
                    <div style={{ fontSize: "10px", color: "var(--text-secondary)", marginBottom: "4px" }}>
                      💡 {matchingAiLog.ai_suggestion}
                    </div>
                  )}
                  {matchingAiLog.ai_summary && (
                    <div style={{ fontSize: "10px", color: "var(--text-muted)", lineHeight: "1.4", maxHeight: "60px", overflow: "hidden" }}>
                      {matchingAiLog.ai_summary.substring(0, 200)}{matchingAiLog.ai_summary.length > 200 ? "..." : ""}
                    </div>
                  )}
                  {matchingAiLog.flow_used && (
                    <div style={{ fontSize: "9px", color: "var(--text-faint)", marginTop: "4px" }}>
                      Flow: {matchingAiLog.flow_used === "custom_rest_api" ? "🔵 Local LLM" : "🟣 Groq/OpenRouter"}
                    </div>
                  )}
                </div>
              )}

              {/* Broker Raw Response (if no AI log) */}
              {!matchingAiLog && brokerResp && (
                <div style={{ borderTop: "1px solid rgba(255,255,255,0.08)", paddingTop: "8px" }}>
                  <div style={{ fontSize: "10px", fontWeight: "800", color: "var(--info)", marginBottom: "4px" }}>BROKER RESPONSE</div>
                  <div style={{ fontSize: "10px", color: "var(--text-muted)", lineHeight: "1.3", maxHeight: "60px", overflow: "hidden" }}>
                    {JSON.stringify(brokerResp, null, 1).substring(0, 200)}
                  </div>
                </div>
              )}
            </div>
          );
        })()}

        {/* Right Column: Live Announcements & Premium AI Logs */}
        <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
          
          {/* Real-Time NSE Corporate Announcements */}
          <div style={{
            background: "rgba(22, 27, 36, 0.6)",
            border: "1px solid rgba(91, 157, 255, 0.2)",
            borderRadius: "12px",
            padding: "16px"
          }}>
            <h3 style={{ fontSize: "14px", fontWeight: "700", color: "var(--text-primary)", marginBottom: "14px", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <span style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <Sparkles size={16} className="text-blue-400" /> Live Results & Corporate Announcements ({nseAnnouncements.length})
              </span>
              <span style={{ fontSize: "10px", color: "var(--accent)", background: "rgba(91, 157, 255,0.1)", padding: "2px 8px", borderRadius: "12px" }}>
                Real-Time AI
              </span>
            </h3>
            <div style={{ maxHeight: "300px", overflowY: "auto" }}>
              {nseAnnouncements.length === 0 ? (
                <div style={{ color: "var(--text-faint)", fontSize: "12px", textAlign: "center", padding: "20px" }}>
                  No corporate announcements received today.
                </div>
              ) : (
                nseAnnouncements.map((ann) => {
                  const isBeat = ann.ai_sentiment === "positive";
                  const isMiss = ann.ai_sentiment === "negative";
                  const isPendingArm = ann.ai_provider === "pending_arm";

                  return (
                    <div key={ann.id} style={{
                      padding: "12px",
                      borderRadius: "8px",
                      backgroundColor: "var(--surface-1)",
                      border: isPendingArm ? "1px dashed rgba(164, 138, 224, 0.4)" : isBeat ? "1px solid rgba(63, 191, 135, 0.25)" : isMiss ? "1px solid rgba(240, 115, 111, 0.25)" : "1px solid rgba(255,255,255,0.06)",
                      marginBottom: "10px",
                      fontSize: "12px"
                    }}>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "6px" }}>
                        <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                          <span 
                            onClick={() => openEarningsChart(ann.symbol, ann.instrument_key)}
                            style={{ fontSize: "13px", fontWeight: "800", color: "var(--accent)", cursor: "pointer", textDecoration: "underline" }}
                          >
                            #{ann.symbol}
                          </span>
                          {ann.source && (
                            <span style={{ 
                              fontSize: "9px", 
                              color: ann.source === "nse" ? "var(--accent)" : "var(--negative)", 
                              backgroundColor: ann.source === "nse" ? "rgba(91, 157, 255,0.15)" : "rgba(240, 115, 111,0.15)",
                              padding: "1px 5px", 
                              borderRadius: "3px",
                              fontWeight: "800",
                              textTransform: "uppercase"
                            }}>
                              {ann.source}
                            </span>
                          )}
                          {ann.time && (
                            <span style={{ fontSize: "10px", color: "var(--text-faint)", fontWeight: "600" }}>
                              🕒 {new Date(ann.time).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true })}
                            </span>
                          )}
                        </div>
                        {isPendingArm ? (
                          <span style={{ fontSize: "10px", fontWeight: "700", color: "var(--ai)", background: "rgba(164, 138, 224, 0.15)", padding: "2px 6px", borderRadius: "4px" }}>
                            ⏳ Awaiting Arm AI
                          </span>
                        ) : ann.ai_sentiment ? (
                          <span style={{
                            fontSize: "10px",
                            fontWeight: "800",
                            color: isBeat ? "var(--positive-strong)" : isMiss ? "var(--negative-strong)" : "var(--warning)",
                            textTransform: "uppercase"
                          }}>
                            {ann.ai_sentiment}
                          </span>
                        ) : null}
                      </div>
                      <div style={{ fontWeight: "600", color: "var(--text-primary)", marginBottom: "6px" }}>
                        {ann.title}
                      </div>
                      {ann.ai_summary && (
                        <div style={{ fontSize: "11px", color: "var(--text-muted)", fontStyle: "italic", borderTop: "1px dashed rgba(255,255,255,0.06)", paddingTop: "6px", marginTop: "6px" }}>
                          💡 {ann.ai_summary}
                        </div>
                      )}
                      {ann.url && ann.url.startsWith("http") && (
                        <div style={{ marginTop: "6px", textAlign: "right" }}>
                          <a href={ann.url} target="_blank" rel="noreferrer" style={{ fontSize: "10px", color: "var(--accent)", textDecoration: "none" }}>
                            View PDF Link <ExternalLink size={10} style={{ display: "inline" }} />
                          </a>
                        </div>
                      )}
                    </div>
                  );
                })
              )}
            </div>
          </div>

          {/* Premium 2-Step AI Earnings Analysis Logs */}
          <div style={{
            background: "rgba(22, 27, 36, 0.6)",
            border: "1px solid rgba(164, 138, 224, 0.2)",
            borderRadius: "12px",
            padding: "16px"
          }}>
            <h3 style={{ fontSize: "14px", fontWeight: "700", color: "var(--text-primary)", marginBottom: "14px", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <span style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <Cpu size={16} className="text-purple-400" /> 2-Step AI Earnings Analysis ({aiLogs.length})
              </span>
              <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <button
                  onClick={handleClearAiLogs}
                  style={{
                    fontSize: "11px",
                    color: "var(--negative-strong)",
                    background: "rgba(240, 115, 111, 0.12)",
                    border: "1px solid rgba(240, 115, 111, 0.25)",
                    padding: "3px 8px",
                    borderRadius: "6px",
                    cursor: "pointer",
                    fontWeight: "700"
                  }}
                >
                  🧹 Clear / Reset Logs
                </button>
                <span style={{ fontSize: "10px", color: "var(--ai)", background: "rgba(164, 138, 224,0.1)", padding: "2px 8px", borderRadius: "12px" }}>
                  Auto-Triggered
                </span>
              </div>
            </h3>

            {/* Filters — applied server-side so the range covers all history,
                not just the rows already loaded. */}
            <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "8px", marginBottom: "12px", padding: "8px 10px", background: "rgba(15, 19, 25,0.5)", borderRadius: "8px", border: "1px solid rgba(255,255,255,0.05)" }}>
              <input
                value={aiLogSearch}
                onChange={e => setAiLogSearch(e.target.value)}
                placeholder="Search symbol, company, summary or ref…"
                style={{ flex: 1, minWidth: "170px", padding: "6px 10px", fontSize: "11px", background: "rgba(10, 13, 18,0.8)", border: "1px solid rgba(164, 138, 224,0.3)", borderRadius: "6px", color: "var(--text-primary)" }}
              />
              <label style={{ fontSize: "10px", color: "var(--text-muted)", display: "flex", alignItems: "center", gap: "4px" }}>
                From
                <input type="date" value={aiLogDateFrom} onChange={e => setAiLogDateFrom(e.target.value)}
                  style={{ padding: "5px 7px", fontSize: "11px", background: "rgba(10, 13, 18,0.8)", border: "1px solid rgba(255,255,255,0.12)", borderRadius: "6px", color: "var(--text-primary)" }} />
              </label>
              <label style={{ fontSize: "10px", color: "var(--text-muted)", display: "flex", alignItems: "center", gap: "4px" }}>
                To
                <input type="date" value={aiLogDateTo} onChange={e => setAiLogDateTo(e.target.value)}
                  style={{ padding: "5px 7px", fontSize: "11px", background: "rgba(10, 13, 18,0.8)", border: "1px solid rgba(255,255,255,0.12)", borderRadius: "6px", color: "var(--text-primary)" }} />
              </label>
              {(aiLogSearch || aiLogDateFrom || aiLogDateTo) && (
                <button onClick={() => { setAiLogSearch(""); setAiLogDateFrom(""); setAiLogDateTo(""); }}
                  style={{ padding: "6px 10px", background: "transparent", border: "1px solid rgba(125, 135, 153,0.3)", borderRadius: "6px", color: "var(--text-muted)", fontSize: "11px", cursor: "pointer" }}>
                  Reset
                </button>
              )}
            </div>

            <div style={{ maxHeight: "560px", overflowY: "auto" }}>
              {aiLogs.length === 0 ? (
                <div style={{ color: "var(--text-faint)", fontSize: "12px", textAlign: "center", padding: "20px" }}>
                  {(aiLogSearch || aiLogDateFrom || aiLogDateTo)
                    ? "No AI analysis matches these filters."
                    : "No earnings AI analysis logs yet. Auto-trading poller will populate AI results as soon as announcements arrive."}
                </div>
              ) : (
                aiLogs.map((log) => {
                  // "NA" means the figures could not be extracted, so the card
                  // must not take on a directional (green/red) border either.
                  const suggestion = (log.ai_suggestion || "NA").toUpperCase();
                  const isNA = suggestion === "NA";
                  const isBeat = !isNA && (suggestion.includes("BEAT") || suggestion === "BUY");
                  const isMiss = !isNA && (suggestion.includes("MISS") || suggestion === "SELL");

                  return (
                    <div key={log.id} id={`ai-card-${(log.symbol || "").toUpperCase()}`} style={{
                      padding: "14px",
                      borderRadius: "10px",
                      backgroundColor: "var(--surface-1)",
                      border: isBeat ? "1px solid rgba(63, 191, 135, 0.3)" : isMiss ? "1px solid rgba(240, 115, 111, 0.3)" : "1px solid rgba(164, 138, 224, 0.25)",
                      marginBottom: "14px",
                      fontSize: "12px"
                    }}>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "10px" }}>
                        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                          <span style={{ fontSize: "15px", fontWeight: "800", color: "var(--text-primary)" }}>#{log.symbol}</span>
                          {log.company_name && (
                            <span style={{ fontSize: "11px", color: "var(--text-muted)", fontWeight: 500 }}>{log.company_name}</span>
                          )}
                          <VerdictBadge verdict={log.ai_suggestion} />
                          {log.tracking_ref && (
                            <span title="Matches the tracking reference on the arrival alert"
                              style={{ fontSize: "10px", fontFamily: "ui-monospace, Menlo, monospace", color: "var(--text-muted)",
                                       background: "var(--surface-2)", padding: "2px 6px", borderRadius: "4px" }}>
                              {log.tracking_ref}
                            </span>
                          )}
                          {log.ai_requested_at && (
                            <span style={{ fontSize: "10px", color: "var(--text-faint)" }}>
                              AI {clockTime(log.ai_requested_at)} → {clockTime(log.ai_completed_at)}
                              {elapsed(log.ai_requested_at, log.ai_completed_at) && ` (${elapsed(log.ai_requested_at, log.ai_completed_at)})`}
                            </span>
                          )}
                          {log.ai_sentiment && (
                            <span style={{
                              fontSize: "10px",
                              fontWeight: "700",
                              color: log.ai_sentiment === "positive" ? "var(--positive-strong)" : log.ai_sentiment === "negative" ? "var(--negative-strong)" : "var(--warning)",
                              textTransform: "uppercase"
                            }}>
                              ({log.ai_sentiment})
                            </span>
                          )}
                          {log.created_at && (
                            <span style={{
                              fontSize: "11px",
                              color: "var(--text-muted)",
                              fontWeight: "600",
                              marginLeft: "6px"
                            }}>
                              • 🕒 {new Date(log.created_at).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true })}
                            </span>
                          )}
                        </div>
                        <span style={{
                          fontSize: "10px",
                          fontWeight: "600",
                          color: log.flow_used === "custom_rest_api" ? "var(--accent)" : "var(--ai)",
                          background: log.flow_used === "custom_rest_api" ? "rgba(91, 157, 255,0.1)" : "rgba(164, 138, 224,0.1)",
                          padding: "2px 8px",
                          borderRadius: "10px",
                          border: log.flow_used === "custom_rest_api" ? "1px solid rgba(91, 157, 255,0.2)" : "1px solid rgba(164, 138, 224,0.2)"
                        }}>
                          {log.flow_used === "custom_rest_api" ? "Flow 1: Custom REST API" : `Flow 2: ${log.provider}`}
                        </span>
                      </div>

                      <div style={{ fontSize: "12px", fontWeight: "600", color: "var(--text-primary)", marginBottom: "10px" }}>
                        📢 {log.nse_event_title || "NSE Corporate Announcement"}
                      </div>

                      <div style={{ backgroundColor: "var(--surface-2)", padding: "10px 12px", borderRadius: "8px", marginBottom: "10px" }}>
                        <MetricsTable metrics={log.metrics} />
                        <ValidationNotice v={log.validation} />
                        {!log.metrics && (
                          <div style={{ fontSize: "11px", color: "var(--text-faint)", fontStyle: "italic" }}>
                            No structured metrics on this analysis (recorded before the metric grid was introduced).
                          </div>
                        )}
                      </div>

                      {log.future_growth_outlook && log.future_growth_outlook !== "NA" && (
                        <div style={{ fontSize: "11px", color: "var(--text-secondary)", marginBottom: "6px", lineHeight: "1.4" }}>
                          <strong style={{ color: "var(--info)" }}>🔮 Future Growth Outlook:</strong> {log.future_growth_outlook}
                        </div>
                      )}
                      {log.future_projected_numbers && log.future_projected_numbers !== "NA" && (
                        <div style={{ fontSize: "11px", color: "var(--text-secondary)", marginBottom: "6px", lineHeight: "1.4" }}>
                          <strong style={{ color: "var(--ai)" }}>📐 Future Projected Numbers:</strong> {log.future_projected_numbers}
                        </div>
                      )}
                      {log.broker_estimates && !["NA", "N/A"].includes(log.broker_estimates) && (
                        <div style={{ fontSize: "11px", color: "var(--text-secondary)", marginBottom: "8px", lineHeight: "1.4" }}>
                          <strong style={{ color: "var(--warning)" }}>🎯 Broker Estimates:</strong> {log.broker_estimates}
                        </div>
                      )}

                      <div style={{ fontSize: "11px", color: "var(--text-muted)", fontStyle: "italic", lineHeight: "1.4", borderTop: "1px dashed rgba(255,255,255,0.08)", paddingTop: "8px", marginTop: "8px" }}>
                        📝 "{log.ai_summary}"
                      </div>

                      {log.attachment_url && log.attachment_url.startsWith("http") && (
                        <div style={{ marginTop: "8px", textAlign: "right" }}>
                          <a
                            href={log.attachment_url}
                            target="_blank"
                            rel="noreferrer"
                            style={{ fontSize: "11px", color: "var(--accent)", textDecoration: "none", display: "inline-flex", alignItems: "center", gap: "4px" }}
                          >
                            View Full NSE Document <ExternalLink size={12} />
                          </a>
                        </div>
                      )}
                    </div>
                  );
                })
              )}
            </div>
          </div>
        </div>
      </div>
      {/* CHART MODAL */}
      {chartSymbol && (
        <div
          style={{
            position: "fixed",
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            zIndex: 100000,
            background: "rgba(0, 0, 0, 0.85)",
            backdropFilter: "blur(10px)",
            display: "flex",
            justifyContent: "center",
            alignItems: "center"
          }}
          onClick={() => { setChartSymbol(null); setChartInstrumentKey(null); setChartCandles([]); }}
        >
          <div
            style={{
              width: "90vw",
              maxWidth: "1100px",
              maxHeight: "85vh",
              background: "linear-gradient(135deg, rgba(15, 19, 25, 0.98) 0%, rgba(10, 14, 28, 0.99) 100%)",
              border: "1px solid rgba(91, 157, 255, 0.25)",
              borderRadius: "16px",
              boxShadow: "0 25px 80px rgba(0, 0, 0, 0.8), 0 0 40px rgba(91, 157, 255, 0.12)",
              padding: "24px",
              overflowY: "auto"
            }}
            onClick={(e) => e.stopPropagation()}
          >
            {/* Chart Header */}
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px" }}>
              <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
                <span style={{
                  padding: "4px 12px",
                  borderRadius: "8px",
                  background: "linear-gradient(135deg, var(--accent), var(--accent))",
                  color: "var(--on-accent)",
                  fontWeight: "800",
                  fontSize: "14px",
                  letterSpacing: "0.5px"
                }}>
                  {chartSymbol}
                </span>
                <span style={{ fontSize: "13px", color: "var(--text-muted)", fontWeight: "600" }}>
                  Price Chart • {chartPeriod}
                </span>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                {/* Period Selector */}
                <div style={{
                  display: "flex",
                  gap: "4px",
                  backgroundColor: "rgba(255,255,255,0.03)",
                  padding: "3px",
                  borderRadius: "8px",
                  border: "1px solid rgba(255,255,255,0.06)"
                }}>
                  {["1D", "5D", "1M", "3M", "6M", "1Y", "5Y"].map((p) => (
                    <button
                      key={p}
                      onClick={() => setChartPeriod(p)}
                      style={{
                        padding: "5px 10px",
                        fontSize: "11px",
                        fontWeight: "700",
                        borderRadius: "6px",
                        border: "none",
                        cursor: "pointer",
                        backgroundColor: chartPeriod === p ? "var(--accent)" : "transparent",
                        color: chartPeriod === p ? "var(--on-accent)" : "var(--text-muted)",
                        transition: "all 0.2s"
                      }}
                    >
                      {p}
                    </button>
                  ))}
                </div>
                {/* Close Button */}
                <button
                  onClick={() => { setChartSymbol(null); setChartInstrumentKey(null); setChartCandles([]); }}
                  style={{
                    padding: "6px 12px",
                    fontSize: "11px",
                    fontWeight: "700",
                    borderRadius: "6px",
                    backgroundColor: "rgba(240, 115, 111, 0.15)",
                    color: "var(--negative-strong)",
                    border: "1px solid rgba(240, 115, 111, 0.3)",
                    cursor: "pointer"
                  }}
                >
                  ✕ Close
                </button>
              </div>
            </div>

            {/* Chart Body */}
            {chartLoading ? (
              <div style={{ height: "400px", display: "flex", flexDirection: "column", justifyContent: "center", alignItems: "center", gap: "10px" }}>
                <RefreshCw className="animate-spin" style={{ color: "var(--accent)" }} size={24} />
                <p style={{ fontSize: "12px", color: "var(--text-muted)" }}>Loading {chartSymbol} candles for {chartPeriod}...</p>
              </div>
            ) : chartCandles && chartCandles.length > 0 ? (
              <Chart candles={chartCandles} period={chartPeriod} />
            ) : (
              <div style={{ height: "400px", display: "flex", justifyContent: "center", alignItems: "center", fontSize: "12px", color: "var(--text-faint)", textAlign: "center", padding: "0 24px" }}>
                {/* "No data for this period" and "this symbol lists nowhere" are
                    different answers, and only one of them is worth retrying. */}
                {chartInstrumentKey
                  ? `Chart data unavailable for ${chartSymbol} (${chartPeriod}). Try a different period.`
                  : `${chartSymbol} does not appear in Upstox's NSE or BSE instrument list, so there is no price history to chart.`}
              </div>
            )}

            {/* Reported quarters, under the price. The price says what the
                market thinks; this says what the company actually filed, and
                the two are worth reading together on a results decision. */}
            <div style={{ marginTop: "18px", paddingTop: "16px", borderTop: "1px solid var(--border-subtle)" }}>
              <div style={{ display: "flex", flexWrap: "wrap", alignItems: "baseline", gap: "10px", marginBottom: "10px" }}>
                <h4 style={{ margin: 0, fontSize: "13px", fontWeight: 700, color: "var(--text-primary)" }}>
                  Reported quarters
                </h4>
                <span style={{ fontSize: "10px", color: "var(--text-muted)" }}>
                  {quarterHistory?.ok
                    ? `${quarterHistory.metrics.length} lines · ${quarterHistory.points.length} quarters · value and year-on-year change`
                    : "value and year-on-year change per quarter"}
                </span>
                {quarterHistory?.source_url && (
                  <a href={quarterHistory.source_url} target="_blank" rel="noreferrer"
                     style={{ marginLeft: "auto", fontSize: "10px", color: "var(--text-muted)" }}>
                    screener.in
                  </a>
                )}
              </div>

              {quarterLoading ? (
                <div style={{ padding: "26px", textAlign: "center", fontSize: "11px", color: "var(--text-muted)" }}>
                  Loading reported quarters…
                </div>
              ) : quarterHistory?.ok && quarterHistory.points.length > 0 ? (
                <div>
                  {/* Quarter headings once, at the top: every strip below shares
                      these columns, and repeating them eight times would be
                      most of the ink on the panel. */}
                  <div style={{ display: "flex", gap: "3px", marginBottom: "6px",
                                position: "sticky", top: 0, zIndex: 1,
                                background: "var(--surface-1)", paddingBottom: "3px" }}>
                    {quarterHistory.points.map(pt => (
                      <div key={pt.quarter} style={{
                        flex: 1, minWidth: 0, textAlign: "center", fontSize: "10px",
                        fontWeight: 700, color: "var(--text-muted)", whiteSpace: "nowrap",
                        overflow: "hidden", textOverflow: "ellipsis",
                      }}>
                        {pt.quarter}
                      </div>
                    ))}
                  </div>

                  <CardBoundary label="These quarters">
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(420px, 1fr))", gap: "0 26px" }}>
                      {quarterHistory.metrics.map(m => (
                        <MetricStrip key={m.key} points={quarterHistory.points}
                                     metric={m.key} label={m.label} />
                      ))}
                    </div>
                  </CardBoundary>

                  <div style={{ fontSize: "9px", color: "var(--text-faint)", marginTop: "2px", lineHeight: 1.5 }}>
                    Figures in ₹ crore, percentage is against the same quarter a year earlier.
                    Expenses, interest and depreciation are drawn neutral — rising is not good news there.
                    A percentage off a negative or zero base is left blank rather than reported.
                  </div>
                </div>
              ) : (
                <div style={{ padding: "20px", textAlign: "center", fontSize: "11px", color: "var(--text-faint)" }}>
                  {/* Screener not holding a company is a different answer from a
                      request that failed, and only one is worth retrying. */}
                  {quarterHistory?.error
                    ? `No reported quarters for ${chartSymbol} — ${quarterHistory.error}.`
                    : `No reported quarters found for ${chartSymbol}.`}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
