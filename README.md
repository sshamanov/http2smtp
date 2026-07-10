# Simple MTA Mail API

A small synchronous HTTP API wrapper for an existing authenticated SMTP MTA.

It is intentionally not a full SendGrid, Mailgun, Postmark, or SES replacement. It gives internal applications a simple HTTP JSON interface for sending low-volume transactional email through an existing mail server — including open relays and self-signed-cert servers.

## Architecture

```
Client app
  → HTTP POST /send  (Bearer token auth)
  → Python mail API  (stdlib, single file)
  → SMTP submission  (auto-probed TLS + auth)
  → existing MTA     (standard or open relay)
```

The request is synchronous. The API opens an SMTP connection, authenticates if needed, submits the message, and returns the result.

Success means the configured SMTP server accepted the message. The app does **not** track final delivery, inbox placement, remote SMTP delivery, or bounces.

## SMTP auto-probing (carousel)

On first startup the app probes the SMTP server to discover a working combination of port, TLS mode, and auth requirement. It tries combinations ordered by likelihood:

**With credentials** (`SMTP_USERNAME` + `SMTP_PASSWORD` set):
1. `587/starttls/auth` — standard submission
2. `587/none/auth`
3. `465/ssl/auth` — legacy SMTPS
4. `25/none/auth`
5. `25/starttls/auth`
6. `25/none/noauth` — open-relay fallback

**Without credentials** (open relay):
1. `25/none/noauth` — most common
2. `587/starttls/noauth`
3. `587/none/noauth`
4. `465/ssl/noauth`
5. `25/starttls/noauth`

The first successful combination is saved to `smtp.json` and reused on subsequent starts. Delete the file (or the specific server key) to force re-probe.

SSL certificate verification is **always disabled** — the app targets internal networks, self-signed certificates (docker-mailserver), and open relays that rarely have valid certs.

`SMTP_PORT` can be set to narrow probing to a single port. Config file keys are hostnames (e.g. `"mail.example.com"`) normally, or `"hostname:port"` when `SMTP_PORT` is explicitly set — so adding a port later creates a separate entry rather than overwriting.

Credentials (`SMTP_USERNAME`, `SMTP_PASSWORD`) are **never written to disk** — only the `auth_required` flag and `auth_mechanism` string are stored.

## Files

| File | Purpose |
|---|---|
| `app.py` | Complete Python app in one file. Stdlib only. |
| `Dockerfile` | Builds a minimal `python:3.12-alpine` image. |
| `entrypoint.sh` | Validates required env vars at startup; prints `API.md` if any are missing. |
| `compose.yaml.example` | Reference Compose deployment with `network_mode: host` and `smtp.json` volume mount. |
| `.env.example` | Template for Compose env vars. Copy to `.env` and fill in. |
| `API.md` | API contract and integration guide. |
| `test_app.py` | 28 tests: 23 validation + 5 real-SMTP E2E. |
| `AGENTS.md` | Guidance for Claude Code agents working in this repo. |
| `.github/workflows/build.yml` | CI: build and push Docker image to GHCR. |

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `API_TOKEN` | **yes** | — | Static Bearer token for HTTP auth. |
| `SMTP_HOST` | **yes** | — | SMTP server hostname or IP. |
| `SMTP_PORT` | no | — | Narrow carousel probing to a single port. |
| `SMTP_USERNAME` | no | — | SMTP auth username. Omit for open relay. |
| `SMTP_PASSWORD` | no | — | SMTP auth password. |
| `SMTP_FROM_DEFAULT` | no | `$SMTP_USERNAME` | Fallback From address when request omits `from`. |
| `LISTEN_HOST` | no | `0.0.0.0` | HTTP bind address. |
| `LISTEN_PORT` | no | `8080` | HTTP bind port. |
| `MAX_RECIPIENTS` | no | `20` | Max total `to` + `cc` + `bcc` recipients per request. |
| `MAX_BODY_BYTES` | no | `1048576` | Max HTTP JSON body size (1 MiB). |
| `SMTP_CONFIG_PATH` | no | `/app/config/smtp.json` | Path to cached probe config inside mounted `./config` dir. |
| `SMTP_PROBE_TIMEOUT` | no | `10` | Seconds per probe attempt. |
| `SMTP_PROBE_DELAY` | no | `3` | Seconds between probe attempts (rate limiting). |
| `SMTP_RUNTIME_TIMEOUT` | no | `30` | Seconds per SMTP send operation. |

## Run with Docker Compose

```bash
cp compose.yaml.example compose.yaml
cp .env.example .env
# Edit .env with your values
docker compose up -d
```

Compose uses `network_mode: host` to avoid NAT/bridge issues with SMTP connections. The `smtp.json` is mounted from the host so probe results persist across container restarts.

## Run directly with Docker

```bash
docker build --network host -t mail-api:latest .
docker run --rm --network host \
  -v "$(pwd)/config:/app/config" \
  -e API_TOKEN=secret \
  -e SMTP_HOST=mail.example.com \
  -e SMTP_USERNAME=user@example.com \
  -e SMTP_PASSWORD=password \
  mail-api:latest
```

## Run with Python (dev only)

```bash
export API_TOKEN=change-this-token
export SMTP_HOST=mail.example.com
export SMTP_USERNAME=user@example.com
export SMTP_PASSWORD=password
python3 app.py
```

Production deployments should use Docker/Compose.

## API summary

### POST /send

```http
POST /send
Authorization: Bearer <API_TOKEN>
Content-Type: application/json
```

```json
{
  "from": "noreply@example.com",
  "to": ["user@example.net"],
  "cc": [],
  "bcc": [],
  "reply_to": "support@example.com",
  "subject": "Subject",
  "text": "Plain text body",
  "html": "<p>HTML body</p>"
}
```

Minimal request:

```json
{
  "to": ["user@example.net"],
  "subject": "Test message",
  "text": "Hello"
}
```

### GET /health

```http
GET /health
```
→ `200 {"status":"ok"}` — no auth required, does not test SMTP connectivity.

### Responses

| Case | HTTP | Body |
|---|---|---|
| SMTP accepted | `200` | `{"status":"ok"}` |
| Wrong/missing token | `401` | `{"status":"auth"}` |
| Bad JSON or invalid request | `400` | `{"status":"bad_request","detail":"reason"}` |
| SMTP failure | `500` | `{"status":"smtp"}` |
| Unexpected error | `500` | `{"status":"failed"}` |

### Client guidance

- `200` — email accepted by MTA. Do not retry.
- `401` — config error. Do not retry.
- `400` — client payload error. Do not retry.
- `500 smtp` — SMTP-side failure. Retry only if duplicate email is acceptable.
- `500 failed` — internal error. Retry only if duplicate email is acceptable.

The API has no idempotency key and no queue. A retry may send the same email twice if the first request reached the SMTP server but the HTTP response failed afterward.

## Non-goals

- message queue, database, retry worker
- delivery tracking, bounce parsing
- templates, attachments, webhooks
- admin UI, campaign or bulk mail tools
- SES/SendGrid/Mailgun/Postmark protocol compatibility

## Requirements

- Docker, or Python 3.12+
- Existing SMTP MTA (or open relay) — auto-probed on first start

## Testing

```bash
# Validation tests (no SMTP needed)
python3 test_app.py

# Full E2E against a real SMTP server
TEST_SMTP_HOST=mail.example.com \
TEST_SMTP_USERNAME=user@example.com \
TEST_SMTP_PASSWORD=password \
TEST_SMTP_FROM_DEFAULT=noreply@example.com \
python3 test_app.py
```

Tests cover health checks, auth, routing, input validation (17+ edge cases), and real SMTP delivery with plain text, HTML, CC, Reply-To, default from, and multiple recipients.

## CI

GitHub Actions builds and pushes to `ghcr.io/sshamanov/mail-api` on push to `main` and tags. PRs build without pushing.
