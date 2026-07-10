#!/usr/bin/env python3
"""
End-to-end test suite for the Simple Mail API.

Covers HTTP endpoints, authentication, input validation, error handling,
and SMTP delivery against a real MTA.

Usage:
  # Run all tests (SMTP tests run only if TEST_SMTP_* env vars are set):
  python3 test_app.py

  # Run with real SMTP:
  TEST_SMTP_HOST=mail.example.com                  \
  TEST_SMTP_PORT=587                              \
  TEST_SMTP_USERNAME=user@example.com       \
  TEST_SMTP_PASSWORD=password \
  TEST_SMTP_FROM_DEFAULT=noreply@example.com        \
  python3 test_app.py
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from typing import Any


def _env_or(name: str, default: str) -> str:
    return os.environ.get(name, default)


# ---------------------------------------------------------------------------
# Configuration for the server-under-test
# ---------------------------------------------------------------------------
API_TOKEN = "test-token-abc123"
LISTEN_PORT = "18080"

BASE_ENV = {
    "API_TOKEN": API_TOKEN,
    "LISTEN_HOST": "127.0.0.1",
    "LISTEN_PORT": LISTEN_PORT,
    # Dummy SMTP – validation-only tests never reach the network.
    # Pre-populate config file so the carousel is skipped.
    "SMTP_HOST": "127.0.0.1",
    "SMTP_USERNAME": "dummy",
    "SMTP_PASSWORD": "dummy",
    "SMTP_FROM_DEFAULT": "noreply@example.com",
    "SMTP_CONFIG_PATH": "/tmp/test_smtp_config_dummy.json",
    "SMTP_PROBE_DELAY": "0",   # no delay in tests
    "MAX_RECIPIENTS": "5",
}


def _write_dummy_config() -> None:
    """Pre-populate config so the carousel doesn't run for validation tests."""
    import json as _json
    os.makedirs(os.path.dirname(BASE_ENV["SMTP_CONFIG_PATH"]) or ".", exist_ok=True)
    with open(BASE_ENV["SMTP_CONFIG_PATH"], "w") as fh:
        _json.dump({BASE_ENV["SMTP_HOST"]: {"port": 1, "tls": "none", "auth_required": True, "auth_mechanism": "PLAIN"}}, fh)


def _has_real_smtp() -> bool:
    return bool(
        os.environ.get("TEST_SMTP_HOST")
        and os.environ.get("TEST_SMTP_USERNAME")
        and os.environ.get("TEST_SMTP_PASSWORD")
        and os.environ.get("TEST_SMTP_FROM_DEFAULT")
    )


def _real_smtp_env() -> dict[str, str]:
    """Return env dict with real SMTP credentials for an E2E delivery test."""
    return {
        **BASE_ENV,
        "LISTEN_PORT": "18081",  # separate port to avoid conflict with main test server
        "SMTP_HOST": os.environ["TEST_SMTP_HOST"],
        "SMTP_USERNAME": os.environ["TEST_SMTP_USERNAME"],
        "SMTP_PASSWORD": os.environ["TEST_SMTP_PASSWORD"],
        "SMTP_FROM_DEFAULT": os.environ["TEST_SMTP_FROM_DEFAULT"],
        "SMTP_CONFIG_PATH": "/tmp/test_smtp_config_e2e.json",
        "SMTP_PROBE_DELAY": "2",
        "SMTP_PROBE_TIMEOUT": "10",
    }

E2E_PORT = "18081"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _http(method: str, path: str, body: Any = None, headers: dict[str, str] | None = None, timeout: int = 10, port: str = LISTEN_PORT) -> tuple[int, dict[str, Any]]:
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------
class TestHealthEndpoint(unittest.TestCase):
    """GET /health – does not require auth."""

    def test_health_returns_ok(self) -> None:
        status, body = _http("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_health_with_query_string_returns_404(self) -> None:
        url = f"http://127.0.0.1:{LISTEN_PORT}/health?foo=bar"
        try:
            urllib.request.urlopen(url, timeout=5)
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)


class TestAuthentication(unittest.TestCase):
    """401 / auth checks."""

    def test_no_auth_header(self) -> None:
        status, body = _http(
            "POST",
            "/send",
            {"to": ["a@b.com"], "subject": "S", "text": "T"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["status"], "auth")

    def test_wrong_token(self) -> None:
        hdrs = {"Authorization": "Bearer wrong-token", "Content-Type": "application/json"}
        status, body = _http(
            "POST",
            "/send",
            {"to": ["a@b.com"], "subject": "S", "text": "T"},
            headers=hdrs,
        )
        self.assertEqual(status, 401)

    def test_missing_bearer_prefix(self) -> None:
        hdrs = {"Authorization": API_TOKEN, "Content-Type": "application/json"}
        status, body = _http(
            "POST",
            "/send",
            {"to": ["a@b.com"], "subject": "S", "text": "T"},
            headers=hdrs,
        )
        self.assertIn(status, (401, 403))


class TestInputValidation(unittest.TestCase):
    """400 / bad_request checks — all tests use dummy SMTP that never connects."""

    def test_missing_body_fields(self) -> None:
        status, body = _http("POST", "/send", {}, headers=_auth_headers())
        self.assertEqual(status, 400)

    def test_to_is_required(self) -> None:
        status, body = _http(
            "POST", "/send",
            {"subject": "Hi", "text": "Body"},
            headers=_auth_headers(),
        )
        self.assertEqual(status, 400)
        self.assertIn("detail", body)

    def test_invalid_email_rejected(self) -> None:
        status, body = _http(
            "POST", "/send",
            {"to": ["not-an-email"], "subject": "Hi", "text": "Body"},
            headers=_auth_headers(),
        )
        self.assertEqual(status, 400)

    def test_empty_local_part_rejected(self) -> None:
        status, body = _http(
            "POST", "/send",
            {"to": ["@example.com"], "subject": "Hi", "text": "Body"},
            headers=_auth_headers(),
        )
        self.assertEqual(status, 400)

    def test_missing_subject(self) -> None:
        status, body = _http(
            "POST", "/send",
            {"to": ["a@b.com"], "text": "Body"},
            headers=_auth_headers(),
        )
        self.assertEqual(status, 400)

    def test_missing_text_and_html(self) -> None:
        status, body = _http(
            "POST", "/send",
            {"to": ["a@b.com"], "subject": "Hi"},
            headers=_auth_headers(),
        )
        self.assertEqual(status, 400)

    def test_subject_with_crlf_rejected(self) -> None:
        status, body = _http(
            "POST", "/send",
            {"to": ["a@b.com"], "subject": "Hi\r\nBcc:spam@x.com", "text": "B"},
            headers=_auth_headers(),
        )
        self.assertEqual(status, 400)

    def test_non_string_text_rejected(self) -> None:
        status, body = _http(
            "POST", "/send",
            {"to": ["a@b.com"], "subject": "Hi", "text": 123},
            headers=_auth_headers(),
        )
        self.assertEqual(status, 400)

    def test_too_many_recipients(self) -> None:
        status, body = _http(
            "POST", "/send",
            {
                "to": [f"user{i}@example.com" for i in range(6)],
                "subject": "Hi",
                "text": "Body",
            },
            headers=_auth_headers(),
        )
        self.assertEqual(status, 400)

    def test_invalid_json(self) -> None:
        url = f"http://127.0.0.1:{LISTEN_PORT}/send"
        req = urllib.request.Request(url, data=b"not json", method="POST")
        for k, v in _auth_headers().items():
            req.add_header(k, v)
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_non_numeric_content_length(self) -> None:
        url = f"http://127.0.0.1:{LISTEN_PORT}/send"
        req = urllib.request.Request(url, data=b"{}", method="POST")
        for k, v in _auth_headers().items():
            req.add_header(k, v)
        req.add_header("Content-Length", "abc")
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_body_too_large(self) -> None:
        huge = {"to": ["a@b.com"], "subject": "Hi", "text": "X" * 2_000_000}
        status, body = _http("POST", "/send", huge, headers=_auth_headers())
        self.assertEqual(status, 400)

    def test_from_with_empty_local_part_rejected(self) -> None:
        status, body = _http(
            "POST", "/send",
            {"from": "@example.com", "to": ["a@b.com"], "subject": "Hi", "text": "B"},
            headers=_auth_headers(),
        )
        self.assertEqual(status, 400)

    def test_cc_string_accepted(self) -> None:
        # Should fail at SMTP connection, not validation
        status, _body = _http(
            "POST", "/send",
            {"to": ["a@b.com"], "cc": "c@d.com", "subject": "Hi", "text": "B"},
            headers=_auth_headers(),
        )
        # Validation passes; SMTP will fail (dummy server on port 1)
        self.assertEqual(status, 500)

    def test_reply_to_accepted(self) -> None:
        status, _body = _http(
            "POST", "/send",
            {
                "to": ["a@b.com"],
                "reply_to": "reply@example.com",
                "subject": "Hi",
                "text": "B",
            },
            headers=_auth_headers(),
        )
        self.assertEqual(status, 500)  # validation passes, SMTP fails

    def test_attachments_rejected(self) -> None:
        status, body = _http(
            "POST", "/send",
            {"to": ["a@b.com"], "subject": "Hi", "text": "B", "attachments": ["x"]},
            headers=_auth_headers(),
        )
        self.assertEqual(status, 400)


class TestRouting(unittest.TestCase):
    """404 checks."""

    def test_unknown_get_path(self) -> None:
        status, body = _http("GET", "/unknown")
        self.assertEqual(status, 404)

    def test_unknown_post_path(self) -> None:
        status, body = _http("POST", "/unknown", {}, headers=_auth_headers())
        self.assertEqual(status, 404)


# ---------------------------------------------------------------------------
# Real SMTP test (only when credentials are supplied)
# ---------------------------------------------------------------------------
@unittest.skipUnless(_has_real_smtp(), "TEST_SMTP_* env vars not set")
class TestRealSMTPDelivery(unittest.TestCase):
    """End-to-end: HTTP POST /send → SMTP → MTA accepts."""

    @classmethod
    def setUpClass(cls) -> None:
        env = _real_smtp_env()
        # Delete stale config so the carousel probes fresh.
        try:
            os.remove(env["SMTP_CONFIG_PATH"])
        except FileNotFoundError:
            pass
        cls._proc = subprocess.Popen(
            [sys.executable, "app.py"],
            env={**os.environ, **env},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Wait until the E2E server is ready.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{E2E_PORT}/health", timeout=2
                )
                break
            except Exception:
                time.sleep(0.3)
        else:
            cls._proc.terminate()
            cls._proc.wait()
            raise RuntimeError("E2E server did not start in time")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._proc.send_signal(signal.SIGTERM)
        try:
            cls._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls._proc.kill()
            cls._proc.wait()

    def _server_stderr(self) -> str:
        """Read any stderr output from the server process."""
        # Non-blocking read of available data
        import select
        stderr_fd = self._proc.stderr.fileno()
        parts = []
        while select.select([stderr_fd], [], [], 0)[0]:
            data = os.read(stderr_fd, 4096)
            if not data:
                break
            parts.append(data)
        return b"".join(parts).decode("utf-8", errors="replace")

    def setUp(self) -> None:
        # Small delay between tests to avoid SMTP server rate limiting.
        time.sleep(1.0)

    def _assert_ok(self, status: int, body: dict[str, Any]) -> None:
        stderr_out = self._server_stderr()
        self.assertEqual(status, 200, f"Expected 200, got {status}: {body}\nServer stderr:\n{stderr_out}")
        self.assertEqual(body["status"], "ok")

    def _send_e2e(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Send an E2E request with retry on SMTP disconnect (rate limiting)."""
        last_status = None
        last_body = None
        for attempt in range(3):
            status, body = _http(
                "POST", "/send", payload,
                headers=_auth_headers(), timeout=30, port=E2E_PORT,
            )
            last_status, last_body = status, body
            if status == 200:
                return status, body
            if status == 500 and body.get("status") == "smtp":
                stderr_out = self._server_stderr()
                if "SMTPServerDisconnected" in stderr_out:
                    time.sleep(2.0)
                    continue
            break
        return last_status, last_body

    def test_plain_text_email_accepted_by_smtp(self) -> None:
        status, body = self._send_e2e({
            "to": [os.environ["TEST_SMTP_USERNAME"]],
            "subject": "E2E test – plain text",
            "text": "This email was sent by the test suite.",
        })
        self._assert_ok(status, body)

    def test_html_email_accepted_by_smtp(self) -> None:
        status, body = self._send_e2e({
            "to": [os.environ["TEST_SMTP_USERNAME"]],
            "subject": "E2E test – HTML",
            "text": "Plain text fallback",
            "html": "<html><body><p>HTML body</p></body></html>",
        })
        self._assert_ok(status, body)

    def test_cc_and_reply_to_accepted_by_smtp(self) -> None:
        status, body = self._send_e2e({
            "from": os.environ["TEST_SMTP_FROM_DEFAULT"],
            "to": [os.environ["TEST_SMTP_USERNAME"]],
            "cc": os.environ["TEST_SMTP_USERNAME"],
            "reply_to": os.environ["TEST_SMTP_FROM_DEFAULT"],
            "subject": "E2E test – CC + Reply-To",
            "text": "Testing CC and Reply-To headers.",
        })
        self._assert_ok(status, body)

    def test_default_from_address_used_when_omitted(self) -> None:
        status, body = self._send_e2e({
            "to": [os.environ["TEST_SMTP_USERNAME"]],
            "subject": "E2E test – default from",
            "text": "From should default to SMTP_FROM_DEFAULT.",
        })
        self._assert_ok(status, body)

    def test_multiple_recipients_accepted(self) -> None:
        addr = os.environ["TEST_SMTP_USERNAME"]
        status, body = self._send_e2e({
            "to": [addr, addr],
            "subject": "E2E test – multiple recipients",
            "text": "Two envelope recipients.",
        })
        self._assert_ok(status, body)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def _start_server() -> subprocess.Popen[bytes]:
    _write_dummy_config()
    proc = subprocess.Popen(
        [sys.executable, "app.py"],
        env={**os.environ, **BASE_ENV},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait until /health responds.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{LISTEN_PORT}/health", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        proc.terminate()
        proc.wait()
        raise RuntimeError("Server did not start in time")
    return proc


def _stop_server(proc: subprocess.Popen[bytes]) -> None:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


if __name__ == "__main__":
    server_proc = _start_server()
    try:
        result = unittest.main(verbosity=2, exit=False)
        if not result.result.wasSuccessful():
            sys.exit(1)
    finally:
        _stop_server(server_proc)
