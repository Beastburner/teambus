# TeamBus

Your teammates' Claude Code agents see what each other are doing, ask each other questions asynchronously, and get stopped **before** they write conflicting code. One hub (MCP server + REST + SQLite + read-only web view); every session connects with its own token. A hub that is down never blocks a session.

```
alice's session ─┐                         ┌─ web view  GET /?token=…
bob's session   ─┼─ hooks + MCP ─▶ hub ◀───┤  teambus status / doctor
carol's session ─┘   (token = identity)    └─ git post-commit → resolve intents
```

## Install
You need `git`, `python3` (3.9 or newer) and the [Claude Code](https://claude.com/claude-code) CLI. Linux or macOS (on Windows use WSL).

```bash
curl -fsSL https://raw.githubusercontent.com/Beastburner/teambus/main/install.sh | sh
```
This clones TeamBus to `~/.teambus` and links `teambus`, `teambus-join` and `teambus-hub` into `~/.local/bin` (it tells you if that folder is not on your `PATH`). Run it again to update. To remove it: `rm -rf ~/.teambus ~/.local/bin/teambus*` (projects you joined keep working: their hooks do nothing once TeamBus is gone).

**Then, one of three things:**
1. **Try it in 30 seconds:** open the hosted sandbox page, pick your agents and roles, and paste the `teambus-join ...` line it gives you inside any project folder. You can hold several sandboxes at once and add or delete agents later.
2. **Join your team's hub:** paste the one-line `teambus-join ...` invite the hub owner sent you, inside your project folder.
3. **Run a hub for your team:** `teambus-hub start`, then `teambus-hub add alice-frontend frontend` (see `docs/team-setup.md`; HTTPS with Docker is in `docs/deploy.md`).

Manual install instead of the script: `git clone https://github.com/Beastburner/teambus ~/.teambus` and run `~/.teambus/teambus-join ...`.

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

Deploying the public sandbox on Render's free tier (and `docs/firebase.md` to keep a Firestore record of every sandbox so dead links explain themselves): `render.yaml` and `docs/sandbox.md` (read the limits there first).

Local dev without Docker: `pip install 'mcp<2'`, then `TEAMBUS_TOKENS=tokens.json python server.py` (or `TEAMBUS_NOAUTH=1` for a throwaway local hub).

## What the agents get
| Tool | Purpose |
|---|---|
| `post_intent`, `resolve_intent` | "I'm editing these files" (auto-posted by hooks; resolved on `git commit`, by the agent, or after `TEAMBUS_INTENT_TTL`) |
| `publish_contract(name, spec, files, source_files)`, `resolve_contract` | announce an API/schema change; consumers get blocked + warned until each consumer file is **committed** (git hook) or the publisher resolves it |
| `check_conflicts` | same file · contract consumer · same directory + same function names · imports a file a teammate is changing |
| `outbox`, `wait_for_messages(timeout_seconds)` | read receipts for what you sent (read? when? answered? replies); wait up to 55 s for an answer instead of polling |
| `ask_teammate_agent(to_agent, question, reply_to?)`, `check_inbox`, `thread` | async Q&A with threads; replies mark the question answered; warns if the recipient is offline >24 h |
| `team_feed`, `report_build`, `whoami` | presence + activity, build results (first line of the command only, credentials removed; a passing run is linked to the failure it fixed), identity |

Hooks: `SessionStart` (identity, protocol, what's new, unread mail) · `UserPromptSubmit` (only what's new since the last prompt) · `PreToolUse` Edit/Write (**blocks a conflicting edit until the agent has actually messaged the teammate** — the hub checks; 3 blocks max so a broken MCP can't lock editing; hub down = never blocks) · `PostToolUse` (records edits; Bash build passes; **delivers a teammate's new message to an agent that is busy working**, within a tool call) · `PostToolUseFailure` (failed builds, from the real exit code) · `Stop` (**if a teammate wrote while the agent worked, hands it over once before the agent finishes**; never loops). A project's own settings always win over an inherited environment, and each join carries its token in a variable unique to that join, so agents started from another TeamBus project or a shell that exports `TEAMBUS_*` can't authenticate as the wrong agent.

## Messages and idle agents
A message reaches an agent only when its session runs: when its user submits a prompt, when it uses a tool, or when it finishes a turn. A session sitting at an empty prompt runs nothing, and nothing can wake it, so mail to an idle agent stays **unread** until its user gives it a turn. TeamBus tells you this: `ask_teammate_agent` says whether the recipient is working or idle, and the dashboard shows the reason next to every unread message.

To keep an agent available for teammates, tell it to loop on `wait_for_messages` ("stay available: call wait_for_messages, answer each message with `ask_teammate_agent(reply_to=<id>, …)`, repeat until I say stop"), or run `/loop 1m check the teambus inbox and reply` in that session. Read receipts (`outbox`, `teambus outbox`) show when a message was read and answered.

**`teambus listen`** watches the hub for your unread mail from a spare terminal in the project folder, so you do not have to notice it yourself:
```bash
teambus listen                      # prints a line, rings the bell, and sends a desktop notification when mail waits for an idle session
teambus listen --tmux 0:1.0         # also types one fixed line into that tmux pane to give the idle session a turn (only if the pane runs claude)
teambus listen --exec './wake.sh'   # or run your own command (gets TEAMBUS_COUNT, TEAMBUS_FROM, TEAMBUS_AGENT)
```
It leaves a working session alone (its hooks deliver within seconds), reminds you at most 3 times, and never puts message text in the notification or the typed line, only the count and sender names, so a teammate's words cannot become keystrokes. Run Claude Code inside tmux (`tmux new -s alice`, then `claude`) to use `--tmux`; find the pane with `tmux list-panes -a`. Checking every few seconds also keeps a free-tier sandbox host awake while you work.

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

## When the MCP tools do not load
Hooks work without them, and so does the shell: `teambus send <agent> "text" [--reply-to N]`, `teambus inbox`, `teambus outbox` use the same hub. Run `/mcp` in Claude Code to reconnect (a sleeping free-tier hub, or a token the server rejected, are the usual causes; `teambus doctor` says which). Anything that looks like a credential is removed from messages, build commands and logs before it is stored, and the sender is told.
