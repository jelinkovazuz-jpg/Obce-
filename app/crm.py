from datetime import date
from pathlib import Path
from uuid import uuid4

import duckdb


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "obce.duckdb"

STATUSES = ["Nová", "K oslovení", "Kontaktovaná", "Jednání", "Klient", "Odloženo"]
PRIORITIES = ["Nízká", "Střední", "Vysoká"]
ACTIVITY_TYPES = ["Telefonát", "E-mail", "Schůzka", "Poznámka"]


def connect():
    return duckdb.connect(str(DB_PATH))


def init_crm(conn):
    """Create the CRM extension without changing the municipality catalogue."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crm_records (
            kod_obce INTEGER PRIMARY KEY,
            status VARCHAR NOT NULL DEFAULT 'Nová',
            priority VARCHAR NOT NULL DEFAULT 'Střední',
            owner_username VARCHAR,
            next_contact DATE,
            note VARCHAR,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # Migration for databases created by an older CRM version.
    conn.execute("ALTER TABLE crm_records ADD COLUMN IF NOT EXISTS contacted_on DATE")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crm_activities (
            id VARCHAR PRIMARY KEY,
            kod_obce INTEGER NOT NULL,
            activity_type VARCHAR NOT NULL,
            subject VARCHAR NOT NULL,
            detail VARCHAR,
            created_by VARCHAR,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crm_tasks (
            id VARCHAR PRIMARY KEY,
            kod_obce INTEGER NOT NULL,
            title VARCHAR NOT NULL,
            due_date DATE,
            assigned_to VARCHAR,
            completed BOOLEAN NOT NULL DEFAULT FALSE,
            created_by VARCHAR,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        )
        """
    )
    init_crm_documents(conn)


def init_crm_documents(conn):
    """Idempotent migration kept callable from hot-reloaded Streamlit views."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crm_documents (
            id VARCHAR PRIMARY KEY,
            kod_obce INTEGER NOT NULL,
            quote_id VARCHAR UNIQUE,
            document_type VARCHAR NOT NULL,
            file_name VARCHAR NOT NULL,
            mime_type VARCHAR NOT NULL,
            file_data BLOB NOT NULL,
            created_by VARCHAR,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def save_offer_document(conn, kod_obce, quote_id, file_name, pdf_data, username):
    """Save the latest generated PDF for a quote in its municipality card."""
    init_crm_documents(conn)
    if not pdf_data or not bytes(pdf_data).startswith(b"%PDF"):
        raise ValueError("Dokument není platné PDF.")
    existing = conn.execute(
        "SELECT id FROM crm_documents WHERE quote_id=?", [quote_id]
    ).fetchone()
    document_id = existing[0] if existing else str(uuid4())
    conn.execute(
        """
        INSERT INTO crm_documents (
            id,kod_obce,quote_id,document_type,file_name,mime_type,
            file_data,created_by,updated_at
        ) VALUES (?,?,?,'Energetická nabídka',?,'application/pdf',?,?,now())
        ON CONFLICT (quote_id) DO UPDATE SET
            kod_obce=excluded.kod_obce,
            file_name=excluded.file_name,
            mime_type=excluded.mime_type,
            file_data=excluded.file_data,
            created_by=excluded.created_by,
            updated_at=now()
        """,
        [document_id, kod_obce, quote_id, file_name, bytes(pdf_data), username],
    )
    return document_id, existing is not None


def save_record(conn, kod_obce, status, priority, owner, next_contact, note):
    conn.execute(
        """
        INSERT INTO crm_records
            (kod_obce, status, priority, owner_username, next_contact, note, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT (kod_obce) DO UPDATE SET
            status = excluded.status,
            priority = excluded.priority,
            owner_username = excluded.owner_username,
            next_contact = excluded.next_contact,
            note = excluded.note,
            updated_at = now()
        """,
        [kod_obce, status, priority, owner or None, next_contact, note or None],
    )


def add_activity(conn, kod_obce, activity_type, subject, detail, username):
    conn.execute(
        """
        INSERT INTO crm_activities
            (id, kod_obce, activity_type, subject, detail, created_by)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [str(uuid4()), kod_obce, activity_type, subject, detail or None, username],
    )


def set_contacted_on(conn, kod_obce, contacted_on, username):
    """Save the latest outreach date and keep each change in activity history."""
    previous = conn.execute(
        "SELECT contacted_on FROM crm_records WHERE kod_obce = ?", [kod_obce]
    ).fetchone()
    previous_date = previous[0] if previous else None
    if previous_date == contacted_on:
        return False

    conn.execute(
        """
        INSERT INTO crm_records (kod_obce, status, priority, contacted_on, updated_at)
        VALUES (?, 'Kontaktovaná', 'Střední', ?, now())
        ON CONFLICT (kod_obce) DO UPDATE SET
            contacted_on = excluded.contacted_on,
            status = CASE
                WHEN crm_records.status IN ('Nová', 'K oslovení') THEN 'Kontaktovaná'
                ELSE crm_records.status
            END,
            updated_at = now()
        """,
        [kod_obce, contacted_on],
    )
    if contacted_on is not None:
        add_activity(
            conn,
            kod_obce,
            "E-mail",
            "Odeslán e-mail",
            f"Datum oslovení: {contacted_on:%d.%m.%Y}",
            username,
        )
    return True


def update_municipality(conn, kod_obce, name, district, region, web, email, phone, ico):
    conn.execute(
        """
        UPDATE obce SET
            nazev = ?, okres = ?, kraj = ?, web = ?, email = ?, telefon = ?, ico = ?
        WHERE kod_obce = ?
        """,
        [
            name.strip(), district.strip() or None, region.strip() or None,
            web.strip() or None, email.strip() or None, phone.strip() or None,
            ico.strip() or None, kod_obce,
        ],
    )


def update_inline_crm(conn, kod_obce, status, owner_username):
    conn.execute(
        """
        INSERT INTO crm_records (kod_obce, status, priority, owner_username, updated_at)
        VALUES (?, ?, 'Střední', ?, now())
        ON CONFLICT (kod_obce) DO UPDATE SET
            status = excluded.status,
            owner_username = excluded.owner_username,
            updated_at = now()
        """,
        [kod_obce, status, owner_username or None],
    )


def add_task(conn, kod_obce, title, due_date, assigned_to, username):
    conn.execute(
        """
        INSERT INTO crm_tasks
            (id, kod_obce, title, due_date, assigned_to, created_by)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [str(uuid4()), kod_obce, title, due_date, assigned_to or None, username],
    )


def set_task_completed(conn, task_id, completed):
    conn.execute(
        """
        UPDATE crm_tasks
        SET completed = ?,
            completed_at = CASE WHEN ? THEN now() ELSE NULL END
        WHERE id = ?
        """,
        [completed, completed, task_id],
    )


def empty_date(value):
    return value if value is not None else date.today()
