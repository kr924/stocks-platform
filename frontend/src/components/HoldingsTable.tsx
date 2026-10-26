import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

const API_BASE = import.meta.env.VITE_API_BASE || (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1" ? "http://localhost:8000" : "");

/**
 * The Stocks Tracker's Holdings tab: what you own at Upstox and Zerodha,
 * with quantity, average price, amount invested, current amount, P&L and how
 * long each lot has been held. Prices are the tracker's live quotes; the
 * positions come from /api/portfolio/positions (the trade ledger reconciled
 * with Upstox's own holdings, re-read at most every 10 minutes), which also
 * keeps the H / U / Z chips in step. Upstox holdings can be sold from here.
 */

export interface TrackerItem {
  id: number; symbol: string; name: string; instrument_key: string;
  last_price: number; change: number; is_holding?: boolean; broker?: string | null;
}

interface Lot { date: string | null; qty: number; days: number | null; long_term: boolean | null; broker: string }
interface Position {
  key: string; symbol: string | null; isin: string | null; instrument_key: string;
  qty: number; avg: number | null; invested: number; broker: string; lots: Lot[];
  brokers: Record<string, { qty: number; avg: number; invested: number; source: string }>;
}

const inr = (v: number | null | undefined, d = 0) =>
  v == null || isNaN(v) ? "—" : `₹${v.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d })}`;
const signed = (v: number | null | undefined) =>
  v == null || isNaN(v) ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}₹${Math.abs(v).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
const pct = (v: number | null | undefined) => v == null || isNaN(v) ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;
const col = (v: number | null | undefined) => v == null || isNaN(v) ? "var(--text-faint)" : v > 0 ? "var(--positive)" : v < 0 ? "var(--negative)" : "var(--text-muted)";
const q = (n: number) => Number.isInteger(n) ? String(n) : n.toFixed(2);

export function HoldingsTable({ items, symbolCell, onRowClick, onChanged }: {
  items: TrackerItem[];
  symbolCell: (item: TrackerItem) => ReactNode;
  onRowClick: (item: TrackerItem) => void;
  onChanged: () => void;
}) {
  const [positions, setPositions] = useState<Position[]>([]);
  const [snapAt, setSnapAt] = useState<string | null>(null);
  const [upErr, setUpErr] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [sellFor, setSellFor] = useState<{ item: TrackerItem; pos: Position } | null>(null);

  const load = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/portfolio/positions`);
      if (!res.ok) return;
      const data = await res.json();
      setPositions(Array.isArray(data.positions) ? data.positions : []);
      setSnapAt(data.upstox_snapshot_at || null);
      setUpErr(data.upstox_error || null);
    } catch { /* offline */ }
  };
  useEffect(() => {
    load();
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  }, []);

  const bySymbol = useMemo(() => {
    const m: Record<string, Position> = {};
    for (const p of positions) if (p.symbol) m[p.symbol.toUpperCase()] = p;
    return m;
  }, [positions]);

  const sync = async () => {
    setSyncing(true);
    try { await fetch(`${API_BASE}/api/portfolio/sync/upstox`, { method: "POST" }); } catch { /* shown by state */ }
    await load();
    onChanged();
    setSyncing(false);
  };

  const rows = items.map(item => {
    const p = bySymbol[item.symbol.toUpperCase()];
    const ltp = item.last_price > 0 ? item.last_price : null;
    const current = p && ltp ? p.qty * ltp : null;
    const pnl = current != null && p ? current - p.invested : null;
    return { item, p, ltp, current, pnl, pnlPct: pnl != null && p && p.invested ? pnl / p.invested * 100 : null };
  });
  const tot = rows.reduce((a, r) => ({
    invested: a.invested + (r.p && r.current != null ? r.p.invested : 0),
    current: a.current + (r.current ?? 0),
  }), { invested: 0, current: 0 });

  const th = (align: "left" | "right" = "right", w?: string) => ({ textAlign: align, width: w });
  const cell = (align: "left" | "right" = "right") => ({ textAlign: align, fontSize: "12px", whiteSpace: "nowrap" as const });

  return (
    <>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "14px", alignItems: "center", padding: "6px 12px 10px", fontSize: "12px" }}>
        <span style={{ color: "var(--text-muted)" }}>Invested <b style={{ color: "var(--text-primary)" }}>{inr(tot.invested)}</b></span>
        <span style={{ color: "var(--text-muted)" }}>Current <b style={{ color: "var(--text-primary)" }}>{inr(tot.current)}</b></span>
        <span style={{ color: "var(--text-muted)" }}>P&L <b style={{ color: col(tot.current - tot.invested) }}>
          {signed(tot.current - tot.invested)} {tot.invested ? `(${pct((tot.current - tot.invested) / tot.invested * 100)})` : ""}</b></span>
        <span style={{ marginLeft: "auto", color: "var(--text-faint)", fontSize: "11px" }}
              title={upErr ? `Upstox holdings: ${upErr}` : "Upstox holdings are re-read at most every 10 minutes; Zerodha comes from the tradebook you import on the Portfolio page."}>
          {upErr ? "⚠ Upstox holdings not read — " : "Upstox holdings "}
          {snapAt ? `as of ${new Date(snapAt).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}` : ""}
        </span>
        <button onClick={sync} disabled={syncing} className="wl-chip" style={{ height: "22px", fontSize: "11px", padding: "0 8px" }}>
          {syncing ? "Syncing…" : "Sync Upstox"}
        </button>
      </div>
      {items.length === 0 ? (
        <div className="empty-state">
          No holdings yet. They fill in from Upstox automatically (Sync Upstox to do it now) and from a Zerodha tradebook
          imported on the 💼 Portfolio page.
        </div>
      ) : (
        <div className="table-scroll-container">
          <table className="stock-data-table">
            <thead>
              <tr>
                <th className="col-symbol">Symbol</th>
                <th style={th("left", "170px")}>Company</th>
                <th style={th("right", "86px")}>LTP</th>
                <th style={th("right", "70px")}>Day</th>
                <th style={th("right", "60px")}>Qty</th>
                <th style={th("right", "90px")}>Avg price</th>
                <th style={th("right", "100px")}>Invested</th>
                <th style={th("right", "100px")}>Current</th>
                <th style={th("right", "110px")}>P&L</th>
                <th style={th("left", "170px")} title="Quantity · days held, per purchase. LT = held over 12 months (long-term).">Held (qty · days)</th>
                <th style={th("right", "70px")}></th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ item, p, ltp, current, pnl, pnlPct }) => {
                const up = p?.brokers?.upstox;
                const lots = Array.isArray(p?.lots) ? p!.lots : [];
                const lotText = (l: Lot) => `${q(l.qty)} · ${l.days == null ? "older" : `${l.days}d`}`;
                return (
                  <tr key={item.id} onClick={() => onRowClick(item)}>
                    <td>{symbolCell(item)}</td>
                    <td><span className="truncate" style={{ color: "var(--text-secondary)", fontSize: "11px", display: "block" }}>{item.name}</span></td>
                    <td style={{ ...cell(), fontWeight: 700, color: "var(--text-primary)" }}>{ltp ? inr(ltp, 2) : "—"}</td>
                    <td style={{ ...cell(), color: col(item.change), fontWeight: 600 }}>{ltp ? pct(item.change) : "—"}</td>
                    <td style={cell()} title={p ? (Object.entries(p.brokers) as [string, Position["brokers"][string]][]).map(([b, v]) => `${b}: ${q(v.qty)} @ ${inr(v.avg, 2)} (${v.source.replace("_", " ")})`).join("\n") : ""}>
                      {p ? q(p.qty) : "—"}
                    </td>
                    <td style={cell()}>{p ? inr(p.avg, 2) : "—"}</td>
                    <td style={cell()}>{p ? inr(p.invested) : "—"}</td>
                    <td style={{ ...cell(), fontWeight: 700 }}>{inr(current)}</td>
                    <td style={{ ...cell(), color: col(pnl), fontWeight: 700 }}>
                      {signed(pnl)}<div style={{ fontSize: "10px", fontWeight: 600 }}>{pct(pnlPct)}</div>
                    </td>
                    <td style={{ fontSize: "11px", color: "var(--text-secondary)" }}
                        title={lots.map(l => `${l.broker === "upstox" ? "U" : "Z"} ${q(l.qty)} bought ${l.date ?? "before the trade history"}${l.days != null ? ` (${l.days} days)` : ""}${l.long_term ? " — long-term" : ""}`).join("\n")}>
                      {p ? (
                        <div style={{ display: "flex", flexWrap: "wrap", gap: "3px" }}>
                          {lots.slice(0, 3).map((l, i) => (
                            <span key={i} style={{ padding: "1px 5px", borderRadius: "4px", whiteSpace: "nowrap",
                                                   background: l.long_term ? "var(--positive-bg)" : "rgba(var(--ink-rgb),0.05)",
                                                   color: l.long_term ? "var(--positive-strong)" : "var(--text-secondary)" }}>
                              {lotText(l)}{l.long_term ? " LT" : ""}
                            </span>
                          ))}
                          {lots.length > 3 && <span style={{ color: "var(--text-faint)" }}>+{lots.length - 3}</span>}
                        </div>
                      ) : <span style={{ color: "var(--text-faint)" }} title="Marked held by hand, or not in the Upstox holdings / imported tradebook">no trades on file</span>}
                    </td>
                    <td style={{ textAlign: "right" }}>
                      {up && up.qty >= 1 && (
                        <button onClick={(e) => { e.stopPropagation(); setSellFor({ item, pos: p! }); }}
                          style={{ fontSize: "11px", fontWeight: 700, padding: "3px 10px", borderRadius: "6px", cursor: "pointer",
                                   border: "1px solid rgba(var(--negative-rgb),0.45)", background: "var(--negative-bg)", color: "var(--negative)" }}
                          title="Sell at Upstox">
                          Sell
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {sellFor && (
        <SellDialog item={sellFor.item} pos={sellFor.pos} onClose={() => setSellFor(null)}
                    onDone={() => { setSellFor(null); load(); onChanged(); }} />
      )}
    </>
  );
}

function SellDialog({ item, pos, onClose, onDone }: { item: TrackerItem; pos: Position; onClose: () => void; onDone: () => void }) {
  const max = Math.floor(pos.brokers.upstox?.qty || 0);
  const [qty, setQty] = useState(String(max));
  const [type, setType] = useState<"MARKET" | "LIMIT">("MARKET");
  const [price, setPrice] = useState(item.last_price ? item.last_price.toFixed(2) : "");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const n = parseInt(qty, 10);
  const lp = parseFloat(price);
  const valid = n >= 1 && n <= max && (type === "MARKET" || lp > 0);
  const est = (type === "LIMIT" ? lp : item.last_price) * (n || 0);

  const submit = async () => {
    setBusy(true);
    try {
      const res = await fetch(`${API_BASE}/api/portfolio/sell`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: item.symbol, quantity: n, order_type: type, limit_price: type === "LIMIT" ? lp : null }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.status);
      setResult({ ok: !!data.success, text: data.success ? `Order placed (${data.order_id}). Holdings update once it fills.` : `Not placed: ${data.message}` });
      if (data.success) setTimeout(onDone, 2500);
    } catch (e: any) {
      setResult({ ok: false, text: `Not placed: ${e.message}` });
    } finally { setBusy(false); }
  };

  const inp = { padding: "7px 9px", fontSize: "13px", background: "var(--surface-1)", color: "var(--text-primary)",
                border: "1px solid var(--border-default)", borderRadius: "7px", width: "100%" };
  return (
    <div onClick={onClose} style={{ position: "fixed", inset: 0, background: "rgba(var(--shadow-rgb),0.45)", zIndex: 1000,
                                   display: "flex", alignItems: "center", justifyContent: "center", padding: "16px" }}>
      <div onClick={e => e.stopPropagation()} style={{ background: "var(--surface-1)", border: "1px solid var(--border-default)",
                                                      borderRadius: "12px", padding: "18px", width: "360px", maxWidth: "100%",
                                                      boxShadow: "0 12px 40px rgba(var(--shadow-rgb),0.35)" }}>
        <div style={{ fontSize: "15px", fontWeight: 800, color: "var(--text-primary)", marginBottom: "4px" }}>Sell {item.symbol} on Upstox</div>
        <div style={{ fontSize: "12px", color: "var(--text-muted)", marginBottom: "12px" }}>
          {max} held at Upstox · avg {inr(pos.brokers.upstox?.avg, 2)} · LTP {inr(item.last_price, 2)}
        </div>
        <label style={{ fontSize: "11px", color: "var(--text-muted)" }}>Quantity (max {max})</label>
        <input value={qty} onChange={e => setQty(e.target.value.replace(/[^0-9]/g, ""))} inputMode="numeric" style={{ ...inp, marginBottom: "10px" }} />
        <div style={{ display: "flex", gap: "6px", marginBottom: "10px" }}>
          {(["MARKET", "LIMIT"] as const).map(t => (
            <button key={t} onClick={() => setType(t)}
              style={{ flex: 1, padding: "7px", fontSize: "12px", fontWeight: 700, borderRadius: "7px", cursor: "pointer",
                       border: `1px solid ${type === t ? "rgba(var(--accent-rgb),0.5)" : "var(--border-default)"}`,
                       background: type === t ? "var(--accent-bg)" : "transparent", color: type === t ? "var(--accent)" : "var(--text-secondary)" }}>
              {t === "MARKET" ? "Market" : "Limit"}
            </button>
          ))}
        </div>
        {type === "LIMIT" && (
          <>
            <label style={{ fontSize: "11px", color: "var(--text-muted)" }}>Limit price</label>
            <input value={price} onChange={e => setPrice(e.target.value.replace(/[^0-9.]/g, ""))} inputMode="decimal" style={{ ...inp, marginBottom: "10px" }} />
          </>
        )}
        <div style={{ fontSize: "12px", color: "var(--text-secondary)", marginBottom: "12px" }}>
          About {inr(est)} · delivery (CNC) sell · real order, placed immediately
        </div>
        {result && (
          <div style={{ fontSize: "12px", marginBottom: "10px", color: result.ok ? "var(--positive-strong)" : "var(--negative-strong)" }}>{result.text}</div>
        )}
        <div style={{ display: "flex", gap: "8px" }}>
          <button onClick={onClose} style={{ flex: 1, padding: "8px", fontSize: "13px", borderRadius: "8px", cursor: "pointer",
                                             border: "1px solid var(--border-default)", background: "transparent", color: "var(--text-secondary)" }}>
            {result?.ok ? "Close" : "Cancel"}
          </button>
          {!result?.ok && (
            <button onClick={submit} disabled={!valid || busy}
              style={{ flex: 1, padding: "8px", fontSize: "13px", fontWeight: 800, borderRadius: "8px", cursor: valid && !busy ? "pointer" : "default",
                       border: "none", background: valid ? "var(--negative)" : "rgba(var(--negative-rgb),0.35)", color: "var(--on-accent)" }}>
              {busy ? "Placing…" : `Sell ${n || 0}`}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
