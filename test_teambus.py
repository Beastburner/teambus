"""Regression tests (pytest). Spins up a real hub on :8799 with a temp DB/tokens and drives it over REST, MCP and the hook script."""
import asyncio, json, os, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

HERE = os.path.dirname(os.path.abspath(__file__)); HOOK = f"{HERE}/hooks/hook.py"; URL = "http://127.0.0.1:8799"
TOK = {"ta": {"agent_id": "alice", "role": "frontend"}, "tb": {"agent_id": "bob", "role": "backend"}, "td": {"agent_id": "dave", "role": "ops"}, "te": {"agent_id": "erin", "role": "qa"}}

@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    t = tmp_path_factory.mktemp("hub"); (t / "tokens.json").write_text(json.dumps(TOK))
    env = {**os.environ, "TEAMBUS_DB": str(t / "h.db"), "TEAMBUS_TOKENS": str(t / "tokens.json"), "TEAMBUS_TOKEN": "viewer", "PORT": "8799", "HOST": "127.0.0.1", "TEAMBUS_INTENT_TTL": "3"}
    p = subprocess.Popen([sys.executable, f"{HERE}/server.py"], env=env, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try: urllib.request.urlopen(URL + "/healthz"); break
        except Exception: time.sleep(.2)
    yield t; p.terminate()

def api(tok, ep, method="POST", **kw):
    r = urllib.request.Request(f"{URL}/api/{ep}", json.dumps(kw).encode() if method == "POST" else None, {"Content-Type": "application/json", "Authorization": f"Bearer {tok}"}, method=method)
    try: return json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e: return {"http": e.code, **json.load(e)}

def hook(mode, tok, payload, proj):
    r = subprocess.run([sys.executable, HOOK, mode], input=json.dumps(payload), capture_output=True, text=True,
                       env={**os.environ, "TEAMBUS_URL": URL, "TEAMBUS_TOKEN": tok, "CLAUDE_PROJECT_DIR": str(proj)})
    return json.loads(r.stdout)["hookSpecificOutput"] if r.stdout.strip() else {}

# ---- auth ----
def test_auth(hub):
    assert api("", "whoami")["http"] == 401 and api("bad", "whoami")["http"] == 401
    assert api("ta", "whoami")["agent_id"] == "alice"
    assert api("ta", "whoami", agent_id="bob")["agent_id"] == "alice"                       # claimed id ignored
    api("ta", "post_intent", files=["spoof.py"], summary="s", agent_id="bob")
    assert api("tb", "conflicts", files=["spoof.py"])["conflicts"][0]["with"] == "alice"    # recorded as alice, not bob
    assert api("viewer", "status", method="GET")["agents"] and api("viewer", "post_intent", files=["x"], summary="s")["http"] == 403
    assert api("viewer", "inbox")["http"] == 403

def test_mcp_needs_token_and_derives_identity(hub):
    async def go(tok):
        async with streamablehttp_client(f"{URL}/mcp", headers={"Authorization": f"Bearer {tok}"}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize(); return json.loads((await s.call_tool("whoami", {"agent_id": "bob"})).content[0].text)
    assert asyncio.run(go("ta"))["agent_id"] == "alice"
    with pytest.raises(BaseException): asyncio.run(go("wrong"))

# ---- intent lifecycle ----
def test_intent_resolve_and_ttl(hub):
    api("tb", "post_intent", files=["life.py"], summary="a"); api("tb", "post_intent", files=["life.py"], summary="b")  # same files -> refreshed, not duplicated
    assert len(api("ta", "conflicts", files=["life.py"])["conflicts"]) == 1
    assert api("tb", "resolve", files=["life.py"])["resolved"] == 1 and api("ta", "conflicts", files=["life.py"])["conflicts"] == []
    api("tb", "post_intent", files=["ttl.py"], summary="x"); assert api("ta", "conflicts", files=["ttl.py"])["conflicts"]
    time.sleep(3.5); assert api("ta", "conflicts", files=["ttl.py"])["conflicts"] == []   # TEAMBUS_INTENT_TTL=3

# ---- smarter conflicts ----
def test_same_dir_symbols_and_imports(hub, tmp_path):
    api("tb", "post_intent", files=["web/api.js"], summary="adding loadUser", symbols=["loadUser"])
    kinds = lambda **kw: {c["kind"] for c in api("ta", "conflicts", **kw)["conflicts"]}
    assert kinds(files=["web/users.js"], symbols=["loadUser"]) == {"same_dir_symbols"}
    assert kinds(files=["other/users.js"], symbols=["loadUser"]) == set()                  # different dir
    assert kinds(files=["web/users.js"], symbols=["somethingElse"]) == set()               # no name overlap
    assert kinds(files=["web/list.js"], imports=["web/api.js"]) == {"imports_changed"}
    (tmp_path / "web").mkdir(); (tmp_path / "web/api.js").write_text("export const x=1"); (tmp_path / "web/list.js").write_text("import {x} from './api.js'\n")
    h = hook("pre", "ta", {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "web/list.js"), "new_string": "y"}, "cwd": str(tmp_path)}, tmp_path)
    assert h["permissionDecision"] == "deny" and "imports" in h["permissionDecisionReason"]   # hook resolved the relative import from disk

def test_hook_import_resolution(tmp_path):
    sys.path.insert(0, f"{HERE}/hooks"); import hook as H
    for f in ("pkg/__init__.py", "pkg/util.py", "pkg/sub/mod.py", "src/a.ts", "src/b/index.js"): (tmp_path / f).parent.mkdir(parents=True, exist_ok=True); (tmp_path / f).write_text("")
    assert H.imports(str(tmp_path), "app.py", "from pkg import util\nimport pkg.sub.mod\n") == ["pkg/__init__.py", "pkg/sub/mod.py", "pkg/util.py"]
    assert H.imports(str(tmp_path), "src/c.tsx", "import a from './a'\nconst b = require('./b')\nimport x from 'react'") == ["src/a.ts", "src/b/index.js"]
    assert H.symbols({"new_string": "function loadUser(){ const res = 1 }\nconst fmt = (a) => a\ndef foo(): pass"}) == ["fmt", "foo", "loadUser"]

# ---- deny-once, relative paths, SessionStart identity ----
def test_deny_once_relative_paths(hub, tmp_path):
    api("tb", "contract", name="GET /u", spec="name->fullName", files=["ui/List.js"])
    ev = lambda root: {"tool_name": "Edit", "tool_input": {"file_path": f"{root}/ui/List.js", "new_string": "q"}, "cwd": str(root)}
    a, d = tmp_path / "a", tmp_path / "d"; a.mkdir(); d.mkdir()
    assert hook("pre", "ta", ev(a), a)["permissionDecision"] == "deny"
    h = hook("pre", "ta", ev(a), a); assert h["permissionDecision"] == "deny" and "Still blocked (attempt 2/3)" in h["permissionDecisionReason"]   # retry without asking: still blocked
    api("ta", "ask", to_agent="bob", question="is the rename final?")                      # ...now she really asked the contract's owner
    assert "permissionDecision" not in hook("pre", "ta", ev(a), a)                         # so the retry is allowed
    assert hook("pre", "td", ev(d), d)["permissionDecision"] == "deny"                     # other checkout path -> same repo-relative file
    assert hook("pre", "ta", {"tool_name": "Edit", "tool_input": {"file_path": f"{a}/ui/Other.js"}, "cwd": str(a)}, a) == {}

# ---- messages, threads, presence ----
def test_threads_roles_presence(hub):
    q = api("ta", "ask", to_agent="backend", question="ok?")                               # role routing
    assert q["to"] == "bob"
    assert api("ta", "ask", to_agent="nobody", question="?")["error"].startswith("unknown recipient")
    assert api("ta", "ask", to_agent="erin", question="staging?")["warning"].startswith("erin has never connected")
    mine = lambda: [m["id"] for m in api("tb", "inbox", peek=True) if m["body"] == "ok?"]; assert mine() == [q["id"]] and mine() == [q["id"]]       # peek doesn't consume
    r = api("tb", "ask", reply_to=q["id"], question="yes")                                  # reply goes to the asker
    assert r["to"] == "alice"
    assert [m["id"] for m in api("ta", "thread", message_id=q["id"])] == [q["id"], r["id"]]
    assert {x["id"]: x["answered"] for x in api("viewer", "status", method="GET")["questions"]}[q["id"]] is True   # original marked answered
    assert [m["id"] for m in api("ta", "inbox")] == [r["id"]] and api("ta", "inbox") == []  # consumed once
    assert next(p for p in api("ta", "feed")["agents"] if p["agent_id"] == "alice")["active"]

def test_offline_over_24h_warns(hub):
    import sqlite3; c = sqlite3.connect(hub / "h.db"); c.execute("INSERT OR REPLACE INTO agents VALUES('bob','backend',?)", (time.time() - 30 * 3600,)); c.commit()
    assert "offline 30h" in api("ta", "ask", to_agent="bob", question="hi")["warning"]

# ---- digest cursor ----
def test_digest_cursor(hub):
    d1 = api("td", "digest", since=0); api("tb", "post_intent", files=["cur.py"], summary="new thing")
    d2 = api("td", "digest", since=d1["now"]); assert [i["summary"] for i in d2["items"]] == ["new thing"]
    assert api("td", "digest", since=d2["now"])["items"] == []

# ---- builds ----
def test_build_detection(hub, tmp_path):
    post = lambda cmd, out: hook("post", "tb", {"tool_name": "Bash", "tool_input": {"command": cmd}, "tool_response": {"stdout": out, "stderr": ""}, "cwd": str(tmp_path)}, tmp_path)
    post("make test", "ok"); post("make test | tail", "FAILED: x"); post("make test | tail", "all good")
    subprocess.run([sys.executable, HOOK, "fail"], input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "pytest"}, "error": "Exit code 1\nboom", "cwd": str(tmp_path)}), text=True, env={**os.environ, "TEAMBUS_URL": URL, "TEAMBUS_TOKEN": "tb", "CLAUDE_PROJECT_DIR": str(tmp_path)})
    post("ls", "x")                                                                          # not a build command
    b = [(x["cmd"], x["ok"]) for x in api("viewer", "status", method="GET")["builds"]][::-1]
    assert b == [("make test", 1), ("make test | tail", 0), ("make test | tail", 1), ("pytest", 0)]

def test_hub_down_is_silent(tmp_path):
    r = subprocess.run([sys.executable, HOOK, "pre"], input=json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "a.py"}, "cwd": str(tmp_path)}), capture_output=True, text=True,
                       env={**os.environ, "TEAMBUS_URL": "http://127.0.0.1:9", "TEAMBUS_TOKEN": "x", "CLAUDE_PROJECT_DIR": str(tmp_path)})
    assert r.returncode == 0 and r.stdout == ""
    s = subprocess.run([sys.executable, HOOK, "start"], input="{}", capture_output=True, text=True, env={**os.environ, "TEAMBUS_URL": "http://127.0.0.1:9", "CLAUDE_PROJECT_DIR": str(tmp_path)})
    assert "offline" in json.loads(s.stdout)["hookSpecificOutput"]["additionalContext"]

def test_refuses_to_start_without_auth(tmp_path):
    r = subprocess.run([sys.executable, f"{HERE}/server.py"], env={k: v for k, v in os.environ.items() if not k.startswith("TEAMBUS")} | {"TEAMBUS_DB": str(tmp_path / "x.db"), "TEAMBUS_TOKENS": str(tmp_path / "none.json")}, capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and "refusing to start without auth" in r.stderr

# ---- CLI ----
CLI = f"{HERE}/teambus"
def cli(cwd, *a, check=True):
    r = subprocess.run([sys.executable, CLI, *a], cwd=cwd, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(cwd)})  # PATH without `claude`
    assert not check or r.returncode == 0, r.stdout + r.stderr; return r

def test_cli_init_merges_and_is_idempotent(hub, tmp_path):
    (tmp_path / ".claude").mkdir(); (tmp_path / ".git").mkdir()
    (tmp_path / ".claude/settings.local.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}, "env": {"MY": "1"}, "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}]}}))
    (tmp_path / "CLAUDE.md").write_text("# mine\n")
    cli(tmp_path, "init", "--hub", URL, "--agent-id", "alice", "--role", "frontend", "--token", "ta")
    s = json.loads((tmp_path / ".claude/settings.local.json").read_text())
    assert s["permissions"] == {"allow": ["Bash(ls:*)"]} and s["env"]["MY"] == "1" and s["env"]["TEAMBUS_TOKEN"] == "ta"
    assert [h["command"] for e in s["hooks"]["PreToolUse"] for h in e["hooks"]][0] == "echo mine" and len(s["hooks"]["PreToolUse"]) == 2
    assert set(s["hooks"]) == {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure"}
    assert (tmp_path / "CLAUDE.md").read_text().startswith("# mine\n") and "## TeamBus" in (tmp_path / "CLAUDE.md").read_text()
    cli(tmp_path, "init", "--hub", URL, "--agent-id", "alice", "--role", "frontend", "--token", "ta")                # second run changes nothing
    s2 = json.loads((tmp_path / ".claude/settings.local.json").read_text()); assert {k: len(v) for k, v in s2["hooks"].items()} == {k: len(v) for k, v in s["hooks"].items()}
    assert (tmp_path / "CLAUDE.md").read_text().count("## TeamBus") == 1 and (tmp_path / ".git/hooks/post-commit").read_text().count("teambus-post-commit") == 1

def test_cli_doctor_status_and_git_hook_resolve(hub, tmp_path):
    cli(tmp_path, "init", "--hub", URL, "--agent-id", "alice", "--role", "frontend", "--token", "ta")
    d = cli(tmp_path, "doctor", check=False); assert "[PASS] token accepted" in d.stdout and "[PASS] MCP endpoint" in d.stdout and "[PASS] hooks wired" in d.stdout, d.stdout
    assert "[WARN] claude CLI" in d.stdout and "[FAIL] .mcp.json registers" in d.stdout and d.returncode == 1                    # no claude on PATH here -> no .mcp.json -> doctor fails
    (tmp_path / ".claude/settings.local.json").write_text((tmp_path / ".claude/settings.local.json").read_text().replace('"ta"', '"bad"'))
    assert "[FAIL] token accepted" in cli(tmp_path, "doctor", check=False).stdout
    (tmp_path / ".claude/settings.local.json").write_text((tmp_path / ".claude/settings.local.json").read_text().replace('"bad"', '"ta"'))
    api("ta", "post_intent", files=["g/commit.py"], summary="working"); api("tb", "ask", to_agent="alice", question="status?")
    out = cli(tmp_path, "status").stdout; assert "you: alice" in out and "from bob" in out and "status?" in out
    subprocess.run("git init -q . && git -c user.name=a -c user.email=a@a commit -q --allow-empty -m e", shell=True, cwd=tmp_path, check=True, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    (tmp_path / "g").mkdir(); (tmp_path / "g/commit.py").write_text("x=1")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}; (tmp_path / ".git/hooks").mkdir(exist_ok=True); cli(tmp_path, "git-hook")
    subprocess.run("git add g && git -c user.name=a -c user.email=a@a commit -q -m c", shell=True, cwd=tmp_path, check=True, env=env)        # real commit -> real post-commit hook
    assert api("tb", "conflicts", files=["g/commit.py"])["conflicts"] == []                                                                 # intent was resolved by the commit

# ---- hard ask-first ----
def test_ask_first_is_enforced_with_three_strikes_and_hub_down_fallback(hub, tmp_path):
    api("tb", "post_intent", files=["strict/f.py"], summary="bob editing")
    ev = {"tool_name": "Edit", "tool_input": {"file_path": f"{tmp_path}/strict/f.py", "new_string": "x"}, "cwd": str(tmp_path)}
    out = [hook("pre", "td", ev, tmp_path).get("permissionDecision") for _ in range(4)]
    assert out == ["deny", "deny", "deny", None]                                           # 3 blocks, then it lets go (a broken MCP must not lock editing forever)
    p2 = tmp_path / "other"; p2.mkdir(); ev["cwd"] = str(p2); ev["tool_input"]["file_path"] = f"{p2}/strict/f.py"
    assert hook("pre", "td", ev, p2)["permissionDecision"] == "deny"
    r = subprocess.run([sys.executable, HOOK, "pre"], input=json.dumps(ev), capture_output=True, text=True, env={**os.environ, "TEAMBUS_URL": "http://127.0.0.1:9", "TEAMBUS_TOKEN": "td", "CLAUDE_PROJECT_DIR": str(p2)})
    assert r.stdout == ""                                                                  # hub unreachable: never blocks
    assert api("td", "asked", to_agent="bob", since=0)["asked"] is False
    api("td", "ask", to_agent="bob", question="hello"); assert api("td", "asked", to_agent="bob", since=0)["asked"] is True and api("ta", "asked", to_agent="bob", since=time.time())["asked"] is False

# ---- contract lifecycle ----
def test_contract_resolves_when_consumers_commit(hub):
    c = api("tb", "contract", name="GET /life", spec="renamed", files=["life/a.js", "life/b.js"], source_files=["life/api.py"])["id"]
    kinds = lambda tok, f: [x["contract"] for x in api(tok, "conflicts", files=[f])["conflicts"] if x["kind"] == "contract_changed"]
    assert kinds("te", "life/a.js") == ["GET /life"] == kinds("te", "life/b.js")
    api("te", "resolve", files=["life/a.js"]); assert kinds("ta", "life/a.js")                       # finishing work (no commit) does NOT count as adapting
    api("te", "resolve", files=["life/a.js"], committed=True)                                              # erin commits a consumer file -> that file is done
    assert kinds("ta", "life/a.js") == [] and kinds("ta", "life/b.js") == ["GET /life"]
    assert any(x["id"] == c for x in api("viewer", "status", method="GET")["contracts"])   # still open in the web view
    api("te", "resolve", files=["life/b.js"], committed=True)                                              # all consumers done -> resolved
    assert not any(x["id"] == c for x in api("viewer", "status", method="GET")["contracts"]) and kinds("ta", "life/b.js") == []
    c2 = api("tb", "contract", name="GET /manual", spec="x", files=["life/m.js"])["id"]
    assert api("ta", "resolve_contract", contract_id=c2)["resolved"] == 0                  # only the publisher can resolve
    assert api("tb", "resolve_contract", contract_id=c2)["resolved"] == 1 and kinds("ta", "life/m.js") == []
    assert api("tb", "contract", name="own", spec="y", files=["life/o.js"]) and api("tb", "resolve", files=["life/o.js"], committed=True)["resolved"] >= 0   # the publisher committing his own file does not resolve it
    assert kinds("ta", "life/o.js") == ["own"]

# ---- AST parsing ----
def test_python_ast_symbols_and_imports(tmp_path):
    sys.path.insert(0, f"{HERE}/hooks"); import hook as H
    src = 'def real(): pass\n"""docstring mentioning def fake(): and class Nope"""\n# def commented(): pass\nclass K:\n    async def m(self): pass\n'
    assert H.symbols({"content": src}, py=True) == ["K", "m", "real"]
    assert "fake" in H.symbols({"content": src}, py=False)                                 # the regex fallback is fooled by the docstring; the AST is not
    assert H.symbols({"new_string": "    def inner(self):\n        pass"}, py=True) == ["inner"]   # a dedent-able fragment still parses
    for f in ("lib/a.py", "lib/b.py", "lib/c.py"): (tmp_path / f).parent.mkdir(exist_ok=True); (tmp_path / f).write_text("")
    code = "# import c\nfrom . import a\nimport lib.b\ns = 'from lib import c'\n"
    assert H.imports(str(tmp_path), "lib/main.py", code) == ["lib/a.py", "lib/b.py"]       # commented / quoted imports ignored


def test_doctor_explains_tls_and_token_failures_differently():
    import importlib.machinery, importlib.util, ssl, urllib.error
    ld = importlib.machinery.SourceFileLoader("teambus_cli", str(Path(__file__).parent / "teambus")); sp = importlib.util.spec_from_loader("teambus_cli", ld); cli = importlib.util.module_from_spec(sp); ld.exec_module(cli)
    assert "TLS problem, not the token" in cli.why(urllib.error.URLError(ssl.SSLCertVerificationError("self signed certificate")))
    assert "token rejected" in cli.why(urllib.error.HTTPError("u", 401, "Unauthorized", {}, None))
    assert "could not reach the hub" in cli.why(urllib.error.URLError(ConnectionRefusedError("refused")), "could not reach the hub")


def test_inbox_tool_result_always_has_content(hub):
    async def go(tok):
        async with streamablehttp_client(f"{URL}/mcp", headers={"Authorization": "Bearer " + tok}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize(); res = await s.call_tool("check_inbox", {"peek": True}); return res.content[0].text if res.content else ""
    api("td", "ask", to_agent="erin", question="inbox shape check")
    out = json.loads(asyncio.run(go("te")))          # erin has at least this unread message
    assert out["unread"] == len(out["messages"]) >= 1
    api("te", "inbox"); out = json.loads(asyncio.run(go("te")))   # reading marks them read: now empty, yet the tool result is still not blank
    assert out == {"unread": 0, "messages": []}


def _cli():
    import importlib.machinery, importlib.util
    ld = importlib.machinery.SourceFileLoader("teambus_cli2", str(Path(HERE) / "teambus")); sp = importlib.util.spec_from_loader("teambus_cli2", ld); m = importlib.util.module_from_spec(sp); ld.exec_module(m); return m


def test_installed_hooks_do_nothing_when_teambus_is_gone_and_survive_paths_with_spaces(tmp_path):
    cli = _cli(); cli.HOOK = Path("/nonexistent/teambus/hooks/hook.py")
    gone = subprocess.run(["sh", "-c", cli.hook_cmd("pre")], input='{"tool_name":"Edit"}', capture_output=True, text=True)
    assert gone.returncode == 0 and gone.stdout == ""                       # a removed install must never block a session (a bare `python3 missing.py` exits 2)
    d = tmp_path / "my tools" / "hooks"; d.mkdir(parents=True); (d / "hook.py").write_text(Path(HOOK).read_text()); cli.HOOK = d / "hook.py"
    ok = subprocess.run(["sh", "-c", cli.hook_cmd("start")], input="{}", capture_output=True, text=True, env={**os.environ, "TEAMBUS_URL": "http://127.0.0.1:1", "TEAMBUS_TOKEN": "x"})
    assert ok.returncode == 0                                                # real script, space in the path, hub unreachable: still silent and fine


def test_init_writes_guarded_hooks_and_is_still_idempotent(tmp_path):
    proj = tmp_path / "p"; proj.mkdir(); env = {**os.environ, "HOME": str(tmp_path / "home")}; (tmp_path / "home").mkdir()
    for _ in range(2): subprocess.run([sys.executable, f"{HERE}/teambus", "init", "--hub", URL, "--agent-id", "alice", "--role", "frontend", "--token", "ta", "--dir", str(proj)], env=env, capture_output=True, text=True, check=True)
    s = json.loads((proj / ".claude" / "settings.local.json").read_text())
    cmds = [h["command"] for ev in s["hooks"].values() for e in ev for h in e["hooks"]]
    assert len(cmds) == 5 and all(c.startswith("[ -f ") and c.endswith("|| true") and "hooks/hook.py" in c for c in cmds)


def test_install_script_installs_links_and_updates(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    for f in ("teambus", "teambus-join", "teambus-hub", "install.sh", "requirements.txt"): (src / f).write_bytes((Path(HERE) / f).read_bytes()); os.chmod(src / f, 0o755)
    sh = lambda *a, **k: subprocess.run(a, cwd=src, capture_output=True, text=True, check=True, **k)
    sh("git", "init", "-q", "-b", "main"); sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"); sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one")
    home, bin_ = tmp_path / "home", tmp_path / "bin"; home.mkdir()
    env = {**os.environ, "HOME": str(home), "TEAMBUS_REPO": f"file://{src}", "TEAMBUS_BIN": str(bin_), "PATH": os.environ["PATH"]}
    r = subprocess.run(["sh", str(src / "install.sh")], env=env, capture_output=True, text=True); assert r.returncode == 0, r.stderr + r.stdout
    assert "Installing into" in r.stdout and "is not on your PATH" in r.stdout                  # bin dir was not on PATH: it says how to fix it
    for c in ("teambus", "teambus-join", "teambus-hub"): assert (bin_ / c).is_symlink() and os.access(bin_ / c, os.X_OK)
    run = lambda *a: subprocess.run(a, env={**env, "PATH": f"{bin_}:{os.environ['PATH']}"}, capture_output=True, text=True)
    assert run("teambus", "--help").returncode == 0 and "init" in run("teambus", "--help").stdout       # works through the symlink
    j = run("teambus-join"); assert j.returncode == 1 and "usage: teambus-join" in j.stderr             # finds its own folder through the symlink (python3/claude checks pass first)
    (src / "NEWFILE").write_text("x"); sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"); sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "two")
    r2 = subprocess.run(["sh", str(src / "install.sh")], env=env, capture_output=True, text=True); assert r2.returncode == 0 and "Updating" in r2.stdout and (home / ".teambus" / "NEWFILE").exists()   # re-running updates
    bad = subprocess.run(["sh", str(src / "install.sh")], env={**env, "TEAMBUS_HOME": str(tmp_path)}, capture_output=True, text=True); assert bad.returncode == 1 and "not a TeamBus checkout" in bad.stderr   # never clobbers a random folder
