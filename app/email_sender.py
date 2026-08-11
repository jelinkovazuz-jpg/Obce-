import base64
import imaplib
import re
import smtplib
import time
from email.message import EmailMessage
from email.utils import make_msgid
from uuid import uuid4

from app.email_sync import (
    EmailSyncError,
    IMAP_HOST,
    IMAP_PORT,
    _sent_mailbox,
    login_imap,
)


SMTP_HOST = "smtp.seznam.cz"
SMTP_PORT = 465
MAX_BATCH_SIZE = 30
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class EmailSendError(RuntimeError):
    pass


def login_smtp(connection, address, password):
    """Log in while supporting UTF-8 passwords on Python 3.14+ SMTP."""
    try:
        return connection.login(address, password)
    except UnicodeEncodeError:
        credentials = f"\0{address}\0{password}".encode("utf-8")
        encoded = base64.b64encode(credentials).decode("ascii")
        code, response = connection.docmd("AUTH", "PLAIN " + encoded)
        if code != 235:
            raise smtplib.SMTPAuthenticationError(code, response)
        return code, response


def init_email_sender(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crm_send_log (
            id VARCHAR PRIMARY KEY, batch_id VARCHAR NOT NULL,
            kod_obce INTEGER NOT NULL, recipient VARCHAR NOT NULL,
            subject VARCHAR, body_text VARCHAR, status VARCHAR NOT NULL,
            error VARCHAR, sent_by VARCHAR,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMP
        )
    """)


def _personalize(template, recipient):
    values = {
        "{{obec}}": recipient.get("name", ""),
        "{{okres}}": recipient.get("district", ""),
        "{{kraj}}": recipient.get("region", ""),
        "{{email}}": recipient.get("email", ""),
    }
    result = template
    for placeholder, value in values.items():
        result = result.replace(placeholder, value or "")
    return result


def _record_success(conn, batch_id, recipient, subject, body, username, message_id):
    code, address = recipient["code"], recipient["email"].strip().lower()
    conn.execute("""
        INSERT INTO crm_send_log
            (id,batch_id,kod_obce,recipient,subject,body_text,status,sent_by,sent_at)
        VALUES (?, ?, ?, ?, ?, ?, 'Odesláno', ?, now())
    """, [str(uuid4()), batch_id, code, address, subject, body, username])
    conn.execute("""
        INSERT INTO crm_emails
            (message_id,kod_obce,direction,sender,recipients,subject,body_text,
             attachments,sent_at)
        VALUES (?,?,'Odesláno',?,?,?,?, '[]',now())
        ON CONFLICT DO NOTHING
    """, [message_id, code, recipient["sender"], address, subject, body])
    conn.execute("""
        INSERT INTO crm_email_sync (message_id,recipient,kod_obce,sent_at,subject)
        VALUES (?,?,?,now(),?) ON CONFLICT DO NOTHING
    """, [message_id, address, code, subject])
    conn.execute("""
        INSERT INTO crm_activities
            (id,kod_obce,activity_type,subject,detail,created_by,created_at)
        VALUES (uuid(),?,'E-mail',?,?,?,now())
    """, [code, subject, f"Komu: {address}", username])
    conn.execute("""
        INSERT INTO crm_records (kod_obce,status,priority,contacted_on,updated_at)
        VALUES (?,'Kontaktovaná','Střední',today(),now())
        ON CONFLICT (kod_obce) DO UPDATE SET
            contacted_on=today(),
            status=CASE WHEN crm_records.status IN ('Nová','K oslovení')
                        THEN 'Kontaktovaná' ELSE crm_records.status END,
            updated_at=now()
    """, [code])


def _record_failure(conn, batch_id, recipient, subject, body, username, error):
    conn.execute("""
        INSERT INTO crm_send_log
            (id,batch_id,kod_obce,recipient,subject,body_text,status,error,sent_by)
        VALUES (?, ?, ?, ?, ?, ?, 'Chyba', ?, ?)
    """, [str(uuid4()), batch_id, recipient["code"], recipient["email"],
            subject, body, str(error)[:1000], username])


def send_individual_messages(conn, sender, password, recipients, subject_template,
                             body_template, username, delay_seconds=1,
                             smtp_factory=smtplib.SMTP_SSL,
                             imap_factory=imaplib.IMAP4_SSL):
    init_email_sender(conn)
    if not recipients:
        raise EmailSendError("Není vybrán žádný příjemce.")
    if len(recipients) > MAX_BATCH_SIZE:
        raise EmailSendError(f"V jedné dávce lze odeslat nejvýše {MAX_BATCH_SIZE} zpráv.")
    if not subject_template.strip() or not body_template.strip():
        raise EmailSendError("Vyplňte předmět i text zprávy.")

    batch_id = str(uuid4())
    results = {"batch_id": batch_id, "sent": 0, "failed": 0, "details": []}
    smtp = None
    imap = None
    try:
        smtp = smtp_factory(SMTP_HOST, SMTP_PORT, timeout=20)
        login_smtp(smtp, sender, password)
        # Verify access to Sent before sending the first message. This avoids
        # starting a batch whose copies cannot be saved in the mailbox.
        imap = imap_factory(IMAP_HOST, IMAP_PORT)
        login_imap(imap, sender, password)
        sent_mailbox = _sent_mailbox(imap)
        for index, recipient in enumerate(recipients):
            address = recipient["email"].strip().lower()
            subject = _personalize(subject_template.strip(), recipient)
            body = _personalize(body_template.strip(), recipient)
            recipient["sender"] = sender
            if not EMAIL_PATTERN.match(address):
                error = "Neplatná e-mailová adresa"
                _record_failure(conn, batch_id, recipient, subject, body, username, error)
                results["failed"] += 1
                results["details"].append((recipient["name"], address, error))
                continue
            message = EmailMessage()
            message["From"] = sender
            message["To"] = address
            message["Subject"] = subject
            message["Message-ID"] = make_msgid(domain="email.cz")
            message.set_content(body)
            try:
                smtp.send_message(message, from_addr=sender, to_addrs=[address])
            except (smtplib.SMTPException, OSError) as exc:
                _record_failure(conn, batch_id, recipient, subject, body, username, exc)
                results["failed"] += 1
                results["details"].append((recipient["name"], address, str(exc)))
            else:
                _record_success(conn, batch_id, recipient, subject, body, username,
                                message["Message-ID"])
                results["sent"] += 1
                try:
                    append_status, _ = imap.append(
                        sent_mailbox,
                        r"(\Seen)",
                        imaplib.Time2Internaldate(time.time()),
                        message.as_bytes(),
                    )
                except (imaplib.IMAP4.error, OSError) as exc:
                    append_status = "NO"
                    append_error = str(exc)
                else:
                    append_error = ""
                if append_status == "OK":
                    result_text = "Odesláno a uloženo v Odeslaných"
                else:
                    result_text = "Odesláno, ale kopii se nepodařilo uložit"
                    conn.execute("""
                        UPDATE crm_send_log SET error=?
                        WHERE batch_id=? AND kod_obce=? AND recipient=?
                    """, [append_error or "IMAP APPEND odmítnut", batch_id,
                            recipient["code"], address])
                results["details"].append((recipient["name"], address, result_text))
            if index < len(recipients) - 1 and delay_seconds:
                time.sleep(delay_seconds)
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailSendError("Přihlášení k SMTP se nezdařilo. Zkontrolujte heslo.") from exc
    except (imaplib.IMAP4.error, EmailSyncError) as exc:
        raise EmailSendError(
            "Nelze otevřít složku Odeslané. Rozesílání nebylo zahájeno."
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailSendError("K odesílacímu serveru Seznamu se nepodařilo připojit.") from exc
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except (smtplib.SMTPException, OSError):
                pass
        if imap is not None:
            try:
                imap.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
    return results
