import re
from pathlib import Path

from app.classifier import get_extension
from app.email_sync import Attachment

INVALID_PATH_CHARS = '\\/:*?"<>|'

DIRECTION_LABELS = {"incoming": "وارد", "outgoing": "صادر"}


def sanitize_path_component(name: str, max_len: int = 150) -> str:
    name = name.strip()
    for ch in INVALID_PATH_CHARS:
        name = name.replace(ch, " ")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name or "بدون اسم")[:max_len]


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    counter = 2
    candidate = parent / f"{stem} ({counter}){suffix}"
    while candidate.exists():
        counter += 1
        candidate = parent / f"{stem} ({counter}){suffix}"
    return candidate


def department_dir(storage_root: str, direction: str, department: str) -> Path:
    root = Path(storage_root)
    direction_label = DIRECTION_LABELS[direction]
    dept = sanitize_path_component(department)
    d = root / direction_label / dept
    d.mkdir(parents=True, exist_ok=True)
    return d


def store_single_attachment(
    storage_root: str,
    direction: str,
    department: str,
    title: str,
    source_email: str,
    dest_email: str,
    attachment: Attachment,
) -> dict:
    dept_dir = department_dir(storage_root, direction, department)
    ext = get_extension(attachment.filename)
    base_name = sanitize_path_component(f"{title} - {source_email} - {dest_email}")
    path = _unique_path(dept_dir / f"{base_name}{ext}")
    path.write_bytes(attachment.content)
    return {
        "folder_path": str(dept_dir),
        "primary_file_path": str(path),
        "attachments": [
            {
                "original_filename": attachment.filename,
                "stored_path": str(path),
                "file_type": attachment.content_type,
            }
        ],
    }


def store_multiple_attachments(
    storage_root: str,
    direction: str,
    department: str,
    title: str,
    attachments: list[Attachment],
    primary_attachment: Attachment,
) -> dict:
    dept_dir = department_dir(storage_root, direction, department)
    letter_dir_name = sanitize_path_component(title)
    letter_dir = dept_dir / letter_dir_name
    counter = 2
    while letter_dir.exists():
        letter_dir = dept_dir / f"{letter_dir_name} ({counter})"
        counter += 1
    letter_dir.mkdir(parents=True, exist_ok=True)

    records = []
    primary_path = None
    for att in attachments:
        safe_name = sanitize_path_component(Path(att.filename).stem) + get_extension(att.filename)
        path = _unique_path(letter_dir / safe_name)
        path.write_bytes(att.content)
        records.append(
            {
                "original_filename": att.filename,
                "stored_path": str(path),
                "file_type": att.content_type,
            }
        )
        if att is primary_attachment:
            primary_path = str(path)

    return {
        "folder_path": str(letter_dir),
        "primary_file_path": primary_path or (records[0]["stored_path"] if records else None),
        "attachments": records,
    }
