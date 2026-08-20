import email
import imaplib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header
from email.utils import getaddresses, parsedate_to_datetime

from app import config

logger = logging.getLogger(__name__)


@dataclass
class Attachment:
    filename: str
    content: bytes
    content_type: str


@dataclass
class FetchedMessage:
    imap_folder: str
    imap_uid: int
    direction: str  # "incoming" | "outgoing"
    department: str
    source_email: str
    dest_email: str
    subject: str
    email_date: datetime
    attachments: list[Attachment] = field(default_factory=list)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            out.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out).strip()


def _first_address(header_value: str | None) -> tuple[str, str]:
    """Returns (display_name, email_address) for the first address in a header."""
    if not header_value:
        return "", ""
    addresses = getaddresses([header_value])
    if not addresses:
        return "", ""
    name, addr = addresses[0]
    return _decode(name), addr.lower()


def _sanitize_department(name: str, fallback_email: str) -> str:
    name = name.strip()
    if not name:
        name = fallback_email.split("@")[0] if fallback_email else "غير مصنف"
    invalid = '\\/:*?"<>|'
    for ch in invalid:
        name = name.replace(ch, "")
    return name.strip() or "غير مصنف"


def connect() -> imaplib.IMAP4_SSL:
    settings = config.load_settings()
    password = config.get_secret(config.KEY_IMAP_PASSWORD)
    conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=30)
    conn.login(settings.imap_user, password)
    return conn


def _parse_message(raw: bytes, uid: int, imap_folder: str, direction: str, own_email: str) -> FetchedMessage | None:
    msg = email.message_from_bytes(raw)

    from_name, from_addr = _first_address(msg.get("From"))
    to_name, to_addr = _first_address(msg.get("To"))
    subject = _decode(msg.get("Subject"))

    date_hdr = msg.get("Date")
    try:
        email_date = parsedate_to_datetime(date_hdr) if date_hdr else datetime.now()
        if email_date.tzinfo is not None:
            email_date = email_date.replace(tzinfo=None)
    except (TypeError, ValueError):
        email_date = datetime.now()

    aliases = config.load_department_aliases()
    if direction == "incoming":
        department = aliases.get(from_addr.lower()) or _sanitize_department(from_name, from_addr)
        source_email = from_addr or "unknown@unknown"
        dest_email = own_email
    else:
        department = aliases.get(to_addr.lower()) or _sanitize_department(to_name, to_addr)
        source_email = own_email
        dest_email = to_addr or "unknown@unknown"

    attachments: list[Attachment] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename:
            continue
        filename = _decode(filename)
        try:
            content = part.get_payload(decode=True)
        except Exception:
            content = None
        if not content:
            continue
        attachments.append(
            Attachment(filename=filename, content=content, content_type=part.get_content_type())
        )

    return FetchedMessage(
        imap_folder=imap_folder,
        imap_uid=uid,
        direction=direction,
        department=department,
        source_email=source_email,
        dest_email=dest_email,
        subject=subject,
        email_date=email_date,
        attachments=attachments,
    )


def fetch_new_messages(
    conn: imaplib.IMAP4_SSL, imap_folder: str, direction: str, since_uid: int, own_email: str
) -> list[FetchedMessage]:
    typ, _ = conn.select(imap_folder, readonly=True)
    if typ != "OK":
        logger.warning("Could not select folder %s", imap_folder)
        return []

    start = since_uid + 1
    typ, data = conn.uid("search", None, f"UID {start}:*")
    if typ != "OK" or not data or not data[0]:
        return []

    uids = [int(u) for u in data[0].split() if int(u) >= start]
    messages: list[FetchedMessage] = []
    for uid in uids:
        typ, msg_data = conn.uid("fetch", str(uid), "(RFC822)")
        if typ != "OK" or not msg_data or not msg_data[0]:
            continue
        raw = msg_data[0][1]
        parsed = _parse_message(raw, uid, imap_folder, direction, own_email)
        if parsed:
            messages.append(parsed)
    return messages
