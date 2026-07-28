"""
Thread-safe API Key Pool & Concurrency Manager for LLM Providers.

Maintains in-flight request locks (`is_busy`) and 429 rate-limit backoffs per key.
Dispatch rules:
1. Try to acquire an IDLE/FREE key for the primary provider (e.g. Groq).
2. If all keys for a provider are busy or rate-limited, try the next provider's free key (Gemini / OpenAI / Anthropic).
3. If ALL cloud keys across ALL providers are busy or rate-limited, route request to local Ollama with 30s timeout.
4. If Ollama also fails, fallback to 0-CPU Rule Engine.
"""
import time
import threading
import logging
from typing import List, Optional, Dict

logger = logging.getLogger("app.key_manager")

class KeyState:
    def __init__(self, provider: str, key: str, index: int):
        self.provider = provider
        self.key = key
        self.index = index
        self.is_busy = False
        self.rate_limited_until = 0.0


class ProviderPool:
    def __init__(self, provider: str):
        self.provider = provider
        self.keys: List[KeyState] = []
        self.lock = threading.Lock()

    def sync_keys(self, raw_input: str):
        """Update pool keys from raw input string (comma/newline separated)."""
        if not raw_input or not isinstance(raw_input, str):
            key_strings = []
        else:
            # Strip trailing '>', quotes, and whitespace from terminal copy-paste
            key_strings = [k.strip().rstrip(">").strip('"').strip("'").strip() for k in raw_input.replace("\n", ",").split(",") if k.strip()]
        
        # Filter out placeholder or invalid keys
        valid_keys = []
        for k in key_strings:
            if self.provider == "gemini" and (k.startswith("AQ.") or k.startswith("YOUR_")):
                continue
            if k.startswith("YOUR_") or len(k) < 5:
                continue
            valid_keys.append(k)

        with self.lock:
            existing_map = {k.key: k for k in self.keys}
            new_keys = []
            for idx, k_str in enumerate(valid_keys):
                if k_str in existing_map:
                    # Preserve existing busy / rate-limit state
                    ks = existing_map[k_str]
                    ks.index = idx
                    new_keys.append(ks)
                else:
                    new_keys.append(KeyState(self.provider, k_str, idx))
            self.keys = new_keys

    def acquire_free_key(self) -> Optional[KeyState]:
        """Find an idle key that is not currently busy and not rate-limited."""
        now = time.time()
        with self.lock:
            for ks in self.keys:
                if not ks.is_busy and now >= ks.rate_limited_until:
                    ks.is_busy = True
                    logger.info(f"🔑 [{self.provider.upper()}] Acquired idle Key #{ks.index + 1}")
                    return ks
        return None

    def release_key(self, key_state: KeyState, is_rate_limited: bool = False, backoff_seconds: float = 60.0):
        """Release busy lock on key and optionally mark rate-limited."""
        now = time.time()
        with self.lock:
            key_state.is_busy = False
            if is_rate_limited:
                key_state.rate_limited_until = now + backoff_seconds
                logger.warning(f"⚠️ [{self.provider.upper()}] Key #{key_state.index + 1} marked 429 rate-limited for {backoff_seconds}s")
            else:
                logger.info(f"🔓 [{self.provider.upper()}] Released Key #{key_state.index + 1}")

    def total_keys(self) -> int:
        with self.lock:
            return len(self.keys)


class LLMKeyManager:
    """Global manager singleton for all LLM provider key pools."""
    def __init__(self):
        self.pools: Dict[str, ProviderPool] = {
            "openrouter": ProviderPool("openrouter"),
            "groq": ProviderPool("groq"),
            "gemini": ProviderPool("gemini"),
            "openai": ProviderPool("openai"),
            "anthropic": ProviderPool("anthropic"),
        }

    def sync_all(self, env_vars: dict):
        self.pools["openrouter"].sync_keys(env_vars.get("openrouter_key", ""))
        self.pools["groq"].sync_keys(env_vars.get("groq_key", ""))
        self.pools["gemini"].sync_keys(env_vars.get("gemini_key", ""))
        self.pools["openai"].sync_keys(env_vars.get("openai_key", ""))
        self.pools["anthropic"].sync_keys(env_vars.get("anthropic_key", ""))

    def acquire_key_for_provider(self, provider: str) -> Optional[KeyState]:
        pool = self.pools.get(provider)
        if not pool:
            return None
        return pool.acquire_free_key()

    def release_key(self, key_state: KeyState, is_rate_limited: bool = False, backoff_seconds: float = 60.0):
        pool = self.pools.get(key_state.provider)
        if pool:
            pool.release_key(key_state, is_rate_limited, backoff_seconds)


key_manager = LLMKeyManager()
