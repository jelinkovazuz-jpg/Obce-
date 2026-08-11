import email
import imaplib
import json
import re
from datetime import datetime
from email import policy
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

IMAP_HOST = "imap.seznam.cz"
IMAP_PORT = 993


class EmailSyncError(RuntimeError):
    pass


class _HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        if data.strip():
            self.parts.append(data.strip())

    def text(self):
        return "\n".join(self.parts)


def init_email_sync(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crm_email_sync (
            message_id VARCHAR NOT NULL, recipient VARCHAR NOT NULL,
            kod_obce INTEGER, sent_at TIMESTAMP, subject VARCHAR,
            synced_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (message_id, recipient)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crm_emails (
            message_id VARCHAR NOT NULL, kod_obce INTEGER NOT NULL,
            direction VARCHAR NOT NULL, sender VARCHAR, recipients VARCHAR,
            subject VARCHAR, body_text VARCHAR, attachments VARCHAR,
            sent_at TIMESTAMP, synced_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (message_id, kod_obce, direction)
        )
    """)


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
        return str(message.get(name, ""))


def _addresses(message, headers):
    values = [message.get(name) for name in headers if message.get(name)]
    return {address.strip().lower() for _, address in getaddresses(values) if address}


def _body_and_attachments(message):
    plain_parts, html_parts, attachments = [], [], []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        filename = part.get_filename()
        if filename:
            try:
                attachments.append(str(make_header(decode_header(filename))))
            except (LookupError, UnicodeError):
                attachments.append(str(filename))
            continue
        if part.get_content_maintype() != "text":
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError, AttributeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/plain":
            plain_parts.append(str(content))
        elif part.get_content_type() == "text/html":
            parser = _HTMLTextExtractor()
            parser.feed(str(content))
            html_parts.append(parser.text())
    return "\n\n".join(plain_parts or html_parts).strip()[:200_000], attachments


def _message_date(message):
    try:
        value = parsedate_to_datetime(message.get("Date"))
        return value.astimezone().replace(tzinfo=None) if value.tzinfo else value
    except (TypeError, ValueError, OverflowError):
        return datetime.now()


def _sync_folder(connection, conn, folder, direction, email_map, username, limit, stats):
    status, _ = connection.select(folder, readonly=True)
    if status != "OK":
        raise EmailSyncError(f"Složku {direction} se nepodařilo otevřít.")
    status, data = connection.uid("search", None, "ALL")
    if status != "OK":
        raise EmailSyncError(f"Zprávy ve složce {direction} se nepodařilo vyhledat.")
    for uid in (data[0] or b"").split()[-limit:]:
        status, response = connection.uid("fetch", uid, "(BODY.PEEK[])")
        raw_message = next((p[1] for p in response or [] if isinstance(p, tuple)), None)
        if status != "OK" or not raw_message:
            continue
        stats["checked"] += 1
        message = email.message_from_bytes(raw_message, policy=policy.default)
        message_id = message.get("Message-ID", "").strip() or f"{direction}:imap:{uid.decode()}"
        senders = _addresses(message, ["From", "Reply-To"])
        recipients = _addresses(message, ["To", "Cc", "Bcc"])
        match_addresses = recipients if direction == "Odesláno" else senders
        codes = {code for address in match_addresses for code in email_map.get(address, [])}
        if not codes:
            continue
        sent_at, subject = _message_date(message), _text_header(message, "Subject") or "(bez předmětu)"
        body, attachments = _body_and_attachments(message)
        sender_text, recipient_text = ", ".join(sorted(senders)), ", ".join(sorted(recipients))
        for kod_obce in codes:
            stats["matched"] += 1
            if conn.execute("""
                SELECT 1 FROM crm_emails
                WHERE message_id=? AND kod_obce=? AND direction=?
            """, [message_id, kod_obce, direction]).fetchone():
                continue
            conn.execute("""
                INSERT INTO crm_emails
                    (message_id,kod_obce,direction,sender,recipients,subject,
                     body_text,attachments,sent_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, [message_id, kod_obce, direction, sender_text, recipient_text,
                    subject, body, json.dumps(attachments, ensure_ascii=False), sent_at])
            stats["new"] += 1
            stats["sent" if direction == "Odesláno" else "received"] += 1
            if direction == "Odesláno":
                recipient = next((a for a in recipients if kod_obce in email_map.get(a, [])), "")
                old_marker = conn.execute("""
                    SELECT 1 FROM crm_email_sync WHERE message_id=? AND recipient=?
                """, [message_id, recipient]).fetchone()
                if not old_marker:
                    conn.execute("""
                        INSERT INTO crm_email_sync
                            (message_id,recipient,kod_obce,sent_at,subject)
                        VALUES (?,?,?,?,?)
                    """, [message_id, recipient, kod_obce, sent_at, subject])
                    conn.execute("""
                        INSERT INTO crm_activities
                            (id,kod_obce,activity_type,subject,detail,created_by,created_at)
                        VALUES (uuid(),?,'E-mail',?,?,?,?)
                    """, [kod_obce, subject, f"Komu: {recipient}", username, sent_at])
                conn.execute("""
                    INSERT INTO crm_records
                        (kod_obce,status,priority,contacted_on,updated_at)
                    VALUES (?,'Kontaktovaná','Střední',?,now())
                    ON CONFLICT (kod_obce) DO UPDATE SET
                        contacted_on=greatest(coalesce(crm_records.contacted_on,
                            excluded.contacted_on),excluded.contacted_on),
                        status=CASE WHEN crm_records.status IN ('Nová','K oslovení')
                            THEN 'Kontaktovaná' ELSE crm_records.status END,
                        updated_at=now()
                """, [kod_obce, sent_at.date()])
            else:
                conn.execute("""
                    INSERT INTO crm_activities
                        (id,kod_obce,activity_type,subject,detail,created_by,created_at)
                    VALUES (uuid(),?,'E-mail',?,?,?,?)
                """, [kod_obce, subject, f"Od: {sender_text}", username, sent_at])


def sync_mailbox(conn, address, password, username, limit=2000):
    init_email_sync(conn)
    stats = {"checked": 0, "matched": 0, "new": 0, "sent": 0, "received": 0}
    mailbox = None
    try:
        mailbox = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mailbox.login(address, password)
        sent_box = _sent_mailbox(mailbox)
        email_map = {}
        for municipality_email, code in conn.execute("""
            SELECT lower(trim(email)),kod_obce FROM obce
            WHERE email IS NOT NULL AND trim(email)<>''
        """).fetchall():
            email_map.setdefault(municipality_email, []).append(code)
        _sync_folder(mailbox, conn, "INBOX", "Přijato", email_map, username, limit, stats)
        _sync_folder(mailbox, conn, sent_box, "Odesláno", email_map, username, limit, stats)
    except imaplib.IMAP4.error as exc:
        raise EmailSyncError("Přihlášení k e-mailu se nezdařilo. Zkontrolujte heslo.") from exc
    except OSError as exc:
        raise EmailSyncError("K serveru Seznam.cz se nepodařilo připojit.") from exc
    finally:
        if mailbox is not None:
            try:
                mailbox.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
    return stats
