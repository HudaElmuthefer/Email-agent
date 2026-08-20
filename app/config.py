import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

import keyring

APP_NAME = "MowrEmailAgent"
KEYRING_SERVICE = APP_NAME

CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
DEPARTMENT_ALIASES_FILE = CONFIG_DIR / "department_aliases.json"

DEFAULT_DEPARTMENT_ALIASES = {
    "dpt-indivisuals@mowr.gov.iq": "الافراد",
    "planning-dpt2012@mowr.gov.iq": "التخطيط",
    "contpremant-2018@mowr.gov.iq": "الحسابات",
    "machineries-dep@mowr.gov.iq": "الاليات",
    "stor-2018@mowr.gov.iq": "الموجودات",
}

DEFAULT_STORAGE_ROOT = str(Path.home() / "Documents" / "مراسلات مديرية مشروع ماء البصرة")

KEY_IMAP_PASSWORD = "imap_password"
KEY_POSTGRES_PASSWORD = "postgres_password"
KEY_GEMINI_API_KEY = "gemini_api_key"


@dataclass
class Settings:
    imap_host: str = "mail.mowr.gov.iq"
    imap_port: int = 993
    imap_user: str = "basrahwp@mowr.gov.iq"
    imap_inbox_folder: str = "INBOX"
    imap_sent_folder: str = "INBOX.Sent"

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "mowr_email_agent"
    postgres_user: str = "postgres"

    gemini_model: str = "gemini-flash-latest"

    storage_root: str = DEFAULT_STORAGE_ROOT
    scan_interval_hours: int = 24
    autostart_enabled: bool = True

    setup_complete: bool = False


def load_settings() -> Settings:
    if not CONFIG_FILE.exists():
        return Settings()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return Settings()
    known_fields = {f for f in Settings.__dataclass_fields__}
    filtered = {k: v for k, v in data.items() if k in known_fields}
    return Settings(**filtered)


def save_settings(settings: Settings) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_secret(key: str) -> str | None:
    return keyring.get_password(KEYRING_SERVICE, key)


def set_secret(key: str, value: str) -> None:
    keyring.set_password(KEYRING_SERVICE, key, value)


def has_all_secrets() -> bool:
    return all(
        get_secret(k)
        for k in (KEY_IMAP_PASSWORD, KEY_POSTGRES_PASSWORD, KEY_GEMINI_API_KEY)
    )


def load_department_aliases() -> dict[str, str]:
    """Maps sender/recipient email -> a friendly department name, used when
    the raw email header has no display name to fall back on. User-editable."""
    if not DEPARTMENT_ALIASES_FILE.exists():
        save_department_aliases(dict(DEFAULT_DEPARTMENT_ALIASES))
        return dict(DEFAULT_DEPARTMENT_ALIASES)
    try:
        data = json.loads(DEPARTMENT_ALIASES_FILE.read_text(encoding="utf-8"))
        return {str(k).lower(): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_DEPARTMENT_ALIASES)


def save_department_aliases(aliases: dict[str, str]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    normalized = {k.lower().strip(): v.strip() for k, v in aliases.items() if k.strip() and v.strip()}
    DEPARTMENT_ALIASES_FILE.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8"
    )
