"""
In-memory AI Log Tracker & SSE Broadcaster.
Stores the last 100 AI activity logs and streams them live to frontend UI.
"""
from collections import deque
from datetime import datetime
import logging
from typing import List, Dict, Any

logger = logging.getLogger("app.ai_log_tracker")

_logs_buffer = deque(maxlen=100)

def record_ai_log(reason: str, provider: str = "groq", key_index: int = 1, tier: str = "standard", level: str = "info", details: str = ""):
    """Record an AI call reason log and broadcast it live to frontend SSE subscribers."""
    now_str = datetime.now().strftime("%H:%M:%S")
    log_entry = {
        "id": f"log_{int(datetime.utcnow().timestamp() * 1000)}",
        "timestamp": now_str,
        "reason": reason,
        "provider": provider,
        "key_index": key_index,
        "tier": tier,
        "level": level,
        "details": details
    }
    
    _logs_buffer.appendleft(log_entry)
    
    # Broadcast to frontend via SSE
    try:
        from app.services.sse_manager import sse_manager
        sse_manager.broadcast("ai_log", log_entry)
    except Exception as e:
        logger.debug(f"Failed to broadcast ai_log SSE: {e}")

def get_recent_ai_logs(limit: int = 50) -> List[Dict[str, Any]]:
    """Return recent AI logs list."""
    return list(_logs_buffer)[:limit]
