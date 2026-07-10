# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Rules

1. **Test in Docker only.** Do not run `python3 test_app.py` directly against the host. Build the image and run tests inside a container with `--network host` and a mounted `app.py` so local changes are tested live:

   ```bash
   docker build --network host -t mail-api:test .
   docker run --rm --network host \
     -v "$(pwd)/app.py:/app/app.py:ro" \
     -e API_TOKEN=test -e SMTP_HOST=mail.example.com \
     -e SMTP_USERNAME=user@example.com \
     -e SMTP_PASSWORD=password \
     mail-api:test
   ```

   For E2E delivery tests, run `test_app.py` from the host but pointed at the Docker container.

2. **README.md is the design authority.** Any architectural change or new env var must be reflected there first, then `app.py`. The compose and env example files must stay in sync.

3. **Single-file app.** `app.py` remains the sole Python module. Do not split into multiple `.py` files. Stdlib only — never introduce pip dependencies.

4. **Production uses Compose.** `compose.yaml.example` is the reference deployment. The Dockerfile bakes `app.py` and `API.md` into the image. At runtime, `smtp_config.json` is mounted as a volume for persistence.

5. **Commit and push after every functional change.** Every code review fix, feature addition, or doc update is a commit. Push immediately after.

6. **JSON-line logging to stderr.** All structured output (events, errors, probe results) goes to stderr as one JSON object per line. stdout is reserved for the HTTP server's own output.

7. **Config file is append-only across servers.** `smtp_config.json` is a dict keyed by server hostname (or `hostname:port` when `SMTP_PORT` is explicitly set). Probing appends new entries; old entries for other servers are preserved. Delete the file or a specific key to force re-probe.

8. **Auth is never stored to disk.** `SMTP_USERNAME` and `SMTP_PASSWORD` live in env vars only. The config file records `auth_required: true` and `auth_mechanism` but never the credentials themselves.

## Commands

```bash
# Build + run validation in Docker (app.py mounted for live edits)
docker build --network host -t mail-api:test . && \
docker run --rm --network host \
  -v "$(pwd)/app.py:/app/app.py:ro" \
  -e API_TOKEN=test-token \
  -e SMTP_HOST=127.0.0.1 \
  -e SMTP_USERNAME=dummy -e SMTP_PASSWORD=dummy \
  mail-api:test

# Full E2E test against real SMTP (starts Docker + runs test suite)
docker build --network host -t mail-api:test . && \
docker run -d --name mail-api-e2e --network host \
  -v "$(pwd)/app.py:/app/app.py:ro" \
  -e API_TOKEN=test-token-abc123 \
  -e SMTP_HOST=mail.example.com \
  -e SMTP_USERNAME=user@example.com \
  -e SMTP_PASSWORD=password \
  mail-api:test && \
TEST_SMTP_HOST=mail.example.com TEST_SMTP_USERNAME=user@example.com \
TEST_SMTP_PASSWORD=password \
TEST_SMTP_FROM_DEFAULT=noreply@example.com python3 test_app.py ; \
docker rm -f mail-api-e2e

# Production Compose
cp compose.yaml.example compose.yaml
cp .env.example .env   # edit .env
docker compose up -d
```

## Architecture

Single-file Python app (`app.py`, ~480 lines). **Python stdlib only** — no pip dependencies. Targets Python 3.12 (Docker) but works on 3.9+.

The app is an **HTTP-to-SMTP relay**: clients `POST /send` with a JSON email payload, the app submits it to a configured SMTP server, returns `200 {"status":"ok"}` on SMTP acceptance.

### Startup flow

1. Parse env vars at **module level** (fast-fail on missing required vars)
2. `main()` calls `get_smtp_settings()` to resolve SMTP config
3. If `SMTP_CONFIG_PATH` file has a cached entry for `SMTP_HOST`, use it
4. Otherwise run the **carousel**: probe port/TLS/auth combinations until one succeeds, save result to JSON
5. Start `ThreadingHTTPServer`

### SMTP carousel

`_probe_carousel()` tries combinations ordered by likelihood. Credentials present → auth-first ordering. No credentials → open-relay-first ordering. On first success, the config is saved to a JSON file keyed by hostname (or `hostname:port` when `SMTP_PORT` is explicitly set). Delete the file to force re-probe on next start.

SSL certificate verification is **always disabled** (`_SSL_CONTEXT`) — the app targets internal networks and self-signed certs (e.g. docker-mailserver).

### HTTP handler

- `GET /health` — no auth, returns `{"status":"ok"}`
- `POST /send` — Bearer token auth via constant-time compare (`hmac.compare_digest`), JSON body validated by `validate_payload()`, then `send_via_smtp()`
- All responses are `{"status":"..."}` with optional `"detail"` on 400 errors
- After SMTP success, the 200 response is written outside the main try-except — client-disconnect `OSError` after a successful send is silently caught since the email was already sent

### Input validation

- `normalize_address()` uses `email.utils.parseaddr` + `formataddr`, rejects header injection (CR/LF) and empty-local-part (`@example.com`)
- `normalize_address_list()` accepts string or list
- `validate_payload()` rejects missing fields, non-string types, attachments, oversized bodies, too many recipients
- `BadRequest` exceptions carry human-readable messages surfaced in the `"detail"` response field

### Logging

All logs are **JSON lines to stderr** (not stdout, which is reserved for the HTTP server). Events: `started`, `smtp_error`, `internal_error`, `probe_success`, `probe_fail`, `smtp_config_loaded`, `smtp_config_saved`, `smtp_config_stale`.

## Env var reference

| Var | Required | Notes |
|---|---|---|
| `API_TOKEN` | yes | Bearer token for HTTP auth |
| `SMTP_HOST` | yes | SMTP server hostname |
| `SMTP_PORT` | no | Narrows carousel probing to one port |
| `SMTP_USERNAME` | no | Omit for open relay |
| `SMTP_PASSWORD` | no | |
| `SMTP_FROM_DEFAULT` | no | Falls back to `SMTP_USERNAME` |
| `SMTP_CONFIG_PATH` | no | Default `/app/config/smtp_config.json` inside mounted `./config` dir |
| `LISTEN_HOST` / `LISTEN_PORT` | no | Default `0.0.0.0:8080` |
| `MAX_RECIPIENTS` / `MAX_BODY_BYTES` | no | Default 20 / 1 MiB |
| `SMTP_PROBE_TIMEOUT` / `SMTP_PROBE_DELAY` | no | Default 10s / 3s |
| `SMTP_RUNTIME_TIMEOUT` | no | Default 30s per send |

## Test architecture

`test_app.py` uses `unittest` (stdlib). Two modes:

- **Validation tests** (23 tests): Pre-populates a dummy `SMTP_CONFIG_PATH` so the carousel is skipped. Tests HTTP endpoints, auth, input validation, error handling. Starts its own server in a subprocess and polls `/health` until ready.
- **E2E SMTP tests** (5 tests, skipped unless `TEST_SMTP_*` env vars are set): Starts a second server on port 18081 with real SMTP credentials, deletes stale config to force fresh probing, sends real emails. Includes retry on `SMTPServerDisconnected`.

## CI

GitHub Actions (`.github/workflows/build.yml`) builds and pushes to `ghcr.io/<repo>` on push to `main` and tags. PRs build but don't push.
