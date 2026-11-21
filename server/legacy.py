"""
Legacy passive-monitor support (unchanged behaviour from v1):

    WS /ws         reporters push one status message per laptop per second (shared/schema.json)
    WS /view       3D/AR viewers get {"type":"state","ts","laptops":[...]} at 2 Hz
    POST /summarize  one raw agent log line -> one plain-English line via Mistral Small

The hub merges these reporter laptops with one synthesized entry per connected orchestra
worker (see Hub.legacy_laptops) so web/ar.html also shows the agents the orchestra runs.
"""
from __future__ import annotations

import copy

import httpx


DEVICE_KEYS = ("hostname", "os", "os_family", "os_version", "arch", "vendor", "model", "model_id", "chip")


def clean_device(d) -> dict:
    """Whitelisted, length-capped device info (see shared/device_info.py); {} if absent."""
    if not isinstance(d, dict):
        return {}
    out = {k: " ".join(str(d.get(k) or "").split())[:80] for k in DEVICE_KEYS if d.get(k)}
    return out


class LegacyState:
    """Latest reporter status per laptop (key = tailscale_ip, fallback laptop name)."""

    def __init__(self, stale_after: float) -> None:
        self.stale_after = stale_after
        self.states: dict[str, dict] = {}

    def ingest(self, data: dict) -> None:
        # key by Tailscale IP; fall back to hostname when the IP is missing
        # or just the reporter's loopback fallback
        key = (data.get("tailscale_ip") or "").strip()
        if not key or key == "127.0.0.1":
            key = data.get("laptop") or "unknown"
        if "device" in data:
            data["device"] = clean_device(data.get("device"))
        self.states[key] = data

    def prune(self, now: float) -> None:
        for key in [k for k, s in self.states.items() if now - s.get("ts", 0) > self.stale_after]:
            del self.states[key]

    def fresh(self, now: float) -> list[dict]:
        """Deep copies of all non-stale laptop states (safe to merge into)."""
        return [copy.deepcopy(s) for s in self.states.values() if now - s.get("ts", 0) <= self.stale_after]


async def summarize(last_log: str, api_key: str, model: str) -> str:
    """
    Turn one raw agent log line into a short task title (max 6 words, like a
    chat title) using Mistral Small. Without MISTRAL_API_KEY (or on any API
    error) it returns "" — the UI then shows the reporter's session title or
    just the agent name.
    """
    line = (last_log or "").strip()
    if not line:
        return ""
    if not api_key:
        return ""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "From one log line of an AI coding agent, name the task it is "
                                "working on as a short title (max 6 words), like a chat title. "
                                "No preamble, no quotes, no emoji, no file paths or IDs."
                            ),
                        },
                        {"role": "user", "content": line[:500]},
                    ],
                    "temperature": 0.2,
                },
            )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # never let summarization break ingestion
        print(f"[summarize] fallback after error: {exc}")
        return ""
