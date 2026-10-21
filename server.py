"""TeamBus hub: MCP tools + REST mirror (for hooks/CLI) + read-only web view over one SQLite DB.
Auth: every request needs a bearer token. TEAMBUS_TOKENS (json file) maps token -> agent (identity is derived from it, never from arguments).
TEAMBUS_TOKEN is a shared read-only token (web view / status). TEAMBUS_NOAUTH=1 disables auth (local dev only)."""
import asyncio, difflib, hashlib, json, os, re, secrets, sqlite3, sys, threading, time
from urllib.parse import parse_qs
import uvicorn
from mcp.server.fastmcp import FastMCP, Context
from starlette.responses import HTMLResponse, JSONResponse

E = os.environ.get
DB, TOKENS, VIEWER, NOAUTH = E("TEAMBUS_DB", "teambus.db"), E("TEAMBUS_TOKENS", "tokens.json"), E("TEAMBUS_TOKEN"), E("TEAMBUS_NOAUTH") == "1"
VIEWER_SHA256 = {h.strip().lower() for h in E("TEAMBUS_TOKEN_SHA256", "").split(",") if h.strip()}   # read-only tokens known only by their SHA-256 (a resumed sandbox)
INTENT_TTL, CONTRACT_TTL = float(E("TEAMBUS_INTENT_TTL", 4 * 3600)), float(E("TEAMBUS_CONTRACT_TTL", 3 * 86400))
ACTIVE, OFFLINE = float(E("TEAMBUS_ACTIVE_SECS", 900)), float(E("TEAMBUS_OFFLINE_SECS", 86400))
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
mcp = FastMCP("teambus", host=E("HOST", "0.0.0.0"), port=int(E("PORT", 8765)))

SCHEMA = {
    "intents": "id INTEGER PRIMARY KEY, ts REAL, agent_id TEXT, role TEXT, files TEXT, summary TEXT, symbols TEXT DEFAULT '[]', status TEXT DEFAULT 'open', resolved_ts REAL",
    "contracts": "id INTEGER PRIMARY KEY, ts REAL, agent_id TEXT, name TEXT, spec TEXT, files TEXT, source_files TEXT DEFAULT '[]', status TEXT DEFAULT 'open', done_files TEXT DEFAULT '[]', resolved_ts REAL",
    "messages": "id INTEGER PRIMARY KEY, ts REAL, from_agent TEXT, to_agent TEXT, body TEXT, reply_to INTEGER, read INTEGER DEFAULT 0, answered INTEGER DEFAULT 0, read_ts REAL",
    "builds": "id INTEGER PRIMARY KEY, ts REAL, agent_id TEXT, ok INTEGER, log_tail TEXT, cmd TEXT DEFAULT '', fixed_by INTEGER, fixed_ts REAL",
    "spawns": "id INTEGER PRIMARY KEY, ts REAL, requested_by TEXT, agent_id TEXT, role TEXT, brief TEXT, status TEXT DEFAULT 'pending', note TEXT DEFAULT ''",
    "agents": "agent_id TEXT PRIMARY KEY, role TEXT, last_seen REAL, last_mcp REAL, last_hook REAL, last_event TEXT, last_event_ts REAL"}

# Anything that looks like a credential is removed before it is stored or shown to other agents: build commands and logs, edit summaries and messages all travel to every
# teammate (and sit in the hub's database). Teammates share secrets out of band, never through TeamBus.
_VAL = r"""(?:(?=[^\s'"&,;]*\d)[^\s'"&,;]{6,}|[^\s'"&,;]{20,})"""            # a value with a digit in it, or a long one: real secrets, not prose like "token: refreshed"
SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), "[private key removed]"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+:)([^\s@/]+)(@)"), r"\1[removed]\3"),                      # user:password@host in a connection string
    (re.compile(r"(?i)\b(bearer|basic)\s+(?!\[)[A-Za-z0-9._~+/=-]{12,}"), r"\1 [removed]"),
    (re.compile(r"(?i)(--?(?:token|password|passwd|api-?key|secret)(?:=|\s+))(?!\[)(\S+)"), r"\1[removed]"),
    (re.compile(r"(?i)\b([A-Za-z0-9_.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|private[_-]?key|credential|authorization)[A-Za-z0-9_.-]*)(\s*[=:]\s*)(['\"]?)(?!\[)" + _VAL), r"\1\2\3[removed]"),
    (re.compile(r"\b0x[a-fA-F0-9]{64}\b"), "[64-hex value removed]"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{20,}\b"), "[key removed]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[github token removed]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[aws key removed]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), "[google key removed]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "[slack token removed]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "[jwt removed]"),
]

def scrub(text):
    """(text with credential-looking values removed, how many were removed)."""
    out, n = text or "", 0
    for rx, sub in SECRET_PATTERNS: out, k = rx.subn(sub, out); n += k
    return out, n

def db():
    c = sqlite3.connect(DB, timeout=10); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA synchronous=NORMAL")      # readers no longer wait for writers: many agents poll while a few write
    return c

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

def sha256(t): return hashlib.sha256(t.encode()).hexdigest()

def ident(tok):
    """token -> identity. tokens.json is keyed by the raw token, or by "sha256:<hex digest>" when the token itself is not stored (a stolen file is then useless)."""
    if tok:
        t = tokens(); v = t.get(tok) or t.get("sha256:" + sha256(tok))
        if v: return v
        if VIEWER and tok == VIEWER or VIEWER_SHA256 and sha256(tok) in VIEWER_SHA256: return {"viewer": True}
    return {"agent_id": None} if NOAUTH else None

def me(ctx, claimed=""):
    """The caller's agent_id: from the authenticated token when there is one; the claimed value only in NOAUTH dev mode."""
    req = getattr(getattr(ctx, "request_context", None), "request", ctx)
    w = (getattr(req, "scope", None) or {}).get("teambus") or {}
    return w.get("agent_id") or (claimed if NOAUTH else "")

def known():
    return {v["agent_id"]: v.get("role", "") for v in tokens().values() if v.get("agent_id")} | {r["agent_id"]: r["role"] for r in rows("SELECT agent_id, role FROM agents")}

def owner_of(a):
    """The person an agent works for: `owner` in tokens.json, else the part of the agent id before the first dash (alice-frontend -> alice)."""
    t = next((v for v in tokens().values() if v.get("agent_id") == a), {}); return t.get("owner") or (a.split("-")[0] if "-" in a else "")

MAX_GROUP = 12
MAX_AGENTS_HUB, MAX_CREATED = int(E("TEAMBUS_MAX_AGENTS", 50)), int(E("TEAMBUS_MAX_CREATED", 5))     # total agents on this hub; most one agent may create
AGENT_ID_RE, AGENT_ROLE_RE = re.compile(r"[a-z][a-z0-9-]{1,30}"), re.compile(r"[A-Za-z0-9_-]{1,24}")   # strict: ids and roles end up in shell commands and in other agents' context
_tokfile = threading.Lock()

def delivery_note(p):
    """What the sender should expect: an agent only sees mail when its session runs (a prompt, a tool call or the end of a turn). An idle one waits for its user."""
    if not p or p.get("last_seen_ago") is None: return "never connected" + ("" if p.get("hook_ago") is not None else "; their hooks have never reported, so they only see this when they call check_inbox")
    mins = lambda t: f"{int(t)} s" if t < 90 else f"{int(t / 60)} min"; state = p["state"]
    note = (f"{state}, seen {mins(p['last_seen_ago'])} ago: " + ("they get it on their next tool call or prompt" if state == "working" else
            "it stays unread until their session next runs (their user types something, or the agent polls with wait_for_messages); nothing can wake an idle session"))
    return note + ("" if p.get("hook_ago") is not None else "; their hooks have never reported, so they only see this when they call check_inbox")

def presence():
    """Per agent: when it was last seen, from which side (hooks, MCP tools), what it was last doing, and a plain state."""
    rs = {r["agent_id"]: r for r in rows("SELECT * FROM agents")}; now = time.time(); out = []
    for a, role in sorted(known().items()):
        r = rs.get(a); seen = r["last_seen"] if r else None; ago = lambda t: (now - t) if t else None
        ev, ev_ago = (r["last_event"], ago(r["last_event_ts"])) if r else (None, None)
        state = ("never connected" if seen is None else "offline" if now - seen > OFFLINE else "working" if ev in ("pre", "post", "prompt", "tool", "fail", "start") and ev_ago is not None and ev_ago < 120
                 else "idle" if ev == "stop" and ev_ago is not None and ev_ago < ACTIVE else "active" if now - seen < ACTIVE else "quiet")
        out.append({"agent_id": a, "role": role, "last_seen_ago": ago(seen), "active": seen is not None and now - seen < ACTIVE, "state": state,
                    "owner": owner_of(a), "hook_ago": ago(r["last_hook"]) if r else None, "mcp_ago": ago(r["last_mcp"]) if r else None, "event": ev, "event_ago": ev_ago})
    return out

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
        if who.get("agent_id"):
            # Presence by source: MCP tool traffic, hook traffic (the hooks label every call with the event that fired them), or anything else (CLI, curl). A teammate whose hooks
            # report but whose tools never connected (or the reverse) is not "coordinated": the feed can now say which half is missing.
            ev = dict(scope["headers"]).get(b"x-teambus-event", b"").decode()[:16]; now = time.time(); mcp = scope["path"].startswith("/mcp")
            run("INSERT INTO agents(agent_id,role,last_seen,last_mcp,last_hook,last_event,last_event_ts) VALUES(?,?,?,?,?,?,?) ON CONFLICT(agent_id) DO UPDATE SET last_seen=excluded.last_seen, "
                "role=CASE WHEN excluded.role!='' THEN excluded.role ELSE role END, last_mcp=COALESCE(excluded.last_mcp,last_mcp), last_hook=COALESCE(excluded.last_hook,last_hook), "
                "last_event=COALESCE(excluded.last_event,last_event), last_event_ts=COALESCE(excluded.last_event_ts,last_event_ts)",
                who["agent_id"], who.get("role", ""), now, now if mcp else None, now if ev else None, "tool" if mcp else (ev or None), now if (mcp or ev) else None)
        await s.app(scope, receive, send)

# ---- core (each is both an MCP tool and POST /api/<name>; `agent_id`/`ctx` are optional: identity comes from the token) ----
def whoami(ctx: Context | None = None, agent_id: str = ""):
    """Which agent the hub thinks you are (derived from your token)."""
    a = me(ctx, agent_id); return {"agent_id": a, "role": known().get(a, "")}

def post_intent(files: list[str], summary: str, symbols: list[str] = [], role: str = "", agent_id: str = "", ctx: Context | None = None):
    """Announce you are editing `files` (repo-relative). Repeats for the same files refresh the existing intent."""
    a = me(ctx, agent_id); role = role or known().get(a, ""); fj = json.dumps(sorted(files)); summary = scrub(summary)[0][:300]
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
        + f("build", rows("SELECT builds.*, (SELECT COUNT(*) FROM builds b2 WHERE b2.fixed_by=builds.id) AS fixes FROM builds WHERE ts>? AND agent_id!=?", since, a)), key=lambda x: x["ts"])}

def _suggest(to, k):
    """'Did you mean' candidates for a mistyped agent_id or role (typos are common: roles are free text)."""
    out = []
    for c in difflib.get_close_matches(to, list(k) + sorted({r for r in k.values() if r}), n=3, cutoff=0.7):
        out.append(f"'{c}'" if c in k else f"role '{c}' ({', '.join(sorted(x for x, r in k.items() if r == c))})")
    return out

def ask_teammate_agent(to_agent: str = "", question: str = "", reply_to: int | None = None, from_agent: str = "", ctx: Context | None = None):
    """Send a teammate's agent a message (async). `to_agent` is an agent_id, a role held by one agent, or a group: `@alice` reaches every agent that works for alice, `@backend` every agent with that role (one message each, up to 12). To answer a message, set reply_to=<its id>; the asker then sees it as answered.
    It reaches them at their next prompt, tool call or when they finish; use `outbox` for read receipts, `wait_for_messages` to wait for the answer."""
    a = me(ctx, from_agent); parent = (rows("SELECT * FROM messages WHERE id=?", reply_to) or [None])[0] if reply_to else None
    if reply_to and not parent: return {"error": f"no message #{reply_to}"}
    to = (to_agent or "").strip() or (parent and parent["from_agent"]) or ""; k = known()
    if to == a: return {"error": "that is you; address a teammate"}
    if to.startswith("@"): return _ask_group(a, to, question, reply_to, k)
    if to not in k:
        byrole = [x for x, r in k.items() if r == to and x != a]
        if len(byrole) > 1: return {"error": f"role {to!r} is held by {sorted(byrole)}; address one of them by agent_id"}
        if not to: return {"error": "to_agent is empty: say who should get it. Teammates: " + ", ".join(f"{x} ({r or 'no role'})" for x, r in sorted(k.items()) if x != a)}
        if len(byrole) != 1:
            sug = _suggest(to, {x: r for x, r in k.items() if x != a}); hint = f" Did you mean {' or '.join(sug)}?" if sug else ""
            return {"error": f"unknown recipient {to!r}.{hint} Teammates: " + ", ".join(f"{x} ({r or 'no role'})" for x, r in sorted(k.items()) if x != a)}
        to = byrole[0]
    body, removed = scrub(question)
    mid = run("INSERT INTO messages(ts,from_agent,to_agent,body,reply_to) VALUES(?,?,?,?,?)", time.time(), a, to, body, reply_to)
    if parent and parent["to_agent"] == a: run("UPDATE messages SET answered=1 WHERE id=?", reply_to)
    p = next((x for x in presence() if x["agent_id"] == to), {}); seen = None if p.get("last_seen_ago") is None else time.time() - p["last_seen_ago"]
    out = {"id": mid, "to": to, "recipient": delivery_note(p)}
    warn = []
    if seen is None or time.time() - seen > OFFLINE: warn.append(f"{to} has " + ("never connected" if seen is None else f"been offline {int((time.time() - seen) / 3600)}h") + "; the answer may take a while. Continue other work and check_inbox later.")
    if removed: warn.append(f"removed {removed} value(s) that looked like credentials from your message; share secrets out of band, never through TeamBus.")
    if warn: out["warning"] = " ".join(warn)
    return out

def _ask_group(a, to, question, reply_to, k):
    """@alice = every agent that works for alice; @backend = every agent with that role. One message per recipient, so read receipts and replies stay per agent."""
    if reply_to: return {"error": "reply_to answers one message; reply to the sender by agent_id, not to a group"}
    g = to[1:].strip().lower(); others = {x: r for x, r in k.items() if x != a}
    owners, roles = sorted(x for x in others if owner_of(x).lower() == g), sorted(x for x, r in others.items() if (r or "").lower() == g)
    if owners and roles and owners != roles: return {"error": f"{to} is both a person ({owners}) and a role ({roles}); address them by agent_id"}
    members = owners or roles
    if not members:
        groups = sorted({"@" + owner_of(x) for x in others if owner_of(x)} | {"@" + r for r in others.values() if r}); return {"error": f"no group {to!r}. Groups: {', '.join(groups) or 'none'}. Teammates: {', '.join(sorted(others))}"}
    if len(members) > MAX_GROUP: return {"error": f"{to} has {len(members)} agents; the most one message may reach is {MAX_GROUP}. Address fewer, by agent_id"}
    body, removed = scrub(question); pres = {p["agent_id"]: p for p in presence()}; now = time.time()
    ids = [run("INSERT INTO messages(ts,from_agent,to_agent,body,reply_to) VALUES(?,?,?,?,NULL)", now, a, m, body) for m in members]
    out = {"id": ids[0], "ids": ids, "to": to, "recipients": {m: delivery_note(pres.get(m, {})) for m in members}}
    if removed: out["warning"] = f"removed {removed} value(s) that looked like credentials from your message; share secrets out of band, never through TeamBus."
    return out

def who_of(ctx):
    """The authenticated identity record of the caller (agent_id, role, and flags such as can_create)."""
    req = getattr(getattr(ctx, "request_context", None), "request", ctx); return (getattr(req, "scope", None) or {}).get("teambus") or {}

def _tokens_raw():
    try: return {t: ({"agent_id": v} if isinstance(v, str) else dict(v)) for t, v in json.load(open(TOKENS)).items()}
    except OSError: return {}

def _write_tokens(d):
    prev = os.path.getmtime(TOKENS) if os.path.exists(TOKENS) else 0; tmp = TOKENS + ".tmp"
    with open(tmp, "w") as f: json.dump(d, f, indent=1)
    os.chmod(tmp, 0o600); os.replace(tmp, TOKENS); os.utime(TOKENS, (max(time.time(), prev + 1),) * 2)       # a guaranteed new mtime, so the running hub reloads at once

def create_agent(name: str, role: str, brief: str = "", spawn: bool = False, ctx: Context | None = None):
    """Create a new agent on this team and get its token (only agents the hub owner allowed may; in a sandbox that is the `orchestrator` role).
    `name` is lowercase letters, digits and dashes (2 to 31 characters); `role` is letters, digits, dashes, underscores. `brief` is delivered to the new agent as its first message, from you. With `spawn=true` you also ask your own TeamBus worker (`teambus worker`, running on your machine, if your user started one) to set up a folder and START that agent's Claude session; without a worker the request stays pending.
    Returns the new agent's token and the exact `teambus-join` command to run, in the project folder where that new agent's Claude session will work. Give the token only to that session; never
    post it in a message. Limits: you can create a few agents, not unlimited ones, and agents you create cannot create more."""
    w = who_of(ctx); a = w.get("agent_id") or ""
    if not a: return {"error": "no identity: connect through a token"}
    if not w.get("can_create"): return {"error": "you may not create agents. The hub owner must allow it for your agent (in a sandbox, give it the role `orchestrator`; on your own hub, set \"can_create\": true on its token)."}
    name, role = (name or "").strip(), (role or "").strip()
    if not AGENT_ID_RE.fullmatch(name): return {"error": "name must be 2 to 31 characters: a lowercase letter first, then lowercase letters, digits or dashes"}
    if not AGENT_ROLE_RE.fullmatch(role): return {"error": "role must be 1 to 24 characters: letters, digits, dashes or underscores"}
    with _tokfile:
        d = _tokens_raw(); ids = {v.get("agent_id") for v in d.values()} | set(known())
        if name in ids: return {"error": f"an agent named {name!r} already exists; pick another name"}
        if sum(1 for v in d.values() if v.get("created_by") == a) >= MAX_CREATED: return {"error": f"you have already created {MAX_CREATED} agents, the most one agent may create; remove one with remove_agent or ask your user"}
        if len(ids) >= MAX_AGENTS_HUB: return {"error": f"this team is full ({MAX_AGENTS_HUB} agents)"}
        tok = secrets.token_urlsafe(24); d[tok] = {"agent_id": name, "role": role, "created_by": a, **({"owner": w["owner"]} if w.get("owner") else {})}; _write_tokens(d)
    text, _ = scrub(brief)
    if text.strip(): run("INSERT INTO messages(ts,from_agent,to_agent,body,reply_to) VALUES(?,?,?,?,NULL)", time.time(), a, name, text[:4000])
    out_spawn = {}
    if spawn: out_spawn = {"spawn_id": run("INSERT INTO spawns(ts,requested_by,agent_id,role,brief) VALUES(?,?,?,?,?)", time.time(), a, name, role, text[:4000]),
                           "spawn": "requested: a `teambus worker` running for you will start this agent's session. If none is running, the request stays pending until your user starts one."}
    return {"agent_id": name, "role": role, "token": tok, "created_by": a, **out_spawn,
            "join": f'teambus-join --hub "$TEAMBUS_URL" --agent-id {name} --role {role} --token {tok}',
            "next": "Run `join` in the project folder where the new agent's Claude session will work (it sets up that folder), then start `claude` there. The brief, if you gave one, is in its inbox."}

def list_spawns(status: str = "pending", ctx: Context | None = None):
    """Spawn requests YOU made (what your `teambus worker` acts on), with the new agent's token so the worker can join it. Only the requester ever sees them."""
    a = who_of(ctx).get("agent_id") or ""
    if not a: return {"spawns": []}
    toks = {v.get("agent_id"): (t, v) for t, v in _tokens_raw().items() if v.get("created_by") == a}
    out = []
    for r in rows("SELECT * FROM spawns WHERE requested_by=? AND status=? ORDER BY id LIMIT 20", a, status):
        if r["agent_id"] in toks: out.append({**r, "token": toks[r["agent_id"]][0]})              # a removed agent has no token and is skipped
    return {"spawns": out}

def spawn_update(id: int, status: str, note: str = "", ctx: Context | None = None):
    """Report progress on one of your spawn requests: claimed, running, done or failed."""
    a = who_of(ctx).get("agent_id") or ""
    if status not in ("claimed", "running", "done", "failed"): return {"error": "status must be claimed, running, done or failed"}
    if not rows("SELECT id FROM spawns WHERE id=? AND requested_by=?", id, a): return {"error": f"no spawn request #{id} of yours"}
    run("UPDATE spawns SET status=?, note=? WHERE id=?", status, scrub(note)[0][:300], id); return {"id": id, "status": status}

def remove_agent(name: str, ctx: Context | None = None):
    """Remove an agent that YOU created (its token stops working at once). You cannot remove agents you did not create."""
    a = who_of(ctx).get("agent_id") or ""
    with _tokfile:
        d = _tokens_raw(); hit = [t for t, v in d.items() if v.get("agent_id") == (name or "").strip() and v.get("created_by") == a and a]
        if not hit: return {"error": f"no agent {name!r} created by you"}
        for t in hit: del d[t]
        _write_tokens(d)
    return {"removed": name}

def outbox(limit: int = 15, agent_id: str = "", ctx: Context | None = None):
    """Messages you sent, newest first, with read receipts: whether the recipient has read each one (and when), whether they answered, and the ids of the replies."""
    a = me(ctx, agent_id); now = time.time(); out = []
    for m in rows("SELECT * FROM messages WHERE from_agent=? ORDER BY id DESC LIMIT ?", a, max(1, min(int(limit), 50))):
        out.append({"id": m["id"], "to": m["to_agent"], "body": m["body"][:100], "sent_s_ago": int(now - m["ts"]), "read": bool(m["read"]), "read_s_ago": int(now - m["read_ts"]) if m["read_ts"] else None,
                    "answered": bool(m["answered"]), "replies": [r["id"] for r in rows("SELECT id FROM messages WHERE reply_to=? ORDER BY id", m["id"])]})
    return {"sent": out}

def check_inbox(peek: bool = False, agent_id: str = "", ctx: Context | None = None):
    """Your unread messages (questions and replies). Marks them read unless peek=true."""
    a = me(ctx, agent_id); msgs = rows("SELECT * FROM messages WHERE to_agent=? AND read=0 ORDER BY id", a)
    if msgs and not peek: run(f"UPDATE messages SET read=1, read_ts=? WHERE id IN ({','.join(str(int(m['id'])) for m in msgs)})", time.time())
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
    """Record a build/test result. Only the first line of the command and the last lines of the log are kept, with anything that looks like a credential removed.
    A passing build also marks the same command's earlier failure by this agent as fixed (the feed then links the two)."""
    a = me(ctx, agent_id); raw = (cmd or "").strip(); first = raw.splitlines()[0][:160] if raw else ""
    shown, n1 = scrub(first + (" ..." if "\n" in raw else "")); tail, n2 = scrub("\n".join((log_tail or "").strip().splitlines()[-12:])[-800:])
    now = time.time(); bid = run("INSERT INTO builds(ts,agent_id,ok,log_tail,cmd) VALUES(?,?,?,?,?)", now, a, int(ok), tail, shown)
    if ok: run("UPDATE builds SET fixed_by=?, fixed_ts=? WHERE agent_id=? AND ok=0 AND fixed_by IS NULL AND cmd=? AND id<?", bid, now, a, shown, bid)
    out = {"id": bid}
    if n1 + n2: out["warning"] = f"removed {n1 + n2} value(s) that looked like credentials from the command or log; never send secrets through TeamBus"
    return out

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
    now = time.time(); q = rows("SELECT * FROM messages WHERE reply_to IS NULL ORDER BY id DESC LIMIT 30"); pres = presence(); by = {p["agent_id"]: p for p in pres}
    d = {"now": now, "agents": pres, "intents": rows("SELECT * FROM intents WHERE status='open' AND ts>? ORDER BY ts DESC LIMIT 50", now - INTENT_TTL),
            "contracts": rows("SELECT * FROM contracts WHERE ts>? AND status='open' ORDER BY ts DESC LIMIT 20", now - CONTRACT_TTL), "builds": rows("SELECT * FROM builds ORDER BY ts DESC LIMIT 20"),
            "questions": [{**m, "answered": bool(m["answered"]), **({} if m["read"] else {"waiting": delivery_note(by.get(m["to_agent"], {}))})} for m in q], "feed": rows("SELECT 'intent' AS type, ts, agent_id, summary AS text, status FROM intents UNION ALL SELECT 'contract', ts, agent_id, name || ': ' || spec, '' FROM contracts UNION ALL SELECT 'message', ts, from_agent, '-> ' || to_agent || ': ' || body, CASE WHEN answered=1 THEN 'answered' WHEN read=1 THEN 'read' ELSE 'unread' END FROM messages ORDER BY ts DESC LIMIT 40")}
    for f in d["feed"]:
        if f["type"] == "message" and f["status"] == "unread": f["note"] = delivery_note(by.get(f["text"][3:f["text"].index(":")], {}))
    return d

# ---- MCP tools (docstrings are what agents read) ----
def check_inbox_tool(peek: bool = False, agent_id: str = "", ctx: Context | None = None):
    """Your unread messages (questions and replies). Marks them read unless peek=true. Returns {"unread": N, "messages": [...]}."""
    m = check_inbox(peek, agent_id, ctx); return {"unread": len(m), "messages": m}   # an empty list would render as "no output" in the agent's tool result
mcp.tool(name="check_inbox")(check_inbox_tool)

async def wait_for_messages(timeout_seconds: int = 30, agent_id: str = "", ctx: Context | None = None):
    """Wait (up to 55 s) until a teammate's message arrives, then return it. Use this when you are waiting for an answer instead of polling check_inbox in a loop."""
    t0 = time.time(); end = t0 + max(1, min(int(timeout_seconds), 55))
    while True:
        m = check_inbox(False, agent_id, ctx)
        if m: return {"unread": len(m), "messages": m, "waited_seconds": round(time.time() - t0, 1)}
        if time.time() >= end: return {"unread": 0, "messages": [], "waited_seconds": round(time.time() - t0, 1), "note": "nothing yet; call again to keep waiting"}
        await asyncio.sleep(1.5)
mcp.tool(name="wait_for_messages")(wait_for_messages)

for fn in (whoami, post_intent, resolve_intent, resolve_contract, asked, team_feed, ask_teammate_agent, outbox, thread, publish_contract, report_build, check_conflicts, create_agent, remove_agent): mcp.tool()(fn)

# ---- REST mirror so hooks/CLI (plain scripts) call the same functions, plus the web view ----
API = dict(whoami=whoami, post_intent=post_intent, resolve=resolve_intent, resolve_contract=resolve_contract, asked=asked, feed=team_feed, ask=ask_teammate_agent, inbox=check_inbox, outbox=outbox, thread=thread,
           contract=publish_contract, build=report_build, conflicts=check_conflicts, digest=digest, create_agent=create_agent, remove_agent=remove_agent, spawns=list_spawns, spawn_update=spawn_update)

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

def add_agent(agent_id, role, owner="", can_create=False):
    """`python server.py add-agent ID ROLE [OWNER] [--can-create]`: append a new token to the tokens file and print it (the running hub picks it up on its next request)."""
    import secrets; t = secrets.token_urlsafe(24); d = json.load(open(TOKENS)) if os.path.exists(TOKENS) else {}
    d[t] = {"agent_id": agent_id, "role": role, **({"owner": owner} if owner else {}), **({"can_create": True} if can_create else {})}; json.dump(d, open(TOKENS, "w"), indent=1); os.chmod(TOKENS, 0o600); print(t)

if __name__ == "__main__":
    if sys.argv[1:2] == ["add-agent"]: sys.exit(add_agent(*[x for x in sys.argv[2:] if x != "--can-create"][:3], can_create="--can-create" in sys.argv[2:]))
    if not (NOAUTH or VIEWER or os.path.exists(TOKENS)): sys.exit(f"refusing to start without auth: create {TOKENS} (token -> agent map) and/or set TEAMBUS_TOKEN, or TEAMBUS_NOAUTH=1 for local dev")
    uvicorn.run(app, host=mcp.settings.host, port=mcp.settings.port, log_level="warning")
