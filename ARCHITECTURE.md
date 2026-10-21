# TeamBus architecture

One hub, N Claude Code sessions. Everything is async; the hub is the only shared state.

```
 session A (alice)                         hub (server.py)                       session B (bob)
 ┌──────────────────┐   MCP /mcp (tools)  ┌──────────────────────┐  MCP /mcp   ┌──────────────────┐
 │ Claude Code      │ ──────────────────▶ │ FastMCP (streamable  │ ◀────────── │ Claude Code      │
 │  hooks/hook.py   │   REST /api/*       │ HTTP) + Starlette    │   REST      │  hooks/hook.py   │
 │  (start|pre|post)│ ──────────────────▶ │ custom routes        │ ◀────────── │                  │
 └──────────────────┘                     │ SQLite: intents,     │             └──────────────────┘
                                          │ contracts, messages, │
                                          │ builds               │
                                          └──────────────────────┘
```

## Pieces
- `server.py` — plain functions (`post_intent`, `team_feed`, `ask_teammate_agent`, `check_inbox`, `publish_contract`, `report_build`, `check_conflicts`) registered twice: as MCP tools (for the agent) and as REST `POST` routes (for hooks, the CLI and the web view, which are plain scripts). The REST names are short aliases, not the tool names: `/api/whoami`, `post_intent`, `resolve`, `resolve_contract`, `asked`, `feed`, `ask`, `inbox`, `thread`, `contract`, `build`, `conflicts`, `digest` (plus `GET /api/status` for the web view). The MCP `check_inbox` tool wraps the REST list as `{unread, messages}` so an empty inbox is never a blank result. One SQLite file (`TEAMBUS_DB`).
- `hooks/hook.py {start|pre|post}` — Claude Code command hook. Reads the hook JSON on stdin, calls the REST mirror (3 s timeout, any error → silent no-op), prints `hookSpecificOutput.additionalContext` JSON to stdout.
  - `start` (SessionStart): inbox + last 10 feed items → context.
  - `pre` (PreToolUse Edit/Write): `check_conflicts` on the target file → warning context.
  - `post` (PostToolUse): Edit/Write → `post_intent`; Bash matching `BUILD_RE` → `report_build`.
- Delivery: messages are async, but they are pushed at the moments an agent can read them: `SessionStart` and `UserPromptSubmit` (digest), `PostToolUse` (a busy agent sees new mail within one tool call, throttled to one check per 4 s), and `Stop` (unread mail blocks the stop once, `stop_hook_active` prevents loops). An agent idle at its prompt with no session activity cannot be interrupted: Claude Code has no push channel into an idle session.
- Identity: every hook and the CLI read the project's `.claude/settings*.json` env first, then the environment; the MCP header uses `${TEAMBUS_TOKEN_<id>}`, a variable unique to this (hub, agent) join, because an inherited generic `TEAMBUS_TOKEN` otherwise beats the project's own value.
- `CLAUDE.snippet.md` — tells the agent the protocol (publish contracts, ask don't guess, check inbox).
- Hook wiring and env (`TEAMBUS_URL`, `TEAMBUS_TOKEN`, `AGENT_ID`, `AGENT_ROLE`) are written into each project's `.claude/settings.local.json` by `teambus init` (or `teambus-join`); there is no separate example file.

## Data flow for the core scenario
1. Bob's agent changes an API → `publish_contract(name, spec, files=[consumers])`.
2. Alice's agent is about to edit a consumer → PreToolUse hook calls `check_conflicts` → contract's `files` overlaps → warning injected before the edit.
3. Alice's agent calls `ask_teammate_agent` → row in `messages`; Bob's agent sees it at next `check_inbox`/SessionStart.

## Phase 0 findings (verified against current docs, 2026-10)
| Question | Finding | Action |
|---|---|---|
| Is `hookSpecificOutput.additionalContext` how context is injected? | Yes for SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure, Stop (plain stdout also works for SessionStart/UserPromptSubmit). Values are wrapped in a system reminder; docs advise factual statements, not imperatives. >10k chars is spilled to a file. | hook.py output shape is correct; keep. |
| Bash `tool_response` shape | `stdout`, `stderr`, `interrupted`, `isImage`. **No exit code, no `is_error`.** | hook.py's `out.get("is_error")` never fires. |
| Failed Bash commands | Non-zero exits fire **PostToolUseFailure** (`error` starts with `Exit code N`), *not* PostToolUse. | Current `post` hook never sees a failed build in a real session (only the simulated test did). Fix: add PostToolUseFailure hook (Phase 2.7). |
| Absolute paths | `tool_input.file_path` is absolute; teammates' checkouts differ. | Not in docs — found by design review; hook must send repo-relative paths (fixed in Phase 1). |
| MCP auth | `claude mcp add --transport http NAME URL --header "Authorization: Bearer T"`; `.mcp.json` supports `${VAR}` in `url`/`headers`. OAuth also exists but is overkill. | Phase 2: bearer token. |
| Plugins | `.claude-plugin/plugin.json` + `hooks/hooks.json` + `.mcp.json`; installed from a marketplace or `--plugin-dir`. | Phase 3. |
| Hooks under `claude -p` | Run; visible as `hook_response` events with `--output-format stream-json --verbose`. | Used for Phase 1 proof. |

---
# v0.2 (after Phases 1–5)

```
 teammate machine                                   VPS
 ┌───────────────────────────────┐        ┌───────────────────────────────────────────┐
 │ Claude Code session            │  HTTPS │ Caddy (TLS) ─▶ hub container (server.py)  │
 │  ├ hooks/hook.py  ─ REST /api/*───────────▶  Auth middleware: Bearer token → agent  │
 │  │  start | prompt | pre | post | fail      │  ├ MCP  /mcp   (same functions as tools)│
 │  ├ MCP "teambus"  ─ /mcp ────────────────▶  ├ REST /api/*  (hooks, CLI, web view)   │
 │  └ git post-commit ─ `teambus resolve`      │  ├ GET /  (web/index.html, read-only)    │
 │ `teambus` CLI: init | doctor | status       │  └ SQLite on volume /data (+ tokens.json)│
 └───────────────────────────────┘        └───────────────────────────────────────────┘
```
- **Identity**: `tokens.json` maps token → `{agent_id, role}`. Every function derives the caller from the request's token (`me()`); an `agent_id` argument is ignored unless `TEAMBUS_NOAUTH=1`. A separate shared `TEAMBUS_TOKEN` is read-only (`GET /`, `GET /api/status`).
- **Intents** are `open` until `resolve_intent` (tool, or the git post-commit hook via `teambus resolve --committed`) or until `TEAMBUS_INTENT_TTL`. Repeated edits of the same files refresh one row.
- **Conflict kinds** (`check_conflicts`): `same_file`, `contract_changed` (file is a listed consumer), `same_dir_symbols` (same directory + overlapping function/class names, extracted by regex in the hook), `imports_changed` (the file imports — via regex-resolved relative JS/TS/Python imports — something a teammate is changing or a contract's `source_files`). The hook **denies** the edit (`permissionDecision: deny`) and lets a retry through only once the hub confirms (`asked`) that this agent has messaged a conflicting teammate since the first block (max 3 blocks per distinct conflict; hub unreachable = allow). Python files are parsed with `ast` (symbols, imports; fragments that don't parse fall back to the regex); other languages use the regex.
- **Messages**: `reply_to` threads; replying marks the parent `answered` and routes to the asker; `thread` returns a conversation; recipient may be an agent_id or a unique role; a recipient never seen / offline > 24 h yields a warning in the tool result.
- **Context hygiene**: `SessionStart` shows identity + protocol + new activity since the local cursor (`.claude/teambus-state.json`); `UserPromptSubmit` injects only what is new since the last prompt (nothing → no output).
- **Builds**: `PostToolUse` (Bash exit 0) → ok, unless the command has a pipe without `pipefail` and the output contains a failure marker (then the exit status is masked, so the output is checked); `PostToolUseFailure` (non-zero exit) → failed with `Exit code N` + output.
- **Packaging**: repo root is also a Claude Code plugin (`.claude-plugin/plugin.json`, `hooks/hooks.json`, `.mcp.json` using `${user_config.*}`); the hook reads `CLAUDE_PLUGIN_OPTION_*` when `TEAMBUS_*` env isn't set.
- **Contract lifecycle**: a contract is `open` until every consumer file it lists has been *committed by someone other than the publisher* (`teambus resolve --committed` from the git hook → `resolve_intent(committed=true)`), or the publisher calls `resolve_contract`, or `TEAMBUS_CONTRACT_TTL` passes. An agent finishing work (`resolve_intent` without `committed`) does **not** count — found in a real run where it resolved a contract before the commit.
