from contextlib import contextmanager
from datetime import date, datetime

import psycopg2
import psycopg2.extras

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS letters (
    id SERIAL PRIMARY KEY,
    direction TEXT NOT NULL CHECK (direction IN ('incoming', 'outgoing')),
    department TEXT NOT NULL,
    title TEXT NOT NULL,
    subject TEXT,
    source_email TEXT NOT NULL,
    dest_email TEXT NOT NULL,
    email_date TIMESTAMP NOT NULL,
    folder_path TEXT NOT NULL,
    primary_file_path TEXT,
    imap_folder TEXT NOT NULL,
    imap_uid BIGINT NOT NULL,
    created_at TIMESTAMP DEFAULT now(),
    UNIQUE (imap_folder, imap_uid)
);

CREATE TABLE IF NOT EXISTS attachments (
    id SERIAL PRIMARY KEY,
    letter_id INTEGER REFERENCES letters(id) ON DELETE CASCADE,
    original_filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    file_type TEXT
);

CREATE TABLE IF NOT EXISTS sync_state (
    imap_folder TEXT PRIMARY KEY,
    last_uid BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_letters_department ON letters (department);
CREATE INDEX IF NOT EXISTS idx_letters_direction ON letters (direction);
CREATE INDEX IF NOT EXISTS idx_letters_email_date ON letters (email_date);
CREATE INDEX IF NOT EXISTS idx_letters_title ON letters (title);

-- SHA-256 of the attachment's bytes, used to detect the same file arriving
-- again (in the same email or a later one) so it isn't archived/printed twice.
-- '' marks an old row whose stored file could no longer be read.
ALTER TABLE attachments ADD COLUMN IF NOT EXISTS content_hash TEXT;
CREATE INDEX IF NOT EXISTS idx_attachments_content_hash ON attachments (content_hash);

-- The letter's own number (العدد, digit groups only) and date as read from
-- the document; together with direction (and, for incoming mail, the sending
-- department) they identify a letter, so a match means it was already
-- archived (and printed). See find_letter_by_number for why the scope
-- matters: different directions/departments keep independent number series.
ALTER TABLE letters ADD COLUMN IF NOT EXISTS letter_number TEXT;
ALTER TABLE letters ADD COLUMN IF NOT EXISTS letter_date DATE;
CREATE INDEX IF NOT EXISTS idx_letters_number_date ON letters (letter_number, letter_date);
"""


def _connection_params():
    settings = config.load_settings()
    password = config.get_secret(config.KEY_POSTGRES_PASSWORD)
    return dict(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=password,
    )


@contextmanager
def get_connection():
    conn = psycopg2.connect(**_connection_params())
    try:
        yield conn
    finally:
        conn.close()


def ensure_schema():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()


def get_last_uid(conn, imap_folder: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_uid FROM sync_state WHERE imap_folder = %s", (imap_folder,)
        )
        row = cur.fetchone()
        return row[0] if row else 0


def set_last_uid(conn, imap_folder: str, last_uid: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_state (imap_folder, last_uid) VALUES (%s, %s)
            ON CONFLICT (imap_folder) DO UPDATE SET last_uid = GREATEST(sync_state.last_uid, EXCLUDED.last_uid)
            """,
            (imap_folder, last_uid),
        )
    conn.commit()


def letter_exists(conn, imap_folder: str, imap_uid: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM letters WHERE imap_folder = %s AND imap_uid = %s LIMIT 1",
            (imap_folder, imap_uid),
        )
        return cur.fetchone() is not None


def get_meta(key: str) -> str | None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM app_meta WHERE key = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else None


def set_meta(key: str, value: str) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app_meta (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (key, value),
        )
        conn.commit()


def title_exists(conn, department: str, direction: str, title: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM letters WHERE department = %s AND direction = %s AND title = %s LIMIT 1",
            (department, direction, title),
        )
        return cur.fetchone() is not None


def insert_letter(
    conn,
    direction: str,
    department: str,
    title: str,
    subject: str,
    source_email: str,
    dest_email: str,
    email_date: datetime,
    folder_path: str,
    primary_file_path: str,
    imap_folder: str,
    imap_uid: int,
    attachments: list[dict],
    letter_number: str | None = None,
    letter_date: date | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO letters
                (direction, department, title, subject, source_email, dest_email,
                 email_date, folder_path, primary_file_path, imap_folder, imap_uid,
                 letter_number, letter_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (imap_folder, imap_uid) DO NOTHING
            RETURNING id
            """,
            (
                direction, department, title, subject, source_email, dest_email,
                email_date, folder_path, primary_file_path, imap_folder, imap_uid,
                letter_number, letter_date,
            ),
        )
        row = cur.fetchone()
        if row is None:
            conn.commit()
            return -1
        letter_id = row[0]
        for att in attachments:
            cur.execute(
                """
                INSERT INTO attachments (letter_id, original_filename, stored_path, file_type, content_hash)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    letter_id, att["original_filename"], att["stored_path"],
                    att.get("file_type"), att.get("content_hash"),
                ),
            )
    conn.commit()
    return letter_id


def find_attachment_by_hash(conn, content_hash: str) -> dict | None:
    """Returns where a file with these exact bytes was archived before, or None."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT a.original_filename, a.stored_path, l.title, l.department,
                   l.direction, l.email_date, l.subject
            FROM attachments a JOIN letters l ON l.id = a.letter_id
            WHERE a.content_hash = %s
            ORDER BY l.email_date
            LIMIT 1
            """,
            (content_hash,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def find_letter_by_number(
    conn, letter_number: str, letter_date: date, direction: str, department: str | None = None
) -> dict | None:
    """Returns the earlier archived letter with this number and date, or None.
    Always scoped to the same direction: incoming and outgoing numbers come
    from two unrelated sequences, so a match across directions is coincidence,
    not a duplicate. For incoming mail it is also scoped to the same sending
    department, since each external department keeps its own independent
    number sequence (two different departments can coincidentally issue a
    letter with the same number on the same date). Outgoing numbers are our
    own single organization-wide sequence, so they are left unscoped by
    department."""
    clauses = ["letter_number = %s", "letter_date = %s", "direction = %s"]
    params: list = [letter_number, letter_date, direction]
    if direction == "incoming" and department:
        clauses.append("department = %s")
        params.append(department)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT title, department, direction, email_date, subject,
                   primary_file_path AS stored_path
            FROM letters
            WHERE {' AND '.join(clauses)}
            ORDER BY email_date
            LIMIT 1
            """,
            params,
        )
        row = cur.fetchone()
        return dict(row) if row else None


def backfill_attachment_hashes(conn, hash_file) -> int:
    """Fills content_hash for attachments archived before duplicate detection
    existed, by hashing their stored files. Runs once per row: a missing file
    gets '' so it isn't retried on every scan."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, stored_path FROM attachments WHERE content_hash IS NULL")
        rows = cur.fetchall()
        for att_id, stored_path in rows:
            try:
                digest = hash_file(stored_path)
            except OSError:
                digest = ""
            cur.execute("UPDATE attachments SET content_hash = %s WHERE id = %s", (digest, att_id))
    conn.commit()
    return len(rows)


def count_letters(
    direction: str | None = None,
    department: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> int:
    clauses, params = [], []
    if direction:
        clauses.append("direction = %s")
        params.append(direction)
    if department:
        clauses.append("department ILIKE %s")
        params.append(f"%{department}%")
    if date_from:
        clauses.append("email_date >= %s")
        params.append(date_from)
    if date_to:
        clauses.append("email_date < %s")
        params.append(date_to)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM letters {where}", params)
        return cur.fetchone()[0]


def search_letters(
    keyword: str | None = None,
    direction: str | None = None,
    department: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
) -> list[dict]:
    clauses, params = [], []
    if keyword:
        clauses.append("(title ILIKE %s OR subject ILIKE %s)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if direction:
        clauses.append("direction = %s")
        params.append(direction)
    if department:
        clauses.append("department ILIKE %s")
        params.append(f"%{department}%")
    if date_from:
        clauses.append("email_date >= %s")
        params.append(date_from)
    if date_to:
        clauses.append("email_date < %s")
        params.append(date_to)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with get_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT id, direction, department, title, subject, source_email, dest_email,
                   email_date, folder_path, primary_file_path
            FROM letters {where}
            ORDER BY email_date DESC
            LIMIT %s
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]


def get_letter_by_id(letter_id: int) -> dict | None:
    with get_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM letters WHERE id = %s", (letter_id,))
        row = cur.fetchone()
        if not row:
            return None
        letter = dict(row)
        cur.execute(
            "SELECT original_filename, stored_path, file_type FROM attachments WHERE letter_id = %s",
            (letter_id,),
        )
        letter["attachments"] = [dict(r) for r in cur.fetchall()]
        return letter


def list_recent_letters(limit: int = 100) -> list[dict]:
    with get_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, direction, department, title, subject, source_email, dest_email,
                   email_date, folder_path, primary_file_path
            FROM letters ORDER BY email_date DESC LIMIT %s
            """,
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]
