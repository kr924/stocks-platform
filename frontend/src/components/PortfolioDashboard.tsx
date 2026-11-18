import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent } from "react";

const API_BASE = import.meta.env.VITE_API_BASE || (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1" ? "http://localhost:8000" : "");

/**
 * Upstox and Zerodha trades in one ledger (backend services/portfolio.py):
 * holdings across both brokers, realised profit, and a capital-gains
 * summary per financial year. Upstox trades come from its API (synced daily
 * at 16:10 and on demand); Zerodha's from the tradebook CSV uploaded here.
 */

type BrokerFilter = "" | "upstox" | "zerodha";

interface Holding {
  key: string; symbol: string | null; isin: string | null; broker: string; account?: string; name?: string;
  qty: number; avg_cost: number; invested: number; first_buy: string | null; last_buy: string | null;
  lots?: { date: string | null; qty: number; days: number | null; long_term: boolean | null }[];
  undated_qty?: number; source?: string;
  long_term_qty: number; next_long_term: string | null;
  ltp: number | null; value: number | null; pnl: number | null; pnl_pct: number | null; day_change_pct: number | null;
}
interface HoldingsResp {
  rows: Holding[];
  totals: Totals;
  by_name?: (Totals & { name: string })[];
  warnings: string[];
}
interface Totals {
  invested: number; value: number | null; pnl: number | null; day_pnl?: number | null; priced_invested: number; unpriced: number;
  realised_fy: string; realised_fy_capital: number; realised_fy_intraday: number;
}
interface Account { client_id: string; broker: string; name: string; trades: number }
interface TaxRow {
  broker: string; symbol: string | null; isin: string | null; buy_date: string; sell_date: string; qty: number;
  buy_price: number; sell_price: number; gain: number; term: string; grandfathered?: boolean; cost: number; sale_value: number;
  charges?: number; stt?: number; tax_gain?: number;
}
interface TaxResp {
  fy: string;
  stcg: { gain: number; loss: number; net: number; rate: number };
  ltcg: { gain: number; loss: number; net: number; rate: number; exemption: number; exemption_used: number; taxable: number };
  short_term_loss_set_off_against_long_term: number;
  long_term_loss_carried_forward: number;
  short_term_loss_carried_forward: number;
  intraday: { pnl: number; turnover: number; trades: number };
  estimated_tax: number; sale_value: number; rows: TaxRow[]; warnings: string[]; notes: string[];
  charges?: { deductible: number; stt: number; gross_gain: number };
  mf_other?: { rows: TaxRow[]; slab_gain: number; ltcg_gain: number; ltcg_tax: number };
}
interface Trade {
  id: number; broker: string; symbol: string; isin: string | null; exchange: string | null; trade_date: string;
  trade_time: string | null; side: string; quantity: number; price: number; trade_id: string | null; source: string; note: string | null;
  account?: string | null; name?: string;
}
interface Summary {
  counts: Record<string, number>; span: Record<string, { first: string; last: string }>;
  financial_years: string[]; current_fy: string;
  upstox_sync: { added: number; fetched: number; errors: string[]; at: string } | null;
  accounts?: Account[]; names?: string[]; upstox_account?: string | null; unassigned?: Record<string, number>;
}

interface MfLot { date: string | null; qty: number; days: number | null; long_term: boolean | null; cost: number }
interface MfRow {
  name: string; accounts: string[]; folios: string[]; isin: string; scheme: string; amc: string | null; category: string | null;
  tax_class: "equity" | "debt" | "other"; tax_why: string | null; overridden: boolean;
  units: number; invested: number; avg_nav: number | null; nav: number | null; nav_date: string | null; nav_source: string | null;
  value: number | null; pnl: number | null; pnl_pct: number | null; day_change_pct: number | null; day_pnl: number | null;
  xirr: number | null; lots: MfLot[]; long_term_units: number; next_long_term: string | null; undated_units: number;
  portfolio: { as_of: string | null; equity_pct: number | null; filename: string | null; n: number } | null;
}
interface MfTotals {
  invested: number; value: number | null; pnl: number | null; priced_invested: number; day_pnl: number | null;
  unpriced: number; xirr: number | null; realised_fy: string; realised_fy_gain: number;
}
interface MfResp {
  rows: MfRow[]; totals: MfTotals; by_name: (MfTotals & { name: string })[]; warnings: string[];
  nav: { last_ok: string | null; error: string | null; running: boolean };
  statements: { account: string; from: string | null; to: string | null }[];
}
interface ExpSource { type: "direct" | "etf" | "mf"; label: string; isin?: string; name: string; amount: number; pct_of_fund: number | null; as_of?: string; qty?: number }
interface ExpStock {
  key: string; isin: string | null; symbol: string | null; company: string; industry: string; sector?: string; kind?: string;
  direct_value: number; etf_value: number; mf_value: number; total: number; invested: number; pct: number | null;
  sources: ExpSource[]; holders?: string[];
}
interface ExpRoll { direct_value: number; etf_value: number; mf_value: number; total: number; invested: number; pct: number | null; stocks: number }
interface ExpIndustry extends ExpRoll { industry: string; rows: ExpStock[] }
interface ExpSector extends ExpRoll { sector: string; pct_of_equity: number | null; industries: ExpIndustry[] }
interface ExpBucket { key: string; label: string; value: number; invested: number; pct: number | null; sources: ExpSource[] }
interface ExpFundStatus { type: "etf" | "mf"; label: string; isin: string; name: string; amc: string | null; as_of: string | null;
                         stale: boolean; value: number; needs_file: boolean; asset_class: string; equity_pct?: number | null; stocks?: number }
interface ExposureResp {
  total: number; by_source: Record<"direct" | "etf" | "mf", { value: number; pct: number | null }>;
  allocation: { key: string; label: string; value: number; pct: number | null }[];
  sectors: ExpSector[]; buckets: ExpBucket[]; rows: ExpStock[];
  concentration: { top10_pct: number | null; top10_pct_of_equity: number | null; stocks: number; in_several: number };
  funds: ExpFundStatus[]; names: string[]; notes: string[];
}
interface OvRow {
  type: "stock" | "etf" | "mf"; key: string; isin: string | null; symbol: string | null; label: string | null; name: string;
  at: string[]; qty: number; avg: number | null; invested: number; value: number | null; pnl: number | null; pnl_pct: number | null;
  day_pnl: number | null; day_change_pct: number | null; price: number | null; price_date: string | null; xirr: number | null;
  weight: number | null; industry: string | null; sector: string | null; category: string | null; amc?: string | null;
}
interface OvTotals { count: number; invested: number; value: number; pnl: number | null; priced_invested: number;
                     day_pnl: number | null; unpriced: number; pct: number | null }
interface OverviewResp {
  rows: OvRow[]; totals: OvTotals; by_type: Record<"stock" | "etf" | "mf", OvTotals>;
  by_name: (OvTotals & { name: string; stock_value: number; etf_value: number; mf_value: number })[];
  realised_fy: string; realised_fy_shares: number; realised_fy_funds: number;
  nav: { last_ok: string | null; error: string | null; running: boolean } | null; warnings: string[];
}

const inr = (v: number | null | undefined, digits = 0) =>
  v == null || isNaN(v) ? "—" : `₹${v.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
const signed = (v: number | null | undefined, digits = 0) =>
  v == null || isNaN(v) ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}₹${Math.abs(v).toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
const pct = (v: number | null | undefined) => v == null || isNaN(v) ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;
const col = (v: number | null | undefined) => v == null || isNaN(v) ? "var(--text-faint)" : v > 0 ? "var(--positive)" : v < 0 ? "var(--negative)" : "var(--text-muted)";
const qtyTxt = (q: number) => Number.isInteger(q) ? q.toLocaleString("en-IN") : q.toFixed(3);
const brokerChip = (b: string) => b === "upstox" ? "U" : b === "zerodha" ? "Z" : b === "both" ? "U+Z" : b === "mf" ? "MF" : "?";

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 200)}`);
  return res.json();
}

export function PortfolioDashboard() {
  const [broker, setBroker] = useState<BrokerFilter>("");
  const [tab, setTab] = useState<"overview" | "holdings" | "funds" | "exposure" | "tax" | "trades">("overview");
  const [ov, setOv] = useState<OverviewResp | null>(null);
  const [mf, setMf] = useState<MfResp | null>(null);
  const [exp, setExp] = useState<ExposureResp | null>(null);
  const [expLoading, setExpLoading] = useState(false);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [hold, setHold] = useState<HoldingsResp | null>(null);
  const [fy, setFy] = useState<string>("");
  const [tax, setTax] = useState<TaxResp | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [tradeFilter, setTradeFilter] = useState("");
  const [busy, setBusy] = useState<string>("");
  const [msg, setMsg] = useState<{ text: string; tone: "ok" | "err" } | null>(null);
  const [importBroker, setImportBroker] = useState<"zerodha" | "upstox">("zerodha");
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [confirmDel, setConfirmDel] = useState<number | null>(null);
  const [sortKey, setSortKey] = useState<"value" | "pnl" | "pnl_pct" | "symbol" | "day">("value");
  // Tracking name (several client IDs can share one). "" = everyone.
  const [who, setWho] = useState<string>("");
  // Tax is per person, so the tax tab always shows one name.
  const [taxWho, setTaxWho] = useState<string>("");
  const [importAccount, setImportAccount] = useState<string>("");
  const [showAccounts, setShowAccounts] = useState(false);

  const names = Array.isArray(summary?.names) ? summary!.names : [];
  const accounts = Array.isArray(summary?.accounts) ? summary!.accounts : [];
  const taxName = taxWho || who || names[0] || "";
  const q = [broker ? `broker=${broker}` : "", who ? `name=${encodeURIComponent(who)}` : ""].filter(Boolean).join("&");

  const loadSummary = async () => {
    try {
      const s = await getJSON<Summary>("/api/portfolio/summary");
      setSummary(s);
      setFy(f => f || s.current_fy);
    } catch (e: any) { setMsg({ text: `Could not load the portfolio: ${e.message}`, tone: "err" }); }
  };
  const loadHoldings = async () => {
    try { setHold(await getJSON<HoldingsResp>(`/api/portfolio/holdings?${q}`)); } catch { /* shown via summary */ }
  };
  const loadTax = async () => {
    if (!fy) return;
    try {
      setTax(await getJSON<TaxResp>(`/api/portfolio/tax?fy=${fy}${broker ? `&broker=${broker}` : ""}${taxName ? `&name=${encodeURIComponent(taxName)}` : ""}`));
    } catch { /* ignore */ }
  };
  const loadTrades = async () => {
    try {
      setTrades(await getJSON<Trade[]>(`/api/portfolio/trades?limit=1000&${q}${tradeFilter.trim() ? `&symbol=${encodeURIComponent(tradeFilter.trim())}` : ""}`));
    } catch { /* ignore */ }
  };
  const loadMf = async () => {
    try { setMf(await getJSON<MfResp>(`/api/portfolio/mf/holdings${who ? `?name=${encodeURIComponent(who)}` : ""}`)); } catch { /* ignore */ }
  };
  const loadExp = async () => {
    setExpLoading(true);
    try { setExp(await getJSON<ExposureResp>(`/api/portfolio/exposure${who ? `?name=${encodeURIComponent(who)}` : ""}`)); }
    catch (e: any) { setMsg({ text: `Could not build the exposure view: ${e.message}`, tone: "err" }); }
    finally { setExpLoading(false); }
  };
  const loadOv = async () => {
    try { setOv(await getJSON<OverviewResp>(`/api/portfolio/overview${who ? `?name=${encodeURIComponent(who)}` : ""}`)); } catch { /* ignore */ }
  };
  const reloadAll = () => {
    loadSummary(); loadHoldings(); loadTax(); loadMf();
    if (tab === "overview") loadOv();
    if (tab === "trades") loadTrades();
    if (tab === "exposure") loadExp();
  };

  useEffect(() => { loadSummary(); }, []);
  useEffect(() => { loadHoldings(); }, [broker, who]);
  // Prices move: refresh holdings every 30 s while this view is open.
  useEffect(() => {
    const t = setInterval(loadHoldings, 30000);
    return () => clearInterval(t);
  }, [broker, who]);
  useEffect(() => { loadTax(); }, [fy, broker, taxName]);
  useEffect(() => { if (tab === "trades") loadTrades(); }, [tab, broker, who]);
  useEffect(() => { loadMf(); }, [who]);
  useEffect(() => { if (tab === "exposure") loadExp(); }, [tab, who]);
  // Share and ETF prices move during the session; funds' NAV once a day.
  useEffect(() => {
    if (tab !== "overview") return;
    loadOv();
    const t = setInterval(loadOv, 30000);
    return () => clearInterval(t);
  }, [tab, who]);

  const syncUpstox = async () => {
    setBusy("sync"); setMsg(null);
    try {
      const res = await fetch(`${API_BASE}/api/portfolio/sync/upstox`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.status);
      setMsg({ text: `Upstox: ${data.added} new trade(s) of ${data.fetched} read${data.errors?.length ? ` — ${data.errors.join("; ")}` : ""}.`,
               tone: data.errors?.length ? "err" : "ok" });
      reloadAll();
    } catch (e: any) {
      setMsg({ text: `Upstox sync failed: ${e.message}`, tone: "err" });
    } finally { setBusy(""); }
  };

  const onFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    setBusy("import"); setMsg(null);
    try {
      const content = await f.text();
      const res = await fetch(`${API_BASE}/api/portfolio/import`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ broker: importBroker, filename: f.name, content, account_id: importAccount || null }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.status);
      setMsg({
        text: `${f.name} (${data.account_id}): ${data.added} trade(s) added, ${data.duplicates} already on file`
          + (data.skipped_non_equity ? `, ${data.skipped_non_equity} F&O/other skipped` : "")
          + (data.errors?.length ? `. ${data.errors.length} line(s) unreadable: ${data.errors.slice(0, 2).join("; ")}` : "."),
        tone: data.errors?.length ? "err" : "ok",
      });
      reloadAll();
    } catch (err: any) {
      setMsg({ text: `Import failed: ${err.message}`, tone: "err" });
    } finally { setBusy(""); }
  };

  const delTrade = async (id: number) => {
    if (confirmDel !== id) { setConfirmDel(id); setTimeout(() => setConfirmDel(c => c === id ? null : c), 3000); return; }
    setConfirmDel(null);
    await fetch(`${API_BASE}/api/portfolio/trades/${id}`, { method: "DELETE" });
    reloadAll(); loadTrades();
  };

  const rows = useMemo(() => {
    const r = Array.isArray(hold?.rows) ? [...hold!.rows] : [];
    const v = (h: Holding) => sortKey === "pnl" ? (h.pnl ?? -Infinity) : sortKey === "pnl_pct" ? (h.pnl_pct ?? -Infinity)
      : sortKey === "day" ? (h.day_change_pct ?? -Infinity) : (h.value ?? h.invested);
    if (sortKey === "symbol") r.sort((a, b) => (a.symbol || "").localeCompare(b.symbol || ""));
    else r.sort((a, b) => v(b) - v(a));
    return r;
  }, [hold, sortKey]);

  const t = hold?.totals;
  const empty = summary && Object.keys(summary.counts || {}).length === 0 && !(mf?.rows?.length);
  const lastSync = summary?.upstox_sync?.at ? new Date(summary.upstox_sync.at) : null;

  const card = { background: "var(--surface-1)", border: "1px solid var(--border-default)", borderRadius: "12px", padding: "16px" };
  const btn = (on = false) => ({
    padding: "6px 12px", fontSize: "12px", fontWeight: 700 as const, borderRadius: "7px", cursor: "pointer",
    border: `1px solid ${on ? "rgba(var(--accent-rgb),0.45)" : "var(--border-default)"}`,
    background: on ? "var(--accent-bg)" : "transparent", color: on ? "var(--accent)" : "var(--text-secondary)",
  });
  const th = (align: "left" | "right" = "right") => ({
    textAlign: align, padding: "6px 10px", fontSize: "10px", fontWeight: 600 as const, color: "var(--text-muted)",
    borderBottom: "1px solid var(--border-subtle)", whiteSpace: "nowrap" as const, letterSpacing: "0.3px",
  });
  const td = (align: "left" | "right" = "right") => ({
    textAlign: align, padding: "7px 10px", fontSize: "12px", borderBottom: "1px solid var(--border-subtle)", whiteSpace: "nowrap" as const,
  });

  return (
    <div style={{ padding: "20px", maxWidth: "1400px", margin: "0 auto" }}>
      {/* Header: filter, sync and import */}
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "10px", marginBottom: "14px" }}>
        <h2 style={{ margin: 0, fontSize: "18px", fontWeight: 800, color: "var(--text-primary)" }}>Portfolio</h2>
        <div style={{ display: "inline-flex", gap: "4px" }}>
          {([["", "All"], ["upstox", "Upstox"], ["zerodha", "Zerodha"]] as [BrokerFilter, string][]).map(([b, label]) => (
            <button key={b || "all"} onClick={() => setBroker(b)} style={btn(broker === b)}>{label}</button>
          ))}
        </div>
        {names.length > 0 && (
          <div style={{ display: "inline-flex", gap: "4px", flexWrap: "wrap" }} title="Tracking name — set per client ID under Accounts">
            {["", ...names].map(n => (
              <button key={n || "everyone"} onClick={() => { setWho(n); if (n) setTaxWho(n); }}
                style={{ ...btn(who === n), borderStyle: who === n ? "solid" : "dashed" }}>{n || "Everyone"}</button>
            ))}
          </div>
        )}
        <div style={{ marginLeft: "auto", display: "flex", flexWrap: "wrap", gap: "8px", alignItems: "center" }}>
          <button onClick={syncUpstox} disabled={busy === "sync"} style={btn()}
            title="Trades from Upstox's trade-history API (last three financial years) and today's trades. Also runs at 16:10 on weekdays.">
            {busy === "sync" ? "Syncing Upstox…" : "Sync Upstox"}
          </button>
          <select value={importBroker} onChange={e => setImportBroker(e.target.value as any)}
            style={{ fontSize: "12px", padding: "5px 6px", background: "var(--surface-1)", color: "var(--text-primary)", border: "1px solid var(--border-default)", borderRadius: "7px" }}>
            <option value="zerodha">Zerodha CSV</option>
            <option value="upstox">Upstox CSV</option>
          </select>
          <select value={importAccount} onChange={e => setImportAccount(e.target.value)}
            title="Zerodha's file name carries the client ID (tradebook-QY8357-EQ.csv); pick one only when it does not."
            style={{ fontSize: "12px", padding: "5px 6px", background: "var(--surface-1)", color: "var(--text-primary)", border: "1px solid var(--border-default)", borderRadius: "7px" }}>
            <option value="">ID from file name</option>
            {accounts.filter(a => a.broker === importBroker).map(a => (
              <option key={a.client_id} value={a.client_id}>{a.client_id} · {a.name}</option>
            ))}
          </select>
          <button onClick={() => fileRef.current?.click()} disabled={busy === "import"} style={btn()}
            title="Zerodha: Console → Reports → Tradebook → Equity, pick the dates, download CSV. Importing the same file again adds nothing.">
            {busy === "import" ? "Importing…" : "Import tradebook"}
          </button>
          <input ref={fileRef} type="file" accept=".csv,text/csv" onChange={onFile} style={{ display: "none" }} />
          <button onClick={() => setShowAdd(v => !v)} style={btn(showAdd)}
            title="IPO allotments, bonus shares (price 0), or trades older than your files">Add trade</button>
          <button onClick={() => setShowAccounts(v => !v)} style={btn(showAccounts)}
            title="Client IDs and the tracking names they roll up under">Accounts ({accounts.length})</button>
        </div>
      </div>

      {msg && (
        <div style={{ marginBottom: "12px", padding: "8px 12px", borderRadius: "8px", fontSize: "12px",
                      background: msg.tone === "ok" ? "var(--positive-bg)" : "var(--negative-bg)",
                      color: msg.tone === "ok" ? "var(--positive-strong)" : "var(--negative-strong)" }}>
          {msg.text}
        </div>
      )}

      {showAdd && <AddTrade accounts={accounts} onDone={() => { setShowAdd(false); reloadAll(); }} />}
      {showAccounts && <Accounts accounts={accounts} unassigned={summary?.unassigned || {}}
                                 upstoxAccount={summary?.upstox_account || null} onChanged={reloadAll} />}
      {!showAccounts && (Object.entries(summary?.unassigned || {}) as [string, number][]).some(([b, n]) => b !== "upstox" && n > 0) && (
        <div style={{ marginBottom: "12px", padding: "8px 12px", borderRadius: "8px", fontSize: "12px",
                      background: "var(--warning-bg)", color: "var(--warning)" }}>
          {(Object.entries(summary!.unassigned!) as [string, number][]).filter(([b]) => b !== "upstox").map(([b, n]) => `${n} ${b} trade(s)`).join(", ")} have no
          client ID yet (imported before IDs were tracked). Open <b style={{ cursor: "pointer", textDecoration: "underline" }}
          onClick={() => setShowAccounts(true)}>Accounts</b> to assign them.
        </div>
      )}

      {empty && (
        <div style={{ ...card, marginBottom: "14px", fontSize: "13px", color: "var(--text-secondary)", lineHeight: 1.7 }}>
          <b style={{ color: "var(--text-primary)" }}>No trades on file yet.</b><br />
          1. <b>Sync Upstox</b> pulls your Upstox equity trades for the last three financial years (log in to Upstox first).<br />
          2. For Zerodha, open Console → Reports → Tradebook → Equity, choose a date range (repeat per year if needed),
          download <b>CSV</b>, then <b>Import tradebook</b>.<br />
          3. Shares from an IPO, a bonus, or older than the files go in with <b>Add trade</b>.<br />
          4. Mutual funds: open <b>Mutual funds</b> and import your CAMS / KFintech consolidated statement (PDF).
        </div>
      )}

      {/* Tiles (shares; the fund and exposure tabs carry their own) */}
      {(tab === "holdings" || tab === "tax" || tab === "trades") && <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: "10px", marginBottom: "14px" }}>
        <Tile label="Invested" value={inr(t?.invested)} />
        <Tile label="Current value" value={inr(t?.value)} sub={t?.unpriced ? `${t.unpriced} without a live price` : undefined} />
        <Tile label="Unrealised P&L" value={signed(t?.pnl)} color={col(t?.pnl)}
              sub={t?.pnl != null && t.priced_invested ? pct(t.pnl / t.priced_invested * 100) : undefined} />
        <Tile label={`Realised FY ${t?.realised_fy || ""}`} value={signed(t?.realised_fy_capital)} color={col(t?.realised_fy_capital)}
              sub="delivery (capital gains)" />
        <Tile label={`Intraday FY ${t?.realised_fy || ""}`} value={signed(t?.realised_fy_intraday)} color={col(t?.realised_fy_intraday)}
              sub="speculative income" />
      </div>}

      {(tab === "holdings" || tab === "tax" || tab === "trades") && !who && (hold?.by_name?.length ?? 0) > 1 && (
        <div style={{ ...card, padding: 0, overflowX: "auto", marginBottom: "14px" }}>
          <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "760px" }}>
            <thead>
              <tr>
                {["BY NAME", "INVESTED", "VALUE", "UNREALISED P&L", "DAY'S P&L", `REALISED FY ${t?.realised_fy || ""}`, "INTRADAY FY"].map((h, i) => (
                  <th key={h} style={th(i === 0 ? "left" : "right")}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {hold!.by_name!.map(b => (
                <tr key={b.name} onClick={() => { setWho(b.name); setTaxWho(b.name); }} style={{ cursor: "pointer" }} title={`Show only ${b.name}`}>
                  <td style={{ ...td("left"), fontWeight: 700, color: "var(--text-primary)" }}>{b.name}</td>
                  <td style={td()}>{inr(b.invested)}</td>
                  <td style={td()}>{inr(b.value)}</td>
                  <td style={{ ...td(), color: col(b.pnl), fontWeight: 700 }}>
                    {signed(b.pnl)} <span style={{ fontSize: "11px", fontWeight: 600 }}>{b.pnl != null && b.priced_invested ? pct(b.pnl / b.priced_invested * 100) : ""}</span>
                  </td>
                  <td style={{ ...td(), color: col(b.day_pnl) }}>{signed(b.day_pnl)}</td>
                  <td style={{ ...td(), color: col(b.realised_fy_capital) }}>{signed(b.realised_fy_capital)}</td>
                  <td style={{ ...td(), color: col(b.realised_fy_intraday) }}>{signed(b.realised_fy_intraday)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div style={{ display: "flex", gap: "6px", marginBottom: "10px", alignItems: "center", flexWrap: "wrap" }}>
        {(["overview", "exposure", "holdings", "funds", "tax", "trades"] as const).map(k => (
          <button key={k} onClick={() => setTab(k)} style={btn(tab === k)}
            title={k === "exposure" ? "Sectors → industries → stocks, counting shares held directly and the stocks inside your ETFs and funds"
              : k === "overview" ? "Shares, ETFs and mutual funds in one list" : undefined}>
            {k === "overview" ? "All holdings" : k === "holdings" ? `Shares & ETFs (${rows.length})` : k === "funds" ? `Mutual funds (${mf?.rows?.length ?? 0})`
              : k === "exposure" ? "Analytics" : k === "tax" ? "Capital gains & tax" : "Trades"}
          </button>
        ))}
        <span style={{ marginLeft: "auto", fontSize: "11px", color: "var(--text-faint)" }}>
          {summary?.span?.upstox && `Upstox ${summary.span.upstox.first} → ${summary.span.upstox.last}`}
          {summary?.span?.upstox && summary?.span?.zerodha && " · "}
          {summary?.span?.zerodha && `Zerodha ${summary.span.zerodha.first} → ${summary.span.zerodha.last}`}
          {lastSync && ` · Upstox synced ${lastSync.toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}`}
        </span>
      </div>

      {tab === "holdings" && (hold?.warnings?.length ?? 0) > 0 && (
        <details style={{ ...card, padding: "10px 14px", marginBottom: "10px", borderColor: "var(--warning-border)", background: "var(--warning-bg)" }}>
          <summary style={{ cursor: "pointer", fontSize: "12px", fontWeight: 700, color: "var(--warning)" }}>
            {hold!.warnings.length} thing(s) to check — the figures for these stocks are incomplete
          </summary>
          {hold!.warnings.map((w, i) => <div key={i} style={{ fontSize: "12px", color: "var(--text-secondary)", marginTop: "4px" }}>• {w}</div>)}
        </details>
      )}

      {tab === "holdings" && (
        <div style={{ ...card, padding: 0, overflowX: "auto" }}>
          <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "980px" }}>
            <thead>
              <tr>
                <th style={{ ...th("left"), cursor: "pointer" }} onClick={() => setSortKey("symbol")}>SYMBOL</th>
                <th style={th("left")}>AT</th>
                {names.length > 1 && <th style={th("left")}>NAME</th>}
                <th style={th()}>QTY</th>
                <th style={th()}>AVG COST</th>
                <th style={th()}>LTP</th>
                <th style={{ ...th(), cursor: "pointer" }} onClick={() => setSortKey("day")}>DAY</th>
                <th style={th()}>INVESTED</th>
                <th style={{ ...th(), cursor: "pointer" }} onClick={() => setSortKey("value")}>VALUE</th>
                <th style={{ ...th(), cursor: "pointer" }} onClick={() => setSortKey("pnl")}>P&L</th>
                <th style={{ ...th(), cursor: "pointer" }} onClick={() => setSortKey("pnl_pct")}>P&L %</th>
                <th style={th()} title="Shares held more than 12 months: a sale is long-term (12.5% above ₹1.25 lakh a year) rather than short-term (20%)">LONG-TERM</th>
                <th style={th("left")} title="Quantity · days held, per purchase">HELD (QTY · DAYS)</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(h => (
                <tr key={`${h.broker}:${h.account || ""}:${h.key}`}>
                  <td style={{ ...td("left"), fontWeight: 700, color: "var(--text-primary)" }} title={h.isin || ""}>{h.symbol || h.key}</td>
                  <td style={td("left")}><span className="wl-chip wl-b on" style={{ cursor: "default" }}>{brokerChip(h.broker)}</span></td>
                  {names.length > 1 && <td style={{ ...td("left"), color: "var(--text-secondary)" }} title={h.account || ""}>{h.name}</td>}
                  <td style={td()}>{qtyTxt(h.qty)}</td>
                  <td style={td()}>{inr(h.avg_cost, 2)}</td>
                  <td style={td()}>{h.ltp ? inr(h.ltp, 2) : "—"}</td>
                  <td style={{ ...td(), color: col(h.day_change_pct) }}>{pct(h.day_change_pct)}</td>
                  <td style={td()}>{inr(h.invested)}</td>
                  <td style={{ ...td(), fontWeight: 700 }}>{inr(h.value)}</td>
                  <td style={{ ...td(), color: col(h.pnl), fontWeight: 700 }}>{signed(h.pnl)}</td>
                  <td style={{ ...td(), color: col(h.pnl_pct) }}>{pct(h.pnl_pct)}</td>
                  <td style={{ ...td(), color: "var(--text-secondary)" }}
                      title={h.next_long_term ? `More become long-term on ${h.next_long_term}` : "All long-term"}>
                    {h.long_term_qty >= h.qty ? "all" : h.long_term_qty > 0 ? `${qtyTxt(h.long_term_qty)} of ${qtyTxt(h.qty)}`
                      : h.next_long_term ? `from ${h.next_long_term}` : "older lots — date unknown"}
                  </td>
                  <td style={{ ...td("left"), color: "var(--text-secondary)", fontSize: "11px" }}
                      title={(h.lots || []).map(l => `${qtyTxt(l.qty)} bought ${l.date ?? "before the trade history"}`).join("\n")}>
                    {(h.lots || []).slice(0, 3).map(l => `${qtyTxt(l.qty)} · ${l.days == null ? "older" : `${l.days}d`}`).join(", ")}
                    {(h.lots || []).length > 3 ? ` +${(h.lots || []).length - 3}` : ""}
                  </td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr><td colSpan={names.length > 1 ? 13 : 12} style={{ ...td("left"), color: "var(--text-faint)", fontStyle: "italic" }}>No open positions on file.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {tab === "overview" && (
        <OverviewTab data={ov} many={names.length > 1} who={who} onPick={n => { setWho(n); setTaxWho(n); }}
                     onTab={k => setTab(k)} />
      )}

      {tab === "funds" && (
        <FundsTab data={mf} names={names} who={who} onReload={reloadAll} setMsg={setMsg} />
      )}

      {tab === "exposure" && (
        <AnalyticsTab data={exp} loading={expLoading} onRefresh={loadExp} setMsg={setMsg} />
      )}

      {tab === "tax" && (
        <div>
          <div style={{ display: "flex", gap: "8px", alignItems: "center", marginBottom: "10px", flexWrap: "wrap" }}>
            <span style={{ fontSize: "12px", color: "var(--text-muted)" }}>Financial year</span>
            <select value={fy} onChange={e => setFy(e.target.value)}
              style={{ fontSize: "12px", padding: "5px 6px", background: "var(--surface-1)", color: "var(--text-primary)", border: "1px solid var(--border-default)", borderRadius: "7px" }}>
              {(summary?.financial_years || [fy]).map(f => <option key={f} value={f}>FY {f}</option>)}
            </select>
            {names.length > 0 && (
              <>
                <span style={{ fontSize: "12px", color: "var(--text-muted)" }}>for</span>
                <select value={taxName} onChange={e => setTaxWho(e.target.value)}
                  title="Tax is per person: each name has its own ₹1.25 lakh LTCG exemption and loss set-off"
                  style={{ fontSize: "12px", padding: "5px 6px", background: "var(--surface-1)", color: "var(--text-primary)", border: "1px solid var(--border-default)", borderRadius: "7px" }}>
                  {names.map(n => <option key={n} value={n}>{n}</option>)}
                </select>
              </>
            )}
            {fy && (
              <a href={`${API_BASE}/api/portfolio/realised.csv?fy=${fy}${broker ? `&broker=${broker}` : ""}${taxName ? `&name=${encodeURIComponent(taxName)}` : ""}`}
                 style={{ ...btn(), textDecoration: "none", marginLeft: "auto" }}>Download capital-gains CSV</a>
            )}
          </div>
          {tax && (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: "10px", marginBottom: "10px" }}>
                <Tile label={`Short-term (STCG ${(tax.stcg.rate * 100).toFixed(1).replace(".0", "")}%)`} value={signed(tax.stcg.net)} color={col(tax.stcg.net)}
                      sub={`gains ${inr(tax.stcg.gain)} · losses ${inr(-tax.stcg.loss)}`} />
                <Tile label={`Long-term (LTCG ${(tax.ltcg.rate * 100).toFixed(1).replace(".0", "")}%)`} value={signed(tax.ltcg.net)} color={col(tax.ltcg.net)}
                      sub={`gains ${inr(tax.ltcg.gain)} · losses ${inr(-tax.ltcg.loss)}`} />
                <Tile label="LTCG exemption used" value={`${inr(tax.ltcg.exemption_used)} of ${inr(tax.ltcg.exemption)}`}
                      sub={`taxable LTCG ${inr(tax.ltcg.taxable)}`} />
                <Tile label="Estimated tax on capital gains" value={inr(tax.estimated_tax)} color="var(--warning)"
                      sub={tax.short_term_loss_set_off_against_long_term ? `after ${inr(tax.short_term_loss_set_off_against_long_term)} short-term loss set off` : "before cess and surcharge"} />
                <Tile label="Intraday (speculative)" value={signed(tax.intraday.pnl)} color={col(tax.intraday.pnl)}
                      sub={`${tax.intraday.trades} round trip(s) · taxed at your slab`} />
              </div>
              {tax.charges && (
                <div style={{ fontSize: "12px", color: "var(--text-secondary)", marginBottom: "8px" }}>
                  Estimated charges on these sales: {inr(tax.charges.deductible)} brokerage, exchange, SEBI, stamp, GST and DP
                  (deducted from the gains above) + {inr(tax.charges.stt)} STT (not deductible from capital gains).
                  Gains before charges: {signed(tax.charges.gross_gain)}.
                </div>
              )}
              {(tax.long_term_loss_carried_forward > 0 || tax.short_term_loss_carried_forward > 0) && (
                <div style={{ fontSize: "12px", color: "var(--text-secondary)", marginBottom: "8px" }}>
                  Loss to carry forward (file the return on time to keep it):
                  {tax.short_term_loss_carried_forward > 0 && ` short-term ${inr(tax.short_term_loss_carried_forward)}`}
                  {tax.long_term_loss_carried_forward > 0 && ` long-term ${inr(tax.long_term_loss_carried_forward)}`}
                </div>
              )}
              <div style={{ ...card, padding: 0, overflowX: "auto", marginBottom: "8px" }}>
                <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "900px" }}>
                  <thead>
                    <tr>
                      {["TYPE", "SYMBOL", "AT", "BOUGHT", "SOLD", "QTY", "BUY", "SELL", "COST", "SALE VALUE", "CHARGES", "STT", "GAIN"].map((h, i) => (
                        <th key={h} style={th(i < 3 ? "left" : "right")}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {tax.rows.map((r, i) => (
                      <tr key={i}>
                        <td style={{ ...td("left"), fontWeight: 700, color: r.term === "LTCG" ? "var(--positive-strong)" : r.term === "STCG" ? "var(--accent)" : "var(--ai)" }}
                            title={r.grandfathered ? "Bought before 1 Feb 2018: the 31 Jan 2018 value rule may lower this gain" : ""}>
                          {r.term}{r.grandfathered ? " *" : ""}
                        </td>
                        <td style={{ ...td("left"), fontWeight: 600 }}>{r.symbol}</td>
                        <td style={td("left")}>{brokerChip(r.broker)}</td>
                        <td style={td()}>{r.buy_date}</td>
                        <td style={td()}>{r.sell_date}</td>
                        <td style={td()}>{qtyTxt(r.qty)}</td>
                        <td style={td()}>{inr(r.buy_price, 2)}</td>
                        <td style={td()}>{inr(r.sell_price, 2)}</td>
                        <td style={td()}>{inr(r.cost)}</td>
                        <td style={td()}>{inr(r.sale_value)}</td>
                        <td style={{ ...td(), color: "var(--text-muted)" }}>{inr(r.charges, 2)}</td>
                        <td style={{ ...td(), color: "var(--text-muted)" }}>{inr(r.stt, 2)}</td>
                        <td style={{ ...td(), color: col(r.tax_gain ?? r.gain), fontWeight: 700 }}
                            title={`Before charges ${signed(r.gain)}`}>{signed(r.tax_gain ?? r.gain)}</td>
                      </tr>
                    ))}
                    {tax.rows.length === 0 && (
                      <tr><td colSpan={13} style={{ ...td("left"), color: "var(--text-faint)", fontStyle: "italic" }}>No sales in FY {tax.fy}.</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
              {(tax.mf_other?.rows?.length ?? 0) > 0 && (
                <div style={{ ...card, padding: 0, overflowX: "auto", marginBottom: "8px" }}>
                  <div style={{ padding: "10px 12px 4px", fontSize: "12px", fontWeight: 700, color: "var(--text-primary)" }}>
                    Mutual funds not taxed as equity
                    <span style={{ fontWeight: 400, color: "var(--text-muted)" }}>
                      {" "}— at your slab {signed(tax.mf_other!.slab_gain)}; long-term at 12.5% {signed(tax.mf_other!.ltcg_gain)}
                      {tax.mf_other!.ltcg_tax > 0 ? ` (≈ ${inr(tax.mf_other!.ltcg_tax)} tax)` : ""}. Not in the estimate above.
                    </span>
                  </div>
                  <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "760px" }}>
                    <thead>
                      <tr>{["TYPE", "SCHEME", "BOUGHT", "SOLD", "UNITS", "COST", "SALE VALUE", "GAIN"].map((h, i) => (
                        <th key={h} style={th(i < 2 ? "left" : "right")}>{h}</th>))}</tr>
                    </thead>
                    <tbody>
                      {tax.mf_other!.rows.map((r, i) => (
                        <tr key={i}>
                          <td style={{ ...td("left"), fontWeight: 700, color: "var(--ai)" }}>{r.term === "SLAB" ? "AT SLAB" : "LTCG 12.5%"}</td>
                          <td style={{ ...td("left"), maxWidth: "320px", overflow: "hidden", textOverflow: "ellipsis" }} title={r.symbol || ""}>{r.symbol}</td>
                          <td style={td()}>{r.buy_date}</td>
                          <td style={td()}>{r.sell_date}</td>
                          <td style={td()}>{qtyTxt(r.qty)}</td>
                          <td style={td()}>{inr(r.cost)}</td>
                          <td style={td()}>{inr(r.sale_value)}</td>
                          <td style={{ ...td(), color: col(r.gain), fontWeight: 700 }}>{signed(r.gain)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {tax.notes.map((n, i) => <div key={i} style={{ fontSize: "11px", color: "var(--text-faint)", lineHeight: 1.6 }}>{n}</div>)}
            </>
          )}
        </div>
      )}

      {tab === "trades" && (
        <div>
          <div style={{ display: "flex", gap: "8px", marginBottom: "10px" }}>
            <input value={tradeFilter} onChange={e => setTradeFilter(e.target.value)} placeholder="Symbol or ISIN"
              onKeyDown={e => { if (e.key === "Enter") loadTrades(); }}
              style={{ padding: "6px 10px", fontSize: "12px", background: "var(--surface-1)", color: "var(--text-primary)", border: "1px solid var(--border-default)", borderRadius: "7px" }} />
            <button onClick={loadTrades} style={btn()}>Show</button>
          </div>
          <div style={{ ...card, padding: 0, overflowX: "auto" }}>
            <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "820px" }}>
              <thead>
                <tr>
                  {["DATE", "SYMBOL", "AT", "NAME", "SIDE", "QTY", "PRICE", "VALUE", "SOURCE", ""].map((h, i) => (
                    <th key={i} style={th(i < 5 ? "left" : "right")}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {trades.map(tr => (
                  <tr key={tr.id}>
                    <td style={td("left")}>{tr.trade_date}{tr.trade_time ? ` ${tr.trade_time.slice(0, 5)}` : ""}</td>
                    <td style={{ ...td("left"), fontWeight: 600 }} title={tr.isin || ""}>{tr.symbol}</td>
                    <td style={td("left")}>{brokerChip(tr.broker)}</td>
                    <td style={{ ...td("left"), color: "var(--text-secondary)" }} title={tr.account || "no client ID"}>{tr.name || "—"}</td>
                    <td style={{ ...td("left"), fontWeight: 700, color: tr.side === "BUY" ? "var(--positive)" : "var(--negative)" }}>{tr.side}</td>
                    <td style={td()}>{qtyTxt(tr.quantity)}</td>
                    <td style={td()}>{inr(tr.price, 2)}</td>
                    <td style={td()}>{inr(tr.quantity * tr.price)}</td>
                    <td style={{ ...td(), color: "var(--text-muted)", fontSize: "11px" }} title={tr.note || ""}>{tr.source}</td>
                    <td style={td()}>
                      <button onClick={() => delTrade(tr.id)}
                        style={{ fontSize: "10px", padding: "2px 6px", borderRadius: "5px", cursor: "pointer",
                                 border: `1px solid ${confirmDel === tr.id ? "var(--negative)" : "var(--border-default)"}`,
                                 background: confirmDel === tr.id ? "var(--negative-bg)" : "transparent",
                                 color: confirmDel === tr.id ? "var(--negative)" : "var(--text-muted)" }}>
                        {confirmDel === tr.id ? "Delete?" : "✕"}
                      </button>
                    </td>
                  </tr>
                ))}
                {trades.length === 0 && (
                  <tr><td colSpan={10} style={{ ...td("left"), color: "var(--text-faint)", fontStyle: "italic" }}>No trades.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function Tile({ label, value, sub, color }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div style={{ background: "var(--surface-1)", border: "1px solid var(--border-default)", borderRadius: "12px", padding: "12px 14px" }}>
      <div style={{ fontSize: "10px", fontWeight: 700, color: "var(--text-muted)", letterSpacing: "0.4px", textTransform: "uppercase" }}>{label}</div>
      <div style={{ fontSize: "18px", fontWeight: 800, color: color || "var(--text-primary)", marginTop: "4px" }}>{value}</div>
      {sub && <div style={{ fontSize: "11px", color: "var(--text-faint)", marginTop: "2px" }}>{sub}</div>}
    </div>
  );
}

/** By hand: IPO allotments, bonus shares (price 0), or history older than the files. */
function AddTrade({ onDone, accounts }: { onDone: () => void; accounts: Account[] }) {
  const [f, setF] = useState({ broker: "zerodha", account_id: "", symbol: "", isin: "", trade_date: "", side: "BUY", quantity: "", price: "", note: "" });
  const [err, setErr] = useState("");
  const set = (k: keyof typeof f) => (e: ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });
  const submit = async () => {
    const quantity = parseFloat(f.quantity), price = parseFloat(f.price);
    if (!f.symbol.trim() || !f.trade_date || !(quantity > 0) || !(price >= 0)) { setErr("Symbol, date, quantity and price are needed."); return; }
    const res = await fetch(`${API_BASE}/api/portfolio/trades`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...f, account_id: f.account_id || null, quantity, price }),
    });
    if (!res.ok) { setErr((await res.json()).detail || "Could not add"); return; }
    onDone();
  };
  const inp = { padding: "6px 8px", fontSize: "12px", background: "var(--surface-1)", color: "var(--text-primary)", border: "1px solid var(--border-default)", borderRadius: "7px" };
  return (
    <div style={{ background: "var(--surface-2)", border: "1px solid var(--border-default)", borderRadius: "12px", padding: "12px", marginBottom: "14px",
                  display: "flex", flexWrap: "wrap", gap: "8px", alignItems: "center" }}>
      <select value={f.broker} onChange={e => setF({ ...f, broker: e.target.value, account_id: "" })} style={inp}><option value="zerodha">Zerodha</option><option value="upstox">Upstox</option></select>
      <select value={f.account_id} onChange={set("account_id")} style={inp} title="Client ID the trade belongs to">
        <option value="">{f.broker === "upstox" ? "Connected Upstox account" : "Client ID…"}</option>
        {accounts.filter(a => a.broker === f.broker).map(a => <option key={a.client_id} value={a.client_id}>{a.client_id} · {a.name}</option>)}
      </select>
      <input value={f.symbol} onChange={set("symbol")} placeholder="Symbol (e.g. INFY)" style={{ ...inp, width: "130px" }} />
      <input value={f.isin} onChange={set("isin")} placeholder="ISIN (optional)" style={{ ...inp, width: "140px" }} />
      <input type="date" value={f.trade_date} onChange={set("trade_date")} style={inp} />
      <select value={f.side} onChange={set("side")} style={inp}><option value="BUY">Buy / allotted</option><option value="SELL">Sell</option></select>
      <input value={f.quantity} onChange={set("quantity")} placeholder="Qty" inputMode="decimal" style={{ ...inp, width: "80px" }} />
      <input value={f.price} onChange={set("price")} placeholder="Price (0 for bonus)" inputMode="decimal" style={{ ...inp, width: "140px" }} />
      <input value={f.note} onChange={set("note")} placeholder="Note (IPO, bonus 1:1…)" style={{ ...inp, width: "170px" }} />
      <button onClick={submit} style={{ ...inp, cursor: "pointer", fontWeight: 700, color: "var(--on-accent)", background: "var(--accent)", border: "none" }}>Add</button>
      {err && <span style={{ fontSize: "12px", color: "var(--negative)" }}>{err}</span>}
      <span style={{ fontSize: "11px", color: "var(--text-faint)", flexBasis: "100%" }}>
        Use the ISIN when the same company trades under a different symbol at the other broker — trades are matched by ISIN first.
      </span>
    </div>
  );
}


/** Client IDs and their tracking names; untagged trades from before IDs were tracked. */
function Accounts({ accounts, unassigned, upstoxAccount, onChanged }: {
  accounts: Account[]; unassigned: Record<string, number>; upstoxAccount: string | null; onChanged: () => void;
}) {
  const [edit, setEdit] = useState<Record<string, string>>({});
  const [add, setAdd] = useState({ client_id: "", broker: "zerodha", name: "" });
  const [assignTo, setAssignTo] = useState<Record<string, string>>({});
  const [err, setErr] = useState("");
  const post = async (path: string, body: object, method = "POST") => {
    setErr("");
    const res = await fetch(`${API_BASE}/api/portfolio${path}`, {
      method, headers: { "Content-Type": "application/json" }, body: method === "DELETE" ? undefined : JSON.stringify(body),
    });
    if (!res.ok) { setErr((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`); return false; }
    onChanged();
    return true;
  };
  const inp = { padding: "5px 8px", fontSize: "12px", background: "var(--surface-1)", color: "var(--text-primary)", border: "1px solid var(--border-default)", borderRadius: "7px" };
  const small = { ...inp, cursor: "pointer", fontWeight: 700 as const };
  const names = Array.from(new Set(accounts.map(a => a.name))).sort();
  return (
    <div style={{ background: "var(--surface-2)", border: "1px solid var(--border-default)", borderRadius: "12px", padding: "12px 14px", marginBottom: "14px" }}>
      <div style={{ fontSize: "13px", fontWeight: 800, color: "var(--text-primary)", marginBottom: "4px" }}>Accounts</div>
      <div style={{ fontSize: "11px", color: "var(--text-muted)", marginBottom: "10px" }}>
        Each broker client ID rolls up under a tracking name; several IDs can share one (your Zerodha and Upstox under the same name).
        Holdings, P&L and tax break down by name. Zerodha IDs are read from the tradebook's file name; the Upstox ID from the connected login;
        mutual funds are filed under the PAN on the statement.
      </div>
      <table style={{ borderCollapse: "collapse", width: "100%", fontSize: "12px" }}>
        <thead>
          <tr>{["CLIENT ID", "BROKER", "TRACKING NAME", "TRADES", ""].map(h => (
            <th key={h} style={{ textAlign: "left", padding: "4px 8px", fontSize: "10px", color: "var(--text-muted)", borderBottom: "1px solid var(--border-subtle)" }}>{h}</th>
          ))}</tr>
        </thead>
        <tbody>
          {accounts.map(a => (
            <tr key={a.client_id}>
              <td style={{ padding: "5px 8px", fontWeight: 700 }}>{a.client_id}{a.client_id === upstoxAccount ? " · connected" : ""}</td>
              <td style={{ padding: "5px 8px" }}>{a.broker}</td>
              <td style={{ padding: "5px 8px" }}>
                <input value={edit[a.client_id] ?? a.name} list="pf-names" onChange={e => setEdit({ ...edit, [a.client_id]: e.target.value })} style={{ ...inp, width: "160px" }} />
              </td>
              <td style={{ padding: "5px 8px", color: "var(--text-muted)" }}>{a.trades}</td>
              <td style={{ padding: "5px 8px", whiteSpace: "nowrap" }}>
                {(edit[a.client_id] ?? a.name) !== a.name && (
                  <button style={small} onClick={async () => {
                    if (await post("/accounts", { client_id: a.client_id, broker: a.broker, name: edit[a.client_id] })) {
                      const n = { ...edit }; delete n[a.client_id]; setEdit(n);
                    }
                  }}>Save</button>
                )}
                {a.trades === 0 && (
                  <button style={{ ...small, marginLeft: "6px", color: "var(--negative)" }}
                    onClick={() => post(`/accounts/${encodeURIComponent(a.client_id)}`, {}, "DELETE")}>Remove</button>
                )}
              </td>
            </tr>
          ))}
          {accounts.length === 0 && (
            <tr><td colSpan={5} style={{ padding: "6px 8px", color: "var(--text-faint)", fontStyle: "italic" }}>
              None yet — they appear when you import a tradebook or sync Upstox, or add one below.
            </td></tr>
          )}
        </tbody>
      </table>
      <datalist id="pf-names">{names.map(n => <option key={n} value={n} />)}</datalist>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", alignItems: "center", marginTop: "10px" }}>
        <input value={add.client_id} onChange={e => setAdd({ ...add, client_id: e.target.value.toUpperCase() })} placeholder="Client ID (e.g. QY8357)" style={{ ...inp, width: "150px" }} />
        <select value={add.broker} onChange={e => setAdd({ ...add, broker: e.target.value })} style={inp}>
          <option value="zerodha">Zerodha</option><option value="upstox">Upstox</option><option value="mf">Mutual funds (PAN)</option>
        </select>
        <input value={add.name} list="pf-names" onChange={e => setAdd({ ...add, name: e.target.value })} placeholder="Tracking name" style={{ ...inp, width: "150px" }} />
        <button style={small} disabled={!add.client_id.trim() || !add.name.trim()}
          onClick={async () => { if (await post("/accounts", add)) setAdd({ client_id: "", broker: "zerodha", name: "" }); }}>Add account</button>
      </div>
      {(Object.entries(unassigned) as [string, number][]).filter(([, n]) => n > 0).map(([b, n]) => (
        <div key={b} style={{ display: "flex", flexWrap: "wrap", gap: "6px", alignItems: "center", marginTop: "10px", fontSize: "12px", color: "var(--warning)" }}>
          {n} {b} trade(s) have no client ID. Assign them to
          <select value={assignTo[b] || ""} onChange={e => setAssignTo({ ...assignTo, [b]: e.target.value })} style={inp}>
            <option value="">choose…</option>
            {accounts.filter(a => a.broker === b).map(a => <option key={a.client_id} value={a.client_id}>{a.client_id} · {a.name}</option>)}
          </select>
          <button style={small} disabled={!assignTo[b]} onClick={() => post("/accounts/assign", { broker: b, client_id: assignTo[b] })}>Assign</button>
        </div>
      ))}
      {err && <div style={{ fontSize: "12px", color: "var(--negative)", marginTop: "8px" }}>{err}</div>}
    </div>
  );
}


// ─── Mutual funds ───────────────────────────────────────────────────────────

const cardStyle = { background: "var(--surface-1)", border: "1px solid var(--border-default)", borderRadius: "12px", padding: "16px" };
const btnStyle = (on = false) => ({
  padding: "6px 12px", fontSize: "12px", fontWeight: 700 as const, borderRadius: "7px", cursor: "pointer",
  border: `1px solid ${on ? "rgba(var(--accent-rgb),0.45)" : "var(--border-default)"}`,
  background: on ? "var(--accent-bg)" : "transparent", color: on ? "var(--accent)" : "var(--text-secondary)",
});
const thStyle = (align: "left" | "right" = "right") => ({
  textAlign: align, padding: "6px 10px", fontSize: "10px", fontWeight: 600 as const, color: "var(--text-muted)",
  borderBottom: "1px solid var(--border-subtle)", whiteSpace: "nowrap" as const, letterSpacing: "0.3px",
});
const tdStyle = (align: "left" | "right" = "right") => ({
  textAlign: align, padding: "7px 10px", fontSize: "12px", borderBottom: "1px solid var(--border-subtle)", whiteSpace: "nowrap" as const,
});
const inputStyle = { fontSize: "12px", padding: "5px 8px", background: "var(--surface-1)", color: "var(--text-primary)",
                     border: "1px solid var(--border-default)", borderRadius: "7px" };

const fileB64 = (f: File) => new Promise<string>((resolve, reject) => {
  const r = new FileReader();
  r.onload = () => resolve(String(r.result).split(",")[1] || "");
  r.onerror = () => reject(r.error);
  r.readAsDataURL(f);
});

async function postJSON(path: string, body: object) {
  const res = await fetch(`${API_BASE}${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

// Where each fund house publishes its month-end portfolios.
const AMC_PAGES: [RegExp, string][] = [
  [/hdfc/i, "https://www.hdfcfund.com/statutory-disclosure/portfolio/monthly-portfolio"],
  [/motilal/i, "https://www.motilaloswalmf.com/download/month-end-portfolio"],
  [/tata/i, "https://www.tatamutualfund.com/schemes-related/portfolio"],
];
const amcPage = (amc: string | null) => AMC_PAGES.find(([re]) => re.test(amc || ""))?.[1] || null;
const classLabel = (c: string) => c === "equity" ? "Equity" : c === "debt" ? "Debt" : "Other";
const fmtDate = (d: string | null | undefined) => d ? new Date(d + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" }) : "—";
const daysSince = (d: string | null | undefined) => d ? Math.floor((Date.now() - new Date(d + "T00:00:00").getTime()) / 86400000) : null;

interface MfTxn { id: number; date: string; scheme: string; isin: string; folio: string; kind: string; description: string | null;
                  amount: number | null; units: number | null; nav: number | null; balance: number | null; name: string }

function FundsTab({ data, names, who, onReload, setMsg }: {
  data: MfResp | null; names: string[]; who: string; onReload: () => void;
  setMsg: (m: { text: string; tone: "ok" | "err" } | null) => void;
}) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState("");
  const [showTx, setShowTx] = useState(false);
  const [txns, setTxns] = useState<MfTxn[]>([]);
  const [forScheme, setForScheme] = useState<string | null>(null);
  const casRef = useRef<HTMLInputElement | null>(null);
  const pfRef = useRef<HTMLInputElement | null>(null);
  const rows = Array.isArray(data?.rows) ? data!.rows : [];
  const t = data?.totals;
  const many = names.length > 1;

  useEffect(() => {
    if (!showTx) return;
    getJSON<MfTxn[]>(`/api/portfolio/mf/transactions${who ? `?name=${encodeURIComponent(who)}` : ""}`).then(setTxns).catch(() => setTxns([]));
  }, [showTx, who, data]);

  const importCas = async (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    setBusy("cas"); setMsg(null);
    try {
      const r = await postJSON("/api/portfolio/mf/import", { filename: f.name, content_b64: await fileB64(f), password: password || null });
      setPassword("");
      setMsg({ tone: r.errors?.length ? "err" : "ok",
               text: `${f.name}: ${r.schemes} scheme(s), ${r.added} transaction line(s) added, ${r.duplicates} already on file`
                 + (r.period_from ? ` · statement ${fmtDate(r.period_from)} → ${fmtDate(r.period_to)}` : "")
                 + (r.errors?.length ? `. Check: ${r.errors.slice(0, 2).join("; ")}` : ". NAVs are being fetched from AMFI.") });
      onReload();
      setTimeout(onReload, 8000);
    } catch (err: any) {
      setMsg({ text: `Statement import failed: ${err.message}`, tone: "err" });
    } finally { setBusy(""); }
  };

  const uploadPortfolios = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    e.target.value = "";
    if (!files.length) return;
    const scheme = forScheme;
    setForScheme(null);
    setBusy("pf"); setMsg(null);
    const lines: string[] = [];
    let bad = false;
    for (const f of files) {
      try {
        const r = await postJSON("/api/portfolio/mf/portfolio-file", { filename: f.name, content_b64: await fileB64(f), scheme_isin: scheme });
        const m = (r.matched || []).map((x: any) => `${x.scheme.split(" - ")[0]} (${fmtDate(x.as_of)}${x.date_found ? "" : " — date assumed"}, ${x.stocks} stocks, ${x.equity_pct}% in shares)`);
        const um = (r.unmatched || []).map((x: any) => `${x.scheme} — best guess "${x.best}" was not close enough`);
        if (!m.length) bad = true;
        lines.push(`${f.name}: ${m.length ? m.join("; ") : "no scheme you hold matched"}${um.length ? `. ${um.join("; ")}` : ""}`);
      } catch (err: any) {
        bad = true;
        lines.push(`${f.name}: ${err.message}`);
      }
    }
    setMsg({ text: lines.join(" · "), tone: bad ? "err" : "ok" });
    setBusy("");
    onReload();
  };

  const refreshNavs = async () => {
    setBusy("nav"); setMsg(null);
    try {
      const r = await postJSON("/api/portfolio/mf/navs/refresh", {});
      setMsg({ text: `NAVs: ${r.updated} scheme(s) updated from AMFI${r.missing ? `, ${r.missing} not in AMFI's file` : ""}.`, tone: "ok" });
      onReload();
    } catch (err: any) { setMsg({ text: `NAV refresh failed: ${err.message}`, tone: "err" }); }
    finally { setBusy(""); }
  };

  const setClass = async (isin: string, cls: string) => {
    try { await postJSON("/api/portfolio/mf/tax-class", { isin, tax_class: cls || null }); onReload(); }
    catch (err: any) { setMsg({ text: err.message, tone: "err" }); }
  };

  const pickFor = (isin: string | null) => { setForScheme(isin); setTimeout(() => pfRef.current?.click(), 0); };
  const navNote = data?.nav?.error ? `AMFI unreachable (${data.nav.error.slice(0, 80)}) — values use the statement's NAV`
    : data?.nav?.running ? "Fetching NAVs from AMFI…"
    : data?.nav?.last_ok ? `NAVs from AMFI, checked ${new Date(data.nav.last_ok).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}` : "";
  const missingPf = rows.filter(r => !r.portfolio);
  const stalePf = rows.filter(r => r.portfolio && (daysSince(r.portfolio.as_of) ?? 0) > 50);

  return (
    <div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "8px", alignItems: "center", marginBottom: "10px" }}>
        <input type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="off"
          placeholder="Statement password, if any" title="Only used to open the PDF; never stored" style={{ ...inputStyle, width: "190px" }} />
        <button onClick={() => casRef.current?.click()} disabled={busy === "cas"} style={btnStyle()}
          title="CAMS or KFintech Consolidated Account Statement (detailed, with transactions), from the first investment to today. Re-importing adds only what is new.">
          {busy === "cas" ? "Reading statement…" : "Import CAS (PDF)"}
        </button>
        <input ref={casRef} type="file" accept=".pdf,application/pdf" onChange={importCas} style={{ display: "none" }} />
        <button onClick={() => pickFor(null)} disabled={busy === "pf"} style={btnStyle()}
          title="Each fund house's month-end portfolio file (Excel, PDF or zip). Several files at once are fine; each sheet is matched to the schemes you hold.">
          {busy === "pf" ? "Reading portfolios…" : "Upload fund portfolios"}
        </button>
        <input ref={pfRef} type="file" multiple accept=".xlsx,.xls,.xlsm,.pdf,.zip" onChange={uploadPortfolios} style={{ display: "none" }} />
        <button onClick={refreshNavs} disabled={busy === "nav"} style={btnStyle()} title="Latest NAVs from AMFI (also fetched every morning at 07:00)">
          {busy === "nav" ? "Fetching…" : "Refresh NAVs"}
        </button>
        <button onClick={() => setShowTx(v => !v)} style={btnStyle(showTx)}>Transactions</button>
        <span style={{ marginLeft: "auto", fontSize: "11px", color: data?.nav?.error ? "var(--warning)" : "var(--text-faint)" }}>{navNote}</span>
      </div>

      {rows.length === 0 && (
        <div style={{ ...cardStyle, marginBottom: "14px", fontSize: "13px", color: "var(--text-secondary)", lineHeight: 1.7 }}>
          <b style={{ color: "var(--text-primary)" }}>No mutual funds on file yet.</b><br />
          1. Ask CAMS for a <b>Consolidated Account Statement</b> (camsonline.com → Investors → Statements → CAS – CAMS+KFintech):
          statement type <b>Detailed</b>, period from your first investment to today. It arrives by email as a PDF covering every fund house.<br />
          2. <b>Import CAS (PDF)</b> here, with the password you set on the request form. Every folio and transaction is read; importing a newer statement later adds only what is new.<br />
          3. For the Exposure tab, add each fund's <b>month-end portfolio</b> from its fund house's website (Excel or PDF) with <b>Upload fund portfolios</b>. Once a month is enough.
        </div>
      )}

      {rows.length > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: "10px", marginBottom: "12px" }}>
          <Tile label="Invested" value={inr(t?.invested)} sub="cost of units held, stamp duty included" />
          <Tile label="Current value" value={inr(t?.value)} sub={t?.unpriced ? `${t.unpriced} without a NAV` : undefined} />
          <Tile label="Unrealised P&L" value={signed(t?.pnl)} color={col(t?.pnl)}
                sub={t?.pnl != null && t.priced_invested ? pct(t.pnl / t.priced_invested * 100) : undefined} />
          <Tile label="Day's change" value={signed(t?.day_pnl)} color={col(t?.day_pnl)} sub="latest NAV against the one before" />
          <Tile label="XIRR" value={t?.xirr == null ? "—" : `${t.xirr.toFixed(2)}%`} color={col(t?.xirr)} sub="every purchase and redemption, annualised" />
          <Tile label={`Realised FY ${t?.realised_fy || ""}`} value={signed(t?.realised_fy_gain)} color={col(t?.realised_fy_gain)} sub="redemptions, before tax" />
        </div>
      )}

      {!who && (data?.by_name?.length ?? 0) > 1 && (
        <div style={{ ...cardStyle, padding: 0, overflowX: "auto", marginBottom: "12px" }}>
          <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "640px" }}>
            <thead><tr>{["BY NAME", "INVESTED", "VALUE", "P&L", "DAY", "XIRR"].map((h, i) => <th key={h} style={thStyle(i ? "right" : "left")}>{h}</th>)}</tr></thead>
            <tbody>
              {data!.by_name.map(b => (
                <tr key={b.name}>
                  <td style={{ ...tdStyle("left"), fontWeight: 700 }}>{b.name}</td>
                  <td style={tdStyle()}>{inr(b.invested)}</td>
                  <td style={tdStyle()}>{inr(b.value)}</td>
                  <td style={{ ...tdStyle(), color: col(b.pnl), fontWeight: 700 }}>{signed(b.pnl)}</td>
                  <td style={{ ...tdStyle(), color: col(b.day_pnl) }}>{signed(b.day_pnl)}</td>
                  <td style={{ ...tdStyle(), color: col(b.xirr) }}>{b.xirr == null ? "—" : `${b.xirr.toFixed(2)}%`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {((data?.warnings?.length ?? 0) > 0 || missingPf.length > 0 || stalePf.length > 0) && rows.length > 0 && (
        <details style={{ ...cardStyle, padding: "10px 14px", marginBottom: "10px", borderColor: "var(--warning-border)", background: "var(--warning-bg)" }}>
          <summary style={{ cursor: "pointer", fontSize: "12px", fontWeight: 700, color: "var(--warning)" }}>
            {[missingPf.length ? `${missingPf.length} fund(s) without a portfolio file` : "",
              stalePf.length ? `${stalePf.length} portfolio(s) over 50 days old` : "",
              data?.warnings?.length ? `${data.warnings.length} statement note(s)` : ""].filter(Boolean).join(" · ")}
          </summary>
          {missingPf.map(r => (
            <div key={r.isin} style={{ fontSize: "12px", color: "var(--text-secondary)", marginTop: "4px" }}>
              • {r.scheme} — {amcPage(r.amc) ? <a href={amcPage(r.amc)!} target="_blank" rel="noreferrer">{r.amc} portfolios</a>
                : `${r.amc || "the fund house"} website → statutory disclosures → monthly portfolio`}, then
              <b style={{ cursor: "pointer", textDecoration: "underline", marginLeft: "4px" }} onClick={() => pickFor(r.isin)}>upload for this fund</b>
            </div>
          ))}
          {stalePf.map(r => (
            <div key={r.isin} style={{ fontSize: "12px", color: "var(--text-secondary)", marginTop: "4px" }}>
              • {r.scheme}: portfolio as of {fmtDate(r.portfolio!.as_of)} — a newer month is out.
            </div>
          ))}
          {(data?.warnings || []).map((w, i) => <div key={i} style={{ fontSize: "12px", color: "var(--text-secondary)", marginTop: "4px" }}>• {w}</div>)}
        </details>
      )}

      {rows.length > 0 && (
        <div style={{ ...cardStyle, padding: 0, overflowX: "auto" }}>
          <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "1200px" }}>
            <thead>
              <tr>
                <th style={thStyle("left")}>SCHEME</th>
                {many && <th style={thStyle("left")}>NAME</th>}
                <th style={thStyle("left")} title="How gains are taxed. Equity: 12 months, 20% / 12.5% with the shared ₹1.25 lakh exemption. Debt bought from Apr 2023: your slab. Other: 12.5% after 24 months.">TAX AS</th>
                <th style={thStyle()}>UNITS</th>
                <th style={thStyle()}>AVG NAV</th>
                <th style={thStyle()}>NAV</th>
                <th style={thStyle()}>DAY</th>
                <th style={thStyle()}>INVESTED</th>
                <th style={thStyle()}>VALUE</th>
                <th style={thStyle()}>P&L</th>
                <th style={thStyle()}>P&L %</th>
                <th style={thStyle()} title="Annualised return on this scheme's own cash flows">XIRR</th>
                <th style={thStyle()}>LONG-TERM</th>
                <th style={thStyle("left")}>HELD (UNITS · DAYS)</th>
                <th style={thStyle("left")} title="The fund's month-end portfolio, used by the Exposure tab">PORTFOLIO</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(r => {
                const age = daysSince(r.portfolio?.as_of);
                return (
                  <tr key={`${r.name}:${r.isin}`}>
                    <td style={{ ...tdStyle("left"), maxWidth: "300px", whiteSpace: "normal" }} title={`${r.isin} · folio ${r.folios.join(", ")}${r.category ? ` · ${r.category}` : ""}`}>
                      <div style={{ fontWeight: 700, color: "var(--text-primary)" }}>{r.scheme}</div>
                      <div style={{ fontSize: "10px", color: "var(--text-faint)" }}>{r.amc}{r.category ? ` · ${r.category}` : ""}</div>
                    </td>
                    {many && <td style={{ ...tdStyle("left"), color: "var(--text-secondary)" }} title={r.accounts.join(", ")}>{r.name}</td>}
                    <td style={tdStyle("left")}>
                      <select value={r.overridden ? r.tax_class : ""} onChange={e => setClass(r.isin, e.target.value)} title={r.tax_why || ""}
                        style={{ ...inputStyle, padding: "2px 4px", fontSize: "11px" }}>
                        <option value="">{classLabel(r.tax_class)} (auto)</option>
                        <option value="equity">Equity</option><option value="debt">Debt</option><option value="other">Other</option>
                      </select>
                    </td>
                    <td style={tdStyle()}>{r.units.toLocaleString("en-IN", { maximumFractionDigits: 3 })}</td>
                    <td style={tdStyle()}>{r.avg_nav ? inr(r.avg_nav, 4) : "—"}</td>
                    <td style={tdStyle()} title={`${fmtDate(r.nav_date)} · ${r.nav_source === "statement" ? "from the statement" : "AMFI"}`}>
                      {r.nav ? inr(r.nav, 4) : "—"}
                      <div style={{ fontSize: "10px", color: r.nav_source === "statement" ? "var(--warning)" : "var(--text-faint)" }}>{r.nav_date ? fmtDate(r.nav_date) : ""}</div>
                    </td>
                    <td style={{ ...tdStyle(), color: col(r.day_change_pct) }}>{pct(r.day_change_pct)}</td>
                    <td style={tdStyle()}>{inr(r.invested)}</td>
                    <td style={{ ...tdStyle(), fontWeight: 700 }}>{inr(r.value)}</td>
                    <td style={{ ...tdStyle(), color: col(r.pnl), fontWeight: 700 }}>{signed(r.pnl)}</td>
                    <td style={{ ...tdStyle(), color: col(r.pnl_pct) }}>{pct(r.pnl_pct)}</td>
                    <td style={{ ...tdStyle(), color: col(r.xirr) }}>{r.xirr == null ? "—" : `${r.xirr.toFixed(2)}%`}</td>
                    <td style={{ ...tdStyle(), color: "var(--text-secondary)" }}
                        title={r.next_long_term ? `More become long-term on ${r.next_long_term}` : r.tax_class === "debt" ? "Debt funds bought from Apr 2023 are taxed at slab however long they are held" : ""}>
                      {r.long_term_units >= r.units - 0.001 ? "all" : r.long_term_units > 0 ? `${qtyTxt(r.long_term_units)} of ${qtyTxt(r.units)}`
                        : r.next_long_term ? `from ${r.next_long_term}` : r.undated_units > 0 ? "older — date unknown" : "never (slab)"}
                    </td>
                    <td style={{ ...tdStyle("left"), color: "var(--text-secondary)", fontSize: "11px" }}
                        title={r.lots.map(l => `${l.qty} units bought ${l.date ?? "before the statement"} for ${inr(l.cost)}`).join("\n")}>
                      {r.lots.slice(0, 3).map(l => `${l.qty.toLocaleString("en-IN", { maximumFractionDigits: 1 })} · ${l.days == null ? "older" : `${l.days}d`}`).join(", ")}
                      {r.lots.length > 3 ? ` +${r.lots.length - 3}` : ""}
                    </td>
                    <td style={tdStyle("left")}>
                      {r.portfolio ? (
                        <span style={{ fontSize: "11px", color: (age ?? 0) > 50 ? "var(--warning)" : "var(--text-secondary)", cursor: "pointer" }}
                              title={`${r.portfolio.filename || ""} — click to replace`} onClick={() => pickFor(r.isin)}>
                          {fmtDate(r.portfolio.as_of)} · {r.portfolio.n} · {r.portfolio.equity_pct ?? "—"}% shares
                        </span>
                      ) : (
                        <button onClick={() => pickFor(r.isin)} style={{ ...btnStyle(), padding: "2px 8px", fontSize: "11px" }}
                          title={amcPage(r.amc) ? `Download from ${amcPage(r.amc)}` : "From the fund house's website: statutory disclosures → monthly portfolio"}>Upload</button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {showTx && (
        <div style={{ ...cardStyle, padding: 0, overflowX: "auto", marginTop: "12px" }}>
          <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "900px" }}>
            <thead><tr>{["DATE", "SCHEME", "NAME", "TYPE", "AMOUNT", "UNITS", "NAV", "BALANCE"].map((h, i) => <th key={h} style={thStyle(i < 4 ? "left" : "right")}>{h}</th>)}</tr></thead>
            <tbody>
              {txns.map(x => (
                <tr key={x.id}>
                  <td style={tdStyle("left")}>{x.date}</td>
                  <td style={{ ...tdStyle("left"), maxWidth: "320px", overflow: "hidden", textOverflow: "ellipsis" }} title={`${x.scheme} · folio ${x.folio}`}>{x.scheme}</td>
                  <td style={{ ...tdStyle("left"), color: "var(--text-secondary)" }}>{x.name}</td>
                  <td style={{ ...tdStyle("left"), fontWeight: 600, color: x.kind === "buy" ? "var(--positive)" : x.kind === "sell" ? "var(--negative)" : "var(--text-muted)" }}
                      title={x.description || ""}>{x.description || x.kind}</td>
                  <td style={tdStyle()}>{x.amount == null ? "—" : inr(Math.abs(x.amount), 2)}</td>
                  <td style={tdStyle()}>{x.units == null ? "" : x.units.toLocaleString("en-IN", { maximumFractionDigits: 3 })}</td>
                  <td style={tdStyle()}>{x.nav == null ? "" : x.nav.toFixed(4)}</td>
                  <td style={tdStyle()}>{x.balance == null ? "" : x.balance.toLocaleString("en-IN", { maximumFractionDigits: 3 })}</td>
                </tr>
              ))}
              {txns.length === 0 && <tr><td colSpan={8} style={{ ...tdStyle("left"), color: "var(--text-faint)", fontStyle: "italic" }}>No transactions.</td></tr>}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── All holdings: shares, ETFs and funds in one list ───────────────────────

const TYPE_STYLE: Record<string, { label: string; rgb: string; color: string }> = {
  stock: { label: "Stock", rgb: "--accent-rgb", color: "var(--accent)" },
  direct: { label: "Direct", rgb: "--accent-rgb", color: "var(--accent)" },
  etf: { label: "ETF", rgb: "--ai-rgb", color: "var(--ai)" },
  mf: { label: "Fund", rgb: "--positive-rgb", color: "var(--positive-strong)" },
};
function TypeChip({ t }: { t: string }) {
  const st = TYPE_STYLE[t] || { label: t, rgb: "--ink-rgb", color: "var(--text-secondary)" };
  return (
    <span style={{ fontSize: "10px", fontWeight: 700, padding: "1px 6px", borderRadius: "4px", whiteSpace: "nowrap",
                   background: `rgba(var(${st.rgb}),0.12)`, color: st.color }}>{st.label}</span>
  );
}
const atLabel = (a: string) => a === "upstox" ? "Upstox" : a === "zerodha" ? "Zerodha" : a === "mf" ? "CAS" : a;

function MixBar({ parts }: { parts: { label: string; value: number; pct: number | null; color: string }[] }) {
  const shown = parts.filter(p => (p.pct ?? 0) > 0);
  return (
    <div>
      <div style={{ display: "flex", height: "12px", borderRadius: "6px", overflow: "hidden", background: "rgba(var(--ink-rgb),0.06)" }}>
        {shown.map(p => <div key={p.label} title={`${p.label}: ${inr(p.value)} (${(p.pct ?? 0).toFixed(1)}%)`}
                             style={{ width: `${p.pct}%`, background: p.color }} />)}
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "14px", marginTop: "6px" }}>
        {shown.map(p => (
          <span key={p.label} style={{ fontSize: "11px", color: "var(--text-secondary)", display: "inline-flex", alignItems: "center", gap: "5px" }}>
            <span style={{ width: "9px", height: "9px", borderRadius: "2px", background: p.color, display: "inline-block" }} />
            {p.label} <b style={{ color: "var(--text-primary)" }}>{inr(p.value)}</b> · {(p.pct ?? 0).toFixed(1)}%
          </span>
        ))}
      </div>
    </div>
  );
}

function OverviewTab({ data, many, who, onPick, onTab }: {
  data: OverviewResp | null; many: boolean; who: string; onPick: (n: string) => void;
  onTab: (k: "holdings" | "funds" | "exposure") => void;
}) {
  const [type, setType] = useState<"" | "stock" | "etf" | "mf">("");
  const [sort, setSort] = useState<"value" | "pnl" | "pnl_pct" | "day" | "name">("value");
  const [q, setQ] = useState("");
  if (!data) return <div style={{ ...cardStyle, fontSize: "13px", color: "var(--text-muted)" }}>Loading holdings…</div>;
  const all = Array.isArray(data.rows) ? data.rows : [];
  const f = q.trim().toLowerCase();
  const rows = all.filter(r => (!type || r.type === type)
    && (!f || [r.symbol, r.label, r.sector, r.category, r.industry].some(x => (x || "").toLowerCase().includes(f))));
  const val = (r: OvRow) => r.value ?? r.invested;
  rows.sort((a, b) => sort === "name" ? (a.symbol || a.label || "").localeCompare(b.symbol || b.label || "")
    : sort === "pnl" ? (b.pnl ?? -Infinity) - (a.pnl ?? -Infinity)
    : sort === "pnl_pct" ? (b.pnl_pct ?? -Infinity) - (a.pnl_pct ?? -Infinity)
    : sort === "day" ? (b.day_change_pct ?? -Infinity) - (a.day_change_pct ?? -Infinity)
    : val(b) - val(a));
  const t = data.totals;
  const bt = data.by_type;
  const sortTh = (k: typeof sort, label: string, align: "left" | "right" = "right", title?: string) => (
    <th style={{ ...thStyle(align), cursor: "pointer", color: sort === k ? "var(--accent)" : undefined }} title={title} onClick={() => setSort(k)}>{label}</th>
  );
  return (
    <div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: "10px", marginBottom: "12px" }}>
        <Tile label="Current value" value={inr(t?.value)} sub={`invested ${inr(t?.invested)}${t?.unpriced ? ` · ${t.unpriced} at cost (no price)` : ""}`} />
        <Tile label="Unrealised P&L" value={signed(t?.pnl)} color={col(t?.pnl)}
              sub={t?.pnl != null && t.priced_invested ? pct(t.pnl / t.priced_invested * 100) : undefined} />
        <Tile label="Day's P&L" value={signed(t?.day_pnl)} color={col(t?.day_pnl)} sub="shares & ETFs live · funds at the latest NAV" />
        <Tile label={`Realised FY ${data.realised_fy || ""}`} value={signed((data.realised_fy_shares || 0) + (data.realised_fy_funds || 0))}
              color={col((data.realised_fy_shares || 0) + (data.realised_fy_funds || 0))}
              sub={`shares ${signed(data.realised_fy_shares)} · funds ${signed(data.realised_fy_funds)}`} />
      </div>

      <div style={{ ...cardStyle, padding: "12px 14px", marginBottom: "12px" }}>
        <div style={{ fontSize: "10px", fontWeight: 700, color: "var(--text-muted)", letterSpacing: "0.4px", marginBottom: "8px" }}>WHERE THE MONEY IS</div>
        <MixBar parts={[
          { label: "Stocks", value: bt?.stock?.value ?? 0, pct: bt?.stock?.pct ?? null, color: "var(--accent)" },
          { label: "ETFs", value: bt?.etf?.value ?? 0, pct: bt?.etf?.pct ?? null, color: "var(--ai)" },
          { label: "Mutual funds", value: bt?.mf?.value ?? 0, pct: bt?.mf?.pct ?? null, color: "var(--positive)" },
        ]} />
        <div style={{ fontSize: "11px", color: "var(--text-faint)", marginTop: "6px" }}>
          What the ETFs and funds themselves hold — sectors and stocks — is in <b style={{ cursor: "pointer", textDecoration: "underline" }} onClick={() => onTab("exposure")}>Analytics</b>.
        </div>
      </div>

      {!who && (data.by_name?.length ?? 0) > 1 && (
        <div style={{ ...cardStyle, padding: 0, overflowX: "auto", marginBottom: "12px" }}>
          <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "760px" }}>
            <thead><tr>{["BY NAME", "VALUE", "INVESTED", "P&L", "DAY", "STOCKS", "ETFS", "FUNDS"].map((h, i) => <th key={h} style={thStyle(i ? "right" : "left")}>{h}</th>)}</tr></thead>
            <tbody>
              {data.by_name.map(b => (
                <tr key={b.name} onClick={() => onPick(b.name)} style={{ cursor: "pointer" }} title={`Show only ${b.name}`}>
                  <td style={{ ...tdStyle("left"), fontWeight: 700 }}>{b.name}</td>
                  <td style={{ ...tdStyle(), fontWeight: 700 }}>{inr(b.value)}</td>
                  <td style={tdStyle()}>{inr(b.invested)}</td>
                  <td style={{ ...tdStyle(), color: col(b.pnl) }}>{signed(b.pnl)}</td>
                  <td style={{ ...tdStyle(), color: col(b.day_pnl) }}>{signed(b.day_pnl)}</td>
                  <td style={tdStyle()}>{inr(b.stock_value)}</td>
                  <td style={tdStyle()}>{inr(b.etf_value)}</td>
                  <td style={tdStyle()}>{inr(b.mf_value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div style={{ display: "flex", gap: "6px", alignItems: "center", marginBottom: "10px", flexWrap: "wrap" }}>
        {([["", "All", t?.count], ["stock", "Stocks", bt?.stock?.count], ["etf", "ETFs", bt?.etf?.count], ["mf", "Mutual funds", bt?.mf?.count]] as const).map(([k, l, n]) => (
          <button key={k || "all"} onClick={() => setType(k)} style={btnStyle(type === k)}>{l} ({n ?? 0})</button>
        ))}
        <input value={q} onChange={e => setQ(e.target.value)} placeholder="Filter name, sector, category" style={{ ...inputStyle, width: "210px" }} />
        <span style={{ marginLeft: "auto", fontSize: "11px", color: "var(--text-faint)" }}>
          Lots, tax dates and selling are in <b style={{ cursor: "pointer" }} onClick={() => onTab("holdings")}>Shares & ETFs</b> and <b style={{ cursor: "pointer" }} onClick={() => onTab("funds")}>Mutual funds</b>
        </span>
      </div>

      <div style={{ ...cardStyle, padding: 0, overflowX: "auto" }}>
        <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "1150px" }}>
          <thead>
            <tr>
              {sortTh("name", "HOLDING", "left")}
              <th style={thStyle("left")}>TYPE</th>
              {many && <th style={thStyle("left")}>NAME</th>}
              <th style={thStyle("left")}>AT</th>
              <th style={thStyle()}>QTY / UNITS</th>
              <th style={thStyle()}>AVG</th>
              <th style={thStyle()} title="Live price for shares and ETFs; the latest NAV (published once a day) for funds">PRICE / NAV</th>
              {sortTh("day", "DAY")}
              <th style={thStyle()}>INVESTED</th>
              {sortTh("value", "VALUE")}
              {sortTh("pnl", "P&L")}
              {sortTh("pnl_pct", "P&L %")}
              <th style={thStyle()} title="Share of everything you hold">WEIGHT</th>
              <th style={thStyle("left")}>SECTOR / CATEGORY</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={`${r.type}:${r.name}:${r.key}`}>
                <td style={{ ...tdStyle("left"), maxWidth: "300px", overflow: "hidden", textOverflow: "ellipsis" }} title={`${r.label || r.symbol || ""}${r.isin ? ` · ${r.isin}` : ""}`}>
                  <b style={{ color: "var(--text-primary)" }}>{r.type === "mf" ? r.label : r.symbol}</b>
                  {r.type === "etf" && r.label && r.label !== r.symbol && <div style={{ fontSize: "10px", color: "var(--text-faint)" }}>{r.label}</div>}
                  {r.type === "mf" && r.amc && <div style={{ fontSize: "10px", color: "var(--text-faint)" }}>{r.amc}</div>}
                </td>
                <td style={tdStyle("left")}><TypeChip t={r.type} /></td>
                {many && <td style={{ ...tdStyle("left"), color: "var(--text-secondary)" }}>{r.name}</td>}
                <td style={{ ...tdStyle("left"), fontSize: "11px", color: "var(--text-secondary)" }}>{r.at.map(atLabel).join(" + ")}</td>
                <td style={tdStyle()}>{r.type === "mf" ? r.qty.toLocaleString("en-IN", { maximumFractionDigits: 3 }) : qtyTxt(r.qty)}</td>
                <td style={tdStyle()}>{r.avg ? inr(r.avg, r.type === "mf" ? 4 : 2) : "—"}</td>
                <td style={tdStyle()} title={r.price_date ? `NAV of ${fmtDate(r.price_date)}` : ""}>
                  {r.price ? inr(r.price, r.type === "mf" ? 4 : 2) : "—"}
                  {r.type === "mf" && r.price_date && <div style={{ fontSize: "10px", color: "var(--text-faint)" }}>{fmtDate(r.price_date)}</div>}
                </td>
                <td style={{ ...tdStyle(), color: col(r.day_change_pct) }}>{pct(r.day_change_pct)}</td>
                <td style={tdStyle()}>{inr(r.invested)}</td>
                <td style={{ ...tdStyle(), fontWeight: 700 }}>{r.value == null ? <span title="No price yet — counted at cost">{inr(r.invested)}*</span> : inr(r.value)}</td>
                <td style={{ ...tdStyle(), color: col(r.pnl), fontWeight: 700 }}>{signed(r.pnl)}</td>
                <td style={{ ...tdStyle(), color: col(r.pnl_pct) }}>{pct(r.pnl_pct)}</td>
                <td style={tdStyle()}>{r.weight == null ? "—" : `${r.weight.toFixed(1)}%`}</td>
                <td style={{ ...tdStyle("left"), fontSize: "11px", color: "var(--text-secondary)", maxWidth: "220px", overflow: "hidden", textOverflow: "ellipsis" }}
                    title={r.industry || r.category || ""}>{r.sector || r.category || "—"}</td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={many ? 14 : 13} style={{ ...tdStyle("left"), color: "var(--text-faint)", fontStyle: "italic" }}>Nothing held.</td></tr>}
          </tbody>
        </table>
      </div>
      <div style={{ fontSize: "11px", color: "var(--text-faint)", marginTop: "6px", lineHeight: 1.6 }}>
        Shares and ETFs are merged across Upstox and Zerodha and priced live (refreshed every 30 s here); mutual funds come from your
        CAS and are valued at AMFI's latest NAV, published once each evening.
        {data.nav?.last_ok ? ` NAVs last checked ${new Date(data.nav.last_ok).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}.` : ""}
        {data.nav?.error ? ` AMFI could not be reached (${data.nav.error.slice(0, 80)}); funds use the statement's NAV.` : ""}
      </div>
    </div>
  );
}

// ─── Analytics: sectors → industries → stocks → where the money comes from ──

const ALLOC_COLOR: Record<string, string> = {
  equity: "var(--accent)", foreign: "var(--info)", debt: "var(--neutral)", gold: "var(--warning)", unknown: "rgba(var(--ink-rgb),0.22)",
};

function AnalyticsTab({ data, loading, onRefresh, setMsg }: {
  data: ExposureResp | null; loading: boolean; onRefresh: () => void;
  setMsg: (m: { text: string; tone: "ok" | "err" } | null) => void;
}) {
  const [view, setView] = useState<"sector" | "stock">("sector");
  const [filter, setFilter] = useState("");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [target, setTarget] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);
  if (!data) return <div style={{ ...cardStyle, fontSize: "13px", color: "var(--text-muted)" }}>{loading ? "Looking through your funds and ETFs…" : "No data yet."}</div>;

  const toggle = (k: string) => setOpen(o => { const n = new Set(o); if (n.has(k)) n.delete(k); else n.add(k); return n; });
  const f = filter.trim().toLowerCase();
  const isOpen = (k: string) => open.has(k) || !!f;
  const hit = (r: ExpStock) => !f || [r.symbol, r.company, r.industry, r.sector].some(x => (x || "").toLowerCase().includes(f));
  const equityTotal = (data.sectors || []).reduce((a, s) => a + s.total, 0);
  const maxSec = Math.max(1, ...(data.sectors || []).map(s => s.pct || 0), ...(data.buckets || []).map(b => b.pct || 0));
  const bar = (p: number | null, max: number) => (
    <div style={{ display: "flex", alignItems: "center", gap: "6px", justifyContent: "flex-end" }}>
      <div style={{ width: "80px", height: "6px", background: "rgba(var(--ink-rgb),0.08)", borderRadius: "3px", overflow: "hidden" }}>
        <div style={{ width: `${Math.min(100, (p || 0) / max * 100)}%`, height: "100%", background: "var(--accent)" }} />
      </div>
      <span style={{ minWidth: "46px" }}>{p == null ? "—" : `${p.toFixed(2)}%`}</span>
    </div>
  );
  const money = (v: number) => v ? inr(v) : <span style={{ color: "var(--text-faint)" }}>—</span>;
  const needFile = (data.funds || []).filter(x => x.needs_file);
  const stale = (data.funds || []).filter(x => x.stale);

  const pickFor = (isin: string) => { setTarget(isin); setTimeout(() => fileRef.current?.click(), 0); };
  const upload = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    e.target.value = "";
    if (!files.length || !target) return;
    setBusy(true); setMsg(null);
    const out: string[] = [];
    let bad = false;
    for (const file of files) {
      try {
        const r = await postJSON("/api/portfolio/mf/portfolio-file", { filename: file.name, content_b64: await fileB64(file), scheme_isin: target });
        const m = r.matched?.[0];
        if (!m) { bad = true; out.push(`${file.name}: no matching section`); }
        else out.push(`${file.name}: ${m.stocks} stocks, ${m.equity_pct}% in shares, as of ${fmtDate(m.as_of)}`);
      } catch (err: any) { bad = true; out.push(`${file.name}: ${err.message}`); }
    }
    setMsg({ text: out.join(" · "), tone: bad ? "err" : "ok" });
    setBusy(false); setTarget(null);
    onRefresh();
  };

  const sourceLines = (sources: ExpSource[], depth: number, withUpload = false) => (
    <div style={{ padding: `4px 10px 8px ${18 + depth * 18}px` }}>
      {sources.map((s, i) => (
        <div key={i} style={{ fontSize: "12px", color: "var(--text-secondary)", padding: "2px 0", display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" }}>
          <TypeChip t={s.type} />
          <span>{s.label}</span>
          {s.pct_of_fund != null && s.type !== "direct" && <span style={{ color: "var(--text-faint)" }}>{s.pct_of_fund.toFixed(2)}% of it</span>}
          <b style={{ color: "var(--text-primary)" }}>{inr(s.amount)}</b>
          {s.as_of && <span style={{ color: "var(--text-faint)", fontSize: "11px" }}>portfolio {fmtDate(s.as_of)}</span>}
          {s.name && (data.names?.length ?? 0) > 1 && <span style={{ color: "var(--text-faint)", fontSize: "11px" }}>{s.name}</span>}
          {withUpload && s.isin && <button onClick={() => pickFor(s.isin!)} disabled={busy} style={{ ...btnStyle(), padding: "1px 8px", fontSize: "11px" }}>Upload portfolio</button>}
        </div>
      ))}
    </div>
  );
  const cells = (g: { direct_value: number; etf_value: number; mf_value: number; total: number; pct: number | null }, bold = false) => (
    <>
      <td style={tdStyle()}>{money(g.direct_value)}</td>
      <td style={tdStyle()}>{money(g.etf_value)}</td>
      <td style={tdStyle()}>{money(g.mf_value)}</td>
      <td style={{ ...tdStyle(), fontWeight: bold ? 800 : 700 }}>{inr(g.total)}</td>
      <td style={tdStyle()}>{bar(g.pct, maxSec)}</td>
      <td style={{ ...tdStyle(), color: "var(--text-muted)" }}>{equityTotal ? `${(g.total / equityTotal * 100).toFixed(1)}%` : "—"}</td>
    </>
  );
  const caret = (k: string) => <span style={{ color: "var(--text-faint)", marginRight: "5px", fontWeight: 400 }}>{isOpen(k) ? "▾" : "▸"}</span>;
  const COLS = 8;

  return (
    <div>
      <input ref={fileRef} type="file" multiple accept=".xlsx,.xls,.xlsm,.pdf,.zip" onChange={upload} style={{ display: "none" }} />
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: "10px", marginBottom: "12px" }}>
        <Tile label="Total portfolio" value={inr(data.total)} sub="shares, ETFs and funds, today's value" />
        <Tile label="Shares held directly" value={inr(data.by_source?.direct?.value)} sub={data.by_source?.direct?.pct != null ? `${data.by_source.direct.pct.toFixed(1)}%` : undefined} />
        <Tile label="In ETFs" value={inr(data.by_source?.etf?.value)} sub={data.by_source?.etf?.pct != null ? `${data.by_source.etf.pct.toFixed(1)}%` : undefined} />
        <Tile label="In mutual funds" value={inr(data.by_source?.mf?.value)} sub={data.by_source?.mf?.pct != null ? `${data.by_source.mf.pct.toFixed(1)}%` : undefined} />
        <Tile label="Top 10 stocks" value={data.concentration?.top10_pct_of_equity == null ? "—" : `${data.concentration.top10_pct_of_equity.toFixed(1)}%`}
              sub={`of the money in shares · ${data.concentration?.stocks ?? 0} stocks in all`} />
        <Tile label="Owned more than one way" value={String(data.concentration?.in_several ?? 0)} sub="stocks you hold directly and/or through several funds" />
      </div>

      <div style={{ ...cardStyle, padding: "12px 14px", marginBottom: "12px" }}>
        <div style={{ fontSize: "10px", fontWeight: 700, color: "var(--text-muted)", letterSpacing: "0.4px", marginBottom: "8px" }}>WHAT IT IS INVESTED IN</div>
        <MixBar parts={(data.allocation || []).map(a => ({ label: a.label, value: a.value, pct: a.pct, color: ALLOC_COLOR[a.key] || "var(--text-faint)" }))} />
      </div>

      {(needFile.length > 0 || stale.length > 0) && (
        <div style={{ marginBottom: "10px", padding: "8px 12px", borderRadius: "8px", fontSize: "12px", background: "var(--warning-bg)", color: "var(--warning)", lineHeight: 1.8 }}>
          {needFile.length > 0 && <div>
            {needFile.length} fund(s)/ETF(s) worth {inr(needFile.reduce((a, x) => a + x.value, 0))} need their month-end portfolio before their stocks count here:{" "}
            {needFile.map(x => (
              <span key={x.isin} style={{ marginRight: "10px", whiteSpace: "nowrap" }}>
                <TypeChip t={x.type} /> {x.label.split(" - ")[0]}{" "}
                {amcPage(x.amc) && <a href={amcPage(x.amc)!} target="_blank" rel="noreferrer">download</a>}{" "}
                <b style={{ cursor: "pointer", textDecoration: "underline" }} onClick={() => pickFor(x.isin)}>upload</b>
              </span>
            ))}
          </div>}
          {stale.length > 0 && <div>Portfolio files over 50 days old: {stale.map(x => `${x.label.split(" - ")[0]} (${fmtDate(x.as_of)})`).join(", ")}.</div>}
        </div>
      )}

      <div style={{ display: "flex", gap: "6px", alignItems: "center", marginBottom: "10px", flexWrap: "wrap" }}>
        <button onClick={() => setView("sector")} style={btnStyle(view === "sector")}>By sector ({data.sectors?.length ?? 0})</button>
        <button onClick={() => setView("stock")} style={btnStyle(view === "stock")}>By stock ({data.rows?.length ?? 0})</button>
        <input value={filter} onChange={e => setFilter(e.target.value)} placeholder="Filter stock, industry or sector" style={{ ...inputStyle, width: "220px" }} />
        {view === "sector" && <button onClick={() => setOpen(o => o.size ? new Set() : new Set((data.sectors || []).map(s => `s:${s.sector}`)))} style={btnStyle()}>
          {open.size ? "Collapse all" : "Expand sectors"}</button>}
        <button onClick={onRefresh} disabled={loading} style={{ ...btnStyle(), marginLeft: "auto" }}>{loading ? "Refreshing…" : "Refresh"}</button>
      </div>

      {view === "sector" && (
        <div style={{ ...cardStyle, padding: 0, overflowX: "auto" }}>
          <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "980px" }}>
            <thead>
              <tr>
                <th style={thStyle("left")}>SECTOR → INDUSTRY → STOCK</th>
                <th style={thStyle()}>DIRECT</th>
                <th style={thStyle()}>VIA ETFS</th>
                <th style={thStyle()}>VIA FUNDS</th>
                <th style={thStyle()}>TOTAL</th>
                <th style={thStyle()}>% OF PORTFOLIO</th>
                <th style={thStyle()} title="Share of the money that is in shares">% OF EQUITY</th>
                <th style={thStyle()}>STOCKS</th>
              </tr>
            </thead>
            <tbody>
              {(data.sectors || []).map(s => {
                const inds = s.industries.map(i => ({ ...i, rows: i.rows.filter(hit) })).filter(i => i.rows.length || (f && i.industry.toLowerCase().includes(f)));
                if (f && !inds.length && !s.sector.toLowerCase().includes(f)) return null;
                const sk = `s:${s.sector}`;
                return (
                  <Fragment key={sk}>
                    <tr onClick={() => toggle(sk)} style={{ cursor: "pointer", background: "rgba(var(--ink-rgb),0.02)" }}>
                      <td style={{ ...tdStyle("left"), fontWeight: 800, color: "var(--text-primary)" }}>{caret(sk)}{s.sector}</td>
                      {cells(s, true)}
                      <td style={{ ...tdStyle(), color: "var(--text-muted)" }}>{s.stocks}</td>
                    </tr>
                    {isOpen(sk) && inds.map(i => {
                      const ik = `i:${s.sector}:${i.industry}`;
                      return (
                        <Fragment key={ik}>
                          <tr onClick={() => toggle(ik)} style={{ cursor: "pointer" }}>
                            <td style={{ ...tdStyle("left"), paddingLeft: "30px", fontWeight: 600, color: "var(--text-primary)" }}>{caret(ik)}{i.industry}</td>
                            {cells(i)}
                            <td style={{ ...tdStyle(), color: "var(--text-muted)" }}>{i.stocks}</td>
                          </tr>
                          {isOpen(ik) && i.rows.map(r => {
                            const rk = `r:${r.key}`;
                            return (
                              <Fragment key={rk}>
                                <tr onClick={() => toggle(rk)} style={{ cursor: "pointer" }}>
                                  <td style={{ ...tdStyle("left"), paddingLeft: "52px", maxWidth: "340px", overflow: "hidden", textOverflow: "ellipsis" }} title={r.isin || ""}>
                                    {caret(rk)}<b>{r.symbol || r.company}</b>
                                    {r.symbol && <span style={{ fontSize: "10px", color: "var(--text-faint)", marginLeft: "6px" }}>{r.company}</span>}
                                  </td>
                                  {cells(r)}
                                  <td style={tdStyle()}>{Array.from(new Set(r.sources.map(x => x.type))).map(t => <span key={t} style={{ marginLeft: "3px" }}><TypeChip t={t} /></span>)}</td>
                                </tr>
                                {isOpen(rk) && !f && (
                                  <tr><td colSpan={COLS} style={{ padding: 0, background: "var(--surface-2)", borderBottom: "1px solid var(--border-subtle)" }}>
                                    {sourceLines(r.sources, 3)}
                                  </td></tr>
                                )}
                              </Fragment>
                            );
                          })}
                        </Fragment>
                      );
                    })}
                  </Fragment>
                );
              })}
              {!f && (data.buckets || []).map(b => {
                const bk = `b:${b.key}`;
                return (
                  <Fragment key={bk}>
                    <tr onClick={() => toggle(bk)} style={{ cursor: "pointer" }}>
                      <td style={{ ...tdStyle("left"), fontWeight: 700, color: b.key === "unknown" ? "var(--warning)" : "var(--text-muted)" }}>{caret(bk)}{b.label}</td>
                      <td style={tdStyle()}>{money(0)}</td>
                      <td style={tdStyle()}>{money(b.sources.filter(x => x.type === "etf").reduce((a, x) => a + x.amount, 0))}</td>
                      <td style={tdStyle()}>{money(b.sources.filter(x => x.type === "mf").reduce((a, x) => a + x.amount, 0))}</td>
                      <td style={{ ...tdStyle(), fontWeight: 700 }}>{inr(b.value)}</td>
                      <td style={tdStyle()}>{bar(b.pct, maxSec)}</td>
                      <td style={tdStyle()}>—</td>
                      <td style={{ ...tdStyle(), color: "var(--text-muted)" }}>{b.sources.length} fund(s)</td>
                    </tr>
                    {isOpen(bk) && (
                      <tr><td colSpan={COLS} style={{ padding: 0, background: "var(--surface-2)", borderBottom: "1px solid var(--border-subtle)" }}>
                        {sourceLines(b.sources, 1, b.key === "unknown")}
                      </td></tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {view === "stock" && (
        <div style={{ ...cardStyle, padding: 0, overflowX: "auto" }}>
          <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "1000px" }}>
            <thead>
              <tr>
                <th style={thStyle("left")}>STOCK</th>
                <th style={thStyle("left")}>SECTOR · INDUSTRY</th>
                <th style={thStyle()}>DIRECT</th>
                <th style={thStyle()}>VIA ETFS</th>
                <th style={thStyle()}>VIA FUNDS</th>
                <th style={thStyle()}>TOTAL</th>
                <th style={thStyle()}>% OF PORTFOLIO</th>
                <th style={thStyle()} title="Shares at what you paid; via funds, your cost in the fund times the stock's weight (approximate)">INVESTED ≈</th>
              </tr>
            </thead>
            <tbody>
              {(data.rows || []).filter(hit).map(r => {
                const rk = `x:${r.key}`;
                return (
                  <Fragment key={rk}>
                    <tr onClick={() => toggle(rk)} style={{ cursor: "pointer" }}>
                      <td style={{ ...tdStyle("left"), maxWidth: "280px", overflow: "hidden", textOverflow: "ellipsis" }} title={r.isin || ""}>
                        {caret(rk)}<b style={{ color: "var(--text-primary)" }}>{r.symbol || r.company}</b>
                        {r.symbol && <span style={{ fontSize: "10px", color: "var(--text-faint)", marginLeft: "6px" }}>{r.company}</span>}
                      </td>
                      <td style={{ ...tdStyle("left"), color: "var(--text-secondary)", maxWidth: "240px", overflow: "hidden", textOverflow: "ellipsis" }}
                          title={r.industry}>{r.sector} · {r.industry}</td>
                      <td style={tdStyle()}>{money(r.direct_value)}</td>
                      <td style={tdStyle()}>{money(r.etf_value)}</td>
                      <td style={tdStyle()}>{money(r.mf_value)}</td>
                      <td style={{ ...tdStyle(), fontWeight: 700 }}>{inr(r.total)}</td>
                      <td style={tdStyle()}>{bar(r.pct, Math.max(1, ...(data.rows || []).map(x => x.pct || 0)))}</td>
                      <td style={{ ...tdStyle(), color: "var(--text-muted)" }}>{inr(r.invested)}</td>
                    </tr>
                    {open.has(rk) && (
                      <tr><td colSpan={8} style={{ padding: 0, background: "var(--surface-2)", borderBottom: "1px solid var(--border-subtle)" }}>
                        {sourceLines(r.sources, 1)}
                      </td></tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <div style={{ marginTop: "8px" }}>
        {(data.notes || []).map((n, i) => <div key={i} style={{ fontSize: "11px", color: "var(--text-faint)", lineHeight: 1.6 }}>{n}</div>)}
      </div>
    </div>
  );
}
