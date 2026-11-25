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

## On your laptop: one hub for every Claude session (permanent, no server needed)
```bash
teambus local install            # hub on 127.0.0.1:8765, data in ~/.teambus/local, starts at login (systemd user service) and restarts if it dies
cd ~/my-project && teambus local join          # hooks + MCP for this folder; a default agent for plain `claude`
teambus session reviewer --role reviewer       # a SECOND Claude in the same folder, as its own agent (any number)
teambus local status             # who is on the hub and what they are doing
```
- Plain `claude` in a joined folder is that folder's default agent (`<folder>-main`). `teambus session NAME [-- claude args]` starts `claude` with `TEAMBUS_SESSION_TOKEN` set for that one process: its hooks, its MCP tools and its inbox cursor are the session agent's, and nothing in the folder changes. Different folders share the same hub, so their agents see each other.
- Everything is also callable by Claude itself: the `orchestrator` agent (admin token in `~/.teambus/local/local.json`) has `create_agent`, `list_agents`, `team_status`, and so on. Without systemd, `teambus local install` runs the hub as a background process; run `teambus local start` after a reboot.
- It never stops an agent: hooks default to **passive** (advice only, nothing is denied and no stop is held), every hook call has a 3 s timeout and swallows errors, and a down hub just means no coordination. `teambus off` pauses all hook traffic; `teambus mode full` opts into conflict blocks.

## Let Claude create the agents (orchestrator token, no shell)
Set `TEAMBUS_ADMIN_TOKEN` (24+ characters, a secret like any token) on the hub. It is the token of one agent, `orchestrator` (rename it with `TEAMBUS_ADMIN_AGENT`), which may call `create_agent`; with it a fresh hub needs no `add-agent` step and no shell. Point your Claude at the hub with it:
```bash
claude mcp add --transport http teambus https://teambus.example.com/mcp --header "Authorization: Bearer <TEAMBUS_ADMIN_TOKEN>"
```
Then ask Claude: "create agents `alice-frontend` (frontend) and `bob-backend` (backend)". Each call returns that agent's token and a ready `join` command (`claude mcp add …`); run it in the folder where that agent's Claude session will work. `create_agent(..., spawn=true)` also asks a running `teambus worker` to start the session. Limits: `TEAMBUS_MAX_CREATED` (default 5 per creating agent; `render-hub.yaml` and `docker-compose.yml` set 20) and `TEAMBUS_MAX_AGENTS` (default 50); agents it creates cannot create more. Remove one with `remove_agent`.
Besides the 30-odd tools the hub serves MCP **resources** (`teambus://agents`, `tasks`, `decisions`, `contracts`, `protocol`) and **prompts** (`teambus_protocol`, `create_teammate`, `handoff`). Set `TEAMBUS_PUBLIC_URL` to the hub's public address so the join lines carry it.
This hub keeps its data (`TEAMBUS_DB`, `TEAMBUS_TOKENS`) on disk: restarting it keeps every agent, token and message. The **public sandbox does not**: it expires sandboxes after `SANDBOX_TTL`, and a free Render instance wipes them when it sleeps. For a permanent team use this hub (`render-hub.yaml` on a paid instance with its disk, or `docker-compose.yml` on a VPS), never `render.yaml`.

## Render (persistent hub, no VPS)
`render-hub.yaml` is a Blueprint for a real hub (the `render.yaml` next to it is the public sandbox, which sleeps and wipes state). It needs a paid instance because the persistent disk (`/data`: SQLite DB + `tokens.json`) is what keeps conversations and tokens across restarts and deploys.
1. Render -> New -> Blueprint -> pick this repo and `render-hub.yaml`.
2. After the first deploy, copy `TEAMBUS_ADMIN_TOKEN` from the Environment tab, connect your Claude with it (section above) and have it create each agent. Or, without it, open the service's **Shell**: `python server.py add-agent alice-frontend frontend` (prints its token).
3. The generated `TEAMBUS_TOKEN` (Environment tab) is the shared read-only token for the web views: `https://<service>.onrender.com/?token=...` (team view) and `/brain?token=...` (brain and terrain).
4. Each teammate runs the printed `teambus-join --hub https://<service>.onrender.com --agent-id ... --token ...`, then `teambus connect all` for Codex and Antigravity.
Not tested here: an actual Render deploy.
