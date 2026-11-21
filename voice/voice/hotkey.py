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


def is_process_trusted() -> bool:
    """AXIsProcessTrustedWithOptions(prompt=False) via ctypes (macOS only)."""
    import ctypes
    try:
        hi = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/"
            "Frameworks/HIServices.framework/HIServices")
        hi.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        hi.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        return bool(hi.AXIsProcessTrustedWithOptions(None))  # None = no prompt
    except Exception:
        return True  # non-macOS or check unavailable: let pynput try


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
        import os
        import sys
        from pynput import keyboard
        self._loop = asyncio.get_event_loop()
        if not is_process_trusted():
            real = os.path.realpath(sys.executable)
            raise RuntimeError(
                "macOS has not trusted this process for input monitoring "
                "(AXIsProcessTrusted = false), so no key events will arrive. "
                "The process to trust is the Python binary ITSELF, not the "
                "terminal:\n"
                f"  {real}\n"
                "Add it to BOTH: System Settings -> Privacy & Security -> "
                "Accessibility AND -> Input Monitoring -> '+' -> paste that "
                "path. Then RESTART this process (trust applies at exec).\n"
                "Open the pane directly: open 'x-apple.systempreferences:"
                "com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility'\n"
                "Prefer no permissions? Run with --realtime alone (VAD mode).")
        try:
            self._listener = keyboard.Listener(
                on_press=lambda k: self._set(True) if _is_right_option(k) else None,
                on_release=lambda k: self._set(False) if _is_right_option(k) else None,
            )
            self._listener.start()
        except Exception as e:
            real = os.path.realpath(sys.executable)
            raise RuntimeError(
                "macOS blocked the global hotkey listener ('process is not "
                "trusted'). The process to trust is the Python binary ITSELF, "
                "not the terminal:\n"
                f"  {real}\n"
                "Add it here: System Settings -> Privacy & Security -> "
                "Accessibility (AND -> Input Monitoring) -> '+' -> paste that "
                "path. Then RESTART this process (permission applies at exec).\n"
                "Open the pane directly: open 'x-apple.systempreferences:"
                "com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility'\n"
                "No permission needed? Run without --ptt (VAD mode listens "
                "automatically).\n"
                f"Underlying error: {e}") from e

    async def wait_pressed(self) -> None:
        await self.pressed.wait()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
