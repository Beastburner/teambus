# Real-session proof (Claude Code 2.1.304, `claude -p`, two sessions, one hub)

Setup: demo repo (`backend/app.py` Flask `GET /users`, `frontend/src/UserList.js` reading `u.name`), cloned twice (`demo/bob`, `demo/alice`). Each clone has `.claude/settings.json` (hooks + `AGENT_ID`/`AGENT_ROLE` env), `.mcp.json` (teambus over streamable HTTP) and the CLAUDE.snippet.md appended to `CLAUDE.md`. Hub on 127.0.0.1:8765, local SQLite. Driver: `docs/run_scenario.sh`; condensed view: `docs/summarize.py <transcript>`; condensed transcripts are in `docs/proof-summaries/` (the raw `stream-json` recordings, where hook stdout appears as `hook_response` events, are not part of this repository).

Scenario: bob is told to rename `name` -> `fullName` in the API and tell teammates; then alice is told only "update the user list component so each user's name is shown in bold".

## What broke, in order (each fixed before the next run)
| Run | Local recording (not published) | Result | Root cause → fix |
|---|---|---|---|
| 1 | `transcripts-run1-failed` | **Fail.** Alice edited without any warning. `PreToolUse` printed nothing. | (a) Bob's agent passed `files` as the string `"backend/app.py, frontend/src/UserList.js"`; tool args were untyped so the hub stored a string and `set("…")` is a set of characters → never matches. → type hints (`files: list[str]`, `ok: bool`, …) so MCP schemas validate. (b) Bob's agent called itself `bob`, not `bob-backend`, because `SessionStart` printed nothing on an empty hub. → `SessionStart` now always states the agent's id. (c) Hook sent absolute paths; two checkouts never match. → hook sends paths relative to `CLAUDE_PROJECT_DIR`/`cwd`. |
| 2 | `transcripts-run2-warning-after-edit` | Hook fired and the warning reached the model, **but after the edit had already been issued**. Alice's own summary: "the edit ran in the same batch as the TeamBus conflict warning". | `PreToolUse` `additionalContext` is delivered with the tool result; it cannot stop the tool. → conflicts now return `permissionDecision: "deny"` the first time per (file, conflict); the retry is allowed. |
| 3 | `transcripts-run3-agent-retried-without-asking` | Edit was denied before writing (blocking works). Alice then judged her edit compatible (`u.fullName ?? u.name`) and retried **without** asking Bob. | My deny text offered "or retry if compatible". → text now requires `ask_teammate_agent(to_agent='<exact id>')` first. |
| 4 | `transcripts/` | **Pass** (below). | |

## Run 4 (alice), from `transcripts/alice.jsonl`
1. `SessionStart` hook stdout → `additionalContext`: "this session's agent_id is 'alice-frontend' … Recent team activity: [contract] bob-backend: GET /users".
2. Agent calls `team_feed`, `check_inbox`, then `Edit frontend/src/UserList.js` (`${u.fullName}` in `<strong>`).
3. `PreToolUse:Edit` hook stdout → `permissionDecision: "deny"`, reason: "TEAMBUS CONFLICT … bob-backend changed contract 'GET /users': BREAKING: `name` renamed to `fullName` … Call ask_teammate_agent(to_agent='bob-backend', …)". The file is unchanged at this point; Claude Code surfaced it as `PreToolUse:Edit hook error: …`.
4. Agent calls `mcp__teambus__ask_teammate_agent(from_agent="alice-frontend", to_agent="bob-backend", question="Your GET /users contract says `name` is renamed to `fullName` … Is the rename final and deployed, or should I support both fields during a transition?")`.
5. Agent retries the edit; `PreToolUse` now returns only `additionalContext` (not a deny); `PostToolUse` posts the intent.
6. Hub row: `messages(1, alice-frontend → bob-backend, "Your GET /users contract says …")`.

## What this does NOT prove (be skeptical of these)
- **Asking is enforced only softly.** The deny is once-per-conflict; the retry is allowed whether or not the agent actually asked. Run 4 asked; nothing guarantees it. (Phase 2 could make the retry conditional on a message from this agent to the owner existing on the hub.)
- **Bob never read Alice's question.** I only ran two sessions in sequence; the inbox → reply → asker-sees-answer loop is not tested on real sessions (Phase 2.4).
- **Build reporting is untested on real sessions.** Bob's `Bash` calls were refused for approval in `-p` mode, so no `report_build` fired. Docs say a failing Bash command fires `PostToolUseFailure`, not `PostToolUse`; the current hook only listens to `PostToolUse`, so **real failed builds are not reported yet** (the simulated test in `test_two_agents.py` step 6 passes only because it hand-feeds a PostToolUse payload).
- Bob's agent addressed one heads-up message to the role `frontend` instead of an agent id (run 3: `to_agent='frontend'`); nobody receives it. Needs an agent directory / role routing (Phase 2.5).
- LLM behaviour varies run to run; this is 1 passing run after 3 failing ones, not a statistical result.


---
# Phases 2–5: what the real sessions showed
All runs: Claude Code 2.1.304, `claude -p`, one clone per teammate of `demo2` (Flask `backend/` + JS `frontend/src/`, `make test` contract check). Condensed transcripts: `docs/proof-summaries/<id>.txt`; raw: re-run `docs/run_scenario.sh`-style (`claude -p … --output-format stream-json --verbose`).

| # | What I did (like a user) | Result | Bug it exposed → fix |
|---|---|---|---|
| 2.1 | curl with no/wrong/alice/viewer tokens; real session: whoami + `post_intent(agent_id="bob-backend")` with alice's token | 401/401/200/403; hub recorded the intent as **alice-frontend**; `${TEAMBUS_TOKEN}` in `.mcp.json` expanded from settings `env` | — |
| 2.1b | real session with a wrong token | MCP `failed` (401), session still answered `2+2`; but SessionStart still claimed "you are carol" | SessionStart now asks the hub (`digest.me`) and says "TeamBus is offline" if it can't |
| 2.3 | bob edits `api.js`; alice edits `UserList.js` (imports it); carol writes `loadUser` in a new file in the same dir | alice denied: *"bob-backend is changing api.js, which this file imports"*; carol denied: *"same names ['loadUser']"*; both asked bob, then retried | symbol regex first captured locals (`res`) → restricted to function/class/arrow names |
| 2.4 | bob's session started with 2 unanswered questions and answered both with `reply_to` | replies routed to each asker, originals `answered=1`; alice's next session got the answer at SessionStart; `thread` returned the conversation | — |
| 2.4b | one live multi-turn session (stream-json input), carol posts activity between prompts | turn 2 got *only* the new message + intent; turn 3 got nothing (`docs/multiturn_test.py`) | — |
| 2.5 | alice asks never-connected `dave-ops` and `bob-backend` aged to 30 h | warnings *"never connected"* / *"offline 30h"* in the tool result | — |
| 2.2 | alice edits (intent open) → carol denied → alice **really** `git commit`s → carol retries; separate run with `TEAMBUS_INTENT_TTL=6` | commit resolved the intent via the post-commit hook; after TTL no conflict | (my first step-4 was a no-op because carol's earlier retry had landed; redone with a clean clone) |
| 2.7 | bob: `make test` pass, rename field, `make test` fail (once piped through `tail`, once bare) | bare failure → `PostToolUseFailure` → `ok=0` with `Exit code 2`; **piped failure was recorded `ok=1`** because `tail` exits 0 | added: if the command has a pipe without `pipefail`, check output for failure markers |
| 3.1 | fresh clone with pre-existing `settings.local.json`/`CLAUDE.md`: `doctor` → `init` → `doctor` → real session | doctor 7 FAIL → 11 PASS; user's own hook/permissions/env/CLAUDE.md preserved; second `init` is a no-op | `"url": "${TEAMBUS_URL}/mcp"` is rejected by Claude Code ("invalid URL") → `init` writes the literal hub URL, token stays `${TEAMBUS_TOKEN}`; doctor checks for it |
| 3.1b | doctor with wrong token / hub down; real edit session with hub down | FAIL lines + exit 1; session and edit unaffected | — |
| 3.2 | plugin: `marketplace add` + `plugin install --config …` in a pristine HOME, real session | MCP `plugin:teambus:hub` connected, whoami = frank-devops, hooks fired via `CLAUDE_PLUGIN_OPTION_*`, protocol injected | — |
| 4 | built the image, refused-to-start check, `add-agent`, compose with Caddy at `https://localhost`, two real sessions over HTTPS, `down`/`up`, backup, token revoke | all worked; data survived restart; revoked token 401 immediately | sandbox only: Docker Hub 429 (used `mirror.gcr.io` base) and a TLS-intercepting proxy (pip needed the sandbox CA; shipped Dockerfile unchanged) |
| 5 | Chromium via Playwright on the container hub (`test_webview.py`), screenshot reviewed | 12/12: token gate, presence, open question, contract, failed build, live update without reload, `<img onerror>` inert, viewer POST → 403 | feed row had no space after "contract" → fixed |

## Still not proven
- A real public domain / Let's Encrypt certificate (tested Caddy's local CA only); `docker compose build` against Docker Hub without the mirror.
- More than 3–4 simultaneous sessions, or any load.
- Interactive (non-`-p`) Claude Code UX: how the deny message renders to a human, the plugin `userConfig` dialog (I passed `--config` on the CLI), and project-scope MCP approval prompts (bypassed with `enabledMcpjsonServers`).
- Windows/macOS (hooks use `python3` and POSIX paths).
- Whether agents *reliably* ask rather than retry: all 6 denied runs after the message was strengthened ended with the agent asking first, but that is a handful of runs of an LLM.


---
# Follow-ups (the three "next improvements")
| # | What I did | Result | Bug it exposed → fix |
|---|---|---|---|
| 6A | alice told "do NOT message anyone"; then told "if blocked, just retry up to three times" (`proof-summaries/6A-alice*.txt`) | 1st agent obeyed the user and gave up (correct). 2nd retried twice: *"Still blocked (attempt 2/3 … you have not sent bob-backend a message)"*, *"attempt 3/3"*, then it asked bob and the edit went through with "…proceeding" | allowed-state message still said "blocked until…" → reworded |
| 6B | bob renames + publishes a contract (consumer `format.js`); alice asks, edits, **really commits**; carol edits afterwards | contract open before the commit, `resolved` after (`done_files=["frontend/src/format.js"]`), carol's edit produced no warning | alice's agent calls `resolve_intent(files=…)` when done, which resolved the contract *before* the commit → only `committed=true` (git hook) acknowledges contracts now. Also my `reset.sh` left earlier commits in the clone (first rerun was a no-op) → reset to `origin/main` |
| 6C | bob creates `backend/utils.py` with `compute()`; carol writes a file whose *docstring/comment mention* `def compute()`; then a file that really defines `compute` | docstring/comment file: **no conflict**; real definition: denied (*"same names ['compute']"*), asked, proceeded | — (unit test shows the regex fallback *is* fooled by the docstring) |

Re-verified after these changes: `pytest test_teambus.py` (17 pass) and the web view in Chromium against the changed server (12/12, run on a local hub; `test_webview.py` now seeds its own data). **Not** re-verified: the Docker image/compose stack (built before these changes; `server.py` has changed since) — rebuild and rerun before deploying.
