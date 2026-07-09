#!/usr/bin/env python3
"""
Simple synchronous HTTP-to-SMTP mail API.

- Python stdlib only
- Static Bearer token auth from env
- Synchronous SMTP submission to an existing authenticated MTA
- Success means the SMTP server accepted the message
"""

from __future__ import annotations

import hmac
import json
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def getenv_required(name: str) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def getenv_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Invalid integer environment variable: {name}") from exc


API_TOKEN = getenv_required("API_TOKEN")
LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = getenv_int("LISTEN_PORT", 8080)

SMTP_HOST = getenv_required("SMTP_HOST")
SMTP_PORT = getenv_int("SMTP_PORT", 587)
SMTP_USERNAME = getenv_required("SMTP_USERNAME")
SMTP_PASSWORD = getenv_required("SMTP_PASSWORD")
SMTP_TLS = os.getenv("SMTP_TLS", "starttls").lower()
SMTP_FROM_DEFAULT = getenv_required("SMTP_FROM_DEFAULT")
SMTP_TIMEOUT_SECONDS = getenv_int("SMTP_TIMEOUT_SECONDS", 30)

MAX_RECIPIENTS = getenv_int("MAX_RECIPIENTS", 20)
MAX_BODY_BYTES = getenv_int("MAX_BODY_BYTES", 1_048_576)

if SMTP_TLS not in {"none", "starttls", "ssl"}:
    raise RuntimeError("SMTP_TLS must be one of: none, starttls, ssl")


class BadRequest(Exception):
    pass


def json_response(handler: BaseHTTPRequestHandler, http_status: int, app_status: str) -> None:
    body = json.dumps({"status": app_status}, separators=(",", ":")).encode("utf-8")
    handler.send_response(http_status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def has_header_injection(value: str) -> bool:
    return "\r" in value or "\n" in value


def normalize_address(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise BadRequest(f"{field_name} must be a string")
    value = value.strip()
    if not value or has_header_injection(value):
        raise BadRequest(f"{field_name} is invalid")

    display_name, email_addr = parseaddr(value)
    if not email_addr or "@" not in email_addr or has_header_injection(email_addr):
        raise BadRequest(f"{field_name} is invalid")

    if display_name:
        return formataddr((display_name, email_addr))
    return email_addr


def normalize_address_list(value: Any, field_name: str, required: bool = False) -> list[str]:
    if value is None:
        if required:
            raise BadRequest(f"{field_name} is required")
        return []

    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise BadRequest(f"{field_name} must be a string or list of strings")

    normalized = [normalize_address(item, field_name) for item in values]
    normalized = [item for item in normalized if item]

    if required and not normalized:
        raise BadRequest(f"{field_name} is required")

    return normalized


def validate_payload(data: Any) -> tuple[EmailMessage, str, list[str]]:
    if not isinstance(data, dict):
        raise BadRequest("JSON body must be an object")

    if data.get("attachments"):
        raise BadRequest("attachments are not supported")

    from_addr = normalize_address(data.get("from") or SMTP_FROM_DEFAULT, "from")
    to_addrs = normalize_address_list(data.get("to"), "to", required=True)
    cc_addrs = normalize_address_list(data.get("cc"), "cc")
    bcc_addrs = normalize_address_list(data.get("bcc"), "bcc")
    recipients = to_addrs + cc_addrs + bcc_addrs

    if len(recipients) > MAX_RECIPIENTS:
        raise BadRequest("too many recipients")

    subject = data.get("subject")
    if not isinstance(subject, str) or not subject.strip() or has_header_injection(subject):
        raise BadRequest("subject is invalid")
    subject = subject.strip()

    text_body = data.get("text")
    html_body = data.get("html")
    if text_body is not None and not isinstance(text_body, str):
        raise BadRequest("text must be a string")
    if html_body is not None and not isinstance(html_body, str):
        raise BadRequest("html must be a string")
    if not text_body and not html_body:
        raise BadRequest("text or html is required")

    reply_to = data.get("reply_to")
    reply_to_addr = None
    if reply_to is not None:
        reply_to_addr = normalize_address(reply_to, "reply_to")

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    if reply_to_addr:
        msg["Reply-To"] = reply_to_addr
    msg["Subject"] = subject

    if text_body and html_body:
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")
    elif html_body:
        msg.set_content(html_body, subtype="html")
    else:
        msg.set_content(text_body or "")

    return msg, from_addr, recipients


def send_via_smtp(msg: EmailMessage, from_addr: str, recipients: list[str]) -> None:
    context = ssl.create_default_context()

    if SMTP_TLS == "ssl":
        smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS, context=context)
    else:
        smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS)

    try:
        if SMTP_TLS == "starttls":
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()

        smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
        smtp.send_message(msg, from_addr=from_addr, to_addrs=recipients)
    finally:
        try:
            smtp.quit()
        except Exception:
            smtp.close()


class MailAPIHandler(BaseHTTPRequestHandler):
    server_version = "SimpleMailAPI/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:
        if self.path == "/health":
            json_response(self, 200, "ok")
            return
        json_response(self, 404, "bad_request")

    def do_POST(self) -> None:
        if self.path != "/send":
            json_response(self, 404, "bad_request")
            return

        expected_auth = "Bearer " + API_TOKEN
        received_auth = self.headers.get("Authorization", "")
        if not hmac.compare_digest(received_auth, expected_auth):
            json_response(self, 401, "auth")
            return

        try:
            content_length_raw = self.headers.get("Content-Length")
            if content_length_raw is None:
                raise BadRequest("missing Content-Length")
            content_length = int(content_length_raw)
            if content_length < 0 or content_length > MAX_BODY_BYTES:
                raise BadRequest("body too large")

            raw_body = self.rfile.read(content_length)
            try:
                data = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BadRequest("invalid JSON") from exc

            msg, from_addr, recipients = validate_payload(data)
            send_via_smtp(msg, from_addr, recipients)
            json_response(self, 200, "ok")

        except BadRequest:
            json_response(self, 400, "bad_request")
        except (smtplib.SMTPException, OSError) as exc:
            print(json.dumps({"event": "smtp_error", "error_type": type(exc).__name__}), file=sys.stderr)
            json_response(self, 500, "smtp")
        except Exception as exc:
            print(json.dumps({"event": "internal_error", "error_type": type(exc).__name__}), file=sys.stderr)
            json_response(self, 500, "failed")


def main() -> None:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), MailAPIHandler)
    print(json.dumps({"event": "started", "listen": f"{LISTEN_HOST}:{LISTEN_PORT}"}), file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
