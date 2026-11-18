import imaplib
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

    folders = [
        (settings.imap_inbox_folder, "incoming"),
        (settings.imap_sent_folder, "outgoing"),
    ]
    archived = 0
    failed_folders = []
    with db.get_connection() as conn:
        for folder, direction in folders:
            try:
                archived += _sync_folder(conn, client, settings, folder, direction, report)
            except Exception:
                # One folder failing (e.g. network drop) must not stop the
                # other folder from being scanned.
                logger.exception("Sync of folder %s failed", folder)
                failed_folders.append(folder)

    if failed_folders:
        raise RuntimeError(
            f"فشل الاتصال أثناء فحص {', '.join(failed_folders)} (تم أرشفة {archived} كتاب)، ستتم إعادة المحاولة تلقائياً."
        )

    db.set_meta("last_scan_at", datetime.now().isoformat())
    report(f"اكتمل الفحص. تم أرشفة {archived} كتاب جديد.")
    return archived


def _fetch_folder(folder: str, direction: str, last_uid: int, own_email: str) -> list[email_sync.FetchedMessage]:
    """Downloads the folder's new messages on a short-lived IMAP connection.
    The connection must not be held open while messages are processed:
    each Gemini call takes minutes, and the mail server drops idle sessions
    long before a full folder is done (seen in agent.log as
    "IMAP4.abort: socket error: EOF" on the next folder's SELECT)."""
    imap_conn = email_sync.connect()
    try:
        return email_sync.fetch_new_messages(imap_conn, folder, direction, last_uid, own_email)
    finally:
        try:
            imap_conn.logout()
        except Exception:
            pass


def _sync_folder(conn, client, settings, folder: str, direction: str, report: Callable[[str], None]) -> int:
    last_uid = db.get_last_uid(conn, folder)
    report(f"جاري الاتصال بالبريد وفحص مجلد {folder}...")
    try:
        messages = _fetch_folder(folder, direction, last_uid, settings.imap_user)
    except (OSError, imaplib.IMAP4.abort):
        logger.warning("IMAP fetch of %s failed; retrying once", folder, exc_info=True)
        messages = _fetch_folder(folder, direction, last_uid, settings.imap_user)

    archived = 0
    for msg in sorted(messages, key=lambda m: m.imap_uid):
        # Already archived by an earlier scan that was interrupted before it
        # saved its progress: skip it instead of writing duplicate files and
        # spending Gemini quota on it again.
        if msg.attachments and not db.letter_exists(conn, folder, msg.imap_uid):
            try:
                process_message(conn, client, settings.gemini_model, settings.storage_root, msg)
                archived += 1
                report(f"تمت أرشفة: {msg.subject or msg.department}")
            except Exception:
                logger.exception("Failed to process message uid=%s in %s", msg.imap_uid, folder)
        # Save progress after every message so an interrupted scan resumes
        # where it stopped rather than redoing the whole folder.
        db.set_last_uid(conn, folder, msg.imap_uid)
    return archived
