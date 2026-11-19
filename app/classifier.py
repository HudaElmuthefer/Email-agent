import io
import json
import logging
import re
import time
from datetime import date

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

# 429 RESOURCE_EXHAUSTED on the free tier is a per-model quota (e.g. 20
# requests/day for the flash model — confirmed in agent.log). Other models
# have their own separate quota, so on 429 we move on to the next model here
# instead of failing. Models that don't exist for the key are skipped.
QUOTA_FALLBACK_MODELS = [
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]
DEFAULT_QUOTA_COOLDOWN_SECONDS = 3600

# model name -> time.time() until which it is known to be out of quota, so
# later calls (e.g. the rest of a sync run) don't waste a request on it.
_exhausted_until: dict[str, float] = {}


class QuotaExhaustedError(Exception):
    """Every candidate Gemini model is currently out of quota."""

    def __init__(self, retry_after_seconds: float):
        super().__init__(f"Gemini quota exhausted for all models; retry in {int(retry_after_seconds)}s")
        self.retry_after_seconds = retry_after_seconds


def _parse_retry_delay(message: str) -> float:
    match = re.search(r"retryDelay'?\"?:\s*'?\"?(\d+(?:\.\d+)?)s", message)
    return float(match.group(1)) if match else DEFAULT_QUOTA_COOLDOWN_SECONDS


def _generate_with_model(client: genai.Client, model: str, contents, **kwargs):
    """One model, with retries for transient 503 and the retired-model fix."""
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


def generate_content_safe(client: genai.Client, model: str, contents, **kwargs):
    """Calls generate_content. Retries with FALLBACK_MODEL if the configured
    model has been retired (Google periodically deprecates dated model
    names), persisting the fix so future calls skip the retry. Also retries
    a couple of times, with a short backoff, on a transient server-side 503
    (see TRANSIENT_STATUS_CODES above) before giving up. On 429 quota
    exhaustion it moves on to the next model in QUOTA_FALLBACK_MODELS, and
    raises QuotaExhaustedError once every candidate is out of quota."""
    candidates = [model] + [m for m in QUOTA_FALLBACK_MODELS if m != model]
    soonest_retry: float | None = None
    for candidate in candidates:
        now = time.time()
        until = _exhausted_until.get(candidate, 0)
        if until > now:
            soonest_retry = min(soonest_retry or until - now, until - now)
            continue
        try:
            return _generate_with_model(client, candidate, contents, **kwargs)
        except Exception as exc:
            message = str(exc)
            status_code = getattr(exc, "code", None)
            if status_code == 429 or "RESOURCE_EXHAUSTED" in message:
                delay = _parse_retry_delay(message)
                _exhausted_until[candidate] = time.time() + delay
                soonest_retry = min(soonest_retry or delay, delay)
                logger.warning("Gemini quota exhausted for %s (retry in %ds); trying next model", candidate, delay)
                continue
            if candidate != model and (status_code == 404 or "NOT_FOUND" in message):
                logger.warning("Fallback model %s not available; skipping", candidate)
                continue
            raise
    raise QuotaExhaustedError(soonest_retry or DEFAULT_QUOTA_COOLDOWN_SECONDS)

# Title for naming the file, plus the letter's number (العدد) and date for
# duplicate detection, in one request so detection costs no extra Gemini quota.
ANALYZE_PROMPT = """أنت موظف أرشفة (document controller) في دائرة حكومية عراقية. لديك محتوى مستند من كتاب رسمي.
المطلوب ثلاثة أشياء:
1) title: عنوان قصير مناسب لتسمية الملف به. إذا وجدت في النص عبارة تبدأ بـ "م/" أو "الموضوع:" أو "الموضوع/"، استخدم النص الذي يليها مباشرة كعنوان (اختصره إذا كان طويلاً جداً مع الحفاظ على المعنى). إذا لم تجد مثل هذه العبارة، لخّص موضوع الكتاب في عبارة عربية قصيرة (٣ إلى ٨ كلمات). بدون الأحرف: \\ / : * ? " < > |
2) number: عدد الكتاب كما هو مكتوب في ترويسة الكتاب بجانب كلمة "العدد" أو "الرقم" (مثال: "ت/1234" أو "5678"). إن لم يوجد أعد "".
3) date: تاريخ الكتاب المكتوب في الترويسة بجانب كلمة "التاريخ" بالصيغة YYYY-MM-DD (ميلادي). إن لم يوجد أعد "".
لا تخلط بين عدد هذا الكتاب وأعداد الكتب المشار إليها في المتن (مثل "كتابكم المرقم ...").
أعد JSON فقط بالشكل: {"title": "...", "number": "...", "date": "..."}
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


# Without a timeout a stalled request blocks the scan forever (and, via the
# scan lock, every later scan too). Legitimate requests uploading a scanned
# PDF over this connection have been seen taking up to ~5.5 minutes.
GEMINI_TIMEOUT_MS = 8 * 60 * 1000


def build_client() -> genai.Client:
    api_key = config.get_secret(config.KEY_GEMINI_API_KEY)
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS))


def _ask_gemini(
    client: genai.Client, model: str, prompt: str, attachment: Attachment | None, **kwargs
) -> str:
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

    response = generate_content_safe(client, model, contents, **kwargs)
    return (response.text or "").strip()


_DIGIT_TRANSLATION = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def normalize_letter_number(raw: str) -> str | None:
    """Reduces a letter number to its digit groups ("ت / ١٢٣٤" -> "1234",
    "ق/12/345" -> "12/345") so the same number matches however Gemini spells
    the prefix, spacing or digits. None when it holds no digits."""
    groups = re.findall(r"\d+", (raw or "").translate(_DIGIT_TRANSLATION))
    return "/".join(str(int(g)) for g in groups) if groups else None


def _parse_letter_date(raw: str) -> date | None:
    """Parses the date Gemini returned. The prompt asks for YYYY-MM-DD, but
    despite that some replies come back as YYYY/MM/DD; without this fallback
    such a letter would silently skip the number+date duplicate check instead
    of just the date separator being different."""
    cleaned = (raw or "").translate(_DIGIT_TRANSLATION).strip().replace("/", "-")
    try:
        return date.fromisoformat(cleaned)
    except ValueError:
        return None


def analyze_letter(
    client: genai.Client, model: str, attachment: Attachment, subject_fallback: str
) -> tuple[str, str | None, date | None]:
    """Returns (title, normalized letter number, letter date) read from the
    attachment. Number/date are None when Gemini could not find them."""
    fallback_title = sanitize_title(subject_fallback) if subject_fallback else "كتاب بدون عنوان"
    try:
        raw = _ask_gemini(
            client, model, ANALYZE_PROMPT, attachment,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        data = json.loads(raw)
        if isinstance(data, list):
            data = data[0] if data else {}
        title = sanitize_title(str(data.get("title") or ""))
        if title == "كتاب بدون عنوان":
            title = fallback_title
        return (
            title,
            normalize_letter_number(str(data.get("number") or "")),
            _parse_letter_date(str(data.get("date") or "")),
        )
    except Exception:
        logger.exception("Gemini letter analysis failed, falling back to subject")
    return fallback_title, None, None


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
