"""Push-to-talk hotkey: hold the RIGHT OPTION key (macOS) to talk.

Uses pynput's global listener. On macOS the terminal app (or this Python)
needs Accessibility permission: System Settings -> Privacy & Security ->
Accessibility -> allow your terminal / uv-run python.

Right Option is matched as pynput `Key.alt_r` (Darwin VK 0x3E); the raw vk
code is checked too, since pynput versions differ on how they label it.
"""

from __future__ import annotations

import asyncio
from typing import Optional

RIGHT_OPTION_VK = 0x3E  # kVK_RightOption on macOS


def _is_right_option(key) -> bool:
    try:
        from pynput.keyboard import Key
        if key == Key.alt_r:
            return True
    except Exception:
        pass
    return getattr(key, "vk", None) == RIGHT_OPTION_VK


class PushToTalk:
    """Global right-Option listener exposing an asyncio.Event `pressed`."""

    def __init__(self) -> None:
        self.pressed = asyncio.Event()
        self._listener = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _set(self, value: bool) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(
                self.pressed.set if value else self.pressed.clear)

    def start(self) -> None:
        from pynput import keyboard
        self._loop = asyncio.get_event_loop()
        try:
            self._listener = keyboard.Listener(
                on_press=lambda k: self._set(True) if _is_right_option(k) else None,
                on_release=lambda k: self._set(False) if _is_right_option(k) else None,
            )
            self._listener.start()
        except Exception as e:
            raise RuntimeError(
                "Could not start the global hotkey listener. On macOS, grant "
                "Accessibility permission to your terminal (System Settings -> "
                "Privacy & Security -> Accessibility). "
                f"Underlying error: {e}") from e

    async def wait_pressed(self) -> None:
        await self.pressed.wait()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
