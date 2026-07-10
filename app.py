#!/usr/bin/env python3
"""
Simple synchronous HTTP-to-SMTP mail API.

- Python stdlib only
- Static Bearer token auth from env
- Auto-probes SMTP server to discover working TLS / auth combination
- Caches discovered settings in a JSON config file
- Success means the SMTP server accepted the message
"""

from __future__ import annotations

import hmac
import json
import os
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from email import policy
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

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

# SMTP — only SMTP_HOST is required; the rest are optional / auto-probed.
SMTP_HOST = getenv_required("SMTP_HOST")
_SMTP_PORT_HINT = os.getenv("SMTP_PORT")          # optional — narrows probing
_SMTP_PORT_HINT_INT = int(_SMTP_PORT_HINT) if _SMTP_PORT_HINT else None
SMTP_USERNAME = os.getenv("SMTP_USERNAME") or None
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD") or None
SMTP_FROM_DEFAULT = os.getenv("SMTP_FROM_DEFAULT") or SMTP_USERNAME or ""

SMTP_CONFIG_PATH = os.getenv("SMTP_CONFIG_PATH", "/app/config/smtp.json")
SMTP_PROBE_TIMEOUT = getenv_int("SMTP_PROBE_TIMEOUT", 10)  # seconds per probe attempt
SMTP_PROBE_DELAY = getenv_int("SMTP_PROBE_DELAY", 3)        # seconds between probes
SMTP_RUNTIME_TIMEOUT = getenv_int("SMTP_RUNTIME_TIMEOUT", 30)  # seconds for normal send

MAX_RECIPIENTS = getenv_int("MAX_RECIPIENTS", 20)
MAX_BODY_BYTES = getenv_int("MAX_BODY_BYTES", 1_048_576)

# SSL — never verify certificates (compatibility with self-signed / open-relay certs).
_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.check_hostname = False
_SSL_CONTEXT.verify_mode = ssl.CERT_NONE

_HAS_CREDENTIALS = bool(SMTP_USERNAME and SMTP_PASSWORD)


# ---------------------------------------------------------------------------
# SMTP config file (auto-probed, cached to disk)
# ---------------------------------------------------------------------------

def _config_key() -> str:
    """Key used to store/retrieve per-server settings.

    Normally just the hostname.  When SMTP_PORT is explicitly set the key
    includes the port so that adding a port later does not overwrite a
    previously discovered entry for the bare hostname.
    """
    if _SMTP_PORT_HINT is not None:
        return f"{SMTP_HOST}:{_SMTP_PORT_HINT}"
    return SMTP_HOST


def _load_config() -> dict[str, Any]:
    try:
        with open(SMTP_CONFIG_PATH) as fh:
            return json.load(fh)
    except (FileNotFoundError, IsADirectoryError, json.JSONDecodeError):
        return {}


def _save_config(settings: dict[str, Any]) -> None:
    cfg = _load_config()
    cfg[_config_key()] = settings
    os.makedirs(os.path.dirname(SMTP_CONFIG_PATH) or ".", exist_ok=True)
    with open(SMTP_CONFIG_PATH, "w") as fh:
        json.dump(cfg, fh, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Probe carousel
# ---------------------------------------------------------------------------

def _probe_one(host: str, port: int, tls: str, use_auth: bool, retry: bool = True) -> dict[str, Any] | None:
    """Try a single host:port:TLS:auth combination.  Return settings dict or None.

    On transient failures (rate limiting) a second attempt is made after a
    short delay.
    """
    for attempt in (1, 2):
        try:
            if tls == "ssl":
                smtp = smtplib.SMTP_SSL(host, port, timeout=SMTP_PROBE_TIMEOUT, context=_SSL_CONTEXT)
            else:
                smtp = smtplib.SMTP(host, port, timeout=SMTP_PROBE_TIMEOUT)

            try:
                if tls == "starttls":
                    smtp.ehlo()
                    smtp.starttls(context=_SSL_CONTEXT)
                    smtp.ehlo()

                if use_auth:
                    assert SMTP_USERNAME and SMTP_PASSWORD  # nosec – guarded by caller
                    smtp.login(SMTP_USERNAME, SMTP_PASSWORD)

                # Verify the session is alive.
                smtp.docmd("NOOP")

                # Record which AUTH mechanisms the server advertises.
                auth_mech = ""
                if use_auth:
                    auth_mech = smtp.esmtp_features.get("auth", "")

                return {
                    "port": port,
                    "tls": tls,
                    "auth_required": use_auth,
                    "auth_mechanism": auth_mech.strip(),
                }
            finally:
                try:
                    smtp.quit()
                except Exception:
                    smtp.close()
        except Exception as exc:
            if attempt == 1 and retry and "reset" in str(exc).lower():
                time.sleep(5)  # rate-limit cooldown
                continue
            print(json.dumps({"event": "probe_fail", "host": host, "port": port, "tls": tls, "auth": use_auth, "error": str(exc)}), file=sys.stderr)
            return None
    return None


def _probe_carousel(host: str) -> dict[str, Any]:
    """Try port / TLS / auth combinations ordered by likelihood; return first winner."""
    ports = [_SMTP_PORT_HINT_INT] if _SMTP_PORT_HINT_INT else [587, 465, 25]

    has_creds = _HAS_CREDENTIALS

    if has_creds:
        combos: list[tuple[int, str, bool]] = [
            (587, "starttls", True),
            (587, "none",     True),
            (465, "ssl",      True),
            (25,  "none",     True),
            (25,  "starttls", True),
            (25,  "none",     False),   # open-relay fallback
        ]
    else:
        combos = [
            (25,  "none",     False),
            (587, "starttls", False),
            (587, "none",     False),
            (465, "ssl",      False),
            (25,  "starttls", False),
        ]

    tried: list[str] = []
    first = True
    for port, tls, use_auth in combos:
        if not first:
            time.sleep(SMTP_PROBE_DELAY)  # avoid SMTP server rate limiting
        first = False
        if port not in ports:
            continue
        label = f"{host}:{port} TLS={tls} auth={'yes' if use_auth else 'no'}"
        tried.append(label)
        result = _probe_one(host, port, tls, use_auth)
        if result is not None:
            print(json.dumps({"event": "probe_success", "host": host, **result}), file=sys.stderr)
            return result

    # Nothing worked.
    msg = f"No working SMTP configuration found for {host}. Tried: {', '.join(tried)}"
    raise RuntimeError(msg)


# resolved lazily — see get_smtp_settings()


_SMTP_SETTINGS: dict[str, Any] | None = None


def get_smtp_settings() -> dict[str, Any]:
    """Return {host, port, tls, auth_required, auth_mechanism} for SMTP_HOST.

    Resolved lazily on first call so that probe tests can import the module
    without triggering the carousel.
    """
    global _SMTP_SETTINGS
    if _SMTP_SETTINGS is not None:
        return _SMTP_SETTINGS

    cfg = _load_config()
    key = _config_key()
    existing = cfg.get(key)

    if existing is not None:
        if all(k in existing for k in ("port", "tls", "auth_required")):
            print(json.dumps({"event": "smtp_config_loaded", "key": key, **existing}), file=sys.stderr)
            _SMTP_SETTINGS = existing
            return _SMTP_SETTINGS
        print(json.dumps({"event": "smtp_config_stale", "key": key}), file=sys.stderr)

    settings = _probe_carousel(SMTP_HOST)
    settings["host"] = SMTP_HOST
    _save_config(settings)
    print(json.dumps({"event": "smtp_config_saved", "key": key}), file=sys.stderr)
    _SMTP_SETTINGS = settings
    return _SMTP_SETTINGS


# ---------------------------------------------------------------------------
# Exceptions / helpers
# ---------------------------------------------------------------------------

class BadRequest(Exception):
    pass


def json_response(handler: BaseHTTPRequestHandler, http_status: int, app_status: str, detail: str | None = None) -> None:
    payload: dict[str, str] = {"status": app_status}
    if detail:
        payload["detail"] = detail
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(http_status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def has_header_injection(value: str) -> bool:
    return "\r" in value or "\n" in value


# ---------------------------------------------------------------------------
# Address validation
# ---------------------------------------------------------------------------

def normalize_address(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise BadRequest(f"{field_name} must be a string")
    value = value.strip()
    if not value or has_header_injection(value):
        raise BadRequest(f"{field_name} is invalid")

    display_name, email_addr = parseaddr(value)
    if not email_addr or "@" not in email_addr or email_addr.startswith("@") or has_header_injection(email_addr):
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

    if required and not normalized:
        raise BadRequest(f"{field_name} is required")

    return normalized


# ---------------------------------------------------------------------------
# Payload → EmailMessage
# ---------------------------------------------------------------------------

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

    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    if reply_to_addr:
        msg["Reply-To"] = reply_to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=parseaddr(from_addr)[1].split("@")[-1] or "localhost")

    if text_body and html_body:
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")
    elif html_body:
        msg.set_content(html_body, subtype="html")
    else:
        msg.set_content(text_body or "")

    return msg, from_addr, recipients


# ---------------------------------------------------------------------------
# SMTP send
# ---------------------------------------------------------------------------

def send_via_smtp(msg: EmailMessage, from_addr: str, recipients: list[str]) -> None:
    s = get_smtp_settings()
    host: str = s["host"]
    port: int = s["port"]
    tls: str = s["tls"]

    if tls == "ssl":
        smtp = smtplib.SMTP_SSL(host, port, timeout=SMTP_RUNTIME_TIMEOUT, context=_SSL_CONTEXT)
    else:
        smtp = smtplib.SMTP(host, port, timeout=SMTP_RUNTIME_TIMEOUT)

    try:
        if tls == "starttls":
            smtp.ehlo()
            smtp.starttls(context=_SSL_CONTEXT)
            smtp.ehlo()

        if s.get("auth_required"):
            if not SMTP_USERNAME or not SMTP_PASSWORD:
                raise RuntimeError("SMTP server requires auth but SMTP_USERNAME/SMTP_PASSWORD not set")
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)

        smtp.send_message(msg, from_addr=from_addr, to_addrs=recipients)
    finally:
        try:
            smtp.quit()
        except Exception:
            smtp.close()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

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
            try:
                content_length = int(content_length_raw)
            except ValueError:
                raise BadRequest("invalid Content-Length")
            if content_length < 0 or content_length > MAX_BODY_BYTES:
                raise BadRequest("body too large")

            raw_body = self.rfile.read(content_length)
            try:
                data = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BadRequest("invalid JSON") from exc

            msg, from_addr, recipients = validate_payload(data)
            send_via_smtp(msg, from_addr, recipients)

        except BadRequest as exc:
            json_response(self, 400, "bad_request", detail=str(exc))
            return
        except (smtplib.SMTPException, OSError) as exc:
            print(json.dumps({"event": "smtp_error", "error_type": type(exc).__name__}), file=sys.stderr)
            json_response(self, 500, "smtp")
            return
        except Exception as exc:
            print(json.dumps({"event": "internal_error", "error_type": type(exc).__name__}), file=sys.stderr)
            json_response(self, 500, "failed")
            return

        # SMTP accepted the message — respond to client
        try:
            json_response(self, 200, "ok")
        except OSError:
            pass  # client disconnected; email was already sent


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # Resolve SMTP settings eagerly so misconfiguration fails fast.
    get_smtp_settings()

    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), MailAPIHandler)
    print(json.dumps({"event": "started", "listen": f"{LISTEN_HOST}:{LISTEN_PORT}"}), file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
