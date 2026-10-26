import { useEffect, useMemo, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE || (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1" ? "http://localhost:8000" : "");

/**
 * What the Stocks Tracker adds to its own table on the Holdings tab. The tab
 * keeps the watchlist's columns (day high, sentiment, buy/sell quantity, 2m
 * signal…); averages, invested amount and P&L live on the Portfolio page.
 * Added here: the day's P&L across holdings (summary strip), a Held column
 * (each purchase as "qty · days"), and Sell for Upstox holdings.
 *
 * Positions come from /api/portfolio/positions — the trade ledger reconciled
 * with Upstox's own holdings, re-read at most every 10 minutes — which also
 * keeps the H / U / Z chips in step.
 */

export interface TrackerItem {
  id: number; symbol: string; name: string; instrument_key: string;
  last_price: number; change: number; close?: number; is_holding?: boolean; broker?: string | null;
}

export interface Lot { date: string | null; qty: number; days: number | null; long_term: boolean | null; broker: string }
export interface Position {
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

/** Positions by symbol, polled every 60 s while `active`. */
export function useHoldingPositions(active: boolean) {
  const [positions, setPositions] = useState<Position[]>([]);
  const [snapAt, setSnapAt] = useState<string | null>(null);
  const [upErr, setUpErr] = useState<string | null>(null);
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
    if (!active) return;
    load();
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  }, [active]);
  const bySymbol = useMemo(() => {
    const m: Record<string, Position> = {};
    for (const p of positions) if (p.symbol) m[p.symbol.toUpperCase()] = p;
    return m;
  }, [positions]);
  return { bySymbol, snapAt, upErr, reload: load };
}

/** Today's P&L across holdings: quantity × (live price − previous close). */
export function HoldingsSummary({ items, bySymbol, snapAt, upErr, onSynced }: {
  items: TrackerItem[]; bySymbol: Record<string, Position>; snapAt: string | null; upErr: string | null;
  onSynced: () => void;
}) {
  const [syncing, setSyncing] = useState(false);
  let day = 0, base = 0, counted = 0;
  for (const it of items) {
    const p = bySymbol[it.symbol.toUpperCase()];
    if (!p || !(it.last_price > 0) || !it.close) continue;
    day += p.qty * (it.last_price - it.close);
    base += p.qty * it.close;
    counted += 1;
  }
  const sync = async () => {
    setSyncing(true);
    try { await fetch(`${API_BASE}/api/portfolio/sync/upstox`, { method: "POST" }); } catch { /* ignore */ }
    onSynced();
    setSyncing(false);
  };
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: "14px", alignItems: "center", padding: "4px 12px 10px", fontSize: "12px" }}>
      <span style={{ color: "var(--text-muted)" }}
            title={`Quantity × (live price − previous close), over ${counted} holding(s) with a quantity on file`}>
        Day's P&L{" "}
        <b style={{ color: col(day), fontSize: "14px" }}>{counted ? signed(day) : "—"}</b>
        {counted && base ? <b style={{ color: col(day) }}> ({pct(day / base * 100)})</b> : null}
      </span>
      <span style={{ marginLeft: "auto", color: "var(--text-faint)", fontSize: "11px" }}
            title={upErr ? `Upstox holdings: ${upErr}` : "Upstox holdings are re-read at most every 10 minutes; Zerodha comes from the tradebook imported on the Portfolio page."}>
        {upErr ? "⚠ Upstox holdings not read" : "Upstox holdings"}
        {snapAt ? ` · ${new Date(snapAt).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}` : ""}
      </span>
      <button onClick={sync} disabled={syncing} className="wl-chip" style={{ height: "22px", fontSize: "11px", padding: "0 8px" }}>
        {syncing ? "Syncing…" : "Sync Upstox"}
      </button>
    </div>
  );
}

/** Each purchase as "qty · days" (LT past 12 months, "older" when undated). */
export function HeldCell({ pos }: { pos?: Position }) {
  if (!pos) {
    return <span style={{ color: "var(--text-faint)", fontSize: "11px" }}
                 title="Marked held by hand, or not in the Upstox holdings / imported tradebook">—</span>;
  }
  const lots = Array.isArray(pos.lots) ? pos.lots : [];
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: "3px", fontSize: "11px" }}
         title={lots.map(l => `${l.broker === "upstox" ? "U" : "Z"} ${q(l.qty)} bought ${l.date ?? "before the trade history"}${l.days != null ? ` (${l.days} days)` : ""}${l.long_term ? " — long-term" : ""}`).join("\n")}>
      {lots.slice(0, 3).map((l, i) => (
        <span key={i} style={{ padding: "1px 5px", borderRadius: "4px", whiteSpace: "nowrap",
                               background: l.long_term ? "var(--positive-bg)" : "rgba(var(--ink-rgb),0.05)",
                               color: l.long_term ? "var(--positive-strong)" : "var(--text-secondary)" }}>
          {q(l.qty)} · {l.days == null ? "older" : `${l.days}d`}{l.long_term ? " LT" : ""}
        </span>
      ))}
      {lots.length > 3 && <span style={{ color: "var(--text-faint)" }}>+{lots.length - 3}</span>}
    </div>
  );
}

/** Sell button for a holding with shares at Upstox; opens the confirm dialog. */
export function SellButton({ item, pos, onDone }: { item: TrackerItem; pos?: Position; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const up = pos?.brokers?.upstox;
  if (!pos || !up || up.qty < 1) return null;
  return (
    <>
      <button onClick={(e) => { e.stopPropagation(); setOpen(true); }} title="Sell at Upstox"
        style={{ fontSize: "10px", fontWeight: 800, padding: "3px 8px", borderRadius: "6px", cursor: "pointer",
                 border: "1px solid rgba(var(--negative-rgb),0.45)", background: "var(--negative-bg)", color: "var(--negative)" }}>
        Sell
      </button>
      {open && <SellDialog item={item} pos={pos} onClose={() => setOpen(false)} onDone={() => { setOpen(false); onDone(); }} />}
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
    <div onClick={(e) => { e.stopPropagation(); onClose(); }} style={{ position: "fixed", inset: 0, background: "rgba(var(--shadow-rgb),0.45)", zIndex: 1000,
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
