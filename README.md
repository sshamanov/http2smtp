# Simple MTA Mail API

A small synchronous HTTP API wrapper for an existing authenticated SMTP MTA.

It is intentionally not a full SendGrid, Mailgun, Postmark, or SES replacement. It only gives internal applications a simple HTTP JSON interface for sending low-volume transactional email through an existing mail server.

## Files

| File | Purpose |
|---|---|
| `app.py` | Complete Python app in one file. Uses only Python stdlib. |
| `compose.yaml` | Example Docker Compose service. |
| `API.md` | API contract and integration guide. |
| `README.md` | Combined project documentation. |

## Architecture

```text
Client app
  -> HTTP POST /send
  -> Bearer token auth
  -> Python mail API
  -> authenticated SMTP
  -> existing MTA
```

The request is synchronous. The API opens an SMTP connection, authenticates, submits the message, and returns the result.

Success means the configured SMTP server accepted the message. The app does not track final delivery, inbox placement, remote SMTP delivery, or bounces.

## Features

- Single-file Python service
- Python standard library only
- Static bearer token authentication
- JSON HTTP API
- Synchronous SMTP submission
- SMTP auth support
- TLS modes: `starttls`, `ssl`, `none`
- Plain text and HTML email support
- Optional `cc`, `bcc`, and `reply_to`
- Simple health check
- Basic request limits

## Non-goals

The app does not provide:

- message queue
- database
- retry worker
- delivery tracking
- bounce parsing
- templates
- attachments
- webhooks
- admin UI
- campaign or bulk mail tools
- SES/SendGrid/Mailgun/Postmark protocol compatibility

## Requirements

- Docker, or Python 3.12+
- Existing SMTP MTA with username/password authentication

The code should also work on recent Python 3 versions with the same standard library APIs, but the Compose example uses `python:3.12-alpine`.

## Environment variables

| Variable | Required | Default | Description |
|---|---:|---|---|
| `API_TOKEN` | Yes | none | Static bearer token for HTTP API auth. |
| `LISTEN_HOST` | No | `0.0.0.0` | HTTP bind address. |
| `LISTEN_PORT` | No | `8080` | HTTP bind port. |
| `SMTP_HOST` | Yes | none | SMTP server hostname or IP. |
| `SMTP_PORT` | No | `587` | SMTP server port. |
| `SMTP_USERNAME` | Yes | none | SMTP auth username. |
| `SMTP_PASSWORD` | Yes | none | SMTP auth password. |
| `SMTP_TLS` | No | `starttls` | One of `starttls`, `ssl`, or `none`. |
| `SMTP_FROM_DEFAULT` | Yes | none | Default From address if request omits `from`. |
| `SMTP_TIMEOUT_SECONDS` | No | `30` | SMTP socket timeout. |
| `MAX_RECIPIENTS` | No | `20` | Maximum total `to` + `cc` + `bcc` recipients. |
| `MAX_BODY_BYTES` | No | `1048576` | Maximum HTTP JSON body size. |

## Run with Docker Compose

Create a `.env` file next to `compose.yaml`:

```env
API_TOKEN=change-this-token
SMTP_HOST=mail.example.com
SMTP_PORT=587
SMTP_USERNAME=smtp-user@example.com
SMTP_PASSWORD=change-this-password
SMTP_TLS=starttls
SMTP_FROM_DEFAULT=noreply@example.com
```

Start the service:

```bash
docker compose up -d
```

Check logs:

```bash
docker compose logs -f mail-api
```

Stop:

```bash
docker compose down
```

## Run directly with Python

```bash
export API_TOKEN=change-this-token
export SMTP_HOST=mail.example.com
export SMTP_PORT=587
export SMTP_USERNAME=smtp-user@example.com
export SMTP_PASSWORD=change-this-password
export SMTP_TLS=starttls
export SMTP_FROM_DEFAULT=noreply@example.com
python app.py
```

## API summary

### Send email

```http
POST /send
Authorization: Bearer <API_TOKEN>
Content-Type: application/json
```

Example:

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

### Responses

| Case | HTTP | Body |
|---|---:|---|
| SMTP MTA accepted the message | `200` | `{"status":"ok"}` |
| Wrong or missing API token | `401` | `{"status":"auth"}` |
| Bad JSON or invalid request | `400` | `{"status":"bad_request"}` |
| SMTP failure | `500` | `{"status":"smtp"}` |
| Unexpected internal failure | `500` | `{"status":"failed"}` |

### Health check

```http
GET /health
```

Response:

```json
{"status":"ok"}
```

The health check confirms the HTTP service is running. It does not test SMTP connectivity.

## Curl test

```bash
curl -sS -X POST http://localhost:8080/send \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "to": ["user@example.net"],
    "subject": "Test message",
    "text": "Hello from Simple MTA Mail API"
  }'
```

Expected success:

```json
{"status":"ok"}
```

## Security notes

- Keep the service behind HTTPS if it is reachable over a network.
- Use a long random `API_TOKEN`.
- Do not expose this service publicly without a reverse proxy, TLS, firewall rules, and rate limiting.
- The app intentionally returns coarse error statuses and does not expose SMTP error details to clients.
- Logs do not print the API token or SMTP password.
- This app is an authenticated HTTP relay. Restrict network access where possible.

## Operational notes

The service uses `ThreadingHTTPServer`, so it can handle more than one HTTP request at a time. Each `/send` request opens its own SMTP connection.

For small message volume this is simpler and good enough. If volume grows, the next likely improvements are:

1. Add a concurrency limit around SMTP sends.
2. Add a queue and worker.
3. Add idempotency keys.
4. Add delivery/bounce processing from MTA logs or mailbox bounces.

Those are intentionally excluded from this version.
