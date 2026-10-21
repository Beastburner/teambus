# Running TeamBus for a startup team (5 to 7 people, several agents each)

This is the setup I recommend for roughly 5 to 7 people who each run 2 to 4 Claude Code agents (about 10 to 30 agents). It uses one persistent hub and nothing else: no database server, no accounts service.

## Decisions, and why
| Question | Decision | Why |
|---|---|---|
| Where does it run? | One always-on hub: a small VPS with Docker ([deploy.md](deploy.md), HTTPS) or a machine on your Tailscale network ([team-setup.md](team-setup.md)). **Not** the free public sandbox. | The sandbox is deleted by design, and a free host sleeps. A hub on a $5 box stays up. |
| Which database? | The built-in SQLite file, in WAL mode. Back up `hub.db`. | Measured on one core, 30 simulated agents each making a request every ~0.6 s (about 10 times a real agent's rate): 48 requests per second, 0 errors, 15 ms at the 95th percentile. 100 agents: 162 requests per second, 0 errors, 18 ms. Before WAL mode 100 agents managed 53 requests per second at 2.4 s. A team of 30 agents needs a fraction of that. |
| How do people get identities? | One token per agent, created by the hub owner with `teambus-hub person`. | Each agent has its own name, role and token, and one person's agents are grouped under that person (the "owner"). |
| How do agents address each other? | By agent name (`dana-backend`), by person (`@dana`, all of dana's agents) or by role (`@qa`, everyone with that role). | With several agents per person, a role is often held by more than one agent. |

## Set up
On the hub (once), for each person and the roles of their agents:
```bash
./teambus-hub start
./teambus-hub person dana frontend backend qa      # creates dana-frontend, dana-backend, dana-qa; prints one join line each
./teambus-hub person ravi backend devops
```
Send each person their own lines privately: a line contains a token. They run each line inside the project folder where that agent works, on their own machine (see [team-setup.md](team-setup.md) for the install step). Different agents of one person can work in different folders, or in the same repo on different branches.

Naming convention: `<person>-<role>`. The person is the owner, so `teambus status` and the dashboard group agents by who they work for. Use lowercase letters and digits for the person.

## Working together
- **Conflicts:** an agent about to edit a file another agent is editing is stopped and told who to ask. This works across people and machines, because it goes through the hub.
- **Asking:** `ask_teammate_agent(to_agent="ravi-backend", question=…)` for one agent, `"@ravi"` for all of ravi's agents, `"@qa"` for every QA agent. A group message is delivered as one message per agent, so read receipts and replies stay per agent. A group may have at most 12 agents.
- **Contracts:** before changing an API, schema or shared interface, an agent publishes a contract; agents that use it are warned.
- **Idle sessions don't see mail.** Agents only see messages when their session runs. For a long-running agent, run `teambus listen` (add `--tmux PANE` if the session runs in tmux) or tell the agent to loop on `wait_for_messages`. See "Messages and idle agents" in the README.
- **Shared repo:** each person works in their own clone, so make sure everyone pulls before starting. (A warning about a teammate's commits you have not pulled yet is not built.)

## Running it
- **Backups:** copy `hub.db` (and `tokens.json`) nightly. In WAL mode use `sqlite3 hub.db ".backup backup.db"`, not a plain file copy.
- **Leaving the team:** remove that person's lines from `tokens.json`; the hub notices on the next request and their agents are locked out. Re-running `teambus-hub person` never reissues an existing token.
- **Watching:** `./teambus-hub url` prints the read-only web view link (agents, who is editing what, messages with read status, build results).
- **Secrets:** the hub removes values that look like credentials from messages and build logs, but it is a filter, not a vault. Never paste secrets into agent conversations that go through TeamBus.

## What this does not give you
- **No per-person access control.** Every agent can see the whole team's activity and message anyone. That is the intent for one company; it is not a multi-tenant service. Run one hub per company or team that must stay apart.
- **No single sign-on or invite links.** The hub owner creates and hands out tokens.
- **One hub is one machine.** If it goes down, agents carry on working without coordination (their hooks say so) and catch up when it returns.
- **Beyond about 100 active agents** you would want a bigger machine or a different database; that is not built.
