"""Condense a claude -p stream-json transcript: hooks, tool calls, tool results, final text."""
import json, sys
for l in open(sys.argv[1]):
    e = json.loads(l); t = e.get("type")
    if t == "system" and e.get("subtype") == "hook_response":
        print(f"[hook {e.get('hook_name')}] exit={e.get('exit_code')} stdout={e.get('stdout','')[:700]!r}")
    elif t == "assistant":
        for c in e["message"]["content"]:
            if c["type"] == "tool_use": print(f"[tool_use] {c['name']} {json.dumps(c['input'])[:300]}")
            elif c["type"] == "text" and c["text"].strip(): print(f"[assistant] {c['text'][:500]}")
    elif t == "user":
        for c in e["message"]["content"] if isinstance(e["message"]["content"], list) else []:
            if c.get("type") == "tool_result": print(f"[tool_result] {str(c.get('content'))[:300]}")
    elif t == "result": print(f"[RESULT] {e.get('result','')[:600]}")
