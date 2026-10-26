import { useEffect, useMemo, useRef, useState } from "react";
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

const inr = (v: number | null | undefined, digits = 0) =>
  v == null || isNaN(v) ? "—" : `₹${v.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
const signed = (v: number | null | undefined, digits = 0) =>
  v == null || isNaN(v) ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}₹${Math.abs(v).toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
const pct = (v: number | null | undefined) => v == null || isNaN(v) ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;
const col = (v: number | null | undefined) => v == null || isNaN(v) ? "var(--text-faint)" : v > 0 ? "var(--positive)" : v < 0 ? "var(--negative)" : "var(--text-muted)";
const qtyTxt = (q: number) => Number.isInteger(q) ? q.toLocaleString("en-IN") : q.toFixed(3);
const brokerChip = (b: string) => b === "upstox" ? "U" : b === "zerodha" ? "Z" : b === "both" ? "U+Z" : "?";

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 200)}`);
  return res.json();
}

export function PortfolioDashboard() {
  const [broker, setBroker] = useState<BrokerFilter>("");
  const [tab, setTab] = useState<"holdings" | "tax" | "trades">("holdings");
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
  const reloadAll = () => { loadSummary(); loadHoldings(); loadTax(); if (tab === "trades") loadTrades(); };

  useEffect(() => { loadSummary(); }, []);
  useEffect(() => { loadHoldings(); }, [broker, who]);
  // Prices move: refresh holdings every 30 s while this view is open.
  useEffect(() => {
    const t = setInterval(loadHoldings, 30000);
    return () => clearInterval(t);
  }, [broker, who]);
  useEffect(() => { loadTax(); }, [fy, broker, taxName]);
  useEffect(() => { if (tab === "trades") loadTrades(); }, [tab, broker, who]);

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
  const empty = summary && Object.keys(summary.counts || {}).length === 0;
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
          3. Shares from an IPO, a bonus, or older than the files go in with <b>Add trade</b>.
        </div>
      )}

      {/* Tiles */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: "10px", marginBottom: "14px" }}>
        <Tile label="Invested" value={inr(t?.invested)} />
        <Tile label="Current value" value={inr(t?.value)} sub={t?.unpriced ? `${t.unpriced} without a live price` : undefined} />
        <Tile label="Unrealised P&L" value={signed(t?.pnl)} color={col(t?.pnl)}
              sub={t?.pnl != null && t.priced_invested ? pct(t.pnl / t.priced_invested * 100) : undefined} />
        <Tile label={`Realised FY ${t?.realised_fy || ""}`} value={signed(t?.realised_fy_capital)} color={col(t?.realised_fy_capital)}
              sub="delivery (capital gains)" />
        <Tile label={`Intraday FY ${t?.realised_fy || ""}`} value={signed(t?.realised_fy_intraday)} color={col(t?.realised_fy_intraday)}
              sub="speculative income" />
      </div>

      {!who && (hold?.by_name?.length ?? 0) > 1 && (
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
        {(["holdings", "tax", "trades"] as const).map(k => (
          <button key={k} onClick={() => setTab(k)} style={btn(tab === k)}>
            {k === "holdings" ? `Holdings (${rows.length})` : k === "tax" ? "Capital gains & tax" : "Trades"}
          </button>
        ))}
        <span style={{ marginLeft: "auto", fontSize: "11px", color: "var(--text-faint)" }}>
          {summary?.span?.upstox && `Upstox ${summary.span.upstox.first} → ${summary.span.upstox.last}`}
          {summary?.span?.upstox && summary?.span?.zerodha && " · "}
          {summary?.span?.zerodha && `Zerodha ${summary.span.zerodha.first} → ${summary.span.zerodha.last}`}
          {lastSync && ` · Upstox synced ${lastSync.toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}`}
        </span>
      </div>

      {(hold?.warnings?.length ?? 0) > 0 && (
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
        Holdings, P&L and tax break down by name. Zerodha IDs are read from the tradebook's file name; the Upstox ID from the connected login.
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
          <option value="zerodha">Zerodha</option><option value="upstox">Upstox</option>
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
