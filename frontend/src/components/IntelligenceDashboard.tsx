import { useState, useEffect, useRef, useCallback } from "react";
import {
  Newspaper,
  Bell,
  TrendingUp,
  TrendingDown,
  FileText,
  Layers,
  Search,
  ExternalLink,
  RefreshCw,
  Info,
  Clock,
  Bot
} from "lucide-react";

function parseUtcDate(isoString: string | null | undefined): Date | null {
  if (!isoString) return null;
  let str = isoString.trim();
  if (!str) return null;

  // Replace space between date and time with T (SQL string "YYYY-MM-DD HH:MM:SS")
  if (/^\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}/.test(str)) {
    str = str.replace(" ", "T");
  }

  // Append Z if ISO string lacks timezone specifier
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(str) && !str.endsWith("Z") && !str.includes("+") && !/-\d{2}:\d{2}$/.test(str)) {
    str += "Z";
  }

  const d = new Date(str);
  return isNaN(d.getTime()) ? null : d;
}

// Inline Twitter Icon SVG to bypass package dependency version issue
function TwitterIcon({ size = 15, className = "" }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      stroke="currentColor"
      strokeWidth="2"
      fill="none"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle" }}
    >
      <path d="M22 4s-.7 2.1-2 3.4c1.6 10-9.4 17.3-18 11.6 2.2.1 4.4-.6 6-2C3 15.5.5 9.6 3 5c2.2 2.6 5.6 4.1 9 4-.9-4.2 4-6.6 7-3.8 1.1 0 3-1.2 3-1.2z" />
    </svg>
  );
}

const API_BASE = import.meta.env.VITE_API_BASE || (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1" ? "http://localhost:8000" : "");

interface FeedItem {
  id: string;
  type: "event" | "news_story" | "filing";
  event_type: string;
  source: string;
  symbol: string | null;
  title: string;
  description: string;
  url: string | null;
  time: string;
  ai_sentiment: "positive" | "negative" | "neutral" | null;
  ai_impact_score: number | null;
  ai_summary: string | null;
  ai_provider?: string;
  ai_affected_stocks?: string[];
  category?: string;
  article_count?: number;
  best_source_tier?: number;
  symbols?: string;
  articles?: Array<{
    source: string;
    headline: string;
    url: string;
    published_at: string;
    source_tier: number;
  }>;
  period?: string;
  ai_key_metrics?: Record<string, any> | null;
}

interface AlertItem {
  id: number;
  alert_type: string;
  severity: "critical" | "high" | "medium" | "low";
  symbol: string | null;
  title: string;
  description: string;
  is_read: boolean;
  created_at: string;
}

interface SuggestionItem {
  symbol: string;
  direction: "positive" | "negative";
  impact_score: number;
  reason: string;
  source_type: string;
  event_type?: string;
  source?: string;
  time: string | null;
}

interface Stats24h {
  events_24h: number;
  news_articles_24h: number;
  news_stories_24h: number;
  filings_24h: number;
  unread_alerts: number;
  critical_alerts: number;
  sentiment_distribution: {
    positive: number;
    negative: number;
    neutral: number;
  };
  event_types: Record<string, number>;
}

export function IntelligenceDashboard() {
  // Feed state
  const [feedItems, setFeedItems] = useState<FeedItem[]>([]);
  const [loadingFeed, setLoadingFeed] = useState(true);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);

  // Filter state
  const [filterCategory, setFilterCategory] = useState<string>("all");
  const [filterSentiment, setFilterSentiment] = useState<string>("all");
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [debouncedSearch, setDebouncedSearch] = useState<string>("");
  const [timeWindow, setTimeWindow] = useState<number>(24); // hours

  // Alerts, suggestions, stats
  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [suggestions, setSuggestions] = useState<SuggestionItem[]>([]);
  const [stats, setStats] = useState<Stats24h | null>(null);
  const [unreadAlertsCount, setUnreadAlertsCount] = useState(0);

  // Market sentiment state
  interface MarketSentimentData {
    sentiment: string;
    score: number;
    summary: string;
    drivers: Array<{
      title: string;
      impact: string;
      source: string;
      time: string;
    }>;
    sectors: {
      positive: string[];
      negative: string[];
    };
    last_updated: string;
  }
  const [marketSentiment, setMarketSentiment] = useState<MarketSentimentData | null>(null);
  const [loadingSentiment, setLoadingSentiment] = useState(true);
  const [refreshingSentiment, setRefreshingSentiment] = useState(false);

  // Sidebar states
  const [sidebarTab, setSidebarTab] = useState<string>("ai");
  const [activeStocks, setActiveStocks] = useState<any[]>([]);
  const [upcomingEarnings, setUpcomingEarnings] = useState<any[]>([]);
  const [sidebarNews, setSidebarNews] = useState<any[]>([]);
  const [hoveredStock, setHoveredStock] = useState<any | null>(null);
  const [readStocks, setReadStocks] = useState<Record<string, string>>(() => {
    try {
      const saved = localStorage.getItem("read_stocks");
      return saved ? JSON.parse(saved) : {};
    } catch {
      return {};
    }
  });

  // Selected item modal
  const [selectedItem, setSelectedItem] = useState<FeedItem | null>(null);

  // Refresh trigger
  const [refreshTrigger, setRefreshTrigger] = useState(0);

  // SSE real-time stream state
  const [sseStatus, setSseStatus] = useState<"connecting" | "connected" | "disconnected">("connecting");
  const [sseClients, setSseClients] = useState(0);
  const [lastStreamEvent, setLastStreamEvent] = useState<string | null>(null);
  const [streamEventCount, setStreamEventCount] = useState(0);
  const eventSourceRef = useRef<EventSource | null>(null);

  // Search debounce effect
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedSearch(searchQuery.trim());
    }, 500);
    return () => clearTimeout(timer);
  }, [searchQuery]);

  // Handle marking active stock as read
  const handleMarkStockRead = (symbol: string, timestamp: string) => {
    const updated = { ...readStocks, [symbol]: timestamp };
    setReadStocks(updated);
    localStorage.setItem("read_stocks", JSON.stringify(updated));
  };

  // Handle marking all active stocks as read
  const handleMarkAllStocksRead = () => {
    const updated = { ...readStocks };
    activeStocks.forEach((stk) => {
      updated[stk.symbol] = stk.time;
    });
    setReadStocks(updated);
    localStorage.setItem("read_stocks", JSON.stringify(updated));
  };

  // Request browser notifications permission
  useEffect(() => {
    if ("Notification" in window && Notification.permission === "default") {
      Notification.requestPermission();
    }
  }, []);

  // Fetch stats, alerts, suggestions, active stocks, upcoming earnings, and sidebar news
  useEffect(() => {
    const fetchAuxiliaryData = async () => {
      try {
        // Fetch Stats
        const statsRes = await fetch(`${API_BASE}/api/intelligence/stats`);
        if (statsRes.ok) {
          const statsData = await statsRes.json();
          setStats(statsData);
          setUnreadAlertsCount(statsData.unread_alerts);
        }

        // Fetch Alerts (increased limit to 40)
        const alertsRes = await fetch(`${API_BASE}/api/intelligence/alerts?unread_only=false&limit=40`);
        if (alertsRes.ok) {
          const alertsData = await alertsRes.json();
          setAlerts(alertsData.alerts);
        }

        // Fetch Suggestions (increased limit to 40)
        const suggRes = await fetch(`${API_BASE}/api/intelligence/suggestions?limit=40`);
        if (suggRes.ok) {
          const suggData = await suggRes.json();
          setSuggestions(suggData.suggestions);
        }

        // Fetch Active Stocks
        const activeRes = await fetch(`${API_BASE}/api/intelligence/active-stocks`);
        if (activeRes.ok) {
          const activeData = await activeRes.json();
          setActiveStocks(activeData);
        }

        // Fetch Upcoming Earnings
        const earningsRes = await fetch(`${API_BASE}/api/intelligence/upcoming-earnings`);
        if (earningsRes.ok) {
          const earningsData = await earningsRes.json();
          setUpcomingEarnings(earningsData);
        }

        // Fetch Sidebar Global/Stock News
        const newsRes = await fetch(`${API_BASE}/api/intelligence/feed?page=1&page_size=40&hours=48&event_type=news`);
        if (newsRes.ok) {
          const newsData = await newsRes.json();
          setSidebarNews(newsData.items || []);
        }
      } catch (err) {
        console.error("Error fetching auxiliary intelligence data:", err);
      }
    };

    fetchAuxiliaryData();
    // Poll auxiliary data every 30 seconds
    const interval = setInterval(fetchAuxiliaryData, 30000);
    return () => clearInterval(interval);
  }, [refreshTrigger]);

  // Fetch market sentiment
  const fetchMarketSentiment = async (force = false) => {
    if (force) setRefreshingSentiment(true);
    else setLoadingSentiment(true);
    try {
      const res = await fetch(`${API_BASE}/api/intelligence/market-sentiment${force ? "?force_refresh=true" : ""}`);
      if (res.ok) {
        const data = await res.json();
        setMarketSentiment(data);
      }
    } catch (err) {
      console.error("Error fetching market sentiment:", err);
    } finally {
      setLoadingSentiment(false);
      setRefreshingSentiment(false);
    }
  };

  useEffect(() => {
    fetchMarketSentiment(false);
    const sentimentInterval = setInterval(() => {
      fetchMarketSentiment(false);
    }, 300000); // 5 minutes
    return () => clearInterval(sentimentInterval);
  }, [refreshTrigger]);

  // Fetch main feed items
  useEffect(() => {
    const fetchFeed = async (isBackground = false) => {
      if (!isBackground) {
        setLoadingFeed(true);
      }
      try {
        let url = `${API_BASE}/api/intelligence/feed?page=${page}&page_size=30&hours=${timeWindow}`;
        if (filterCategory !== "all") {
          url += `&category=${filterCategory}`;
        }
        if (filterSentiment !== "all") {
          url += `&sentiment=${filterSentiment}`;
        }
        if (debouncedSearch) {
          url += `&search=${encodeURIComponent(debouncedSearch)}`;
        }

        const res = await fetch(url);
        if (res.ok) {
          const data = await res.json();
          setFeedItems(data.items);
          setTotalPages(data.total_pages);
        }
      } catch (err) {
        console.error("Error fetching intelligence feed:", err);
      } finally {
        if (!isBackground) {
          setLoadingFeed(false);
        }
      }
    };

    fetchFeed(false);

    // Fallback poll every 2 minutes (SSE handles real-time updates)
    const interval = setInterval(() => {
      fetchFeed(true);
    }, 120000);

    return () => clearInterval(interval);
  }, [page, filterCategory, filterSentiment, debouncedSearch, timeWindow, refreshTrigger]);

  // ─── SSE Real-Time Stream ─────────────────────────────────────────
  useEffect(() => {
    let es: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      setSseStatus("connecting");
      es = new EventSource(`${API_BASE}/api/intelligence/stream`);
      eventSourceRef.current = es;

      es.onopen = () => {
        setSseStatus("connected");
      };

      es.addEventListener("connected", (e: MessageEvent) => {
        setSseStatus("connected");
        try {
          const data = JSON.parse(e.data);
          setSseClients(data.clients || 1);
        } catch { /* ignore */ }
      });

      es.addEventListener("new_event", (e: MessageEvent) => {
        try {
          const data = JSON.parse(e.data);
          const itemDate = parseUtcDate(data.time);
          if (itemDate && (Date.now() - itemDate.getTime()) > timeWindow * 3600 * 1000) {
            return;
          }
          const newItem: FeedItem = {
            id: data.id || `sse_event_${Date.now()}`,
            type: "event",
            event_type: data.event_type || "announcement",
            source: data.source || "nse",
            symbol: data.symbol || null,
            title: data.title || "",
            description: data.description || "",
            url: data.url || null,
            time: data.time || new Date().toISOString(),
            ai_sentiment: data.ai_sentiment || null,
            ai_impact_score: data.ai_impact_score || null,
            ai_summary: data.ai_summary || null,
            category: data.category || "general",
          };
          // Prepend or update item in-place when AI analysis arrives
          setFeedItems(prev => {
            const idx = prev.findIndex(item => item.id === newItem.id);
            if (idx !== -1) {
              const updated = [...prev];
              updated[idx] = { ...updated[idx], ...newItem };
              return updated;
            }
            return [newItem, ...prev].slice(0, 100);
          });
          setStreamEventCount(prev => prev + 1);
          setLastStreamEvent(new Date().toISOString());
        } catch { /* ignore parse errors */ }
      });

      es.addEventListener("new_news", (e: MessageEvent) => {
        try {
          const data = JSON.parse(e.data);
          const itemDate = parseUtcDate(data.time);
          if (itemDate && (Date.now() - itemDate.getTime()) > timeWindow * 3600 * 1000) {
            return;
          }
          const newItem: FeedItem = {
            id: `sse_news_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
            type: "news_story",
            event_type: "news",
            source: data.source || "multi",
            symbol: data.symbol || null,
            title: data.title || "",
            description: data.description || "",
            url: data.url || null,
            time: data.time || new Date().toISOString(),
            ai_sentiment: null,
            ai_impact_score: null,
            ai_summary: null,
            category: "news",
          };
          setFeedItems(prev => [newItem, ...prev].slice(0, 100));
          setStreamEventCount(prev => prev + 1);
          setLastStreamEvent(new Date().toISOString());
        } catch { /* ignore */ }
      });

      es.addEventListener("new_alert", (e: MessageEvent) => {
        try {
          const data = JSON.parse(e.data);
          // Update unread alerts count
          setUnreadAlertsCount(prev => prev + 1);
          // Add alert to the list
          const newAlert: AlertItem = {
            id: data.id || Date.now(),
            alert_type: data.alert_type || "high_impact",
            severity: data.severity || "medium",
            symbol: data.symbol || null,
            title: data.title || "",
            description: data.description || "",
            is_read: false,
            created_at: data.created_at || new Date().toISOString(),
          };
          setAlerts(prev => [newAlert, ...prev]);
          // Browser notification for critical/high alerts
          if (
            (data.severity === "critical" || data.severity === "high") &&
            "Notification" in window &&
            Notification.permission === "granted"
          ) {
            new Notification(`${data.severity.toUpperCase()} Alert: ${data.symbol || "Market"}`, {
              body: data.title,
            });
          }
        } catch { /* ignore */ }
      });

      es.addEventListener("heartbeat", (e: MessageEvent) => {
        try {
          const data = JSON.parse(e.data);
          setSseClients(data.clients || 0);
          setLastStreamEvent(new Date().toISOString());
        } catch { /* ignore */ }
      });

      es.onerror = () => {
        setSseStatus("disconnected");
        es?.close();
        eventSourceRef.current = null;
        // Auto-reconnect after 5 seconds
        reconnectTimer = setTimeout(connect, 5000);
      };
    };

    connect();

    return () => {
      if (reconnectTimer) clearTimeout(reconnectTimer);
      es?.close();
      eventSourceRef.current = null;
      setSseStatus("disconnected");
    };
  }, []); // Only connect once on mount

  // Check for critical alerts to trigger browser notifications
  useEffect(() => {
    const criticalAlerts = alerts.filter(a => !a.is_read && a.severity === "critical");
    if (criticalAlerts.length > 0 && "Notification" in window && Notification.permission === "granted") {
      criticalAlerts.forEach(alert => {
        new Notification(`CRITICAL Market Alert: ${alert.symbol || "Market"}`, {
          body: alert.title,
          icon: "/logo192.png" // placeholder or path to your icon
        });
      });
    }
  }, [alerts]);

  const handleMarkAlertRead = async (id: number) => {
    try {
      const res = await fetch(`${API_BASE}/api/intelligence/alerts/${id}/read`, {
        method: "POST"
      });
      if (res.ok) {
        setAlerts(prev => prev.map(a => a.id === id ? { ...a, is_read: true } : a));
        setUnreadAlertsCount(prev => Math.max(0, prev - 1));
      }
    } catch (err) {
      console.error("Error marking alert as read:", err);
    }
  };

  const handleMarkAllAlertsRead = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/intelligence/alerts/read-all`, {
        method: "POST"
      });
      if (res.ok) {
        setAlerts(prev => prev.map(a => ({ ...a, is_read: true })));
        setUnreadAlertsCount(0);
      }
    } catch (err) {
      console.error("Error marking all alerts read:", err);
    }
  };

  const [reanalyzingIds, setReanalyzingIds] = useState<Record<string, boolean>>({});

  const handleReanalyzeItem = async (e: React.MouseEvent, item: FeedItem) => {
    e.stopPropagation();
    const key = `${item.type}-${item.id}`;
    if (reanalyzingIds[key]) return;

    setReanalyzingIds(prev => ({ ...prev, [key]: true }));

    try {
      const res = await fetch(`${API_BASE}/api/intelligence/reanalyze/${item.type}/${item.id}`, {
        method: "POST"
      });
      if (res.ok) {
        const updated = await res.json();
        setFeedItems(prev => prev.map(f => {
          if (f.id === item.id && f.type === item.type) {
            return {
              ...f,
              ai_sentiment: updated.sentiment || f.ai_sentiment,
              ai_impact_score: updated.impact_score !== undefined ? updated.impact_score : f.ai_impact_score,
              ai_summary: updated.summary || f.ai_summary,
              ai_affected_stocks: updated.affected_stocks || f.ai_affected_stocks
            };
          }
          return f;
        }));
      }
    } catch (err) {
      console.error("Error re-analyzing item:", err);
    } finally {
      setReanalyzingIds(prev => ({ ...prev, [key]: false }));
    }
  };

  const [pollingNow, setPollingNow] = useState(false);

  const forceTriggerReload = async () => {
    setPollingNow(true);
    try {
      await fetch(`${API_BASE}/api/intelligence/poll`, { method: "POST" });
    } catch (err) {
      console.error("Error triggering manual poll:", err);
    } finally {
      setTimeout(() => {
        setPollingNow(false);
        setRefreshTrigger(prev => prev + 1);
      }, 1500);
    }
  };

  // UI Helper functions
  const getSentimentStyles = (sentiment: string | null) => {
    switch (sentiment) {
      case "positive":
        return {
          bg: "rgba(16, 185, 129, 0.1)",
          text: "#10b981",
          border: "rgba(16, 185, 129, 0.2)",
          badgeText: "Positive Impact"
        };
      case "negative":
        return {
          bg: "rgba(239, 68, 68, 0.1)",
          text: "#ef4444",
          border: "rgba(239, 68, 68, 0.2)",
          badgeText: "Negative Impact"
        };
      case "neutral":
      default:
        return {
          bg: "rgba(107, 114, 128, 0.1)",
          text: "#9ca3af",
          border: "rgba(107, 114, 128, 0.2)",
          badgeText: "Neutral / Minimal"
        };
    }
  };

  const getSourceIcon = (source: string, eventType: string) => {
    const s = source.toLowerCase();
    const et = eventType.toLowerCase();
    if (s.includes("twitter") || s.includes("x")) {
      return <TwitterIcon size={15} className="text-blue-400" />;
    }
    if (et.includes("filing") || et.includes("quarterly_result") || et.includes("transcript")) {
      return <FileText size={15} className="text-amber-400" />;
    }
    if (s.includes("nse") || s.includes("bse")) {
      return <Layers size={15} className="text-indigo-400" />;
    }
    return <Newspaper size={15} className="text-sky-400" />;
  };

  const getSeverityStyle = (severity: string) => {
    switch (severity) {
      case "critical":
        return { bg: "#ef4444", text: "#ffffff", label: "Critical" };
      case "high":
        return { bg: "#f97316", text: "#ffffff", label: "High" };
      case "medium":
        return { bg: "#eab308", text: "#000000", label: "Medium" };
      case "low":
      default:
        return { bg: "#3b82f6", text: "#ffffff", label: "Low" };
    }
  };

  const formatTime = (isoString: string) => {
    const date = parseUtcDate(isoString);
    if (!date) return "";

    const now = new Date();
    const diffMs = now.getTime() - date.getTime();

    if (diffMs < 0 && Math.abs(diffMs) < 60000) return "Just now";

    const diffSecs = Math.floor(diffMs / 1000);
    const diffMins = Math.floor(diffSecs / 60);
    const diffHours = Math.floor(diffMins / 60);
    const diffDays = Math.floor(diffHours / 24);

    if (diffSecs < 30) return "Just now";
    if (diffSecs < 60) return `${diffSecs}s ago`;
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ${diffMins % 60}m ago`;
    if (diffDays < 7) return `${diffDays}d ago`;

    return "";
  };

  const formatSourceName = (source: string) => {
    if (!source) return "";
    const s = source.toLowerCase().trim();
    if (s === "nse") return "NSE";
    if (s === "bse") return "BSE";
    if (s === "moneycontrol") return "Moneycontrol";
    if (s === "moneycontrol_recos") return "MC Recommendations";
    if (s === "analyst_ratings") return "Analyst Ratings";
    if (s === "broker_ratings") return "Broker Ratings";
    if (s === "economic_times" || s === "economictimes") return "Economic Times";
    if (s === "business_standard") return "Business Standard";
    if (s === "livemint" || s === "mint") return "Mint";
    if (s === "ndtv_profit" || s === "ndtv") return "NDTV Profit";
    if (s === "reuters") return "Reuters";
    if (s === "bloomberg") return "Bloomberg";
    if (s === "google_news_market" || s === "google_news") return "Google News";
    if (s === "times_now" || s === "timesnow") return "Times Now";
    if (s === "filing") return "Filing";
    return source
      .replace(/[_-]/g, " ")
      .split(" ")
      .map(w => w.charAt(0).toUpperCase() + w.slice(1))
      .join(" ");
  };

  const getDisplaySource = (item: FeedItem) => {
    if (item.type === "news_story") {
      if (item.articles && item.articles.length > 0) {
        const uniqueSources = Array.from(new Set(item.articles.map(a => formatSourceName(a.source))));
        if (uniqueSources.length === 1) {
          return uniqueSources[0];
        } else if (uniqueSources.length === 2) {
          return `${uniqueSources[0]} & ${uniqueSources[1]}`;
        } else if (uniqueSources.length > 2) {
          return `${uniqueSources[0]} + ${uniqueSources.length - 1} outlets`;
        }
      }
      return "Market News";
    }
    return formatSourceName(item.source);
  };

  const formatFullTimestamp = (isoString: string) => {
    const date = parseUtcDate(isoString);
    if (!date) return "—";
    return date.toLocaleString("en-IN", {
      day: "numeric",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: true
    });
  };

  const getCategoryLabel = (category: string | undefined, eventType: string) => {
    if (!category) {
      if (eventType === "board_meeting") return { label: "Board Meeting", icon: "🏛️", color: "#e9d5ff", bg: "rgba(168, 85, 247, 0.15)" };
      if (eventType === "announcement") return { label: "Announcement", icon: "📢", color: "#93c5fd", bg: "rgba(59, 130, 246, 0.15)" };
      return { label: eventType.replace("_", " "), icon: "📝", color: "#94a3b8", bg: "rgba(148, 163, 184, 0.15)" };
    }
    const cat = category.toLowerCase();
    switch (cat) {
      case "board_meeting":
        return { label: "Board Meeting", icon: "🏛️", color: "#e9d5ff", bg: "rgba(168, 85, 247, 0.15)" };
      case "earnings":
        return { label: "Earnings/Results", icon: "📊", color: "#a7f3d0", bg: "rgba(16, 185, 129, 0.15)" };
      case "corporate_action":
        return { label: "Corporate Action", icon: "⚡", color: "#fef08a", bg: "rgba(234, 179, 8, 0.15)" };
      case "sebi_filing":
        return { label: "SEBI Filing", icon: "⚖️", color: "#93c5fd", bg: "rgba(59, 130, 246, 0.15)" };
      case "insider_trade":
        return { label: "Insider Trade", icon: "👤", color: "#fed7aa", bg: "rgba(249, 115, 22, 0.15)" };
      case "bulk_deal":
        return { label: "Bulk/Block Deal", icon: "💼", color: "#fbcfe8", bg: "rgba(236, 72, 153, 0.15)" };
      case "credit_rating":
        return { label: "Credit Rating", icon: "⭐️", color: "#ddd6fe", bg: "rgba(139, 92, 246, 0.15)" };
      case "filing":
        return { label: "Quarterly Filing", icon: "📁", color: "#cbd5e1", bg: "rgba(100, 116, 139, 0.15)" };
      case "news":
      case "market_update":
        return { label: "Market News", icon: "📰", color: "#67e8f9", bg: "rgba(6, 182, 212, 0.15)" };
      default:
        return { label: category.replace("_", " "), icon: "📝", color: "#94a3b8", bg: "rgba(148, 163, 184, 0.15)" };
    }
  };

  return (
    <div className="intelligence-grid" style={{
      display: "grid",
      gridTemplateColumns: "1fr 340px",
      gap: "20px",
      padding: "20px",
      minHeight: "calc(100vh - 80px)",
      backgroundColor: "#090d16",
      color: "#e2e8f0",
      fontFamily: "system-ui, sans-serif"
    }}>
      {/* LEFT COLUMN: Filters, Feed Stream, Modal */}
      <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>

        {/* Header Stats Strip */}
        {stats && (
          <div style={{
            display: "grid",
            gridTemplateColumns: "repeat(5, 1fr)",
            gap: "12px"
          }}>
            {[
              { label: "Events Ingested", value: stats.events_24h, icon: <Layers size={18} />, color: "#3b82f6" },
              { label: "News Stories", value: stats.news_stories_24h, icon: <Newspaper size={18} />, color: "#06b6d4" },
              { label: "Results & Filings", value: stats.filings_24h, icon: <FileText size={18} />, color: "#f59e0b" },
              { label: "Positive Impact Events", value: stats.sentiment_distribution.positive, icon: <TrendingUp size={18} />, color: "#10b981" },
              { label: "Negative Impact Events", value: stats.sentiment_distribution.negative, icon: <TrendingDown size={18} />, color: "#ef4444" }
            ].map((stat, i) => (
              <div key={i} style={{
                background: "rgba(17, 24, 39, 0.6)",
                border: "1px solid rgba(255, 255, 255, 0.05)",
                backdropFilter: "blur(10px)",
                padding: "12px 16px",
                borderRadius: "12px",
                display: "flex",
                alignItems: "center",
                gap: "14px"
              }}>
                <div style={{
                  padding: "8px",
                  borderRadius: "8px",
                  backgroundColor: `${stat.color}15`,
                  color: stat.color,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center"
                }}>
                  {stat.icon}
                </div>
                <div>
                  <div style={{ fontSize: "11px", color: "#94a3b8", fontWeight: "500" }}>{stat.label}</div>
                  <div style={{ fontSize: "18px", fontWeight: "700", color: "#f8fafc", marginTop: "2px" }}>{stat.value}</div>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Toolbar & Filters */}
        <div style={{
          background: "rgba(17, 24, 39, 0.4)",
          border: "1px solid rgba(255, 255, 255, 0.05)",
          padding: "16px",
          borderRadius: "12px",
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "16px"
        }}>
          {/* Filters section */}
          <div style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" }}>
            <div style={{ position: "relative", display: "flex", alignItems: "center" }}>
              <Search size={15} style={{ position: "absolute", left: "10px", color: "#64748b" }} />
              <input
                type="text"
                placeholder="Search symbol or words..."
                value={searchQuery}
                onChange={(e) => {
                  setSearchQuery(e.target.value);
                  setPage(1);
                }}
                style={{
                  backgroundColor: "#0d131f",
                  border: "1px solid rgba(255, 255, 255, 0.08)",
                  borderRadius: "8px",
                  padding: "8px 12px 8px 32px",
                  fontSize: "12px",
                  color: "#f8fafc",
                  width: "180px",
                  outline: "none"
                }}
              />
            </div>

            <select
              value={filterCategory}
              onChange={(e) => {
                setFilterCategory(e.target.value);
                setPage(1);
              }}
              style={{
                backgroundColor: "#0d131f",
                border: "1px solid rgba(255, 255, 255, 0.08)",
                borderRadius: "8px",
                padding: "8px 12px",
                fontSize: "12px",
                color: "#f8fafc",
                outline: "none",
                cursor: "pointer"
              }}
            >
              <option value="all">All Categories</option>
              <option value="board_meeting">Board Meetings</option>
              <option value="sebi_filing">SEBI / Exchange Filings</option>
              <option value="earnings">Earnings & Results</option>
              <option value="corporate_action">Corporate Actions</option>
              <option value="insider_trade">Insider Trades</option>
              <option value="bulk_deal">Bulk/Block Deals</option>
              <option value="news">Market News</option>
              <option value="filing">Quarterly Filings</option>
            </select>

            <select
              value={filterSentiment}
              onChange={(e) => {
                setFilterSentiment(e.target.value);
                setPage(1);
              }}
              style={{
                backgroundColor: "#0d131f",
                border: "1px solid rgba(255, 255, 255, 0.08)",
                borderRadius: "8px",
                padding: "8px 12px",
                fontSize: "12px",
                color: "#f8fafc",
                outline: "none",
                cursor: "pointer"
              }}
            >
              <option value="all">All Sentiments</option>
              <option value="positive">Positive Impact</option>
              <option value="negative">Negative Impact</option>
              <option value="neutral">Neutral Impact</option>
            </select>

            <select
              value={timeWindow}
              onChange={(e) => {
                setTimeWindow(Number(e.target.value));
                setPage(1);
              }}
              style={{
                backgroundColor: "#0d131f",
                border: "1px solid rgba(255, 255, 255, 0.08)",
                borderRadius: "8px",
                padding: "8px 12px",
                fontSize: "12px",
                color: "#f8fafc",
                outline: "none",
                cursor: "pointer"
              }}
            >
              <option value={6}>Last 6 Hours</option>
              <option value={12}>Last 12 Hours</option>
              <option value={24}>Last 24 Hours</option>
              <option value={48}>Last 48 Hours</option>
              <option value={168}>Last 7 Days</option>
            </select>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            {/* Live SSE Indicator */}
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: "6px",
                padding: "6px 12px",
                borderRadius: "8px",
                fontSize: "11px",
                fontWeight: "600",
                background:
                  sseStatus === "connected"
                    ? "rgba(16, 185, 129, 0.1)"
                    : sseStatus === "connecting"
                      ? "rgba(234, 179, 8, 0.1)"
                      : "rgba(239, 68, 68, 0.1)",
                border: `1px solid ${sseStatus === "connected"
                  ? "rgba(16, 185, 129, 0.25)"
                  : sseStatus === "connecting"
                    ? "rgba(234, 179, 8, 0.25)"
                    : "rgba(239, 68, 68, 0.25)"
                  }`,
                color:
                  sseStatus === "connected"
                    ? "#10b981"
                    : sseStatus === "connecting"
                      ? "#eab308"
                      : "#ef4444",
              }}
              title={
                sseStatus === "connected"
                  ? `Live stream connected • ${sseClients} client(s) • ${streamEventCount} events received`
                  : sseStatus === "connecting"
                    ? "Connecting to live stream..."
                    : "Disconnected — reconnecting..."
              }
            >
              <span
                style={{
                  width: "8px",
                  height: "8px",
                  borderRadius: "50%",
                  backgroundColor:
                    sseStatus === "connected"
                      ? "#10b981"
                      : sseStatus === "connecting"
                        ? "#eab308"
                        : "#ef4444",
                  display: "inline-block",
                  animation:
                    sseStatus === "connected"
                      ? "sse-pulse 2s ease-in-out infinite"
                      : sseStatus === "connecting"
                        ? "sse-pulse 1s ease-in-out infinite"
                        : "none",
                  boxShadow:
                    sseStatus === "connected"
                      ? "0 0 6px rgba(16, 185, 129, 0.6)"
                      : "none",
                }}
              />
              {sseStatus === "connected"
                ? "LIVE"
                : sseStatus === "connecting"
                  ? "CONNECTING"
                  : "OFFLINE"}
            </div>

            <button
              onClick={forceTriggerReload}
              style={{
                display: "flex",
                alignItems: "center",
                gap: "6px",
                backgroundColor: "rgba(59, 130, 246, 0.1)",
                border: "1px solid rgba(59, 130, 246, 0.2)",
                color: "#60a5fa",
                borderRadius: "8px",
                padding: "8px 14px",
                fontSize: "12px",
                fontWeight: "600",
                cursor: "pointer",
                transition: "all 0.2s"
              }}
              onMouseOver={(e) => {
                e.currentTarget.style.backgroundColor = "rgba(59, 130, 246, 0.2)";
              }}
              onMouseOut={(e) => {
                e.currentTarget.style.backgroundColor = "rgba(59, 130, 246, 0.1)";
              }}
              disabled={pollingNow}
            >
              <RefreshCw size={14} className={pollingNow ? "animate-spin" : ""} />
              {pollingNow ? "Polling Web & Exchanges..." : "Poll Now"}
            </button>
          </div>
        </div>

        {/* Live Stream Feed */}
        <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
          {loadingFeed ? (
            <div style={{
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              padding: "80px 20px",
              background: "rgba(17, 24, 39, 0.2)",
              borderRadius: "16px",
              border: "1px solid rgba(255, 255, 255, 0.03)"
            }}>
              <RefreshCw className="animate-spin text-blue-500" size={32} />
              <div style={{ marginTop: "16px", fontSize: "14px", color: "#64748b" }}>Analyzing real-time intelligence feed...</div>
            </div>
          ) : feedItems.length === 0 ? (
            <div style={{
              textAlign: "center",
              padding: "60px 20px",
              background: "rgba(17, 24, 39, 0.2)",
              borderRadius: "16px",
              border: "1px solid rgba(255, 255, 255, 0.03)",
              color: "#64748b"
            }}>
              <Info size={32} style={{ margin: "0 auto 12px auto", color: "#475569" }} />
              <div style={{ fontSize: "15px", fontWeight: "600" }}>No intelligence items found</div>
              <div style={{ fontSize: "12px", marginTop: "4px" }}>Try broadening your search window or active filters.</div>
            </div>
          ) : (
            feedItems.map((item) => {
              const sentiment = getSentimentStyles(item.ai_sentiment);
              return (
                <div
                  key={item.id}
                  onClick={() => setSelectedItem(item)}
                  style={{
                    background: "rgba(17, 24, 39, 0.4)",
                    borderLeft: `4px solid ${sentiment.text}`,
                    borderTop: "1px solid rgba(255, 255, 255, 0.04)",
                    borderRight: "1px solid rgba(255, 255, 255, 0.04)",
                    borderBottom: "1px solid rgba(255, 255, 255, 0.04)",
                    borderRadius: "0 12px 12px 0",
                    padding: "16px",
                    cursor: "pointer",
                    display: "flex",
                    flexDirection: "column",
                    gap: "10px",
                    transition: "all 0.2s"
                  }}
                  className="hover-card"
                  onMouseOver={(e) => {
                    e.currentTarget.style.backgroundColor = "rgba(17, 24, 39, 0.6)";
                  }}
                  onMouseOut={(e) => {
                    e.currentTarget.style.backgroundColor = "rgba(17, 24, 39, 0.4)";
                  }}
                >
                  <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: "10px" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
                      <span style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "5px",
                        backgroundColor: "rgba(255, 255, 255, 0.04)",
                        padding: "3px 8px",
                        borderRadius: "6px",
                        fontSize: "10px",
                        fontWeight: "600"
                      }}>
                        {getSourceIcon(item.type === "news_story" && item.articles && item.articles.length > 0 ? item.articles[0].source : item.source, item.event_type)}
                        {getDisplaySource(item)}
                      </span>
                      {(() => {
                        const cat = getCategoryLabel(item.category, item.event_type);
                        return (
                          <span style={{
                            display: "flex",
                            alignItems: "center",
                            gap: "4px",
                            backgroundColor: cat.bg,
                            color: cat.color,
                            padding: "3px 8px",
                            borderRadius: "6px",
                            fontSize: "10px",
                            fontWeight: "600",
                            textTransform: "uppercase"
                          }}>
                            <span style={{ fontSize: "11px" }}>{cat.icon}</span>
                            <span>{cat.label}</span>
                          </span>
                        );
                      })()}
                      {item.symbol && (
                        <span style={{
                          backgroundColor: "rgba(59, 130, 246, 0.15)",
                          color: "#93c5fd",
                          padding: "3px 8px",
                          borderRadius: "6px",
                          fontSize: "11px",
                          fontWeight: "700"
                        }}>
                          {item.symbol}
                        </span>
                      )}
                      {item.ai_provider && (
                        <span style={{
                          display: "flex",
                          alignItems: "center",
                          gap: "4px",
                          backgroundColor: item.ai_provider === "groq" ? "rgba(168, 85, 247, 0.15)"
                            : item.ai_provider === "ollama" ? "rgba(245, 158, 11, 0.15)"
                            : item.ai_provider === "gemini" ? "rgba(16, 185, 129, 0.15)"
                            : "rgba(100, 116, 139, 0.15)",
                          color: item.ai_provider === "groq" ? "#c084fc"
                            : item.ai_provider === "ollama" ? "#fbbf24"
                            : item.ai_provider === "gemini" ? "#34d399"
                            : "#94a3b8",
                          padding: "3px 8px",
                          borderRadius: "6px",
                          fontSize: "10px",
                          fontWeight: "700",
                          border: `1px solid ${
                            item.ai_provider === "groq" ? "rgba(168, 85, 247, 0.3)"
                            : item.ai_provider === "ollama" ? "rgba(245, 158, 11, 0.3)"
                            : item.ai_provider === "gemini" ? "rgba(16, 185, 129, 0.3)"
                            : "rgba(100, 116, 139, 0.3)"
                          }`
                        }}>
                          <Bot size={11} />
                          {
                            item.ai_provider === "groq" ? "Groq AI"
                            : item.ai_provider === "ollama" ? "Local Ollama"
                            : item.ai_provider === "gemini" ? "Gemini AI"
                            : item.ai_provider
                          }
                        </span>
                      )}
                      <span style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "4px",
                        backgroundColor: "rgba(56, 189, 248, 0.08)",
                        color: "#38bdf8",
                        border: "1px solid rgba(56, 189, 248, 0.15)",
                        padding: "3px 8px",
                        borderRadius: "6px",
                        fontSize: "10.5px",
                        fontWeight: "600"
                      }} title={`Arrived at: ${formatFullTimestamp(item.time)}`}>
                        <Clock size={11} style={{ color: "#38bdf8" }} />
                        <span>{formatFullTimestamp(item.time)}</span>
                        {formatTime(item.time) && (
                          <span style={{ color: "#94a3b8", fontSize: "10px", fontWeight: "500" }}>
                            ({formatTime(item.time)})
                          </span>
                        )}
                      </span>
                    </div>

                    <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                      <button
                        onClick={(e) => handleReanalyzeItem(e, item)}
                        disabled={reanalyzingIds[`${item.type}-${item.id}`]}
                        title="Re-analyze this news item with AI (Groq API -> Ollama fallback)"
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: "4px",
                          backgroundColor: "rgba(59, 130, 246, 0.1)",
                          border: "1px solid rgba(59, 130, 246, 0.25)",
                          color: "#60a5fa",
                          borderRadius: "6px",
                          padding: "2px 8px",
                          fontSize: "10.5px",
                          fontWeight: "600",
                          cursor: "pointer",
                          transition: "all 0.2s"
                        }}
                      >
                        <RefreshCw size={11} className={reanalyzingIds[`${item.type}-${item.id}`] ? "animate-spin" : ""} />
                        <span>{reanalyzingIds[`${item.type}-${item.id}`] ? "Analyzing..." : "Re-analyze"}</span>
                      </button>

                      {item.ai_sentiment && (
                        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                          <span style={{
                            backgroundColor: sentiment.bg,
                            color: sentiment.text,
                            border: `1px solid ${sentiment.border}`,
                            fontSize: "10px",
                            fontWeight: "700",
                            padding: "2px 8px",
                            borderRadius: "4px",
                            textTransform: "uppercase"
                          }}>
                            {sentiment.badgeText}
                          </span>
                          {item.ai_impact_score !== null && (
                            <span style={{
                              fontSize: "11px",
                              fontWeight: "700",
                              color: item.ai_impact_score > 0 ? "#10b981" : item.ai_impact_score < 0 ? "#ef4444" : "#9ca3af"
                            }}>
                              {item.ai_impact_score > 0 ? "+" : ""}{item.ai_impact_score.toFixed(1)}
                            </span>
                          )}
                        </div>
                      )}
                    </div>
                  </div>

                  <h3 style={{
                    fontSize: "14px",
                    fontWeight: "600",
                    color: "#f1f5f9",
                    margin: 0,
                    lineHeight: "1.4"
                  }}>
                    {item.title}
                  </h3>

                  {item.ai_summary && (
                    <p style={{
                      fontSize: "12px",
                      color: "#94a3b8",
                      margin: 0,
                      lineHeight: "1.5"
                    }}>
                      {item.ai_summary}
                    </p>
                  )}

                  {(() => {
                    const affectedStocks = Array.isArray(item.ai_affected_stocks)
                      ? item.ai_affected_stocks
                      : typeof item.ai_affected_stocks === "string"
                        ? (() => { try { const p = JSON.parse(item.ai_affected_stocks); return Array.isArray(p) ? p : []; } catch { return []; } })()
                        : [];
                    if (!affectedStocks.length) return null;
                    return (
                      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "6px", marginTop: "2px" }}>
                        <span style={{ fontSize: "10px", color: "#64748b", fontWeight: "600" }}>Impacted:</span>
                        {affectedStocks.map((stk, idx) => (
                          <span
                            key={idx}
                            onClick={(e) => {
                              e.stopPropagation(); // prevent modal opening
                              setSearchQuery(stk); // filter by this stock!
                              setPage(1);
                            }}
                            style={{
                              backgroundColor: "rgba(16, 185, 129, 0.12)",
                              color: "#34d399",
                              padding: "2px 6px",
                              borderRadius: "4px",
                              fontSize: "10px",
                              fontWeight: "700",
                              cursor: "pointer",
                              transition: "all 0.15s"
                            }}
                            onMouseOver={(e) => {
                              e.currentTarget.style.backgroundColor = "rgba(16, 185, 129, 0.25)";
                            }}
                            onMouseOut={(e) => {
                              e.currentTarget.style.backgroundColor = "rgba(16, 185, 129, 0.12)";
                            }}
                          >
                            {stk}
                          </span>
                        ))}
                      </div>
                    );
                  })()}

                  {/* News Story specific indicator */}
                  {item.type === "news_story" && item.article_count && item.article_count > 1 && (
                    <div style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "6px",
                      fontSize: "11px",
                      color: "#60a5fa",
                      backgroundColor: "rgba(96, 165, 250, 0.08)",
                      padding: "4px 8px",
                      borderRadius: "6px",
                      width: "fit-content"
                    }}>
                      <Newspaper size={12} />
                      Consolidated {item.article_count} similar sources
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>

        {/* Pagination */}
        {totalPages > 1 && (
          <div style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: "10px",
            marginTop: "10px"
          }}>
            <button
              disabled={page === 1}
              onClick={() => setPage(prev => Math.max(1, prev - 1))}
              style={{
                backgroundColor: "rgba(255,255,255,0.04)",
                border: "1px solid rgba(255,255,255,0.06)",
                borderRadius: "6px",
                padding: "6px 12px",
                color: page === 1 ? "#475569" : "#f1f5f9",
                fontSize: "12px",
                cursor: page === 1 ? "not-allowed" : "pointer"
              }}
            >
              Previous
            </button>
            <span style={{ fontSize: "12px", color: "#94a3b8" }}>
              Page {page} of {totalPages}
            </span>
            <button
              disabled={page === totalPages}
              onClick={() => setPage(prev => Math.min(totalPages, prev + 1))}
              style={{
                backgroundColor: "rgba(255,255,255,0.04)",
                border: "1px solid rgba(255,255,255,0.06)",
                borderRadius: "6px",
                padding: "6px 12px",
                color: page === totalPages ? "#475569" : "#f1f5f9",
                fontSize: "12px",
                cursor: page === totalPages ? "not-allowed" : "pointer"
              }}
            >
              Next
            </button>
          </div>
        )}
      </div>

      {/* RIGHT COLUMN: AI Alerts center & Suggestion Engine with Tabs */}
      <div style={{ display: "flex", flexDirection: "column", gap: "20px", width: "360px", flexShrink: 0 }}>
        {/* Tab Buttons */}
        <div style={{
          display: "flex",
          backgroundColor: "rgba(17, 24, 39, 0.4)",
          border: "1px solid rgba(255, 255, 255, 0.05)",
          borderRadius: "12px",
          padding: "4px",
          gap: "4px"
        }}>
          {[
            { id: "ai", label: "AI Market Sentiment" },
            { id: "active", label: "Active Stocks (24h)" },
            { id: "news", label: "Global/Stock News" },
            { id: "earnings", label: "Upcoming Earnings" }
          ].map((tab) => (
            <button
              key={tab.id}
              onClick={() => setSidebarTab(tab.id)}
              style={{
                flex: 1,
                padding: "8px 2px",
                borderRadius: "8px",
                fontSize: "10px",
                fontWeight: "700",
                border: "none",
                cursor: "pointer",
                backgroundColor: sidebarTab === tab.id ? "#2563eb" : "transparent",
                color: sidebarTab === tab.id ? "#ffffff" : "#94a3b8",
                transition: "all 0.2s"
              }}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {/* Tab Content: AI recommendations & Alert Center */}
        {sidebarTab === "ai" && (
          <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
            <div style={{
              background: "rgba(17, 24, 39, 0.4)",
              border: "1px solid rgba(255, 255, 255, 0.05)",
              borderRadius: "16px",
              padding: "16px",
              display: "flex",
              flexDirection: "column",
              gap: "14px"
            }}>
              {/* Header */}
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                  <TrendingUp size={18} className="text-blue-500" />
                  <h2 style={{ fontSize: "14px", fontWeight: "700", color: "#f8fafc", margin: 0 }}>
                    AI Market Sentiment
                  </h2>
                </div>
                <button
                  onClick={() => fetchMarketSentiment(true)}
                  disabled={refreshingSentiment || loadingSentiment}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "4px",
                    background: "rgba(255, 255, 255, 0.05)",
                    border: "1px solid rgba(255, 255, 255, 0.08)",
                    borderRadius: "6px",
                    padding: "4px 8px",
                    fontSize: "11px",
                    fontWeight: "600",
                    color: "#94a3b8",
                    cursor: (refreshingSentiment || loadingSentiment) ? "not-allowed" : "pointer",
                    transition: "all 0.15s"
                  }}
                >
                  <RefreshCw size={12} className={refreshingSentiment ? "animate-spin text-blue-500" : ""} />
                  {refreshingSentiment ? "Updating..." : "Refresh"}
                </button>
              </div>

              {/* Main Content */}
              {loadingSentiment && !refreshingSentiment ? (
                <div style={{
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  justifyContent: "center",
                  padding: "40px 10px",
                  gap: "12px"
                }}>
                  <RefreshCw className="animate-spin text-blue-500" size={24} />
                  <span style={{ fontSize: "12px", color: "#64748b" }}>Analyzing market sentiment...</span>
                </div>
              ) : !marketSentiment ? (
                <div style={{ fontSize: "12px", color: "#ef4444", padding: "20px 0", textAlign: "center" }}>
                  Failed to load market sentiment.
                </div>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
                  {/* Gauge / Sentiment Score Display */}
                  <div style={{
                    backgroundColor: "rgba(13, 19, 31, 0.6)",
                    border: "1px solid rgba(255, 255, 255, 0.03)",
                    borderRadius: "12px",
                    padding: "14px",
                    display: "flex",
                    flexDirection: "column",
                    gap: "10px"
                  }}>
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                      <span style={{ fontSize: "11px", fontWeight: "600", color: "#64748b" }}>Market Mood</span>
                      <span style={{
                        backgroundColor: marketSentiment.sentiment === "Bullish" ? "rgba(16, 185, 129, 0.15)" : marketSentiment.sentiment === "Bearish" ? "rgba(239, 68, 68, 0.15)" : "rgba(107, 114, 128, 0.15)",
                        color: marketSentiment.sentiment === "Bullish" ? "#10b981" : marketSentiment.sentiment === "Bearish" ? "#ef4444" : "#9ca3af",
                        fontSize: "11px",
                        fontWeight: "800",
                        padding: "2px 8px",
                        borderRadius: "4px",
                        textTransform: "uppercase"
                      }}>
                        {marketSentiment.sentiment === "Bullish" ? "📈 Bullish" : marketSentiment.sentiment === "Bearish" ? "📉 Bearish" : "⚖️ Neutral"}
                      </span>
                    </div>

                    {/* Horizontal Score Meter */}
                    <div style={{ display: "flex", flexDirection: "column", gap: "4px", marginTop: "4px" }}>
                      <div style={{
                        position: "relative",
                        height: "6px",
                        width: "100%",
                        backgroundColor: "rgba(255, 255, 255, 0.08)",
                        borderRadius: "3px"
                      }}>
                        {/* Middle anchor */}
                        <div style={{
                          position: "absolute",
                          left: "50%",
                          top: 0,
                          width: "1px",
                          height: "6px",
                          backgroundColor: "rgba(255,255,255,0.2)"
                        }} />
                        {/* Score indicator pin */}
                        <div style={{
                          position: "absolute",
                          left: `calc(50% + (${marketSentiment.score} * 50%))`,
                          transform: "translateX(-50%)",
                          top: "-3px",
                          width: "12px",
                          height: "12px",
                          borderRadius: "50%",
                          backgroundColor: marketSentiment.score > 0.2 ? "#10b981" : marketSentiment.score < -0.2 ? "#ef4444" : "#9ca3af",
                          boxShadow: marketSentiment.score > 0.2 ? "0 0 8px #10b981" : marketSentiment.score < -0.2 ? "0 0 8px #ef4444" : "0 0 8px #9ca3af",
                          transition: "all 0.3s ease"
                        }} />
                      </div>
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "9px", color: "#475569" }}>
                        <span>Bearish</span>
                        <span style={{ fontWeight: "700", color: marketSentiment.score > 0.2 ? "#10b981" : marketSentiment.score < -0.2 ? "#ef4444" : "#9ca3af" }}>
                          Score: {marketSentiment.score > 0 ? "+" : ""}{marketSentiment.score.toFixed(2)}
                        </span>
                        <span>Bullish</span>
                      </div>
                    </div>

                    {/* Buyer/Seller Pressure Bar */}
                    <div style={{ display: "flex", flexDirection: "column", gap: "6px", marginTop: "10px", borderTop: "1px solid rgba(255,255,255,0.04)", paddingTop: "10px" }}>
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "10px", color: "#64748b" }}>
                        <span>Nifty 50 Activity</span>
                        <span>Advances: <strong style={{ color: "#10b981" }}>{(marketSentiment as any).advances || 0}</strong> | Declines: <strong style={{ color: "#ef4444" }}>{(marketSentiment as any).declines || 0}</strong></span>
                      </div>

                      <div style={{
                        display: "flex",
                        height: "6px",
                        width: "100%",
                        backgroundColor: "rgba(255, 255, 255, 0.05)",
                        borderRadius: "3px",
                        overflow: "hidden"
                      }}>
                        <div style={{
                          width: `${(marketSentiment as any).buyers_pct || 50}%`,
                          backgroundColor: "#10b981",
                          height: "100%",
                          transition: "width 0.5s ease-in-out"
                        }} />
                        <div style={{
                          width: `${(marketSentiment as any).sellers_pct || 50}%`,
                          backgroundColor: "#ef4444",
                          height: "100%",
                          transition: "width 0.5s ease-in-out"
                        }} />
                      </div>

                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "9px", color: "#475569" }}>
                        <span style={{ color: "#10b981", fontWeight: "600" }}>Buyers: {(marketSentiment as any).buyers_pct || 50}%</span>
                        <span style={{ color: "#ef4444", fontWeight: "600" }}>Sellers: {(marketSentiment as any).sellers_pct || 50}%</span>
                      </div>
                    </div>
                  </div>

                  {/* Narrative Summary */}
                  <div style={{
                    fontSize: "12px",
                    color: "#cbd5e1",
                    lineHeight: "1.5",
                    borderLeft: "2px solid #2563eb",
                    paddingLeft: "10px",
                    margin: "2px 0"
                  }}>
                    {marketSentiment.summary}
                  </div>

                  {/* Major Drivers */}
                  {marketSentiment.drivers && marketSentiment.drivers.length > 0 && (
                    <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
                      <span style={{ fontSize: "11px", fontWeight: "700", color: "#64748b" }}>Recent Market Drivers:</span>
                      <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
                        {marketSentiment.drivers.map((drv, idx) => (
                          <div key={idx} style={{
                            backgroundColor: "rgba(255,255,255,0.01)",
                            border: "1px solid rgba(255,255,255,0.02)",
                            borderRadius: "8px",
                            padding: "8px 10px",
                            display: "flex",
                            flexDirection: "column",
                            gap: "3px"
                          }}>
                            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "6px" }}>
                              <span style={{
                                fontSize: "8px",
                                fontWeight: "800",
                                color: drv.impact === "Positive" ? "#10b981" : drv.impact === "Negative" ? "#ef4444" : "#9ca3af",
                                backgroundColor: drv.impact === "Positive" ? "rgba(16,185,129,0.1)" : drv.impact === "Negative" ? "rgba(239,68,68,0.1)" : "rgba(107,114,128,0.1)",
                                padding: "1px 5px",
                                borderRadius: "3px",
                                textTransform: "uppercase"
                              }}>
                                {drv.impact}
                              </span>
                              <span style={{ fontSize: "9px", color: "#475569" }}>
                                {drv.source} • {drv.time}
                              </span>
                            </div>
                            <span style={{ fontSize: "11px", fontWeight: "500", color: "#e2e8f0", lineHeight: "1.4" }}>
                              {drv.title}
                            </span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Impacted Sectors */}
                  {marketSentiment.sectors && (
                    <div style={{ display: "flex", flexDirection: "column", gap: "8px", marginTop: "2px" }}>
                      <span style={{ fontSize: "11px", fontWeight: "700", color: "#64748b" }}>Sector Impacts:</span>
                      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "10px" }}>
                        <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
                          <span style={{ fontSize: "9px", color: "#10b981", fontWeight: "700" }}>🟢 Positive Focus</span>
                          <div style={{ display: "flex", flexWrap: "wrap", gap: "4px" }}>
                            {marketSentiment.sectors.positive.length === 0 ? (
                              <span style={{ fontSize: "10px", color: "#475569" }}>None</span>
                            ) : (
                              marketSentiment.sectors.positive.map((sec, i) => (
                                <span key={i} style={{
                                  backgroundColor: "rgba(16,185,129,0.08)",
                                  color: "#34d399",
                                  padding: "2px 6px",
                                  borderRadius: "4px",
                                  fontSize: "9px",
                                  fontWeight: "600"
                                }}>
                                  {sec}
                                </span>
                              ))
                            )}
                          </div>
                        </div>
                        <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
                          <span style={{ fontSize: "9px", color: "#ef4444", fontWeight: "700" }}>🔴 Negative Risk</span>
                          <div style={{ display: "flex", flexWrap: "wrap", gap: "4px" }}>
                            {marketSentiment.sectors.negative.length === 0 ? (
                              <span style={{ fontSize: "10px", color: "#475569" }}>None</span>
                            ) : (
                              marketSentiment.sectors.negative.map((sec, i) => (
                                <span key={i} style={{
                                  backgroundColor: "rgba(239,68,68,0.08)",
                                  color: "#f87171",
                                  padding: "2px 6px",
                                  borderRadius: "4px",
                                  fontSize: "9px",
                                  fontWeight: "600"
                                }}>
                                  {sec}
                                </span>
                              ))
                            )}
                          </div>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* Footer Stats Ticker */}
                  <div style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    borderTop: "1px solid rgba(255,255,255,0.03)",
                    paddingTop: "10px",
                    fontSize: "9px",
                    color: "#475569"
                  }}>
                    <span>Checked every 5 min</span>
                    <span>Last analyzed: {formatTime(marketSentiment.last_updated)}</span>
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {/* Tab Content: Active Stocks (24h) */}
        {sidebarTab === "active" && (
          <div style={{
            background: "rgba(17, 24, 39, 0.4)",
            border: "1px solid rgba(255, 255, 255, 0.05)",
            borderRadius: "16px",
            padding: "16px",
            display: "flex",
            flexDirection: "column",
            gap: "14px"
          }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <TrendingUp size={18} className="text-emerald-500" />
                <h2 style={{ fontSize: "14px", fontWeight: "700", color: "#f8fafc", margin: 0 }}>
                  Active Stocks (Past 24h)
                </h2>
              </div>
              {activeStocks.some(stk => readStocks[stk.symbol] !== stk.time) && (
                <button
                  onClick={handleMarkAllStocksRead}
                  style={{
                    background: "none",
                    border: "none",
                    color: "#60a5fa",
                    fontSize: "11px",
                    fontWeight: "600",
                    cursor: "pointer"
                  }}
                >
                  Mark all read
                </button>
              )}
            </div>

            <div style={{
              display: "flex",
              flexWrap: "wrap",
              gap: "8px",
              maxHeight: "380px",
              overflowY: "auto",
              paddingRight: "4px",
              paddingTop: "6px"
            }}>
              {activeStocks.length === 0 ? (
                <div style={{ fontSize: "12px", color: "#64748b", padding: "30px 10px", width: "100%", textAlign: "center" }}>
                  No stock-specific news in the past 24h...
                </div>
              ) : (
                activeStocks.map((stk) => {
                  const isUnread = !readStocks[stk.symbol] || readStocks[stk.symbol] !== stk.time;
                  const sentimentColor =
                    stk.sentiment === "positive" ? "#10b981" :
                      stk.sentiment === "negative" ? "#ef4444" : "#64748b";

                  return (
                    <div
                      key={stk.symbol}
                      onClick={() => {
                        setSearchQuery(stk.symbol);
                        setPage(1);
                        handleMarkStockRead(stk.symbol, stk.time);
                      }}
                      onMouseEnter={() => setHoveredStock(stk)}
                      onMouseLeave={() => setHoveredStock(null)}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "6px",
                        backgroundColor: isUnread ? "rgba(37, 99, 235, 0.15)" : "rgba(13, 19, 31, 0.4)",
                        border: `1px solid ${isUnread ? "rgba(37, 99, 235, 0.4)" : "rgba(255, 255, 255, 0.05)"}`,
                        borderRadius: "20px",
                        padding: "6px 12px",
                        cursor: "pointer",
                        transition: "all 0.15s",
                        opacity: isUnread ? 1 : 0.6,
                        boxShadow: isUnread ? "0 0 8px rgba(37, 99, 235, 0.1)" : "none"
                      }}
                    >
                      <span style={{
                        width: "6px",
                        height: "6px",
                        borderRadius: "50%",
                        backgroundColor: sentimentColor,
                        display: "inline-block",
                        boxShadow: isUnread ? `0 0 6px ${sentimentColor}` : "none"
                      }} />
                      <span style={{
                        fontSize: "11px",
                        fontWeight: isUnread ? "700" : "500",
                        color: isUnread ? "#ffffff" : "#94a3b8"
                      }}>
                        {stk.symbol}
                      </span>
                    </div>
                  );
                })
              )}
            </div>

            {/* Hover preview area */}
            {hoveredStock && (
              <div style={{
                marginTop: "4px",
                backgroundColor: "rgba(13, 19, 31, 0.9)",
                border: "1px solid rgba(255,255,255,0.06)",
                borderRadius: "10px",
                padding: "10px 12px",
                display: "flex",
                flexDirection: "column",
                gap: "4px"
              }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span style={{ fontWeight: "700", color: "#f8fafc", fontSize: "11px" }}>{hoveredStock.symbol}</span>
                  <span style={{
                    fontSize: "9px",
                    fontWeight: "700",
                    color: hoveredStock.sentiment === "positive" ? "#10b981" : hoveredStock.sentiment === "negative" ? "#ef4444" : "#94a3b8"
                  }}>
                    {hoveredStock.sentiment.toUpperCase()}
                  </span>
                  <span style={{ fontSize: "9px", color: "#64748b" }}>{formatTime(hoveredStock.time)}</span>
                </div>
                <p style={{ fontSize: "11px", color: "#f1f5f9", margin: 0, lineHeight: "1.4", fontWeight: "600" }}>
                  {hoveredStock.title}
                </p>
                {hoveredStock.impact_score !== 0 && (
                  <span style={{ fontSize: "9px", color: "#64748b" }}>
                    Impact Score: {hoveredStock.impact_score > 0 ? "+" : ""}{hoveredStock.impact_score}
                  </span>
                )}
              </div>
            )}
          </div>
        )}

        {/* Tab Content: Global/Stock News */}
        {sidebarTab === "news" && (
          <div style={{
            background: "rgba(17, 24, 39, 0.4)",
            border: "1px solid rgba(255, 255, 255, 0.05)",
            borderRadius: "16px",
            padding: "16px",
            display: "flex",
            flexDirection: "column",
            gap: "14px"
          }}>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <Newspaper size={18} className="text-blue-400" />
              <h2 style={{ fontSize: "14px", fontWeight: "700", color: "#f8fafc", margin: 0 }}>
                Global/Stock News (Past 48h)
              </h2>
            </div>

            <div style={{
              display: "flex",
              flexDirection: "column",
              gap: "10px",
              maxHeight: "650px",
              overflowY: "auto",
              paddingRight: "4px"
            }}>
              {sidebarNews.length === 0 ? (
                <div style={{ fontSize: "12px", color: "#64748b", padding: "30px 10px", textAlign: "center" }}>
                  No macro or stock market news stories...
                </div>
              ) : (
                sidebarNews.map((story, i) => {
                  const sentiment = getSentimentStyles(story.ai_sentiment);
                  return (
                    <div
                      key={i}
                      onClick={() => setSelectedItem(story)}
                      style={{
                        backgroundColor: "rgba(13, 19, 31, 0.6)",
                        borderLeft: `3px solid ${sentiment.text}`,
                        borderTop: "1px solid rgba(255,255,255,0.03)",
                        borderRight: "1px solid rgba(255,255,255,0.03)",
                        borderBottom: "1px solid rgba(255,255,255,0.03)",
                        borderRadius: "0 10px 10px 0",
                        padding: "10px 12px",
                        cursor: "pointer",
                        transition: "all 0.15s",
                        display: "flex",
                        flexDirection: "column",
                        gap: "6px"
                      }}
                      onMouseOver={(e) => {
                        e.currentTarget.style.backgroundColor = "rgba(13, 19, 31, 0.8)";
                      }}
                      onMouseOut={(e) => {
                        e.currentTarget.style.backgroundColor = "rgba(13, 19, 31, 0.6)";
                      }}
                    >
                      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "6px" }}>
                        <span style={{ fontSize: "9px", color: "#64748b", fontWeight: "600" }}>
                          {getDisplaySource(story)}
                        </span>
                        <span style={{
                          fontSize: "9px",
                          fontWeight: "700",
                          backgroundColor: sentiment.bg,
                          color: sentiment.text,
                          padding: "1px 5px",
                          borderRadius: "4px",
                          textTransform: "uppercase"
                        }}>
                          {sentiment.badgeText}
                        </span>
                      </div>

                      <div style={{
                        fontSize: "11px",
                        fontWeight: "600",
                        color: "#f1f5f9",
                        lineHeight: "1.4"
                      }}>
                        {story.title}
                      </div>

                      {story.symbol && (
                        <span
                          onClick={(e) => {
                            e.stopPropagation();
                            setSearchQuery(story.symbol);
                            setPage(1);
                          }}
                          style={{
                            backgroundColor: "rgba(59, 130, 246, 0.15)",
                            color: "#93c5fd",
                            padding: "2px 6px",
                            borderRadius: "4px",
                            fontSize: "9px",
                            fontWeight: "700",
                            width: "fit-content",
                            cursor: "pointer"
                          }}
                        >
                          {story.symbol}
                        </span>
                      )}

                      <div style={{
                        fontSize: "9.5px",
                        color: "#38bdf8",
                        fontWeight: "500",
                        display: "flex",
                        alignItems: "center",
                        gap: "4px",
                        justifyContent: "flex-end",
                        marginTop: "4px"
                      }}>
                        <Clock size={10} />
                        <span>{formatFullTimestamp(story.time)}{formatTime(story.time) ? ` (${formatTime(story.time)})` : ""}</span>
                      </div>
                    </div>
                  );
                })
              )}
            </div>
          </div>
        )}

        {/* Tab Content: Upcoming Earnings (7d) */}
        {sidebarTab === "earnings" && (
          <div style={{
            background: "rgba(17, 24, 39, 0.4)",
            border: "1px solid rgba(255, 255, 255, 0.05)",
            borderRadius: "16px",
            padding: "16px",
            display: "flex",
            flexDirection: "column",
            gap: "14px"
          }}>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <FileText size={18} className="text-blue-500" />
              <h2 style={{ fontSize: "14px", fontWeight: "700", color: "#f8fafc", margin: 0 }}>
                Upcoming Earnings (7 Days)
              </h2>
            </div>

            <div style={{
              display: "flex",
              flexDirection: "column",
              gap: "10px",
              maxHeight: "650px",
              overflowY: "auto",
              paddingRight: "4px"
            }}>
              {upcomingEarnings.length === 0 ? (
                <div style={{ fontSize: "12px", color: "#64748b", padding: "30px 10px", textAlign: "center" }}>
                  No earnings scheduled in the next 7 days...
                </div>
              ) : (
                upcomingEarnings.map((earn, i) => (
                  <div
                    key={i}
                    onClick={() => {
                      setSearchQuery(earn.symbol);
                      setPage(1);
                    }}
                    style={{
                      backgroundColor: "rgba(13, 19, 31, 0.6)",
                      border: "1px solid rgba(255,255,255,0.03)",
                      borderRadius: "10px",
                      padding: "10px 12px",
                      cursor: "pointer",
                      transition: "all 0.15s"
                    }}
                    onMouseOver={(e) => {
                      e.currentTarget.style.backgroundColor = "rgba(13, 19, 31, 0.8)";
                    }}
                    onMouseOut={(e) => {
                      e.currentTarget.style.backgroundColor = "rgba(13, 19, 31, 0.6)";
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                      <span style={{ fontWeight: "700", color: "#f1f5f9", fontSize: "13px" }}>
                        {earn.symbol}
                      </span>
                      <span style={{
                        fontSize: "10px",
                        fontWeight: "700",
                        color: "#60a5fa",
                        backgroundColor: "rgba(96, 165, 250, 0.12)",
                        padding: "2px 6px",
                        borderRadius: "4px"
                      }}>
                        {new Date(earn.date).toLocaleDateString("en-IN", { month: "short", day: "numeric" })}
                      </span>
                    </div>

                    <div style={{
                      fontSize: "11px",
                      color: "#94a3b8",
                      marginTop: "6px",
                      lineHeight: "1.4"
                    }}>
                      {earn.purpose}
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        )}
      </div>

      {/* DETAIL MODAL OVERLAY */}
      {selectedItem && (
        <div style={{
          position: "fixed",
          top: 0,
          left: 0,
          right: 0,
          bottom: 0,
          backgroundColor: "rgba(0,0,0,0.8)",
          backdropFilter: "blur(4px)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          zIndex: 1000,
          padding: "20px"
        }} onClick={() => setSelectedItem(null)}>
          <div style={{
            background: "#0c1322",
            border: "1px solid rgba(255,255,255,0.08)",
            borderRadius: "16px",
            width: "100%",
            maxWidth: "600px",
            maxHeight: "85vh",
            overflowY: "auto",
            display: "flex",
            flexDirection: "column",
            boxShadow: "0 20px 50px rgba(0,0,0,0.5)"
          }} onClick={(e) => e.stopPropagation()}>
            {/* Modal Header */}
            <div style={{
              padding: "16px 20px",
              borderBottom: "1px solid rgba(255,255,255,0.05)",
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              width: "100%"
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <span style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "4px",
                  backgroundColor: "rgba(255, 255, 255, 0.05)",
                  padding: "3px 8px",
                  borderRadius: "4px",
                  fontSize: "10px",
                  fontWeight: "600"
                }}>
                  {getDisplaySource(selectedItem)}
                </span>
                <span style={{ fontSize: "11px", color: "#38bdf8", display: "flex", alignItems: "center", gap: "4px" }}>
                  <Clock size={12} />
                  {formatFullTimestamp(selectedItem.time)}
                </span>
              </div>
              <button
                onClick={() => setSelectedItem(null)}
                style={{
                  background: "none",
                  border: "none",
                  color: "#94a3b8",
                  fontSize: "20px",
                  cursor: "pointer"
                }}
              >
                &times;
              </button>
            </div>

            {/* Modal Body */}
            <div style={{ padding: "20px", display: "flex", flexDirection: "column", gap: "16px" }}>
              {selectedItem.symbol && (
                <div style={{
                  backgroundColor: "rgba(59, 130, 246, 0.15)",
                  color: "#93c5fd",
                  padding: "4px 10px",
                  borderRadius: "6px",
                  fontSize: "12px",
                  fontWeight: "700",
                  width: "fit-content"
                }}>
                  Stock Ticker: {selectedItem.symbol}
                </div>
              )}

              {selectedItem.ai_affected_stocks && selectedItem.ai_affected_stocks.length > 0 && (
                <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
                  <span style={{ fontSize: "11px", fontWeight: "600", color: "#64748b" }}>Impacted Stocks:</span>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: "8px" }}>
                    {selectedItem.ai_affected_stocks.map((stk, idx) => (
                      <span
                        key={idx}
                        onClick={() => {
                          setSearchQuery(stk); // filter by this stock!
                          setPage(1);
                          setSelectedItem(null); // close modal
                        }}
                        style={{
                          backgroundColor: "rgba(16, 185, 129, 0.15)",
                          color: "#34d399",
                          padding: "4px 10px",
                          borderRadius: "6px",
                          fontSize: "12px",
                          fontWeight: "700",
                          cursor: "pointer",
                          transition: "all 0.15s"
                        }}
                        onMouseOver={(e) => {
                          e.currentTarget.style.backgroundColor = "rgba(16, 185, 129, 0.25)";
                        }}
                        onMouseOut={(e) => {
                          e.currentTarget.style.backgroundColor = "rgba(16, 185, 129, 0.15)";
                        }}
                      >
                        {stk}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              <h2 style={{
                fontSize: "16px",
                fontWeight: "700",
                color: "#ffffff",
                margin: 0,
                lineHeight: "1.4"
              }}>
                {selectedItem.title}
              </h2>

              {/* Sentiment block */}
              {selectedItem.ai_sentiment && (
                <div style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "12px",
                  backgroundColor: "rgba(255,255,255,0.02)",
                  padding: "12px",
                  borderRadius: "10px",
                  border: "1px solid rgba(255,255,255,0.04)"
                }}>
                  <div style={{
                    backgroundColor: getSentimentStyles(selectedItem.ai_sentiment).bg,
                    color: getSentimentStyles(selectedItem.ai_sentiment).text,
                    fontSize: "11px",
                    fontWeight: "800",
                    padding: "3px 10px",
                    borderRadius: "4px",
                    textTransform: "uppercase"
                  }}>
                    {getSentimentStyles(selectedItem.ai_sentiment).badgeText}
                  </div>
                  {selectedItem.ai_impact_score !== null && (
                    <div style={{ fontSize: "13px", fontWeight: "700" }}>
                      AI Score: <span style={{
                        color: selectedItem.ai_impact_score > 0 ? "#10b981" : selectedItem.ai_impact_score < 0 ? "#ef4444" : "#9ca3af"
                      }}>
                        {selectedItem.ai_impact_score > 0 ? "+" : ""}{selectedItem.ai_impact_score.toFixed(2)}
                      </span>
                    </div>
                  )}
                </div>
              )}

              {/* Source Original Description */}
              {selectedItem.description && (
                <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
                  <span style={{ fontSize: "11px", fontWeight: "600", color: "#64748b" }}>Details / Content:</span>
                  <div style={{
                    fontSize: "12px",
                    color: "#cbd5e1",
                    backgroundColor: "rgba(0,0,0,0.2)",
                    padding: "12px",
                    borderRadius: "8px",
                    maxHeight: "180px",
                    overflowY: "auto",
                    whiteSpace: "pre-line",
                    lineHeight: "1.5"
                  }}>
                    {selectedItem.description}
                  </div>
                </div>
              )}

              {/* Key Metrics block for Filings */}
              {selectedItem.ai_key_metrics && (
                <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
                  <span style={{ fontSize: "11px", fontWeight: "600", color: "#64748b" }}>Extracted Key Metrics:</span>
                  <div style={{
                    display: "grid",
                    gridTemplateColumns: "1fr 1fr",
                    gap: "10px",
                    backgroundColor: "rgba(255,255,255,0.02)",
                    padding: "12px",
                    borderRadius: "8px"
                  }}>
                    {Object.entries(selectedItem.ai_key_metrics).map(([key, val]) => (
                      <div key={key} style={{ fontSize: "12px" }}>
                        <span style={{ color: "#94a3b8", textTransform: "capitalize" }}>{key.replace("_", " ")}: </span>
                        <span style={{ fontWeight: "700", color: "#f8fafc" }}>
                          {typeof val === "object" ? JSON.stringify(val) : String(val)}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Consolidated sources list */}
              {selectedItem.articles && selectedItem.articles.length > 0 && (
                <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
                  <span style={{ fontSize: "11px", fontWeight: "600", color: "#64748b" }}>Similar Outlets:</span>
                  {selectedItem.articles.map((art, idx) => (
                    <a
                      key={idx}
                      href={art.url}
                      target="_blank"
                      rel="noreferrer"
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        backgroundColor: "rgba(255,255,255,0.02)",
                        padding: "10px 14px",
                        borderRadius: "8px",
                        textDecoration: "none",
                        color: "#93c5fd",
                        fontSize: "12px",
                        border: "1px solid rgba(255, 255, 255, 0.03)",
                        transition: "all 0.2s"
                      }}
                      onMouseOver={(e) => {
                        e.currentTarget.style.backgroundColor = "rgba(255, 255, 255, 0.05)";
                        e.currentTarget.style.borderColor = "rgba(255, 255, 255, 0.08)";
                      }}
                      onMouseOut={(e) => {
                        e.currentTarget.style.backgroundColor = "rgba(255, 255, 255, 0.02)";
                        e.currentTarget.style.borderColor = "rgba(255, 255, 255, 0.03)";
                      }}
                    >
                      <div style={{ display: "flex", flexDirection: "column", gap: "4px", width: "90%" }}>
                        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                          <span style={{
                            backgroundColor: "rgba(96, 165, 250, 0.1)",
                            color: "#60a5fa",
                            padding: "2px 6px",
                            borderRadius: "4px",
                            fontSize: "9px",
                            fontWeight: "700"
                          }}>
                            {formatSourceName(art.source)}
                          </span>
                          {art.published_at && (
                            <span style={{ fontSize: "9px", color: "#64748b" }}>
                              {formatFullTimestamp(art.published_at)}
                            </span>
                          )}
                        </div>
                        <span style={{ color: "#cbd5e1", fontWeight: "500", lineHeight: "1.4" }}>
                          {art.headline}
                        </span>
                      </div>
                      <ExternalLink size={14} style={{ color: "#64748b", flexShrink: 0 }} />
                    </a>
                  ))}
                </div>
              )}
            </div>

            {/* Modal Footer */}
            {selectedItem.url && (
              <div style={{
                padding: "14px 20px",
                borderTop: "1px solid rgba(255,255,255,0.05)",
                marginTop: "auto"
              }}>
                <a
                  href={selectedItem.url}
                  target="_blank"
                  rel="noreferrer"
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    gap: "6px",
                    backgroundColor: "#2563eb",
                    color: "white",
                    borderRadius: "8px",
                    padding: "10px",
                    fontSize: "12px",
                    fontWeight: "700",
                    textDecoration: "none",
                    textAlign: "center"
                  }}
                >
                  View Original Source
                  <ExternalLink size={14} />
                </a>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
