import { useEffect, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE || (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1" ? "http://localhost:8000" : "");

interface Group {
  group: string; n: number; direction: string;
  d0_n: number; d0_avg: number | null; d0_median: number | null; d0_hit: number | null; d0_up?: number | null;
  d1_n: number; d1_avg: number | null; d1_median: number | null; d1_hit: number | null; d1_up?: number | null;
  d5_n: number; d5_avg: number | null; d5_median: number | null; d5_hit: number | null; d5_up?: number | null;
}
interface Row {
  pending_id: number; symbol: string; trade_date: string; group: string; verdict: string;
  trend: string | null; provider: string | null; base_price: number | null; d0_date: string | null;
  ret_d0: number | null; ret_d1: number | null; ret_d5: number | null; note: string | null;
}
interface Summary {
  days: number; groups: Group[]; rows: Row[]; filling: boolean;
  last_fill: { at?: string; fetched?: number }; missing_price: number;
}

const pctTxt = (v: number | null | undefined) => v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(1)}%`;
const pctCol = (v: number | null | undefined) => v == null ? "var(--text-faint)" : v > 0 ? "var(--positive)" : v < 0 ? "var(--negative)" : "var(--text-muted)";
const groupCol = (g: string) => g === "BUY" || g === "BEATS ESTIMATES" ? "var(--positive)"
  : g === "SELL" || g === "MISSES ESTIMATES" ? "var(--negative)" : g === "BUY withheld" ? "var(--warning)" : "var(--text-muted)";

/**
 * How each AI verdict on a result has done afterwards: the average move and
 * the hit rate at the first close after the result, the next session and five
 * sessions on, measured from the price when the filing landed
 * (services/verdict_scorecard.py). NA and withheld BUYs have no direction;
 * their average move is the yardstick a BUY has to beat.
 */
export function VerdictScorecard({ onClose }: { onClose: () => void }) {
  const [days, setDays] = useState<number>(30);
  const [data, setData] = useState<Summary | null>(null);
  const [err, setErr] = useState("");
  const [showRows, setShowRows] = useState<string>("");

  const load = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/trading/scorecard?days=${days}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
      setErr("");
    } catch (e: any) {
      setErr(String(e?.message || e));
    }
  };

  useEffect(() => { load(); }, [days]);
  // While closes are being fetched, look again every 10 s.
  useEffect(() => {
    if (!data?.filling) return;
    const t = setTimeout(load, 10000);
    return () => clearTimeout(t);
  }, [data]);

  const refresh = async () => {
    try { await fetch(`${API_BASE}/api/trading/scorecard/refresh?days=${days}`, { method: "POST" }); } catch { /* offline */ }
    setTimeout(load, 1500);
  };

  const groups = Array.isArray(data?.groups) ? data!.groups : [];
  const rows = Array.isArray(data?.rows) ? data!.rows : [];
  const cell = { padding: "5px 8px", textAlign: "right" as const, whiteSpace: "nowrap" as const };
  const horizon = (g: Group, h: "d0" | "d1" | "d5") => {
    const avg = g[`${h}_avg` as const];
    const n = g[`${h}_n` as const];
    const hit = g[`${h}_hit` as const];
    const up = (g as any)[`${h}_up`] as number | null | undefined;
    return (
      <td style={cell}>
        <span style={{ fontWeight: 700, color: pctCol(avg) }}>{pctTxt(avg)}</span>
        <span style={{ color: "var(--text-faint)", fontSize: "10px" }}>
          {n ? (hit != null ? ` · ${hit}% right` : up != null ? ` · ${up}% up` : "") + ` · n=${n}` : ""}
        </span>
      </td>
    );
  };

  return (
    <div style={{ background: "var(--surface-2)", border: "1px solid var(--border-default)", borderRadius: "10px", padding: "12px 14px", marginBottom: "12px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap", marginBottom: "8px" }}>
        <span style={{ fontSize: "13px", fontWeight: 700, color: "var(--text-primary)" }}>AI verdict scorecard</span>
        <span style={{ fontSize: "11px", color: "var(--text-muted)" }}>move from the price when the filing landed</span>
        <select value={days} onChange={e => setDays(Number(e.target.value))}
          style={{ marginLeft: "auto", fontSize: "11px", padding: "3px 6px", background: "var(--surface-1)", color: "var(--text-primary)", border: "1px solid var(--border-default)", borderRadius: "6px" }}>
          {[30, 60, 90, 120].map(d => <option key={d} value={d}>last {d} days</option>)}
        </select>
        <button onClick={refresh} disabled={!!data?.filling}
          style={{ fontSize: "11px", padding: "3px 8px", background: "transparent", color: "var(--text-secondary)", border: "1px solid var(--border-default)", borderRadius: "6px", cursor: data?.filling ? "default" : "pointer" }}>
          {data?.filling ? "Fetching closes…" : "Fetch closes"}
        </button>
        <button onClick={onClose}
          style={{ fontSize: "11px", padding: "3px 8px", background: "transparent", color: "var(--text-muted)", border: "1px solid var(--border-default)", borderRadius: "6px", cursor: "pointer" }}>
          Close
        </button>
      </div>
      {err && <div style={{ fontSize: "11px", color: "var(--negative)" }}>Could not load the scorecard: {err}</div>}
      {!err && groups.length === 0 && (
        <div style={{ fontSize: "11px", color: "var(--text-faint)", fontStyle: "italic" }}>
          {data ? "No analysed results with a price at announcement in this period yet." : "Loading…"}
        </div>
      )}
      {groups.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ borderCollapse: "collapse", fontSize: "11px", width: "100%", minWidth: "560px" }}>
            <thead>
              <tr>
                {["Verdict", "Filings", "First close after", "Next session", "5 sessions on"].map((h, i) => (
                  <th key={h} style={{ textAlign: i === 0 ? "left" : "right", padding: "4px 8px", color: "var(--text-muted)", fontWeight: 600, fontSize: "10px", borderBottom: "1px solid var(--border-subtle)" }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {groups.map(g => (
                <tr key={g.group} onClick={() => setShowRows(showRows === g.group ? "" : g.group)} style={{ cursor: "pointer" }}
                    title="Show the filings">
                  <td style={{ padding: "5px 8px", fontWeight: 700, color: groupCol(g.group) }}>{g.group}</td>
                  <td style={cell}>{g.n}</td>
                  {horizon(g, "d0")}
                  {horizon(g, "d1")}
                  {horizon(g, "d5")}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {showRows && (
        <div style={{ marginTop: "8px", maxHeight: "220px", overflowY: "auto" }}>
          <table style={{ borderCollapse: "collapse", fontSize: "11px", width: "100%" }}>
            <tbody>
              {rows.filter(r => r.group === showRows).map(r => (
                <tr key={r.pending_id}>
                  <td style={{ padding: "3px 8px", fontWeight: 700, color: "var(--text-primary)" }}>{r.symbol}</td>
                  <td style={{ padding: "3px 8px", color: "var(--text-muted)" }}>{r.trade_date}</td>
                  <td style={{ padding: "3px 8px", color: "var(--text-muted)" }}>{r.trend ? `trend: ${r.trend}` : ""}</td>
                  <td style={{ ...cell, color: pctCol(r.ret_d0) }}>{pctTxt(r.ret_d0)}</td>
                  <td style={{ ...cell, color: pctCol(r.ret_d1) }}>{pctTxt(r.ret_d1)}</td>
                  <td style={{ ...cell, color: pctCol(r.ret_d5) }}>{pctTxt(r.ret_d5)}</td>
                  <td style={{ padding: "3px 8px", color: "var(--text-faint)" }}>{r.note || (r.base_price == null ? "no price at result" : "")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div style={{ fontSize: "10px", color: "var(--text-faint)", marginTop: "6px", lineHeight: 1.5 }}>
        "Right" means the stock moved the way the verdict said (up for BUY, down for SELL). NA and withheld BUYs have no
        direction: their average is the yardstick a BUY has to beat. Closes are fetched after 16:00 each weekday.
        {data?.missing_price ? ` ${data.missing_price} filing(s) had no price at announcement and are not counted.` : ""}
      </div>
    </div>
  );
}
