# Feature research: what would make TeamBus more usable (Nov 2026)

Status: research only, nothing here is built. Sources are mostly vendor blogs and practitioner posts, so treat them as design direction, not proof.
Not found in this pass: merge-queue tooling, and Linear / Notion / Figma presence patterns.

## What the research shows
- **Visibility beats chat.** Teams want to see who owns what and what is blocked, instead of constant check-ins ([Zoho](https://www.zoho.com/blog/cliq/team-communication-in-2026.html)). TeamBus already does this (intents, tasks, team map).
- **Hooks beat telemetry for agents.** A Claude Code team dashboard reads hook events because OpenTelemetry misses tool-level detail ([Show HN summary](https://www.pulse.bot/ai/news/show-hn-real-time-dashboard-for-claude-code-agent-teams-298d7a5e-8671-43f1-b26a-dd21730d9a63)). TeamBus is already hook-based.
- **Cost and usage per agent is the common ask** ([Dash0](https://www.dash0.com/blog/introducing-agentic-engineering-intelligence-stop-guessing-what-your-coding-agents-deliver), [Coralogix](https://coralogix.com/blog/code-agents-need-observability)).
- **Conflicts survive worktrees.** Shared files (routes, config, shared types), databases, ports and build locks still collide; one source mentions warning before two agents touch the same function ([Nimbalyst](https://nimbalyst.com/blog/best-git-worktree-tools-ai-coding-2026/), [MindStudio](https://mindstudio.ai/blog/git-worktrees-parallel-ai-coding-agents)).
- **The inbox is the human-agent pattern.** A review queue with tiers (auto-approve, notify, block) to avoid approval fatigue, showing what the agent intends, with an audit trail ([Zoho](https://www.zoho.com/agentinbox/articles/human-approval-loops-ai-email-agents.html), [DEV](https://dev.to/omnithium/human-in-the-loop-patterns-for-high-stakes-ai-agent-decisions-1fg6)).
- **Board-per-agent is popular.** Vibe Kanban: one card = one worktree = one agent; Conductor: review, then PR with CI ([Morph](https://www.morphllm.com/comparisons/superset-vs-conductor-vs-emdash)).

## Ideas, best first
1. **Decision inbox.** One queue of what needs a human (open questions, conflicts, blocked tasks, contracts awaiting an answer): answer / approve / snooze, with an audit trail. Needs the owner-mode permission design. The alerts badge already covers the notification half.
2. **Predictive conflict warning.** Warn before two agents edit the same function or class, and for shared hot files (routes, config, types). We already store the symbols each agent says it touches.
3. **Cost and activity per agent.** Tokens, tool calls, builds passed/failed, time blocked, per agent and per task. Hooks already see the traffic.
4. **Handoff and session recap.** "What happened while I was away", a daily digest per agent, a one-click handoff note. `digest` and `handoff` exist in the hub with no UI.
5. **Time slider and replay.** Scrub the graph and timeline back in time. Needs the hub to store file-edit history.
6. **Task board tied to agents.** Cards linked to the agent and the files it touched, drag to reassign, link into the graph.
7. **Guardrail tiers.** Per-folder rules (e.g. `infra/` blocks, `docs/` advises) instead of one global passive/full mode.
8. **Shared-resource locks.** Claim a port, a database or a migration so agents do not collide where worktrees do not help.
9. **Spawn from the UI.** "Add an agent" from role templates (reviewer, tester, docs); `create_agent` already exists over MCP.
10. **Integrations.** Slack for conflicts and blocks, GitHub issue/PR links, CI status on builds.

## Suggested order
2, 3, 4 first (data already collected, no new permissions), then 1 (owner mode), then 5 and 7 as larger projects.

## Known gaps in what is built today
Agent and conversations pages not checked with real data; alerts notifications and text-size controls not clicked in a browser; no diagrams for Go/Java/Rust; no Prisma/TypeORM in ER; Memory on Render needs the repo mounted (a clone-from-GitHub option would fix that).
