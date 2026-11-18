import { Fragment, useEffect, useRef, useState } from "react";
import type { ChangeEvent } from "react";

const API_BASE = import.meta.env.VITE_API_BASE || (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1" ? "http://localhost:8000" : "");

/**
 * Upstox and Zerodha trades in one ledger (backend services/portfolio.py):
 * holdings across both brokers, realised profit, and a capital-gains
 * summary per financial year. Upstox trades come from its API (synced daily
 * at 16:10 and on demand); Zerodha's from the tradebook CSV uploaded here.
 */

type BrokerFilter = "" | "upstox" | "zerodha";

interface Account { client_id: string; broker: string; name: string; trades: number }
interface TaxRow {
  broker: string; symbol: string | null; isin: string | null; buy_date: string; sell_date: string; qty: number;
  buy_price: number; sell_price: number; gain: number; term: string; grandfathered?: boolean; cost: number; sale_value: number;
  charges?: number; stt?: number; tax_gain?: number; asset?: string; name?: string;
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
  flows?: Record<"stock" | "etf" | "mf" | "fno", { bought: number; sold: number; count: number }>;
  per_name?: { name: string; stcg: number; ltcg: number; exemption: number; exemption_used: number; taxable_ltcg: number;
               estimated_tax: number; intraday: number; other_funds: number; fno: number | null; fno_turnover: number | null }[];
  itr?: ItrView;
  fno?: { gross: number; net: number; turnover: number; closed: number; futures: number; options: number;
          charges: { total: number; stt: number }; by_underlying: { underlying: string; net: number }[]; warnings: string[] } | null;
}
interface ItrCat {
  key: string; head: string; label: string; section: string; act: string; rate: string; where: string;
  periods: number[]; net: number; sale_value: number; cost: number; expenses: number; stt: number; turnover: number;
  count: number; gains: number; losses: number; gross?: number; rate_split?: { before: number; after: number };
}
interface ItrView {
  fy: string; act: string; assessment_year: string | null; form: string; checklist: string[];
  periods: { key: string; label: string; pay_by: string; cum_pct: number }[];
  categories: ItrCat[];
  schedule_112a: { isin: string | null; name: string; acquired: string; units: number; sale_value: number; cost: number;
                   expenses: number; gain: number; kind: string }[];
  advance_tax: { label: string; pay_by: string; special_rate_tax: number; cumulative: number; slab_income: number }[];
}
interface FnoTxn {
  id: number; broker: string; symbol: string; underlying: string | null; instrument: string | null; expiry: string | null;
  strike: number | null; exchange: string | null; trade_date: string; trade_time: string | null; side: string;
  quantity: number; price: number; source: string; account: string | null; name: string;
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
interface OvLot { date: string | null; qty: number; days: number | null; long_term: boolean | null; cost?: number; broker?: string }
interface OvPart { broker: string; account: string | null; name: string; qty: number; avg: number; invested: number; value: number | null }
interface OvFno { side: string; avg: number; expiry: string | null; expiry_estimated: boolean; underlying: string | null;
                 instrument: string | null; strike: number | null; opened: string; mtm: number | null; notional: number; priced: boolean }
interface OvRow {
  type: "stock" | "etf" | "mf" | "fno"; key: string; isin: string | null; symbol: string | null; label: string | null; name: string;
  at: string[]; qty: number; avg: number | null; invested: number; value: number | null; pnl: number | null; pnl_pct: number | null;
  day_pnl: number | null; day_change_pct: number | null; price: number | null; price_date: string | null; xirr: number | null;
  weight: number | null; industry: string | null; sector: string | null; category: string | null; amc?: string | null;
  lots?: OvLot[]; parts?: OvPart[]; long_term_qty?: number; next_long_term?: string | null; undated_qty?: number | null;
  folios?: string[]; tax_class?: string | null; tax_why?: string | null; overridden?: boolean; nav_source?: string | null;
  portfolio?: { as_of: string | null; equity_pct: number | null; n: number } | null;
  fno?: OvFno;
  first_bought?: string | null; days_held?: number | null; avg_days?: number | null; undated?: boolean;
}
interface OvTotals { count: number; invested: number; value: number; pnl: number | null; priced_invested: number;
                     day_pnl: number | null; unpriced: number; pct: number | null }
interface OverviewResp {
  rows: OvRow[]; totals: OvTotals; by_type: Record<"stock" | "etf" | "mf" | "fno", OvTotals>;
  by_name: (OvTotals & { name: string; stock_value: number; etf_value: number; mf_value: number; fno_value: number })[];
  realised_fy: string; realised_fy_shares: number; realised_fy_funds: number;
  nav: { last_ok: string | null; error: string | null; running: boolean } | null; warnings: string[];
}

const inr = (v: number | null | undefined, digits = 0) =>
  v == null || isNaN(v) ? "—" : `${v < 0 ? "−" : ""}₹${Math.abs(v).toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
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
  const [tab, setTab] = useState<"overview" | "exposure" | "fno" | "tax" | "trades">("overview");
  const [ov, setOv] = useState<OverviewResp | null>(null);
  const [exp, setExp] = useState<ExposureResp | null>(null);
  const [expLoading, setExpLoading] = useState(false);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [fy, setFy] = useState<string>("");
  const [tax, setTax] = useState<TaxResp | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [mfTxns, setMfTxns] = useState<MfTxn[]>([]);
  const [fnoTxns, setFnoTxns] = useState<FnoTxn[]>([]);
  const [tradeKind, setTradeKind] = useState<"shares" | "mf" | "fno">("shares");
  const [tradeFilter, setTradeFilter] = useState("");
  const [busy, setBusy] = useState<string>("");
  const [msg, setMsg] = useState<{ text: string; tone: "ok" | "err" } | null>(null);
  const [importBroker, setImportBroker] = useState<"zerodha" | "upstox">("zerodha");
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [confirmDel, setConfirmDel] = useState<string | null>(null);
  // Tracking name (several client IDs can share one). "" = everyone.
  const [who, setWho] = useState<string>("");
  // Tax is per person, so the tax tab always shows one name.
  const [taxWho, setTaxWho] = useState<string>("");
  const [importAccount, setImportAccount] = useState<string>("");
  const [showAccounts, setShowAccounts] = useState(false);

  const names = Array.isArray(summary?.names) ? summary!.names : [];
  const accounts = Array.isArray(summary?.accounts) ? summary!.accounts : [];
  const taxName = taxWho;
  const q = [broker ? `broker=${broker}` : "", who ? `name=${encodeURIComponent(who)}` : ""].filter(Boolean).join("&");

  const loadSummary = async () => {
    try {
      const s = await getJSON<Summary>("/api/portfolio/summary");
      setSummary(s);
      setFy(f => f || s.current_fy);
    } catch (e: any) { setMsg({ text: `Could not load the portfolio: ${e.message}`, tone: "err" }); }
  };
  const loadTax = async () => {
    if (!fy) return;
    try {
      setTax(await getJSON<TaxResp>(`/api/portfolio/tax?fy=${fy}${broker ? `&broker=${broker}` : ""}${taxName ? `&name=${encodeURIComponent(taxName)}` : ""}`));
    } catch { /* ignore */ }
  };
  const loadTrades = async () => {
    const sym = tradeFilter.trim() ? `&symbol=${encodeURIComponent(tradeFilter.trim())}` : "";
    try {
      if (tradeKind === "shares") setTrades(await getJSON<Trade[]>(`/api/portfolio/trades?limit=1000&${q}${sym}`));
      else if (tradeKind === "fno") setFnoTxns(await getJSON<FnoTxn[]>(`/api/portfolio/fno/trades?limit=2000&${q}${sym}`));
      else setMfTxns(await getJSON<MfTxn[]>(`/api/portfolio/mf/transactions${who ? `?name=${encodeURIComponent(who)}` : ""}`));
    } catch { /* ignore */ }
  };
  const loadExp = async () => {
    setExpLoading(true);
    try { setExp(await getJSON<ExposureResp>(`/api/portfolio/exposure${who ? `?name=${encodeURIComponent(who)}` : ""}`)); }
    catch (e: any) { setMsg({ text: `Could not build the analytics: ${e.message}`, tone: "err" }); }
    finally { setExpLoading(false); }
  };
  const loadOv = async () => {
    try { setOv(await getJSON<OverviewResp>(`/api/portfolio/overview?${q}`)); } catch { /* ignore */ }
  };
  const reloadAll = () => {
    loadSummary(); loadTax();
    if (tab === "overview") loadOv();
    if (tab === "trades") loadTrades();
    if (tab === "exposure") loadExp();
  };

  useEffect(() => { loadSummary(); }, []);
  useEffect(() => { loadTax(); }, [fy, broker, taxName]);
  useEffect(() => { if (tab === "trades") loadTrades(); }, [tab, broker, who, tradeKind]);
  useEffect(() => { if (tab === "exposure") loadExp(); }, [tab, who]);
  // Share and ETF prices move during the session; funds' NAV once a day.
  useEffect(() => {
    if (tab !== "overview") return;
    loadOv();
    const t = setInterval(loadOv, 30000);
    return () => clearInterval(t);
  }, [tab, who, broker]);

  const syncUpstox = async () => {
    setBusy("sync"); setMsg(null);
    try {
      const res = await fetch(`${API_BASE}/api/portfolio/sync/upstox`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.status);
      setMsg({ text: `Upstox: ${data.added} new share/ETF trade(s) of ${data.fetched} read`
                 + (data.fno_fetched ? `, ${data.fno_added} new F&O trade(s) of ${data.fno_fetched}` : "")
                 + `${data.errors?.length ? ` — ${data.errors.join("; ")}` : ""}.`,
               tone: data.errors?.length ? "err" : "ok" });
      reloadAll();
    } catch (e: any) {
      setMsg({ text: `Upstox sync failed: ${e.message}`, tone: "err" });
    } finally { setBusy(""); }
  };

  const onFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    e.target.value = "";
    if (!files.length) return;
    setBusy("import"); setMsg(null);
    const out: string[] = [];
    let bad = false;
    for (const f of files) {
      try {
        const content = await f.text();
        const res = await fetch(`${API_BASE}/api/portfolio/import`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ broker: importBroker, filename: f.name, content, account_id: importAccount || null }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || res.status);
        if (data.errors?.length) bad = true;
        out.push(`${f.name} (${data.account_id}): `
          + (data.read ? `${data.added} share/ETF trade(s) added, ${data.duplicates} already on file` : "")
          + (data.fno_read ? `${data.read ? "; " : ""}${data.fno_added} F&O trade(s) added, ${data.fno_duplicates} already on file` : "")
          + (!data.read && !data.fno_read ? "no trades read" : "")
          + (data.skipped_non_equity ? `, ${data.skipped_non_equity} commodity/currency skipped` : "")
          + (data.errors?.length ? `. ${data.errors.length} line(s) unreadable: ${data.errors.slice(0, 2).join("; ")}` : ""));
      } catch (err: any) {
        bad = true;
        out.push(`${f.name}: ${err.message}`);
      }
    }
    setMsg({ text: out.join(" · "), tone: bad ? "err" : "ok" });
    setBusy("");
    reloadAll();
  };

  const delTrade = async (kind: "shares" | "fno", id: number) => {
    const k = `${kind}:${id}`;
    if (confirmDel !== k) { setConfirmDel(k); setTimeout(() => setConfirmDel(c => c === k ? null : c), 3000); return; }
    setConfirmDel(null);
    await fetch(`${API_BASE}/api/portfolio/${kind === "fno" ? "fno/trades" : "trades"}/${id}`, { method: "DELETE" });
    reloadAll(); loadTrades();
  };

  const empty = summary && Object.keys(summary.counts || {}).length === 0 && !(ov?.rows?.length);
  const lastSync = summary?.upstox_sync?.at ? new Date(summary.upstox_sync.at) : null;

  const card = cardStyle;
  const btn = btnStyle;
  const th = thStyle;
  const td = tdStyle;
  const delBtn = (kind: "shares" | "fno", id: number) => {
    const on = confirmDel === `${kind}:${id}`;
    return (
      <button onClick={() => delTrade(kind, id)}
        style={{ fontSize: "10px", padding: "2px 6px", borderRadius: "5px", cursor: "pointer",
                 border: `1px solid ${on ? "var(--negative)" : "var(--border-default)"}`,
                 background: on ? "var(--negative-bg)" : "transparent", color: on ? "var(--negative)" : "var(--text-muted)" }}>
        {on ? "Delete?" : "✕"}
      </button>
    );
  };

  return (
    <div style={{ padding: "20px", maxWidth: "1400px", margin: "0 auto" }}>
      {/* Header: filter, sync and import */}
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "10px", marginBottom: "14px" }}>
        <h2 style={{ margin: 0, fontSize: "18px", fontWeight: 800, color: "var(--text-primary)" }}>Portfolio</h2>
        <div style={{ display: "inline-flex", gap: "4px" }} title="Broker: shares, ETFs and F&O. Mutual funds show under All.">
          {([["", "All"], ["upstox", "Upstox"], ["zerodha", "Zerodha"]] as [BrokerFilter, string][]).map(([b, label]) => (
            <button key={b || "all"} onClick={() => setBroker(b)} style={btn(broker === b)}>{label}</button>
          ))}
        </div>
        {names.length > 0 && (
          <div style={{ display: "inline-flex", gap: "4px", flexWrap: "wrap" }} title="Tracking name — set per client ID under Accounts">
            {["", ...names].map(n => (
              <button key={n || "everyone"} onClick={() => { setWho(n); setTaxWho(n); }}
                style={{ ...btn(who === n), borderStyle: who === n ? "solid" : "dashed" }}>{n || "Everyone"}</button>
            ))}
          </div>
        )}
        <div style={{ marginLeft: "auto", display: "flex", flexWrap: "wrap", gap: "8px", alignItems: "center" }}>
          <button onClick={syncUpstox} disabled={busy === "sync"} style={btn()}
            title="Share, ETF and F&O trades from Upstox's trade history (last three financial years) and today's trades. Also runs at 16:10 on weekdays.">
            {busy === "sync" ? "Syncing Upstox…" : "Sync Upstox"}
          </button>
          <select value={importBroker} onChange={e => setImportBroker(e.target.value as any)} style={inputStyle}>
            <option value="zerodha">Zerodha CSV</option>
            <option value="upstox">Upstox CSV</option>
          </select>
          <select value={importAccount} onChange={e => setImportAccount(e.target.value)}
            title="Zerodha's file name carries the client ID (tradebook-QY8357-EQ.csv); pick one only when it does not." style={inputStyle}>
            <option value="">ID from file name</option>
            {accounts.filter(a => a.broker === importBroker).map(a => (
              <option key={a.client_id} value={a.client_id}>{a.client_id} · {a.name}</option>
            ))}
          </select>
          <button onClick={() => fileRef.current?.click()} disabled={busy === "import"} style={btn()}
            title="Zerodha: Console → Reports → Tradebook → Equity, and again for Futures & Options; download each as CSV. Several files at once are fine; importing a file again adds nothing.">
            {busy === "import" ? "Importing…" : "Import tradebook"}
          </button>
          <input ref={fileRef} type="file" multiple accept=".csv,text/csv" onChange={onFile} style={{ display: "none" }} />
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
          <b style={{ color: "var(--text-primary)" }}>Nothing on file yet.</b><br />
          1. <b>Sync Upstox</b> pulls your Upstox share, ETF and F&O trades for the last three financial years (log in to Upstox first).<br />
          2. For Zerodha, open Console → Reports → Tradebook, choose Equity (and again Futures &amp; Options) and a date range,
          download <b>CSV</b>, then <b>Import tradebook</b>.<br />
          3. Mutual funds: <b>Import CAS (PDF)</b> below, your CAMS / KFintech consolidated statement.<br />
          4. Shares from an IPO, a bonus, or older than the files go in with <b>Add trade</b>.
        </div>
      )}

      <div style={{ display: "flex", gap: "6px", marginBottom: "10px", alignItems: "center", flexWrap: "wrap" }}>
        {(["overview", "exposure", "fno", "tax", "trades"] as const).map(k => (
          <button key={k} onClick={() => setTab(k)} style={btn(tab === k)}
            title={k === "exposure" ? "Sectors → industries → stocks, counting shares held directly and the stocks inside your ETFs and funds"
              : k === "overview" ? "Shares, ETFs and mutual funds in one list — open a row for its lots, tax dates and detail"
              : k === "fno" ? "Futures and options: open positions, closed trades, profit, charges and turnover" : undefined}>
            {k === "overview" ? `All holdings${ov ? ` (${ov.rows.length})` : ""}` : k === "exposure" ? "Analytics" : k === "fno" ? "F&O"
              : k === "tax" ? "Capital gains & tax" : "Trades"}
          </button>
        ))}
        <span style={{ marginLeft: "auto", fontSize: "11px", color: "var(--text-faint)" }}>
          {summary?.span?.upstox && `Upstox ${summary.span.upstox.first} → ${summary.span.upstox.last}`}
          {summary?.span?.upstox && summary?.span?.zerodha && " · "}
          {summary?.span?.zerodha && `Zerodha ${summary.span.zerodha.first} → ${summary.span.zerodha.last}`}
          {lastSync && ` · Upstox synced ${lastSync.toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}`}
        </span>
      </div>

      {tab === "overview" && (
        <OverviewTab data={ov} many={names.length > 1} who={who} broker={broker}
                     onPick={n => { setWho(n); setTaxWho(n); }} onTab={k => setTab(k)}
                     onReload={() => { loadOv(); loadSummary(); }} setMsg={setMsg} />
      )}

      {tab === "exposure" && (
        <AnalyticsTab data={exp} loading={expLoading} onRefresh={loadExp} setMsg={setMsg} />
      )}

      {tab === "fno" && <FnoTab broker={broker} who={who} many={names.length > 1} setMsg={setMsg} />}

      {tab === "tax" && (
        <div>
          <div style={{ display: "flex", gap: "8px", alignItems: "center", marginBottom: "10px", flexWrap: "wrap" }}>
            <span style={{ fontSize: "12px", color: "var(--text-muted)" }}>Financial year</span>
            <select value={fy} onChange={e => setFy(e.target.value)} style={inputStyle}>
              {(summary?.financial_years || [fy]).map(f => <option key={f} value={f}>FY {f}</option>)}
            </select>
            {names.length > 0 && (
              <>
                <span style={{ fontSize: "12px", color: "var(--text-muted)" }}>for</span>
                <select value={taxName} onChange={e => setTaxWho(e.target.value)}
                  title="Tax is per person: each name has its own ₹1.25 lakh LTCG exemption and loss set-off. Everyone adds up each person's own tax." style={inputStyle}>
                  <option value="">Everyone</option>
                  {names.map(n => <option key={n} value={n}>{n}</option>)}
                </select>
              </>
            )}
            {fy && (
              <a href={`${API_BASE}/api/portfolio/realised.csv?fy=${fy}${broker ? `&broker=${broker}` : ""}${taxName ? `&name=${encodeURIComponent(taxName)}` : ""}`}
                 style={{ ...btn(), textDecoration: "none", marginLeft: "auto" }}>Download capital-gains CSV</a>
            )}
          </div>
          {tax && <TaxView tax={tax} onFno={() => setTab("fno")}
            csvHref={`${API_BASE}/api/portfolio/itr.csv?fy=${fy}${broker ? `&broker=${broker}` : ""}${taxName ? `&name=${encodeURIComponent(taxName)}` : ""}`} />}
        </div>
      )}

      {tab === "trades" && (
        <div>
          <div style={{ display: "flex", gap: "8px", marginBottom: "10px", flexWrap: "wrap", alignItems: "center" }}>
            {([["shares", "Shares & ETFs"], ["fno", "F&O"], ["mf", "Mutual funds"]] as const).map(([k, l]) => (
              <button key={k} onClick={() => setTradeKind(k)} style={btn(tradeKind === k)}>{l}</button>
            ))}
            {tradeKind !== "mf" && (
              <>
                <input value={tradeFilter} onChange={e => setTradeFilter(e.target.value)} placeholder={tradeKind === "fno" ? "Contract or underlying" : "Symbol or ISIN"}
                  onKeyDown={e => { if (e.key === "Enter") loadTrades(); }} style={{ ...inputStyle, width: "180px" }} />
                <button onClick={loadTrades} style={btn()}>Show</button>
              </>
            )}
          </div>
          <div style={{ ...card, padding: 0, overflowX: "auto" }}>
            {tradeKind === "shares" && (
              <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "860px" }}>
                <thead>
                  <tr>
                    {["DATE", "SYMBOL", "TYPE", "AT", "NAME", "SIDE", "QTY", "PRICE", "VALUE", "SOURCE", ""].map((h, i) => (
                      <th key={i} style={th(i < 6 ? "left" : "right")}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {trades.map(tr => (
                    <tr key={tr.id}>
                      <td style={td("left")}>{tr.trade_date}{tr.trade_time ? ` ${tr.trade_time.slice(0, 5)}` : ""}</td>
                      <td style={{ ...td("left"), fontWeight: 600 }} title={tr.isin || ""}>{tr.symbol}</td>
                      <td style={td("left")}><TypeChip t={(tr.isin || "").startsWith("INF") ? "etf" : "stock"} /></td>
                      <td style={td("left")}>{brokerChip(tr.broker)}</td>
                      <td style={{ ...td("left"), color: "var(--text-secondary)" }} title={tr.account || "no client ID"}>{tr.name || "—"}</td>
                      <td style={{ ...td("left"), fontWeight: 700, color: tr.side === "BUY" ? "var(--positive)" : "var(--negative)" }}>{tr.side}</td>
                      <td style={td()}>{qtyTxt(tr.quantity)}</td>
                      <td style={td()}>{inr(tr.price, 2)}</td>
                      <td style={td()}>{inr(tr.quantity * tr.price)}</td>
                      <td style={{ ...td(), color: "var(--text-muted)", fontSize: "11px" }} title={tr.note || ""}>{tr.source}</td>
                      <td style={td()}>{delBtn("shares", tr.id)}</td>
                    </tr>
                  ))}
                  {trades.length === 0 && <tr><td colSpan={11} style={{ ...td("left"), color: "var(--text-faint)", fontStyle: "italic" }}>No trades.</td></tr>}
                </tbody>
              </table>
            )}
            {tradeKind === "fno" && (
              <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "960px" }}>
                <thead>
                  <tr>
                    {["DATE", "CONTRACT", "TYPE", "EXPIRY", "AT", "NAME", "SIDE", "QTY", "PRICE", "VALUE", "SOURCE", ""].map((h, i) => (
                      <th key={i} style={th(i < 7 ? "left" : "right")}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {fnoTxns.map(tr => (
                    <tr key={tr.id}>
                      <td style={td("left")}>{tr.trade_date}{tr.trade_time ? ` ${tr.trade_time.slice(0, 5)}` : ""}</td>
                      <td style={{ ...td("left"), fontWeight: 600 }}>{tr.symbol}</td>
                      <td style={td("left")}>{tr.instrument}{tr.strike ? ` ${tr.strike}` : ""}</td>
                      <td style={td("left")}>{fmtDate(tr.expiry)}</td>
                      <td style={td("left")}>{brokerChip(tr.broker)}</td>
                      <td style={{ ...td("left"), color: "var(--text-secondary)" }}>{tr.name || "—"}</td>
                      <td style={{ ...td("left"), fontWeight: 700, color: tr.side === "BUY" ? "var(--positive)" : "var(--negative)" }}>{tr.side}</td>
                      <td style={td()}>{qtyTxt(tr.quantity)}</td>
                      <td style={td()}>{inr(tr.price, 2)}</td>
                      <td style={td()}>{inr(tr.quantity * tr.price)}</td>
                      <td style={{ ...td(), color: "var(--text-muted)", fontSize: "11px" }}>{tr.source}</td>
                      <td style={td()}>{delBtn("fno", tr.id)}</td>
                    </tr>
                  ))}
                  {fnoTxns.length === 0 && <tr><td colSpan={12} style={{ ...td("left"), color: "var(--text-faint)", fontStyle: "italic" }}>No F&O trades — import Zerodha's Futures &amp; Options tradebook, or Sync Upstox.</td></tr>}
                </tbody>
              </table>
            )}
            {tradeKind === "mf" && (
              <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "900px" }}>
                <thead><tr>{["DATE", "SCHEME", "NAME", "TYPE", "AMOUNT", "UNITS", "NAV", "BALANCE"].map((h, i) => <th key={h} style={th(i < 4 ? "left" : "right")}>{h}</th>)}</tr></thead>
                <tbody>
                  {mfTxns.map(x => (
                    <tr key={x.id}>
                      <td style={td("left")}>{x.date}</td>
                      <td style={{ ...td("left"), maxWidth: "340px", overflow: "hidden", textOverflow: "ellipsis" }} title={`${x.scheme} · folio ${x.folio}`}>{x.scheme}</td>
                      <td style={{ ...td("left"), color: "var(--text-secondary)" }}>{x.name}</td>
                      <td style={{ ...td("left"), fontWeight: 600, color: x.kind === "buy" ? "var(--positive)" : x.kind === "sell" ? "var(--negative)" : "var(--text-muted)" }}
                          title={x.description || ""}>{x.description || x.kind}</td>
                      <td style={td()}>{x.amount == null ? "—" : inr(Math.abs(x.amount), 2)}</td>
                      <td style={td()}>{x.units == null ? "" : x.units.toLocaleString("en-IN", { maximumFractionDigits: 3 })}</td>
                      <td style={td()}>{x.nav == null ? "" : x.nav.toFixed(4)}</td>
                      <td style={td()}>{x.balance == null ? "" : x.balance.toLocaleString("en-IN", { maximumFractionDigits: 3 })}</td>
                    </tr>
                  ))}
                  {mfTxns.length === 0 && <tr><td colSpan={8} style={{ ...td("left"), color: "var(--text-faint)", fontStyle: "italic" }}>No fund transactions — import your CAS in All holdings.</td></tr>}
                </tbody>
              </table>
            )}
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

interface MfTxn { id: number; date: string; scheme: string; isin: string; folio: string; kind: string; description: string | null;
                  amount: number | null; units: number | null; nav: number | null; balance: number | null; name: string }

// ─── All holdings: shares, ETFs and funds in one list ───────────────────────

const TYPE_STYLE: Record<string, { label: string; rgb: string; color: string }> = {
  stock: { label: "Stock", rgb: "--accent-rgb", color: "var(--accent)" },
  direct: { label: "Direct", rgb: "--accent-rgb", color: "var(--accent)" },
  etf: { label: "ETF", rgb: "--ai-rgb", color: "var(--ai)" },
  mf: { label: "Fund", rgb: "--positive-rgb", color: "var(--positive-strong)" },
  fno: { label: "F&O", rgb: "--warning-rgb", color: "var(--warning)" },
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

// The HELD cell's hover: each purchase still held, as "qty · days", oldest first.
// Lots of the same day (one buy split across brokers) are added together.
function heldInfo(r: OvRow): { tip: string; many: boolean } {
  const lots = Array.isArray(r.lots) ? r.lots : [];
  if (r.type === "fno") return { tip: r.first_bought ? `Opened ${fmtDate(r.first_bought)}` : "", many: false };
  const byDate = new Map<string, { qty: number; days: number | null }>();
  for (const l of lots) {
    const k = l.date || "";
    const e = byDate.get(k) || { qty: 0, days: l.days };
    e.qty += l.qty || 0;
    byDate.set(k, e);
  }
  const fmtQ = (q: number) => r.type === "mf" ? q.toLocaleString("en-IN", { maximumFractionDigits: 3 })
                                             : (Math.round(q * 1000) / 1000).toLocaleString("en-IN");
  const dated = [...byDate.entries()].filter(([d]) => d).sort(([a], [b]) => a.localeCompare(b));
  const lines = dated.map(([d, e]) => `${fmtQ(e.qty)} · ${e.days ?? "?"} d (${fmtDate(d)})`);
  const und = byDate.get("");
  if (und && und.qty > 0) lines.push(`${fmtQ(und.qty)} · older than the history on file`);
  if (!lines.length) return { tip: "No dated purchase on file", many: false };
  const many = lines.length > 1;
  const avg = many && r.avg_days != null ? `\nAverage ${r.avg_days} d by quantity` : "";
  return { tip: (many ? `${lines.length} purchases held:\n` : "Bought ") + lines.join("\n") + avg, many };
}

function OverviewTab({ data, many, who, broker, onPick, onTab, onReload, setMsg }: {
  data: OverviewResp | null; many: boolean; who: string; broker: string; onPick: (n: string) => void;
  onTab: (k: "exposure" | "fno" | "tax" | "trades") => void; onReload: () => void;
  setMsg: (m: { text: string; tone: "ok" | "err" } | null) => void;
}) {
  const [type, setType] = useState<"" | "stock" | "etf" | "mf" | "fno">("");
  const [sort, setSort] = useState<"value" | "pnl" | "pnl_pct" | "day" | "name">("value");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState("");
  const [forScheme, setForScheme] = useState<string | null>(null);
  const casRef = useRef<HTMLInputElement | null>(null);
  const pfRef = useRef<HTMLInputElement | null>(null);

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
        const m = (r.matched || []).map((x: any) => `${String(x.scheme).split(" - ")[0]} (${fmtDate(x.as_of)}${x.date_found ? "" : " — date assumed"}, ${x.stocks} stocks, ${x.equity_pct}% in shares)`);
        const um = (r.unmatched || []).map((x: any) => `${x.scheme} — best guess "${x.best}" was not close enough`);
        if (!m.length) bad = true;
        lines.push(`${f.name}: ${m.length ? m.join("; ") : "no fund or ETF you hold matched"}${um.length ? `. ${um.join("; ")}` : ""}`);
      } catch (err: any) { bad = true; lines.push(`${f.name}: ${err.message}`); }
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

  const tools = (
    <div style={{ display: "flex", flexWrap: "wrap", gap: "8px", alignItems: "center", marginBottom: "10px" }}>
      <span style={{ fontSize: "11px", fontWeight: 700, color: "var(--text-muted)", letterSpacing: "0.3px" }}>MUTUAL FUNDS</span>
      <input type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="off"
        placeholder="CAS password, if any" title="Only used to open the PDF; never stored" style={{ ...inputStyle, width: "160px" }} />
      <button onClick={() => casRef.current?.click()} disabled={busy === "cas"} style={btnStyle()}
        title="CAMS or KFintech Consolidated Account Statement (detailed, with transactions), from the first investment to today. Re-importing adds only what is new.">
        {busy === "cas" ? "Reading statement…" : "Import CAS (PDF)"}
      </button>
      <input ref={casRef} type="file" accept=".pdf,application/pdf" onChange={importCas} style={{ display: "none" }} />
      <button onClick={() => pickFor(null)} disabled={busy === "pf"} style={btnStyle()}
        title="Each fund house's month-end portfolio file (Excel, PDF or zip), for funds and ETFs. Each sheet is matched to what you hold.">
        {busy === "pf" ? "Reading portfolios…" : "Upload fund portfolios"}
      </button>
      <input ref={pfRef} type="file" multiple accept=".xlsx,.xls,.xlsm,.pdf,.zip" onChange={uploadPortfolios} style={{ display: "none" }} />
      <button onClick={refreshNavs} disabled={busy === "nav"} style={btnStyle()} title="Latest NAVs from AMFI (also fetched at 07:00 and 22:30)">
        {busy === "nav" ? "Fetching…" : "Refresh NAVs"}
      </button>
    </div>
  );

  if (!data) return <div>{tools}<div style={{ ...cardStyle, fontSize: "13px", color: "var(--text-muted)" }}>Loading holdings…</div></div>;
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
  const fundsMissing = all.filter(r => (r.type === "mf" || r.type === "etf") && !r.portfolio && r.tax_class === "equity");
  const COLS = many ? 16 : 15;
  const lotLine = (l: OvLot) => `${l.qty.toLocaleString("en-IN", { maximumFractionDigits: 3 })} bought ${l.date ? fmtDate(l.date) : "before the history on file"}`
    + `${l.days != null ? ` · ${l.days} days` : ""}${l.long_term ? " · long-term" : ""}${l.broker ? ` · ${atLabel(l.broker)}` : ""}${l.cost != null ? ` · cost ${inr(l.cost)}` : ""}`;

  return (
    <div>
      {tools}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: "10px", marginBottom: "12px" }}>
        <Tile label="Current value" value={inr(t?.value)} sub={`invested ${inr(t?.invested)}${t?.unpriced ? ` · ${t.unpriced} at cost (no price)` : ""}`} />
        <Tile label="Unrealised P&L" value={signed(t?.pnl)} color={col(t?.pnl)}
              sub={t?.pnl != null && t.priced_invested ? pct(t.pnl / t.priced_invested * 100) : undefined} />
        <Tile label="Day's P&L" value={signed(t?.day_pnl)} color={col(t?.day_pnl)} sub="shares & ETFs live · funds at the latest NAV" />
        <Tile label={`Realised FY ${data.realised_fy || ""}`} value={signed((data.realised_fy_shares || 0) + (data.realised_fy_funds || 0))}
              color={col((data.realised_fy_shares || 0) + (data.realised_fy_funds || 0))}
              sub={`shares ${signed(data.realised_fy_shares)} · funds ${signed(data.realised_fy_funds)} · F&O in its tab`} />
      </div>

      <div style={{ ...cardStyle, padding: "12px 14px", marginBottom: "12px" }}>
        <div style={{ fontSize: "10px", fontWeight: 700, color: "var(--text-muted)", letterSpacing: "0.4px", marginBottom: "8px" }}>WHERE THE MONEY IS</div>
        <MixBar parts={[
          { label: "Stocks", value: bt?.stock?.value ?? 0, pct: bt?.stock?.pct ?? null, color: "var(--accent)" },
          { label: "ETFs", value: bt?.etf?.value ?? 0, pct: bt?.etf?.pct ?? null, color: "var(--ai)" },
          { label: "Mutual funds", value: bt?.mf?.value ?? 0, pct: bt?.mf?.pct ?? null, color: "var(--positive)" },
          { label: "F&O", value: bt?.fno?.value ?? 0, pct: bt?.fno?.pct ?? null, color: "var(--warning)" },
        ]} />
        <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "640px", marginTop: "10px" }}>
          <thead><tr>{["KIND", "HOLDINGS", "INVESTED", "VALUE", "UNREALISED P&L", "P&L %", "DAY'S P&L", "SHARE"].map((h, i) => <th key={h} style={thStyle(i ? "right" : "left")}>{h}</th>)}</tr></thead>
          <tbody>
            {([["stock", "Stocks"], ["etf", "ETFs"], ["mf", "Mutual funds"], ["fno", "F&O"]] as const).map(([k, l]) => {
              const x = bt?.[k];
              if (!x || !x.count) return null;
              return (
                <tr key={k} onClick={() => setType(type === k ? "" : k)} style={{ cursor: "pointer", background: type === k ? "rgba(var(--accent-rgb),0.05)" : undefined }}>
                  <td style={tdStyle("left")}><TypeChip t={k} /> <span style={{ marginLeft: "4px", fontWeight: 600 }}>{l}</span></td>
                  <td style={tdStyle()}>{x.count}</td>
                  <td style={tdStyle()} title={k === "fno" ? "Premium paid on long options, less premium received on short ones; futures carry none" : ""}>{inr(x.invested)}</td>
                  <td style={{ ...tdStyle(), fontWeight: 700 }}>{inr(x.value)}</td>
                  <td style={{ ...tdStyle(), color: col(x.pnl), fontWeight: 700 }}>{signed(x.pnl)}</td>
                  <td style={{ ...tdStyle(), color: col(x.pnl) }}>{x.pnl != null && x.priced_invested ? pct(x.pnl / Math.abs(x.priced_invested) * 100) : "—"}</td>
                  <td style={{ ...tdStyle(), color: col(x.day_pnl) }}>{signed(x.day_pnl)}</td>
                  <td style={tdStyle()}>{x.pct == null ? "—" : `${x.pct.toFixed(1)}%`}</td>
                </tr>
              );
            })}
            <tr>
              <td style={{ ...tdStyle("left"), fontWeight: 800 }}>Total</td>
              <td style={tdStyle()}>{t?.count}</td>
              <td style={tdStyle()}>{inr(t?.invested)}</td>
              <td style={{ ...tdStyle(), fontWeight: 800 }}>{inr(t?.value)}</td>
              <td style={{ ...tdStyle(), color: col(t?.pnl), fontWeight: 800 }}>{signed(t?.pnl)}</td>
              <td style={{ ...tdStyle(), color: col(t?.pnl) }}>{t?.pnl != null && t.priced_invested ? pct(t.pnl / t.priced_invested * 100) : "—"}</td>
              <td style={{ ...tdStyle(), color: col(t?.day_pnl) }}>{signed(t?.day_pnl)}</td>
              <td style={tdStyle()}>100%</td>
            </tr>
          </tbody>
        </table>
        <div style={{ fontSize: "11px", color: "var(--text-faint)", marginTop: "6px" }}>
          What the ETFs and funds themselves hold — sectors and stocks — is in <b style={{ cursor: "pointer", textDecoration: "underline" }} onClick={() => onTab("exposure")}>Analytics</b>.
          {broker && " Mutual funds are left out while a broker is picked."}
        </div>
      </div>

      {!who && (data.by_name?.length ?? 0) > 1 && (
        <div style={{ ...cardStyle, padding: 0, overflowX: "auto", marginBottom: "12px" }}>
          <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "760px" }}>
            <thead><tr>{["BY NAME", "VALUE", "INVESTED", "P&L", "DAY", "STOCKS", "ETFS", "FUNDS", "F&O"].map((h, i) => <th key={h} style={thStyle(i ? "right" : "left")}>{h}</th>)}</tr></thead>
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
                  <td style={tdStyle()}>{b.fno_value ? inr(b.fno_value) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {((data.warnings?.length ?? 0) > 0 || fundsMissing.length > 0) && (
        <details style={{ ...cardStyle, padding: "10px 14px", marginBottom: "10px", borderColor: "var(--warning-border)", background: "var(--warning-bg)" }}>
          <summary style={{ cursor: "pointer", fontSize: "12px", fontWeight: 700, color: "var(--warning)" }}>
            {[fundsMissing.length ? `${fundsMissing.length} equity fund(s)/ETF(s) without a month-end portfolio (Analytics cannot see inside them)` : "",
              data.warnings?.length ? `${data.warnings.length} note(s) on the history` : ""].filter(Boolean).join(" · ")}
          </summary>
          {fundsMissing.map(r => (
            <div key={r.key} style={{ fontSize: "12px", color: "var(--text-secondary)", marginTop: "4px" }}>
              • <TypeChip t={r.type} /> {r.label || r.symbol} — {amcPage(r.amc || null) ? <a href={amcPage(r.amc || null)!} target="_blank" rel="noreferrer">{r.amc} portfolios</a>
                : `${r.amc || "the fund house"} website → statutory disclosures → monthly portfolio`}, then
              <b style={{ cursor: "pointer", textDecoration: "underline", marginLeft: "4px" }} onClick={() => pickFor(r.isin)}>upload for this one</b>
            </div>
          ))}
          {(data.warnings || []).map((w, i) => <div key={i} style={{ fontSize: "12px", color: "var(--text-secondary)", marginTop: "4px" }}>• {w}</div>)}
        </details>
      )}

      <div style={{ display: "flex", gap: "6px", alignItems: "center", marginBottom: "10px", flexWrap: "wrap" }}>
        {([["", "All", t?.count], ["stock", "Stocks", bt?.stock?.count], ["etf", "ETFs", bt?.etf?.count], ["mf", "Mutual funds", bt?.mf?.count], ["fno", "F&O", bt?.fno?.count]] as const).map(([k, l, n]) => (
          <button key={k || "all"} onClick={() => setType(k)} style={btnStyle(type === k)}>{l} ({n ?? 0})</button>
        ))}
        <input value={q} onChange={e => setQ(e.target.value)} placeholder="Filter name, sector, category" style={{ ...inputStyle, width: "210px" }} />
        <span style={{ marginLeft: "auto", fontSize: "11px", color: "var(--text-faint)" }}>Click a row for its lots, tax dates and detail</span>
      </div>

      <div style={{ ...cardStyle, padding: 0, overflowX: "auto" }}>
        <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "1200px" }}>
          <thead>
            <tr>
              {sortTh("name", "HOLDING", "left")}
              <th style={thStyle("left")}>TYPE</th>
              {many && <th style={thStyle("left")}>NAME</th>}
              <th style={thStyle("left")}>AT</th>
              <th style={thStyle()} title="Days since the oldest purchase still held. Several purchases: hover for each one's quantity and days held">HELD</th>
              <th style={thStyle()}>QTY / UNITS</th>
              <th style={thStyle()}>AVG</th>
              <th style={thStyle()} title="Live price for shares and ETFs; the latest NAV (published once a day) for funds">PRICE / NAV</th>
              {sortTh("day", "DAY")}
              <th style={thStyle()}>INVESTED</th>
              {sortTh("value", "VALUE")}
              {sortTh("pnl", "P&L")}
              {sortTh("pnl_pct", "P&L %")}
              <th style={thStyle()} title="Share of everything you hold">WEIGHT</th>
              <th style={thStyle()} title="Units past the long-term holding period (12 months for shares and equity funds)">LONG-TERM</th>
              <th style={thStyle("left")}>SECTOR / CATEGORY</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => {
              const k = `${r.type}:${r.name}:${r.key}`;
              const isOpen = open === k;
              const lt = r.long_term_qty ?? 0;
              const held = heldInfo(r);
              return (
                <Fragment key={k}>
                  <tr onClick={() => setOpen(isOpen ? null : k)} style={{ cursor: "pointer", background: isOpen ? "rgba(var(--accent-rgb),0.04)" : undefined }}>
                    <td style={{ ...tdStyle("left"), maxWidth: "300px", overflow: "hidden", textOverflow: "ellipsis" }} title={`${r.label || r.symbol || ""}${r.isin ? ` · ${r.isin}` : ""}`}>
                      <span style={{ color: "var(--text-faint)", marginRight: "5px" }}>{isOpen ? "▾" : "▸"}</span>
                      <b style={{ color: "var(--text-primary)" }}>{r.type === "mf" || r.type === "fno" ? r.label : r.symbol}</b>
                      {r.type === "fno" && r.fno && <span style={{ marginLeft: "6px", fontSize: "10px", fontWeight: 700, color: r.fno.side === "long" ? "var(--positive)" : "var(--negative)" }}>{r.fno.side.toUpperCase()}</span>}
                      {r.type === "etf" && r.label && r.label !== r.symbol && <div style={{ fontSize: "10px", color: "var(--text-faint)", marginLeft: "15px" }}>{r.label}</div>}
                      {r.type === "mf" && r.amc && <div style={{ fontSize: "10px", color: "var(--text-faint)", marginLeft: "15px" }}>{r.amc}</div>}
                    </td>
                    <td style={tdStyle("left")}><TypeChip t={r.type} /></td>
                    {many && <td style={{ ...tdStyle("left"), color: "var(--text-secondary)" }}>{r.name}</td>}
                    <td style={{ ...tdStyle("left"), fontSize: "11px", color: "var(--text-secondary)" }}>{r.at.map(atLabel).join(" + ")}</td>
                    <td style={tdStyle()} title={held.tip}>
                      <span style={held.many ? { borderBottom: "1px dotted var(--text-faint)", cursor: "help" } : undefined}>
                        {r.days_held != null ? `${r.days_held.toLocaleString("en-IN")} d${r.undated ? "+" : ""}` : "—"}</span>
                    </td>
                    <td style={tdStyle()}>{r.type === "mf" ? r.qty.toLocaleString("en-IN", { maximumFractionDigits: 3 }) : qtyTxt(r.qty)}</td>
                    <td style={tdStyle()}>{r.avg ? inr(r.avg, r.type === "mf" ? 4 : 2) : "—"}</td>
                    <td style={tdStyle()} title={r.price_date ? `NAV of ${fmtDate(r.price_date)}` : ""}>
                      {r.price ? inr(r.price, r.type === "mf" ? 4 : 2) : "—"}
                      {r.type === "mf" && r.price_date && <div style={{ fontSize: "10px", color: r.nav_source === "statement" ? "var(--warning)" : "var(--text-faint)" }}>{fmtDate(r.price_date)}</div>}
                    </td>
                    <td style={{ ...tdStyle(), color: col(r.day_change_pct) }}>{pct(r.day_change_pct)}</td>
                    <td style={tdStyle()}>{inr(r.invested)}</td>
                    <td style={{ ...tdStyle(), fontWeight: 700 }}>{r.value == null ? <span title="No price yet — counted at cost">{inr(r.invested)}*</span> : inr(r.value)}</td>
                    <td style={{ ...tdStyle(), color: col(r.pnl), fontWeight: 700 }}>{signed(r.pnl)}</td>
                    <td style={{ ...tdStyle(), color: col(r.pnl_pct) }}>{pct(r.pnl_pct)}</td>
                    <td style={tdStyle()}>{r.weight == null ? "—" : `${r.weight.toFixed(1)}%`}</td>
                    <td style={{ ...tdStyle(), color: "var(--text-secondary)", fontSize: "11px" }}
                        title={r.next_long_term ? `More become long-term on ${r.next_long_term}` : ""}>
                      {r.type === "fno" ? (r.fno?.expiry ? `expires ${fmtDate(r.fno.expiry)}${r.fno.expiry_estimated ? " ≈" : ""}` : "—")
                        : lt >= r.qty - 0.001 ? "all" : lt > 0 ? `${qtyTxt(Math.round(lt * 1000) / 1000)} of ${qtyTxt(Math.round(r.qty * 1000) / 1000)}`
                        : r.next_long_term ? `from ${fmtDate(r.next_long_term)}` : (r.undated_qty ?? 0) > 0 ? "older — date unknown" : r.type === "mf" ? "never (slab)" : "—"}
                    </td>
                    <td style={{ ...tdStyle("left"), fontSize: "11px", color: "var(--text-secondary)", maxWidth: "220px", overflow: "hidden", textOverflow: "ellipsis" }}
                        title={r.industry || r.category || ""}>{r.sector || r.category || "—"}</td>
                  </tr>
                  {isOpen && (
                    <tr>
                      <td colSpan={COLS} style={{ padding: "8px 14px 12px 34px", background: "var(--surface-2)", borderBottom: "1px solid var(--border-subtle)", fontSize: "12px", color: "var(--text-secondary)" }}>
                        <div style={{ display: "flex", flexWrap: "wrap", gap: "24px" }}>
                          {r.type !== "fno" && <div style={{ minWidth: "280px" }}>
                            <div style={{ fontSize: "10px", fontWeight: 700, color: "var(--text-muted)", marginBottom: "4px" }}>LOTS (FIRST IN, FIRST OUT)</div>
                            {(r.lots || []).length ? (r.lots || []).map((l, i) => <div key={i}>{lotLine(l)}</div>) : <div>—</div>}
                            {r.next_long_term && <div style={{ marginTop: "4px" }}>Next lot turns long-term on <b>{fmtDate(r.next_long_term)}</b>.</div>}
                          </div>}
                          {(r.parts?.length ?? 0) > 1 && (
                            <div style={{ minWidth: "240px" }}>
                              <div style={{ fontSize: "10px", fontWeight: 700, color: "var(--text-muted)", marginBottom: "4px" }}>BY BROKER</div>
                              {r.parts!.map((p, i) => (
                                <div key={i}>{atLabel(p.broker)}{p.account ? ` (${p.account})` : ""}: {qtyTxt(p.qty)} @ {inr(p.avg, 2)} · invested {inr(p.invested)}{p.value != null ? ` · now ${inr(p.value)}` : ""}</div>
                              ))}
                            </div>
                          )}
                          {r.type === "fno" && r.fno && (
                            <div style={{ minWidth: "320px" }}>
                              <div style={{ fontSize: "10px", fontWeight: 700, color: "var(--text-muted)", marginBottom: "4px" }}>CONTRACT</div>
                              <div>{r.symbol} · {r.fno.side} {qtyTxt(Math.abs(r.qty))} @ {inr(r.fno.avg, 2)} · opened {r.fno.opened}</div>
                              <div>Expiry {fmtDate(r.fno.expiry)}{r.fno.expiry_estimated ? " (worked out from the month)" : ""} · notional {inr(r.fno.notional)}</div>
                              <div>MTM {signed(r.fno.mtm)}{r.fno.priced ? "" : " — no live price yet"}</div>
                              <div style={{ color: "var(--text-faint)", marginTop: "4px" }}>
                                {r.fno.instrument === "FUT" ? "A future holds no capital: its value here is its mark-to-market."
                                  : r.fno.side === "short" ? "A short option is a liability: invested is the premium received, value what it would cost to buy back (both negative)."
                                  : "A long option is worth its premium: invested is what you paid, value today's premium."} Closed trades and charges are in the F&O tab.
                              </div>
                            </div>
                          )}
                          {r.type === "mf" && (
                            <div style={{ minWidth: "260px" }}>
                              <div style={{ fontSize: "10px", fontWeight: 700, color: "var(--text-muted)", marginBottom: "4px" }}>FUND</div>
                              <div>{r.category || "category not known yet"}{r.folios?.length ? ` · folio ${r.folios.join(", ")}` : ""}</div>
                              <div>XIRR {r.xirr == null ? "—" : `${r.xirr.toFixed(2)}%`} · NAV {r.nav_source === "statement" ? "from the statement" : "from AMFI"}</div>
                            </div>
                          )}
                          {(r.type === "mf" || r.type === "etf") && (
                            <div style={{ minWidth: "260px" }} onClick={e => e.stopPropagation()}>
                              <div style={{ fontSize: "10px", fontWeight: 700, color: "var(--text-muted)", marginBottom: "4px" }}>TAX AND PORTFOLIO</div>
                              {r.type === "mf" ? (
                                <div>Taxed as{" "}
                                  <select value={r.overridden ? r.tax_class || "" : ""} onChange={e => setClass(r.isin!, e.target.value)} title={r.tax_why || ""}
                                    style={{ ...inputStyle, padding: "2px 4px", fontSize: "11px" }}>
                                    <option value="">{classLabel(r.tax_class || "equity")} (auto)</option>
                                    <option value="equity">Equity</option><option value="debt">Debt</option><option value="other">Other</option>
                                  </select>
                                  <span style={{ color: "var(--text-faint)", marginLeft: "6px" }}>{r.tax_why}</span>
                                </div>
                              ) : <div>Taxed as {classLabel(r.tax_class || "equity")}{r.category ? ` (${r.category})` : ""}</div>}
                              <div style={{ marginTop: "4px" }}>
                                Month-end portfolio: {r.portfolio ? <>{fmtDate(r.portfolio.as_of)} · {r.portfolio.n} stocks · {r.portfolio.equity_pct ?? "—"}% in shares </> : "none yet "}
                                <button onClick={() => pickFor(r.isin)} style={{ ...btnStyle(), padding: "1px 8px", fontSize: "11px" }}>{r.portfolio ? "Replace" : "Upload"}</button>
                                {amcPage(r.amc || null) && <a href={amcPage(r.amc || null)!} target="_blank" rel="noreferrer" style={{ marginLeft: "8px" }}>download page</a>}
                              </div>
                            </div>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
            {rows.length === 0 && <tr><td colSpan={COLS} style={{ ...tdStyle("left"), color: "var(--text-faint)", fontStyle: "italic" }}>Nothing held.</td></tr>}
          </tbody>
        </table>
      </div>
      <div style={{ fontSize: "11px", color: "var(--text-faint)", marginTop: "6px", lineHeight: 1.6 }}>
        Shares and ETFs are merged across Upstox and Zerodha and priced live (refreshed every 30 s here); mutual funds come from your
        CAS and are valued at AMFI's latest NAV, published once each evening (fetched at 07:00, 22:30, every 3 h while this page is open, and on Refresh NAVs).
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
  const [view, setView] = useState<"sector" | "stock" | "months">("sector");
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
        <button onClick={() => setView("months")} style={btnStyle(view === "months")}
          title="What each fund or ETF held month by month, from the portfolio files uploaded">Fund holdings by month</button>
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

      {view === "months" && <FundMonths setMsg={setMsg} />}

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

// ─── Capital gains & tax: shares, ETFs, funds and F&O for one FY ─────────────

function TaxView({ tax, onFno, csvHref }: { tax: TaxResp; onFno: () => void; csvHref: string }) {
  const card = cardStyle, th = thStyle, td = tdStyle;
  const fl = tax.flows;
  const assetOf = (r: TaxRow) => r.asset || (r.broker === "mf" ? "mf" : (r.isin || "").startsWith("INF") ? "etf" : "stock");
  const fno = tax.fno;
  return (
    <>
      {fl && (
        <div style={{ ...card, padding: "10px 14px", marginBottom: "10px", display: "flex", flexWrap: "wrap", gap: "18px", fontSize: "12px", color: "var(--text-secondary)" }}>
          <b style={{ color: "var(--text-primary)" }}>FY {tax.fy} activity</b>
          {([["stock", "Stocks"], ["etf", "ETFs"], ["mf", "Mutual funds"]] as const).map(([k, l]) => (
            <span key={k}><TypeChip t={k} /> {l}: bought <b>{inr(fl[k]?.bought)}</b> · sold <b>{inr(fl[k]?.sold)}</b> <span style={{ color: "var(--text-faint)" }}>({fl[k]?.count ?? 0})</span></span>
          ))}
          <span><TypeChip t="fno" /> F&amp;O: <b>{fl.fno?.count ?? 0}</b> trades{fno ? <> · net <b style={{ color: col(fno.net) }}>{signed(fno.net)}</b></> : ""}</span>
        </div>
      )}
      {(tax.per_name?.length ?? 0) > 1 && (
        <div style={{ ...card, padding: 0, overflowX: "auto", marginBottom: "10px" }}>
          <div style={{ padding: "10px 12px 4px", fontSize: "12px", fontWeight: 700, color: "var(--text-primary)" }}>
            Per person <span style={{ fontWeight: 400, color: "var(--text-muted)" }}>— each with their own exemption and set-off; the estimate below is these added up</span>
          </div>
          <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "860px" }}>
            <thead><tr>{["NAME", "STCG", "LTCG", "EXEMPTION USED", "TAXABLE LTCG", "EST. TAX", "INTRADAY", "OTHER FUNDS/ETFS", "F&O NET"].map((h, i) => <th key={h} style={th(i ? "right" : "left")}>{h}</th>)}</tr></thead>
            <tbody>
              {tax.per_name!.map(p => (
                <tr key={p.name}>
                  <td style={{ ...td("left"), fontWeight: 700 }}>{p.name}</td>
                  <td style={{ ...td(), color: col(p.stcg) }}>{signed(p.stcg)}</td>
                  <td style={{ ...td(), color: col(p.ltcg) }}>{signed(p.ltcg)}</td>
                  <td style={td()}>{inr(p.exemption_used)} of {inr(p.exemption)}</td>
                  <td style={td()}>{inr(p.taxable_ltcg)}</td>
                  <td style={{ ...td(), fontWeight: 700, color: "var(--warning)" }}>{inr(p.estimated_tax)}</td>
                  <td style={{ ...td(), color: col(p.intraday) }}>{signed(p.intraday)}</td>
                  <td style={{ ...td(), color: col(p.other_funds) }}>{signed(p.other_funds)}</td>
                  <td style={{ ...td(), color: col(p.fno) }}>{p.fno == null ? "—" : signed(p.fno)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))", gap: "10px", marginBottom: "10px" }}>
        <Tile label={`Short-term (STCG ${(tax.stcg.rate * 100).toFixed(1).replace(".0", "")}%)`} value={signed(tax.stcg.net)} color={col(tax.stcg.net)}
              sub={`gains ${inr(tax.stcg.gain)} · losses ${inr(-tax.stcg.loss)}`} />
        <Tile label={`Long-term (LTCG ${(tax.ltcg.rate * 100).toFixed(1).replace(".0", "")}%)`} value={signed(tax.ltcg.net)} color={col(tax.ltcg.net)}
              sub={`gains ${inr(tax.ltcg.gain)} · losses ${inr(-tax.ltcg.loss)}`} />
        <Tile label="LTCG exemption used" value={`${inr(tax.ltcg.exemption_used)} of ${inr(tax.ltcg.exemption)}`}
              sub={`taxable LTCG ${inr(tax.ltcg.taxable)}`} />
        <Tile label="Estimated tax on capital gains" value={inr(tax.estimated_tax)} color="var(--warning)"
              sub={(tax.per_name?.length ?? 0) > 1 ? "each person's own, added up · before cess"
                : tax.short_term_loss_set_off_against_long_term ? `after ${inr(tax.short_term_loss_set_off_against_long_term)} short-term loss set off` : "shares, equity ETFs and funds · before cess"} />
        <Tile label="Intraday (speculative)" value={signed(tax.intraday.pnl)} color={col(tax.intraday.pnl)}
              sub={`${tax.intraday.trades} round trip(s) · taxed at your slab`} />
        <Tile label="F&O (business income)" value={fno ? signed(fno.net) : "—"} color={col(fno?.net)}
              sub={fno ? `after ${inr(fno.charges.total)} charges · turnover ${inr(fno.turnover)} · at your slab` : "no F&O trades this year"} />
      </div>
      {tax.itr && <ItrSection itr={tax.itr} everyone={(tax.per_name?.length ?? 0) > 1} csvHref={csvHref} />}
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
        <div style={{ padding: "10px 12px 4px", fontSize: "12px", fontWeight: 700, color: "var(--text-primary)" }}>
          Equity-taxed sales <span style={{ fontWeight: 400, color: "var(--text-muted)" }}>— shares, equity ETFs and equity mutual funds</span>
        </div>
        <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "960px" }}>
          <thead>
            <tr>
              {["TERM", "WHAT", "HOLDING", "AT", "BOUGHT", "SOLD", "QTY", "BUY", "SELL", "COST", "SALE VALUE", "CHARGES", "STT", "GAIN"].map((h, i) => (
                <th key={h} style={th(i < 4 ? "left" : "right")}>{h}</th>
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
                <td style={td("left")}><TypeChip t={assetOf(r)} /></td>
                <td style={{ ...td("left"), fontWeight: 600, maxWidth: "260px", overflow: "hidden", textOverflow: "ellipsis" }} title={r.symbol || ""}>{r.symbol}</td>
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
                <td style={{ ...td(), color: col(r.tax_gain ?? r.gain), fontWeight: 700 }} title={`Before charges ${signed(r.gain)}`}>{signed(r.tax_gain ?? r.gain)}</td>
              </tr>
            ))}
            {tax.rows.length === 0 && (
              <tr><td colSpan={14} style={{ ...td("left"), color: "var(--text-faint)", fontStyle: "italic" }}>No equity-taxed sales in FY {tax.fy}.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {(tax.mf_other?.rows?.length ?? 0) > 0 && (
        <div style={{ ...card, padding: 0, overflowX: "auto", marginBottom: "8px" }}>
          <div style={{ padding: "10px 12px 4px", fontSize: "12px", fontWeight: 700, color: "var(--text-primary)" }}>
            Funds and ETFs not taxed as equity
            <span style={{ fontWeight: 400, color: "var(--text-muted)" }}>
              {" "}— debt, gold, overseas, FoF. At your slab {signed(tax.mf_other!.slab_gain)}; long-term at 12.5% {signed(tax.mf_other!.ltcg_gain)}
              {tax.mf_other!.ltcg_tax > 0 ? ` (≈ ${inr(tax.mf_other!.ltcg_tax)} tax)` : ""}. Not in the estimate above.
            </span>
          </div>
          <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "820px" }}>
            <thead>
              <tr>{["TERM", "WHAT", "HOLDING", "BOUGHT", "SOLD", "UNITS", "COST", "SALE VALUE", "GAIN"].map((h, i) => (
                <th key={h} style={th(i < 3 ? "left" : "right")}>{h}</th>))}</tr>
            </thead>
            <tbody>
              {tax.mf_other!.rows.map((r, i) => (
                <tr key={i}>
                  <td style={{ ...td("left"), fontWeight: 700, color: "var(--ai)" }}>{r.term === "SLAB" ? "AT SLAB" : "LTCG 12.5%"}</td>
                  <td style={td("left")}><TypeChip t={assetOf(r)} /></td>
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
      {fno && (
        <div style={{ ...card, padding: "12px 14px", marginBottom: "8px" }}>
          <div style={{ display: "flex", alignItems: "baseline", gap: "10px", flexWrap: "wrap", marginBottom: "6px" }}>
            <b style={{ fontSize: "12px", color: "var(--text-primary)" }}>F&amp;O — non-speculative business income</b>
            <span style={{ fontSize: "12px", color: "var(--text-secondary)" }}>
              gross {signed(fno.gross)} (futures {signed(fno.futures)}, options {signed(fno.options)}) − charges {inr(fno.charges.total)}
              {" "}(STT {inr(fno.charges.stt)}) = <b style={{ color: col(fno.net) }}>{signed(fno.net)}</b> · turnover {inr(fno.turnover)} · {fno.closed} closed trade(s)
            </span>
            <b style={{ marginLeft: "auto", fontSize: "12px", cursor: "pointer", textDecoration: "underline", color: "var(--accent)" }} onClick={onFno}>Open F&amp;O</b>
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "14px", fontSize: "11px", color: "var(--text-secondary)" }}>
            {fno.by_underlying.slice(0, 12).map(u => (
              <span key={u.underlying}>{u.underlying} <b style={{ color: col(u.net) }}>{signed(u.net)}</b></span>
            ))}
          </div>
          {(fno.warnings?.length ?? 0) > 0 && <div style={{ fontSize: "11px", color: "var(--warning)", marginTop: "6px" }}>{fno.warnings.join(" · ")}</div>}
        </div>
      )}
      {(tax.warnings || []).map((w, i) => <div key={`w${i}`} style={{ fontSize: "11px", color: "var(--warning)", lineHeight: 1.6 }}>{w}</div>)}
      {tax.notes.map((n, i) => <div key={i} style={{ fontSize: "11px", color: "var(--text-faint)", lineHeight: 1.6 }}>{n}</div>)}
      {fno && <div style={{ fontSize: "11px", color: "var(--text-faint)", lineHeight: 1.6 }}>F&amp;O profit is added to your other income and taxed at your slab; every charge, STT included, is an expense there. A loss can be set off against any income except salary and carried forward 8 years.</div>}
    </>
  );
}


// ─── F&O: positions, closed trades, profit, charges and turnover ─────────────

interface FnoPos {
  symbol: string; underlying: string | null; instrument: string | null; expiry: string | null; expiry_estimated: boolean;
  strike: number | null; exchange: string | null; broker: string; account: string; name: string; qty: number; side: string;
  avg: number | null; opened: string; expired: boolean; settlement: number | null; ltp?: number | null; mtm?: number | null;
  day_pnl?: number | null; day_change_pct?: number | null;
}
interface FnoClosed {
  symbol: string; underlying: string | null; instrument: string | null; expiry: string | null; strike: number | null;
  broker: string; name: string; opened: string; closed: string; qty: number; side: string; entry: number; exit: number;
  pnl: number; how: string;
}
interface FnoResp {
  fy: string; gross: number; net: number; turnover: number; closed: number; futures: number; options: number;
  charges: { brokerage: number; stt: number; exchange: number; sebi: number; stamp: number; gst: number; total: number; turnover: number };
  by_underlying: { underlying: string; gross: number; charges: number; net: number; trades: number }[];
  rows: FnoClosed[]; positions: FnoPos[]; warnings: string[]; notes: string[]; financial_years: string[];
  trade_count: number; first: string | null; last: string | null;
  index?: { ready: boolean; error: string | null };
}

function FnoTab({ broker, who, many, setMsg }: {
  broker: string; who: string; many: boolean; setMsg: (m: { text: string; tone: "ok" | "err" } | null) => void;
}) {
  const [fy, setFy] = useState("");
  const [data, setData] = useState<FnoResp | null>(null);
  const [loading, setLoading] = useState(false);
  const [settle, setSettle] = useState<Record<string, string>>({});
  const [showAll, setShowAll] = useState(false);
  const load = async () => {
    setLoading(true);
    try {
      const qs = [fy ? `fy=${fy}` : "", broker ? `broker=${broker}` : "", who ? `name=${encodeURIComponent(who)}` : ""].filter(Boolean).join("&");
      const d = await getJSON<FnoResp>(`/api/portfolio/fno?${qs}`);
      setData(d);
      if (!fy) setFy(d.fy);
    } catch (e: any) { setMsg({ text: `F&O: ${e.message}`, tone: "err" }); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, [fy, broker, who]);
  useEffect(() => {
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [fy, broker, who]);
  const saveSettle = async (symbol: string, value: string | null) => {
    try {
      await postJSON("/api/portfolio/fno/settle", { symbol, price: value == null || value === "" ? null : parseFloat(value) });
      load();
    } catch (e: any) { setMsg({ text: e.message, tone: "err" }); }
  };
  if (!data) return <div style={{ ...cardStyle, fontSize: "13px", color: "var(--text-muted)" }}>{loading ? "Loading F&O…" : "No data."}</div>;
  const th = thStyle, td = tdStyle;
  const open = data.positions.filter(p => !p.expired);
  const expired = data.positions.filter(p => p.expired);
  const mtm = open.reduce((a, p) => a + (p.mtm ?? 0), 0);
  const priced = open.some(p => p.mtm != null);
  const contract = (x: { underlying: string | null; instrument: string | null; strike: number | null; expiry: string | null; symbol: string }) =>
    x.underlying ? `${x.underlying} ${x.strike ? `${x.strike} ` : ""}${x.instrument || ""} · ${fmtDate(x.expiry)}` : x.symbol;
  const rows = showAll ? data.rows : data.rows.slice(0, 100);
  if (!data.trade_count) {
    return (
      <div style={{ ...cardStyle, fontSize: "13px", color: "var(--text-secondary)", lineHeight: 1.7 }}>
        <b style={{ color: "var(--text-primary)" }}>No F&amp;O trades on file.</b><br />
        Zerodha: Console → Reports → Tradebook → segment <b>Futures &amp; Options</b>, pick a date range (repeat per year), download CSV, then
        <b> Import tradebook</b> at the top. Upstox: <b>Sync Upstox</b> now brings F&amp;O trades too.
      </div>
    );
  }
  return (
    <div>
      <div style={{ display: "flex", gap: "8px", alignItems: "center", marginBottom: "10px", flexWrap: "wrap" }}>
        <span style={{ fontSize: "12px", color: "var(--text-muted)" }}>Financial year</span>
        <select value={fy} onChange={e => setFy(e.target.value)} style={inputStyle}>
          {data.financial_years.map(f => <option key={f} value={f}>FY {f}</option>)}
        </select>
        <span style={{ fontSize: "11px", color: "var(--text-faint)" }}>{data.trade_count} trades on file, {data.first} → {data.last}</span>
        <button onClick={load} disabled={loading} style={{ ...btnStyle(), marginLeft: "auto" }}>{loading ? "Refreshing…" : "Refresh"}</button>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: "10px", marginBottom: "12px" }}>
        <Tile label={`Net P&L FY ${data.fy}`} value={signed(data.net)} color={col(data.net)} sub="after estimated charges" />
        <Tile label="Gross P&L" value={signed(data.gross)} color={col(data.gross)} sub={`futures ${signed(data.futures)} · options ${signed(data.options)}`} />
        <Tile label="Charges (est.)" value={inr(data.charges.total)} sub={`STT ${inr(data.charges.stt)} · brokerage ${inr(data.charges.brokerage)} · exch+GST+stamp ${inr(data.charges.exchange + data.charges.gst + data.charges.stamp + data.charges.sebi)}`} />
        <Tile label="Turnover (tax)" value={inr(data.turnover)} sub={`${data.closed} closed trade(s) · sum of |profit or loss|`} />
        <Tile label="Open positions" value={String(open.length)} color={priced ? col(mtm) : undefined}
              sub={priced ? `MTM ${signed(mtm)} · day ${signed(open.reduce((a, p) => a + (p.day_pnl ?? 0), 0))}`
                : !open.length ? "nothing open"
                : data.index?.error ? `no price: Upstox's contract list unavailable (${data.index.error.slice(0, 60)})`
                : !data.index?.ready ? "looking up the contracts' prices…" : "no live price (log in to Upstox)"} />
      </div>

      {(data.warnings?.length ?? 0) > 0 && (
        <div style={{ marginBottom: "10px", padding: "8px 12px", borderRadius: "8px", fontSize: "12px", background: "var(--warning-bg)", color: "var(--warning)" }}>
          {data.warnings.map((w, i) => <div key={i}>• {w}</div>)}
        </div>
      )}

      {(open.length > 0 || expired.length > 0) && (
        <div style={{ ...cardStyle, padding: 0, overflowX: "auto", marginBottom: "12px" }}>
          <div style={{ padding: "10px 12px 4px", fontSize: "12px", fontWeight: 700, color: "var(--text-primary)" }}>Open positions</div>
          <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "960px" }}>
            <thead><tr>{["CONTRACT", "AT", ...(many ? ["NAME"] : []), "SIDE", "QTY", "AVG", "LTP", "DAY", "MTM", "OPENED", "STATUS"].map((h, i) => <th key={h} style={th(i < (many ? 4 : 3) ? "left" : "right")}>{h}</th>)}</tr></thead>
            <tbody>
              {[...open, ...expired].map(p => (
                <tr key={`${p.account}:${p.symbol}`}>
                  <td style={{ ...td("left"), fontWeight: 600 }} title={p.symbol}>{contract(p)}{p.expiry_estimated ? <span title="Expiry worked out from the month; the source did not state it" style={{ color: "var(--text-faint)" }}> ≈</span> : ""}</td>
                  <td style={td("left")}>{brokerChip(p.broker)}</td>
                  {many && <td style={{ ...td("left"), color: "var(--text-secondary)" }}>{p.name}</td>}
                  <td style={{ ...td("left"), fontWeight: 700, color: p.side === "long" ? "var(--positive)" : "var(--negative)" }}>{p.side.toUpperCase()}</td>
                  <td style={td()}>{qtyTxt(Math.abs(p.qty))}</td>
                  <td style={td()}>{inr(p.avg, 2)}</td>
                  <td style={td()}>{p.ltp ? inr(p.ltp, 2) : "—"}</td>
                  <td style={{ ...td(), color: col(p.day_pnl) }} title={p.day_change_pct != null ? pct(p.day_change_pct) : ""}>{signed(p.day_pnl)}</td>
                  <td style={{ ...td(), color: col(p.mtm), fontWeight: 700 }}>{signed(p.mtm)}</td>
                  <td style={td()}>{p.opened}</td>
                  <td style={td()}>
                    {p.expired ? (
                      <span style={{ display: "inline-flex", gap: "4px", alignItems: "center" }}>
                        <span style={{ color: "var(--warning)" }}>expired — settlement</span>
                        <input value={settle[p.symbol] ?? (p.settlement != null ? String(p.settlement) : "")} inputMode="decimal"
                          onChange={e => setSettle({ ...settle, [p.symbol]: e.target.value })} placeholder="price" style={{ ...inputStyle, width: "80px", padding: "2px 6px" }} />
                        <button onClick={() => saveSettle(p.symbol, settle[p.symbol] ?? "")} style={{ ...btnStyle(), padding: "2px 8px", fontSize: "11px" }}>Save</button>
                      </span>
                    ) : "open"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data.by_underlying.length > 0 && (
        <div style={{ ...cardStyle, padding: 0, overflowX: "auto", marginBottom: "12px" }}>
          <div style={{ padding: "10px 12px 4px", fontSize: "12px", fontWeight: 700, color: "var(--text-primary)" }}>By underlying, FY {data.fy}</div>
          <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "560px" }}>
            <thead><tr>{["UNDERLYING", "CLOSED TRADES", "GROSS", "CHARGES", "NET"].map((h, i) => <th key={h} style={th(i ? "right" : "left")}>{h}</th>)}</tr></thead>
            <tbody>
              {[...data.by_underlying].sort((a, b) => b.net - a.net).map(u => (
                <tr key={u.underlying}>
                  <td style={{ ...td("left"), fontWeight: 700 }}>{u.underlying}</td>
                  <td style={td()}>{u.trades}</td>
                  <td style={{ ...td(), color: col(u.gross) }}>{signed(u.gross)}</td>
                  <td style={{ ...td(), color: "var(--text-muted)" }}>{inr(u.charges)}</td>
                  <td style={{ ...td(), color: col(u.net), fontWeight: 700 }}>{signed(u.net)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div style={{ ...cardStyle, padding: 0, overflowX: "auto", marginBottom: "8px" }}>
        <div style={{ padding: "10px 12px 4px", fontSize: "12px", fontWeight: 700, color: "var(--text-primary)" }}>Closed trades, FY {data.fy} ({data.rows.length})</div>
        <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "960px" }}>
          <thead><tr>{["CLOSED", "CONTRACT", "AT", ...(many ? ["NAME"] : []), "SIDE", "OPENED", "QTY", "ENTRY", "EXIT", "P&L"].map((h, i) => <th key={h} style={th(i < (many ? 5 : 4) ? "left" : "right")}>{h}</th>)}</tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td style={td("left")}>{r.closed}{r.how !== "trade" && <span style={{ color: "var(--warning)", fontSize: "10px" }} title={r.how === "expiry_assumed_zero" ? "Open at expiry — counted as expired worthless" : "Settled at expiry at the price entered"}> {r.how === "expiry_assumed_zero" ? "expired (0)" : "expiry"}</span>}</td>
                <td style={{ ...td("left"), fontWeight: 600 }} title={r.symbol}>{contract(r)}</td>
                <td style={td("left")}>{brokerChip(r.broker)}</td>
                {many && <td style={{ ...td("left"), color: "var(--text-secondary)" }}>{r.name}</td>}
                <td style={{ ...td("left"), color: r.side === "long" ? "var(--positive)" : "var(--negative)" }}>{r.side}</td>
                <td style={td()}>{r.opened}</td>
                <td style={td()}>{qtyTxt(r.qty)}</td>
                <td style={td()}>{inr(r.entry, 2)}</td>
                <td style={td()}>{inr(r.exit, 2)}</td>
                <td style={{ ...td(), color: col(r.pnl), fontWeight: 700 }}>{signed(r.pnl)}</td>
              </tr>
            ))}
            {data.rows.length === 0 && <tr><td colSpan={many ? 10 : 9} style={{ ...td("left"), color: "var(--text-faint)", fontStyle: "italic" }}>No trades closed in FY {data.fy}.</td></tr>}
          </tbody>
        </table>
        {data.rows.length > 100 && !showAll && <div style={{ padding: "8px 12px" }}><button onClick={() => setShowAll(true)} style={btnStyle()}>Show all {data.rows.length}</button></div>}
      </div>
      {data.notes.map((n, i) => <div key={i} style={{ fontSize: "11px", color: "var(--text-faint)", lineHeight: 1.6 }}>{n}</div>)}
    </div>
  );
}

// ─── A fund's holdings month by month ───────────────────────────────────────

interface FundMonthsResp {
  funds: { isin: string; scheme: string; type: "etf" | "mf"; months: number }[];
  isin: string | null; scheme?: string; amc?: string | null;
  months: { month: string; uploaded: boolean; as_of: string | null; equity_pct: number | null; stocks: number | null; filename: string | null }[];
  rows: { isin: string; symbol: string | null; name: string; industry: string | null; sector: string | null;
          weights: (number | null)[]; latest: number | null; change: number | null; status: string }[];
  changes: { month: string; against: string; added: { name: string; pct: number }[]; removed: { name: string; pct: number }[];
             up: { name: string; change: number }[]; down: { name: string; change: number }[] }[];
}

const monthLabel = (m: string) => new Date(`${m}-01T00:00:00`).toLocaleDateString("en-IN", { month: "short", year: "2-digit" });

function FundMonths({ setMsg }: { setMsg: (m: { text: string; tone: "ok" | "err" } | null) => void }) {
  const [isin, setIsin] = useState("");
  const [n, setN] = useState(12);
  const [data, setData] = useState<FundMonthsResp | null>(null);
  const [busy, setBusy] = useState(false);
  const [onlyHeld, setOnlyHeld] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const load = async () => {
    try {
      const d = await getJSON<FundMonthsResp>(`/api/portfolio/mf/portfolio-history?months=${n}${isin ? `&isin=${isin}` : ""}`);
      setData(d);
      if (!isin && d.isin) setIsin(d.isin);
    } catch (e: any) { setMsg({ text: `Fund history: ${e.message}`, tone: "err" }); }
  };
  useEffect(() => { load(); }, [isin, n]);
  const upload = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    e.target.value = "";
    if (!files.length || !isin) return;
    setBusy(true);
    const out: string[] = [];
    let bad = false;
    for (const f of files) {
      try {
        const r = await postJSON("/api/portfolio/mf/portfolio-file", { filename: f.name, content_b64: await fileB64(f), scheme_isin: isin });
        const m = r.matched?.[0];
        if (!m) { bad = true; out.push(`${f.name}: no matching section`); }
        else out.push(`${f.name}: ${monthLabel(m.as_of.slice(0, 7))}${m.date_found ? "" : " (date not found in the file — assumed last month)"}, ${m.stocks} stocks`);
      } catch (err: any) { bad = true; out.push(`${f.name}: ${err.message}`); }
    }
    setMsg({ text: out.join(" · "), tone: bad ? "err" : "ok" });
    setBusy(false);
    load();
  };
  if (!data) return <div style={{ ...cardStyle, fontSize: "13px", color: "var(--text-muted)" }}>Loading…</div>;
  if (!data.funds.length) {
    return <div style={{ ...cardStyle, fontSize: "13px", color: "var(--text-secondary)" }}>
      No portfolio files uploaded yet. Upload a fund's or ETF's month-end portfolio (All holdings → Upload fund portfolios, or open the fund's row);
      each month you upload becomes a column here.
    </div>;
  }
  const th = thStyle, td = tdStyle;
  const rows = data.rows.filter(r => !onlyHeld || r.latest);
  const last = data.changes[0];
  return (
    <div>
      <input ref={fileRef} type="file" multiple accept=".xlsx,.xls,.xlsm,.pdf,.zip" onChange={upload} style={{ display: "none" }} />
      <div style={{ display: "flex", gap: "8px", alignItems: "center", marginBottom: "10px", flexWrap: "wrap" }}>
        <select value={isin} onChange={e => setIsin(e.target.value)} style={{ ...inputStyle, maxWidth: "420px" }}>
          {data.funds.map(f => <option key={f.isin} value={f.isin}>{f.type === "etf" ? "ETF · " : ""}{f.scheme} ({f.months} month{f.months === 1 ? "" : "s"})</option>)}
        </select>
        <select value={n} onChange={e => setN(parseInt(e.target.value))} style={inputStyle}>
          {[6, 12, 24].map(x => <option key={x} value={x}>last {x} months</option>)}
        </select>
        <label style={{ fontSize: "12px", color: "var(--text-secondary)", display: "inline-flex", gap: "4px", alignItems: "center" }}>
          <input type="checkbox" checked={onlyHeld} onChange={e => setOnlyHeld(e.target.checked)} /> only stocks held in the latest month
        </label>
        <button onClick={() => fileRef.current?.click()} disabled={busy} style={{ ...btnStyle(), marginLeft: "auto" }}
          title="Any month's portfolio file for this fund — the month is read from the file. Several at once are fine.">
          {busy ? "Reading…" : "Upload a month for this fund"}</button>
      </div>

      {last && (
        <div style={{ ...cardStyle, padding: "10px 14px", marginBottom: "10px", fontSize: "12px", color: "var(--text-secondary)", lineHeight: 1.8 }}>
          <b style={{ color: "var(--text-primary)" }}>{monthLabel(last.month)} against {monthLabel(last.against)}</b>
          {last.added.length > 0 && <div><span style={{ color: "var(--positive)", fontWeight: 700 }}>New:</span> {last.added.map(a => `${a.name} ${a.pct.toFixed(2)}%`).join(", ")}</div>}
          {last.removed.length > 0 && <div><span style={{ color: "var(--negative)", fontWeight: 700 }}>Exited:</span> {last.removed.map(a => `${a.name} (was ${a.pct.toFixed(2)}%)`).join(", ")}</div>}
          {last.up.length > 0 && <div><span style={{ fontWeight: 700 }}>Raised:</span> {last.up.map(a => `${a.name} +${a.change.toFixed(2)}`).join(", ")}</div>}
          {last.down.length > 0 && <div><span style={{ fontWeight: 700 }}>Cut:</span> {last.down.map(a => `${a.name} ${a.change.toFixed(2)}`).join(", ")}</div>}
          {!last.added.length && !last.removed.length && !last.up.length && !last.down.length && <div>No change in the stocks held.</div>}
        </div>
      )}

      <div style={{ ...cardStyle, padding: 0, overflowX: "auto" }}>
        <table style={{ borderCollapse: "collapse", width: "100%", minWidth: `${420 + data.months.length * 70}px` }}>
          <thead>
            <tr>
              <th style={th("left")}>STOCK</th>
              <th style={th("left")}>SECTOR</th>
              {data.months.map(m => (
                <th key={m.month} style={{ ...th(), color: m.uploaded ? undefined : "var(--text-faint)" }}
                    title={m.uploaded ? `${fmtDate(m.as_of)} · ${m.stocks} stocks · ${m.equity_pct ?? "—"}% in shares · ${m.filename || ""}` : "No portfolio uploaded for this month"}>
                  {monthLabel(m.month)}
                  <div style={{ fontWeight: 400, fontSize: "9px" }}>{m.uploaded ? `${m.equity_pct ?? "—"}% eq` : "no upload"}</div>
                </th>
              ))}
              <th style={th()} title="Latest uploaded month against the one before it">CHANGE</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.isin}>
                <td style={{ ...td("left"), maxWidth: "240px", overflow: "hidden", textOverflow: "ellipsis" }} title={`${r.name} · ${r.isin}`}>
                  <b style={{ color: r.status === "exited" ? "var(--text-faint)" : "var(--text-primary)" }}>{r.symbol || r.name}</b>
                  {r.status === "new" && <span style={{ marginLeft: "6px", fontSize: "10px", fontWeight: 700, color: "var(--positive)" }}>NEW</span>}
                  {r.status === "exited" && <span style={{ marginLeft: "6px", fontSize: "10px", fontWeight: 700, color: "var(--negative)" }}>EXITED</span>}
                </td>
                <td style={{ ...td("left"), fontSize: "11px", color: "var(--text-secondary)", maxWidth: "160px", overflow: "hidden", textOverflow: "ellipsis" }} title={r.industry || ""}>{r.sector || "—"}</td>
                {r.weights.map((w, i) => (
                  <td key={i} style={{ ...td(), color: data.months[i].uploaded ? (w ? "var(--text-primary)" : "var(--text-faint)") : "var(--text-faint)",
                                       background: data.months[i].uploaded ? undefined : "rgba(var(--ink-rgb),0.03)" }}>
                    {data.months[i].uploaded ? (w ? w.toFixed(2) : "–") : ""}
                  </td>
                ))}
                <td style={{ ...td(), color: col(r.change), fontWeight: 700 }}>{r.change == null ? "—" : `${r.change > 0 ? "+" : ""}${r.change.toFixed(2)}`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ fontSize: "11px", color: "var(--text-faint)", marginTop: "6px", lineHeight: 1.6 }}>
        Weights are % of the fund's net assets in each month's file. A month with no upload stays empty, and changes are measured
        against the latest earlier month that has one. Upload older months to fill the history; the newest file stays the one Analytics uses.
      </div>
    </div>
  );
}

// ─── Filing view: each kind of income by Schedule CG's accrual windows ───────

function ItrSection({ itr, everyone, csvHref }: { itr: ItrView; everyone: boolean; csvHref: string }) {
  const th = thStyle, td = tdStyle;
  const [open, setOpen] = useState<string | null>(null);
  const cats = Array.isArray(itr.categories) ? itr.categories : [];
  const money = (v: number) => v ? signed(v) : <span style={{ color: "var(--text-faint)" }}>—</span>;
  return (
    <div style={{ ...cardStyle, padding: 0, marginBottom: "10px", overflowX: "auto" }}>
      <div style={{ padding: "12px 14px 6px", display: "flex", gap: "10px", alignItems: "baseline", flexWrap: "wrap" }}>
        <b style={{ fontSize: "13px", color: "var(--text-primary)" }}>Income-tax filing view — FY {itr.fy}</b>
        <span style={{ fontSize: "12px", color: "var(--text-secondary)" }}>
          {itr.act}{itr.assessment_year ? ` · AY ${itr.assessment_year}` : " · tax year " + itr.fy} · {itr.form}
        </span>
        <a href={csvHref} style={{ ...btnStyle(), textDecoration: "none", marginLeft: "auto" }}>Download filing sheet (CSV)</a>
      </div>
      {everyone && (
        <div style={{ margin: "0 14px 8px", padding: "6px 10px", borderRadius: "6px", fontSize: "12px", background: "var(--warning-bg)", color: "var(--warning)" }}>
          A return is filed per person: these add up everyone. Pick a name above for that person's own figures to file.
        </div>
      )}
      <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "1060px" }}>
        <thead>
          <tr>
            <th style={th("left")}>INCOME</th>
            <th style={th("left")}>SECTION · RATE</th>
            {itr.periods.map(p => <th key={p.key} style={th()} title={`Advance-tax instalment by ${p.pay_by}`}>{p.label.toUpperCase()}</th>)}
            <th style={th()}>TOTAL</th>
          </tr>
        </thead>
        <tbody>
          {cats.map(c => (
            <Fragment key={c.key}>
              <tr onClick={() => setOpen(open === c.key ? null : c.key)} style={{ cursor: "pointer" }}>
                <td style={{ ...td("left"), whiteSpace: "normal", maxWidth: "260px" }}>
                  <span style={{ color: "var(--text-faint)", marginRight: "5px" }}>{open === c.key ? "▾" : "▸"}</span>
                  <b style={{ color: "var(--text-primary)" }}>{c.label}</b>
                  <div style={{ fontSize: "10px", color: "var(--text-faint)", marginLeft: "15px" }}>{c.head}</div>
                </td>
                <td style={{ ...td("left"), whiteSpace: "normal", maxWidth: "240px", fontSize: "11px", color: "var(--text-secondary)" }}>
                  <b>{c.section.split(" — ")[0].split(";")[0]}</b><div>{c.rate}</div>
                </td>
                {c.periods.map((v, i) => <td key={i} style={{ ...td(), color: col(v) }}>{money(v)}</td>)}
                <td style={{ ...td(), color: col(c.net), fontWeight: 800 }}>{signed(c.net)}</td>
              </tr>
              {open === c.key && (
                <tr><td colSpan={8} style={{ padding: "8px 14px 12px 34px", background: "var(--surface-2)", borderBottom: "1px solid var(--border-subtle)", fontSize: "12px", color: "var(--text-secondary)", lineHeight: 1.7 }}>
                  <div><b>Section:</b> {c.section} ({c.act})</div>
                  <div><b>Where in the return:</b> {c.where}</div>
                  {c.key.startsWith("stcg") || c.key.startsWith("ltcg") ? (
                    <div><b>Figures:</b> full value of consideration {inr(c.sale_value)} · cost of acquisition {inr(c.cost)} · expenditure on transfer {inr(c.expenses)}
                      {c.stt ? ` · STT ${inr(c.stt)} (not deductible)` : ""} · gain {signed(c.net)} (gains {signed(c.gains)}, losses {signed(c.losses)}) · {c.count} sale(s)</div>
                  ) : (
                    <div><b>Figures:</b> turnover {inr(c.turnover)}{c.gross != null ? ` · gross profit ${signed(c.gross)}` : ""} · expenses {inr(c.expenses)}
                      {c.stt ? ` (STT ${inr(c.stt)}, deductible)` : ""} · net {signed(c.net)} · {c.count} closed trade(s)</div>
                  )}
                  {c.rate_split && <div><b>Rate split (FY 2024-25):</b> sold before 23 Jul 2024 {signed(c.rate_split.before)} · on or after {signed(c.rate_split.after)} — the return asks for them separately.</div>}
                </td></tr>
              )}
            </Fragment>
          ))}
          {cats.length === 0 && <tr><td colSpan={8} style={{ ...td("left"), color: "var(--text-faint)", fontStyle: "italic" }}>No gains or trading income this year.</td></tr>}
          {cats.length > 0 && (
            <>
              <tr>
                <td style={{ ...td("left"), fontWeight: 700 }} colSpan={2} title="Tax at the special rates (111A/112A/112 or 196/198/197) on gains accrued in each window, after the exemption and loss set-off; due in the instalment by the date shown">
                  Special-rate tax accrued (estimate) → pay by
                </td>
                {itr.advance_tax.map((a, i) => (
                  <td key={i} style={{ ...td(), fontWeight: 700, color: a.special_rate_tax ? "var(--warning)" : "var(--text-faint)" }}>
                    {a.special_rate_tax ? inr(a.special_rate_tax) : "—"}<div style={{ fontSize: "10px", fontWeight: 400, color: "var(--text-faint)" }}>by {a.pay_by}</div>
                  </td>
                ))}
                <td style={{ ...td(), fontWeight: 800, color: "var(--warning)" }}>{inr(itr.advance_tax.length ? itr.advance_tax[itr.advance_tax.length - 1].cumulative : 0)}</td>
              </tr>
              <tr>
                <td style={{ ...td("left"), color: "var(--text-secondary)" }} colSpan={2}>Slab-rate income in the window (add at your rate)</td>
                {itr.advance_tax.map((a, i) => <td key={i} style={{ ...td(), color: col(a.slab_income) }}>{money(a.slab_income)}</td>)}
                <td style={td()} />
              </tr>
            </>
          )}
        </tbody>
      </table>

      {itr.schedule_112a.length > 0 && (
        <div style={{ padding: "10px 0 0" }}>
          <div style={{ padding: "0 14px 4px", fontSize: "12px", fontWeight: 700, color: "var(--text-primary)" }}>
            Schedule 112A — long-term equity sales, scrip by scrip
          </div>
          <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "1060px" }}>
            <thead><tr>{["ACQUIRED", "ISIN", "NAME", "UNITS", "SALE PRICE / UNIT", "FULL VALUE OF CONSIDERATION", "COST OF ACQUISITION", "FMV 31 JAN 2018", "EXPENDITURE ON TRANSFER", "BALANCE"].map((h, i) => <th key={h} style={th(i < 3 ? "left" : "right")}>{h}</th>)}</tr></thead>
            <tbody>
              {itr.schedule_112a.map((x, i) => (
                <tr key={i}>
                  <td style={td("left")}>{x.acquired}</td>
                  <td style={td("left")}>{x.isin || "—"}</td>
                  <td style={{ ...td("left"), fontWeight: 600 }}><TypeChip t={x.kind} /> {x.name}</td>
                  <td style={td()}>{qtyTxt(Math.round(x.units * 1000) / 1000)}</td>
                  <td style={td()}>{x.units ? inr(x.sale_value / x.units, 2) : "—"}</td>
                  <td style={td()}>{inr(x.sale_value, 2)}</td>
                  <td style={td()}>{inr(x.cost, 2)}</td>
                  <td style={{ ...td(), color: x.acquired.startsWith("on or before") ? "var(--warning)" : "var(--text-faint)" }}>{x.acquired.startsWith("on or before") ? "fill in" : "n/a"}</td>
                  <td style={td()}>{inr(x.expenses, 2)}</td>
                  <td style={{ ...td(), color: col(x.gain), fontWeight: 700 }}>{signed(x.gain, 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div style={{ padding: "10px 14px 12px" }}>
        <div style={{ fontSize: "11px", fontWeight: 700, color: "var(--text-muted)", marginBottom: "4px", letterSpacing: "0.3px" }}>WHAT TO FILL, AND WHERE</div>
        {itr.checklist.map((c, i) => <div key={i} style={{ fontSize: "12px", color: "var(--text-secondary)", lineHeight: 1.6 }}>• {c}</div>)}
      </div>
    </div>
  );
}
