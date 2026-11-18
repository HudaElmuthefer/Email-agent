import imaplib
import logging
import threading
from datetime import datetime
from typing import Callable

from app import classifier, config, db, duplicate_alert, email_sync, file_organizer

logger = logging.getLogger(__name__)

_scan_lock = threading.Lock()

DIRECTION_LABELS = {"incoming": "وارد", "outgoing": "صادر"}

# Small images are almost always signature logos / inline pictures that every
# email from the same sender carries; alarming on them would be pure noise.
SMALL_IMAGE_BYTES = 50 * 1024


def _is_small_image(att: email_sync.Attachment) -> bool:
    return att.content_type.startswith("image/") and len(att.content) < SMALL_IMAGE_BYTES


def _split_duplicates(conn, msg: email_sync.FetchedMessage):
    """Returns (attachments to archive, alert lines for the duplicates dropped).
    An attachment is a duplicate when the exact same bytes were already archived
    (from any email) or appear earlier in this same email."""
    keep, alerts = [], []
    seen_in_message: dict[str, str] = {}
    for att in msg.attachments:
        if _is_small_image(att):
            keep.append(att)
            continue
        digest = file_organizer.content_hash(att.content)
        if digest in seen_in_message:
            alerts.append(
                f"• الملف «{att.filename}» مرفق أكثر من مرة في نفس الإيميل "
                f"(مكرر لـ «{seen_in_message[digest]}»)."
            )
            continue
        seen_in_message[digest] = att.filename
        previous = db.find_attachment_by_hash(conn, digest)
        if previous:
            alerts.append(
                f"• الملف «{att.filename}» مطابق تماماً لملف تمت أرشفته سابقاً:\n"
                + _describe_previous(previous)
            )
            continue
        keep.append(att)
    return keep, alerts


def _describe_previous(previous: dict) -> str:
    return (
        f"   الكتاب: {previous['title']}\n"
        f"   القسم: {previous['department']} ({DIRECTION_LABELS.get(previous['direction'], '')})\n"
        f"   تاريخ الإيميل السابق: {previous['email_date']:%Y-%m-%d %H:%M}\n"
        f"   المسار: {previous['stored_path']}"
    )


def _alert_duplicates(msg: email_sync.FetchedMessage, alerts: list[str], outcome: str) -> None:
    sender = msg.source_email if msg.direction == "incoming" else msg.dest_email
    text = (
        "⚠ تم العثور على مرفق مكرر — لا تطبعه مرة أخرى!\n\n"
        f"الإيميل: {msg.subject or '(بدون موضوع)'}\n"
        f"{'من' if msg.direction == 'incoming' else 'إلى'}: {sender}\n"
        f"التاريخ: {msg.email_date:%Y-%m-%d %H:%M}\n\n"
        + "\n\n".join(alerts)
        + f"\n\n{outcome}"
    )
    logger.warning("Duplicate attachment(s) in uid=%s (%s): %s", msg.imap_uid, msg.imap_folder, alerts)
    duplicate_alert.notify(text)


def process_message(conn, client, model: str, storage_root: str, msg: email_sync.FetchedMessage) -> bool:
    """Archives the message's attachments, skipping duplicates (with an alarm).
    A letter is a duplicate when its number (العدد) and date, read from the
    document, match a letter archived before; an individual attachment is also
    dropped when its exact bytes were archived before. Returns True if a letter
    was archived, False if it was skipped as a duplicate."""
    attachments, alerts = _split_duplicates(conn, msg)
    if not attachments:
        _alert_duplicates(msg, alerts, "لم يتم حفظ هذا الإيميل لأن كل مرفقاته مكررة.")
        return False
    msg.attachments = attachments

    primary = classifier.pick_primary_attachment(msg.attachments)
    base_title, letter_number, letter_date = classifier.analyze_letter(client, model, primary, msg.subject)
    if letter_number and letter_date:
        previous = db.find_letter_by_number(conn, letter_number, letter_date)
        if previous:
            alerts.insert(
                0,
                f"• الكتاب «{primary.filename}» بالعدد {letter_number} "
                f"والتاريخ {letter_date:%Y-%m-%d} تمت أرشفته سابقاً:\n"
                + _describe_previous(previous),
            )
            _alert_duplicates(msg, alerts, "لم يتم حفظ هذا الكتاب لأنه مكرر.")
            return False
    else:
        logger.info("No letter number/date found in %s (uid=%s); duplicate check by number skipped",
                    primary.filename, msg.imap_uid)
    if alerts:
        _alert_duplicates(msg, alerts, "تم حفظ بقية المرفقات فقط، ولم يُحفظ المكرر.")

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
        letter_number=letter_number,
        letter_date=letter_date,
    )
    logger.info("Archived letter: %s [%s/%s]", title, msg.direction, msg.department)
    return True


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
        try:
            backfilled = db.backfill_attachment_hashes(conn, file_organizer.hash_file)
            if backfilled:
                logger.info("Computed content hashes for %d previously archived attachments", backfilled)
        except Exception:
            conn.rollback()
            logger.exception("Backfilling attachment hashes failed")
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
                if process_message(conn, client, settings.gemini_model, settings.storage_root, msg):
                    archived += 1
                    report(f"تمت أرشفة: {msg.subject or msg.department}")
                else:
                    report(f"⚠ مرفق مكرر لم يُحفظ: {msg.subject or msg.department}")
            except Exception:
                logger.exception("Failed to process message uid=%s in %s", msg.imap_uid, folder)
        # Save progress after every message so an interrupted scan resumes
        # where it stopped rather than redoing the whole folder.
        db.set_last_uid(conn, folder, msg.imap_uid)
    return archived
