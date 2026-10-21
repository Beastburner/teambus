#!/usr/bin/env python3
"""TeamBus Claude Code hook. Usage: hook.py {start|prompt|pre|post|fail|stop}. Env: TEAMBUS_URL, TEAMBUS_TOKEN (identity comes from the token), AGENT_ID/AGENT_ROLE (display only).
Any hub error is swallowed: a down hub must never break or block a session."""
import ast, json, os, re, sys, textwrap, time, urllib.error, urllib.request

def _project_env():
    """The project's own .claude/settings*.json env block. It WINS over the ambient environment: a session started from another TeamBus project (a parent Claude
    session, or a shell exporting TEAMBUS_*) must never make this project talk to somebody else's hub, or authenticate as somebody else."""
    root, out = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(), {}
    for f in ("settings.json", "settings.local.json"):
        try: out.update(json.load(open(os.path.join(root, ".claude", f))).get("env") or {})
        except Exception: pass
    return out
PE = _project_env()
E = lambda k, o="": PE.get(k) or os.environ.get(k) or os.environ.get("CLAUDE_PLUGIN_OPTION_" + o) or ""  # project settings, then the environment, then the plugin's userConfig (CLAUDE_PLUGIN_OPTION_<KEY>)
URL, TOKEN = (E("TEAMBUS_URL", "HUB_URL") or "http://localhost:8765").rstrip("/"), E("TEAMBUS_TOKEN", "TOKEN")
ME, ROLE = E("AGENT_ID", "AGENT_ID") or "agent", E("AGENT_ROLE", "ROLE") or "dev"
PROTOCOL = ("Protocol: if an edit is blocked with a TEAMBUS CONFLICT, call ask_teammate_agent to the named teammate (exact agent_id), then retry the edit. Before changing an API/schema/shared interface call publish_contract(name, spec, files=consumers, source_files=yours). "
            "Answer inbox messages with ask_teammate_agent(reply_to=<id>, question=<answer>). Never guess a teammate's code: ask. Call resolve_intent when a piece of work is done.")
BUILD_RE = re.compile(r"\b(npm (run )?(build|test)|pnpm (build|test)|yarn (build|test)|pytest|make( test| build)?|cargo (build|test)|go (build|test)|gradle|mvn)\b")  # which Bash commands count as a build/test
FAIL_RE = re.compile(r"\b(FAILED|FAIL|ERROR|Traceback)\b|\bError:")
SYM_RE = re.compile(r"\b(?:def|class|function\*?|func|fn)\s+([A-Za-z_$][\w$]*)|\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[\w$]+\s*=>)")  # function/class names only, not locals
IMP_JS = re.compile(r"""(?:from\s+|import\s*\(?\s*|require\(\s*)['"](\.{1,2}/[^'"]*)['"]""")
IMP_PY = re.compile(r"^\s*(?:from\s+(\.*[\w.]*)\s+import\s+([\w, ]+)|import\s+([\w.]+))", re.M)
EXT = ["", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json", ".py"]

WHY = ""   # why the last call failed, in words (filled by api())

def api(name, **kw):
    global WHY
    try:
        req = urllib.request.Request(f"{URL}/api/{name}", json.dumps(kw).encode(), {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
        return json.load(urllib.request.urlopen(req, timeout=3))
    except urllib.error.HTTPError as e:
        try: msg = (json.loads(e.read()) or {}).get("message") or ""          # a sandbox explains itself: expired, deleted, lost in a restart, never existed
        except Exception: msg = ""
        WHY = msg or ("the hub rejected the token" if e.code in (401, 403) else f"the hub answered HTTP {e.code}")
        return None
    except Exception: WHY = "the hub is unreachable"; return None

def ctx(event, text): print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))

STATE = os.path.join(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(), ".claude", "teambus-state.json")
def state():
    try: return json.load(open(STATE))
    except Exception: return {}
def save(st):
    try: os.makedirs(os.path.dirname(STATE), exist_ok=True); json.dump(st, open(STATE, "w"))
    except Exception: pass

def py_tree(text):
    try: return ast.parse(textwrap.dedent(text))
    except (SyntaxError, ValueError): return None  # an edit fragment may not parse on its own

def symbols(ti, py=False):
    out = set()
    for piece in [ti.get("new_string"), ti.get("old_string"), ti.get("content")] + [e.get("new_string") for e in ti.get("edits", [])]:
        if not piece: continue
        t = py_tree(piece) if py else None
        out |= {n.name for n in ast.walk(t) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))} if t else {n for g in SYM_RE.findall(str(piece)) for n in g if n}
    return sorted(out)

def imports(root, rel, text):
    """Repo-relative files that `text` (python/js/ts source of `rel`) imports and that exist on disk."""
    d, out, cand = os.path.dirname(rel), set(), []
    for m in IMP_JS.finditer(text): b = os.path.normpath(os.path.join(d, m[1])); cand += [b + e for e in EXT] + [b + "/index" + e for e in EXT]
    tree = py_tree(text) if rel.endswith(".py") else None
    mods = [(n.level, n.module or "", [x.name for x in n.names]) for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)] + [(0, x.name, []) for n in ast.walk(tree) if isinstance(n, ast.Import) for x in n.names] if tree else \
           [(len(m[1]) - len(m[1].lstrip(".")), m[1].lstrip("."), [n.strip() for n in (m[2] or "").split(",") if n.strip()]) if m[1] is not None else (0, m[3], []) for m in IMP_PY.finditer(text)]
    for dots, mod, names in mods:
        base = os.path.join(d, *[".."] * (dots - 1)) if dots else ""; parts = [x for x in mod.split(".") if x]
        for b in {os.path.normpath(os.path.join(base, *parts))} | ({os.path.normpath(os.path.join(d, *parts))} if dots == 0 else set()):
            cand += [b + ".py", b + "/__init__.py"] + [os.path.normpath(os.path.join(b, n)) + ".py" for n in names]
    return sorted({c for c in cand if c and os.path.isfile(os.path.join(root, c)) and c != rel})

def fmt(m):
    t = m["type"]; what = m.get("summary") or m.get("name") or ("build ok" if m.get("ok") else "build FAILED: " + (m.get("log_tail") or "")[-120:].replace("\n", " "))
    return f"- [{t}] {m.get('agent_id')}: {what}"

def digest_lines(dg, cap=10):
    L = []
    for m in dg.get("inbox") or []: L.append(f"- NEW MESSAGE from {m['from_agent']} (#{m['id']}" + (f", reply to #{m['reply_to']}" if m.get("reply_to") else "") + f"): {m['body']}")
    if L: L.insert(0, "Inbox (answer questions with ask_teammate_agent reply_to=<id>):")
    items = [fmt(x) for x in (dg.get("items") or [])[-cap:]]
    return L + (["Team activity:"] + items if items else [])

def unread_note(throttle=4.0):
    """Unread messages for this agent as text, or "" (also when the hub is down). Marks them read on the hub. Throttled so a burst of tool calls costs one request."""
    st = state()
    if time.time() - st.get("inbox_ts", 0) < throttle: return ""
    st["inbox_ts"] = time.time(); save(st)
    return "\n".join(digest_lines({"inbox": api("inbox") or []}))

def main(mode):
    d = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    ti = d.get("tool_input") or {}
    root = os.environ.get("CLAUDE_PROJECT_DIR") or d.get("cwd") or os.getcwd()
    path = ti.get("file_path")
    if path and os.path.isabs(path): path = os.path.relpath(path, root)  # teammates' checkouts live at different absolute paths
    if mode in ("start", "prompt"):
        st = state(); dg = api("digest", since=st.get("cursor", 0))
        if dg is not None: st["cursor"] = dg["now"]; save(st)
        L = digest_lines(dg or {})
        if mode == "start": ctx("SessionStart", "\n".join([f"TeamBus: the hub identifies you as agent '{dg['me']}' (from your token), so agent_id arguments are optional." + " " + PROTOCOL if dg else f"TeamBus is offline ({WHY or 'hub unreachable or token rejected'}). Continue without it; do not retry its tools."] + L))
        elif L: ctx("UserPromptSubmit", "TeamBus update since your last prompt:\n" + "\n".join(L))
    elif mode == "pre" and path:
        try: text = open(os.path.join(root, path)).read() + " " + str(ti.get("content") or "")
        except OSError: text = str(ti.get("content") or "")
        r = api("conflicts", files=[path], symbols=symbols(ti, path.endswith(".py")), imports=imports(root, path, text))
        if r and r["conflicts"]:
            lines = [f"TEAMBUS CONFLICT for {path}:"]
            for c in r["conflicts"]:
                k = c["kind"]; lines.append(f"- {c['with']} " + {"same_file": f"is changing the same file: {c.get('summary')}", "contract_changed": f"changed contract '{c.get('contract')}': {c.get('spec')}",
                    "same_dir_symbols": f"is working in the same directory on the same names {c.get('symbols')} ({', '.join(c['files'])}): {c.get('summary')}", "imports_changed": f"is changing {', '.join(c['files'])}, which this file imports: {c.get('summary')}"}[k])
            who = sorted({c["with"] for c in r["conflicts"]}); lines.append(f"This edit is blocked until you have messaged a teammate. Call ask_teammate_agent(to_agent={who[0]!r}, ...) to confirm how to proceed (use the exact agent_id, not the role), then retry the edit.")
            key = path + "|" + json.dumps(sorted(str((c["with"], c["kind"], c.get("contract") or c.get("summary"))) for c in r["conflicts"]))
            st = state(); den = st.setdefault("denied", {}); rec = den.get(key)
            if rec is None:  # first time: block so the agent must react
                den[key] = {"ts": time.time(), "n": 1}; save(st)
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "\n".join(lines)}}))
            elif rec["n"] < 3 and not any((api("asked", to_agent=w, since=rec["ts"]) or {"asked": True})["asked"] for w in who):  # a retry is allowed only once the agent has actually messaged a teammate (hub unreachable counts as asked; 3 strikes max so a broken MCP can't lock editing)
                rec["n"] += 1; save(st)
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": f"Still blocked (attempt {rec['n']}/3): you have not sent {' or '.join(who)} a message since the first block. Call ask_teammate_agent(to_agent={who[0]!r}, question=...) first, then retry the edit.\n" + "\n".join(lines[:-1])}}))
            else: ctx("PreToolUse", "\n".join(lines[:-1]) + "\nYou have contacted a teammate (or the hub is unreachable): proceeding.")
    elif mode == "stop":
        # About to finish. If a teammate wrote while we worked, hand it over once instead of going idle with unread mail (stop_hook_active: we already did, so never loop).
        if not d.get("stop_hook_active"):
            note = unread_note(0)
            if note: print(json.dumps({"decision": "block", "reason": "TeamBus: before you finish, a teammate's agent wrote to you. Read and answer it (or say why not):\n" + note}))
    elif mode == "post":
        if path: api("post_intent", files=[path], summary=f"edited {os.path.basename(path)}", symbols=symbols(ti, path.endswith(".py")), role=ROLE)
        elif d.get("tool_name") == "Bash" and BUILD_RE.search(ti.get("command", "")):  # PostToolUse only fires for commands that exited 0
            out = d.get("tool_response") or {}; text = ((out.get("stdout") or "") + (out.get("stderr") or "")) if isinstance(out, dict) else str(out); cmd = ti["command"]
            masked = re.search(r"(?<!\|)\|(?!\|)", cmd) and "pipefail" not in cmd  # `make test | tail` exits 0 whatever make did: only then fall back to output markers
            api("build", ok=not out.get("interrupted") and not (masked and FAIL_RE.search(text)), cmd=cmd, log_tail=text)
        note = unread_note()      # mid-task delivery: a teammate's message reaches a busy agent within a tool call, not only at its next prompt
        if note: ctx("PostToolUse", "TeamBus: a teammate wrote to you while you were working.\n" + note)
    elif mode == "fail" and d.get("tool_name") == "Bash" and BUILD_RE.search(ti.get("command", "")) and not d.get("is_interrupt"):  # PostToolUseFailure: non-zero exit, `error` starts "Exit code N"
        api("build", ok=False, cmd=ti["command"], log_tail=d.get("error") or "")

if __name__ == "__main__":
    try: main(sys.argv[1])
    except Exception: pass  # never break the session
