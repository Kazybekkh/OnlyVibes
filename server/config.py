"""
Hub settings, read from the environment once at app creation (see main.py for the list).
Tests build Settings directly to point at a temp data dir and shrink timeouts.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: SERVER_DIR / "data")
    web_dir: Path = field(default_factory=lambda: SERVER_DIR.parent / "web")
    public_url: str = ""          # base URL agents use for `ov` (else derived from the worker's Host header)
    shared_secret: str = ""       # if set, WS /worker requires ?secret=<value>
    control_secret: str = ""      # if set, mutating control endpoints require ?key=<value> or X-OV-Key
    persist: bool = True          # tests may disable disk writes
    # orchestration timing (PROTOCOL.md §2/§3)
    offline_after: float = 15.0   # worker offline after this long without any message
    claim_timeout: float = 30.0   # claimed without task_started -> back to todo
    max_attempts: int = 3
    tick_interval: float = 1.0    # scheduler period (plus immediate kicks on change)
    ping_interval: float = 15.0   # WS /board keep-alive
    reattach_grace: float = 10.0  # requeued-by-disconnect tasks wait this long for their worker to re-hello
    restart_grace: float = 30.0   # same after a hub restart (workers may be in reconnect backoff)
    save_debounce: float = 0.5
    frame_flush_interval: float = 0.5  # coalesced task frames after transcript events
    # legacy monitor
    stale_after: float = 5.0
    mistral_api_key: str = ""
    mistral_model: str = "mistral-small-latest"

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        if os.environ.get("OV_DATA_DIR"):
            s.data_dir = Path(os.environ["OV_DATA_DIR"]).expanduser().resolve()
        if os.environ.get("OV_WEB_DIR"):
            s.web_dir = Path(os.environ["OV_WEB_DIR"]).expanduser().resolve()
        s.public_url = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
        s.shared_secret = os.environ.get("OV_SHARED_SECRET", "")
        s.control_secret = os.environ.get("OV_CONTROL_SECRET", "")
        s.stale_after = _float("STALE_AFTER", s.stale_after)
        s.mistral_api_key = os.environ.get("MISTRAL_API_KEY", "")
        s.mistral_model = os.environ.get("MISTRAL_MODEL", s.mistral_model)
        return s
