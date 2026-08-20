import ctypes
import logging
import logging.handlers
import subprocess
import sys
import threading
import time
from pathlib import Path

import win32api
import win32con
import win32event
import win32gui
import webview
import winerror

from app import autostart, config, db
from app.webview_api import Api

SINGLE_INSTANCE_MUTEX_NAME = "Global\\MowrEmailAgentSingleInstance"
WINDOW_TITLE = "E-Mail AI AGENT - مديرية مشروع ماء البصرة"

LOG_DIR = Path(config.CONFIG_DIR) / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# The supervisor and worker are separate OS processes (see main()/run_worker()
# below) that both run this module-level setup. They must NOT share one log
# file: RotatingFileHandler renames the file on rotation, and Windows refuses
# to rename a file that another process still has open, which would crash
# whichever process tries to rotate first. Each gets its own file instead.
_LOG_FILE = "agent.log" if "--worker" in sys.argv else "supervisor.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        # File only: this is a --windowed app with no console for the user,
        # and a console StreamHandler crashes on Arabic log text anyway since
        # Windows' default console codepage (cp1252) can't encode it.
        logging.handlers.RotatingFileHandler(
            LOG_DIR / _LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger(__name__)

# pywebview's edgechromium backend occasionally tries to read WebView2 COM
# properties (drag-drop/background-color sync) from a non-UI thread while a
# background thread (e.g. the scan) is busy; WebView2 rejects that cross-thread
# access, and pywebview's own error-formatting path for that specific
# exception has an unrelated infinite-recursion bug. Harmless to app
# functionality (verified: scans complete normally despite it) but extremely
# noisy, so it's suppressed here rather than left to flood the log file.
logging.getLogger("pywebview").setLevel(logging.CRITICAL)

stop_event = threading.Event()

if getattr(sys, "frozen", False):
    WEB_DIR = Path(sys._MEIPASS) / "app" / "web"
else:
    WEB_DIR = Path(__file__).resolve().parent / "web"


def scheduler_loop(api: Api):
    logger.info("scheduler_loop thread started")
    # Give WebView2 a moment to finish its own native startup before this
    # thread starts hammering the network — doing both at once was the
    # source of an earlier silent (native-level) crash shortly after launch.
    stop_event.wait(4)
    while not stop_event.is_set():
        settings = config.load_settings()
        interval_seconds = max(settings.scan_interval_hours, 1) * 3600
        try:
            api.scan_now()
        except Exception:
            logger.exception("Scheduled sync failed")
        stop_event.wait(interval_seconds)


def build_tray_icon(window):
    import pystray
    from PIL import Image, ImageDraw

    def make_image():
        img = Image.new("RGB", (64, 64), "#0B5394")
        draw = ImageDraw.Draw(img)
        draw.text((10, 22), "MW", fill="white")
        return img

    def on_open(icon, item):
        window.show()

    def on_exit(icon, item):
        stop_event.set()
        icon.stop()
        window.destroy()

    menu = pystray.Menu(
        pystray.MenuItem("فتح البرنامج", on_open, default=True),
        pystray.MenuItem("خروج", on_exit),
    )
    return pystray.Icon("MowrEmailAgent", make_image(), "E-Mail AI AGENT", menu)


def _show_existing_window() -> bool:
    """Finds the already-running instance's window (it may be hidden in the
    tray) and brings it to the foreground. Returns False if it couldn't be
    found, e.g. the other instance is still on the setup screen."""
    hwnd = win32gui.FindWindow(None, WINDOW_TITLE)
    if not hwnd:
        return False
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    return True


def run_worker():
    """The actual application window/tray/scheduler. Launched as a child
    process by the supervisor in main(). WebView2's native interop
    occasionally hard-crashes the process from a place Python's own
    try/except cannot reach (confirmed: no Python exception is ever logged
    when it happens). Running it as a child means that crash only kills
    this child process — the supervisor detects the death and relaunches
    it automatically, so the user never has to notice or re-double-click
    the exe themselves."""
    try:
        db.ensure_schema()
    except Exception:
        logger.warning("Database not reachable at startup; will retry on first use.")

    api = Api()
    window = webview.create_window(
        WINDOW_TITLE,
        url=str(WEB_DIR / "index.html"),
        js_api=api,
        width=1200,
        height=820,
        min_size=(900, 600),
    )
    api.set_window(window)

    def on_closing():
        window.hide()
        return False

    window.events.closing += on_closing

    tray_icon = build_tray_icon(window)
    threading.Thread(target=tray_icon.run, daemon=True).start()
    threading.Thread(target=scheduler_loop, args=(api,), daemon=True).start()

    settings = config.load_settings()
    if settings.autostart_enabled:
        autostart.register()

    background_launch = "--background" in sys.argv

    # private_mode defaults to True in pywebview (incognito-style session),
    # which silently wipes localStorage (e.g. the dark/light theme choice)
    # on every restart. Disable it and pin a stable storage_path so
    # preferences persist across launches like a normal browser profile.
    storage_path = str(config.CONFIG_DIR / "webview_storage")
    logger.info("webview.start() entering main loop")
    webview.start(
        func=(window.hide if background_launch else None),
        private_mode=False,
        storage_path=storage_path,
    )
    # If this line is missing after an unexpected exit, webview.start() never
    # returned normally (native-level crash rather than a clean tray exit).
    logger.info("webview.start() returned; shutting down normally")

    stop_event.set()
    try:
        tray_icon.stop()
    except Exception:
        pass


MAX_WORKER_RESTARTS = 20
HANG_CHECK_INTERVAL_SECONDS = 5
HANG_CONFIRM_COUNT = 3


def _watch_for_hang(proc: subprocess.Popen, stop_flag: threading.Event):
    """Runs alongside the worker subprocess for its whole lifetime. A native
    WebView2/COM fault doesn't always kill the process outright — it can also
    leave the worker's main thread stuck without ever returning, which Windows
    itself flags via IsHungAppWindow (confirmed via direct testing: the
    window's title bar showed "(Not Responding)" and mid-paint content, e.g.
    the banner image never finishing, could visually look like a broken
    layout even though nothing is actually wrong with the CSS). The exit-code
    based restart in main() can't catch this since the process never exits on
    its own — so this watchdog force-kills it after a sustained hang, which
    lets the normal restart loop take over exactly as it would for a crash.
    """
    hung_streak = 0
    while not stop_flag.wait(HANG_CHECK_INTERVAL_SECONDS):
        if proc.poll() is not None:
            return
        hwnd = win32gui.FindWindow(None, WINDOW_TITLE)
        if not hwnd:
            hung_streak = 0
            continue
        if ctypes.windll.user32.IsHungAppWindow(hwnd):
            hung_streak += 1
            logger.warning("Worker window unresponsive (%d/%d checks)", hung_streak, HANG_CONFIRM_COUNT)
            if hung_streak >= HANG_CONFIRM_COUNT:
                logger.error("Worker window hung for too long; force-killing it so the supervisor can restart it.")
                try:
                    proc.kill()
                except Exception:
                    pass
                return
        else:
            hung_streak = 0


def main():
    if "--worker" in sys.argv:
        # Launched by the supervisor branch below as a child process. Must
        # NOT repeat the mutex check: the supervisor already holds the named
        # mutex for the app's whole lifetime, so this child would always see
        # ERROR_ALREADY_EXISTS and wrongly conclude another instance is
        # running and exit immediately.
        run_worker()
        return

    # Prevent multiple concurrent supervisors: each one would spawn its own
    # worker, independently hitting IMAP/Postgres/Gemini, which previously
    # caused severe slowdowns and memory blowup when the app was launched
    # more than once (e.g. double-clicked again while already running).
    mutex = win32event.CreateMutex(None, False, SINGLE_INSTANCE_MUTEX_NAME)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        if not _show_existing_window():
            ctypes.windll.user32.MessageBoxW(
                0,
                "البرنامج يعمل بالفعل. تحقق من أيقونته بجانب الساعة.",
                "E-Mail AI AGENT",
                0x40,
            )
        sys.exit(0)

    # Supervisor mode: relaunch this same exe as a child process ("--worker")
    # and watch it. A native-level crash in the worker (WebView2/pythonnet
    # COM fault — confirmed unreachable by Python's own try/except, since it
    # kills the process with zero Python exception ever logged) only kills
    # that child; the supervisor notices it exited with a non-zero/abnormal
    # code and relaunches it within a second, so the app self-heals instead
    # of silently disappearing on the user.
    if getattr(sys, "frozen", False):
        worker_args = [sys.executable, "--worker"]
    else:
        worker_args = [sys.executable, str(Path(__file__).resolve()), "--worker"]
    if "--background" in sys.argv:
        worker_args.append("--background")

    restart_count = 0
    while True:
        logger.info("Supervisor launching worker (attempt %d)", restart_count + 1)
        proc = subprocess.Popen(worker_args)
        watchdog_stop = threading.Event()
        watchdog_thread = threading.Thread(
            target=_watch_for_hang, args=(proc, watchdog_stop), daemon=True
        )
        watchdog_thread.start()
        proc.wait()
        watchdog_stop.set()
        if proc.returncode == 0:
            logger.info("Worker exited cleanly (code 0); supervisor stopping.")
            break
        restart_count += 1
        logger.warning(
            "Worker exited abnormally (code %s); restart %d/%d",
            proc.returncode, restart_count, MAX_WORKER_RESTARTS,
        )
        if restart_count >= MAX_WORKER_RESTARTS:
            logger.error("Worker crashed too many times; giving up.")
            ctypes.windll.user32.MessageBoxW(
                0,
                "حدث خطأ متكرر أدى إلى توقف البرنامج بشكل متكرر. الرجاء إعادة تشغيل الجهاز أو التواصل مع الدعم الفني.",
                "E-Mail AI AGENT",
                0x10,
            )
            break
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Unhandled exception escaped main()")
        raise
