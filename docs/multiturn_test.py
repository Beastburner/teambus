"""One live Claude Code session (alice), three prompts. Between prompts teammates act through the hub.
Prints, per turn, what the UserPromptSubmit hook injected. Expectation: turn 2 = only the NEW items; turn 3 = nothing."""
import json, subprocess, sys, urllib.request
D, URL = sys.argv[1], "http://127.0.0.1:8765"
def api(tok, name, **kw): return json.load(urllib.request.urlopen(urllib.request.Request(f"{URL}/api/{name}", json.dumps(kw).encode(), {"Content-Type": "application/json", "Authorization": "Bearer " + tok})))
p = subprocess.Popen(["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits"], cwd=D, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
def turn(text):
    p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"); p.stdin.flush(); hooks = []
    for l in p.stdout:
        e = json.loads(l)
        if e.get("subtype") == "hook_response" and e.get("hook_name", "").startswith(("UserPromptSubmit", "SessionStart")): hooks.append((e["hook_name"], e.get("stdout", "")))
        if e.get("type") == "result": return hooks, e.get("result", "")[:100]
def show(i, r): print(f"--- turn {i}: {r[1]!r}"); [print(f"   [{n}] {json.loads(s)['hookSpecificOutput']['additionalContext'] if s.strip() else '(empty)'}") for n, s in r[0]]
show(1, turn("Reply with just OK."))
api("tok-carol", "post_intent", files=["frontend/src/footer.js"], summary="adding footer", symbols=["renderFooter"])
api("tok-carol", "ask", to_agent="alice-frontend", question="Did you finish the Users header? I need it for the footer.")
show(2, turn("Reply with just OK again."))
show(3, turn("Reply with just OK a third time."))
p.stdin.close(); p.wait(timeout=60)
