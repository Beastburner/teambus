## TeamBus (team coordination)
You share a codebase with teammates whose Claude agents are on the `teambus` MCP server. The hub knows who you are from your token; `agent_id` arguments are optional.
- Edits and builds are reported automatically by hooks. If an edit is blocked with a TEAMBUS CONFLICT, call `ask_teammate_agent` (exact agent_id of the teammate) before retrying the edit.
- Before changing an API response, DB schema or shared interface: call `publish_contract(name, spec, files, source_files)` — `files` are the files that consume it, `source_files` the files you changed — so teammates' agents get warned.
- Never guess how a teammate's code behaves. Ask via `ask_teammate_agent(to_agent, question)`; answers arrive in their inbox asynchronously, so continue other work.
- When you see a message in your inbox, answer it with `ask_teammate_agent(reply_to=<its id>, question=<answer>)`.
- Run the build/tests before finishing so the result is reported. When you finish a piece of work, call `resolve_intent`.
