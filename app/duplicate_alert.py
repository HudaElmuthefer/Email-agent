import ctypes
import logging
import threading
import winsound

logger = logging.getLogger(__name__)

MB_ICONWARNING = 0x30
MB_SETFOREGROUND = 0x10000
MB_TOPMOST = 0x40000
MB_RIGHT = 0x80000
MB_RTLREADING = 0x100000

ALERT_TITLE = "تنبيه: مرفق مكرر - E-Mail AI AGENT"


def _play_alarm():
    try:
        for _ in range(3):
            winsound.Beep(1200, 250)
            winsound.Beep(800, 250)
    except RuntimeError:
        # No sound device that supports Beep: fall back to the system sound.
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)


def _show(text: str):
    try:
        _play_alarm()
        ctypes.windll.user32.MessageBoxW(
            0, text, ALERT_TITLE,
            MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST | MB_RIGHT | MB_RTLREADING,
        )
    except Exception:
        logger.exception("Could not show duplicate attachment alert")


def notify(text: str) -> None:
    """Plays an alarm and shows a topmost message box. Runs on its own thread:
    a message box blocks until dismissed, and the scan (and the WebView2 UI
    thread) must keep going while it is open, even if the user is away."""
    threading.Thread(target=_show, args=(text,), daemon=True).start()
