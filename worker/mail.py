"""E-mails the Studio sends itself: the password reset link and 'your password was changed'.

Sent through Gmail (or any SMTP server) with the settings in .env:
  SMTP_USER=studio.sender@gmail.com     the sending Gmail
  SMTP_PASSWORD=abcd efgh ijkl mnop     its 16-letter app password (Google Account -> Security -> App passwords)
  MAIL_FROM=Vid Studio <studio.sender@gmail.com>   optional, the name people see
  SMTP_HOST=smtp.gmail.com  SMTP_PORT=465          optional, these are the defaults
The password never leaves .env and is never logged.
"""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

import pipeline


class MailError(Exception):
    pass


def configured():
    return bool(pipeline.env("SMTP_USER") and pipeline.env("SMTP_PASSWORD"))


def send(to, subject, text):
    """Plain-text e-mail. Raises MailError (without the password or server answer details) on failure."""
    if not configured():
        raise MailError("e-mail is not set up (SMTP_USER and SMTP_PASSWORD in .env)")
    user = pipeline.env("SMTP_USER")
    name, addr = parseaddr(pipeline.env("MAIL_FROM") or "")
    msg = EmailMessage()
    msg["From"] = formataddr((name or "Vid Studio", addr or user))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    host, port = pipeline.env("SMTP_HOST", "smtp.gmail.com"), int(pipeline.env("SMTP_PORT", "465"))
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.starttls(context=ssl.create_default_context())
        with server:
            server.login(user, pipeline.env("SMTP_PASSWORD").replace(" ", ""))
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise MailError("the mail server refused the login - check SMTP_USER and the app password")
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"cannot send e-mail: {type(e).__name__}")
    print(f"[mail] sent “{subject}” to {to}", flush=True)
