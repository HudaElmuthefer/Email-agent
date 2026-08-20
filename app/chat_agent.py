import logging
from datetime import date, datetime

from google.genai import types

from app import classifier, config, db

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """أنت مساعد أرشفة المراسلات لمديرية مشروع ماء البصرة. مهمتك الإجابة عن أسئلة موظف الأرشفة
حول الكتب الرسمية (الوارد والصادر) المؤرشفة في قاعدة البيانات، باستخدام الأدوات المتاحة فقط.
- لا تخترع معلومات؛ إن لم تجد نتيجة عبر الأدوات، أخبر المستخدم أنك لم تجد كتاباً مطابقاً.
- عند سؤال المستخدم عن كتاب محدد أو طلبه إياه، استخدم get_letter_details لإحضار مساره على الحاسبة.
- أجب دائماً باللغة العربية وبإيجاز ووضوح.
- التواريخ بصيغة YYYY-MM-DD.
"""


def _parse_date(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _simplify(letter: dict) -> dict:
    return {
        "id": letter["id"],
        "title": letter["title"],
        "direction": "وارد" if letter["direction"] == "incoming" else "صادر",
        "department": letter["department"],
        "email_date": str(letter["email_date"]),
        "source_email": letter.get("source_email"),
        "dest_email": letter.get("dest_email"),
        "file_path": letter.get("primary_file_path"),
        "folder_path": letter.get("folder_path"),
    }


class _Tools:
    """Holds only DB-facing tool methods and captured results — deliberately
    has no reference to the genai Client, whose internal objects (thread
    locks, connection pools) can't survive the SDK's deepcopy of tool configs."""

    def __init__(self):
        self.captured: list[dict] = []

    def _capture(self, letters: list[dict]) -> None:
        seen_ids = {c["id"] for c in self.captured}
        for letter in letters:
            if letter["id"] not in seen_ids:
                self.captured.append(letter)
                seen_ids.add(letter["id"])

    def count_letters(
        self, direction: str = "", department: str = "", date_from: str = "", date_to: str = ""
    ) -> int:
        """يحسب عدد الكتب المؤرشفة المطابقة للفلاتر.

        Args:
            direction: "incoming" للوارد، "outgoing" للصادر، أو فارغ لكل الاتجاهات.
            department: اسم القسم/الجهة (بحث جزئي)، أو فارغ لكل الأقسام.
            date_from: تاريخ البداية بصيغة YYYY-MM-DD، أو فارغ.
            date_to: تاريخ النهاية (غير شامل) بصيغة YYYY-MM-DD، أو فارغ.
        """
        return db.count_letters(
            direction=direction or None,
            department=department or None,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
        )

    def search_letters(
        self,
        keyword: str = "",
        direction: str = "",
        department: str = "",
        date_from: str = "",
        date_to: str = "",
    ) -> list[dict]:
        """يبحث عن كتب مؤرشفة حسب كلمة مفتاحية في العنوان/الموضوع أو فلاتر أخرى.

        Args:
            keyword: كلمة أو عبارة من عنوان أو موضوع الكتاب، أو فارغ.
            direction: "incoming" للوارد، "outgoing" للصادر، أو فارغ.
            department: اسم القسم/الجهة، أو فارغ.
            date_from: تاريخ البداية بصيغة YYYY-MM-DD، أو فارغ.
            date_to: تاريخ النهاية (غير شامل) بصيغة YYYY-MM-DD، أو فارغ.
        """
        rows = db.search_letters(
            keyword=keyword or None,
            direction=direction or None,
            department=department or None,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
        )
        simplified = [_simplify(r) for r in rows]
        self._capture(simplified)
        return simplified

    def get_letter_details(self, query: str) -> dict:
        """يبحث عن كتاب معين بالاسم أو كلمة من عنوانه ويعيد تفاصيله الكاملة بما فيها مسار الملف لفتحه.

        Args:
            query: نص من عنوان الكتاب أو موضوعه.
        """
        rows = db.search_letters(keyword=query, limit=5)
        if not rows:
            return {"found": False}
        simplified = [_simplify(r) for r in rows]
        self._capture(simplified)
        return {"found": True, "matches": simplified}

    def check_letter_sent(self, query: str) -> dict:
        """يتحقق هل يوجد كتاب صادر (تم إرساله) مطابق للاسم أو الموضوع المعطى.

        Args:
            query: نص من عنوان الكتاب أو موضوعه.
        """
        rows = db.search_letters(keyword=query, direction="outgoing", limit=5)
        simplified = [_simplify(r) for r in rows]
        self._capture(simplified)
        return {"sent": len(simplified) > 0, "matches": simplified}


class ChatAgent:
    def __init__(self):
        settings = config.load_settings()
        self.client = classifier.build_client()
        self.model = settings.gemini_model

    def ask(self, question: str) -> tuple[str, list[dict]]:
        tools = _Tools()
        gen_config = types.GenerateContentConfig(
            tools=[
                tools.count_letters,
                tools.search_letters,
                tools.get_letter_details,
                tools.check_letter_sent,
            ],
            system_instruction=SYSTEM_PROMPT,
        )
        try:
            response = classifier.generate_content_safe(
                self.client, self.model, question, config=gen_config
            )
            text = (response.text or "").strip() or "لم أتمكن من إيجاد إجابة."
        except Exception:
            logger.exception("Chat agent request failed")
            text = "حدث خطأ أثناء التواصل مع Gemini. الرجاء المحاولة مرة أخرى."
        return text, list(tools.captured)
