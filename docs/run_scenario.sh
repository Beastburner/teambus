#!/bin/bash
# usage: run_scenario.sh <demo_dir> <out_dir>   (hub must be up on :8765; each clone has .claude/settings.json + .mcp.json)
D=$1; O=$2; mkdir -p $O
run(){ (cd $D/$1 && timeout 300 claude -p "$2" --mcp-config .mcp.json --output-format stream-json --verbose --permission-mode acceptEdits --allowedTools mcp__teambus "Bash(git diff:*)" "Bash(git status:*)" < /dev/null > $O/$1.jsonl 2> $O/$1.err); }
run bob "Backend task: in backend/app.py rename the user field \`name\` to \`fullName\` in the /users response. The React/JS frontend consumes this endpoint (frontend/src/UserList.js). Do not touch frontend files; tell teammates about the breaking change per the TeamBus protocol in CLAUDE.md."
run alice "Frontend task: update the user list component (frontend/src/UserList.js) so each user's name is shown in bold."
