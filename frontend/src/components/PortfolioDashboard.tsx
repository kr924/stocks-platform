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
  key: string; symbol: string | null; isin: string | null; broker: string;
  qty: number; avg_cost: number; invested: number; first_buy: string; last_buy: string;
  long_term_qty: number; next_long_term: string | null;
  ltp: number | null; value: number | null; pnl: number | null; pnl_pct: number | null; day_change_pct: number | null;
}
interface HoldingsResp {
  rows: Holding[];
  totals: { invested: number; value: number | null; pnl: number | null; priced_invested: number; unpriced: number;
            realised_fy: string; realised_fy_capital: number; realised_fy_intraday: number };
  warnings: string[];
}
interface TaxRow {
  broker: string; symbol: string | null; isin: string | null; buy_date: string; sell_date: string; qty: number;
  buy_price: number; sell_price: number; gain: number; term: string; grandfathered?: boolean; cost: number; sale_value: number;
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
}
interface Trade {
  id: number; broker: string; symbol: string; isin: string | null; exchange: string | null; trade_date: string;
  trade_time: string | null; side: string; quantity: number; price: number; trade_id: string | null; source: string; note: string | null;
}
interface Summary {
  counts: Record<string, number>; span: Record<string, { first: string; last: string }>;
  financial_years: string[]; current_fy: string;
  upstox_sync: { added: number; fetched: number; errors: string[]; at: string } | null;
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

  const q = broker ? `broker=${broker}` : "";

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
    try { setTax(await getJSON<TaxResp>(`/api/portfolio/tax?fy=${fy}&${q}`)); } catch { /* ignore */ }
  };
  const loadTrades = async () => {
    try {
      setTrades(await getJSON<Trade[]>(`/api/portfolio/trades?limit=1000&${q}${tradeFilter.trim() ? `&symbol=${encodeURIComponent(tradeFilter.trim())}` : ""}`));
    } catch { /* ignore */ }
  };
  const reloadAll = () => { loadSummary(); loadHoldings(); loadTax(); if (tab === "trades") loadTrades(); };

  useEffect(() => { loadSummary(); }, []);
  useEffect(() => { loadHoldings(); }, [broker]);
  // Prices move: refresh holdings every 30 s while this view is open.
  useEffect(() => {
    const t = setInterval(loadHoldings, 30000);
    return () => clearInterval(t);
  }, [broker]);
  useEffect(() => { loadTax(); }, [fy, broker]);
  useEffect(() => { if (tab === "trades") loadTrades(); }, [tab, broker]);

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
        body: JSON.stringify({ broker: importBroker, filename: f.name, content }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.status);
      setMsg({
        text: `${f.name}: ${data.added} trade(s) added, ${data.duplicates} already on file`
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
          <button onClick={() => fileRef.current?.click()} disabled={busy === "import"} style={btn()}
            title="Zerodha: Console → Reports → Tradebook → Equity, pick the dates, download CSV. Importing the same file again adds nothing.">
            {busy === "import" ? "Importing…" : "Import tradebook"}
          </button>
          <input ref={fileRef} type="file" accept=".csv,text/csv" onChange={onFile} style={{ display: "none" }} />
          <button onClick={() => setShowAdd(v => !v)} style={btn(showAdd)}
            title="IPO allotments, bonus shares (price 0), or trades older than your files">Add trade</button>
        </div>
      </div>

      {msg && (
        <div style={{ marginBottom: "12px", padding: "8px 12px", borderRadius: "8px", fontSize: "12px",
                      background: msg.tone === "ok" ? "var(--positive-bg)" : "var(--negative-bg)",
                      color: msg.tone === "ok" ? "var(--positive-strong)" : "var(--negative-strong)" }}>
          {msg.text}
        </div>
      )}

      {showAdd && <AddTrade onDone={() => { setShowAdd(false); reloadAll(); }} />}

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
                <th style={th()}>QTY</th>
                <th style={th()}>AVG COST</th>
                <th style={th()}>LTP</th>
                <th style={{ ...th(), cursor: "pointer" }} onClick={() => setSortKey("day")}>DAY</th>
                <th style={th()}>INVESTED</th>
                <th style={{ ...th(), cursor: "pointer" }} onClick={() => setSortKey("value")}>VALUE</th>
                <th style={{ ...th(), cursor: "pointer" }} onClick={() => setSortKey("pnl")}>P&L</th>
                <th style={{ ...th(), cursor: "pointer" }} onClick={() => setSortKey("pnl_pct")}>P&L %</th>
                <th style={th()} title="Shares held more than 12 months: a sale is long-term (12.5% above ₹1.25 lakh a year) rather than short-term (20%)">LONG-TERM</th>
                <th style={th()}>FIRST BUY</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(h => (
                <tr key={`${h.broker}:${h.key}`}>
                  <td style={{ ...td("left"), fontWeight: 700, color: "var(--text-primary)" }} title={h.isin || ""}>{h.symbol || h.key}</td>
                  <td style={td("left")}><span className="wl-chip wl-b on" style={{ cursor: "default" }}>{brokerChip(h.broker)}</span></td>
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
                    {h.long_term_qty >= h.qty ? "all" : h.long_term_qty > 0 ? `${qtyTxt(h.long_term_qty)} of ${qtyTxt(h.qty)}` : `from ${h.next_long_term}`}
                  </td>
                  <td style={{ ...td(), color: "var(--text-muted)" }}>{h.first_buy}</td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr><td colSpan={12} style={{ ...td("left"), color: "var(--text-faint)", fontStyle: "italic" }}>No open positions on file.</td></tr>
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
            {fy && (
              <a href={`${API_BASE}/api/portfolio/realised.csv?fy=${fy}${broker ? `&broker=${broker}` : ""}`}
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
                      {["TYPE", "SYMBOL", "AT", "BOUGHT", "SOLD", "QTY", "BUY", "SELL", "COST", "SALE VALUE", "GAIN"].map((h, i) => (
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
                        <td style={{ ...td(), color: col(r.gain), fontWeight: 700 }}>{signed(r.gain)}</td>
                      </tr>
                    ))}
                    {tax.rows.length === 0 && (
                      <tr><td colSpan={11} style={{ ...td("left"), color: "var(--text-faint)", fontStyle: "italic" }}>No sales in FY {tax.fy}.</td></tr>
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
                  {["DATE", "SYMBOL", "AT", "SIDE", "QTY", "PRICE", "VALUE", "SOURCE", ""].map((h, i) => (
                    <th key={i} style={th(i < 4 ? "left" : "right")}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {trades.map(tr => (
                  <tr key={tr.id}>
                    <td style={td("left")}>{tr.trade_date}{tr.trade_time ? ` ${tr.trade_time.slice(0, 5)}` : ""}</td>
                    <td style={{ ...td("left"), fontWeight: 600 }} title={tr.isin || ""}>{tr.symbol}</td>
                    <td style={td("left")}>{brokerChip(tr.broker)}</td>
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
                  <tr><td colSpan={9} style={{ ...td("left"), color: "var(--text-faint)", fontStyle: "italic" }}>No trades.</td></tr>
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
function AddTrade({ onDone }: { onDone: () => void }) {
  const [f, setF] = useState({ broker: "zerodha", symbol: "", isin: "", trade_date: "", side: "BUY", quantity: "", price: "", note: "" });
  const [err, setErr] = useState("");
  const set = (k: keyof typeof f) => (e: ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });
  const submit = async () => {
    const quantity = parseFloat(f.quantity), price = parseFloat(f.price);
    if (!f.symbol.trim() || !f.trade_date || !(quantity > 0) || !(price >= 0)) { setErr("Symbol, date, quantity and price are needed."); return; }
    const res = await fetch(`${API_BASE}/api/portfolio/trades`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...f, quantity, price }),
    });
    if (!res.ok) { setErr((await res.json()).detail || "Could not add"); return; }
    onDone();
  };
  const inp = { padding: "6px 8px", fontSize: "12px", background: "var(--surface-1)", color: "var(--text-primary)", border: "1px solid var(--border-default)", borderRadius: "7px" };
  return (
    <div style={{ background: "var(--surface-2)", border: "1px solid var(--border-default)", borderRadius: "12px", padding: "12px", marginBottom: "14px",
                  display: "flex", flexWrap: "wrap", gap: "8px", alignItems: "center" }}>
      <select value={f.broker} onChange={set("broker")} style={inp}><option value="zerodha">Zerodha</option><option value="upstox">Upstox</option></select>
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
