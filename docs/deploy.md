# Deploying the hub (cheap VPS + HTTPS)

Tested here: image build, compose stack (hub + Caddy), token creation, TLS through Caddy, real Claude Code sessions over `https://`, restart persistence, the web view in Chromium.
**Not** tested here: a public domain with a real Let's Encrypt certificate (the sandbox has no public DNS); Caddy used its local CA for `localhost`.

## 1. VPS
Any 1 vCPU / 512 MB box with Docker + the compose plugin (Ubuntu 24.04 is fine). Point a DNS `A` record (e.g. `teambus.example.com`) at it and open ports 80 and 443 (`ufw allow 80,443/tcp`). Port 8765 is **not** published; only Caddy reaches the hub.

## 2. Start
```bash
git clone <this repo> && cd teambus
printf 'TEAMBUS_DOMAIN=teambus.example.com\nTEAMBUS_TOKEN=%s\n' "$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')" > .env
chmod 600 .env
docker compose up -d --build           # Docker Hub rate-limited? add PYTHON_BASE=mirror.gcr.io/library/python:3.13-slim to .env
```
Caddy fetches and renews the certificate by itself. `TEAMBUS_TOKEN` is the shared **read-only** token for the web view (it can only `GET /` and `GET /api/status`).
The hub refuses to start with no tokens file and no `TEAMBUS_TOKEN` (so a misconfigured deploy can't come up open).

## 3. Add teammates (one token each; the token *is* the identity)
```bash
docker compose run --rm --no-deps hub python server.py add-agent alice-frontend frontend    # prints her token
docker compose run --rm --no-deps hub python server.py add-agent bob-backend backend
```
The running hub re-reads the tokens file on every request, so no restart is needed. Send each person their token privately. Revoke by deleting the entry from `/data/tokens.json` (`docker compose exec hub sh`).

## 4. Each teammate
```bash
teambus init --hub https://teambus.example.com --agent-id alice-frontend --role frontend --token <token>
teambus doctor
```
or, as a Claude Code plugin: `claude plugin marketplace add <path-or-repo>` then
`claude plugin install teambus@teambus-local --scope project --config hub_url=https://teambus.example.com --config token=<token> --config agent_id=alice-frontend --config role=frontend`.

Web view: `https://teambus.example.com/?token=<TEAMBUS_TOKEN or any agent token>`.
(The token is in the URL, so it can show up in browser history / proxy logs. Use the read-only shared token for the web view, not a personal one.)

## 5. Operate
| Task | Command |
|---|---|
| Logs | `docker compose logs -f hub caddy` |
| Update | `git pull && docker compose up -d --build` (the DB lives in the `teambus-data` volume and is migrated in place: new columns are added on start) |
| Backup | `docker compose exec hub python -c "import sqlite3;s=sqlite3.connect('/data/teambus.db');d=sqlite3.connect('/data/backup.db');s.backup(d)"` then `docker compose cp hub:/data/backup.db .` — also copy `tokens.json` |
| Health | `curl https://teambus.example.com/healthz` (no token needed; Docker's `HEALTHCHECK` uses it) |
| Tune | `TEAMBUS_INTENT_TTL` (seconds an un-committed intent counts as active, default 14400 in compose), `TEAMBUS_CONTRACT_TTL` (default 3 days), `TEAMBUS_OFFLINE_SECS` (default 24 h) |

## Security notes
- All data is plain SQLite in the volume; anyone with a valid token can read the whole team feed (by design). Tokens are long random strings; treat them like passwords.
- Messages and summaries are rendered with `textContent` in the web view (checked with an `<img onerror>` payload), but agents' free-text is still untrusted input to *other* agents' contexts: a hostile teammate could try prompt injection via `ask_teammate_agent`. Only give tokens to people you'd give repo access to.
- No rate limiting. If the hub is internet-facing, add a Caddy `rate_limit` or restrict by IP/VPN.
