#!/usr/bin/env python3
"""
OnlyVibes mock fleet — N fake laptops, each a worker running the mock agent,
all in one process. For demos of the board without any real agent CLI.

Workers get ids like mock-ada, mock-bob, mock-cy … with distinct laptop names,
owners and fake Tailscale IPs, and their own workspaces dir
(<WORKSPACES_DIR>/<worker id>) so they behave like separate machines.

Run:
    cd worker
    SERVER_URL=ws://localhost:8765/worker uv run mock_fleet.py --n 3
    MOCK_SPEED=5 SERVER_URL=ws://localhost:8765/worker uv run mock_fleet.py --n 5 --max-concurrent 2

Env vars:
    SERVER_URL, OV_SHARED_SECRET, WORKSPACES_DIR, AUTO_MERGE   as for worker.py
    MOCK_SPEED                                                 mock agent speed multiplier (default 1)
"""
from __future__ import annotations

import argparse
import asyncio
import os

from worker import Config, Worker, run_workers

NAMES = ["ada", "bob", "cy", "dee", "eve", "fay", "gus", "hal", "ivy", "jo", "kai", "lu"]
LAPTOPS = ["macbook-air", "thinkpad", "macbook-pro", "framework", "xps"]


def fleet_configs(n: int, max_concurrent: int = 1, env: dict | None = None) -> list[Config]:
    env = os.environ if env is None else env
    root = os.path.expanduser(env.get("WORKSPACES_DIR") or "~/.onlyvibes/workspaces")
    configs = []
    for i in range(n):
        name = NAMES[i % len(NAMES)] + (str(i // len(NAMES) + 1) if i >= len(NAMES) else "")
        wid = f"mock-{name}"
        configs.append(Config(
            server_url=env.get("SERVER_URL", "ws://localhost:8000/worker"),
            worker_id=wid,
            laptop=f"{name}-{LAPTOPS[i % len(LAPTOPS)]}",
            owner=name,
            tailscale_ip=f"100.64.0.{10 + i}",
            agents=["mock"],
            max_concurrent=max_concurrent,
            workspaces_dir=os.path.join(root, wid),
            auto_merge=(env.get("AUTO_MERGE", "1").strip().lower() not in ("0", "false", "no", "off")),
            shared_secret=env.get("OV_SHARED_SECRET", ""),
            label=wid,
        ))
    return configs


def main() -> None:
    p = argparse.ArgumentParser(description="Run N mock OnlyVibes workers in one process.")
    p.add_argument("--n", type=int, default=3, help="number of fake laptops (default 3)")
    p.add_argument("--max-concurrent", type=int, default=1, help="tasks per fake laptop (default 1)")
    args = p.parse_args()
    workers = [Worker(c) for c in fleet_configs(max(1, args.n), max(1, args.max_concurrent))]
    print(f"[fleet] {len(workers)} mock workers: {', '.join(w.cfg.worker_id for w in workers)}", flush=True)
    asyncio.run(run_workers(workers))


if __name__ == "__main__":
    main()
