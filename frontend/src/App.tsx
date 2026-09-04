import React, { useState, useEffect, useRef } from "react";
import {
  Plus,
  Trash2,
  Search,
  RefreshCw,
  Brain,
  Newspaper,
  UserCheck,
  ExternalLink,
  X,
  Send,
  Briefcase,
  Layers,
  Play,
  Pause,
  TrendingUp
} from "lucide-react";
import { Chart } from "./components/Chart";
import { IntelligenceDashboard } from "./components/IntelligenceDashboard";
import { TradingDashboard } from "./components/TradingDashboard";

/**
 * Is it inside NSE trading hours right now? Mon-Fri, 09:00 to 15:35 IST.
 *
 * At module scope because more than one refresh loop asks. Returns true when
 * the clock cannot be read: refreshing needlessly is cheaper than a dashboard
 * that silently stops updating.
 */
function checkIsMarketHours(): boolean {
  try {
    const istStr = new Date().toLocaleString("en-US", { timeZone: "Asia/Kolkata" });
    const ist = new Date(istStr);
    const day = ist.getDay();
    if (day === 0 || day === 6) return false;
    const mins = ist.getHours() * 60 + ist.getMinutes();
    return mins >= 540 && mins <= 935;
  } catch {
    return true;
  }
}

const API_BASE = import.meta.env.VITE_API_BASE || (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1" ? "http://localhost:8000" : "");

interface AnalystRecommendation {
  analyst_firm: string;
  recommendation: string;
  date: string;
}

interface StockAnalysis {
  comment: string | null;
  resistance_levels: string | null;
  support_levels: string | null;
  recommendation: string | null;
  sector: string | null;
  analyst_recommendations?: AnalystRecommendation[];
  fetched_at: string | null;
}

interface WatchlistItem {
  id: number;
  symbol: string;
  name: string;
  instrument_key: string;
  last_price: number;
  change: number;
  close?: number;
  high?: number;
  low?: number;
  is_holding?: boolean;
  analysis?: StockAnalysis | null;
  depth_buy_pct?: number;
  depth_sell_pct?: number;
  total_buy_qty?: number;
  total_sell_qty?: number;
}

interface MoverItem {
  symbol: string;
  name: string;
  instrument_key: string;
  last_price: number;
  change: number;
  volume: number;
  open: number;
  high: number;
  low: number;
  close: number;
  analysis?: StockAnalysis | null;
  depth_buy_pct?: number;
  depth_sell_pct?: number;
  total_buy_qty?: number;
  total_sell_qty?: number;
}

interface StockDetail {
  instrument_key: string;
  name: string;
  quote: {
    last_price: number;
    volume: number;
    ohlc: {
      open: number;
      high: number;
      low: number;
      close: number;
    };
    depth?: {
      buy: Array<{ price: number; quantity: number; orders: number }>;
      sell: Array<{ price: number; quantity: number; orders: number }>;
    };
    depth_buy_pct?: number;
    depth_sell_pct?: number;
  };
  candles: any[];
  news: any[];
  news_fetched_at: string | null;
  ai_comment: string | null;
  ai_fetched_at: string | null;
  analysis?: StockAnalysis | null;
}


/**
 * Live/Paused switch for a table's automatic quote polling.
 *
 * Upstox meters quote requests per account, and the whole account shares one
 * allowance — so a table polling in the background is spending the budget the
 * results panel needs for the filing being decided on. Pausing a table stops
 * its automatic fetches; rows can still be refreshed one at a time, and the
 * whole table on demand.
 */
function LivePriceToggle({ paused, onToggle, onRefreshAll, label, name }: {
  paused: boolean;
  onToggle: () => void;
  onRefreshAll: () => void;
  label: string;
  name?: string;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
      <button
        onClick={onToggle}
        title={paused
          ? `Automatic price updates are off for the ${label}. Click to resume 10s polling.`
          : `Prices update every 10s. Click to pause and free up Upstox quota.`}
        style={{
          display: "inline-flex", alignItems: "center", gap: "5px",
          padding: "4px 10px", borderRadius: "6px", cursor: "pointer",
          fontSize: "10px", fontWeight: 700, letterSpacing: "0.3px",
          background: paused ? "rgba(216, 174, 100, 0.09)" : "rgba(91, 190, 147, 0.09)",
          border: `1px solid ${paused ? "rgba(216, 174, 100, 0.22)" : "rgba(91, 190, 147, 0.22)"}`,
          color: paused ? "var(--warning)" : "var(--positive)",
        }}
      >
        {paused ? <Play size={11} /> : <Pause size={11} />}
        {name ? `${name} · ` : ""}{paused ? "PAUSED" : "LIVE"}
      </button>
      {paused && (
        <button
          onClick={onRefreshAll}
          title={`Fetch prices for the whole ${label} once`}
          style={{
            display: "inline-flex", alignItems: "center", gap: "4px",
            padding: "4px 8px", borderRadius: "6px", cursor: "pointer",
            fontSize: "10px", fontWeight: 600,
            background: "transparent", border: "1px solid rgba(255,255,255,0.15)",
            color: "var(--text-secondary)",
          }}
        >
          <RefreshCw size={10} /> Refresh all
        </button>
      )}
    </div>
  );
}

const formatQty = (qty: number): string => {
  if (qty >= 10000000) return (qty / 10000000).toFixed(2) + "Cr";
  if (qty >= 100000) return (qty / 100000).toFixed(2) + "L";
  if (qty >= 1000) return (qty / 1000).toFixed(1) + "K";
  return qty.toString();
};

export default function App() {
  const [activeView, setActiveView] = useState<"tracker" | "intelligence">("tracker");
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [detailsMap, setDetailsMap] = useState<Record<string, StockDetail>>({});
  const [detailsLoading, setDetailsLoading] = useState<Record<string, boolean>>({});
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [chartPeriod, setChartPeriod] = useState<string>("1D");
  const [chartCandles, setChartCandles] = useState<any[]>([]);
  const [chartLoading, setChartLoading] = useState<boolean>(false);

  const [hoveredAnalysisKey, setHoveredAnalysisKey] = useState<{
    key: string;
    symbol: string;
    name: string;
    x: number;
    y: number;
  } | null>(null);

  const handleMouseEnter = (e: React.MouseEvent, item: WatchlistItem | MoverItem) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const width = 380; // tooltip width
    const height = 350; // estimated tooltip height

    let left = rect.left - 180; // try to center it
    if (left + width > window.innerWidth) {
      left = window.innerWidth - width - 16;
    }
    if (left < 16) {
      left = 16;
    }

    let top = rect.bottom + 8;
    if (top + height > window.innerHeight) {
      top = rect.top - height - 8;
      if (top < 16) {
        top = 16;
      }
    }

    setHoveredAnalysisKey({
      key: item.instrument_key,
      symbol: item.symbol,
      name: item.name,
      x: left,
      y: top
    });
  };

  const handleMouseLeave = () => {
    setHoveredAnalysisKey(null);
  };

  const [hoveredSentimentKey, setHoveredSentimentKey] = useState<{
    key: string;
    symbol: string;
    name: string;
    x: number;
    y: number;
  } | null>(null);

  const sentimentHistoryRef = useRef<Record<string, number[]>>({});
  const quantityHistoryRef = useRef<Record<string, { buy: number[]; sell: number[] }>>({});

  const handleSentimentMouseEnter = (e: React.MouseEvent, item: WatchlistItem | MoverItem) => {
    console.log("Hovered key:", item.instrument_key);
    console.log("Current sentiment history map:", sentimentHistoryRef.current);
    console.log("History for hovered key:", sentimentHistoryRef.current[item.instrument_key]);

    const rect = e.currentTarget.getBoundingClientRect();
    const width = 360; // tooltip width
    const height = 180; // tooltip height

    let left = rect.left - 130; // center it relative to cell
    if (left + width > window.innerWidth) {
      left = window.innerWidth - width - 16;
    }
    if (left < 16) {
      left = 16;
    }

    let top = rect.bottom + 8;
    if (top + height > window.innerHeight) {
      top = rect.top - height - 8;
      if (top < 16) {
        top = 16;
      }
    }

    setHoveredSentimentKey({
      key: item.instrument_key,
      symbol: item.symbol,
      name: item.name,
      x: left,
      y: top
    });
  };

  const handleSentimentMouseLeave = () => {
    setHoveredSentimentKey(null);
  };

  const get2MinRecommendation = (item: WatchlistItem | MoverItem) => {
    const key = item.instrument_key;
    const sentHistory = sentimentHistoryRef.current[key] || [];
    const qtyHistory = quantityHistoryRef.current[key] || { buy: [], sell: [] };

    const currentSent = sentHistory[sentHistory.length - 1] !== undefined ? sentHistory[sentHistory.length - 1] : 50;
    const currentBuyQty = item.total_buy_qty || 0;
    const currentSellQty = item.total_sell_qty || 0;
    const totalQty = currentBuyQty + currentSellQty;

    // If insufficient data points (less than 3 ticks = 30 seconds), display default neutral
    if (sentHistory.length < 3) {
      return {
        recommendation: "HOLD",
        badgeClass: "hold",
        confidence: "Insuff. Data",
        score: 0,
        explanation: "Collecting real-time order book ticks to establish trend baseline (takes ~30s)."
      };
    }

    let score = 0;

    // 1. Sentiment Level Component (current buyer/seller distribution)
    if (currentSent >= 75) score += 1.5;
    else if (currentSent >= 60) score += 1.0;
    else if (currentSent <= 25) score -= 1.5;
    else if (currentSent <= 40) score -= 1.0;

    // 2. Sentiment Delta Component (2-minute trend)
    const startingSent = sentHistory[0];
    const sentDelta = currentSent - startingSent;
    if (sentDelta >= 15) score += 2.0;
    else if (sentDelta >= 5) score += 1.0;
    else if (sentDelta <= -15) score -= 2.0;
    else if (sentDelta <= -5) score -= 1.0;

    // 3. Current Quantity Dominance
    if (totalQty > 0) {
      const buyRatio = currentBuyQty / totalQty;
      if (buyRatio >= 0.75) score += 1.5;
      else if (buyRatio >= 0.60) score += 1.0;
      else if (buyRatio <= 0.25) score -= 1.5;
      else if (buyRatio <= 0.40) score -= 1.0;
    }

    // 4. Quantity Accumulation Trend
    if (qtyHistory.buy.length >= 3) {
      const startBuyQty = qtyHistory.buy[0];
      const startSellQty = qtyHistory.sell[0];
      const endBuyQty = qtyHistory.buy[qtyHistory.buy.length - 1];
      const endSellQty = qtyHistory.sell[qtyHistory.sell.length - 1];

      const buyQtyChange = endBuyQty - startBuyQty;
      const sellQtyChange = endSellQty - startSellQty;

      if (buyQtyChange > 0 && buyQtyChange > sellQtyChange) score += 1.0;
      else if (sellQtyChange > 0 && sellQtyChange > buyQtyChange) score -= 1.0;
    }

    // Determine final advice
    let recommendation = "HOLD";
    let badgeClass = "hold";
    let confidence = "Low";

    if (score >= 3.5) {
      recommendation = "STRONG BUY";
      badgeClass = "strong-buy";
      confidence = "High";
    } else if (score >= 1.5) {
      recommendation = "BUY";
      badgeClass = "buy";
      confidence = "Medium";
    } else if (score <= -3.5) {
      recommendation = "STRONG SELL";
      badgeClass = "strong-sell";
      confidence = "High";
    } else if (score <= -1.5) {
      recommendation = "SELL";
      badgeClass = "sell";
      confidence = "Medium";
    } else {
      recommendation = "HOLD";
      badgeClass = "hold";
      confidence = "Low";
    }

    const sentTrendText = sentDelta > 0 ? "improving" : sentDelta < 0 ? "weakening" : "stable";
    const qtyDominance = currentBuyQty > currentSellQty ? "Buyer volume dominance" : currentSellQty > currentBuyQty ? "Seller volume dominance" : "Balanced volume";
    const explanation = `Sentiment is ${sentTrendText} (${sentDelta >= 0 ? "+" : ""}${sentDelta.toFixed(0)}% delta). ${qtyDominance} with total interest of ${formatQty(totalQty)}.`;

    return {
      recommendation,
      badgeClass,
      confidence,
      score,
      explanation
    };
  };

  // Chatbot State
  const [chatTab, setChatTab] = useState<"analysis" | "chat" | "depth">("analysis");
  const [chatHistory, setChatHistory] = useState<Record<string, Array<{ role: "user" | "assistant"; content: string }>>>({});
  const [chatInput, setChatInput] = useState("");
  const [chatLoading, setChatLoading] = useState(false);
  const chatEndRef = useRef<HTMLDivElement | null>(null);

  // Sync analysis details to Watchlist and Movers states
  const syncAnalysisData = (key: string, analysis: StockAnalysis | null) => {
    if (!analysis) return;
    setWatchlist((prev) =>
      prev.map((item) =>
        item.instrument_key === key ? { ...item, analysis } : item
      )
    );
    setGainers((prev) =>
      prev.map((item) =>
        item.instrument_key === key ? { ...item, analysis } : item
      )
    );
    setLosers((prev) =>
      prev.map((item) =>
        item.instrument_key === key ? { ...item, analysis } : item
      )
    );
  };

  // Watchlist filter tabs and periods
  const [watchlistTab, setWatchlistTab] = useState<"gainers" | "losers" | "holdings">("gainers");

  interface IndexData {
    name: string;
    symbol: string;
    last_price: number;
    change: number;
    pct_change: number;
  }

  interface IndicesState {
    nifty: IndexData;
    sensex: IndexData;
    sectors?: IndexData[];
  }

  const [indices, setIndices] = useState<IndicesState>({
    nifty: { name: "Nifty 50", symbol: "NIFTY 50", last_price: 23550.0, change: 117.5, pct_change: 0.5 },
    sensex: { name: "BSE SENSEX", symbol: "SENSEX", last_price: 77200.0, change: 385.0, pct_change: 0.5 },
    sectors: []
  });
  const [watchlistPeriod, setWatchlistPeriod] = useState<string>("today");

  interface BoardRow {
    name: string; key: string; group: "index" | "sector" | "commodity";
    last_price: number | null; prev_close: number | null;
    change: number | null; pct_change: number | null;
    period?: string; from_date?: string | null; truncated?: boolean;
  }
  const [board, setBoard] = useState<BoardRow[]>([]);
  const [boardPeriod, setBoardPeriod] = useState<string>("today");
  const boardPeriodRef = useRef<string>("today");
  useEffect(() => { boardPeriodRef.current = boardPeriod; }, [boardPeriod]);
  const BOARD_PERIODS = ["today", "5d", "10d", "15d", "1m", "3m", "6m", "12m", "3y", "5y"];

  // Movers lists
  const [gainers, setGainers] = useState<MoverItem[]>([]);
  const [losers, setLosers] = useState<MoverItem[]>([]);
  const [moversTab, setMoversTab] = useState<"gainers" | "losers">("gainers");
  const [moversPeriod, setMoversPeriod] = useState<string>("today");

  // Search state
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<any[]>([]);
  const [showSearchDropdown, setShowSearchDropdown] = useState(false);

  // Auth state
  const [authState, setAuthState] = useState({ authenticated: false, provider: "mock", updated_at: null });
  const [authUrl, setAuthUrl] = useState("");
  const [toast, setToast] = useState<{ message: string; type: "success" | "error" } | null>(null);

  // App-level loading states
  const [watchlistLoading, setWatchlistLoading] = useState(false);
  const [moversLoading, setMoversLoading] = useState(false);

  // Price tracking for flash animation on tick updates
  const prevPricesRef = useRef<Record<string, number>>({});
  const prevSentimentBuyPctRef = useRef<Record<string, number>>({});
  const [priceFlash, setPriceFlash] = useState<Record<string, "up" | "down" | "">>({});
  const [sentimentTrend, setSentimentTrend] = useState<Record<string, "up" | "down" | "flat">>({});

  // Refs to avoid setInterval closure bugs
  const selectedKeyRef = useRef<string | null>(null);
  const watchlistPeriodRef = useRef<string>("today");
  const moversPeriodRef = useRef<string>("today");

  // Paused sections stop fetching quotes automatically; rows are then refreshed
  // one at a time on demand. The choice survives a reload — the reason to pause
  // is that quota is scarce, and that is still true after F5.
  const [watchlistPaused, setWatchlistPaused] = useState<boolean>(
    () => localStorage.getItem("watchlistPaused") === "1"
  );
  const [moversPaused, setMoversPaused] = useState<boolean>(
    () => localStorage.getItem("moversPaused") === "1"
  );
  // Holdings are stock actually owned, so they keep updating even when the rest
  // of the watchlist is paused — the reason to pause is to save quota on stocks
  // being watched, not on money currently at risk.
  const [holdingsPaused, setHoldingsPaused] = useState<boolean>(
    () => localStorage.getItem("holdingsPaused") === "1"
  );
  const watchlistPausedRef = useRef(watchlistPaused);
  const moversPausedRef = useRef(moversPaused);
  const holdingsPausedRef = useRef(holdingsPaused);
  const [rowRefreshing, setRowRefreshing] = useState<Record<string, boolean>>({});

  useEffect(() => {
    watchlistPausedRef.current = watchlistPaused;
    localStorage.setItem("watchlistPaused", watchlistPaused ? "1" : "0");
  }, [watchlistPaused]);

  useEffect(() => {
    moversPausedRef.current = moversPaused;
    localStorage.setItem("moversPaused", moversPaused ? "1" : "0");
  }, [moversPaused]);

  useEffect(() => {
    holdingsPausedRef.current = holdingsPaused;
    localStorage.setItem("holdingsPaused", holdingsPaused ? "1" : "0");
  }, [holdingsPaused]);

  useEffect(() => {
    selectedKeyRef.current = selectedKey;
  }, [selectedKey]);

  useEffect(() => {
    watchlistPeriodRef.current = watchlistPeriod;
  }, [watchlistPeriod]);

  useEffect(() => {
    moversPeriodRef.current = moversPeriod;
  }, [moversPeriod]);

  // 1. Initial Load & Auth Checks
  useEffect(() => {
    // Check url search params for auth status
    const params = new URLSearchParams(window.location.search);
    const authParam = params.get("auth");
    const messageParam = params.get("message");

    if (authParam === "success") {
      setToast({ message: "Upstox account connected successfully!", type: "success" });
      window.history.replaceState({}, document.title, window.location.pathname);
    } else if (authParam === "error") {
      setToast({ message: `Failed to connect with Upstox: ${messageParam || "Unknown error"}`, type: "error" });
      window.history.replaceState({}, document.title, window.location.pathname);
    }

    fetchAuthStatus();
    fetchWatchlist("today");
    fetchMovers("today");
    fetchIndices();

    // Auto-refresh quotes every 10 seconds during market hours. Each section can
    // be paused independently: Upstox meters quote requests per account, so a
    // table left polling in a background tab is quota the results panel cannot
    // then spend on the filing you are actually deciding on.
    const interval = setInterval(() => {
      if (checkIsMarketHours()) {
        // The watchlist endpoint returns holdings too, so one request serves
        // both; the pauses decide which rows are allowed to change.
        if (!watchlistPausedRef.current || !holdingsPausedRef.current) {
          refreshLiveQuotes();
        }
        if (!watchlistPausedRef.current) fetchIndices();
        if (!moversPausedRef.current) refreshLiveMovers();
      }
    }, 10000);

    // Off-market refresh (every 5 minutes) to conserve API calls when trading is closed
    const offMarketInterval = setInterval(() => {
      if (!checkIsMarketHours()) {
        if (!watchlistPausedRef.current || !holdingsPausedRef.current) {
          refreshLiveQuotes();
        }
        if (!watchlistPausedRef.current) fetchIndices();
        if (!moversPausedRef.current) refreshLiveMovers();
      }
    }, 300000);

    return () => {
      clearInterval(interval);
      clearInterval(offMarketInterval);
    };
  }, []);

  const fetchIndices = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/market/indices`);
      if (res.ok) {
        const data = await res.json();
        setIndices(data);
      }
    } catch (err) {
      console.error("Error fetching indices:", err);
    }
  };

  /**
   * The index, sector and commodity strip.
   *
   * Reads the backend's shared quote cache, so polling it costs no Upstox
   * request however often it runs — the refresher upstream is what sets the
   * rate. A period other than today comes off daily candles and does not move
   * intraday, so it is fetched on change rather than on the tick.
   */
  const fetchBoard = async (period?: string) => {
    const want = period ?? boardPeriodRef.current;
    try {
      const res = await fetch(`${API_BASE}/api/market/board?period=${want}`);
      if (!res.ok) return;
      const data = await res.json();
      if (boardPeriodRef.current === want) setBoard(data.rows || []);
    } catch (err) {
      console.error("Error fetching the index board:", err);
    }
  };

  useEffect(() => { fetchBoard(boardPeriod); }, [boardPeriod]);

  // Live only while a session is authorised and the market is open: the
  // backend cache is ~3s fresh in hours, and there is nothing to refresh at
  // that rate once trading has stopped.
  useEffect(() => {
    const t = setInterval(() => {
      if (boardPeriodRef.current !== "today") return;
      if (!authState.authenticated) return;
      if (checkIsMarketHours()) fetchBoard("today");
    }, 3000);
    return () => clearInterval(t);
  }, [authState.authenticated]);

  const handleToggleHolding = async (id: number, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      const res = await fetch(`${API_BASE}/api/watchlist/${id}/toggle-holding`, { method: "POST" });
      if (res.ok) {
        const data = await res.json();
        setWatchlist((prev) =>
          prev.map((item) =>
            item.id === id ? { ...item, is_holding: data.is_holding } : item
          )
        );
        setToast({
          message: data.is_holding ? "Stock tagged as Holding." : "Stock removed from Holdings.",
          type: "success"
        });
      }
    } catch (err) {
      console.error("Error toggling holding:", err);
    }
  };

  // Toast auto-clear
  useEffect(() => {
    if (toast) {
      const timer = setTimeout(() => {
        setToast(null);
      }, 5000);
      return () => clearTimeout(timer);
    }
  }, [toast]);

  // Fetch details for selected stock key
  useEffect(() => {
    if (selectedKey && !detailsMap[selectedKey] && !detailsLoading[selectedKey]) {
      fetchStockDetail(selectedKey);
    }
  }, [selectedKey]);

  // Auth Functions
  const fetchAuthStatus = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/auth/status`);
      const data = await res.json();
      setAuthState(data);
      if (!data.authenticated) {
        const urlRes = await fetch(`${API_BASE}/api/auth/login`);
        const urlData = await urlRes.json();
        setAuthUrl(urlData.url);
      }
    } catch (err) {
      console.error("Auth status error:", err);
    }
  };

  const handleAuthorize = async () => {
    if (authUrl) {
      window.location.href = authUrl;
      return;
    }

    try {
      const res = await fetch(`${API_BASE}/api/auth/login`);
      const data = await res.json();
      if (data.url) {
        window.location.href = data.url;
      } else {
        setToast({ message: "Failed to load Upstox login URL.", type: "error" });
      }
    } catch (err) {
      console.error("Authorization URL fetch error:", err);
      setToast({ message: "Failed to connect to backend auth service.", type: "error" });
    }
  };

  const handleDisconnect = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/auth/logout`, { method: "POST" });
      if (res.ok) {
        setAuthState({ authenticated: false, provider: "upstox", updated_at: null });
        setDetailsMap({});
        setSelectedKey(null);
        setWatchlist([]);
        setGainers([]);
        setLosers([]);
        setToast({ message: "Disconnected from Upstox.", type: "success" });
        // Fetch new login URL
        const urlRes = await fetch(`${API_BASE}/api/auth/login`);
        const urlData = await urlRes.json();
        setAuthUrl(urlData.url);
      } else {
        const errorData = await res.json();
        setToast({ message: `Failed to disconnect: ${errorData.detail || "Unknown error"}`, type: "error" });
      }
    } catch (err) {
      console.error("Disconnect error:", err);
      setToast({ message: "Failed to connect to backend server.", type: "error" });
    }
  };

  // Watchlist Fetching
  const fetchWatchlist = async (period = watchlistPeriod) => {
    setWatchlistLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/watchlist?period=${period}`);
      if (res.status === 401) {
        setAuthState({ authenticated: false, provider: "upstox", updated_at: null });
        setWatchlist([]);
        return;
      }
      const data = await res.json();

      // Check price changes for flash effects and compute sentiment trend
      const newFlashes: Record<string, "up" | "down" | ""> = {};
      const newTrends: Record<string, "up" | "down" | "flat"> = {};

      data.forEach((item: WatchlistItem) => {
        const prev = prevPricesRef.current[item.instrument_key];
        if (prev !== undefined && prev !== item.last_price) {
          newFlashes[item.instrument_key] = item.last_price > prev ? "up" : "down";
        }
        prevPricesRef.current[item.instrument_key] = item.last_price;

        const high = item.high || 0;
        const low = item.low || 0;
        const range = high - low;
        const priceBuyPct = range > 0 ? ((item.last_price - low) / range) * 100 : 50;
        const depthBuyPct = item.depth_buy_pct !== undefined ? item.depth_buy_pct : 50;
        const compositeBuyPct = Math.round((priceBuyPct * 0.15) + (depthBuyPct * 0.85));

        const prevSentiment = prevSentimentBuyPctRef.current[item.instrument_key];
        if (prevSentiment !== undefined && prevSentiment !== compositeBuyPct) {
          newTrends[item.instrument_key] = compositeBuyPct > prevSentiment ? "up" : "down";
        }
        prevSentimentBuyPctRef.current[item.instrument_key] = compositeBuyPct;
        const hist = sentimentHistoryRef.current[item.instrument_key] || [];
        sentimentHistoryRef.current[item.instrument_key] = [...hist, compositeBuyPct].slice(-15);

        const qHist = quantityHistoryRef.current[item.instrument_key] || { buy: [], sell: [] };
        const buyQty = item.total_buy_qty || 0;
        const sellQty = item.total_sell_qty || 0;
        quantityHistoryRef.current[item.instrument_key] = {
          buy: [...qHist.buy, buyQty].slice(-15),
          sell: [...qHist.sell, sellQty].slice(-15)
        };
      });
      if (Object.keys(newFlashes).length > 0) {
        setPriceFlash((prev) => ({ ...prev, ...newFlashes }));
        setTimeout(() => {
          setPriceFlash({});
        }, 1000);
      }
      if (Object.keys(newTrends).length > 0) {
        setSentimentTrend((prev) => ({ ...prev, ...newTrends }));
      }

      setWatchlist(data);

      // Default select the first stock if not selected yet
      if (data.length > 0 && !selectedKey) {
        setSelectedKey(data[0].instrument_key);
      }
    } catch (err) {
      console.error("Watchlist fetch error:", err);
    } finally {
      setWatchlistLoading(false);
    }
  };

  const refreshLiveQuotes = async () => {
    try {
      const currentPeriod = watchlistPeriodRef.current;
      const currentSelectedKey = selectedKeyRef.current;
      const wlRes = await fetch(`${API_BASE}/api/watchlist?period=${currentPeriod}`);
      if (wlRes.status === 401) {
        setAuthState({ authenticated: false, provider: "upstox", updated_at: null });
        setWatchlist([]);
        return;
      }
      const wlData = await wlRes.json();

      const newFlashes: Record<string, "up" | "down" | ""> = {};
      const newTrends: Record<string, "up" | "down" | "flat"> = {};

      wlData.forEach((item: WatchlistItem) => {
        const prev = prevPricesRef.current[item.instrument_key];
        if (prev !== undefined && prev !== item.last_price && item.last_price > 0) {
          newFlashes[item.instrument_key] = item.last_price > prev ? "up" : "down";
        }
        prevPricesRef.current[item.instrument_key] = item.last_price;

        const high = item.high || 0;
        const low = item.low || 0;
        const range = high - low;
        const priceBuyPct = range > 0 ? ((item.last_price - low) / range) * 100 : 50;
        const depthBuyPct = item.depth_buy_pct !== undefined ? item.depth_buy_pct : 50;
        const compositeBuyPct = Math.round((priceBuyPct * 0.15) + (depthBuyPct * 0.85));

        const prevSentiment = prevSentimentBuyPctRef.current[item.instrument_key];
        if (prevSentiment !== undefined && prevSentiment !== compositeBuyPct) {
          newTrends[item.instrument_key] = compositeBuyPct > prevSentiment ? "up" : "down";
        }
        prevSentimentBuyPctRef.current[item.instrument_key] = compositeBuyPct;
        const hist = sentimentHistoryRef.current[item.instrument_key] || [];
        sentimentHistoryRef.current[item.instrument_key] = [...hist, compositeBuyPct].slice(-15);

        const qHist = quantityHistoryRef.current[item.instrument_key] || { buy: [], sell: [] };
        const buyQty = item.total_buy_qty || 0;
        const sellQty = item.total_sell_qty || 0;
        quantityHistoryRef.current[item.instrument_key] = {
          buy: [...qHist.buy, buyQty].slice(-15),
          sell: [...qHist.sell, sellQty].slice(-15)
        };
      });
      if (Object.keys(newFlashes).length > 0) {
        setPriceFlash((prev) => ({ ...prev, ...newFlashes }));
        setTimeout(() => {
          setPriceFlash({});
        }, 800);
      }
      if (Object.keys(newTrends).length > 0) {
        setSentimentTrend((prev) => ({ ...prev, ...newTrends }));
      }
      // A paused section keeps its previous prices. Holdings and the rest of
      // the watchlist arrive in the same payload, so the merge is per row.
      setWatchlist(prev => {
        if (!watchlistPausedRef.current && !holdingsPausedRef.current) return wlData;
        const byKey = new Map(prev.map((r: WatchlistItem) => [r.instrument_key, r]));
        return wlData.map((row: WatchlistItem) => {
          const frozen = row.is_holding ? holdingsPausedRef.current : watchlistPausedRef.current;
          const previous = byKey.get(row.instrument_key);
          return frozen && previous ? previous : row;
        });
      });

      // Also refresh the selected stock detail quote
      if (currentSelectedKey) {
        fetchStockDetail(currentSelectedKey);
      }
    } catch (err) {
      console.error("Background quote update error:", err);
    }
  };

  /**
   * Refresh one stock's quote, for use while a section is paused.
   *
   * Costs a single upstream request instead of the whole table, and folds the
   * result through the same flash and sentiment bookkeeping the bulk refresh
   * uses, so a hand-refreshed row behaves like any other.
   */
  const refreshSingleQuote = async (item: { symbol: string; instrument_key: string }, e?: React.MouseEvent) => {
    e?.stopPropagation();
    const key = item.instrument_key;
    setRowRefreshing((prev) => ({ ...prev, [key]: true }));
    try {
      const res = await fetch(`${API_BASE}/api/market/quotes-by-symbols?symbols=${encodeURIComponent(item.symbol)}`);
      if (!res.ok) return;
      const data = await res.json();
      const q = data[item.symbol.toUpperCase()];
      if (!q || !q.last_price) {
        setToast({ message: `No live quote available for ${item.symbol}.`, type: "error" });
        return;
      }

      const prev = prevPricesRef.current[key];
      if (prev !== undefined && prev !== q.last_price) {
        setPriceFlash((p) => ({ ...p, [key]: q.last_price > prev ? "up" : "down" }));
        setTimeout(() => setPriceFlash({}), 800);
      }
      prevPricesRef.current[key] = q.last_price;

      const range = (q.high || 0) - (q.low || 0);
      const priceBuyPct = range > 0 ? ((q.last_price - q.low) / range) * 100 : 50;
      const depthBuyPct = q.depth_buy_pct !== undefined ? q.depth_buy_pct : 50;
      const compositeBuyPct = Math.round((priceBuyPct * 0.15) + (depthBuyPct * 0.85));
      const hist = sentimentHistoryRef.current[key] || [];
      sentimentHistoryRef.current[key] = [...hist, compositeBuyPct].slice(-15);
      const qHist = quantityHistoryRef.current[key] || { buy: [], sell: [] };
      quantityHistoryRef.current[key] = {
        buy: [...qHist.buy, q.total_buy_qty || 0].slice(-15),
        sell: [...qHist.sell, q.total_sell_qty || 0].slice(-15),
      };

      const merge = (row: any) =>
        row.instrument_key === key
          ? {
              ...row,
              last_price: q.last_price,
              change: q.change,
              close: q.close,
              high: q.high,
              low: q.low,
              depth_buy_pct: q.depth_buy_pct,
              depth_sell_pct: q.depth_sell_pct,
              total_buy_qty: q.total_buy_qty,
              total_sell_qty: q.total_sell_qty,
            }
          : row;
      setWatchlist((prevRows) => prevRows.map(merge));
      setGainers((prevRows) => prevRows.map(merge));
      setLosers((prevRows) => prevRows.map(merge));
    } catch (err) {
      console.error("Single quote refresh failed:", err);
    } finally {
      setRowRefreshing((prev) => ({ ...prev, [key]: false }));
    }
  };

  const refreshLiveMovers = async () => {
    try {
      const currentPeriod = moversPeriodRef.current;
      const res = await fetch(`${API_BASE}/api/market/movers?period=${currentPeriod}`);
      if (res.status === 401) {
        setAuthState({ authenticated: false, provider: "upstox", updated_at: null });
        setGainers([]);
        setLosers([]);
        return;
      }
      if (res.ok) {
        const data = await res.json();
        const newFlashes: Record<string, "up" | "down" | ""> = {};
        const newTrends: Record<string, "up" | "down" | "flat"> = {};

        const allMovers = [...(data.gainers || []), ...(data.losers || [])];
        allMovers.forEach((item: any) => {
          const prev = prevPricesRef.current[item.instrument_key];
          if (prev !== undefined && prev !== item.last_price && item.last_price > 0) {
            newFlashes[item.instrument_key] = item.last_price > prev ? "up" : "down";
          }
          prevPricesRef.current[item.instrument_key] = item.last_price;

          const high = item.high || 0;
          const low = item.low || 0;
          const range = high - low;
          const priceBuyPct = range > 0 ? ((item.last_price - low) / range) * 100 : 50;
          const depthBuyPct = item.depth_buy_pct !== undefined ? item.depth_buy_pct : 50;
          const compositeBuyPct = Math.round((priceBuyPct * 0.15) + (depthBuyPct * 0.85));

          const prevSentiment = prevSentimentBuyPctRef.current[item.instrument_key];
          if (prevSentiment !== undefined && prevSentiment !== compositeBuyPct) {
            newTrends[item.instrument_key] = compositeBuyPct > prevSentiment ? "up" : "down";
          }
          prevSentimentBuyPctRef.current[item.instrument_key] = compositeBuyPct;
          const hist = sentimentHistoryRef.current[item.instrument_key] || [];
          sentimentHistoryRef.current[item.instrument_key] = [...hist, compositeBuyPct].slice(-15);

          const qHist = quantityHistoryRef.current[item.instrument_key] || { buy: [], sell: [] };
          const buyQty = item.total_buy_qty || 0;
          const sellQty = item.total_sell_qty || 0;
          quantityHistoryRef.current[item.instrument_key] = {
            buy: [...qHist.buy, buyQty].slice(-15),
            sell: [...qHist.sell, sellQty].slice(-15)
          };
        });

        if (Object.keys(newFlashes).length > 0) {
          setPriceFlash((prev) => ({ ...prev, ...newFlashes }));
          setTimeout(() => {
            setPriceFlash({});
          }, 800);
        }
        if (Object.keys(newTrends).length > 0) {
          setSentimentTrend((prev) => ({ ...prev, ...newTrends }));
        }

        setGainers(data.gainers || []);
        setLosers(data.losers || []);
      }
    } catch (err) {
      console.error("Background movers update error:", err);
    }
  };

  const fetchMovers = async (period = moversPeriod) => {
    setMoversLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/market/movers?period=${period}`);
      if (res.status === 401) {
        setAuthState({ authenticated: false, provider: "upstox", updated_at: null });
        setGainers([]);
        setLosers([]);
        return;
      }
      if (res.ok) {
        const data = await res.json();
        const allMovers = [...(data.gainers || []), ...(data.losers || [])];
        allMovers.forEach((item: any) => {
          if (item.last_price > 0) {
            prevPricesRef.current[item.instrument_key] = item.last_price;
          }
        });
        setGainers(data.gainers || []);
        setLosers(data.losers || []);
      }
    } catch (err) {
      console.error("Movers fetch error:", err);
    } finally {
      setMoversLoading(false);
    }
  };

  const fetchStockDetail = async (key: string) => {
    setDetailsLoading((prev) => ({ ...prev, [key]: true }));
    try {
      const res = await fetch(`${API_BASE}/api/market/stock/${encodeURIComponent(key)}`);
      if (res.status === 401) {
        setAuthState({ authenticated: false, provider: "upstox", updated_at: null });
        return;
      }
      if (res.ok) {
        const data = await res.json();
        setDetailsMap((prev) => ({ ...prev, [key]: data }));
        if (data.analysis) {
          syncAnalysisData(key, data.analysis);
        }
      }
    } catch (err) {
      console.error("Stock detail fetch error:", err);
    } finally {
      setDetailsLoading((prev) => ({ ...prev, [key]: false }));
    }
  };

  const fetchChartCandles = async (key: string, period: string) => {
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
  };

  useEffect(() => {
    if (selectedKey) {
      fetchChartCandles(selectedKey, chartPeriod);
    } else {
      setChartCandles([]);
    }
  }, [selectedKey, chartPeriod]);

  // Reset chart period to 1D whenever selected stock changes
  useEffect(() => {
    setChartPeriod("1D");
  }, [selectedKey]);

  // Watchlist Actions
  const handleAddToWatchlist = async (stock: any) => {
    try {
      const res = await fetch(
        `${API_BASE}/api/watchlist?symbol=${stock.symbol}&name=${encodeURIComponent(stock.name)}&instrument_key=${encodeURIComponent(stock.key)}`,
        { method: "POST" }
      );
      if (res.status === 401) {
        setAuthState({ authenticated: false, provider: "upstox", updated_at: null });
        setToast({ message: "Upstox disconnected. Please reconnect.", type: "error" });
        return;
      }
      if (res.ok) {
        setSearchQuery("");
        setSearchResults([]);
        setShowSearchDropdown(false);
        fetchWatchlist(watchlistPeriod);
        setSelectedKey(stock.key);
        setToast({ message: `${stock.symbol} added to watchlist.`, type: "success" });
      }
    } catch (err) {
      console.error("Error adding to watchlist:", err);
    }
  };

  const handleDeleteFromWatchlist = async (id: number, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      const res = await fetch(`${API_BASE}/api/watchlist/${id}`, { method: "DELETE" });
      if (res.ok) {
        fetchWatchlist(watchlistPeriod);
        setToast({ message: "Stock removed from watchlist.", type: "success" });
      }
    } catch (err) {
      console.error("Error deleting watchlist item:", err);
    }
  };

  // Search autocomplete
  const handleSearchChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const val = e.target.value;
    setSearchQuery(val);
    if (val.trim().length > 0) {
      try {
        const res = await fetch(`${API_BASE}/api/market/search?query=${val}`);
        if (res.status === 401) {
          setAuthState({ authenticated: false, provider: "upstox", updated_at: null });
          setSearchResults([]);
          return;
        }
        const data = await res.json();
        setSearchResults(data);
        setShowSearchDropdown(true);
      } catch (err) {
        console.error("Search error:", err);
      }
    } else {
      setSearchResults([]);
      setShowSearchDropdown(false);
    }
  };

  // Trigger manual Fetch News & AI Insight Update
  const handleFetchNewsAndAI = async (key: string) => {
    setDetailsLoading((prev) => ({ ...prev, [key]: true }));
    try {
      const res = await fetch(
        `${API_BASE}/api/market/stock/${encodeURIComponent(key)}/fetch-news`,
        { method: "POST" }
      );
      if (res.status === 401) {
        setAuthState({ authenticated: false, provider: "upstox", updated_at: null });
        setToast({ message: "Upstox disconnected. Please reconnect.", type: "error" });
        return;
      }
      if (res.ok) {
        const data = await res.json();
        setDetailsMap((prev) => {
          const current = prev[key];
          const baseDetail = current || {
            instrument_key: key,
            name: "",
            quote: { last_price: 0, volume: 0, ohlc: { open: 0, high: 0, low: 0, close: 0 } },
            candles: [],
            news: [],
            news_fetched_at: null,
            ai_comment: null,
            ai_fetched_at: null,
            analysis: null
          };
          return {
            ...prev,
            [key]: {
              ...baseDetail,
              news: data.news,
              news_fetched_at: data.news_fetched_at,
              ai_comment: data.ai_comment,
              ai_fetched_at: data.ai_fetched_at,
              analysis: data.analysis
            }
          };
        });
        if (data.analysis) {
          syncAnalysisData(key, data.analysis);
        }
        setToast({ message: "Feeds analyzed and commentary updated.", type: "success" });
      }
    } catch (err) {
      console.error("Force refresh news error:", err);
    } finally {
      setDetailsLoading((prev) => ({ ...prev, [key]: false }));
    }
  };

  // Utility to format time
  const formatLastUpdated = (isoString: string | null) => {
    if (!isoString) return "Never";
    let formattedStr = isoString;
    if (!isoString.endsWith("Z") && !isoString.includes("+") && !isoString.includes("-")) {
      formattedStr = isoString + "Z";
    }
    const dt = new Date(formattedStr);
    return dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  };

  const handleMoversPeriodChange = (period: string) => {
    setMoversPeriod(period);
    fetchMovers(period);
  };

  const handleWatchlistPeriodChange = (period: string) => {
    setWatchlistPeriod(period);
    fetchWatchlist(period);
  };

  // Scroll to bottom of chat log
  useEffect(() => {
    if (chatEndRef.current) {
      chatEndRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [chatHistory, chatTab, chatLoading]);

  // Reset chat tab back to analysis when modal changes or closes
  useEffect(() => {
    if (!isModalOpen) {
      setChatTab("analysis");
    }
  }, [isModalOpen]);

  // Send message to backend stock assistant
  const handleSendChatMessage = async (key: string) => {
    if (!chatInput.trim() || chatLoading) return;

    const message = chatInput.trim();
    setChatInput("");

    const currentHistory = chatHistory[key] || [];
    const updatedHistory = [...currentHistory, { role: "user" as const, content: message }];

    // Append user message to list instantly
    setChatHistory((prev) => ({
      ...prev,
      [key]: updatedHistory
    }));
    setChatLoading(true);

    try {
      const res = await fetch(`${API_BASE}/api/market/stock/${encodeURIComponent(key)}/chat`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          message: message,
          history: currentHistory
        })
      });

      if (res.status === 401) {
        setAuthState({ authenticated: false, provider: "upstox", updated_at: null });
        setToast({ message: "Upstox disconnected. Please reconnect.", type: "error" });
        return;
      }

      if (res.ok) {
        const data = await res.json();
        setChatHistory((prev) => ({
          ...prev,
          [key]: [...updatedHistory, { role: "assistant" as const, content: data.response }]
        }));
      } else {
        const errData = await res.json();
        setToast({ message: errData.detail || "Failed to get AI response.", type: "error" });
      }
    } catch (err) {
      console.error("Chat error:", err);
      setToast({ message: "Failed to connect to AI server.", type: "error" });
    } finally {
      setChatLoading(false);
    }
  };

  // Filter Watchlist items by selected subtab & sort
  const filteredWatchlist = watchlist
    .filter((w) => {
      if (watchlistTab === "holdings") return w.is_holding;
      return watchlistTab === "gainers" ? w.change >= 0 : w.change < 0;
    })
    .sort((a, b) => {
      if (watchlistTab === "holdings") return b.change - a.change;
      return watchlistTab === "gainers" ? b.change - a.change : a.change - b.change;
    });

  const periods = [
    { label: "1D", val: "today" },
    { label: "1W", val: "week" },
    { label: "1M", val: "month" },
    { label: "3M", val: "3months" },
    { label: "6M", val: "6months" },
    { label: "1Y", val: "1year" },
    { label: "5Y", val: "5years" }
  ];

  const renderBoardTile = (row: BoardRow) => {
    const pct = row.pct_change;
    const tone = pct == null ? "var(--text-faint)"
      : pct > 0 ? "var(--positive)" : pct < 0 ? "var(--negative)" : "var(--text-muted)";
    const money = (v: number | null) =>
      v == null ? "—" : v.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return (
      <div key={row.key} title={
        row.period && row.period !== "today"
          ? `${row.name}: ${money(row.prev_close)} on ${row.from_date || "the start of the period"} → ${money(row.last_price)}`
          : `${row.name}: previous close ${money(row.prev_close)} → ${money(row.last_price)}`}
        style={{
          display: "flex", flexDirection: "column", gap: "1px",
          padding: "4px 8px", borderRadius: "6px", minWidth: "96px",
          backgroundColor: "rgba(33, 36, 43, 0.5)",
          border: `1px solid ${row.group === "commodity" ? "rgba(216, 174, 100, 0.12)" : "rgba(255,255,255,0.03)"}`,
        }}>
        <span style={{
          fontSize: "8px", fontWeight: 700, letterSpacing: "0.4px",
          textTransform: "uppercase", whiteSpace: "nowrap",
          overflow: "hidden", textOverflow: "ellipsis",
          color: row.group === "commodity" ? "var(--warning)" : "var(--text-faint)",
        }}>
          {row.name}
          {row.truncated && (
            <span title="This contract has less history than the period asked for, so the change covers a shorter span."
                  style={{ marginLeft: "4px", color: "var(--text-faint)" }}>*</span>
          )}
        </span>

        <div style={{ display: "flex", alignItems: "baseline", gap: "6px" }}>
          <span style={{ fontSize: "12px", fontWeight: 700, color: "var(--text-primary)",
                         fontVariantNumeric: "tabular-nums" }}>
            {money(row.last_price)}
          </span>
          <span style={{ fontSize: "9px", fontWeight: 700, color: tone,
                         fontVariantNumeric: "tabular-nums" }}>
            {pct == null ? "—" : `${pct > 0 ? "+" : ""}${pct.toFixed(2)}%`}
          </span>
        </div>

        {/* The move in points. The starting price it is measured from is in
            the tooltip rather than on the tile: printing both ends spent a
            whole line restating a number already shown. */}
        <span style={{ fontSize: "9px", fontWeight: 600, color: tone,
                       fontVariantNumeric: "tabular-nums" }}>
          {row.change == null ? "—" : `${row.change > 0 ? "+" : ""}${money(row.change)}`}
        </span>
      </div>
    );
  };

  return (
    <div className="app-container">
      {/* Top Header */}
      <header className="app-header">
        <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
          <div style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            width: "36px",
            height: "36px",
            borderRadius: "8px",
            backgroundColor: "var(--accent)",
            color: "var(--on-accent)",
            fontWeight: "bold",
            fontSize: "18px",
            boxShadow: "0 0 15px rgba(127, 166, 225, 0.26)"
          }}>
            S
          </div>
          <div>
            <h1 style={{ fontSize: "16px", fontWeight: "bold", color: "var(--text-primary)", letterSpacing: "0.5px" }}>STOCKS EYE</h1>
            <p style={{ fontSize: "9px", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "1px", fontWeight: "600" }}>
              Indian Markets Dashboard
            </p>
          </div>
        </div>

        {/* Authentication Pill */}
        <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
          {authState.provider === "mock" ? (
            <div style={{
              display: "flex",
              alignItems: "center",
              gap: "8px",
              padding: "6px 12px",
              fontSize: "11px",
              fontWeight: "600",
              borderRadius: "20px",
              backgroundColor: "rgba(127, 166, 225, 0.09)",
              color: "var(--accent)",
              border: "1px solid rgba(127, 166, 225, 0.13)"
            }}>
              <span style={{ width: "6px", height: "6px", borderRadius: "50%", backgroundColor: "var(--accent)" }} className="animate-pulse"></span>
              Demo Feed Active
            </div>
          ) : authState.authenticated ? (
            <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
              <div style={{
                display: "flex",
                alignItems: "center",
                gap: "6px",
                padding: "6px 12px",
                fontSize: "11px",
                fontWeight: "600",
                borderRadius: "20px",
                backgroundColor: "rgba(91, 190, 147, 0.09)",
                color: "var(--positive-strong)",
                border: "1px solid rgba(91, 190, 147, 0.13)"
              }}>
                <UserCheck size={13} />
                Upstox Connected ({formatLastUpdated(authState.updated_at)})
              </div>
              <button
                onClick={handleDisconnect}
                style={{
                  padding: "6px 12px",
                  fontSize: "11px",
                  fontWeight: "600",
                  borderRadius: "6px",
                  backgroundColor: "rgba(226, 141, 131, 0.08)",
                  color: "var(--negative-strong)",
                  border: "1px solid rgba(226, 141, 131, 0.13)",
                  transition: "all 0.2s"
                }}
                onMouseOver={(e) => {
                  e.currentTarget.style.backgroundColor = "var(--negative)";
                  e.currentTarget.style.color = "var(--on-accent)";
                }}
                onMouseOut={(e) => {
                  e.currentTarget.style.backgroundColor = "rgba(226, 141, 131, 0.08)";
                  e.currentTarget.style.color = "var(--negative-strong)";
                }}
              >
                Disconnect
              </button>
            </div>
          ) : (
            <button
              onClick={handleAuthorize}
              style={{
                display: "flex",
                alignItems: "center",
                gap: "8px",
                padding: "6px 16px",
                fontSize: "11px",
                fontWeight: "700",
                borderRadius: "8px",
                backgroundColor: "var(--accent)",
                color: "var(--on-accent)",
                boxShadow: "0 4px 12px rgba(127, 166, 225, 0.16)",
                transition: "background 0.2s"
              }}
              onMouseOver={(e) => e.currentTarget.style.backgroundColor = "var(--accent)"}
              onMouseOut={(e) => e.currentTarget.style.backgroundColor = "var(--accent)"}
            >
              Connect Upstox Account
            </button>
          )}
        </div>
      </header>

      {/* Index, sector and commodity board */}
      <div style={{
        padding: "6px 24px 8px",
        backgroundColor: "rgba(33, 36, 43, 0.4)",
        borderBottom: "1px solid rgba(255, 255, 255, 0.05)",
        flexShrink: 0
      }}>
        {/* The period selector gets its own row. Inside the tile grid it
            occupied a single 115px cell and clipped after three buttons. */}
        <div style={{ display: "flex", gap: "3px", alignItems: "center",
                      flexWrap: "wrap", marginBottom: "8px" }}>
          <span style={{ fontSize: "9px", fontWeight: 700, letterSpacing: "0.5px",
                         color: "var(--text-faint)", textTransform: "uppercase",
                         marginRight: "4px" }}>
            Change over
          </span>
          {BOARD_PERIODS.map(p => (
            <button key={p} onClick={() => setBoardPeriod(p)}
              title={p === "today" ? "Move against the previous close, live" : `Move over the last ${p}`}
              style={{
                padding: "3px 7px", borderRadius: "5px", cursor: "pointer",
                fontSize: "9px", fontWeight: 700, textTransform: "uppercase",
                background: boardPeriod === p ? "var(--accent-bg)" : "transparent",
                border: `1px solid ${boardPeriod === p ? "var(--accent-border)" : "rgba(255,255,255,0.06)"}`,
                color: boardPeriod === p ? "var(--accent)" : "var(--text-faint)",
              }}>
              {p}
            </button>
          ))}

        </div>

        <div style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(96px, 1fr))",
          gap: "5px",
        }}>
          {board.length === 0
            ? <span style={{ fontSize: "10px", color: "var(--text-faint)", padding: "6px 10px" }}>
                Loading market board…
              </span>
            : board.map(renderBoardTile)}
        </div>
      </div>

      {/* Toast Alert */}
      {toast && (
        <div style={{
          position: "fixed",
          top: "24px",
          right: "24px",
          zIndex: 9999,
          padding: "12px 20px",
          borderRadius: "8px",
          backgroundColor: toast.type === "success" ? "rgba(91, 190, 147, 0.95)" : "rgba(226, 141, 131, 0.95)",
          color: "var(--text-primary)",
          fontWeight: "600",
          fontSize: "13px",
          boxShadow: toast.type === "success" ? "0 4px 15px rgba(91, 190, 147, 0.26)" : "0 4px 15px rgba(226, 141, 131, 0.26)",
          display: "flex",
          alignItems: "center",
          gap: "10px",
          backdropFilter: "blur(4px)",
          animation: "slide-in 0.3s ease-out"
        }}>
          {toast.type === "success" ? <UserCheck size={16} /> : <Brain size={16} />}
          <span>{toast.message}</span>
          <button onClick={() => setToast(null)} style={{ background: "transparent", color: "var(--text-primary)", fontWeight: "bold", fontSize: "14px", marginLeft: "10px", border: "none", cursor: "pointer" }}>Ãƒâ€”</button>
        </div>
      )}

      {/* Main Content Area */}
      {activeView === "trading" ? (
        <div className="main-scroll-area" style={{ padding: 0, paddingBottom: "80px" }}>
          <TradingDashboard />
        </div>
      ) : activeView === "intelligence" ? (
        <div className="main-scroll-area" style={{ padding: 0, paddingBottom: "80px" }}>
          <IntelligenceDashboard />
        </div>
      ) : (
        <div className="main-scroll-area" style={{ paddingBottom: "80px" }}>

          {/* Top middle bar (Search) */}
          <div className="top-search-indices-bar">
            {/* Search bar */}
            <div className="search-section" style={{ maxWidth: "100%" }}>
              <div className="search-input-wrapper">
                <Search size={14} style={{ color: "var(--text-muted)", marginRight: "8px" }} />
                <input
                  type="text"
                  placeholder="Search and add stock..."
                  value={searchQuery}
                  onChange={handleSearchChange}
                  style={{
                    backgroundColor: "transparent",
                    border: "none",
                    outline: "none",
                    fontSize: "12px",
                    width: "100%",
                    color: "var(--text-primary)"
                  }}
                />
              </div>

              {/* Autocomplete Dropdown */}
              {showSearchDropdown && searchResults.length > 0 && (
                <div className="search-dropdown">
                  {searchResults.map((item) => (
                    <div
                      key={item.key}
                      onClick={() => handleAddToWatchlist(item)}
                      className="search-item"
                    >
                      <span style={{ fontSize: "12px", fontWeight: "600", color: "var(--text-primary)" }}>{item.symbol}</span>
                      <span style={{ fontSize: "9px", color: "var(--text-muted)" }} className="truncate">{item.name}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* ===== MY WATCHLIST ===== */}
          <section className="data-section">
            <div className="section-toolbar">
              <h3 className="section-title">
                My Watchlist <span className="count">({watchlist.length})</span>
              </h3>

              <div className="toggle-group">
                <button
                  onClick={() => setWatchlistTab("gainers")}
                  className={`toggle-btn ${watchlistTab === "gainers" ? "active-gain" : ""}`}
                >
                  Gainers
                </button>
                <button
                  onClick={() => setWatchlistTab("losers")}
                  className={`toggle-btn ${watchlistTab === "losers" ? "active-loss" : ""}`}
                >
                  Losers
                </button>
                <button
                  onClick={() => setWatchlistTab("holdings")}
                  className={`toggle-btn ${watchlistTab === "holdings" ? "active-gain" : ""}`}
                >
                  Holdings
                </button>
              </div>

              <LivePriceToggle
                paused={watchlistPaused}
                onToggle={() => setWatchlistPaused((v) => !v)}
                onRefreshAll={refreshLiveQuotes}
                label="watchlist"
                name="WATCHED"
              />
              {/* Separate on purpose: pausing the stocks you are watching should
                  not stop the prices of the stocks you own. */}
              <LivePriceToggle
                paused={holdingsPaused}
                onToggle={() => setHoldingsPaused((v) => !v)}
                onRefreshAll={refreshLiveQuotes}
                label="holdings"
                name="HOLDINGS"
              />

              <div className="period-bar">
                {periods.map((p) => (
                  <button
                    key={p.val}
                    onClick={() => handleWatchlistPeriodChange(p.val)}
                    className={`period-pill ${watchlistPeriod === p.val ? "active" : ""}`}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            </div>

            {watchlistLoading ? (
              <div className="loading-state">
                <RefreshCw className="animate-spin" size={16} style={{ color: "var(--accent-color)" }} />
              </div>
            ) : filteredWatchlist.length === 0 ? (
              <div className="empty-state">
                No stocks match this filter in watchlist.
              </div>
            ) : (
              <div className="table-scroll-container">
                <table className="stock-data-table">
                  <thead>
                    <tr>
                      <th className="col-symbol">Symbol</th>
                      <th className="col-name">Company</th>
                      <th className="col-ltp" style={{ textAlign: "right" }}>LTP</th>
                      <th className="col-change" style={{ textAlign: "right" }}>Change</th>
                      <th className="col-close" style={{ textAlign: "right" }}>Prev Close</th>
                      <th className="col-high" style={{ textAlign: "right" }}>Day High</th>
                      <th className="col-interest" style={{ textAlign: "center", width: "110px" }}>Sentiment (B/S)</th>
                      <th className="col-qty" style={{ textAlign: "center", width: "120px" }}>Buy/Sell Qty</th>
                      <th className="col-trend-signal" style={{ textAlign: "center", width: "115px" }}>
                        2m Signal
                        <span
                          title="Short-term (2 min) trend calculated from order book depth sentiment and quantity dynamics. Hover for details and reliability notice."
                          style={{ cursor: "help", fontSize: "10px", color: "var(--text-muted)", marginLeft: "4px" }}
                        >
                          ⓘ
                        </span>
                      </th>
                      <th className="col-ai">AI</th>
                      <th className="col-sector">Sector</th>
                      <th className="col-res">Resistance</th>
                      <th className="col-actions"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredWatchlist.map((item) => {
                      const isActive = selectedKey === item.instrument_key && isModalOpen;
                      const isUp = item.change >= 0;
                      const flash = priceFlash[item.instrument_key];

                      return (
                        <tr
                          key={item.id}
                          onClick={() => {
                            setSelectedKey(item.instrument_key);
                            setIsModalOpen(true);
                          }}
                          className={`${isActive ? "active" : ""} ${flash === "up" ? "tick-up" : flash === "down" ? "tick-down" : ""
                            }`}
                        >
                          <td>
                            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                              <span style={{ fontWeight: 700, color: "var(--text-primary)", fontSize: "12px" }}>
                                {item.symbol}
                              </span>
                              <button
                                onClick={(e) => handleToggleHolding(item.id, e)}
                                style={{
                                  background: "none",
                                  border: "none",
                                  padding: 2,
                                  display: "inline-flex",
                                  alignItems: "center",
                                  justifyContent: "center",
                                  color: item.is_holding ? "var(--warning)" : "rgba(255,255,255,0.15)",
                                  cursor: "pointer",
                                  transition: "all 0.2s"
                                }}
                                className={`action-btn-holdings ${item.is_holding ? "active" : ""}`}
                                title={item.is_holding ? "Remove from holdings" : "Add to holdings"}
                              >
                                <Briefcase size={12} fill={item.is_holding ? "var(--warning)" : "none"} />
                              </button>
                            </div>
                          </td>
                          <td>
                            <span className="truncate" style={{ color: "var(--text-secondary)", fontSize: "11px", display: "block" }}>
                              {item.name}
                            </span>
                          </td>
                          <td style={{ textAlign: "right", fontWeight: 700, color: "var(--text-primary)" }}>
                            {item.last_price > 0 ? `₹${item.last_price.toFixed(2)}` : "—"}
                          </td>
                          <td style={{ textAlign: "right" }}>
                            {item.last_price > 0 && (
                              <span style={{
                                fontWeight: 700,
                                fontSize: "11px",
                                color: isUp ? "var(--success-color)" : "var(--danger-color)"
                              }}>
                                {isUp ? "+" : ""}{item.change.toFixed(2)}%
                              </span>
                            )}
                          </td>
                          <td style={{ textAlign: "right", color: "var(--text-secondary)", fontSize: "11px" }}>
                            {item.close ? `₹${item.close.toFixed(2)}` : "—"}
                          </td>
                          <td style={{ textAlign: "right", color: "var(--positive-strong)", fontWeight: 600, fontSize: "11px" }}>
                            {item.high ? `₹${item.high.toFixed(2)}` : "—"}
                          </td>
                          <td
                            style={{ verticalAlign: "middle", padding: "8px 12px" }}
                            onMouseEnter={(e) => handleSentimentMouseEnter(e, item)}
                            onMouseLeave={handleSentimentMouseLeave}
                          >
                            {(() => {
                              const high = item.high || 0;
                              const low = item.low || 0;
                              const range = high - low;
                              const priceBuyPct = range > 0 ? ((item.last_price - low) / range) * 100 : 50;
                              const depthBuyPct = item.depth_buy_pct !== undefined ? item.depth_buy_pct : 50;
                              const compositeBuyPct = Math.round((priceBuyPct * 0.15) + (depthBuyPct * 0.85));
                              const compositeSellPct = 100 - compositeBuyPct;
                              const trend = sentimentTrend[item.instrument_key] || "flat";
                              return (
                                <div
                                  style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "2px", width: "90px", margin: "0 auto", cursor: "help" }}
                                >
                                  <div style={{ display: "flex", justifyContent: "space-between", width: "100%", fontSize: "9px", fontWeight: "800" }}>
                                    <span style={{ color: "var(--positive)", display: "flex", alignItems: "center", gap: "2px" }}>
                                      {compositeBuyPct}% B
                                      {trend === "up" && <span style={{ fontSize: "8px", color: "var(--positive-strong)", fontWeight: "900" }}>▲</span>}
                                      {trend === "down" && <span style={{ fontSize: "8px", color: "var(--negative-strong)", fontWeight: "900" }}>▼</span>}
                                    </span>
                                    <span style={{ color: "var(--negative)" }}>{compositeSellPct}% S</span>
                                  </div>
                                  <div style={{ width: "100%", height: "5px", backgroundColor: "var(--negative)", borderRadius: "3px", overflow: "hidden", display: "flex" }}>
                                    <div style={{ width: `${compositeBuyPct}%`, height: "100%", backgroundColor: "var(--positive)" }} />
                                  </div>
                                </div>
                              );
                            })()}
                          </td>
                          <td style={{ textAlign: "center", padding: "8px 6px" }}>
                            {(() => {
                              const buyQty = item.total_buy_qty || 0;
                              const sellQty = item.total_sell_qty || 0;
                              const totalQty = buyQty + sellQty;
                              if (totalQty === 0) return <span style={{ color: "var(--text-muted)", fontSize: "10px" }}>—</span>;
                              return (
                                <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "2px", width: "110px", margin: "0 auto" }}>
                                  <div style={{ display: "flex", justifyContent: "space-between", width: "100%", fontSize: "9px", fontWeight: "700" }}>
                                    <span style={{ color: "var(--positive)" }}>{formatQty(buyQty)}</span>
                                    <span style={{ color: "var(--negative)" }}>{formatQty(sellQty)}</span>
                                  </div>
                                  <div style={{ width: "100%", height: "4px", backgroundColor: "var(--negative)", borderRadius: "2px", overflow: "hidden", display: "flex" }}>
                                    <div style={{ width: `${(buyQty / totalQty) * 100}%`, height: "100%", backgroundColor: "var(--positive)" }} />
                                  </div>
                                  <div style={{ fontSize: "8px", color: "var(--text-muted)", fontWeight: "600" }}>
                                    Σ {formatQty(totalQty)}
                                  </div>
                                </div>
                              );
                            })()}
                          </td>
                          <td style={{ textAlign: "center", padding: "8px 6px" }}>
                            {(() => {
                              const sig = get2MinRecommendation(item);
                              return (
                                <span
                                  className={`trend-badge ${sig.badgeClass}`}
                                  title={`${sig.explanation}\n\nReliability Warning:\nThis micro-trend is based on high-frequency order-book data. Useful for near-term momentum, but vulnerable to spoofing. Use as helper, not standalone decision.`}
                                >
                                  {sig.recommendation}
                                  <span style={{ fontSize: "7px", opacity: 0.8, marginTop: "1px", fontWeight: "normal", display: "block" }}>
                                    Conf: {sig.confidence}
                                  </span>
                                </span>
                              );
                            })()}
                          </td>
                          <td>
                            {item.analysis?.recommendation ? (
                              <span className={`ai-badge ${item.analysis.recommendation.toLowerCase()}`}>
                                {item.analysis.recommendation}
                              </span>
                            ) : (
                              <span style={{ color: "var(--text-muted)", fontSize: "10px" }}>—</span>
                            )}
                          </td>
                          <td style={{ color: "var(--text-muted)", fontSize: "11px" }}>
                            {item.analysis?.sector || "—"}
                          </td>
                          <td
                            style={{
                              color: "var(--text-secondary)",
                              fontSize: "11px",
                              cursor: "help"
                            }}
                            onMouseEnter={(e) => handleMouseEnter(e, item)}
                            onMouseLeave={handleMouseLeave}
                          >
                            <span style={{ borderBottom: "1px dashed rgba(255, 255, 255, 0.3)" }}>
                              {item.analysis?.resistance_levels
                                ? item.analysis.resistance_levels.split(",")[0]
                                : "—"}
                            </span>
                          </td>
                          <td>
                            <div style={{ display: "flex", gap: "6px", justifyContent: "flex-end", alignItems: "center" }}>
                              <button
                                onClick={(e) => refreshSingleQuote(item, e)}
                                className="action-btn-refresh"
                                disabled={rowRefreshing[item.instrument_key]}
                                title="Refresh this stock's price — one request, works while paused"
                              >
                                <TrendingUp size={12} className={rowRefreshing[item.instrument_key] ? "animate-spin" : ""} />
                              </button>
                              <button
                                onClick={(e) => {
                                  e.stopPropagation();
                                  handleFetchNewsAndAI(item.instrument_key);
                                }}
                                className="action-btn-refresh"
                                disabled={detailsLoading[item.instrument_key]}
                                title="Refresh news & AI"
                              >
                                <RefreshCw size={12} className={detailsLoading[item.instrument_key] ? "animate-spin" : ""} />
                              </button>
                              <button
                                onClick={(e) => handleDeleteFromWatchlist(item.id, e)}
                                className="action-btn-danger"
                              >
                                <Trash2 size={12} />
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
          </section>

          {/* ===== TOP 50 MOVERS ===== */}
          <section className="data-section">
            <div className="section-toolbar">
              <h3 className="section-title">Top 50 Movers</h3>

              <div className="toggle-group">
                <button
                  onClick={() => setMoversTab("gainers")}
                  className={`toggle-btn ${moversTab === "gainers" ? "active-gain" : ""}`}
                >
                  Gainers
                </button>
                <button
                  onClick={() => setMoversTab("losers")}
                  className={`toggle-btn ${moversTab === "losers" ? "active-loss" : ""}`}
                >
                  Losers
                </button>
              </div>

              <LivePriceToggle
                paused={moversPaused}
                onToggle={() => setMoversPaused((v) => !v)}
                onRefreshAll={refreshLiveMovers}
                label="movers"
              />

              <div className="period-bar">
                {periods.map((p) => (
                  <button
                    key={p.val}
                    onClick={() => handleMoversPeriodChange(p.val)}
                    className={`period-pill ${moversPeriod === p.val ? "active" : ""}`}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            </div>

            {moversLoading ? (
              <div className="loading-state">
                <div style={{ display: "flex", flexDirection: "column", gap: "6px", width: "100%", padding: "0 16px" }}>
                  {[1, 2, 3, 4, 5].map((n) => (
                    <div key={n} style={{ height: "36px", borderRadius: "6px", backgroundColor: "rgba(255,255,255,0.02)" }} className="shimmer" />
                  ))}
                </div>
              </div>
            ) : (moversTab === "gainers" ? gainers : losers).length === 0 ? (
              <div className="empty-state">
                {authState.provider === "upstox" && !authState.authenticated ? (
                  <span>
                    Please{" "}
                    <button
                      onClick={handleAuthorize}
                      style={{
                        background: "none",
                        border: "none",
                        color: "var(--accent)",
                        textDecoration: "underline",
                        cursor: "pointer",
                        padding: 0,
                        font: "inherit",
                        fontWeight: "600",
                        transition: "color 0.2s"
                      }}
                      onMouseOver={(e) => e.currentTarget.style.color = "var(--accent)"}
                      onMouseOut={(e) => e.currentTarget.style.color = "var(--accent)"}
                    >
                      Authorize Upstox
                    </button>{" "}
                    to load top market movers.
                  </span>
                ) : (
                  "No movers data available."
                )}
              </div>
            ) : (
              <div className="table-scroll-container">
                <table className="stock-data-table">
                  <thead>
                    <tr>
                      <th className="col-symbol">Symbol</th>
                      <th className="col-name">Company</th>
                      <th className="col-ltp" style={{ textAlign: "right" }}>LTP</th>
                      <th className="col-change" style={{ textAlign: "right" }}>Change</th>
                      <th className="col-close" style={{ textAlign: "right" }}>Prev Close</th>
                      <th className="col-high" style={{ textAlign: "right" }}>Day High</th>
                      <th className="col-interest" style={{ textAlign: "center", width: "110px" }}>Sentiment (B/S)</th>
                      <th className="col-qty" style={{ textAlign: "center", width: "120px" }}>Buy/Sell Qty</th>
                      <th className="col-trend-signal" style={{ textAlign: "center", width: "115px" }}>
                        2m Signal
                        <span
                          title="Short-term (2 min) trend calculated from order book depth sentiment and quantity dynamics. Hover for details and reliability notice."
                          style={{ cursor: "help", fontSize: "10px", color: "var(--text-muted)", marginLeft: "4px" }}
                        >
                          ⓘ
                        </span>
                      </th>
                      <th className="col-ai">AI</th>
                      <th className="col-sector">Sector</th>
                      <th className="col-res">Resistance</th>
                      <th className="col-actions"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {(moversTab === "gainers" ? gainers : losers).map((item) => {
                      const isActive = selectedKey === item.instrument_key && isModalOpen;
                      const isUp = item.change >= 0;
                      const inWatchlist = watchlist.some((w) => w.instrument_key === item.instrument_key);
                      const flash = priceFlash[item.instrument_key];

                      return (
                        <tr
                          key={item.instrument_key}
                          onClick={() => {
                            setSelectedKey(item.instrument_key);
                            setIsModalOpen(true);
                          }}
                          className={`${isActive ? "active" : ""} ${flash === "up" ? "tick-up" : flash === "down" ? "tick-down" : ""
                            }`}
                        >
                          <td>
                            <span style={{ fontWeight: 700, color: "var(--text-primary)", fontSize: "12px" }}>
                              {item.symbol}
                            </span>
                          </td>
                          <td>
                            <span className="truncate" style={{ color: "var(--text-secondary)", fontSize: "11px", display: "block" }}>
                              {item.name}
                            </span>
                          </td>
                          <td style={{ textAlign: "right", fontWeight: 700, color: "var(--text-primary)" }}>
                            ₹{item.last_price.toFixed(2)}
                          </td>
                          <td style={{ textAlign: "right" }}>
                            <span style={{
                              fontWeight: 700,
                              fontSize: "11px",
                              color: isUp ? "var(--success-color)" : "var(--danger-color)"
                            }}>
                              {isUp ? "+" : ""}{item.change.toFixed(2)}%
                            </span>
                          </td>
                          <td style={{ textAlign: "right", color: "var(--text-secondary)", fontSize: "11px" }}>
                            {item.close ? `₹${item.close.toFixed(2)}` : "—"}
                          </td>
                          <td style={{ textAlign: "right", color: "var(--positive-strong)", fontWeight: 600, fontSize: "11px" }}>
                            {item.high ? `₹${item.high.toFixed(2)}` : "—"}
                          </td>
                          <td
                            style={{ verticalAlign: "middle", padding: "8px 12px" }}
                            onMouseEnter={(e) => handleSentimentMouseEnter(e, item)}
                            onMouseLeave={handleSentimentMouseLeave}
                          >
                            {(() => {
                              const high = item.high || 0;
                              const low = item.low || 0;
                              const range = high - low;
                              const priceBuyPct = range > 0 ? ((item.last_price - low) / range) * 100 : 50;
                              const depthBuyPct = item.depth_buy_pct !== undefined ? item.depth_buy_pct : 50;
                              const compositeBuyPct = Math.round((priceBuyPct * 0.15) + (depthBuyPct * 0.85));
                              const compositeSellPct = 100 - compositeBuyPct;
                              const trend = sentimentTrend[item.instrument_key] || "flat";
                              return (
                                <div
                                  style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "2px", width: "90px", margin: "0 auto", cursor: "help" }}
                                >
                                  <div style={{ display: "flex", justifyContent: "space-between", width: "100%", fontSize: "9px", fontWeight: "800" }}>
                                    <span style={{ color: "var(--positive)", display: "flex", alignItems: "center", gap: "2px" }}>
                                      {compositeBuyPct}% B
                                      {trend === "up" && <span style={{ fontSize: "8px", color: "var(--positive-strong)", fontWeight: "900" }}>▲</span>}
                                      {trend === "down" && <span style={{ fontSize: "8px", color: "var(--negative-strong)", fontWeight: "900" }}>▼</span>}
                                    </span>
                                    <span style={{ color: "var(--negative)" }}>{compositeSellPct}% S</span>
                                  </div>
                                  <div style={{ width: "100%", height: "5px", backgroundColor: "var(--negative)", borderRadius: "3px", overflow: "hidden", display: "flex" }}>
                                    <div style={{ width: `${compositeBuyPct}%`, height: "100%", backgroundColor: "var(--positive)" }} />
                                  </div>
                                </div>
                              );
                            })()}
                          </td>
                          <td style={{ textAlign: "center", padding: "8px 6px" }}>
                            {(() => {
                              const buyQty = item.total_buy_qty || 0;
                              const sellQty = item.total_sell_qty || 0;
                              const totalQty = buyQty + sellQty;
                              if (totalQty === 0) return <span style={{ color: "var(--text-muted)", fontSize: "10px" }}>—</span>;
                              return (
                                <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "2px", width: "110px", margin: "0 auto" }}>
                                  <div style={{ display: "flex", justifyContent: "space-between", width: "100%", fontSize: "9px", fontWeight: "700" }}>
                                    <span style={{ color: "var(--positive)" }}>{formatQty(buyQty)}</span>
                                    <span style={{ color: "var(--negative)" }}>{formatQty(sellQty)}</span>
                                  </div>
                                  <div style={{ width: "100%", height: "4px", backgroundColor: "var(--negative)", borderRadius: "2px", overflow: "hidden", display: "flex" }}>
                                    <div style={{ width: `${(buyQty / totalQty) * 100}%`, height: "100%", backgroundColor: "var(--positive)" }} />
                                  </div>
                                  <div style={{ fontSize: "8px", color: "var(--text-muted)", fontWeight: "600" }}>
                                    Σ {formatQty(totalQty)}
                                  </div>
                                </div>
                              );
                            })()}
                          </td>
                          <td style={{ textAlign: "center", padding: "8px 6px" }}>
                            {(() => {
                              const sig = get2MinRecommendation(item);
                              return (
                                <span
                                  className={`trend-badge ${sig.badgeClass}`}
                                  title={`${sig.explanation}\n\nReliability Warning:\nThis micro-trend is based on high-frequency order-book data. Useful for near-term momentum, but vulnerable to spoofing. Use as helper, not standalone decision.`}
                                >
                                  {sig.recommendation}
                                  <span style={{ fontSize: "7px", opacity: 0.8, marginTop: "1px", fontWeight: "normal", display: "block" }}>
                                    Conf: {sig.confidence}
                                  </span>
                                </span>
                              );
                            })()}
                          </td>
                          <td>
                            {item.analysis?.recommendation ? (
                              <span className={`ai-badge ${item.analysis.recommendation.toLowerCase()}`}>
                                {item.analysis.recommendation}
                              </span>
                            ) : (
                              <span style={{ color: "var(--text-muted)", fontSize: "10px" }}>—</span>
                            )}
                          </td>
                          <td style={{ color: "var(--text-muted)", fontSize: "11px" }}>
                            {item.analysis?.sector || "—"}
                          </td>
                          <td
                            style={{
                              color: "var(--text-secondary)",
                              fontSize: "11px",
                              cursor: "help"
                            }}
                            onMouseEnter={(e) => handleMouseEnter(e, item)}
                            onMouseLeave={handleMouseLeave}
                          >
                            <span style={{ borderBottom: "1px dashed rgba(255, 255, 255, 0.3)" }}>
                              {item.analysis?.resistance_levels
                                ? item.analysis.resistance_levels.split(",")[0]
                                : "—"}
                            </span>
                          </td>
                          <td>
                            <div style={{ display: "flex", gap: "6px", justifyContent: "flex-end", alignItems: "center" }}>
                              <button
                                onClick={(e) => refreshSingleQuote(item, e)}
                                className="action-btn-refresh"
                                disabled={rowRefreshing[item.instrument_key]}
                                title="Refresh this stock's price — one request, works while paused"
                              >
                                <TrendingUp size={12} className={rowRefreshing[item.instrument_key] ? "animate-spin" : ""} />
                              </button>
                              <button
                                onClick={(e) => {
                                  e.stopPropagation();
                                  handleFetchNewsAndAI(item.instrument_key);
                                }}
                                className="action-btn-refresh"
                                disabled={detailsLoading[item.instrument_key]}
                                title="Refresh news & AI"
                              >
                                <RefreshCw size={12} className={detailsLoading[item.instrument_key] ? "animate-spin" : ""} />
                              </button>
                              <button
                                onClick={(e) => {
                                  e.stopPropagation();
                                  if (!inWatchlist) {
                                    handleAddToWatchlist({
                                      symbol: item.symbol,
                                      name: item.name,
                                      key: item.instrument_key
                                    });
                                  }
                                }}
                                className={`action-btn-add ${inWatchlist ? "in-watchlist" : ""}`}
                                disabled={inWatchlist}
                              >
                                {inWatchlist ? "✓" : <Plus size={12} />}
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
          </section>

        </div>
      )}

      {/* Detail Popup Modal */}
      {isModalOpen && selectedKey && (() => {
        const detail = detailsMap[selectedKey];
        const isLoading = detailsLoading[selectedKey];

        let symbol = selectedKey.split("|").pop() || "STOCK";
        let name = "NSE Equity Stock";
        let ltp = 0.0;
        let change = 0.0;

        const wlItem = watchlist.find((w) => w.instrument_key === selectedKey);
        if (wlItem) {
          symbol = wlItem.symbol;
          name = wlItem.name;
          ltp = wlItem.last_price;
          change = wlItem.change;
        } else {
          const moverItem = [...gainers, ...losers].find((m) => m.instrument_key === selectedKey);
          if (moverItem) {
            symbol = moverItem.symbol;
            name = moverItem.name;
            ltp = moverItem.last_price;
            change = moverItem.change;
          } else if (detail) {
            symbol = detail.instrument_key.split("|").pop() || "STOCK";
            name = detail.name;
            ltp = detail.quote.last_price;
            const closePrice = detail.quote.ohlc.close;
            change = closePrice > 0 ? ((ltp - closePrice) / closePrice) * 100 : 0.0;
          }
        }

        const isUp = change >= 0;
        const inWatchlist = watchlist.some((w) => w.instrument_key === selectedKey);
        const wlObj = watchlist.find((w) => w.instrument_key === selectedKey);

        return (
          <div style={{
            position: "fixed",
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            backgroundColor: "rgba(20, 23, 28, 0.8)",
            backdropFilter: "blur(20px)",
            WebkitBackdropFilter: "blur(20px)",
            zIndex: 10000,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: "24px"
          }}
            onClick={() => setIsModalOpen(false)}
          >
            <div style={{
              width: "100%",
              maxWidth: "960px",
              maxHeight: "90vh",
              background: "linear-gradient(135deg, rgba(40, 44, 52, 0.95) 0%, rgba(25, 28, 34, 0.98) 100%)",
              border: "1px solid rgba(255, 255, 255, 0.1)",
              borderRadius: "16px",
              boxShadow: "0 24px 60px rgba(0, 0, 0, 0.8)",
              display: "flex",
              flexDirection: "column",
              overflow: "hidden"
            }}
              onClick={(e) => e.stopPropagation()}
            >
              {/* Modal Header */}
              <div style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "16px 20px",
                borderBottom: "1px solid rgba(255, 255, 255, 0.08)",
                backgroundColor: "rgba(25, 28, 34, 0.4)",
                flexShrink: 0
              }}>
                <div style={{ display: "flex", alignItems: "center", gap: "14px" }}>
                  <div style={{
                    padding: "6px 12px",
                    borderRadius: "8px",
                    background: "var(--accent)",
                    color: "var(--on-accent)",
                    fontWeight: "800",
                    fontSize: "16px",
                    boxShadow: "0 4px 12px rgba(127, 166, 225, 0.18)"
                  }}>
                    {symbol}
                  </div>
                  <div>
                    <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                      <h2 style={{ fontSize: "16px", fontWeight: "800", color: "var(--text-primary)", margin: 0 }}>{name}</h2>
                      <span className="badge-nse">NSE</span>
                    </div>
                    <p style={{ fontSize: "9px", color: "var(--text-muted)", margin: "2px 0 0 0" }}>KEY: {selectedKey}</p>
                  </div>
                </div>

                <div style={{ display: "flex", alignItems: "center", gap: "16px" }}>
                  <div style={{ textAlign: "right" }}>
                    <div style={{ fontSize: "18px", fontWeight: "900", color: "var(--text-primary)" }}>
                      {ltp > 0 ? `₹${ltp.toFixed(2)}` : "—"}
                    </div>
                    {ltp > 0 && (
                      <span className={`pill-change ${isUp ? "positive" : "negative"}`} style={{ marginTop: "2px", display: "inline-block" }}>
                        {isUp ? "+" : ""}{change.toFixed(2)}%
                      </span>
                    )}
                  </div>

                  <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                    {inWatchlist ? (
                      <button
                        onClick={(e) => wlObj && handleDeleteFromWatchlist(wlObj.id, e)}
                        style={{
                          padding: "6px 10px",
                          fontSize: "11px",
                          fontWeight: "700",
                          borderRadius: "6px",
                          backgroundColor: "rgba(226, 141, 131, 0.08)",
                          color: "var(--negative-strong)",
                          border: "1px solid rgba(226, 141, 131, 0.16)"
                        }}
                      >
                        Remove Watchlist
                      </button>
                    ) : (
                      <button
                        onClick={() => handleAddToWatchlist({ symbol, name, key: selectedKey })}
                        style={{
                          padding: "6px 10px",
                          fontSize: "11px",
                          fontWeight: "700",
                          borderRadius: "6px",
                          backgroundColor: "var(--accent)",
                          color: "var(--on-accent)",
                          boxShadow: "0 4px 12px rgba(127, 166, 225, 0.13)"
                        }}
                      >
                        Add Watchlist
                      </button>
                    )}

                    <button
                      onClick={() => setIsModalOpen(false)}
                      style={{
                        width: "30px",
                        height: "30px",
                        borderRadius: "50%",
                        backgroundColor: "rgba(255, 255, 255, 0.05)",
                        color: "var(--text-primary)",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        border: "1px solid rgba(255, 255, 255, 0.08)"
                      }}
                    >
                      <X size={14} />
                    </button>
                  </div>
                </div>
              </div>

              {/* Modal Body */}
              <div className="modal-scroll-area" style={{
                flex: 1,
                overflowY: "auto",
                padding: "20px",
                display: "flex",
                flexDirection: "column",
                gap: "20px",
                minHeight: 0,
                maxHeight: "calc(90vh - 70px)"
              }}>
                {isLoading && !detail ? (
                  <div style={{ display: "flex", flexDirection: "column", padding: "80px 0", justifyContent: "center", alignItems: "center", gap: "12px" }}>
                    <RefreshCw className="animate-spin" style={{ color: "var(--accent-color)" }} size={24} />
                    <p style={{ fontSize: "13px", color: "var(--text-secondary)", fontWeight: "500" }}>
                      Loading candlestick charts and news feeds...
                    </p>
                  </div>
                ) : !detail ? (
                  <div style={{
                    textAlign: "center",
                    padding: "60px 24px",
                    border: "1px dashed var(--border-color)",
                    borderRadius: "12px",
                    background: "rgba(40, 44, 52,0.1)",
                    color: "var(--text-secondary)",
                    fontSize: "13px"
                  }}>
                    Failed to load stock detail. Please make sure the feed is connected.
                  </div>
                ) : (
                  <>
                    {/* Stats Grid */}
                    <div className="stats-grid">
                      {[
                        { label: "Open Price", val: detail.quote.ohlc.open },
                        { label: "Day High", val: detail.quote.ohlc.high, color: "var(--positive-strong)" },
                        { label: "Day Low", val: detail.quote.ohlc.low, color: "var(--negative-strong)" },
                        { label: "Prev Close", val: detail.quote.ohlc.close },
                        { label: "Traded Volume", val: detail.quote.volume }
                      ].map((stat, idx) => (
                        <div key={idx} className="stats-card">
                          <span className="stats-label">{stat.label}</span>
                          <span className="stats-value" style={{ color: stat.color || "var(--text-primary)" }}>
                            {stat.label.includes("Volume") ? stat.val.toLocaleString() : (stat.val ? `₹${stat.val.toFixed(2)}` : "—")}
                          </span>
                        </div>
                      ))}
                    </div>

                    {/* Candlestick Chart */}
                    <div className="chart-card">
                      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "12px", borderBottom: "1px solid rgba(255,255,255,0.04)", paddingBottom: "8px" }}>
                        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                          <span style={{ width: "8px", height: "8px", borderRadius: "50%", backgroundColor: "var(--accent)", boxShadow: "0 0 8px var(--accent)" }}></span>
                          <span style={{ fontSize: "11px", fontWeight: "700", color: "var(--on-accent)", textTransform: "uppercase", letterSpacing: "0.5px" }}>
                            Stock Performance Chart
                          </span>
                        </div>
                        <div style={{
                          display: "flex",
                          gap: "4px",
                          backgroundColor: "rgba(255,255,255,0.03)",
                          padding: "2px",
                          borderRadius: "6px",
                          border: "1px solid rgba(255,255,255,0.05)"
                        }}>
                          {["1D", "5D", "1M", "3M", "6M", "1Y", "5Y"].map((p) => (
                            <button
                              key={p}
                              onClick={() => setChartPeriod(p)}
                              style={{
                                padding: "3px 8px",
                                fontSize: "10px",
                                fontWeight: "700",
                                borderRadius: "4px",
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
                      </div>

                      {chartLoading ? (
                        <div style={{ height: "380px", display: "flex", flexDirection: "column", justifyContent: "center", alignItems: "center", gap: "10px" }}>
                          <RefreshCw className="animate-spin" style={{ color: "var(--accent-color)" }} size={20} />
                          <p style={{ fontSize: "11px", color: "var(--text-secondary)" }}>Loading historical candles for {chartPeriod}...</p>
                        </div>
                      ) : chartCandles && chartCandles.length > 0 ? (
                        <Chart candles={chartCandles} period={chartPeriod} />
                      ) : (
                        <div style={{ height: "380px", display: "flex", justifyContent: "center", alignItems: "center", fontSize: "11px", color: "var(--text-muted)" }}>
                          Chart candles are currently unavailable for this period.
                        </div>
                      )}
                    </div>

                    {/* AI & News Splits */}
                    <div className="details-split-grid">
                      {/* AI Commentary & Metrics Card */}
                      <div className="info-card" style={{ background: "linear-gradient(135deg, rgba(40, 44, 52,0.4) 0%, rgba(127, 166, 225,0.03) 100%)", minHeight: "340px", display: "flex", flexDirection: "column" }}>
                        {/* Tab Headers */}
                        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "14px", borderBottom: "1px solid rgba(255,255,255,0.08)", paddingBottom: "8px", flexShrink: 0 }}>
                          <div style={{ display: "flex", gap: "16px" }}>
                            <button
                              onClick={() => setChatTab("analysis")}
                              style={{
                                display: "flex",
                                alignItems: "center",
                                gap: "6px",
                                background: "none",
                                border: "none",
                                padding: "4px 8px",
                                borderBottom: chatTab === "analysis" ? "2px solid var(--accent)" : "2px solid transparent",
                                color: chatTab === "analysis" ? "var(--on-accent)" : "var(--text-muted)",
                                fontWeight: "700",
                                fontSize: "11px",
                                textTransform: "uppercase",
                                transition: "all 0.2s"
                              }}
                            >
                              <Brain size={14} />
                              AI Analysis
                            </button>
                            <button
                              onClick={() => setChatTab("chat")}
                              style={{
                                display: "flex",
                                alignItems: "center",
                                gap: "6px",
                                background: "none",
                                border: "none",
                                padding: "4px 8px",
                                borderBottom: chatTab === "chat" ? "2px solid var(--accent)" : "2px solid transparent",
                                color: chatTab === "chat" ? "var(--on-accent)" : "var(--text-muted)",
                                fontWeight: "700",
                                fontSize: "11px",
                                textTransform: "uppercase",
                                transition: "all 0.2s"
                              }}
                            >
                              <Send size={12} />
                              Ask AI Assistant
                            </button>
                            <button
                              onClick={() => setChatTab("depth")}
                              style={{
                                display: "flex",
                                alignItems: "center",
                                gap: "6px",
                                background: "none",
                                border: "none",
                                padding: "4px 8px",
                                borderBottom: chatTab === "depth" ? "2px solid var(--accent)" : "2px solid transparent",
                                color: chatTab === "depth" ? "var(--on-accent)" : "var(--text-muted)",
                                fontWeight: "700",
                                fontSize: "11px",
                                textTransform: "uppercase",
                                transition: "all 0.2s"
                              }}
                            >
                              <Layers size={12} />
                              Order Book
                            </button>
                          </div>
                          {chatTab === "analysis" && detail.analysis?.fetched_at && (
                            <span style={{ fontSize: "9px", color: "var(--text-muted)" }}>
                              Updated {formatLastUpdated(detail.analysis.fetched_at)}
                            </span>
                          )}
                        </div>

                        {/* Switchable Views */}
                        {chatTab === "analysis" ? (
                          <div style={{ display: "flex", flexDirection: "column", justifyContent: "space-between", flex: 1 }}>
                            <div>
                              {/* Sector, Recommendation, Resistance Grid */}
                              <div style={{
                                display: "grid",
                                gridTemplateColumns: "1fr 1fr",
                                gap: "12px",
                                marginBottom: "16px",
                                padding: "10px",
                                borderRadius: "8px",
                                backgroundColor: "rgba(255,255,255,0.02)",
                                border: "1px solid rgba(255,255,255,0.04)"
                              }}>
                                <div>
                                  <span style={{ fontSize: "9px", color: "var(--text-muted)", textTransform: "uppercase", display: "block" }}>Sector</span>
                                  <span style={{ fontSize: "12px", color: "var(--text-primary)", fontWeight: "600" }}>
                                    {detail.analysis?.sector || "General Market"}
                                  </span>
                                </div>
                                <div>
                                  <span style={{ fontSize: "9px", color: "var(--text-muted)", textTransform: "uppercase", display: "block" }}>Consensus Recommendation</span>
                                  {detail.analysis?.recommendation ? (
                                    <span style={{
                                      padding: "2px 8px",
                                      borderRadius: "4px",
                                      fontSize: "10px",
                                      fontWeight: "800",
                                      textTransform: "uppercase",
                                      display: "inline-block",
                                      marginTop: "2px",
                                      backgroundColor: detail.analysis.recommendation === "BUY"
                                        ? "rgba(91, 190, 147, 0.1)"
                                        : detail.analysis.recommendation === "SELL"
                                          ? "rgba(226, 141, 131, 0.1)"
                                          : "rgba(216, 174, 100, 0.1)",
                                      color: detail.analysis.recommendation === "BUY"
                                        ? "var(--success-color)"
                                        : detail.analysis.recommendation === "SELL"
                                          ? "var(--danger-color)"
                                          : "var(--warning)",
                                      border: `1px solid ${detail.analysis.recommendation === "BUY"
                                          ? "rgba(91, 190, 147, 0.18)"
                                          : detail.analysis.recommendation === "SELL"
                                            ? "rgba(226, 141, 131, 0.18)"
                                            : "rgba(216, 174, 100, 0.18)"
                                        }`
                                    }}>
                                      {detail.analysis.recommendation}
                                    </span>
                                  ) : (
                                    <span style={{ fontSize: "11px", color: "var(--text-muted)" }}>None</span>
                                  )}
                                </div>
                                <div>
                                  <span style={{ fontSize: "9px", color: "var(--text-muted)", textTransform: "uppercase", display: "block" }}>Resistance Levels</span>
                                  <span style={{ fontSize: "12px", color: "var(--text-primary)", fontWeight: "600" }}>
                                    {detail.analysis?.resistance_levels || "Not calculated"}
                                  </span>
                                </div>
                                <div>
                                  <span style={{ fontSize: "9px", color: "var(--text-muted)", textTransform: "uppercase", display: "block" }}>Support Levels</span>
                                  <span style={{ fontSize: "12px", color: "var(--text-primary)", fontWeight: "600" }}>
                                    {detail.analysis?.support_levels || "Not calculated"}
                                  </span>
                                </div>
                              </div>

                              {detail.analysis?.comment ? (
                                <p style={{ fontSize: "12.5px", color: "var(--text-primary)", lineHeight: "1.7", fontStyle: "italic", fontWeight: "500", marginTop: "12px" }}>
                                  "{detail.analysis.comment}"
                                </p>
                              ) : (
                                <p style={{ fontSize: "11px", color: "var(--text-muted)", fontStyle: "italic", marginTop: "12px" }}>
                                  No AI commentary generated for this stock. Click "Analyze Feeds (AI)" below to scan market context and generate indicators.
                                </p>
                              )}

                              {/* Analyst Recommendations Consensus */}
                              {detail.analysis?.analyst_recommendations && detail.analysis.analyst_recommendations.length > 0 && (
                                <div style={{ marginTop: "16px", borderTop: "1px solid rgba(255,255,255,0.06)", paddingTop: "12px" }}>
                                  <span style={{ fontSize: "9px", color: "var(--text-muted)", textTransform: "uppercase", display: "block", marginBottom: "8px", fontWeight: "700", letterSpacing: "0.5px" }}>
                                    Analyst Recommendations Consensus
                                  </span>
                                  <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
                                    {detail.analysis.analyst_recommendations.map((rec, idx) => (
                                      <div key={idx} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", fontSize: "11px", background: "rgba(255,255,255,0.015)", padding: "5px 10px", borderRadius: "6px", border: "1px solid rgba(255,255,255,0.02)" }}>
                                        <span style={{ color: "var(--text-primary)", fontWeight: "500" }}>{rec.analyst_firm}</span>
                                        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                                          <span style={{
                                            fontWeight: "800",
                                            fontSize: "9px",
                                            padding: "2px 6px",
                                            borderRadius: "4px",
                                            backgroundColor: rec.recommendation === "BUY"
                                              ? "rgba(91, 190, 147, 0.09)"
                                              : rec.recommendation === "SELL"
                                                ? "rgba(226, 141, 131, 0.09)"
                                                : "rgba(216, 174, 100, 0.09)",
                                            color: rec.recommendation === "BUY"
                                              ? "var(--success-color)"
                                              : rec.recommendation === "SELL"
                                                ? "var(--danger-color)"
                                                : "var(--warning)",
                                            border: `1px solid ${rec.recommendation === "BUY"
                                                ? "rgba(91, 190, 147, 0.13)"
                                                : rec.recommendation === "SELL"
                                                  ? "rgba(226, 141, 131, 0.13)"
                                                  : "rgba(216, 174, 100, 0.13)"
                                              }`
                                          }}>
                                            {rec.recommendation}
                                          </span>
                                          <span style={{ color: "var(--text-muted)", fontSize: "10px" }}>{rec.date}</span>
                                        </div>
                                      </div>
                                    ))}
                                  </div>
                                </div>
                              )}
                            </div>

                            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: "24px" }}>
                              <span style={{ fontSize: "9px", color: "var(--text-muted)", fontWeight: "600", letterSpacing: "1px" }}>
                                GEMINI AI ANALYST
                              </span>
                              <button
                                onClick={() => handleFetchNewsAndAI(selectedKey)}
                                disabled={isLoading}
                                style={{
                                  display: "flex",
                                  alignItems: "center",
                                  gap: "6px",
                                  padding: "8px 16px",
                                  fontSize: "11px",
                                  fontWeight: "600",
                                  borderRadius: "6px",
                                  backgroundColor: "var(--accent)",
                                  color: "var(--on-accent)"
                                }}
                              >
                                {isLoading ? (
                                  <RefreshCw size={11} className="animate-spin" />
                                ) : (
                                  <Brain size={12} />
                                )}
                                Analyze Feeds (AI)
                              </button>
                            </div>
                          </div>
                        ) : chatTab === "chat" ? (
                          /* AI Chat Assistant View */
                          <div className="chat-container">
                            <div className="chat-log modal-scroll-area">
                              {(chatHistory[selectedKey] || []).length === 0 ? (
                                <div style={{
                                  display: "flex",
                                  flexDirection: "column",
                                  alignItems: "center",
                                  justifyContent: "center",
                                  height: "100%",
                                  padding: "20px",
                                  textAlign: "center",
                                  color: "var(--text-muted)",
                                  gap: "10px"
                                }}>
                                  <Brain size={32} style={{ opacity: 0.5, color: "var(--accent)" }} />
                                  <p style={{ fontSize: "12px", fontWeight: "500" }}>
                                    Ask me anything about {symbol}! For example:
                                  </p>
                                  <div style={{ display: "flex", flexDirection: "column", gap: "6px", width: "100%", maxWidth: "260px" }}>
                                    {[
                                      "What is the sector of this company?",
                                      "Summarize the latest news.",
                                      "What are the target resistance levels?"
                                    ].map((q, idx) => (
                                      <button
                                        key={idx}
                                        onClick={() => setChatInput(q)}
                                        style={{
                                          padding: "6px 10px",
                                          fontSize: "10.5px",
                                          borderRadius: "6px",
                                          backgroundColor: "rgba(255,255,255,0.03)",
                                          border: "1px solid rgba(255,255,255,0.05)",
                                          color: "var(--text-secondary)",
                                          textAlign: "left"
                                        }}
                                        onMouseOver={(e) => {
                                          e.currentTarget.style.backgroundColor = "rgba(127, 166, 225, 0.08)";
                                          e.currentTarget.style.borderColor = "rgba(127, 166, 225, 0.13)";
                                        }}
                                        onMouseOut={(e) => {
                                          e.currentTarget.style.backgroundColor = "rgba(255,255,255,0.03)";
                                          e.currentTarget.style.borderColor = "rgba(255,255,255,0.05)";
                                        }}
                                      >
                                        {q}
                                      </button>
                                    ))}
                                  </div>
                                </div>
                              ) : (
                                <>
                                  {(chatHistory[selectedKey] || []).map((msg, idx) => (
                                    <div
                                      key={idx}
                                      className={`chat-bubble ${msg.role}`}
                                    >
                                      {msg.content}
                                    </div>
                                  ))}
                                  {chatLoading && (
                                    <div className="chat-bubble assistant">
                                      <span className="chat-typing">Typing...</span>
                                    </div>
                                  )}
                                  <div ref={chatEndRef} />
                                </>
                              )}
                            </div>

                            {/* Chat input form */}
                            <form
                              onSubmit={(e) => {
                                e.preventDefault();
                                handleSendChatMessage(selectedKey);
                              }}
                              className="chat-input-form"
                            >
                              <input
                                type="text"
                                value={chatInput}
                                onChange={(e) => setChatInput(e.target.value)}
                                placeholder={`Ask about ${symbol}...`}
                                className="chat-input-field"
                                disabled={chatLoading}
                              />
                              <button
                                type="submit"
                                className="chat-send-btn"
                                disabled={chatLoading || !chatInput.trim()}
                              >
                                <Send size={12} />
                              </button>
                            </form>
                          </div>
                        ) : (
                          /* Real-time Order Book (Depth) View */
                          <div style={{ display: "flex", flexDirection: "column", gap: "14px", flex: 1, overflow: "hidden" }}>
                            {/* OBI Summary */}
                            <div style={{ padding: "10px", borderRadius: "8px", backgroundColor: "rgba(255,255,255,0.02)", border: "1px solid rgba(255,255,255,0.04)" }}>
                              <div style={{ display: "flex", justifyContent: "space-between", fontSize: "11px", fontWeight: "700", marginBottom: "6px" }}>
                                <span style={{ color: "var(--text-secondary)" }}>ORDER BOOK PRESSURE</span>
                                <span style={{ color: "var(--accent)" }}>WEIGHTED DISTANCE IMBALANCE</span>
                              </div>
                              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: "4px" }}>
                                <span style={{ fontSize: "18px", fontWeight: "800", color: "var(--positive)" }}>
                                  {detail?.quote?.depth_buy_pct !== undefined ? detail.quote.depth_buy_pct : 50}%
                                  <span style={{ fontSize: "10px", fontWeight: "700", color: "var(--text-muted)", marginLeft: "4px" }}>BIDS</span>
                                </span>
                                <span style={{ fontSize: "18px", fontWeight: "800", color: "var(--negative)" }}>
                                  {detail?.quote?.depth_sell_pct !== undefined ? detail.quote.depth_sell_pct : 50}%
                                  <span style={{ fontSize: "10px", fontWeight: "700", color: "var(--text-muted)", marginRight: "4px" }}>ASKS</span>
                                </span>
                              </div>
                              <div style={{ width: "100%", height: "8px", backgroundColor: "rgba(226, 141, 131, 0.18)", borderRadius: "4px", overflow: "hidden", display: "flex" }}>
                                <div style={{
                                  width: `${detail?.quote?.depth_buy_pct !== undefined ? detail.quote.depth_buy_pct : 50}%`,
                                  height: "100%",
                                  backgroundColor: "var(--positive)",
                                  boxShadow: "0 0 8px var(--positive)"
                                }} />
                              </div>
                            </div>

                            {/* Spread and Totals Cards */}
                            {(() => {
                              const buyLevels = detail?.quote?.depth?.buy || [];
                              const sellLevels = detail?.quote?.depth?.sell || [];
                              const totalBuyQty = buyLevels.reduce((acc, curr) => acc + (curr.quantity || 0), 0);
                              const totalSellQty = sellLevels.reduce((acc, curr) => acc + (curr.quantity || 0), 0);
                              const maxQty = Math.max(
                                1,
                                ...buyLevels.map(b => b.quantity || 0),
                                ...sellLevels.map(s => s.quantity || 0)
                              );

                              const bestBid = buyLevels[0]?.price || 0;
                              const bestAsk = sellLevels[0]?.price || 0;
                              const spread = bestAsk > 0 && bestBid > 0 ? (bestAsk - bestBid) : 0;
                              const spreadPct = bestBid > 0 ? (spread / bestBid) * 100 : 0;

                              return (
                                <>
                                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "8px" }}>
                                    <div style={{ padding: "8px", borderRadius: "6px", backgroundColor: "rgba(255,255,255,0.015)", border: "1px solid rgba(255,255,255,0.03)", textAlign: "center" }}>
                                      <span style={{ display: "block", fontSize: "8.5px", color: "var(--text-muted)", textTransform: "uppercase", fontWeight: "700" }}>Bid-Ask Spread</span>
                                      <span style={{ display: "block", fontSize: "11px", fontWeight: "700", color: "var(--text-primary)", marginTop: "2px" }}>
                                        {spread > 0 ? `₹${spread.toFixed(2)} (${spreadPct.toFixed(2)}%)` : "—"}
                                      </span>
                                    </div>
                                    <div style={{ padding: "8px", borderRadius: "6px", backgroundColor: "rgba(255,255,255,0.015)", border: "1px solid rgba(255,255,255,0.03)", textAlign: "center" }}>
                                      <span style={{ display: "block", fontSize: "8.5px", color: "var(--text-muted)", textTransform: "uppercase", fontWeight: "700" }}>Total Bid Qty</span>
                                      <span style={{ display: "block", fontSize: "11px", fontWeight: "700", color: "var(--positive-strong)", marginTop: "2px" }}>
                                        {totalBuyQty.toLocaleString()}
                                      </span>
                                    </div>
                                    <div style={{ padding: "8px", borderRadius: "6px", backgroundColor: "rgba(255,255,255,0.015)", border: "1px solid rgba(255,255,255,0.03)", textAlign: "center" }}>
                                      <span style={{ display: "block", fontSize: "8.5px", color: "var(--text-muted)", textTransform: "uppercase", fontWeight: "700" }}>Total Ask Qty</span>
                                      <span style={{ display: "block", fontSize: "11px", fontWeight: "700", color: "var(--negative-strong)", marginTop: "2px" }}>
                                        {totalSellQty.toLocaleString()}
                                      </span>
                                    </div>
                                  </div>

                                  {/* Bids vs Asks side by side */}
                                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "10px", flex: 1, minHeight: "180px" }}>
                                    {/* Bids Column */}
                                    <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
                                      <div style={{ display: "grid", gridTemplateColumns: "1fr 1.2fr 0.6fr", padding: "4px 6px", fontSize: "9px", color: "var(--text-muted)", fontWeight: "700", borderBottom: "1px solid rgba(255,255,255,0.05)" }}>
                                        <span>BID PRICE</span>
                                        <span style={{ textAlign: "right" }}>QUANTITY</span>
                                        <span style={{ textAlign: "right" }}>ORDERS</span>
                                      </div>
                                      <div style={{ display: "flex", flexDirection: "column", gap: "2px", overflowY: "auto", flex: 1 }}>
                                        {buyLevels.length === 0 ? (
                                          <div style={{ display: "flex", justifyContent: "center", alignItems: "center", height: "100%", fontSize: "10px", color: "var(--text-muted)", fontStyle: "italic" }}>
                                            No active bids
                                          </div>
                                        ) : (
                                          buyLevels.map((bid, idx) => {
                                            const pct = Math.min(100, Math.round(((bid.quantity || 0) / maxQty) * 100));
                                            return (
                                              <div
                                                key={idx}
                                                style={{
                                                  display: "grid",
                                                  gridTemplateColumns: "1fr 1.2fr 0.6fr",
                                                  padding: "5px 6px",
                                                  fontSize: "10.5px",
                                                  borderRadius: "4px",
                                                  background: `linear-gradient(to right, rgba(91, 190, 147, 0.08) ${pct}%, transparent ${pct}%)`,
                                                  border: "1px solid rgba(255,255,255,0.01)"
                                                }}
                                              >
                                                <span style={{ color: "var(--positive-strong)", fontWeight: "700" }}>₹{(bid.price || 0).toFixed(2)}</span>
                                                <span style={{ color: "var(--text-primary)", textAlign: "right", fontWeight: "600" }}>{(bid.quantity || 0).toLocaleString()}</span>
                                                <span style={{ color: "var(--text-muted)", textAlign: "right" }}>{bid.orders || 0}</span>
                                              </div>
                                            );
                                          })
                                        )}
                                      </div>
                                    </div>

                                    {/* Asks Column */}
                                    <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
                                      <div style={{ display: "grid", gridTemplateColumns: "1fr 1.2fr 0.6fr", padding: "4px 6px", fontSize: "9px", color: "var(--text-muted)", fontWeight: "700", borderBottom: "1px solid rgba(255,255,255,0.05)" }}>
                                        <span>ASK PRICE</span>
                                        <span style={{ textAlign: "right" }}>QUANTITY</span>
                                        <span style={{ textAlign: "right" }}>ORDERS</span>
                                      </div>
                                      <div style={{ display: "flex", flexDirection: "column", gap: "2px", overflowY: "auto", flex: 1 }}>
                                        {sellLevels.length === 0 ? (
                                          <div style={{ display: "flex", justifyContent: "center", alignItems: "center", height: "100%", fontSize: "10px", color: "var(--text-muted)", fontStyle: "italic" }}>
                                            No active asks
                                          </div>
                                        ) : (
                                          sellLevels.map((ask, idx) => {
                                            const pct = Math.min(100, Math.round(((ask.quantity || 0) / maxQty) * 100));
                                            return (
                                              <div
                                                key={idx}
                                                style={{
                                                  display: "grid",
                                                  gridTemplateColumns: "1fr 1.2fr 0.6fr",
                                                  padding: "5px 6px",
                                                  fontSize: "10.5px",
                                                  borderRadius: "4px",
                                                  background: `linear-gradient(to left, rgba(226, 141, 131, 0.08) ${pct}%, transparent ${pct}%)`,
                                                  border: "1px solid rgba(255,255,255,0.01)"
                                                }}
                                              >
                                                <span style={{ color: "var(--negative-strong)", fontWeight: "700" }}>₹{(ask.price || 0).toFixed(2)}</span>
                                                <span style={{ color: "var(--text-primary)", textAlign: "right", fontWeight: "600" }}>{(ask.quantity || 0).toLocaleString()}</span>
                                                <span style={{ color: "var(--text-muted)", textAlign: "right" }}>{ask.orders || 0}</span>
                                              </div>
                                            );
                                          })
                                        )}
                                      </div>
                                    </div>
                                  </div>
                                </>
                              );
                            })()}

                            <p style={{ margin: 0, fontSize: "9px", color: "var(--text-muted)", textAlign: "center", fontStyle: "italic" }}>
                              * Real-time bid/ask snapshots weighted by proximity to Last Traded Price (LTP).
                            </p>
                          </div>
                        )}
                      </div>

                      {/* News Column */}
                      <div className="info-card" style={{ minHeight: "320px", display: "flex", flexDirection: "column", justifyContent: "space-between" }}>
                        <div>
                          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "14px", borderBottom: "1px solid rgba(255,255,255,0.04)", paddingBottom: "8px" }}>
                            <div style={{ display: "flex", alignItems: "center", gap: "8px", color: "var(--positive-strong)" }}>
                              <Newspaper size={14} />
                              <span style={{ fontSize: "11px", fontWeight: "700", textTransform: "uppercase", color: "var(--text-primary)" }}>
                                Company News Feed
                              </span>
                            </div>
                            <button
                              onClick={() => handleFetchNewsAndAI(selectedKey)}
                              disabled={isLoading}
                              style={{ background: "transparent", color: "var(--accent)", fontSize: "10px", fontWeight: "600", display: "flex", alignItems: "center", gap: "4px" }}
                            >
                              {isLoading ? <RefreshCw size={9} className="animate-spin" /> : <RefreshCw size={9} />}
                              Refresh
                            </button>
                          </div>

                          {detail.news && detail.news.length > 0 ? (
                            <div className="news-scroll-area" style={{ maxHeight: "240px", overflowY: "auto" }}>
                              {detail.news.map((n: any, idx: number) => (
                                <div key={idx} style={{ display: "flex", flexDirection: "column", gap: "4px", borderBottom: "1px solid rgba(255,255,255,0.02)", paddingBottom: "10px", marginBottom: "10px" }}>
                                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: "8px", color: "var(--text-muted)" }}>
                                    <span style={{ fontWeight: "700", color: "var(--positive-strong)" }}>{n.source}</span>
                                    <span>{new Date(n.published_at).toLocaleDateString()}</span>
                                  </div>
                                  <a
                                    href={n.url}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    style={{ fontSize: "11px", fontWeight: "700", color: "var(--text-primary)", textDecoration: "none", display: "flex", alignItems: "center", gap: "4px" }}
                                    onMouseOver={(e) => e.currentTarget.style.color = "var(--accent)"}
                                    onMouseOut={(e) => e.currentTarget.style.color = "var(--text-primary)"}
                                  >
                                    {n.headline}
                                    <ExternalLink size={8} style={{ flexShrink: 0 }} />
                                  </a>
                                  <p style={{ fontSize: "9.5px", color: "var(--text-secondary)", lineHeight: "1.4" }} className="line-clamp-2">
                                    {n.summary}
                                  </p>
                                </div>
                              ))}
                            </div>
                          ) : (
                            <div style={{ flex: 1, display: "flex", flexDirection: "column", justifyContent: "center", alignItems: "center", padding: "40px 0" }}>
                              <Newspaper size={20} style={{ color: "var(--text-muted)", marginBottom: "4px" }} />
                              <p style={{ fontSize: "11px", color: "var(--text-muted)" }}>No cached news articles.</p>
                              <button
                                onClick={() => handleFetchNewsAndAI(selectedKey)}
                                style={{
                                  marginTop: "8px",
                                  padding: "6px 12px",
                                  fontSize: "10px",
                                  fontWeight: "600",
                                  borderRadius: "4px",
                                  backgroundColor: "rgba(127, 166, 225, 0.08)",
                                  color: "var(--accent)",
                                  border: "1px solid rgba(127, 166, 225, 0.1)"
                                }}
                              >
                                Fetch Live News
                              </button>
                            </div>
                          )}
                        </div>
                      </div>
                    </div>
                  </>
                )}
              </div>
            </div>
          </div>
        );
      })()}

      {/* Floating AI Analysis Popover */}
      {hoveredAnalysisKey && (() => {
        const activeHoverItem = watchlist.find(w => w.instrument_key === hoveredAnalysisKey.key)
          || [...gainers, ...losers].find(m => m.instrument_key === hoveredAnalysisKey.key);
        const activeAnalysis = activeHoverItem?.analysis;

        return (
          <div
            style={{
              position: "fixed",
              top: hoveredAnalysisKey.y,
              left: hoveredAnalysisKey.x,
              width: "380px",
              background: "linear-gradient(135deg, rgba(40, 44, 52, 0.98) 0%, rgba(25, 28, 34, 0.99) 100%)",
              border: "1px solid rgba(255, 255, 255, 0.12)",
              borderRadius: "12px",
              boxShadow: "0 20px 50px rgba(0, 0, 0, 0.9), 0 0 30px rgba(127, 166, 225, 0.1)",
              padding: "16px",
              zIndex: 10000,
              pointerEvents: "none",
              display: "flex",
              flexDirection: "column",
              gap: "12px",
              backdropFilter: "blur(20px)",
              WebkitBackdropFilter: "blur(20px)"
            }}
          >
            {/* Popover Header */}
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", borderBottom: "1px solid rgba(255, 255, 255, 0.08)", paddingBottom: "8px" }}>
              <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <div style={{
                  padding: "3px 8px",
                  borderRadius: "6px",
                  background: "var(--accent)",
                  color: "var(--on-accent)",
                  fontWeight: "800",
                  fontSize: "12px"
                }}>
                  {hoveredAnalysisKey.symbol}
                </div>
                <span style={{ fontSize: "12px", fontWeight: "700", color: "var(--text-primary)" }} className="truncate">
                  {hoveredAnalysisKey.name}
                </span>
              </div>
              {activeAnalysis?.fetched_at && (
                <span style={{ fontSize: "9px", color: "var(--text-muted)" }}>
                  Updated {formatLastUpdated(activeAnalysis.fetched_at)}
                </span>
              )}
            </div>

            {!activeAnalysis ? (
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center", padding: "24px 0", gap: "8px", textAlign: "center" }}>
                <Brain size={24} style={{ color: "var(--text-muted)", opacity: 0.6 }} />
                <p style={{ fontSize: "11px", color: "var(--text-secondary)" }}>
                  No AI analysis has been generated for this stock.
                </p>
                <p style={{ fontSize: "10px", color: "var(--text-muted)" }}>
                  Click the refresh symbol in the actions column to analyze now.
                </p>
              </div>
            ) : (
              <>
                {/* Sector & Recommendation Grid */}
                <div style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 1fr",
                  gap: "10px",
                  padding: "8px",
                  borderRadius: "6px",
                  backgroundColor: "rgba(255,255,255,0.02)",
                  border: "1px solid rgba(255,255,255,0.04)"
                }}>
                  <div>
                    <span style={{ fontSize: "8px", color: "var(--text-muted)", textTransform: "uppercase", display: "block" }}>Sector</span>
                    <span style={{ fontSize: "11px", color: "var(--text-primary)", fontWeight: "600" }}>
                      {activeAnalysis.sector || "General Market"}
                    </span>
                  </div>
                  <div>
                    <span style={{ fontSize: "8px", color: "var(--text-muted)", textTransform: "uppercase", display: "block" }}>Consensus Recommendation</span>
                    {activeAnalysis.recommendation ? (
                      <span style={{
                        padding: "1px 6px",
                        borderRadius: "3px",
                        fontSize: "9px",
                        fontWeight: "800",
                        textTransform: "uppercase",
                        display: "inline-block",
                        marginTop: "1px",
                        backgroundColor: activeAnalysis.recommendation === "BUY"
                          ? "rgba(91, 190, 147, 0.1)"
                          : activeAnalysis.recommendation === "SELL"
                            ? "rgba(226, 141, 131, 0.1)"
                            : "rgba(216, 174, 100, 0.1)",
                        color: activeAnalysis.recommendation === "BUY"
                          ? "var(--success-color)"
                          : activeAnalysis.recommendation === "SELL"
                            ? "var(--danger-color)"
                            : "var(--warning)",
                        border: `1px solid ${activeAnalysis.recommendation === "BUY"
                            ? "rgba(91, 190, 147, 0.18)"
                            : activeAnalysis.recommendation === "SELL"
                              ? "rgba(226, 141, 131, 0.18)"
                              : "rgba(216, 174, 100, 0.18)"
                          }`
                      }}>
                        {activeAnalysis.recommendation}
                      </span>
                    ) : (
                      <span style={{ fontSize: "10px", color: "var(--text-muted)" }}>None</span>
                    )}
                  </div>
                  <div>
                    <span style={{ fontSize: "8px", color: "var(--text-muted)", textTransform: "uppercase", display: "block" }}>Resistance Levels</span>
                    <span style={{ fontSize: "11px", color: "var(--text-primary)", fontWeight: "600" }}>
                      {activeAnalysis.resistance_levels || "Not calculated"}
                    </span>
                  </div>
                  <div>
                    <span style={{ fontSize: "8px", color: "var(--text-muted)", textTransform: "uppercase", display: "block" }}>Support Levels</span>
                    <span style={{ fontSize: "11px", color: "var(--text-primary)", fontWeight: "600" }}>
                      {activeAnalysis.support_levels || "Not calculated"}
                    </span>
                  </div>
                </div>

                {/* AI Commentary */}
                {activeAnalysis.comment ? (
                  <p style={{ fontSize: "11px", color: "var(--text-primary)", lineHeight: "1.5", fontStyle: "italic", fontWeight: "500" }}>
                    "{activeAnalysis.comment}"
                  </p>
                ) : (
                  <p style={{ fontSize: "10px", color: "var(--text-muted)", fontStyle: "italic" }}>
                    No AI commentary generated.
                  </p>
                )}

                {/* Analyst Recommendations Consensus */}
                {activeAnalysis.analyst_recommendations && activeAnalysis.analyst_recommendations.length > 0 && (
                  <div style={{ borderTop: "1px solid rgba(255,255,255,0.06)", paddingTop: "8px" }}>
                    <span style={{ fontSize: "8px", color: "var(--text-muted)", textTransform: "uppercase", display: "block", marginBottom: "6px", fontWeight: "700", letterSpacing: "0.5px" }}>
                      Analyst Recommendations Consensus
                    </span>
                    <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
                      {activeAnalysis.analyst_recommendations.map((rec, idx) => (
                        <div key={idx} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", fontSize: "10px", background: "rgba(255,255,255,0.015)", padding: "4px 8px", borderRadius: "4px", border: "1px solid rgba(255,255,255,0.02)" }}>
                          <span style={{ color: "var(--text-primary)", fontWeight: "500" }}>{rec.analyst_firm}</span>
                          <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                            <span style={{
                              fontWeight: "800",
                              fontSize: "8px",
                              padding: "1px 4px",
                              borderRadius: "3px",
                              backgroundColor: rec.recommendation === "BUY"
                                ? "rgba(91, 190, 147, 0.09)"
                                : rec.recommendation === "SELL"
                                  ? "rgba(226, 141, 131, 0.09)"
                                  : "rgba(216, 174, 100, 0.09)",
                              color: rec.recommendation === "BUY"
                                ? "var(--success-color)"
                                : rec.recommendation === "SELL"
                                  ? "var(--danger-color)"
                                  : "var(--warning)",
                              border: `1px solid ${rec.recommendation === "BUY"
                                  ? "rgba(91, 190, 147, 0.13)"
                                  : rec.recommendation === "SELL"
                                    ? "rgba(226, 141, 131, 0.13)"
                                    : "rgba(216, 174, 100, 0.13)"
                                }`
                            }}>
                              {rec.recommendation}
                            </span>
                            <span style={{ color: "var(--text-muted)", fontSize: "9px" }}>{rec.date}</span>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        );
      })()}

      {/* Floating Sentiment Trend Popover */}
      {hoveredSentimentKey && (() => {
        const activeHoverItem = watchlist.find(w => w.instrument_key === hoveredSentimentKey.key)
          || [...gainers, ...losers].find(m => m.instrument_key === hoveredSentimentKey.key);

        if (!activeHoverItem) return null;

        const high = activeHoverItem.high || 0;
        const low = activeHoverItem.low || 0;
        const range = high - low;
        const priceBuyPct = range > 0 ? ((activeHoverItem.last_price - low) / range) * 100 : 50;
        const depthBuyPct = activeHoverItem.depth_buy_pct !== undefined ? activeHoverItem.depth_buy_pct : 50;
        const compositeBuyPct = Math.round((priceBuyPct * 0.15) + (depthBuyPct * 0.85));
        const compositeSellPct = 100 - compositeBuyPct;

        const history = sentimentHistoryRef.current[hoveredSentimentKey.key] || [];

        const drawSparkline = (hist: number[]) => {
          if (hist.length === 0) {
            return (
              <div style={{ height: "40px", display: "flex", alignItems: "center", justifyContent: "center", fontSize: "10px", color: "var(--text-muted)", fontStyle: "italic" }}>
                Waiting for quote updates to plot trend...
              </div>
            );
          }

          const displayHist = hist.length === 1 ? [hist[0], hist[0]] : hist;
          const width = 328;
          const height = 40;
          const padding = 5;
          const minX = 0;
          const maxX = displayHist.length - 1;
          const minY = 0;
          const maxY = 100;

          const points = displayHist.map((val, index) => {
            const x = padding + (index / maxX) * (width - 2 * padding);
            const y = height - (padding + (val / maxY) * (height - 2 * padding));
            return `${x},${y}`;
          }).join(" ");

          const isUp = displayHist[displayHist.length - 1] >= displayHist[0];
          const strokeColor = isUp ? "var(--positive)" : "var(--negative)";

          return (
            <svg width={width} height={height} style={{ overflow: "visible" }}>
              <line
                x1={0}
                y1={height / 2}
                x2={width}
                y2={height / 2}
                stroke="rgba(255,255,255,0.06)"
                strokeDasharray="3,3"
              />
              <polyline
                fill="none"
                stroke={strokeColor}
                strokeWidth="2"
                points={points}
              />
              {displayHist.length > 0 && (() => {
                const lastIndex = displayHist.length - 1;
                const x = padding + (lastIndex / maxX) * (width - 2 * padding);
                const y = height - (padding + (displayHist[lastIndex] / maxY) * (height - 2 * padding));
                return (
                  <circle cx={x} cy={y} r="3" fill={strokeColor} stroke="var(--bg-base)" strokeWidth="1.5" />
                );
              })()}
            </svg>
          );
        };

        return (
          <div
            style={{
              position: "fixed",
              top: hoveredSentimentKey.y,
              left: hoveredSentimentKey.x,
              width: "360px",
              background: "linear-gradient(135deg, rgba(40, 44, 52, 0.98) 0%, rgba(25, 28, 34, 0.99) 100%)",
              border: "1px solid rgba(255, 255, 255, 0.12)",
              borderRadius: "12px",
              boxShadow: "0 20px 50px rgba(0, 0, 0, 0.9), 0 0 30px rgba(127, 166, 225, 0.1)",
              padding: "16px",
              zIndex: 10000,
              pointerEvents: "none",
              display: "flex",
              flexDirection: "column",
              gap: "10px",
              backdropFilter: "blur(20px)",
              WebkitBackdropFilter: "blur(20px)"
            }}
          >
            {/* Popover Header */}
            <div style={{ display: "flex", alignItems: "center", gap: "8px", borderBottom: "1px solid rgba(255, 255, 255, 0.08)", paddingBottom: "8px" }}>
              <div style={{
                padding: "3px 8px",
                borderRadius: "6px",
                background: "var(--positive)",
                color: "var(--on-accent)",
                fontWeight: "800",
                fontSize: "11px"
              }}>
                {hoveredSentimentKey.symbol}
              </div>
              <span style={{ fontSize: "12px", fontWeight: "700", color: "var(--text-primary)" }} className="truncate">
                Sentiment & Trend Analyzer
              </span>
            </div>

            {/* Main stats */}
            <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                <span style={{ fontSize: "18px", fontWeight: "800", color: "var(--positive)" }}>{compositeBuyPct}% <span style={{ fontSize: "9px", fontWeight: "700", color: "var(--text-muted)", marginLeft: "2px" }}>BUYERS</span></span>
                <span style={{ fontSize: "18px", fontWeight: "800", color: "var(--negative)" }}>{compositeSellPct}% <span style={{ fontSize: "9px", fontWeight: "700", color: "var(--text-muted)", marginRight: "2px" }}>SELLERS</span></span>
              </div>
              <div style={{ width: "100%", height: "6px", backgroundColor: "rgba(226, 141, 131, 0.18)", borderRadius: "3px", overflow: "hidden", display: "flex" }}>
                <div style={{ width: `${compositeBuyPct}%`, height: "100%", backgroundColor: "var(--positive)" }} />
              </div>
            </div>

            {/* Breakdown */}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px", fontSize: "10.5px", margin: "4px 0" }}>
              <div style={{ padding: "8px", borderRadius: "6px", backgroundColor: "rgba(255,255,255,0.02)", border: "1px solid rgba(255,255,255,0.04)" }}>
                <span style={{ display: "block", fontSize: "8px", color: "var(--text-muted)", textTransform: "uppercase", fontWeight: "700" }}>Daily Range (15% Wt)</span>
                <span style={{ display: "block", fontWeight: "700", color: "var(--text-primary)", marginTop: "2px" }}>
                  {Math.round(priceBuyPct)}% B / {100 - Math.round(priceBuyPct)}% S
                </span>
              </div>
              <div style={{ padding: "8px", borderRadius: "6px", backgroundColor: "rgba(255,255,255,0.02)", border: "1px solid rgba(255,255,255,0.04)" }}>
                <span style={{ display: "block", fontSize: "8px", color: "var(--text-muted)", textTransform: "uppercase", fontWeight: "700" }}>Order Book (85% Wt)</span>
                <span style={{ display: "block", fontWeight: "700", color: "var(--text-primary)", marginTop: "2px" }}>
                  {Math.round(depthBuyPct)}% B / {100 - Math.round(depthBuyPct)}% S
                </span>
              </div>
            </div>

            {/* Sparkline trend over time */}
            <div style={{ borderTop: "1px solid rgba(255,255,255,0.06)", paddingTop: "8px" }}>
              <span style={{ display: "block", fontSize: "8px", color: "var(--text-muted)", textTransform: "uppercase", fontWeight: "700", marginBottom: "6px" }}>
                Buyer Sentiment Sparkline (Last 15 Updates)
              </span>
              {drawSparkline(history)}
            </div>
          </div>
        );
      })()}

      {/* Floating Bottom Nav Toggle */}
      <div style={{
        position: "fixed",
        bottom: "20px",
        left: "50%",
        transform: "translateX(-50%)",
        zIndex: 1000,
        display: "flex",
        gap: "6px",
        backgroundColor: "rgba(40, 44, 52, 0.95)",
        backdropFilter: "blur(16px)",
        WebkitBackdropFilter: "blur(16px)",
        padding: "6px",
        borderRadius: "12px",
        border: "1px solid rgba(255, 255, 255, 0.15)",
        boxShadow: "0 12px 40px rgba(0, 0, 0, 0.6)"
      }}>
        <button
          onClick={() => setActiveView("tracker")}
          style={{
            padding: "9px 20px",
            fontSize: "12px",
            fontWeight: "700",
            borderRadius: "8px",
            border: "none",
            cursor: "pointer",
            backgroundColor: activeView === "tracker" ? "var(--accent)" : "transparent",
            color: activeView === "tracker" ? "var(--on-accent)" : "var(--text-secondary)",
            boxShadow: activeView === "tracker" ? "0 2px 14px rgba(127, 166, 225, 0.34)" : "none",
            transition: "all 0.2s"
          }}
        >
          📊 Stocks Tracker
        </button>
        <button
          onClick={() => setActiveView("intelligence")}
          style={{
            padding: "8px 18px",
            fontSize: "12px",
            fontWeight: "700",
            borderRadius: "7px",
            border: "none",
            cursor: "pointer",
            backgroundColor: activeView === "intelligence" ? "var(--accent)" : "transparent",
            color: activeView === "intelligence" ? "var(--on-accent)" : "var(--text-muted)",
            boxShadow: activeView === "intelligence" ? "0 2px 10px rgba(127, 166, 225, 0.26)" : "none",
            transition: "all 0.2s"
          }}
        >
          💡 AI Intelligence
        </button>
        <button
          onClick={() => setActiveView("trading")}
          style={{
            padding: "8px 18px",
            fontSize: "12px",
            fontWeight: "700",
            borderRadius: "7px",
            border: "none",
            cursor: "pointer",
            backgroundColor: activeView === "trading" ? "var(--positive)" : "transparent",
            color: activeView === "trading" ? "var(--on-accent)" : "var(--text-muted)",
            boxShadow: activeView === "trading" ? "0 2px 10px rgba(91, 190, 147, 0.26)" : "none",
            transition: "all 0.2s"
          }}
        >
          ⚡ Auto Trading
        </button>
      </div>
    </div>
  );
}

