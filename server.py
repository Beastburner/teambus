"""TeamBus hub: MCP tools + REST mirror (for hooks/CLI) + read-only web view over one SQLite DB.
Auth: every request needs a bearer token. TEAMBUS_TOKENS (json file) maps token -> agent (identity is derived from it, never from arguments).
TEAMBUS_TOKEN is a shared read-only token (web view / status). TEAMBUS_NOAUTH=1 disables auth (local dev only)."""
import json, os, sqlite3, sys, time
from urllib.parse import parse_qs
import uvicorn
from mcp.server.fastmcp import FastMCP, Context
from starlette.responses import HTMLResponse, JSONResponse

E = os.environ.get
DB, TOKENS, VIEWER, NOAUTH = E("TEAMBUS_DB", "teambus.db"), E("TEAMBUS_TOKENS", "tokens.json"), E("TEAMBUS_TOKEN"), E("TEAMBUS_NOAUTH") == "1"
INTENT_TTL, CONTRACT_TTL = float(E("TEAMBUS_INTENT_TTL", 4 * 3600)), float(E("TEAMBUS_CONTRACT_TTL", 3 * 86400))
ACTIVE, OFFLINE = float(E("TEAMBUS_ACTIVE_SECS", 900)), float(E("TEAMBUS_OFFLINE_SECS", 86400))
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
mcp = FastMCP("teambus", host=E("HOST", "0.0.0.0"), port=int(E("PORT", 8765)))

SCHEMA = {
    "intents": "id INTEGER PRIMARY KEY, ts REAL, agent_id TEXT, role TEXT, files TEXT, summary TEXT, symbols TEXT DEFAULT '[]', status TEXT DEFAULT 'open', resolved_ts REAL",
    "contracts": "id INTEGER PRIMARY KEY, ts REAL, agent_id TEXT, name TEXT, spec TEXT, files TEXT, source_files TEXT DEFAULT '[]', status TEXT DEFAULT 'open', done_files TEXT DEFAULT '[]', resolved_ts REAL",
    "messages": "id INTEGER PRIMARY KEY, ts REAL, from_agent TEXT, to_agent TEXT, body TEXT, reply_to INTEGER, read INTEGER DEFAULT 0, answered INTEGER DEFAULT 0",
    "builds": "id INTEGER PRIMARY KEY, ts REAL, agent_id TEXT, ok INTEGER, log_tail TEXT, cmd TEXT DEFAULT ''",
    "agents": "agent_id TEXT PRIMARY KEY, role TEXT, last_seen REAL"}

def db():
    c = sqlite3.connect(DB, timeout=10); c.row_factory = sqlite3.Row; return c

with db() as c:  # create + add any column an older DB lacks
    for t, cols in SCHEMA.items():
        c.execute(f"CREATE TABLE IF NOT EXISTS {t}({cols})")
        have = {r["name"] for r in c.execute(f"PRAGMA table_info({t})")}
        for col in cols.split(", "):
            if col.split()[0] not in have: c.execute(f"ALTER TABLE {t} ADD COLUMN {col.replace(' PRIMARY KEY', '')}")

def rows(q, *a):
    with db() as c: return [dict(r) for r in c.execute(q, a)]

def run(q, *a):
    with db() as c: return c.execute(q, a).lastrowid

# ---- identity ----
_tok = [0.0, {}]
def tokens():
    try:
        if os.path.getmtime(TOKENS) != _tok[0]:
            _tok[1] = {t: ({"agent_id": v} if isinstance(v, str) else v) for t, v in json.load(open(TOKENS)).items()}; _tok[0] = os.path.getmtime(TOKENS)
    except OSError: _tok[1] = {}
    return _tok[1]

def ident(tok):
    if tok and tok in tokens(): return tokens()[tok]
    if tok and VIEWER and tok == VIEWER: return {"viewer": True}
    return {"agent_id": None} if NOAUTH else None

def me(ctx, claimed=""):
    """The caller's agent_id: from the authenticated token when there is one; the claimed value only in NOAUTH dev mode."""
    req = getattr(getattr(ctx, "request_context", None), "request", ctx)
    w = (getattr(req, "scope", None) or {}).get("teambus") or {}
    return w.get("agent_id") or (claimed if NOAUTH else "")

def known():
    return {v["agent_id"]: v.get("role", "") for v in tokens().values() if v.get("agent_id")} | {r["agent_id"]: r["role"] for r in rows("SELECT agent_id, role FROM agents")}

def presence():
    seen = {r["agent_id"]: r["last_seen"] for r in rows("SELECT agent_id, last_seen FROM agents")}; now = time.time()
    return [{"agent_id": a, "role": r, "last_seen_ago": (now - seen[a]) if a in seen else None, "active": a in seen and now - seen[a] < ACTIVE} for a, r in sorted(known().items())]

class Auth:
    """ASGI middleware: 401 without a valid token; shared viewer token may only GET / and /api/status; records presence."""
    def __init__(s, app): s.app = app
    async def __call__(s, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/healthz": return await s.app(scope, receive, send)
        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        who = ident(auth[7:].strip() if auth.lower().startswith("bearer ") else parse_qs(scope["query_string"].decode()).get("token", [""])[0])
        if not who: return await JSONResponse({"error": "unauthorized"}, 401)(scope, receive, send)
        if who.get("viewer") and not (scope["method"] == "GET" and scope["path"] in ("/", "/api/status")):
            return await JSONResponse({"error": "viewer token is read-only"}, 403)(scope, receive, send)
        scope["teambus"] = who
        if who.get("agent_id"): run("INSERT INTO agents(agent_id,role,last_seen) VALUES(?,?,?) ON CONFLICT(agent_id) DO UPDATE SET last_seen=excluded.last_seen, role=CASE WHEN excluded.role!='' THEN excluded.role ELSE role END", who["agent_id"], who.get("role", ""), time.time())
        await s.app(scope, receive, send)

# ---- core (each is both an MCP tool and POST /api/<name>; `agent_id`/`ctx` are optional: identity comes from the token) ----
def whoami(ctx: Context | None = None, agent_id: str = ""):
    """Which agent the hub thinks you are (derived from your token)."""
    a = me(ctx, agent_id); return {"agent_id": a, "role": known().get(a, "")}

def post_intent(files: list[str], summary: str, symbols: list[str] = [], role: str = "", agent_id: str = "", ctx: Context | None = None):
    """Announce you are editing `files` (repo-relative). Repeats for the same files refresh the existing intent."""
    a = me(ctx, agent_id); role = role or known().get(a, ""); fj = json.dumps(sorted(files))
    old = rows("SELECT id, symbols FROM intents WHERE agent_id=? AND status='open' AND files=?", a, fj)
    if old:
        run("UPDATE intents SET ts=?, summary=?, symbols=? WHERE id=?", time.time(), summary, json.dumps(sorted(set(json.loads(old[0]["symbols"])) | set(symbols))), old[0]["id"]); return {"id": old[0]["id"]}
    return {"id": run("INSERT INTO intents(ts,agent_id,role,files,summary,symbols) VALUES(?,?,?,?,?,?)", time.time(), a, role, fj, summary, json.dumps(sorted(set(symbols))))}

def resolve_intent(intent_id: int | None = None, files: list[str] = [], committed: bool = False, agent_id: str = "", ctx: Context | None = None):
    """Mark your open intents done. By id, by files, or all of yours when neither is given. `committed=true` (sent by the git post-commit hook) also counts those files as adapted for any open contract that lists them."""
    a = me(ctx, agent_id); n = 0
    if committed: _ack_contracts(a, files)
    for r in rows("SELECT id, files FROM intents WHERE agent_id=? AND status='open'", a):
        if (intent_id is None and not files) or r["id"] == intent_id or set(files) & set(json.loads(r["files"])):
            run("UPDATE intents SET status='resolved', resolved_ts=? WHERE id=?", time.time(), r["id"]); n += 1
    return {"resolved": n}

def _ack_contracts(a, files):
    """A teammate committing a consumer file of an open contract counts as having adapted to it; the contract resolves once every consumer file is done."""
    for c in rows("SELECT * FROM contracts WHERE status='open' AND agent_id!=?", a):
        done = sorted(set(json.loads(c["done_files"])) | (set(files) & set(json.loads(c["files"]))))
        if done != json.loads(c["done_files"]): run("UPDATE contracts SET done_files=?, status=?, resolved_ts=? WHERE id=?", json.dumps(done), "resolved" if set(done) >= set(json.loads(c["files"])) else "open", time.time() if set(done) >= set(json.loads(c["files"])) else None, c["id"])

def resolve_contract(contract_id: int, agent_id: str = "", ctx: Context | None = None):
    """Mark a contract you published as fully rolled out (consumers adapted); it stops warning teammates."""
    n = len(rows("SELECT id FROM contracts WHERE id=? AND agent_id=? AND status='open'", contract_id, me(ctx, agent_id)))
    if n: run("UPDATE contracts SET status='resolved', resolved_ts=? WHERE id=?", time.time(), contract_id)
    return {"resolved": n}

def asked(to_agent: str, since: float = 0.0, ctx: Context | None = None, agent_id: str = ""):
    """Has the caller sent `to_agent` a message since `since`? (used by the hook to require an actual question before a blocked edit is retried)"""
    return {"asked": bool(rows("SELECT id FROM messages WHERE from_agent=? AND to_agent=? AND ts>?", me(ctx, agent_id), to_agent, since))}

def _active_intents(a):
    return rows("SELECT * FROM intents WHERE status='open' AND ts>? AND agent_id!=?", time.time() - INTENT_TTL, a)

def team_feed(since: float = 0.0, agent_id: str = "", ctx: Context | None = None):
    """Who is active, plus teammates' intents/contracts/builds since `since` (unix time)."""
    a = me(ctx, agent_id); f = lambda t, r: [{**x, "type": t} for x in r]
    return {"now": time.time(), "agents": presence(), "items": sorted(
        f("intent", rows("SELECT * FROM intents WHERE ts>? AND agent_id!=? AND (status='open' OR resolved_ts>?)", since, a, since))
        + f("contract", rows("SELECT * FROM contracts WHERE ts>? AND agent_id!=?", since, a))
        + f("build", rows("SELECT * FROM builds WHERE ts>? AND agent_id!=?", since, a)), key=lambda x: x["ts"])}

def ask_teammate_agent(to_agent: str = "", question: str = "", reply_to: int | None = None, from_agent: str = "", ctx: Context | None = None):
    """Send a teammate's agent a message (async). `to_agent` is an agent_id (or a role held by one agent). To answer a message, set reply_to=<its id>; the asker then sees it as answered."""
    a = me(ctx, from_agent); parent = (rows("SELECT * FROM messages WHERE id=?", reply_to) or [None])[0] if reply_to else None
    if reply_to and not parent: return {"error": f"no message #{reply_to}"}
    to = to_agent or (parent and parent["from_agent"]) or ""; k = known()
    if to not in k:
        byrole = [x for x, r in k.items() if r == to and x != a]
        if len(byrole) != 1: return {"error": f"unknown recipient {to!r}; use one of the agent_ids: {sorted(k)}"}
        to = byrole[0]
    mid = run("INSERT INTO messages(ts,from_agent,to_agent,body,reply_to) VALUES(?,?,?,?,?)", time.time(), a, to, question, reply_to)
    if parent and parent["to_agent"] == a: run("UPDATE messages SET answered=1 WHERE id=?", reply_to)
    seen = (rows("SELECT last_seen FROM agents WHERE agent_id=?", to) or [{"last_seen": None}])[0]["last_seen"]; out = {"id": mid, "to": to}
    if seen is None or time.time() - seen > OFFLINE: out["warning"] = f"{to} has " + ("never connected" if seen is None else f"been offline {int((time.time() - seen) / 3600)}h") + "; the answer may take a while. Continue other work and check_inbox later."
    return out

def check_inbox(peek: bool = False, agent_id: str = "", ctx: Context | None = None):
    """Your unread messages (questions and replies). Marks them read unless peek=true."""
    a = me(ctx, agent_id); msgs = rows("SELECT * FROM messages WHERE to_agent=? AND read=0 ORDER BY id", a)
    if msgs and not peek: run(f"UPDATE messages SET read=1 WHERE id IN ({','.join(str(int(m['id'])) for m in msgs)})")
    return msgs

def thread(message_id: int, ctx: Context | None = None):
    """The whole conversation containing message #message_id, oldest first."""
    root = message_id
    while (p := rows("SELECT reply_to FROM messages WHERE id=?", root)) and p[0]["reply_to"]: root = p[0]["reply_to"]
    todo, out = [root], []
    while todo: i = todo.pop(); out += rows("SELECT * FROM messages WHERE id=?", i); todo += [r["id"] for r in rows("SELECT id FROM messages WHERE reply_to=?", i)]
    return sorted(out, key=lambda m: m["id"])

def publish_contract(name: str, spec: str, files: list[str], source_files: list[str] = [], agent_id: str = "", ctx: Context | None = None):
    """Announce a breaking/shared-interface change. `files` = repo-relative files that consume it; `source_files` = files you changed that define it."""
    return {"id": run("INSERT INTO contracts(ts,agent_id,name,spec,files,source_files) VALUES(?,?,?,?,?,?)", time.time(), me(ctx, agent_id), name, spec, json.dumps(files), json.dumps(source_files))}

def report_build(ok: bool, log_tail: str, cmd: str = "", agent_id: str = "", ctx: Context | None = None):
    """Record a build/test result."""
    return {"id": run("INSERT INTO builds(ts,agent_id,ok,log_tail,cmd) VALUES(?,?,?,?,?)", time.time(), me(ctx, agent_id), int(ok), log_tail[-2000:], cmd[:300])}

def check_conflicts(files: list[str], symbols: list[str] = [], imports: list[str] = [], agent_id: str = "", ctx: Context | None = None):
    """Conflicts for editing `files`: same file, shared contract, same directory + overlapping function names, or `imports` a teammate is changing."""
    a, mine, out, syms = me(ctx, agent_id), set(files), [], set(symbols)
    dirs = {os.path.dirname(f) for f in mine}
    for r in _active_intents(a):
        rf = set(json.loads(r["files"]))
        if ov := mine & rf: out.append({"kind": "same_file", "with": r["agent_id"], "files": sorted(ov), "summary": r["summary"]})
        elif (sh := syms & set(json.loads(r["symbols"]))) and dirs & {os.path.dirname(f) for f in rf}: out.append({"kind": "same_dir_symbols", "with": r["agent_id"], "files": sorted(rf), "symbols": sorted(sh), "summary": r["summary"]})
        if ov := set(imports) & rf: out.append({"kind": "imports_changed", "with": r["agent_id"], "files": sorted(ov), "summary": r["summary"]})
    for r in rows("SELECT * FROM contracts WHERE ts>? AND agent_id!=? AND status='open'", time.time() - CONTRACT_TTL, a):
        if ov := mine & (set(json.loads(r["files"])) - set(json.loads(r["done_files"]))): out.append({"kind": "contract_changed", "with": r["agent_id"], "contract": r["name"], "files": sorted(ov), "spec": r["spec"]})
        if ov := set(imports) & set(json.loads(r["source_files"])): out.append({"kind": "imports_changed", "with": r["agent_id"], "files": sorted(ov), "summary": f"contract '{r['name']}': {r['spec']}"})
    return {"conflicts": out}

def digest(since: float = 0.0, agent_id: str = "", ctx: Context | None = None):
    """What's new for you: unread messages (marked read) and teammates' activity since `since`."""
    f = team_feed(since, agent_id, ctx); return {"me": me(ctx, agent_id), "now": f["now"], "inbox": check_inbox(False, agent_id, ctx), "items": f["items"], "agents": f["agents"]}

def status(ctx=None):
    """Everything the web view shows (read-only)."""
    now = time.time(); q = rows("SELECT * FROM messages WHERE reply_to IS NULL ORDER BY id DESC LIMIT 30")
    return {"now": now, "agents": presence(), "intents": rows("SELECT * FROM intents WHERE status='open' AND ts>? ORDER BY ts DESC LIMIT 50", now - INTENT_TTL),
            "contracts": rows("SELECT * FROM contracts WHERE ts>? AND status='open' ORDER BY ts DESC LIMIT 20", now - CONTRACT_TTL), "builds": rows("SELECT * FROM builds ORDER BY ts DESC LIMIT 20"),
            "questions": [{**m, "answered": bool(m["answered"])} for m in q], "feed": rows("SELECT 'intent' AS type, ts, agent_id, summary AS text, status FROM intents UNION ALL SELECT 'contract', ts, agent_id, name || ': ' || spec, '' FROM contracts UNION ALL SELECT 'message', ts, from_agent, '-> ' || to_agent || ': ' || body, CASE answered WHEN 1 THEN 'answered' ELSE '' END FROM messages ORDER BY ts DESC LIMIT 40")}

# ---- MCP tools (docstrings are what agents read) ----
def check_inbox_tool(peek: bool = False, agent_id: str = "", ctx: Context | None = None):
    """Your unread messages (questions and replies). Marks them read unless peek=true. Returns {"unread": N, "messages": [...]}."""
    m = check_inbox(peek, agent_id, ctx); return {"unread": len(m), "messages": m}   # an empty list would render as "no output" in the agent's tool result
mcp.tool(name="check_inbox")(check_inbox_tool)

for fn in (whoami, post_intent, resolve_intent, resolve_contract, asked, team_feed, ask_teammate_agent, thread, publish_contract, report_build, check_conflicts): mcp.tool()(fn)

# ---- REST mirror so hooks/CLI (plain scripts) call the same functions, plus the web view ----
API = dict(whoami=whoami, post_intent=post_intent, resolve=resolve_intent, resolve_contract=resolve_contract, asked=asked, feed=team_feed, ask=ask_teammate_agent, inbox=check_inbox, thread=thread,
           contract=publish_contract, build=report_build, conflicts=check_conflicts, digest=digest)

def _route(name):
    async def h(request):
        try: return JSONResponse(API[name](**(await request.json()), ctx=request))
        except TypeError as e: return JSONResponse({"error": str(e)}, 400)
    return h

for n in API: mcp.custom_route(f"/api/{n}", methods=["POST"])(_route(n))

@mcp.custom_route("/api/status", methods=["GET"])
async def _status(request): return JSONResponse(status())

@mcp.custom_route("/healthz", methods=["GET"])
async def _health(request): return JSONResponse({"ok": True})

@mcp.custom_route("/", methods=["GET"])
async def _index(request): return HTMLResponse(open(WEB).read()) if os.path.exists(WEB) else JSONResponse({"error": "no web view installed"}, 404)

app = mcp.streamable_http_app(); app.add_middleware(Auth)

def add_agent(agent_id, role):
    """`python server.py add-agent ID ROLE`: append a new token to the tokens file and print it (the running hub picks it up on its next request)."""
    import secrets; t = secrets.token_urlsafe(24); d = json.load(open(TOKENS)) if os.path.exists(TOKENS) else {}
    d[t] = {"agent_id": agent_id, "role": role}; json.dump(d, open(TOKENS, "w"), indent=1); os.chmod(TOKENS, 0o600); print(t)

if __name__ == "__main__":
    if sys.argv[1:2] == ["add-agent"]: sys.exit(add_agent(*sys.argv[2:4]))
    if not (NOAUTH or VIEWER or os.path.exists(TOKENS)): sys.exit(f"refusing to start without auth: create {TOKENS} (token -> agent map) and/or set TEAMBUS_TOKEN, or TEAMBUS_NOAUTH=1 for local dev")
    uvicorn.run(app, host=mcp.settings.host, port=mcp.settings.port, log_level="warning")
