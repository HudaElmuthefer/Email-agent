import imaplib
import logging
import os
import threading
from datetime import date, datetime

import psycopg2
import webview

from app import autostart, chat_agent, classifier, config, db, sync_service

logger = logging.getLogger(__name__)


def _today_range():
    today = date.today()
    tomorrow = date.fromordinal(today.toordinal() + 1)
    return today, tomorrow


class Api:
    """Bridge exposed to the web frontend via window.pywebview.api.*.
    Every public method here is callable from JS and must return
    JSON-serializable data (dict/list/str/int/bool/None)."""

    def __init__(self):
        self.window = None
        self._chat_agent = None
        self._scan_status = "جاهز"

    def set_window(self, window):
        self.window = window

    # ---------- setup / settings ----------
    def get_setup_status(self):
        settings = config.load_settings()
        return {"setup_complete": settings.setup_complete and config.has_all_secrets()}

    def get_settings(self):
        settings = config.load_settings()
        return {
            "imap_host": settings.imap_host,
            "imap_port": settings.imap_port,
            "imap_user": settings.imap_user,
            "imap_inbox_folder": settings.imap_inbox_folder,
            "imap_sent_folder": settings.imap_sent_folder,
            "postgres_host": settings.postgres_host,
            "postgres_port": settings.postgres_port,
            "postgres_db": settings.postgres_db,
            "postgres_user": settings.postgres_user,
            "storage_root": settings.storage_root,
            "scan_interval_hours": settings.scan_interval_hours,
            "autostart_enabled": settings.autostart_enabled,
            "has_imap_password": bool(config.get_secret(config.KEY_IMAP_PASSWORD)),
            "has_postgres_password": bool(config.get_secret(config.KEY_POSTGRES_PASSWORD)),
            "has_gemini_key": bool(config.get_secret(config.KEY_GEMINI_API_KEY)),
        }

    def save_settings(self, payload: dict):
        try:
            settings = config.load_settings()
            settings.imap_host = payload["imap_host"].strip()
            settings.imap_port = int(payload["imap_port"])
            settings.imap_user = payload["imap_user"].strip()
            settings.imap_inbox_folder = payload["imap_inbox_folder"].strip()
            settings.imap_sent_folder = payload["imap_sent_folder"].strip()
            settings.postgres_host = payload["postgres_host"].strip()
            settings.postgres_port = int(payload["postgres_port"])
            settings.postgres_db = payload["postgres_db"].strip()
            settings.postgres_user = payload["postgres_user"].strip()
            settings.storage_root = payload["storage_root"].strip()
            settings.scan_interval_hours = int(payload["scan_interval_hours"])
            settings.autostart_enabled = bool(payload["autostart_enabled"])

            imap_password = (payload.get("imap_password") or "").strip()
            pg_password = (payload.get("postgres_password") or "").strip()
            gemini_key = (payload.get("gemini_key") or "").strip()

            if not imap_password and not config.get_secret(config.KEY_IMAP_PASSWORD):
                return {"ok": False, "error": "الرجاء إدخال كلمة مرور البريد."}
            if not pg_password and not config.get_secret(config.KEY_POSTGRES_PASSWORD):
                return {"ok": False, "error": "الرجاء إدخال كلمة مرور قاعدة البيانات."}
            if not gemini_key and not config.get_secret(config.KEY_GEMINI_API_KEY):
                return {"ok": False, "error": "الرجاء إدخال مفتاح Gemini API."}

            settings.setup_complete = True
            config.save_settings(settings)
            if imap_password:
                config.set_secret(config.KEY_IMAP_PASSWORD, imap_password)
            if pg_password:
                config.set_secret(config.KEY_POSTGRES_PASSWORD, pg_password)
            if gemini_key:
                config.set_secret(config.KEY_GEMINI_API_KEY, gemini_key)

            if settings.autostart_enabled:
                autostart.register()
            else:
                autostart.unregister()

            try:
                db.ensure_schema()
            except Exception:
                logger.warning("Could not create schema right after saving settings; will retry on first use.")

            return {"ok": True}
        except Exception as exc:
            logger.exception("save_settings failed")
            return {"ok": False, "error": str(exc)}

    def test_imap(self, payload: dict):
        try:
            password = (payload.get("imap_password") or "").strip() or config.get_secret(
                config.KEY_IMAP_PASSWORD
            )
            conn = imaplib.IMAP4_SSL(
                payload["imap_host"].strip(), int(payload["imap_port"]), timeout=15
            )
            conn.login(payload["imap_user"].strip(), password)
            typ1, _ = conn.select(payload["imap_inbox_folder"].strip(), readonly=True)
            typ2, _ = conn.select(payload["imap_sent_folder"].strip(), readonly=True)
            conn.logout()
            if typ1 == "OK" and typ2 == "OK":
                return {"ok": True, "message": "تم الاتصال بالبريد بنجاح والتحقق من المجلدين."}
            return {"ok": False, "message": "تم تسجيل الدخول لكن أحد المجلدين غير موجود بهذا الاسم."}
        except Exception as exc:
            return {"ok": False, "message": f"تعذر الاتصال بالبريد: {exc}"}

    def test_postgres(self, payload: dict):
        try:
            password = (payload.get("postgres_password") or "").strip() or config.get_secret(
                config.KEY_POSTGRES_PASSWORD
            )
            conn = psycopg2.connect(
                host=payload["postgres_host"].strip(),
                port=int(payload["postgres_port"]),
                dbname=payload["postgres_db"].strip(),
                user=payload["postgres_user"].strip(),
                password=password,
                connect_timeout=10,
            )
            conn.close()
            return {"ok": True, "message": "تم الاتصال بقاعدة البيانات بنجاح."}
        except Exception as exc:
            return {"ok": False, "message": f"تعذر الاتصال بقاعدة البيانات: {exc}"}

    def browse_storage_folder(self):
        if not self.window:
            return None
        result = self.window.create_file_dialog(webview.FileDialog.FOLDER)
        if not result:
            return None
        return result[0]

    # ---------- dashboard ----------
    def get_dashboard_stats(self):
        try:
            today, tomorrow = _today_range()
            incoming_today = db.count_letters(direction="incoming", date_from=today, date_to=tomorrow)
            outgoing_today = db.count_letters(direction="outgoing", date_from=today, date_to=tomorrow)
            last_scan_at = db.get_meta("last_scan_at")
            return {
                "incoming_today": incoming_today,
                "outgoing_today": outgoing_today,
                "last_scan_at": last_scan_at,
                "ok": True,
            }
        except Exception as exc:
            logger.exception("get_dashboard_stats failed")
            return {"incoming_today": 0, "outgoing_today": 0, "last_scan_at": None, "ok": False, "error": str(exc)}

    # ---------- letters ----------
    def list_letters(self, keyword: str = "", direction: str = "", limit: int = 200):
        try:
            if keyword or direction:
                rows = db.search_letters(keyword=keyword or None, direction=direction or None, limit=limit)
            else:
                rows = db.list_recent_letters(limit=limit)
            for row in rows:
                row["email_date"] = str(row["email_date"])
            return {"ok": True, "letters": rows}
        except Exception as exc:
            logger.exception("list_letters failed")
            return {"ok": False, "error": str(exc), "letters": []}

    def open_file(self, path: str):
        if not path or not os.path.exists(path):
            return {"ok": False, "error": "الملف غير موجود على القرص."}
        os.startfile(path)
        return {"ok": True}

    def open_folder(self, path: str):
        if not path or not os.path.exists(path):
            return {"ok": False, "error": "المجلد غير موجود على القرص."}
        os.startfile(path)
        return {"ok": True}

    # ---------- scan ----------
    # NOTE: scan progress is exposed via polling (get_scan_status), not
    # window.evaluate_js() — calling evaluate_js from a background thread
    # crashes WebView2 (COM objects are UI-thread-affine), and pywebview's
    # own error-formatting path for that exception has an infinite-recursion
    # bug, so it must never be called off the pywebview UI thread.
    def scan_now(self):
        def progress(text: str):
            self._scan_status = text

        try:
            count = sync_service.run_sync_cycle(progress=progress)
            return {"ok": True, "archived": count}
        except Exception as exc:
            logger.exception("scan_now failed")
            return {"ok": False, "error": str(exc)}

    def scan_now_async(self):
        self._scan_status = "جاري الفحص..."
        threading.Thread(target=self.scan_now, daemon=True).start()
        return {"ok": True}

    def get_scan_status(self):
        return {"status": self._scan_status}

    # ---------- chat ----------
    def ask(self, question: str):
        try:
            if self._chat_agent is None:
                self._chat_agent = chat_agent.ChatAgent()
            answer, letters = self._chat_agent.ask(question)
            return {"ok": True, "answer": answer, "letters": letters}
        except Exception as exc:
            logger.exception("ask failed")
            return {"ok": False, "answer": f"حدث خطأ: {exc}", "letters": []}

    # ---------- department aliases ----------
    def list_aliases(self):
        aliases = config.load_department_aliases()
        return [{"email": k, "department": v} for k, v in sorted(aliases.items())]

    def save_alias(self, email: str, department: str):
        email = (email or "").strip().lower()
        department = (department or "").strip()
        if not email or not department:
            return {"ok": False, "error": "الرجاء إدخال البريد الإلكتروني واسم القسم."}
        aliases = config.load_department_aliases()
        aliases[email] = department
        config.save_department_aliases(aliases)
        return {"ok": True}

    def delete_alias(self, email: str):
        email = (email or "").strip().lower()
        aliases = config.load_department_aliases()
        if email in aliases:
            del aliases[email]
            config.save_department_aliases(aliases)
        return {"ok": True}
