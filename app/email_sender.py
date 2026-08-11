import re
import smtplib
import time
from email.message import EmailMessage
from email.utils import make_msgid
from uuid import uuid4


SMTP_HOST = "smtp.seznam.cz"
SMTP_PORT = 465
MAX_BATCH_SIZE = 30
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class EmailSendError(RuntimeError):
    pass


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
                             smtp_factory=smtplib.SMTP_SSL):
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
    try:
        smtp = smtp_factory(SMTP_HOST, SMTP_PORT, timeout=20)
        smtp.login(sender, password)
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
                results["details"].append((recipient["name"], address, "Odesláno"))
            if index < len(recipients) - 1 and delay_seconds:
                time.sleep(delay_seconds)
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailSendError("Přihlášení k SMTP se nezdařilo. Zkontrolujte heslo.") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailSendError("K odesílacímu serveru Seznamu se nepodařilo připojit.") from exc
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except (smtplib.SMTPException, OSError):
                pass
    return results
