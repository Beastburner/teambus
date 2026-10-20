# Public sandbox

`sandbox.py` lets anyone get a **private, throwaway hub** (alice-frontend / bob-backend / carol-fullstack) from a web page, so they can try TeamBus without hosting anything. Each visitor gets their own `server.py` process and SQLite file, so visitors never see each other's data and agents cannot prompt-inject strangers.

## Run it
```bash
echo "SANDBOX_DOMAIN=sandbox.example.com" > .env     # DNS A record -> this host; ports 80/443 open
docker compose -f docker-compose.sandbox.yml up -d --build
```
(Separate from `docker-compose.yml`; both use ports 80/443, so run one per host.) Without Docker: `python sandbox.py` (listens on 127.0.0.1:8780; put your own TLS proxy in front and set `SANDBOX_PUBLIC_URL`, `SANDBOX_TRUST_PROXY=1`).

Visitors open `https://sandbox.example.com/`, click **Create my sandbox**, and get a `teambus-join …` line per agent plus a web-view link. `curl -X POST https://sandbox.example.com/new` returns the same as JSON.

## Several sandboxes, your own agents and roles
A visitor can hold several sandboxes at once (default 3 active, 6 new per hour), and chooses each one's agents and roles. The page keeps your sandboxes in your browser (with their tokens) so you can come back to them; they disappear when they expire.

API (the page uses exactly this):
| Call | What it does |
|---|---|
| `POST /new` with optional `{"label": "checkout rewrite", "agents": [{"agent_id": "dana-web", "role": "web"}, ...]}` | creates a sandbox. 2 to `SANDBOX_MAX_AGENTS` agents; names are 2 to 31 characters of lowercase letters, digits and dashes starting with a letter; roles are 1 to 24 letters, digits, dashes or underscores. No body means alice-frontend, bob-backend and carol-fullstack. Returns the hub URL, the web view, every agent's token and join command, and a `manage_token`. |
| `GET /manage/<id>` | the same description again (bearer `manage_token`) |
| `POST /manage/<id>/agents` with `{"agent_id", "role"}` | adds an agent to a running sandbox; every existing token keeps working |
| `DELETE /manage/<id>` | deletes the sandbox now and frees its slot |

Validation is strict on purpose: names and roles end up in a shell command (the join line) and in other agents' context. The manage token is separate from the agent tokens: an agent token cannot delete or edit a sandbox.


## Joining, and knowing what happened (the registry)
Every sandbox is recorded in a registry (in memory by default; in Google Firestore with `docs/firebase.md`). That is what lets the service answer questions instead of returning a bare 404.

- `GET /status/<id>` (public, no secrets): `active` with each agent's `joined` flag, last-seen age and join time, or the reason it ended: `expired`, `deleted`, `evicted` (over the data limit) or `lost` (the server stopped or restarted). An id the registry never saw answers `unknown`.
- The sandbox's own URL explains itself: `GET <hub_url>/healthz` on a dead sandbox returns 404 with a `message`, and `teambus-join` prints it (for example "This sandbox expired at 03:12 UTC ... Create a new one at ..."), then stops without changing anything.
- An agent counts as **joined** the first time it authenticates against the hub, which is exactly what `teambus-join` does at its last step (`teambus doctor`). The page shows "joined, seen 8s ago" or "not joined yet" per agent and refreshes every few seconds, and a sandbox that vanished is explained in a banner.
- With the default in-memory registry a restart forgets everything, and an old id is "unknown" (the message says a restart is the usual cause). Add Firestore for "lost: the server restarted" and for join history that outlives the sandbox.
- `/healthz` shows `registry` (`memory` or `firestore`), `registry_errors` and `registry_last_error`. Registry problems never block creating or using a sandbox.

## Limits (all environment variables)
| Variable | Default | Meaning |
|---|---|---|
| `SANDBOX_TTL` | 3600 | seconds until a sandbox and its data are deleted |
| `SANDBOX_MAX` | 20 | concurrent sandboxes (503 when full) |
| `SANDBOX_NEW_PER_IP_HOUR` | 6 | new sandboxes per client IP per hour (429) |
| `SANDBOX_MAX_ACTIVE_PER_IP` | 3 | sandboxes one client can hold at once (429; delete one to free the slot) |
| `SANDBOX_MAX_AGENTS` | 5 | most agents in one sandbox |
| `SANDBOX_INSTALL_CMD` | the install one-liner | what the dialog shows under "First time on this machine?" |
| `SANDBOX_RPM` | 600 | requests per minute per sandbox (429) |
| `SANDBOX_MAX_BODY` | 65536 | bytes per request (413) |
| `SANDBOX_MAX_DB_MB` | 5 | a sandbox whose DB exceeds this is deleted |
| `SANDBOX_WARM` | off | `1` keeps one hub pre-started, so a visitor gets theirs instantly (matters on slow hosts) |
| `SANDBOX_START_TIMEOUT` | 45 | seconds to wait for a new hub to answer before giving up |
| `SANDBOX_SPAWN_CONCURRENCY` | 2 | hubs started at the same time (they starve each other on a small CPU) |
| `SANDBOX_TRUST_PROXY` | off | `1` behind a reverse proxy you control: the client address and the https scheme/host come from its headers |
| `SANDBOX_CLIENT_IP_HEADER` | `x-forwarded-for` | header holding the client address, e.g. `cf-connecting-ip` behind Cloudflare |
| `SANDBOX_TRUST_PROXY_HOPS` | 1 | how many of your proxies are in front; the Nth entry from the RIGHT is the client (anything left of that is client-supplied) |
| `SANDBOX_DEBUG_IP` | off | `1` adds `GET /_ip`, which echoes what the proxy sends; turn it off again afterwards |
| `SANDBOX_MEM` (compose) | 2g | container memory; each sandbox is one small Python process, so set `SANDBOX_MAX` from a measurement |

The compose service is read-only, drops all capabilities, runs as a non-root user and keeps sandbox data on a RAM-backed tmpfs (gone on restart).

## Tested
`pytest test_sandbox.py` (isolation, every limit, expiry cleanup, DB-cap eviction, shutdown cleanup, MCP through the proxy, `X-Forwarded-For` spoofing) and, in Docker with Caddy's local CA: creation over https, quota not resettable via a spoofed header, a real `claude -p` session over https, expiry inside the container, clean teardown.
**Not tested:** a real public domain / Let's Encrypt certificate, load, or many distinct client IPs. Many IPs can still exhaust `SANDBOX_MAX`; size it, watch the logs, and consider Caddy-level rules.

## Deploy on Render (free instance)
`render.yaml` is a Blueprint for it. Nothing here has been deployed or tested on Render itself; the numbers below come from running the same image in Docker with Render's documented free limits (`--memory 512m --cpus 0.1`).

**What the free instance means for this service**
- **512 MB RAM, 0.1 CPU.** Measured: about 40 MiB per sandbox (230 MiB with 4 sandboxes and a spare), so `SANDBOX_MAX=8` is the blueprint's default (about 390 MiB with a spare). Starting a hub takes about 15 s of wall time at 0.1 CPU, which is why `SANDBOX_WARM=1` keeps one ready (visitors then get theirs instantly; the next one warms in 7 to 11 s).
- **Sleeps after 15 minutes without requests, about a minute to wake.** Every sandbox disappears when it sleeps or restarts, and invite commands stop working. For throwaway sandboxes that is acceptable; say so on the page if it matters.
- **Ephemeral disk, 750 free instance hours a month.** Fine for data that is deleted anyway; the service stops for the month when the hours run out.
- **TLS** is terminated by Render's load balancer, so the container sees plain http: `SANDBOX_TRUST_PROXY=1` makes invites use `https://` and your `onrender.com` host.

**Zero-configuration deploy.** The default `Dockerfile` starts the hub, but on Render (the platform sets `RENDER` itself) with no hub auth configured, `start.sh` starts the sandbox instead, with defaults sized for a free instance. So a service created by hand with the default settings works without changing the Dockerfile path, the start command or any environment variable; anything you do set overrides those defaults. A hub on Render (with `TEAMBUS_TOKEN` or a tokens file) still starts as the hub, and `TEAMBUS_MODE=hub` or `sandbox` forces either.

**Steps**
1. Create a Blueprint from the repo (`render.yaml` uses `Dockerfile.sandbox`, which starts `sandbox.py` by default). If you create the service by hand in the dashboard instead, set **Dockerfile Path** to `./Dockerfile.sandbox` (the plain `./Dockerfile` is the hub and exits with "refusing to start without auth"), **Health Check Path** to `/healthz`, and add the environment variables listed in `render.yaml`. A hand-made service ignores `render.yaml`.
2. (Already verified on a live deploy: Render's `X-Forwarded-For` is `client, Cloudflare, Render-internal`, and `CF-Connecting-IP` carries the client. The Render defaults use that header with a third-from-right XFF fallback. A fixed hop count of 1 would have seen Render's internal address for every visitor, i.e. one shared quota for everyone.) After a deploy open `https://<your-service>.onrender.com/_ip` from two different networks (for example your phone on mobile data). `derived_ip` must be your own address and must differ between the two. Render does not document its forwarding headers and reports conflict, so check rather than assume.
   - If `derived_ip` is a Render or Cloudflare address (the same for everyone), raise `SANDBOX_TRUST_PROXY_HOPS` to 2, or set `SANDBOX_CLIENT_IP_HEADER=cf-connecting-ip` if that header appears in the output.
   - If a visitor could change `derived_ip` by sending their own `X-Forwarded-For`, the hop count is too low.
3. Create a sandbox from the page and check the invite starts with `https://` and your host. If it shows `http://`, set `SANDBOX_PUBLIC_URL=https://<your-service>.onrender.com`.
4. Set `SANDBOX_DEBUG_IP` to `0` (or delete it).

**Not verified on Render:** whether its proxy keeps the long-lived streaming connection a Claude Code session holds open (MCP over streamable HTTP) for as long as a session needs. Test one real `claude` session through the deployed URL before announcing it.
