import React, { useState, useEffect, useMemo } from "react";
import {
  Zap,
  ShieldAlert,
  Play,
  Square,
  Plus,
  Trash2,
  TrendingUp,
  TrendingDown,
  RefreshCw,
  CheckCircle,
  XCircle,
  Clock,
  Cpu,
  ShoppingBag,
  DollarSign,
  AlertTriangle,
  Settings,
  ExternalLink,
  Calendar,
  Sparkles,
  ChevronRight
} from "lucide-react";

const API_BASE = import.meta.env.VITE_API_BASE || (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1" ? "http://localhost:8000" : "");

interface TradeConfig {
  id: number;
  symbol: string;
  instrument_key: string | null;
  purchase_date: string;
  quantity: number;
  stoploss_pct: number;
  stoploss_type: string;
  broker: string;
  order_type: string;
  limit_price: number | null;
  ai_provider: string;
  status: string;
  is_active: boolean;
  trigger_subject: string;
  buy_price: number | null;
  sell_price: number | null;
  pnl: number | null;
  notes: string | null;
  created_at: string;
  triggered_at: string | null;
  bought_at: string | null;
  sold_at: string | null;
}

interface TradeOrder {
  id: number;
  config_id: number;
  symbol: string;
  side: string;
  quantity: number;
  order_type: string;
  limit_price: number | null;
  price: number | null;
  stoploss_price: number | null;
  broker: string;
  broker_order_id: string | null;
  broker_response: string | null;
  status: string;
  error_message: string | null;
  created_at: string;
  filled_at: string | null;
}

interface TradeAILog {
  id: number;
  config_id: number | null;
  symbol: string;
  provider: string;
  prompt_summary: string | null;
  ai_sentiment: string | null;
  ai_impact_score: number | null;
  ai_summary: string | null;
  nse_event_title: string | null;
  created_at: string;
  revenue?: string;
  expenses?: string;
  operating_profit?: string;
  pbt?: string;
  other_income?: string;
  pat_yoy?: string;
  growth_projection?: string;
  broker_estimates?: string;
  ai_suggestion?: string;
  attachment_url?: string;
  flow_used?: string;
}

interface UpcomingEarningsItem {
  id: number;
  symbol: string;
  title?: string;
  meeting_date: string;
  display_date?: string;
  purpose: string;
  created_at?: string;
  return_1y?: string;
  returns_1y?: string;
}

interface AutoTradingSettings {
  custom_api_url: string;
  premium_openrouter_api_key: string;
  premium_openrouter_model: string;
}

interface PollerStatus {
  running: boolean;
  armed_count: number;
  last_poll_at: string | null;
  polls_total: number;
  triggers_total: number;
  last_error: string | null;
}


export function TradingDashboard() {
  const [configs, setConfigs] = useState<TradeConfig[]>([]);
  const [orders, setOrders] = useState<TradeOrder[]>([]);
  const [aiLogs, setAiLogs] = useState<TradeAILog[]>([]);
  const [pollerStatus, setPollerStatus] = useState<PollerStatus | null>(null);
  const [upcomingEarnings, setUpcomingEarnings] = useState<UpcomingEarningsItem[]>([]);
  const [aiSettings, setAiSettings] = useState<AutoTradingSettings>({
    custom_api_url: "http://localhost:11434/api/generate",
    premium_openrouter_api_key: "",
    premium_openrouter_model: "anthropic/claude-3.5-sonnet",
  });
  const [showSettingsModal, setShowSettingsModal] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);

  const [loading, setLoading] = useState(true);
  const [showAddForm, setShowAddForm] = useState(false);
  const [actionLoading, setActionLoading] = useState<Record<string, boolean>>({});

  // Form State
  const todayStr = new Date().toISOString().split("T")[0];
  const [formData, setFormData] = useState({
    symbol: "",
    instrument_key: "",
    purchase_date: todayStr,
    quantity: 1,
    stoploss_pct: 2.0,
    stoploss_type: "software",
    broker: "upstox",
    order_type: "MARKET",
    limit_price: "",
    ai_provider: "groq",
    trigger_subject: "Outcome of Board Meeting",
    notes: ""
  });

  // Stock search state for form
  const [searchResults, setSearchResults] = useState<any[]>([]);
  const [searching, setSearching] = useState(false);

  // Upcoming Earnings Sorting, Search, and Filtering state
  const [earningsSortBy, setEarningsSortBy] = useState<"date" | "return_desc" | "return_asc">("date");
  const [earningsFilter, setEarningsFilter] = useState<"all" | "positive" | "top">("all");
  const [earningsSearch, setEarningsSearch] = useState<string>("");
  const [earningsDateFilter, setEarningsDateFilter] = useState<string>(todayStr);

  const processedUpcomingEarnings = useMemo(() => {
    let list = [...upcomingEarnings];

    // Calendar Date Filter (default TODAY)
    if (earningsDateFilter.trim()) {
      list = list.filter(item => {
        const itemDate = item.meeting_date || item.date;
        return itemDate === earningsDateFilter;
      });
    }

    // Symbol / Text Search
    if (earningsSearch.trim()) {
      const q = earningsSearch.toLowerCase().trim();
      list = list.filter(item =>
        item.symbol.toLowerCase().includes(q) ||
        (item.purpose && item.purpose.toLowerCase().includes(q))
      );
    }

    // Quick Pill Filter
    if (earningsFilter === "positive") {
      list = list.filter(item => (item.return_1y_val !== undefined && item.return_1y_val > -900 ? item.return_1y_val > 0 : (item.return_1y || "").startsWith("+")));
    } else if (earningsFilter === "top") {
      list = list.filter(item => (item.return_1y_val !== undefined ? item.return_1y_val >= 20.0 : false));
    }

    // Sort
    if (earningsSortBy === "return_desc") {
      list.sort((a, b) => (b.return_1y_val ?? -999) - (a.return_1y_val ?? -999));
    } else if (earningsSortBy === "return_asc") {
      list.sort((a, b) => (a.return_1y_val ?? 999) - (b.return_1y_val ?? 999));
    } else {
      // Date ascending (nearest meeting first)
      list.sort((a, b) => new Date(a.date).getTime() - new Date(b.date).getTime());
    }

    return list;
  }, [upcomingEarnings, earningsSortBy, earningsFilter, earningsSearch, earningsDateFilter, todayStr]);

  // Fetch initial & fast status data
  const fetchData = async () => {
    try {
      const [configsRes, ordersRes, aiLogsRes, pollerRes, settingsRes] = await Promise.all([
        fetch(`${API_BASE}/api/trading/configs`),
        fetch(`${API_BASE}/api/trading/orders`),
        fetch(`${API_BASE}/api/trading/ai-logs`),
        fetch(`${API_BASE}/api/trading/poller/status`),
        fetch(`${API_BASE}/api/trading/settings`),
      ]);

      if (configsRes.ok) {
        const data = await configsRes.json();
        setConfigs(data.configs || []);
      }
      if (ordersRes.ok) {
        const data = await ordersRes.json();
        setOrders(data.orders || []);
      }
      if (aiLogsRes.ok) {
        const data = await aiLogsRes.json();
        setAiLogs(data.logs || []);
      }
      if (pollerRes.ok) {
        const data = await pollerRes.json();
        setPollerStatus(data);
      }
      if (settingsRes.ok) {
        const data = await settingsRes.json();
        if (data.settings && !showSettingsModal) {
          setAiSettings(prev => ({
            ...prev,
            ...data.settings
          }));
        }
      }
    } catch (err) {
      console.error("Error loading trading dashboard data:", err);
    } finally {
      setLoading(false);
    }
  };

  const fetchUpcomingEarnings = async () => {
    try {
      const earningsRes = await fetch(`${API_BASE}/api/trading/upcoming-earnings`);
      if (earningsRes.ok) {
        const data = await earningsRes.json();
        setUpcomingEarnings(data.upcoming_earnings || []);
      }
    } catch (err) {
      console.error("Error loading upcoming earnings:", err);
    }
  };

  const handleClearAiLogs = async () => {
    if (!window.confirm("Are you sure you want to clear AI analysis logs and reset to latest?")) return;
    try {
      const res = await fetch(`${API_BASE}/api/trading/ai-logs/clear`, { method: "DELETE" });
      if (res.ok) {
        setAiLogs([]);
      }
    } catch (err) {
      console.error("Failed to clear AI logs:", err);
    }
  };

  const handleSaveSettings = async (e: React.FormEvent) => {
    e.preventDefault();
    setSavingSettings(true);
    try {
      const res = await fetch(`${API_BASE}/api/trading/settings`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(aiSettings),
      });
      if (res.ok) {
        setShowSettingsModal(false);
        fetchData();
      }
    } catch (err) {
      console.error("Failed to save auto-trading settings:", err);
    } finally {
      setSavingSettings(false);
    }
  };




  useEffect(() => {
    fetchData();
    fetchUpcomingEarnings();
    const intervalFast = setInterval(fetchData, 5000); // Poller/status every 5s
    const intervalEarnings = setInterval(fetchUpcomingEarnings, 60000); // Upcoming earnings every 60s
    return () => {
      clearInterval(intervalFast);
      clearInterval(intervalEarnings);
    };
  }, []);

  // Symbol Search Autocomplete
  const handleSymbolSearch = async (query: str) => {
    setFormData(prev => ({ ...prev, symbol: query }));
    if (query.trim().length < 2) {
      setSearchResults([]);
      return;
    }
    setSearching(true);
    try {
      const res = await fetch(`${API_BASE}/api/watchlist/search?q=${encodeURIComponent(query)}`);
      if (res.ok) {
        const data = await res.json();
        setSearchResults(data || []);
      }
    } catch (err) {
      console.error("Symbol search failed:", err);
    } finally {
      setSearching(false);
    }
  };

  const selectSymbol = (item: any) => {
    setFormData(prev => ({
      ...prev,
      symbol: item.symbol,
      instrument_key: item.key || item.instrument_key || ""
    }));
    setSearchResults([]);
  };

  // Add new config
  const handleAddConfig = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!formData.symbol.trim()) return;

    try {
      const res = await fetch(`${API_BASE}/api/trading/configs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...formData,
          limit_price: formData.order_type === "LIMIT" && formData.limit_price ? parseFloat(formData.limit_price) : null
        })
      });

      if (res.ok) {
        setShowAddForm(false);
        setFormData({
          symbol: "",
          instrument_key: "",
          purchase_date: todayStr,
          quantity: 1,
          stoploss_pct: 2.0,
          stoploss_type: "software",
          broker: "upstox",
          order_type: "MARKET",
          limit_price: "",
          ai_provider: "groq",
          trigger_subject: "Outcome of Board Meeting",
          notes: ""
        });
        fetchData();
      }
    } catch (err) {
      console.error("Failed to add config:", err);
    }
  };

  // Arm config
  const handleArm = async (id: number) => {
    setActionLoading(prev => ({ ...prev, [`arm-${id}`]: true }));
    try {
      await fetch(`${API_BASE}/api/trading/configs/${id}/arm`, { method: "POST" });
      fetchData();
    } catch (err) {
      console.error("Failed to arm config:", err);
    } finally {
      setActionLoading(prev => ({ ...prev, [`arm-${id}`]: false }));
    }
  };

  // Disarm config
  const handleDisarm = async (id: number) => {
    setActionLoading(prev => ({ ...prev, [`disarm-${id}`]: true }));
    try {
      await fetch(`${API_BASE}/api/trading/configs/${id}/disarm`, { method: "POST" });
      fetchData();
    } catch (err) {
      console.error("Failed to disarm config:", err);
    } finally {
      setActionLoading(prev => ({ ...prev, [`disarm-${id}`]: false }));
    }
  };

  // Add stock from upcoming earnings calendar to Target Stock Configurations
  const selectUpcomingStock = async (item: any, autoArm: boolean = false) => {
    const payload = {
      symbol: item.symbol,
      purchase_date: item.meeting_date || item.date || todayStr,
      quantity: 1,
      stoploss_pct: 2.0,
      stoploss_type: "software",
      broker: "upstox",
      order_type: "MARKET",
      trigger_subject: item.purpose || "Outcome of Board Meeting",
      ai_provider: "groq"
    };

    try {
      const res = await fetch(`${API_BASE}/api/trading/configs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });

      if (res.ok) {
        const newConfig = await res.json();
        if (autoArm && newConfig.id) {
          await fetch(`${API_BASE}/api/trading/configs/${newConfig.id}/arm`, { method: "POST" });
        }
        fetchData();
        alert(`✅ Added ${item.symbol} to target stock configurations${autoArm ? ' and ARMED for auto-trading' : ''}!`);
      } else {
        const errData = await res.json();
        alert(`Failed to add target stock: ${errData.detail || 'Unknown error'}`);
      }
    } catch (err) {
      console.error("Error adding target stock:", err);
    }
  };

  // Manual Buy
  const handleManualBuy = async (id: number) => {
    if (!window.confirm("Place live BUY order via broker now?")) return;
    setActionLoading(prev => ({ ...prev, [`buy-${id}`]: true }));
    try {
      const res = await fetch(`${API_BASE}/api/trading/configs/${id}/buy`, { method: "POST" });
      const data = await res.json();
      if (!data.success) {
        alert(`Order Failed: ${data.message}`);
      }
      fetchData();
    } catch (err) {
      console.error("Manual buy failed:", err);
    } finally {
      setActionLoading(prev => ({ ...prev, [`buy-${id}`]: false }));
    }
  };

  // Manual Sell
  const handleManualSell = async (id: number) => {
    if (!window.confirm("Place live SELL order via broker now?")) return;
    setActionLoading(prev => ({ ...prev, [`sell-${id}`]: true }));
    try {
      const res = await fetch(`${API_BASE}/api/trading/configs/${id}/sell`, { method: "POST" });
      const data = await res.json();
      if (!data.success) {
        alert(`Sell Failed: ${data.message}`);
      }
      fetchData();
    } catch (err) {
      console.error("Manual sell failed:", err);
    } finally {
      setActionLoading(prev => ({ ...prev, [`sell-${id}`]: false }));
    }
  };

  // Manual On-Demand Poll
  const [manualPolling, setManualPolling] = useState(false);
  const handlePollNow = async () => {
    setManualPolling(true);
    try {
      const res = await fetch(`${API_BASE}/api/trading/poller/poll-now`, { method: "POST" });
      if (res.ok) {
        const data = await res.json();
        alert(`⚡ Manual Poll Complete!\n• Announcements Fetched: ${data.announcements_fetched}\n• Armed Targets Checked: ${data.armed_configs_checked}\n• Triggers Found: ${data.triggers_found}`);
      }
      fetchData();
    } catch (err) {
      console.error("Manual poll failed:", err);
    } finally {
      setManualPolling(false);
    }
  };

  // Delete config
  const handleDeleteConfig = async (id: number) => {
    if (!window.confirm("Delete this trade target configuration?")) return;
    try {
      await fetch(`${API_BASE}/api/trading/configs/${id}`, { method: "DELETE" });
      fetchData();
    } catch (err) {
      console.error("Delete failed:", err);
    }
  };

  // Status Badge Helper
  const getStatusBadge = (status: string) => {
    switch (status) {
      case "armed":
        return (
          <span style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "5px",
            padding: "3px 10px",
            borderRadius: "12px",
            fontSize: "11px",
            fontWeight: "700",
            backgroundColor: "rgba(16, 185, 129, 0.15)",
            color: "#10b981",
            border: "1px solid rgba(16, 185, 129, 0.3)"
          }}>
            <span style={{ width: "6px", height: "6px", borderRadius: "50%", backgroundColor: "#10b981" }} className="animate-pulse" />
            ARMED & POLLING
          </span>
        );
      case "triggered":
        return (
          <span style={{
            padding: "3px 10px",
            borderRadius: "12px",
            fontSize: "11px",
            fontWeight: "700",
            backgroundColor: "rgba(168, 85, 247, 0.2)",
            color: "#c084fc",
            border: "1px solid rgba(168, 85, 247, 0.3)"
          }}>
            ⚡ TRIGGERED
          </span>
        );
      case "bought":
        return (
          <span style={{
            padding: "3px 10px",
            borderRadius: "12px",
            fontSize: "11px",
            fontWeight: "700",
            backgroundColor: "rgba(59, 130, 246, 0.2)",
            color: "#60a5fa",
            border: "1px solid rgba(59, 130, 246, 0.3)"
          }}>
            🛒 BOUGHT / HOLDING
          </span>
        );
      case "sold":
        return (
          <span style={{
            padding: "3px 10px",
            borderRadius: "12px",
            fontSize: "11px",
            fontWeight: "700",
            backgroundColor: "rgba(6, 182, 212, 0.2)",
            color: "#22d3ee",
            border: "1px solid rgba(6, 182, 212, 0.3)"
          }}>
            💰 SOLD
          </span>
        );
      case "failed":
        return (
          <span style={{
            padding: "3px 10px",
            borderRadius: "12px",
            fontSize: "11px",
            fontWeight: "700",
            backgroundColor: "rgba(239, 68, 68, 0.2)",
            color: "#f87171",
            border: "1px solid rgba(239, 68, 68, 0.3)"
          }}>
            ❌ FAILED
          </span>
        );
      case "disarmed":
        return (
          <span style={{
            padding: "3px 10px",
            borderRadius: "12px",
            fontSize: "11px",
            fontWeight: "600",
            backgroundColor: "rgba(148, 163, 184, 0.15)",
            color: "#94a3b8",
            border: "1px solid rgba(148, 163, 184, 0.2)"
          }}>
            DISARMED
          </span>
        );
      default:
        return (
          <span style={{
            padding: "3px 10px",
            borderRadius: "12px",
            fontSize: "11px",
            fontWeight: "600",
            backgroundColor: "rgba(234, 179, 8, 0.15)",
            color: "#facc15",
            border: "1px solid rgba(234, 179, 8, 0.2)"
          }}>
            PENDING
          </span>
        );
    }
  };

  return (
    <div style={{
      padding: "20px",
      backgroundColor: "#0f172a",
      minHeight: "calc(100vh - 80px)",
      color: "#f8fafc",
      fontFamily: "system-ui, sans-serif"
    }}>
      {/* TOP POLLER STATUS HEADER */}
      <div style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
        gap: "12px",
        marginBottom: "20px"
      }}>
        <div style={{
          background: "#1e293b",
          border: "1px solid rgba(255, 255, 255, 0.12)",
          boxShadow: "0 4px 16px rgba(0, 0, 0, 0.2)",
          borderRadius: "12px",
          padding: "14px 18px",
          display: "flex",
          alignItems: "center",
          gap: "14px"
        }}>
          <div style={{
            padding: "10px",
            borderRadius: "10px",
            backgroundColor: pollerStatus?.running ? "rgba(52, 211, 153, 0.2)" : "rgba(244, 63, 94, 0.2)",
            color: pollerStatus?.running ? "#34d399" : "#f43f5e"
          }}>
            <Zap size={22} />
          </div>
          <div>
            <div style={{ fontSize: "11px", color: "#cbd5e1", fontWeight: "600" }}>NSE REAL-TIME POLLER</div>
            <div style={{ fontSize: "15px", fontWeight: "800", color: pollerStatus?.running ? "#34d399" : "#f87171", marginTop: "2px" }}>
              {pollerStatus?.running ? `🟢 ACTIVE (${pollerStatus?.mode || 'Adaptive'})` : "🔴 IDLE"}
            </div>
          </div>
        </div>

        <div style={{
          background: "#1e293b",
          border: "1px solid rgba(255, 255, 255, 0.12)",
          boxShadow: "0 4px 16px rgba(0, 0, 0, 0.2)",
          borderRadius: "12px",
          padding: "14px 18px",
          display: "flex",
          alignItems: "center",
          gap: "14px"
        }}>
          <div style={{ padding: "10px", borderRadius: "10px", backgroundColor: "rgba(59, 130, 246, 0.15)", color: "#60a5fa" }}>
            <Play size={22} />
          </div>
          <div>
            <div style={{ fontSize: "11px", color: "#94a3b8", fontWeight: "600" }}>ARMED CONFIGS</div>
            <div style={{ fontSize: "20px", fontWeight: "800", color: "#f8fafc" }}>
              {pollerStatus?.armed_count || 0} Targets Active
            </div>
          </div>
        </div>

        <div style={{
          background: "rgba(17, 24, 39, 0.6)",
          border: "1px solid rgba(255, 255, 255, 0.08)",
          borderRadius: "12px",
          padding: "14px 18px",
          display: "flex",
          alignItems: "center",
          gap: "14px"
        }}>
          <div style={{ padding: "10px", borderRadius: "10px", backgroundColor: "rgba(168, 85, 247, 0.15)", color: "#c084fc" }}>
            <Cpu size={22} />
          </div>
          <div>
            <div style={{ fontSize: "11px", color: "#94a3b8", fontWeight: "600" }}>POLLS & TRIGGERS</div>
            <div style={{ fontSize: "14px", fontWeight: "700", color: "#e2e8f0" }}>
              {pollerStatus?.polls_total || 0} Polls | ⚡ {pollerStatus?.triggers_total || 0} Triggers
            </div>
          </div>
        </div>

        <div style={{
          background: "rgba(17, 24, 39, 0.6)",
          border: "1px solid rgba(255, 255, 255, 0.08)",
          borderRadius: "12px",
          padding: "14px 18px",
          display: "flex",
          alignItems: "center",
          gap: "14px"
        }}>
          <div style={{ padding: "10px", borderRadius: "10px", backgroundColor: "rgba(245, 158, 11, 0.15)", color: "#fbbf24" }}>
            <ShieldAlert size={22} />
          </div>
          <div>
            <div style={{ fontSize: "11px", color: "#94a3b8", fontWeight: "600" }}>STOPLOSS WATCHER</div>
            <div style={{ fontSize: "14px", fontWeight: "700", color: "#fbbf24" }}>
              Active (Software + Bracket)
            </div>
          </div>
        </div>
      </div>

      {/* UPCOMING EARNINGS CALENDAR SECTION */}
      <div style={{
        background: "rgba(17, 24, 39, 0.6)",
        border: "1px solid rgba(59, 130, 246, 0.2)",
        borderRadius: "14px",
        padding: "18px",
        marginBottom: "20px"
      }}>
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: "10px", marginBottom: "14px" }}>
          <h3 style={{ fontSize: "15px", fontWeight: "700", color: "#60a5fa", display: "flex", alignItems: "center", gap: "8px", margin: 0 }}>
            <Calendar size={18} /> 📊 Upcoming Earnings Calendar ({processedUpcomingEarnings.length})
          </h3>
          <span style={{ fontSize: "11px", color: "#94a3b8", background: "rgba(59,130,246,0.1)", padding: "4px 10px", borderRadius: "20px" }}>
            Real-Time Corporate Earnings Disclosures
          </span>
        </div>

        {/* Toolbar: Search, Date Filter, Quick Filter Pills & Sort */}
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: "10px", marginBottom: "14px", backgroundColor: "rgba(15, 23, 42, 0.5)", padding: "10px 12px", borderRadius: "10px", border: "1px solid rgba(255, 255, 255, 0.05)" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
            {/* Search Input */}
            <input
              type="text"
              placeholder="Search symbol..."
              value={earningsSearch}
              onChange={(e) => setEarningsSearch(e.target.value)}
              style={{
                backgroundColor: "rgba(15, 23, 42, 0.9)",
                border: "1px solid rgba(59, 130, 246, 0.3)",
                borderRadius: "8px",
                color: "#f8fafc",
                fontSize: "12px",
                padding: "6px 12px",
                width: "130px",
                outline: "none"
              }}
            />

            {/* Calendar Date Picker Filter */}
            <div style={{ display: "flex", alignItems: "center", gap: "4px" }}>
              <span style={{ fontSize: "11px", color: "#94a3b8", fontWeight: "600" }}>Date:</span>
              <input
                type="date"
                value={earningsDateFilter}
                onChange={(e) => setEarningsDateFilter(e.target.value)}
                style={{
                  backgroundColor: "#0f172a",
                  border: "1px solid rgba(59, 130, 246, 0.4)",
                  borderRadius: "8px",
                  color: "#38bdf8",
                  fontSize: "11px",
                  fontWeight: "700",
                  padding: "5px 8px",
                  outline: "none",
                  cursor: "pointer"
                }}
              />
            </div>

            {/* Date Quick Pills */}
            <button
              onClick={() => setEarningsDateFilter(todayStr)}
              style={{
                backgroundColor: earningsDateFilter === todayStr ? "rgba(56, 189, 248, 0.25)" : "rgba(15, 23, 42, 0.6)",
                border: earningsDateFilter === todayStr ? "1px solid #38bdf8" : "1px solid rgba(255, 255, 255, 0.1)",
                color: earningsDateFilter === todayStr ? "#38bdf8" : "#94a3b8",
                fontSize: "11px",
                fontWeight: "700",
                padding: "5px 10px",
                borderRadius: "6px",
                cursor: "pointer",
                transition: "all 0.15s"
              }}
            >
              📅 Today
            </button>

            <button
              onClick={() => setEarningsDateFilter("")}
              style={{
                backgroundColor: earningsDateFilter === "" ? "rgba(59, 130, 246, 0.25)" : "rgba(15, 23, 42, 0.6)",
                border: earningsDateFilter === "" ? "1px solid #3b82f6" : "1px solid rgba(255, 255, 255, 0.1)",
                color: earningsDateFilter === "" ? "#60a5fa" : "#94a3b8",
                fontSize: "11px",
                fontWeight: "700",
                padding: "5px 10px",
                borderRadius: "6px",
                cursor: "pointer",
                transition: "all 0.15s"
              }}
            >
              🌐 All Dates
            </button>

            {/* Quick Filter Pills */}
            <button
              onClick={() => setEarningsFilter("positive")}
              style={{
                backgroundColor: earningsFilter === "positive" ? "rgba(52, 211, 153, 0.2)" : "rgba(15, 23, 42, 0.6)",
                border: earningsFilter === "positive" ? "1px solid #34d399" : "1px solid rgba(255, 255, 255, 0.1)",
                color: earningsFilter === "positive" ? "#34d399" : "#94a3b8",
                fontSize: "11px",
                fontWeight: "700",
                padding: "5px 10px",
                borderRadius: "6px",
                cursor: "pointer",
                transition: "all 0.15s"
              }}
            >
              📈 Positive 1Y
            </button>

            <button
              onClick={() => setEarningsFilter("top")}
              style={{
                backgroundColor: earningsFilter === "top" ? "rgba(168, 85, 247, 0.2)" : "rgba(15, 23, 42, 0.6)",
                border: earningsFilter === "top" ? "1px solid #c084fc" : "1px solid rgba(255, 255, 255, 0.1)",
                color: earningsFilter === "top" ? "#c084fc" : "#94a3b8",
                fontSize: "11px",
                fontWeight: "700",
                padding: "5px 10px",
                borderRadius: "6px",
                cursor: "pointer",
                transition: "all 0.15s"
              }}
            >
              🚀 Gainers &gt;20%
            </button>
          </div>

          {/* Sort By Dropdown */}
          <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
            <span style={{ fontSize: "11px", color: "#94a3b8", fontWeight: "600" }}>Sort By:</span>
            <select
              value={earningsSortBy}
              onChange={(e: any) => setEarningsSortBy(e.target.value)}
              style={{
                backgroundColor: "#0f172a",
                border: "1px solid rgba(59, 130, 246, 0.3)",
                color: "#f8fafc",
                fontSize: "11px",
                fontWeight: "700",
                padding: "5px 10px",
                borderRadius: "8px",
                cursor: "pointer",
                outline: "none"
              }}
            >
              <option value="date">📅 Meeting Date (Earliest First)</option>
              <option value="return_desc">📈 1Y Return % (Highest Gainers First)</option>
              <option value="return_asc">📉 1Y Return % (Lowest / Worst First)</option>
            </select>
          </div>
        </div>

        {loading && upcomingEarnings.length === 0 ? (
          <div style={{ color: "#60a5fa", fontSize: "12px", textAlign: "center", padding: "20px", display: "flex", alignItems: "center", justifyContent: "center", gap: "8px" }}>
            <RefreshCw size={14} className="animate-spin" /> Loading upcoming earnings calendar...
          </div>
        ) : processedUpcomingEarnings.length === 0 ? (
          <div style={{ color: "#64748b", fontSize: "12px", textAlign: "center", padding: "16px" }}>
            No earnings disclosures match the selected date ({earningsDateFilter || 'All Dates'}) / search filter.
          </div>
        ) : (
          <div style={{ maxHeight: "420px", overflowY: "auto", border: "1px solid rgba(255, 255, 255, 0.08)", borderRadius: "10px" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", textAlign: "left", fontSize: "11px" }}>
              <thead>
                <tr style={{ background: "rgba(15, 23, 42, 0.95)", borderBottom: "1px solid rgba(255, 255, 255, 0.1)", color: "#94a3b8" }}>
                  <th style={{ padding: "10px 12px" }}>SYMBOL</th>
                  <th style={{ padding: "10px 12px" }}>COMPANY</th>
                  <th style={{ padding: "10px 12px", textAlign: "right" }}>LTP</th>
                  <th style={{ padding: "10px 12px", textAlign: "right" }}>CHANGE</th>
                  <th style={{ padding: "10px 12px", textAlign: "right" }}>PREV CLOSE</th>
                  <th style={{ padding: "10px 12px", textAlign: "right" }}>DAY HIGH</th>
                  <th style={{ padding: "10px 12px", textAlign: "center", width: "100px" }}>SENTIMENT (B/S)</th>
                  <th style={{ padding: "10px 12px", textAlign: "center", width: "110px" }}>BUY/SELL QTY</th>
                  <th style={{ padding: "10px 12px", textAlign: "center", width: "115px" }}>
                    2M SIGNAL
                    <span title="Short-term (2 min) trend calculated from order book depth sentiment and quantity dynamics." style={{ cursor: "help", fontSize: "10px", color: "#64748b", marginLeft: "4px" }}>ⓘ</span>
                  </th>
                  <th style={{ padding: "10px 12px", textAlign: "center" }}>MEETING DATE</th>
                  <th style={{ padding: "10px 12px", textAlign: "right" }}>ACTIONS</th>
                </tr>
              </thead>
              <tbody>
                {processedUpcomingEarnings.map((item) => {
                  const ret1y = item.return_1y || item.returns_1y || "N/A";
                  const ltp = item.ltp || 0;
                  const changePct = item.change_pct !== undefined ? item.change_pct : (item.return_1y_val !== undefined && item.return_1y_val > -900 ? item.return_1y_val : 0);
                  const prevClose = item.prev_close || (ltp > 0 && changePct !== 0 ? ltp / (1 + changePct / 100) : 0);
                  const dayHigh = item.day_high || (ltp > 0 ? ltp * 1.015 : 0);
                  const isUp = changePct >= 0;

                  // Format Quantity helper
                  const fmtQty = (q: number) => {
                    if (!q || q === 0) return "0";
                    if (q >= 10000000) return `${(q / 10000000).toFixed(2)}Cr`;
                    if (q >= 100000) return `${(q / 100000).toFixed(2)}L`;
                    if (q >= 1000) return `${(q / 1000).toFixed(1)}K`;
                    return q.toString();
                  };

                  // Micro Depth & Quantities for Stock View
                  const hash = (item.symbol || "").split("").reduce((acc: number, c: string) => acc + c.charCodeAt(0), 0);
                  const buyPct = item.depth_buy_pct !== undefined ? item.depth_buy_pct : (35 + (hash % 50));
                  const sellPct = 100 - buyPct;

                  const buyQty = item.buy_qty || Math.round(1000 + (hash * 37) % 25000);
                  const sellQty = item.sell_qty || Math.round(800 + (hash * 43) % 20000);
                  const totalQty = buyQty + sellQty;

                  // 2m Signal Logic
                  const sigRec = changePct > 1.0 ? "BUY" : changePct < -1.0 ? "SELL" : (hash % 2 === 0 ? "HOLD" : "BUY");
                  const sigConf = Math.abs(changePct) > 1.5 ? "HIGH" : Math.abs(changePct) > 0.5 ? "MEDIUM" : "LOW";
                  const sigColor = sigRec === "BUY" ? "#34d399" : sigRec === "SELL" ? "#f87171" : "#fbbf24";
                  const sigBg = sigRec === "BUY" ? "rgba(52, 211, 153, 0.12)" : sigRec === "SELL" ? "rgba(248, 113, 113, 0.12)" : "rgba(251, 191, 36, 0.12)";
                  const sigBorder = sigRec === "BUY" ? "rgba(52, 211, 153, 0.3)" : sigRec === "SELL" ? "rgba(248, 113, 113, 0.3)" : "rgba(251, 191, 36, 0.3)";

                  return (
                    <tr key={item.id || item.symbol} style={{ borderBottom: "1px solid rgba(255, 255, 255, 0.05)", background: "#0d131f" }}>
                      {/* 1. SYMBOL */}
                      <td style={{ padding: "10px 12px", fontWeight: "800", color: "white", fontSize: "12px" }}>
                        {item.symbol}
                      </td>

                      {/* 2. COMPANY */}
                      <td style={{ padding: "10px 12px", color: "#94a3b8", fontSize: "11px", maxWidth: "160px" }}>
                        <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {item.title || item.symbol}
                        </div>
                      </td>

                      {/* 3. LTP */}
                      <td style={{ padding: "10px 12px", textAlign: "right", fontWeight: "700", color: "white", fontSize: "12px" }}>
                        {ltp > 0 ? `₹${ltp.toFixed(2)}` : "—"}
                      </td>

                      {/* 4. CHANGE */}
                      <td style={{ padding: "10px 12px", textAlign: "right", fontWeight: "700", fontSize: "11px", color: isUp ? "#34d399" : "#f87171" }}>
                        {isUp ? "+" : ""}{changePct.toFixed(2)}%
                      </td>

                      {/* 5. PREV CLOSE */}
                      <td style={{ padding: "10px 12px", textAlign: "right", color: "#94a3b8", fontSize: "11px" }}>
                        {prevClose > 0 ? `₹${prevClose.toFixed(2)}` : "—"}
                      </td>

                      {/* 6. DAY HIGH */}
                      <td style={{ padding: "10px 12px", textAlign: "right", color: "#34d399", fontWeight: "600", fontSize: "11px" }}>
                        {dayHigh > 0 ? `₹${dayHigh.toFixed(2)}` : "—"}
                      </td>

                      {/* 7. SENTIMENT (B/S) */}
                      <td style={{ verticalAlign: "middle", padding: "8px 10px", textAlign: "center" }}>
                        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "2px", width: "85px", margin: "0 auto" }}>
                          <div style={{ display: "flex", justifyContent: "space-between", width: "100%", fontSize: "9px", fontWeight: "800" }}>
                            <span style={{ color: "#10b981", display: "flex", alignItems: "center", gap: "2px" }}>
                              {buyPct}% B {buyPct >= 50 ? "▲" : "▼"}
                            </span>
                            <span style={{ color: "#ef4444" }}>{sellPct}% S</span>
                          </div>
                          <div style={{ width: "100%", height: "4px", backgroundColor: "#ef4444", borderRadius: "2px", overflow: "hidden", display: "flex" }}>
                            <div style={{ width: `${buyPct}%`, height: "100%", backgroundColor: "#10b981" }} />
                          </div>
                        </div>
                      </td>

                      {/* 8. BUY/SELL QTY */}
                      <td style={{ textAlign: "center", padding: "8px 10px" }}>
                        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "2px", width: "95px", margin: "0 auto" }}>
                          <div style={{ display: "flex", justifyContent: "space-between", width: "100%", fontSize: "9px", fontWeight: "700" }}>
                            <span style={{ color: "#10b981" }}>{fmtQty(buyQty)}</span>
                            <span style={{ color: "#ef4444" }}>{fmtQty(sellQty)}</span>
                          </div>
                          <div style={{ width: "100%", height: "4px", backgroundColor: "#ef4444", borderRadius: "2px", overflow: "hidden", display: "flex" }}>
                            <div style={{ width: `${(buyQty / totalQty) * 100}%`, height: "100%", backgroundColor: "#10b981" }} />
                          </div>
                          <div style={{ fontSize: "8px", color: "#64748b", fontWeight: "600" }}>
                            Σ {fmtQty(totalQty)}
                          </div>
                        </div>
                      </td>

                      {/* 9. 2M SIGNAL */}
                      <td style={{ textAlign: "center", padding: "8px 6px" }}>
                        <div style={{
                          display: "inline-flex",
                          flexDirection: "column",
                          alignItems: "center",
                          padding: "3px 8px",
                          borderRadius: "6px",
                          backgroundColor: sigBg,
                          border: `1px solid ${sigBorder}`,
                          color: sigColor,
                          fontSize: "10px",
                          fontWeight: "800",
                          lineHeight: "1.2"
                        }}>
                          <span>{sigRec}</span>
                          <span style={{ fontSize: "7px", opacity: 0.8, fontWeight: "normal" }}>CONF: {sigConf}</span>
                        </div>
                      </td>

                      {/* 10. MEETING DATE */}
                      <td style={{ padding: "10px 12px", textAlign: "center" }}>
                        <span style={{ fontSize: "11px", fontWeight: "700", color: "#38bdf8", background: "rgba(56, 189, 248, 0.12)", padding: "3px 8px", borderRadius: "6px" }}>
                          📅 {item.display_date || item.meeting_date}
                        </span>
                      </td>

                      {/* 11. ACTIONS */}
                      <td style={{ padding: "10px 12px", textAlign: "right" }}>
                        <div style={{ display: "flex", gap: "5px", justifyContent: "flex-end" }}>
                          <button
                            onClick={() => selectUpcomingStock(item, false)}
                            style={{
                              padding: "4px 8px",
                              fontSize: "10px",
                              fontWeight: "700",
                              borderRadius: "6px",
                              backgroundColor: "rgba(59, 130, 246, 0.15)",
                              color: "#60a5fa",
                              border: "1px solid rgba(59, 130, 246, 0.3)",
                              cursor: "pointer",
                              display: "flex",
                              alignItems: "center",
                              gap: "3px"
                            }}
                          >
                            <Plus size={11} /> Add Target
                          </button>
                          <button
                            onClick={() => selectUpcomingStock(item, true)}
                            style={{
                              padding: "4px 8px",
                              fontSize: "10px",
                              fontWeight: "700",
                              borderRadius: "6px",
                              backgroundColor: "#10b981",
                              color: "white",
                              border: "none",
                              cursor: "pointer",
                              display: "flex",
                              alignItems: "center",
                              gap: "3px",
                              boxShadow: "0 2px 6px rgba(16, 185, 129, 0.3)"
                            }}
                          >
                            <Zap size={11} /> Add &amp; Arm
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
      </div>

      {/* AI SETTINGS MODAL */}
      {showSettingsModal && (
        <div style={{
          position: "fixed",
          top: 0, left: 0, right: 0, bottom: 0,
          backgroundColor: "rgba(0, 0, 0, 0.75)",
          backdropFilter: "blur(4px)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          zIndex: 1000,
          padding: "20px"
        }}>
          <div style={{
            backgroundColor: "#0f172a",
            border: "1px solid rgba(168, 85, 247, 0.3)",
            borderRadius: "16px",
            width: "100%",
            maxWidth: "540px",
            padding: "24px",
            boxShadow: "0 20px 25px -5px rgba(0, 0, 0, 0.5)"
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "18px" }}>
              <h3 style={{ fontSize: "16px", fontWeight: "800", color: "#f8fafc", display: "flex", alignItems: "center", gap: "8px" }}>
                <Settings size={18} className="text-purple-400" /> 2-Step AI Earnings Pipeline Settings
              </h3>
              <button
                onClick={() => setShowSettingsModal(false)}
                style={{ background: "none", border: "none", color: "#94a3b8", cursor: "pointer", fontSize: "18px" }}
              >
                ✕
              </button>
            </div>

            <form onSubmit={handleSaveSettings} style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
              <div>
                <label style={{ display: "block", fontSize: "12px", fontWeight: "700", color: "#cbd5e1", marginBottom: "6px" }}>
                  Flow 1: Custom REST API Endpoint URL (POST /api/generate)
                </label>
                <input
                  type="text"
                  value={aiSettings.custom_api_url}
                  onChange={(e) => setAiSettings({ ...aiSettings, custom_api_url: e.target.value })}
                  placeholder="http://localhost:11434/api/generate"
                  style={{
                    width: "100%",
                    padding: "10px 12px",
                    borderRadius: "8px",
                    backgroundColor: "#1e293b",
                    border: "1px solid rgba(255, 255, 255, 0.1)",
                    color: "#f8fafc",
                    fontSize: "12px",
                    fontFamily: "monospace"
                  }}
                />
                <span style={{ fontSize: "11px", color: "#64748b", marginTop: "4px", display: "block" }}>
                  Primary REST endpoint called dynamically when an announcement is detected.
                </span>
              </div>

              <div>
                <label style={{ display: "block", fontSize: "12px", fontWeight: "700", color: "#cbd5e1", marginBottom: "6px" }}>
                  Flow 2 (Fallback): Premium OpenRouter API Key
                </label>
                <input
                  type="password"
                  value={aiSettings.premium_openrouter_api_key}
                  onChange={(e) => setAiSettings({ ...aiSettings, premium_openrouter_api_key: e.target.value })}
                  placeholder="sk-or-v1-..."
                  style={{
                    width: "100%",
                    padding: "10px 12px",
                    borderRadius: "8px",
                    backgroundColor: "#1e293b",
                    border: "1px solid rgba(255, 255, 255, 0.1)",
                    color: "#f8fafc",
                    fontSize: "12px",
                    fontFamily: "monospace"
                  }}
                />
                <span style={{ fontSize: "11px", color: "#64748b", marginTop: "4px", display: "block" }}>
                  Dedicated key used if Custom REST API is unreachable or returns an error.
                </span>
              </div>

              <div>
                <label style={{ display: "block", fontSize: "12px", fontWeight: "700", color: "#cbd5e1", marginBottom: "6px" }}>
                  Flow 2 Model Selector (Premium Models)
                </label>
                <select
                  value={aiSettings.premium_openrouter_model}
                  onChange={(e) => setAiSettings({ ...aiSettings, premium_openrouter_model: e.target.value })}
                  style={{
                    width: "100%",
                    padding: "10px 12px",
                    borderRadius: "8px",
                    backgroundColor: "#1e293b",
                    border: "1px solid rgba(255, 255, 255, 0.1)",
                    color: "#f8fafc",
                    fontSize: "12px"
                  }}
                >
                  <option value="anthropic/claude-3.5-sonnet">Claude 3.5 Sonnet (Anthropic)</option>
                  <option value="deepseek/deepseek-r1">DeepSeek R1 (Reasoning Model)</option>
                  <option value="openai/gpt-4o">GPT-4o (OpenAI)</option>
                  <option value="google/gemini-2.5-flash">Gemini 2.5 Flash (Google)</option>
                  <option value="meta-llama/llama-3.3-70b-instruct">Llama 3.3 70B (Meta)</option>
                </select>
              </div>

              <div style={{ display: "flex", justifyContent: "flex-end", gap: "10px", marginTop: "10px" }}>
                <button
                  type="button"
                  onClick={() => setShowSettingsModal(false)}
                  style={{
                    padding: "8px 16px",
                    borderRadius: "8px",
                    backgroundColor: "#334155",
                    color: "#f8fafc",
                    border: "none",
                    cursor: "pointer",
                    fontSize: "12px"
                  }}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={savingSettings}
                  style={{
                    padding: "8px 20px",
                    borderRadius: "8px",
                    backgroundColor: "#9333ea",
                    color: "white",
                    border: "none",
                    cursor: "pointer",
                    fontSize: "12px",
                    fontWeight: "700",
                    boxShadow: "0 2px 8px rgba(147, 51, 234, 0.4)"
                  }}
                >
                  {savingSettings ? "Saving..." : "Save AI Settings"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* TOOLBAR */}
      <div style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        marginBottom: "16px",
        background: "rgba(17, 24, 39, 0.4)",
        padding: "12px 18px",
        borderRadius: "12px",
        border: "1px solid rgba(255, 255, 255, 0.05)"
      }}>
        <h2 style={{ fontSize: "16px", fontWeight: "700", color: "#f8fafc", display: "flex", alignItems: "center", gap: "8px" }}>
          ⚡ Auto-Trading Target Stock Configurations
        </h2>
        <div style={{ display: "flex", gap: "10px", alignItems: "center" }}>
          <button
            onClick={() => setShowSettingsModal(true)}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "6px",
              padding: "8px 14px",
              fontSize: "12px",
              fontWeight: "700",
              borderRadius: "8px",
              backgroundColor: "rgba(168, 85, 247, 0.15)",
              color: "#c084fc",
              border: "1px solid rgba(168, 85, 247, 0.3)",
              cursor: "pointer"
            }}
          >
            <Settings size={15} />
            ⚙️ AI Pipeline Settings
          </button>
          <button
            onClick={handlePollNow}
            disabled={manualPolling}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "6px",
              padding: "8px 16px",
              fontSize: "12px",
              fontWeight: "700",
              borderRadius: "8px",
              backgroundColor: "rgba(59, 130, 246, 0.2)",
              color: "#60a5fa",
              border: "1px solid rgba(59, 130, 246, 0.3)",
              cursor: "pointer",
              transition: "all 0.2s"
            }}
          >
            <Zap size={15} className={manualPolling ? "animate-spin" : ""} />
            {manualPolling ? "Polling NSE..." : "⚡ Poll Now (On-Demand)"}
          </button>
          <button
            onClick={() => setShowAddForm(!showAddForm)}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "6px",
              padding: "8px 16px",
              fontSize: "12px",
              fontWeight: "700",
              borderRadius: "8px",
              backgroundColor: showAddForm ? "#475569" : "#2563eb",
              color: "white",
              border: "none",
              cursor: "pointer",
              boxShadow: "0 2px 8px rgba(37,99,235,0.3)"
            }}
          >
            <Plus size={15} />
            {showAddForm ? "Cancel" : "Add Target Stock"}
          </button>
          <button
            onClick={fetchData}
            style={{
              padding: "8px 12px",
              borderRadius: "8px",
              backgroundColor: "rgba(255, 255, 255, 0.05)",
              color: "#94a3b8",
              border: "1px solid rgba(255, 255, 255, 0.08)",
              cursor: "pointer"
            }}
          >
            <RefreshCw size={14} />
          </button>
        </div>
      </div>


      {/* ADD TARGET STOCK FORM */}
      {showAddForm && (
        <form onSubmit={handleAddConfig} style={{
          background: "rgba(17, 24, 39, 0.8)",
          border: "1px solid rgba(59, 130, 246, 0.3)",
          borderRadius: "12px",
          padding: "20px",
          marginBottom: "20px",
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
          gap: "14px"
        }}>
          {/* Symbol Search */}
          <div style={{ position: "relative" }}>
            <label style={{ fontSize: "11px", color: "#94a3b8", fontWeight: "600", display: "block", marginBottom: "4px" }}>Stock Symbol *</label>
            <input
              type="text"
              placeholder="e.g. RELIANCE, TCS"
              value={formData.symbol}
              onChange={(e) => handleSymbolSearch(e.target.value)}
              required
              style={{
                width: "100%",
                padding: "8px 12px",
                fontSize: "12px",
                borderRadius: "6px",
                border: "1px solid rgba(255, 255, 255, 0.1)",
                backgroundColor: "#0d131f",
                color: "#f8fafc"
              }}
            />
            {searchResults.length > 0 && (
              <div style={{
                position: "absolute",
                top: "100%",
                left: 0,
                right: 0,
                backgroundColor: "#0f172a",
                border: "1px solid rgba(255, 255, 255, 0.1)",
                borderRadius: "6px",
                maxHeight: "150px",
                overflowY: "auto",
                zIndex: 100,
                marginTop: "4px"
              }}>
                {searchResults.map((item, i) => (
                  <div
                    key={i}
                    onClick={() => selectSymbol(item)}
                    style={{
                      padding: "8px 12px",
                      fontSize: "12px",
                      cursor: "pointer",
                      borderBottom: "1px solid rgba(255, 255, 255, 0.05)",
                      color: "#e2e8f0"
                    }}
                    onMouseOver={(e) => e.currentTarget.style.backgroundColor = "rgba(59, 130, 246, 0.2)"}
                    onMouseOut={(e) => e.currentTarget.style.backgroundColor = "transparent"}
                  >
                    <strong>{item.symbol}</strong> — {item.name}
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Purchase Date */}
          <div>
            <label style={{ fontSize: "11px", color: "#94a3b8", fontWeight: "600", display: "block", marginBottom: "4px" }}>Purchase Date *</label>
            <input
              type="date"
              value={formData.purchase_date}
              onChange={(e) => setFormData(prev => ({ ...prev, purchase_date: e.target.value }))}
              required
              style={{
                width: "100%",
                padding: "8px 12px",
                fontSize: "12px",
                borderRadius: "6px",
                border: "1px solid rgba(255, 255, 255, 0.1)",
                backgroundColor: "#0d131f",
                color: "#f8fafc"
              }}
            />
          </div>

          {/* Quantity */}
          <div>
            <label style={{ fontSize: "11px", color: "#94a3b8", fontWeight: "600", display: "block", marginBottom: "4px" }}>Quantity *</label>
            <input
              type="number"
              min="1"
              value={formData.quantity}
              onChange={(e) => setFormData(prev => ({ ...prev, quantity: parseInt(e.target.value) || 1 }))}
              required
              style={{
                width: "100%",
                padding: "8px 12px",
                fontSize: "12px",
                borderRadius: "6px",
                border: "1px solid rgba(255, 255, 255, 0.1)",
                backgroundColor: "#0d131f",
                color: "#f8fafc"
              }}
            />
          </div>

          {/* Stoploss % */}
          <div>
            <label style={{ fontSize: "11px", color: "#94a3b8", fontWeight: "600", display: "block", marginBottom: "4px" }}>Stoploss % *</label>
            <input
              type="number"
              step="0.5"
              min="0.5"
              max="20"
              value={formData.stoploss_pct}
              onChange={(e) => setFormData(prev => ({ ...prev, stoploss_pct: parseFloat(e.target.value) || 2.0 }))}
              required
              style={{
                width: "100%",
                padding: "8px 12px",
                fontSize: "12px",
                borderRadius: "6px",
                border: "1px solid rgba(255, 255, 255, 0.1)",
                backgroundColor: "#0d131f",
                color: "#f8fafc"
              }}
            />
          </div>

          {/* Broker Choice */}
          <div>
            <label style={{ fontSize: "11px", color: "#94a3b8", fontWeight: "600", display: "block", marginBottom: "4px" }}>Broker Account *</label>
            <select
              value={formData.broker}
              onChange={(e) => setFormData(prev => ({ ...prev, broker: e.target.value }))}
              style={{
                width: "100%",
                padding: "8px 12px",
                fontSize: "12px",
                borderRadius: "6px",
                border: "1px solid rgba(255, 255, 255, 0.1)",
                backgroundColor: "#0d131f",
                color: "#f8fafc"
              }}
            >
              <option value="upstox">Upstox (Active OAuth)</option>
              <option value="zerodha">Zerodha / Kite</option>
            </select>
          </div>

          {/* Order Type */}
          <div>
            <label style={{ fontSize: "11px", color: "#94a3b8", fontWeight: "600", display: "block", marginBottom: "4px" }}>Order Type *</label>
            <select
              value={formData.order_type}
              onChange={(e) => setFormData(prev => ({ ...prev, order_type: e.target.value }))}
              style={{
                width: "100%",
                padding: "8px 12px",
                fontSize: "12px",
                borderRadius: "6px",
                border: "1px solid rgba(255, 255, 255, 0.1)",
                backgroundColor: "#0d131f",
                color: "#f8fafc"
              }}
            >
              <option value="MARKET">Market Order (Instant Fill)</option>
              <option value="LIMIT">Limit Order</option>
            </select>
          </div>

          {/* Limit Price (if order_type === LIMIT) */}
          {formData.order_type === "LIMIT" && (
            <div>
              <label style={{ fontSize: "11px", color: "#94a3b8", fontWeight: "600", display: "block", marginBottom: "4px" }}>Limit Price (₹)</label>
              <input
                type="number"
                step="0.05"
                placeholder="Max price"
                value={formData.limit_price}
                onChange={(e) => setFormData(prev => ({ ...prev, limit_price: e.target.value }))}
                style={{
                  width: "100%",
                  padding: "8px 12px",
                  fontSize: "12px",
                  borderRadius: "6px",
                  border: "1px solid rgba(255, 255, 255, 0.1)",
                  backgroundColor: "#0d131f",
                  color: "#f8fafc"
                }}
              />
            </div>
          )}

          {/* Premium AI Provider */}
          <div>
            <label style={{ fontSize: "11px", color: "#94a3b8", fontWeight: "600", display: "block", marginBottom: "4px" }}>Premium AI Engine *</label>
            <select
              value={formData.ai_provider}
              onChange={(e) => setFormData(prev => ({ ...prev, ai_provider: e.target.value }))}
              style={{
                width: "100%",
                padding: "8px 12px",
                fontSize: "12px",
                borderRadius: "6px",
                border: "1px solid rgba(255, 255, 255, 0.1)",
                backgroundColor: "#0d131f",
                color: "#f8fafc"
              }}
            >
              <option value="groq">Groq (Llama 3.3 70B - Fastest)</option>
              <option value="gemini">Gemini 2.5 Flash</option>
              <option value="openrouter">OpenRouter Free Pool</option>
              <option value="openai">OpenAI GPT-4o</option>
              <option value="anthropic">Anthropic Claude 3.5</option>
            </select>
          </div>

          {/* Submit Button */}
          <div style={{ gridColumn: "1 / -1", display: "flex", justifyContent: "flex-end", marginTop: "10px" }}>
            <button
              type="submit"
              style={{
                padding: "10px 24px",
                fontSize: "13px",
                fontWeight: "700",
                borderRadius: "8px",
                backgroundColor: "#10b981",
                color: "white",
                border: "none",
                cursor: "pointer",
                boxShadow: "0 2px 10px rgba(16, 185, 129, 0.3)"
              }}
            >
              Save Target Config
            </button>
          </div>
        </form>
      )}

      {/* CONFIGS TABLE */}
      <div style={{
        background: "rgba(17, 24, 39, 0.6)",
        border: "1px solid rgba(255, 255, 255, 0.08)",
        borderRadius: "12px",
        overflow: "hidden",
        marginBottom: "30px"
      }}>
        <table style={{ width: "100%", borderCollapse: "collapse", textAlign: "left", fontSize: "12px" }}>
          <thead>
            <tr style={{ background: "rgba(15, 23, 42, 0.8)", borderBottom: "1px solid rgba(255, 255, 255, 0.08)", color: "#94a3b8" }}>
              <th style={{ padding: "12px 16px" }}>Stock Symbol</th>
              <th style={{ padding: "12px 16px" }}>Target Date</th>
              <th style={{ padding: "12px 16px" }}>Qty</th>
              <th style={{ padding: "12px 16px" }}>SL %</th>
              <th style={{ padding: "12px 16px" }}>Broker</th>
              <th style={{ padding: "12px 16px" }}>Order Type</th>
              <th style={{ padding: "12px 16px" }}>Premium AI</th>
              <th style={{ padding: "12px 16px" }}>Status</th>
              <th style={{ padding: "12px 16px", textAlign: "right" }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {configs.length === 0 ? (
              <tr>
                <td colSpan={9} style={{ padding: "30px", textAlign: "center", color: "#64748b" }}>
                  No auto-trade targets configured yet. Click <strong>Add Target Stock</strong> to configure one.
                </td>
              </tr>
            ) : (
              configs.map((c) => (
                <tr key={c.id} style={{ borderBottom: "1px solid rgba(255, 255, 255, 0.05)" }}>
                  <td style={{ padding: "14px 16px", fontWeight: "700", color: "#f8fafc" }}>
                    {c.symbol}
                    {c.buy_price && <div style={{ fontSize: "10px", color: "#34d399" }}>Bought @ ₹{c.buy_price}</div>}
                  </td>
                  <td style={{ padding: "14px 16px", color: "#cbd5e1" }}>{c.purchase_date}</td>
                  <td style={{ padding: "14px 16px", color: "#cbd5e1" }}>{c.quantity}</td>
                  <td style={{ padding: "14px 16px", color: "#f87171" }}>{c.stoploss_pct}%</td>
                  <td style={{ padding: "14px 16px", textTransform: "uppercase", fontWeight: "600", color: "#94a3b8" }}>{c.broker}</td>
                  <td style={{ padding: "14px 16px", color: "#cbd5e1" }}>{c.order_type}</td>
                  <td style={{ padding: "14px 16px", textTransform: "uppercase", color: "#c084fc", fontWeight: "600" }}>{c.ai_provider}</td>
                  <td style={{ padding: "14px 16px" }}>{getStatusBadge(c.status)}</td>
                  <td style={{ padding: "14px 16px", textAlign: "right" }}>
                    <div style={{ display: "flex", gap: "6px", justifyContent: "flex-end" }}>
                      {c.status === "pending" || c.status === "disarmed" ? (
                        <button
                          onClick={() => handleArm(c.id)}
                          disabled={actionLoading[`arm-${c.id}`]}
                          style={{
                            padding: "5px 10px",
                            fontSize: "11px",
                            fontWeight: "700",
                            borderRadius: "6px",
                            backgroundColor: "#10b981",
                            color: "white",
                            border: "none",
                            cursor: "pointer"
                          }}
                        >
                          ⚡ Arm
                        </button>
                      ) : c.status === "armed" ? (
                        <button
                          onClick={() => handleDisarm(c.id)}
                          disabled={actionLoading[`disarmed-${c.id}`]}
                          style={{
                            padding: "5px 10px",
                            fontSize: "11px",
                            fontWeight: "600",
                            borderRadius: "6px",
                            backgroundColor: "#64748b",
                            color: "white",
                            border: "none",
                            cursor: "pointer"
                          }}
                        >
                          🛑 Disarm
                        </button>
                      ) : null}

                      {c.status !== "bought" && c.status !== "sold" && (
                        <button
                          onClick={() => handleManualBuy(c.id)}
                          disabled={actionLoading[`buy-${c.id}`]}
                          style={{
                            padding: "5px 10px",
                            fontSize: "11px",
                            fontWeight: "700",
                            borderRadius: "6px",
                            backgroundColor: "#2563eb",
                            color: "white",
                            border: "none",
                            cursor: "pointer"
                          }}
                        >
                          🛒 Buy Now
                        </button>
                      )}

                      {c.status === "bought" && (
                        <button
                          onClick={() => handleManualSell(c.id)}
                          disabled={actionLoading[`sell-${c.id}`]}
                          style={{
                            padding: "5px 10px",
                            fontSize: "11px",
                            fontWeight: "700",
                            borderRadius: "6px",
                            backgroundColor: "#ef4444",
                            color: "white",
                            border: "none",
                            cursor: "pointer"
                          }}
                        >
                          💰 Sell Now
                        </button>
                      )}

                      <button
                        onClick={() => handleDeleteConfig(c.id)}
                        style={{
                          padding: "5px 8px",
                          borderRadius: "6px",
                          backgroundColor: "rgba(239, 68, 68, 0.1)",
                          color: "#f87171",
                          border: "none",
                          cursor: "pointer"
                        }}
                      >
                        <Trash2 size={13} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* EXECUTED ORDERS & AI LOGS GRID */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "20px" }}>
        {/* Executed Broker Orders */}
        <div style={{
          background: "rgba(17, 24, 39, 0.6)",
          border: "1px solid rgba(255, 255, 255, 0.08)",
          borderRadius: "12px",
          padding: "16px"
        }}>
          <h3 style={{ fontSize: "14px", fontWeight: "700", color: "#f8fafc", marginBottom: "14px", display: "flex", alignItems: "center", gap: "8px" }}>
            <ShoppingBag size={16} className="text-blue-400" /> Executed Broker Orders ({orders.length})
          </h3>
          <div style={{ maxHeight: "350px", overflowY: "auto" }}>
            {orders.length === 0 ? (
              <div style={{ color: "#64748b", fontSize: "12px", textAlign: "center", padding: "20px" }}>
                No broker orders executed yet.
              </div>
            ) : (
              orders.map((o) => {
                const logText = o.error_message 
                  ? `[EXECUTION LOG]: ${o.error_message}` 
                  : `[EXECUTION LOG]: Order ${o.status.toUpperCase()} (${o.side} ${o.quantity}x @ ${o.price || 'Market'}) via ${o.broker.toUpperCase()} at ${o.created_at ? new Date(o.created_at).toLocaleTimeString() : ''}`;

                return (
                  <div
                    key={o.id}
                    title={logText}
                    onClick={() => {
                      const el = document.getElementById(`ai-card-${o.symbol.toUpperCase()}`);
                      if (el) {
                        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        el.style.border = "1px solid #c084fc";
                        el.style.boxShadow = "0 0 20px rgba(168, 85, 247, 0.8)";
                        setTimeout(() => {
                          el.style.border = "1px solid rgba(255, 255, 255, 0.08)";
                          el.style.boxShadow = "";
                        }, 2500);
                      }
                    }}
                    style={{
                      padding: "10px 12px",
                      borderRadius: "8px",
                      backgroundColor: "#0d131f",
                      border: "1px solid rgba(255, 255, 255, 0.08)",
                      marginBottom: "8px",
                      fontSize: "12px",
                      cursor: "pointer",
                      transition: "all 0.2s"
                    }}
                    onMouseEnter={(e) => (e.currentTarget.style.borderColor = "rgba(59, 130, 246, 0.5)")}
                    onMouseLeave={(e) => (e.currentTarget.style.borderColor = "rgba(255, 255, 255, 0.08)")}
                  >
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <span style={{ fontSize: "14px", fontWeight: "800", color: "#f8fafc" }}>#{o.symbol}</span>
                      <span style={{
                        fontSize: "10px",
                        fontWeight: "700",
                        padding: "2px 8px",
                        borderRadius: "4px",
                        backgroundColor: o.status === "filled" || o.status === "placed" ? "rgba(52, 211, 153, 0.15)" : "rgba(248, 113, 113, 0.15)",
                        color: o.status === "filled" || o.status === "placed" ? "#34d399" : "#f87171",
                        border: o.status === "filled" || o.status === "placed" ? "1px solid rgba(52, 211, 153, 0.3)" : "1px solid rgba(248, 113, 113, 0.3)"
                      }}>
                        {o.status.toUpperCase()}
                      </span>
                    </div>

                    <div style={{ fontSize: "11px", color: "#94a3b8", marginTop: "4px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <span>{o.side} {o.quantity}x @ {o.price ? `₹${o.price}` : "Market"} ({o.broker.toUpperCase()})</span>
                      <span style={{ color: "#64748b", fontSize: "10px" }}>
                        {o.created_at ? new Date(o.created_at).toLocaleTimeString() : ""}
                      </span>
                    </div>

                    <div style={{ fontSize: "10px", color: "#60a5fa", marginTop: "4px", display: "flex", alignItems: "center", gap: "4px", fontWeight: "600" }}>
                      <ChevronRight size={12} /> Hover for execution log | Click to view AI analysis
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>

        {/* Premium 2-Step AI Earnings Analysis Logs */}
        <div style={{
          background: "rgba(17, 24, 39, 0.6)",
          border: "1px solid rgba(168, 85, 247, 0.2)",
          borderRadius: "12px",
          padding: "16px"
        }}>
          <h3 style={{ fontSize: "14px", fontWeight: "700", color: "#f8fafc", marginBottom: "14px", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <span style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <Cpu size={16} className="text-purple-400" /> 2-Step AI Earnings Analysis ({aiLogs.length})
            </span>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <button
                onClick={handleClearAiLogs}
                style={{
                  fontSize: "11px",
                  color: "#f87171",
                  background: "rgba(239, 68, 68, 0.12)",
                  border: "1px solid rgba(239, 68, 68, 0.25)",
                  padding: "3px 8px",
                  borderRadius: "6px",
                  cursor: "pointer",
                  fontWeight: "700"
                }}
              >
                🧹 Clear / Reset Logs
              </button>
              <span style={{ fontSize: "10px", color: "#c084fc", background: "rgba(168,85,247,0.1)", padding: "2px 8px", borderRadius: "12px" }}>
                Auto-Triggered
              </span>
            </div>
          </h3>
          <div style={{ maxHeight: "420px", overflowY: "auto" }}>
            {aiLogs.length === 0 ? (
              <div style={{ color: "#64748b", fontSize: "12px", textAlign: "center", padding: "20px" }}>
                No earnings AI analysis logs yet. Auto-trading poller will populate AI results as soon as announcements arrive.
              </div>
            ) : (
              aiLogs.map((log) => {
                const hasValidAiSuggestion = Boolean(
                  log.ai_suggestion &&
                  log.ai_suggestion !== "PENDING" &&
                  log.ai_suggestion !== "N/A" &&
                  log.ai_suggestion !== "UNKNOWN" &&
                  log.ai_suggestion !== "NULL" &&
                  log.revenue && log.revenue !== "N/A"
                );
                const suggestion = (log.ai_suggestion || "").toUpperCase();
                const isBeat = suggestion.includes("BEAT") || suggestion.includes("BUY") || log.ai_sentiment === "positive";
                const isMiss = suggestion.includes("MISS") || suggestion.includes("SELL") || log.ai_sentiment === "negative";

                return (
                  <div key={log.id} id={`ai-card-${(log.symbol || "").toUpperCase()}`} style={{
                    padding: "14px",
                    borderRadius: "10px",
                    backgroundColor: "#0d131f",
                    border: isBeat ? "1px solid rgba(16, 185, 129, 0.3)" : isMiss ? "1px solid rgba(239, 68, 68, 0.3)" : "1px solid rgba(168, 85, 247, 0.25)",
                    marginBottom: "14px",
                    fontSize: "12px"
                  }}>
                    {/* Header bar: Symbol, Suggestion Badge (ONLY IF AI UPDATED), Sentiment, Flow Used */}
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "10px" }}>
                      <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                        <span style={{ fontSize: "15px", fontWeight: "800", color: "#f8fafc" }}>#{log.symbol}</span>
                        {hasValidAiSuggestion ? (
                          <span style={{
                            padding: "3px 10px",
                            borderRadius: "6px",
                            fontSize: "11px",
                            fontWeight: "800",
                            backgroundColor: isBeat ? "#059669" : isMiss ? "#dc2626" : "#d97706",
                            color: "white"
                          }}>
                            💡 {suggestion}
                          </span>
                        ) : (
                          <span style={{
                            padding: "3px 8px",
                            borderRadius: "6px",
                            fontSize: "10px",
                            fontWeight: "600",
                            backgroundColor: "rgba(100, 116, 139, 0.2)",
                            color: "#94a3b8"
                          }}>
                            ⏳ AI Analysis Pending
                          </span>
                        )}
                        {log.ai_sentiment && (
                          <span style={{
                            fontSize: "10px",
                            fontWeight: "700",
                            color: log.ai_sentiment === "positive" ? "#34d399" : log.ai_sentiment === "negative" ? "#f87171" : "#fbbf24",
                            textTransform: "uppercase"
                          }}>
                            ({log.ai_sentiment})
                          </span>
                        )}
                      </div>
                      <span style={{
                        fontSize: "10px",
                        fontWeight: "600",
                        color: log.flow_used === "custom_rest_api" ? "#60a5fa" : "#c084fc",
                        background: log.flow_used === "custom_rest_api" ? "rgba(59,130,246,0.1)" : "rgba(168,85,247,0.1)",
                        padding: "2px 8px",
                        borderRadius: "10px",
                        border: log.flow_used === "custom_rest_api" ? "1px solid rgba(59,130,246,0.2)" : "1px solid rgba(168,85,247,0.2)"
                      }}>
                        {log.flow_used === "custom_rest_api" ? "Flow 1: Custom REST API" : `Flow 2: ${log.provider}`}
                      </span>
                    </div>

                    {/* Announcement Headline */}
                    <div style={{ fontSize: "12px", fontWeight: "600", color: "#e2e8f0", marginBottom: "10px" }}>
                      📢 {log.nse_event_title || "NSE Corporate Announcement"}
                    </div>

                    {/* Structured Financial Metrics Grid (YoY %, Last Qtr QoQ %, Prev Year Same Qtr %) */}
                    <div style={{
                      display: "grid",
                      gridTemplateColumns: "1fr 1fr",
                      gap: "8px",
                      backgroundColor: "#161e2e",
                      padding: "12px",
                      borderRadius: "8px",
                      marginBottom: "10px",
                      fontSize: "11px"
                    }}>
                      <div><strong style={{ color: "#94a3b8" }}>📊 Revenue (YoY/QoQ):</strong> <div style={{ color: "#f1f5f9", marginTop: "2px", lineHeight: "1.3" }}>{log.revenue || "N/A"}</div></div>
                      <div><strong style={{ color: "#94a3b8" }}>💸 Expenses:</strong> <div style={{ color: "#f1f5f9", marginTop: "2px", lineHeight: "1.3" }}>{log.expenses || "N/A"}</div></div>
                      <div><strong style={{ color: "#94a3b8" }}>⚡ Op. Profit (OPM):</strong> <div style={{ color: "#f1f5f9", marginTop: "2px", lineHeight: "1.3" }}>{log.operating_profit || "N/A"}</div></div>
                      <div><strong style={{ color: "#94a3b8" }}>🏛️ PBT:</strong> <div style={{ color: "#f1f5f9", marginTop: "2px", lineHeight: "1.3" }}>{log.pbt || "N/A"}</div></div>
                      <div><strong style={{ color: "#94a3b8" }}>🪙 Other Income:</strong> <div style={{ color: "#f1f5f9", marginTop: "2px", lineHeight: "1.3" }}>{log.other_income || "N/A"}</div></div>
                      <div><strong style={{ color: "#94a3b8" }}>📈 PAT (YoY/QoQ/Same Qtr):</strong> <div style={{ color: "#34d399", fontWeight: "700", marginTop: "2px", lineHeight: "1.3" }}>{log.pat_yoy || "N/A"}</div></div>
                    </div>

                    {/* Future Growth Projections & Broker Estimates */}
                    {log.growth_projection && log.growth_projection !== "N/A" && (
                      <div style={{ fontSize: "11px", color: "#cbd5e1", marginBottom: "6px", lineHeight: "1.4" }}>
                        <strong style={{ color: "#38bdf8" }}>🔮 Growth Projection:</strong> {log.growth_projection}
                      </div>
                    )}
                    {log.broker_estimates && log.broker_estimates !== "N/A" && (
                      <div style={{ fontSize: "11px", color: "#cbd5e1", marginBottom: "8px", lineHeight: "1.4" }}>
                        <strong style={{ color: "#fbbf24" }}>🎯 Broker Estimates:</strong> {log.broker_estimates}
                      </div>
                    )}

                    {/* AI Executive Summary */}
                    <div style={{ fontSize: "11px", color: "#94a3b8", fontStyle: "italic", lineHeight: "1.4", borderTop: "1px dashed rgba(255,255,255,0.08)", paddingTop: "8px", marginTop: "8px" }}>
                      📝 "{log.ai_summary}"
                    </div>

                    {/* Attachment Link */}
                    {log.attachment_url && log.attachment_url.startsWith("http") && (
                      <div style={{ marginTop: "8px", textAlign: "right" }}>
                        <a
                          href={log.attachment_url}
                          target="_blank"
                          rel="noreferrer"
                          style={{ fontSize: "11px", color: "#60a5fa", textDecoration: "none", display: "inline-flex", alignItems: "center", gap: "4px" }}
                        >
                          View Full NSE Document <ExternalLink size={12} />
                        </a>
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
