import io
import logging
import re
import time

import openpyxl
from docx import Document
from google import genai
from google.genai import types

from app import config, db
from app.email_sync import Attachment

logger = logging.getLogger(__name__)

INVALID_FILENAME_CHARS = '\\/:*?"<>|'

PDF_EXTS = {".pdf"}
WORD_EXTS = {".doc", ".docx"}
EXCEL_EXTS = {".xls", ".xlsx", ".xlsm"}

FALLBACK_MODEL = "gemini-flash-latest"

# 503 UNAVAILABLE ("model is currently experiencing high demand") is Google's
# own transient overload signal — confirmed via a real captured failure where
# the exact same model/key/request succeeded 22 seconds earlier. Worth a
# short retry; anything else (auth, bad request, not-found) is not transient
# and should surface immediately rather than being retried.
TRANSIENT_STATUS_CODES = {503}
MAX_TRANSIENT_RETRIES = 2


def generate_content_safe(client: genai.Client, model: str, contents, **kwargs):
    """Calls generate_content. Retries with FALLBACK_MODEL if the configured
    model has been retired (Google periodically deprecates dated model
    names), persisting the fix so future calls skip the retry. Also retries
    a couple of times, with a short backoff, on a transient server-side 503
    (see TRANSIENT_STATUS_CODES above) before giving up."""
    current_model = model
    for attempt in range(MAX_TRANSIENT_RETRIES + 1):
        try:
            return client.models.generate_content(model=current_model, contents=contents, **kwargs)
        except Exception as exc:
            message = str(exc)
            if current_model != FALLBACK_MODEL and "NOT_FOUND" in message and "no longer available" in message:
                logger.warning("Model %s retired, retrying with %s", current_model, FALLBACK_MODEL)
                current_model = FALLBACK_MODEL
                settings = config.load_settings()
                settings.gemini_model = FALLBACK_MODEL
                config.save_settings(settings)
                continue
            status_code = getattr(exc, "code", None)
            if status_code in TRANSIENT_STATUS_CODES and attempt < MAX_TRANSIENT_RETRIES:
                wait_seconds = 2 * (attempt + 1)
                logger.warning(
                    "Gemini returned %s (transient); retrying in %ss (%d/%d)",
                    status_code, wait_seconds, attempt + 1, MAX_TRANSIENT_RETRIES,
                )
                time.sleep(wait_seconds)
                continue
            raise

TITLE_PROMPT = """أنت موظف أرشفة (document controller) في دائرة حكومية عراقية. لديك محتوى مستند من كتاب رسمي.
مهمتك: استخرج عنواناً قصيراً مناسباً لتسمية الملف به.
القاعدة: إذا وجدت في النص عبارة تبدأ بـ "م/" أو "الموضوع:" أو "الموضوع/"، استخدم النص الذي يليها مباشرة كعنوان (اختصره إذا كان طويلاً جداً مع الحفاظ على المعنى).
إذا لم تجد مثل هذه العبارة، لخّص موضوع الكتاب في عبارة عربية قصيرة (٣ إلى ٨ كلمات) تدل على محتواه.
أعد فقط نص العنوان بدون أي شرح إضافي، بدون علامات اقتباس، وبدون الأحرف: \\ / : * ? " < > |
"""

DISTINGUISH_PROMPT = """العنوان "{title}" مستخدم سابقاً لكتاب آخر بنفس القسم والاتجاه. ابحث في نص المستند أدناه عن رقم مرجعي أو رقم كتاب أو أي تفصيل يميز هذا الكتاب عن غيره (مثال: رقم، تسلسل، تاريخ مذكور بالنص).
أعد فقط نص العنوان الأصلي "{title}" متبوعاً بمسافة ثم التفصيل المميز الذي وجدته (مثال: "{title} رقم 4").
إن لم تجد أي تفصيل مميز، أعد "{title}" كما هو بدون إضافة.
أعد فقط نص العنوان بدون أي شرح إضافي، بدون علامات اقتباس، وبدون الأحرف: \\ / : * ? " < > |
"""


def sanitize_title(text: str) -> str:
    text = text.strip().strip('"').strip("'")
    for ch in INVALID_FILENAME_CHARS:
        text = text.replace(ch, "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120] if text else "كتاب بدون عنوان"


def get_extension(filename: str) -> str:
    idx = filename.rfind(".")
    return filename[idx:].lower() if idx != -1 else ""


def extract_docx_text(content: bytes) -> str:
    try:
        doc = Document(io.BytesIO(content))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    if cell.text.strip():
                        parts.append(cell.text)
        return "\n".join(parts)
    except Exception:
        logger.exception("Failed to extract docx text")
        return ""


def extract_xlsx_text(content: bytes) -> str:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        parts = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    parts.append(" | ".join(cells))
        return "\n".join(parts[:500])
    except Exception:
        logger.exception("Failed to extract xlsx text")
        return ""


def pick_primary_attachment(attachments: list[Attachment]) -> Attachment:
    for att in attachments:
        if get_extension(att.filename) in PDF_EXTS:
            return att
    for att in attachments:
        if get_extension(att.filename) in WORD_EXTS:
            return att
    for att in attachments:
        if get_extension(att.filename) in EXCEL_EXTS:
            return att
    return attachments[0]


def build_client() -> genai.Client:
    api_key = config.get_secret(config.KEY_GEMINI_API_KEY)
    return genai.Client(api_key=api_key)


def _ask_gemini(client: genai.Client, model: str, prompt: str, attachment: Attachment | None) -> str:
    contents = []
    ext = get_extension(attachment.filename) if attachment else ""

    if attachment and ext in PDF_EXTS:
        contents.append(types.Part.from_bytes(data=attachment.content, mime_type="application/pdf"))
        contents.append(prompt)
    elif attachment and ext in WORD_EXTS:
        text = extract_docx_text(attachment.content) if ext == ".docx" else ""
        contents.append(f"{prompt}\n\nنص المستند:\n{text[:6000]}")
    elif attachment and ext in EXCEL_EXTS:
        text = extract_xlsx_text(attachment.content)
        contents.append(f"{prompt}\n\nمحتوى جدول البيانات:\n{text[:6000]}")
    else:
        contents.append(prompt)

    response = generate_content_safe(client, model, contents)
    return (response.text or "").strip()


def generate_letter_title(
    client: genai.Client, model: str, attachment: Attachment, subject_fallback: str
) -> str:
    try:
        raw = _ask_gemini(client, model, TITLE_PROMPT, attachment)
        title = sanitize_title(raw)
        if title and title != "كتاب بدون عنوان":
            return title
    except Exception:
        logger.exception("Gemini title generation failed, falling back to subject")
    return sanitize_title(subject_fallback) if subject_fallback else "كتاب بدون عنوان"


def make_unique_title(
    conn,
    client: genai.Client,
    model: str,
    department: str,
    direction: str,
    base_title: str,
    attachment: Attachment,
) -> str:
    if not db.title_exists(conn, department, direction, base_title):
        return base_title

    try:
        prompt = DISTINGUISH_PROMPT.format(title=base_title)
        raw = _ask_gemini(client, model, prompt, attachment)
        candidate = sanitize_title(raw)
        if candidate and not db.title_exists(conn, department, direction, candidate):
            return candidate
    except Exception:
        logger.exception("Gemini disambiguation failed, falling back to counter suffix")

    counter = 2
    candidate = f"{base_title} ({counter})"
    while db.title_exists(conn, department, direction, candidate):
        counter += 1
        candidate = f"{base_title} ({counter})"
    return candidate
