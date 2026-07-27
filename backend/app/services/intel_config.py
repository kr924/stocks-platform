"""
Configuration loader for the Intelligence Platform.

Loads intelligence_config.yaml and allows environment variable overrides.
Env var format: INTEL_<SECTION>_<KEY> (e.g., INTEL_POLLING_NEWS_AGGREGATOR=180)
"""
import os
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("app.intel_config")

# Try to import yaml; fall back to a simple parser if not installed
try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False
    logger.warning("PyYAML not installed. Using built-in config defaults.")


def _deep_get(d: dict, keys: list, default=None):
    """Safely traverse nested dicts."""
    for key in keys:
        if isinstance(d, dict):
            d = d.get(key, default)
        else:
            return default
    return d


def _deep_set(d: dict, keys: list, value):
    """Set a value in a nested dict."""
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value


def _apply_env_overrides(config: dict, prefix: str = "INTEL"):
    """
    Override config values with environment variables.
    
    Format: INTEL_SECTION_SUBSECTION_KEY=value
    Example: INTEL_POLLING_NEWS_AGGREGATOR=180
             INTEL_NEWS_SOURCES_NEWSDATA_IO_API_KEY=abc123
             INTEL_NOTIFICATIONS_TELEGRAM_BOT_TOKEN=token
    """
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(f"{prefix}_"):
            continue
        # Convert env key to config path
        parts = env_key[len(prefix) + 1:].lower().split("_")
        
        # Try to convert value to appropriate type
        parsed_val: Any = env_val
        if env_val.lower() in ("true", "yes", "1"):
            parsed_val = True
        elif env_val.lower() in ("false", "no", "0"):
            parsed_val = False
        else:
            try:
                parsed_val = int(env_val)
            except ValueError:
                try:
                    parsed_val = float(env_val)
                except ValueError:
                    pass
        
        _deep_set(config, parts, parsed_val)
    
    # Also check specific well-known env vars
    env_mappings = {
        "NEWSDATA_IO_API_KEY": ["news", "sources", "newsdata_io", "api_key"],
        "TWITTER_BEARER_TOKEN": ["social_media", "twitter", "bearer_token"],
        "TELEGRAM_BOT_TOKEN": ["notifications", "telegram", "bot_token"],
        "TELEGRAM_CHAT_ID": ["notifications", "telegram", "chat_id"],
    }
    for env_key, config_path in env_mappings.items():
        val = os.getenv(env_key)
        if val:
            _deep_set(config, config_path, val)


def _get_defaults() -> dict:
    """Return hardcoded defaults matching the YAML schema."""
    return {
        "general": {
            "monitor_scope": "all",
            "database_url": "sqlite:///./market_tracker.db",
            "log_level": "INFO",
        },
        "polling": {
            "nse_bse_announcements": 150,
            "nse_bse_bulk_deals": 150,
            "nse_bse_board_meetings": 150,
            "nse_bse_insider_trading": 150,
            "news_aggregator": 150,
            "social_media": 150,
            "company_filings": 900,
            "ai_analysis_queue": 120,
            "off_market_multiplier": 4,
        },
        "market_hours": {
            "open": "09:00",
            "close": "16:30",
            "timezone": "Asia/Kolkata",
            "include_pre_market": True,
            "pre_market_open": "08:45",
        },
        "rate_limits": {
            "nse_api": 12,
            "bse_api": 12,
            "google_news_rss": 20,
            "newsdata_io": 5,
            "twitter_api": 10,
            "general_rss": 30,
        },
        "nse_bse": {
            "enabled": True,
            "sources": {
                "corporate_announcements": {
                    "enabled": True,
                    "nse_url": "https://www.nseindia.com/api/corporate-announcements?index=equities",
                    "bse_url": "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w",
                },
                "bulk_block_deals": {
                    "enabled": True,
                    "nse_url": "https://www.nseindia.com/api/snapshot-capital-market-largedeal",
                },
                "board_meetings": {
                    "enabled": True,
                    "nse_url": "https://www.nseindia.com/api/corporate-board-meetings?index=equities",
                },
                "insider_trading": {
                    "enabled": True,
                    "nse_url": "https://www.nseindia.com/api/corporates-pit",
                },
                "financial_results": {
                    "enabled": True,
                    "nse_url": "https://www.nseindia.com/api/corporates-financial-results?index=equities",
                },
            },
            "headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Referer": "https://www.nseindia.com/",
            },
            "cookie_refresh_interval": 300,
        },
        "news": {
            "enabled": True,
            "max_articles_per_source": 20,
            "max_total_articles": 5000,
            "retention_days": 30,
            "sources": {},
        },
        "social_media": {
            "enabled": True,
            "twitter": {
                "enabled": True,
                "method": "rss",
                "bearer_token": "",
                "tracked_accounts": ["ETMarkets", "NDTVProfit"],
                "keywords": ["NSE", "BSE", "Nifty", "Sensex"],
            },
        },
        "filings": {
            "enabled": True,
            "sources": {
                "quarterly_results": {"enabled": True},
                "investor_presentations": {"enabled": True},
                "conference_calls": {"enabled": True, "query": "conference call transcript India stock"},
                "annual_reports": {"enabled": True},
            },
            "pdf": {"enabled": True, "max_pages": 20, "max_file_size_mb": 50},
        },
        "ai": {
            "enabled": True,
            "primary_provider": "groq",
            "fallback_providers": ["gemini", "openai", "anthropic"],
            "batch_size": 10,
            "thresholds": {"alert_threshold": 0.6, "critical_threshold": 0.85},
            "max_prompt_tokens": 4000,
            "cache_hours": 4,
        },
        "notifications": {
            "browser": {"enabled": True, "min_severity": "high"},
            "telegram": {"enabled": False, "bot_token": "", "chat_id": "", "min_severity": "critical"},
        },
        "deduplication": {
            "headline_similarity_threshold": 0.75,
            "time_window_hours": 48,
            "normalize_urls": True,
            "story_clustering": {"enabled": True, "min_articles": 2, "max_gap_hours": 24},
        },
    }


class IntelConfig:
    """Singleton configuration manager for the Intelligence Platform."""
    
    _instance: Optional["IntelConfig"] = None
    _config: Dict[str, Any] = {}
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load()
        return cls._instance
    
    def _load(self):
        """Load config from YAML file, then apply env overrides."""
        config_path = Path(__file__).parent.parent.parent / "intelligence_config.yaml"
        
        if HAS_YAML and config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    self._config = yaml.safe_load(f) or {}
                logger.info(f"Loaded intelligence config from {config_path}")
            except Exception as e:
                logger.error(f"Error loading config YAML: {e}. Using defaults.")
                self._config = _get_defaults()
        else:
            logger.info("Using default intelligence config (no YAML found or PyYAML not installed)")
            self._config = _get_defaults()
        
        # Merge defaults for any missing keys
        defaults = _get_defaults()
        self._merge_defaults(self._config, defaults)
        
        # Apply environment variable overrides
        _apply_env_overrides(self._config)
    
    def _merge_defaults(self, config: dict, defaults: dict):
        """Recursively merge defaults into config for missing keys."""
        for key, default_val in defaults.items():
            if key not in config:
                config[key] = default_val
            elif isinstance(default_val, dict) and isinstance(config.get(key), dict):
                self._merge_defaults(config[key], default_val)
    
    def reload(self):
        """Force-reload the configuration."""
        self._load()
        logger.info("Intelligence config reloaded")
    
    def get(self, *keys, default=None):
        """Get a config value by dot-path keys."""
        return _deep_get(self._config, list(keys), default)
    
    @property
    def polling(self) -> dict:
        return self._config.get("polling", {})
    
    @property
    def rate_limits(self) -> dict:
        return self._config.get("rate_limits", {})
    
    @property
    def nse_bse(self) -> dict:
        return self._config.get("nse_bse", {})
    
    @property
    def news(self) -> dict:
        return self._config.get("news", {})
    
    @property
    def social_media(self) -> dict:
        return self._config.get("social_media", {})
    
    @property
    def filings(self) -> dict:
        return self._config.get("filings", {})
    
    @property
    def ai(self) -> dict:
        return self._config.get("ai", {})
    
    @property
    def notifications(self) -> dict:
        return self._config.get("notifications", {})
    
    @property
    def deduplication(self) -> dict:
        return self._config.get("deduplication", {})
    
    @property
    def market_hours(self) -> dict:
        return self._config.get("market_hours", {})
    
    @property
    def general(self) -> dict:
        return self._config.get("general", {})
    
    def is_source_enabled(self, section: str, source_name: str) -> bool:
        """Check if a specific data source is enabled."""
        section_config = self._config.get(section, {})
        if not section_config.get("enabled", True):
            return False
        sources = section_config.get("sources", {})
        source = sources.get(source_name, {})
        return source.get("enabled", True)
    
    @property
    def raw(self) -> dict:
        """Access the raw config dict."""
        return self._config


def get_intel_config() -> IntelConfig:
    """Get the global IntelConfig instance."""
    return IntelConfig()
