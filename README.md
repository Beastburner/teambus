# TeamBus

Your teammates' Claude Code agents see what each other are doing, ask each other questions asynchronously, and get stopped **before** they write conflicting code. One hub (MCP server + REST + SQLite + read-only web view); every session connects with its own token. A hub that is down never blocks a session.

```
alice's session ─┐                         ┌─ web view  GET /?token=…
bob's session   ─┼─ hooks + MCP ─▶ hub ◀───┤  teambus status / doctor
carol's session ─┘   (token = identity)    └─ git post-commit → resolve intents
```

## Quick start
**Hub** (Docker, HTTPS via Caddy — see `docs/deploy.md`):
```bash
echo "TEAMBUS_DOMAIN=teambus.example.com" > .env && docker compose up -d --build
docker compose run --rm --no-deps hub python server.py add-agent alice-frontend frontend   # prints her token
```
**Each teammate** — either the CLI…
```bash
./teambus init --hub https://teambus.example.com --agent-id alice-frontend --role frontend --token <token>
./teambus doctor
```
…or the plugin: `claude plugin marketplace add <this folder or repo>` then
`claude plugin install teambus@teambus-local --scope project --config hub_url=https://teambus.example.com --config token=<token> --config agent_id=alice-frontend --config role=frontend`

**Easiest team setup (VPN / Tailscale / LAN, no Docker):** on one machine run `./teambus-hub start`, then `./teambus-hub add alice-frontend frontend` — it prints a one-line `teambus-join …` invite per person. Each teammate pastes it inside their project (needs `python3` + `claude`; Linux/macOS). Details: `docs/team-setup.md`.

**Try it without installing a hub:** a hosted sandbox hands every visitor a private, throwaway hub (3 agents, deleted after an hour): `docs/sandbox.md`.

Deploying the public sandbox on Render's free tier: `render.yaml` and `docs/sandbox.md` (read the limits there first).

Local dev without Docker: `pip install 'mcp<2'`, then `TEAMBUS_TOKENS=tokens.json python server.py` (or `TEAMBUS_NOAUTH=1` for a throwaway local hub).

## What the agents get
| Tool | Purpose |
|---|---|
| `post_intent`, `resolve_intent` | "I'm editing these files" (auto-posted by hooks; resolved on `git commit`, by the agent, or after `TEAMBUS_INTENT_TTL`) |
| `publish_contract(name, spec, files, source_files)`, `resolve_contract` | announce an API/schema change; consumers get blocked + warned until each consumer file is **committed** (git hook) or the publisher resolves it |
| `check_conflicts` | same file · contract consumer · same directory + same function names · imports a file a teammate is changing |
| `ask_teammate_agent(to_agent, question, reply_to?)`, `check_inbox`, `thread` | async Q&A with threads; replies mark the question answered; warns if the recipient is offline >24 h |
| `team_feed`, `report_build`, `whoami` | presence + activity, build results, identity |

Hooks: `SessionStart` (identity, protocol, what's new) · `UserPromptSubmit` (only what's new since the last prompt) · `PreToolUse` Edit/Write (**blocks a conflicting edit until the agent has actually messaged the teammate** — the hub checks; 3 blocks max so a broken MCP can't lock editing; hub down = never blocks) · `PostToolUse` (records edits; Bash build passes) · `PostToolUseFailure` (failed builds, from the real exit code).

## Layout
`server.py` hub · `web/index.html` web view · `hooks/hook.py` Claude Code hook · `teambus` CLI · `.claude-plugin/`, `hooks/hooks.json`, `.mcp.json` plugin · `teambus-hub` / `teambus-join` easy setup · `sandbox.py` public sandbox · `render.yaml` · `Dockerfile`, `docker-compose.yml`, `docker-compose.sandbox.yml`, `Caddyfile*` · `docs/` (deploy guide, real-session proof, transcripts) · `ARCHITECTURE.md`

## Tests
```bash
pip install 'mcp<2' pytest playwright
pytest test_teambus.py test_sandbox.py      # 38 tests (a number of them spawn real server processes)
# test_teambus.py (19): auth, spoofing, TTL/resolve, conflicts, threads, presence, builds, hub-down, CLI, git hook
# test_sandbox.py (19): isolation, per-IP/global/size/rate limits, expiry + shutdown cleanup, DB cap, MCP via the proxy
python test_webview.py URL VIEWER ALICE_TOKEN BOB_TOKEN shot.png   # 12 checks in real Chromium against a running hub
```
The real-session evidence (actual `claude -p` runs, with the bugs they exposed) is in `docs/real-session-proof.md`; condensed transcripts in `docs/proof-summaries/` (the raw recordings are kept out of the repository).

## Known gaps
- "Ask first" is enforced by checking that *a* message was sent to *a* conflicting teammate since the block (not that it was a good question, or answered). After 3 blocks the edit goes through.
- Conflict detection: Python uses `ast` (symbols, imports); JS/TS (and anything else) is still regex (function/arrow names, relative `import`/`require`). No cross-language graph, no semantic merge-conflict prediction.
- A contract is "done" for a consumer file when *anyone but the publisher* commits that file; committing an unrelated change to it counts too.
- No rate limiting; the web-view token is passed in the URL. Run on a VPN or behind Caddy rules if internet-facing.
- Agent free-text reaches other agents' context (prompt-injection surface among teammates).

## Licence
MIT, see `LICENSE`. Vendored libraries and fonts in `web/assets/` keep their own licences (`web/assets/THIRD-PARTY.txt`); review GSAP's before redistributing commercially.
