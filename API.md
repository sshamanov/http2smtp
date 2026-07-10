# Simple Mail API

This document describes the HTTP API for integration agents and client applications.

The app is a small synchronous HTTP wrapper over an existing authenticated SMTP MTA.

A successful response means the SMTP server accepted the message. It does not mean the recipient received it, the message reached an inbox, or no bounce will occur later.

## Base URL

Example local URL:

```text
http://localhost:8080
```

In production, put the service behind HTTPS if it is accessed over a network.

## Authentication

Every mail send request must include a static bearer token:

```http
Authorization: Bearer <API_TOKEN>
```

`API_TOKEN` is configured by environment variable on the server.

Wrong or missing token returns:

```http
401 Unauthorized
```

```json
{"status":"auth"}
```

## Endpoints

## POST /send

Submits one email synchronously to the configured SMTP server.

### Request headers

```http
POST /send
Authorization: Bearer <API_TOKEN>
Content-Type: application/json
```

### Request body

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

### Fields

Address fields (`from`, `to`, `cc`, `bcc`, `reply_to`) accept plain email or display-name form:

- `"user@example.com"`
- `"John Doe <user@example.com>"`

| Field | Required | Type | Notes |
|---|---:|---|---|
| `from` | No | string | Falls back to `SMTP_FROM_DEFAULT` if omitted. |
| `to` | Yes | string or string array | At least one recipient is required. |
| `cc` | No | string or string array | Optional. |
| `bcc` | No | string or string array | Optional. Not added as a visible header. |
| `reply_to` | No | string | Optional Reply-To header. |
| `subject` | Yes | string | Must be non-empty. |
| `text` | Conditionally | string | Required if `html` is omitted. |
| `html` | Conditionally | string | Required if `text` is omitted. |

At least one of `text` or `html` must be present.

Attachments are not supported. If a non-empty `attachments` field is sent, the API returns `bad_request`.

### Success response

The SMTP server accepted the message:

```http
200 OK
```

```json
{"status":"ok"}
```

### Failure responses

Wrong or missing API token:

```http
401 Unauthorized
```

```json
{"status":"auth"}
```

Invalid request body, invalid JSON, missing required fields, too many recipients, or body too large:

```http
400 Bad Request
```

```json
{"status":"bad_request"}
```

SMTP connection, authentication, TLS, or send failure:

```http
500 Internal Server Error
```

```json
{"status":"smtp"}
```

Unexpected internal failure:

```http
500 Internal Server Error
```

```json
{"status":"failed"}
```

## GET /health

Health check endpoint.

```http
GET /health
```

Success:

```http
200 OK
```

```json
{"status":"ok"}
```

This endpoint does not require authentication and does not check SMTP connectivity.

## Client behavior

Recommended integration behavior:

1. Treat `200 {"status":"ok"}` as accepted by the SMTP MTA.
2. Treat `401 {"status":"auth"}` as a configuration error. Do not retry automatically.
3. Treat `400 {"status":"bad_request"}` as a client payload error. Do not retry automatically.
4. Treat `500 {"status":"smtp"}` as a temporary or SMTP-side failure. Retry only if the caller can tolerate duplicate email.
5. Treat `500 {"status":"failed"}` as an internal service failure. Retry only if duplicate email is acceptable.

The API has no idempotency key and no queue. A client retry may send the same email more than once if the first request reached the SMTP server but the HTTP response failed afterward.

## Curl examples

Plain text email:

```bash
curl -sS -X POST http://localhost:8080/send \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "to": ["user@example.net"],
    "subject": "Test message",
    "text": "Hello from Simple Mail API"
  }'
```

HTML and text email:

```bash
curl -sS -X POST http://localhost:8080/send \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "from": "noreply@example.com",
    "to": ["user@example.net"],
    "subject": "HTML test",
    "text": "Hello",
    "html": "<p>Hello</p>"
  }'
```

Health check:

```bash
curl -sS http://localhost:8080/health
```

Expected response:

```json
{"status":"ok"}
```
