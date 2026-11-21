"""
Non-blocking WebSocket senders.

Every connected socket (worker or board browser) gets a Peer: `send()` serialises the frame
immediately and puts it on a per-socket queue; a writer task drains the queue. So the hub can
broadcast while holding its lock, frame order always matches state order, and one slow or
dead browser never blocks the others (an overflowing queue marks the peer closed and the hub
drops it on the next broadcast).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable

from fastapi import WebSocket, WebSocketDisconnect


class Peer:
    def __init__(self, ws: WebSocket, name: str, max_queue: int = 5000) -> None:
        self.ws = ws
        self.name = name
        self.max_queue = max_queue
        self.closed = False
        self.close_code = 1000
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()

    def send(self, frame: dict) -> bool:
        return self.send_text(json.dumps(frame, ensure_ascii=False))

    def send_text(self, text: str) -> bool:
        if self.closed:
            return False
        if self._queue.qsize() >= self.max_queue:
            print(f"[peer] {self.name}: send queue overflow, dropping connection")
            self.close(1013)
            return False
        self._queue.put_nowait(text)
        return True

    def close(self, code: int = 1000) -> None:
        if not self.closed:
            self.closed = True
            self.close_code = code
            self._queue.put_nowait(None)

    async def run_writer(self) -> None:
        try:
            while True:
                item = await self._queue.get()
                if item is None:
                    break
                await self.ws.send_text(item)
        except Exception:  # socket died; the reader side will notice too
            pass
        finally:
            self.closed = True
            with contextlib.suppress(Exception):
                await self.ws.close(code=self.close_code)


async def serve_peer(peer: Peer, on_text: Callable[[str], Awaitable[None]] | None = None) -> None:
    """
    Run reader + writer for an accepted socket until either side ends.
    `on_text` handles incoming text frames (None = ignore client input).
    """

    async def reader() -> None:
        try:
            while True:
                msg = await peer.ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                text = msg.get("text")
                if text is None and msg.get("bytes") is not None:
                    text = msg["bytes"].decode("utf-8", "replace")
                if text is not None and on_text is not None:
                    await on_text(text)
        except (WebSocketDisconnect, RuntimeError):
            return

    tasks = {asyncio.create_task(reader()), asyncio.create_task(peer.run_writer())}
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        peer.closed = True
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
