import email
import imaplib
import re
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime


IMAP_HOST = "imap.seznam.cz"
IMAP_PORT = 993


class EmailSyncError(RuntimeError):
    pass


def init_email_sync(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crm_email_sync (
            message_id VARCHAR NOT NULL,
            recipient VARCHAR NOT NULL,
            kod_obce INTEGER,
            sent_at TIMESTAMP,
            subject VARCHAR,
            synced_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (message_id, recipient)
        )
        """
    )


def _sent_mailbox(connection):
    status, mailboxes = connection.list()
    if status != "OK":
        raise EmailSyncError("Nepodařilo se načíst seznam složek schránky.")

    for row in mailboxes or []:
        if b"\\Sent" in row:
            match = re.search(rb" (\"(?:[^\"]|\\.)*\"|[^ ]+)$", row)
            if match:
                return match.group(1)

    for candidate in ("Sent", "Odeslane", "Odeslaná", "Odeslané"):
        status, _ = connection.select(candidate, readonly=True)
        if status == "OK":
            return candidate
    raise EmailSyncError("Ve schránce nebyla nalezena složka Odeslané.")


def _text_header(message, name):
    try:
        return str(make_header(decode_header(message.get(name, ""))))
    except (LookupError, UnicodeError):
        return message.get(name, "")


def sync_sent_mail(conn, address, password, username, limit=2000):
    """Read sent-message headers and match recipients to municipality e-mails."""
    init_email_sync(conn)
    stats = {"checked": 0, "matched": 0, "new": 0}
    mailbox = None
    try:
        mailbox = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mailbox.login(address, password)
        sent_box = _sent_mailbox(mailbox)
        status, _ = mailbox.select(sent_box, readonly=True)
        if status != "OK":
            raise EmailSyncError("Složku Odeslané se nepodařilo otevřít.")

        status, data = mailbox.uid("search", None, "ALL")
        if status != "OK":
            raise EmailSyncError("Odeslané zprávy se nepodařilo vyhledat.")
        uids = (data[0] or b"").split()[-limit:]

        municipality_emails = {
            row[0].strip().lower(): row[1]
            for row in conn.execute(
                """
                SELECT lower(trim(email)), min(kod_obce)
                FROM obce WHERE email IS NOT NULL AND trim(email) <> ''
                GROUP BY lower(trim(email))
                """
            ).fetchall()
        }

        for uid in uids:
            status, response = mailbox.uid(
                "fetch", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID DATE TO CC BCC SUBJECT)])"
            )
            if status != "OK" or not response:
                continue
            raw_header = next(
                (part[1] for part in response if isinstance(part, tuple) and len(part) > 1), None
            )
            if not raw_header:
                continue
            stats["checked"] += 1
            message = email.message_from_bytes(raw_header)
            message_id = message.get("Message-ID", "").strip() or f"imap:{uid.decode()}"
            subject = _text_header(message, "Subject")
            try:
                sent_at = parsedate_to_datetime(message.get("Date")).astimezone().replace(tzinfo=None)
            except (TypeError, ValueError, OverflowError):
                sent_at = datetime.now()

            address_headers = [
                value
                for value in (
                    message.get("To"), message.get("Cc"), message.get("Bcc")
                )
                if value
            ]
            recipients = {
                addr.strip().lower()
                for _, addr in getaddresses(address_headers)
                if addr
            }
            for recipient in recipients:
                kod_obce = municipality_emails.get(recipient)
                if kod_obce is None:
                    continue
                stats["matched"] += 1
                exists = conn.execute(
                    "SELECT 1 FROM crm_email_sync WHERE message_id = ? AND recipient = ?",
                    [message_id, recipient],
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    """
                    INSERT INTO crm_email_sync
                        (message_id, recipient, kod_obce, sent_at, subject)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [message_id, recipient, kod_obce, sent_at, subject],
                )
                conn.execute(
                    """
                    INSERT INTO crm_activities
                        (id, kod_obce, activity_type, subject, detail, created_by, created_at)
                    VALUES (uuid(), ?, 'E-mail', ?, ?, ?, ?)
                    """,
                    [kod_obce, subject or "Odeslán e-mail", f"Komu: {recipient}", username, sent_at],
                )
                conn.execute(
                    """
                    INSERT INTO crm_records
                        (kod_obce, status, priority, contacted_on, updated_at)
                    VALUES (?, 'Kontaktovaná', 'Střední', ?, now())
                    ON CONFLICT (kod_obce) DO UPDATE SET
                        contacted_on = greatest(coalesce(crm_records.contacted_on, excluded.contacted_on), excluded.contacted_on),
                        status = CASE WHEN crm_records.status IN ('Nová', 'K oslovení')
                                      THEN 'Kontaktovaná' ELSE crm_records.status END,
                        updated_at = now()
                    """,
                    [kod_obce, sent_at.date()],
                )
                stats["new"] += 1
    except imaplib.IMAP4.error as exc:
        raise EmailSyncError(
            "Přihlášení k e-mailu se nezdařilo. Zkontrolujte heslo nebo heslo pro aplikaci."
        ) from exc
    except OSError as exc:
        raise EmailSyncError("K serveru Seznam.cz se nepodařilo připojit.") from exc
    finally:
        if mailbox is not None:
            try:
                mailbox.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
    return stats
