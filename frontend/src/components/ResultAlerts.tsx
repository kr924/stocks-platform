import { useEffect, useRef } from "react";

const API_BASE = import.meta.env.VITE_API_BASE || (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1" ? "http://localhost:8000" : "");

/**
 * Desktop notification + tone when a result or impact-news filing lands.
 *
 * Mounted once in App, so it works whichever view is open. It used to live
 * inside TradingDashboard, which App unmounts on Stocks Tracker or AI
 * Intelligence: with the alerts switch on, a filing arriving while you were on
 * another tab raised nothing (16 Oct 2026).
 *
 * Driven by the server's SSE stream ("pending_result_order", broadcast the
 * moment the order screen is created), not by the dashboard's 3-second poll.
 * If the stream is down, it falls back to polling today's pending list.
 *
 * The switch stays on the Order Decisions header (localStorage
 * "resultAlertsOn"); the header tells this component through a
 * "resultalerts" window event. Filings outside 09:00-15:30 are deferred and,
 * by design, never alert.
 */
export function ResultAlerts() {
  const onRef = useRef<boolean>(false);
  const seenRef = useRef<Set<number>>(new Set());
  const seededRef = useRef(false);
  const audioRef = useRef<AudioContext | null>(null);
  const batchRef = useRef<{ symbol: string; company?: string; title?: string; id: number }[]>([]);
  const batchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const titleRef = useRef<string>(document.title);
  const unseenRef = useRef(0);

  useEffect(() => {
    const read = () => {
      try { onRef.current = localStorage.getItem("resultAlertsOn") === "1"; } catch { onRef.current = false; }
    };
    read();
    const onToggle = () => read();
    window.addEventListener("resultalerts", onToggle);
    window.addEventListener("storage", onToggle);

    // Browsers only let audio start after a user gesture. After a reload the
    // switch is still on but the old AudioContext is gone, so any click on
    // the page re-arms it.
    const arm = () => {
      try {
        audioRef.current = audioRef.current || new AudioContext();
        if (audioRef.current.state === "suspended") audioRef.current.resume();
      } catch { /* no audio */ }
    };
    document.addEventListener("pointerdown", arm);

    const onVisible = () => {
      if (!document.hidden && unseenRef.current) {
        unseenRef.current = 0;
        document.title = titleRef.current;
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("resultalerts", onToggle);
      window.removeEventListener("storage", onToggle);
      document.removeEventListener("pointerdown", arm);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  const tone = () => {
    const ctx = audioRef.current;
    if (!ctx || ctx.state !== "running") return;
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
    } catch { /* ignore */ }
  };

  const flush = () => {
    batchTimer.current = null;
    const items = batchRef.current;
    batchRef.current = [];
    if (!items.length || !onRef.current) return;
    tone();
    if (document.hidden) {
      unseenRef.current += items.length;
      document.title = `(${unseenRef.current}) New result — ${titleRef.current}`;
    }
    if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
    try {
      // A unique tag every time: a reused tag (the old "result-batch")
      // replaced the previous toast silently, with no popup and no sound.
      const opts = (body: string, tag: string): NotificationOptions =>
        ({ body, tag, requireInteraction: false, renotify: true } as NotificationOptions);
      const n = items.length === 1
        ? new Notification(`${items[0].symbol} — new filing`,
            opts((items[0].company ? `${items[0].company}\n` : "") + (items[0].title || "").slice(0, 120),
                 `result-${items[0].id}`))
        : new Notification(`${items.length} filings just landed`,
            opts(items.slice(0, 8).map(i => i.symbol).join(", ") + (items.length > 8 ? "…" : ""),
                 `result-batch-${Date.now()}`));
      n.onclick = () => { window.focus(); n.close(); };
    } catch (err) {
      console.error("Result notification failed:", err);
    }
  };

  const announce = (row: { id: number; symbol: string; company_name?: string; title?: string;
                           status?: string; deferred?: boolean }) => {
    if (!row || typeof row.id !== "number" || seenRef.current.has(row.id)) return;
    seenRef.current.add(row.id);
    if (row.deferred || (row.status && row.status !== "pending")) return;
    batchRef.current.push({ id: row.id, symbol: row.symbol, company: row.company_name, title: row.title });
    // Collect a burst for a moment so twenty filings make one toast, not twenty.
    if (!batchTimer.current) batchTimer.current = setTimeout(flush, 1500);
  };

  useEffect(() => {
    let es: EventSource | null = null;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let closed = false;

    const istToday = () => new Date(Date.now() + 5.5 * 3600 * 1000).toISOString().slice(0, 10);

    // Fallback while the stream is down: today's pending list every 10 s.
    // The first read only seeds what is already there.
    const poll = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/trading/pending-results?status=pending&trade_date=${istToday()}`);
        if (!res.ok) return;
        const data = await res.json();
        const rows = Array.isArray(data.pending) ? data.pending : [];
        if (!seededRef.current) {
          rows.forEach((r: any) => seenRef.current.add(r.id));
          seededRef.current = true;
          return;
        }
        rows.forEach((r: any) => announce(r));
      } catch { /* offline */ }
    };

    const connect = () => {
      if (closed) return;
      try {
        es = new EventSource(`${API_BASE}/api/intelligence/stream`);
      } catch {
        if (!pollTimer) pollTimer = setInterval(poll, 10000);
        return;
      }
      es.onopen = () => {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      };
      es.addEventListener("pending_result_order", (e: MessageEvent) => {
        try { announce(JSON.parse(e.data)); } catch { /* malformed */ }
      });
      es.onerror = () => {
        es?.close();
        es = null;
        if (!pollTimer) { poll(); pollTimer = setInterval(poll, 10000); }
        retry = setTimeout(connect, 15000);
      };
    };

    poll();          // seed with what already exists, so a reload is silent
    connect();
    return () => {
      closed = true;
      es?.close();
      if (pollTimer) clearInterval(pollTimer);
      if (retry) clearTimeout(retry);
      if (batchTimer.current) clearTimeout(batchTimer.current);
    };
  }, []);

  return null;
}
