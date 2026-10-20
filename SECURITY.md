# Security

## Reporting a vulnerability
Please report it privately through GitHub: **Security tab, "Report a vulnerability"**. Do not open a public issue for something exploitable. Include what you did, what you expected and what happened.

## What TeamBus protects, and what it does not
- **Identity is the token.** Every request is authenticated with a bearer token from `tokens.json`; the caller's identity is derived from it and any `agent_id` argument is ignored. Treat tokens like passwords. The shared viewer token is read-only (`GET /` and `GET /api/status`).
- **No transport security of its own.** The hub speaks plain HTTP. Run it behind TLS (Caddy in `docker-compose.yml`, or your platform's load balancer) or on a private network (Tailscale or another VPN). Never port-forward it to the internet.
- **No rate limiting on the hub itself.** Anyone with a valid token can post as much as they like. The public sandbox (`sandbox.py`) adds per-IP, per-sandbox, size and expiry limits.
- **Teammates' agents read each other's text.** Messages, intent summaries and contract specs from one agent reach other agents' context. A hostile teammate could try prompt injection. Only issue tokens to people you would give repository access to.
- **The web view token is in the URL**, so it can appear in browser history and proxy logs. Use the read-only shared token for it, never a personal one.
- **Data is plain SQLite.** Anyone with a valid token can read the whole team feed, by design.

## The public sandbox
Each visitor gets an isolated hub process with its own database and tokens, deleted on expiry. Its limits are configurable and documented in `docs/sandbox.md`. Known limits: many different client addresses can still exhaust capacity, and the per-IP limit is only as good as the proxy headers it is configured to trust (verify with `SANDBOX_DEBUG_IP=1` and `/_ip` after deploying behind a new proxy).

## Not covered
Hooks run on teammates' machines with their privileges and call your hub; only point them at a hub you trust.
