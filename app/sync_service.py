import logging
import threading
from datetime import datetime
from typing import Callable

from app import classifier, config, db, email_sync, file_organizer

logger = logging.getLogger(__name__)

_scan_lock = threading.Lock()


def process_message(conn, client, model: str, storage_root: str, msg: email_sync.FetchedMessage) -> None:
    primary = classifier.pick_primary_attachment(msg.attachments)
    base_title = classifier.generate_letter_title(client, model, primary, msg.subject)
    title = classifier.make_unique_title(
        conn, client, model, msg.department, msg.direction, base_title, primary
    )

    if len(msg.attachments) == 1:
        result = file_organizer.store_single_attachment(
            storage_root, msg.direction, msg.department, title,
            msg.source_email, msg.dest_email, msg.attachments[0],
        )
    else:
        result = file_organizer.store_multiple_attachments(
            storage_root, msg.direction, msg.department, title, msg.attachments, primary,
        )

    db.insert_letter(
        conn,
        direction=msg.direction,
        department=msg.department,
        title=title,
        subject=msg.subject,
        source_email=msg.source_email,
        dest_email=msg.dest_email,
        email_date=msg.email_date,
        folder_path=result["folder_path"],
        primary_file_path=result["primary_file_path"],
        imap_folder=msg.imap_folder,
        imap_uid=msg.imap_uid,
        attachments=result["attachments"],
    )
    logger.info("Archived letter: %s [%s/%s]", title, msg.direction, msg.department)


def run_sync_cycle(progress: Callable[[str], None] | None = None) -> int:
    """Runs one full sync pass over INBOX and Sent folders. Returns number of letters archived."""
    def report(text: str):
        logger.info(text)
        if progress:
            progress(text)

    if not _scan_lock.acquire(blocking=False):
        report("يوجد فحص آخر قيد التنفيذ بالفعل، تم تجاهل هذا الطلب.")
        return 0

    try:
        return _run_sync_cycle_locked(report)
    finally:
        _scan_lock.release()


def _run_sync_cycle_locked(report: Callable[[str], None]) -> int:
    settings = config.load_settings()
    db.ensure_schema()
    client = classifier.build_client()

    report("جاري الاتصال بالبريد الإلكتروني...")
    imap_conn = email_sync.connect()
    archived = 0
    try:
        folders = [
            (settings.imap_inbox_folder, "incoming"),
            (settings.imap_sent_folder, "outgoing"),
        ]
        with db.get_connection() as conn:
            for folder, direction in folders:
                last_uid = db.get_last_uid(conn, folder)
                report(f"جاري فحص مجلد {folder}...")
                messages = email_sync.fetch_new_messages(
                    imap_conn, folder, direction, last_uid, settings.imap_user
                )
                max_uid = last_uid
                for msg in messages:
                    max_uid = max(max_uid, msg.imap_uid)
                    if not msg.attachments:
                        continue
                    try:
                        process_message(conn, client, settings.gemini_model, settings.storage_root, msg)
                        archived += 1
                        report(f"تمت أرشفة: {msg.subject or msg.department}")
                    except Exception:
                        logger.exception("Failed to process message uid=%s in %s", msg.imap_uid, folder)
                db.set_last_uid(conn, folder, max_uid)
    finally:
        try:
            imap_conn.logout()
        except Exception:
            pass

    db.set_meta("last_scan_at", datetime.now().isoformat())
    report(f"اكتمل الفحص. تم أرشفة {archived} كتاب جديد.")
    return archived
