"""TeamBus public sandbox: lets anyone spin up a private, short-lived hub (with alice/bob/carol tokens) and try TeamBus without installing a hub.
Each sandbox is its own `server.py` process + SQLite file, so visitors never see each other's data (no cross-visitor prompt injection).
This service creates them (POST /new), reverse-proxies /s/<id>/... to the right one, rate-limits, caps size, and deletes everything on expiry.
Run: python sandbox.py        (env: SANDBOX_PUBLIC_URL, SANDBOX_DIR, SANDBOX_TTL, SANDBOX_MAX, SANDBOX_NEW_PER_IP_HOUR, SANDBOX_RPM, SANDBOX_MAX_DB_MB, SANDBOX_MAX_BODY, SANDBOX_TRUST_PROXY, HOST, PORT)"""
import asyncio, base64, collections, hashlib, json, mimetypes, os, re, shlex, secrets, shutil, socket, sqlite3, subprocess, sys, time, zlib
from contextlib import asynccontextmanager
from pathlib import Path
import httpx, uvicorn
import accounts, registry
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route

E = lambda k, d="": os.environ.get(k, d)
HERE = Path(__file__).resolve().parent
DIR = Path(E("SANDBOX_DIR", str(HERE / "sandboxes"))).resolve()
TTL, MAX, PER_IP, RPM = int(E("SANDBOX_TTL", 3600)), int(E("SANDBOX_MAX", 20)), int(E("SANDBOX_NEW_PER_IP_HOUR", 6)), int(E("SANDBOX_RPM", 600))
MAX_DB, MAX_BODY, TRUST_PROXY = int(float(E("SANDBOX_MAX_DB_MB", 5)) * 1e6), int(E("SANDBOX_MAX_BODY", 65536)), E("SANDBOX_TRUST_PROXY") == "1"
START_TIMEOUT, WARM, SPAWN_PAR = float(E("SANDBOX_START_TIMEOUT", 45)), E("SANDBOX_WARM") == "1", max(1, int(E("SANDBOX_SPAWN_CONCURRENCY", 2)))
NOTICE = E("SANDBOX_NOTICE")   # shown in the dialog, e.g. a warning that the host sleeps and wipes sandboxes
INSTALL_CMD = E("SANDBOX_INSTALL_CMD", "curl -fsSL https://raw.githubusercontent.com/Beastburner/teambus/main/install.sh | sh")
IP_HEADER, TRUST_HOPS, DEBUG_IP = E("SANDBOX_CLIENT_IP_HEADER", "x-forwarded-for").lower(), max(1, int(E("SANDBOX_TRUST_PROXY_HOPS", 1))), E("SANDBOX_DEBUG_IP") == "1"
PUBLIC = E("SANDBOX_PUBLIC_URL").rstrip("/")
AGENTS = [("alice-frontend", "frontend"), ("bob-backend", "backend"), ("carol-fullstack", "fullstack")]
MAX_AGE, IDLE = int(E("SANDBOX_MAX_AGE", 86400)), int(E("SANDBOX_IDLE", 7200))   # hard ceiling on any sandbox's life; and how long without any request before it is dropped
RESUME, SNAP_SECS, SNAP_MAX = E("SANDBOX_RESUME", "1") != "0", max(5, int(E("SANDBOX_SNAPSHOT_SECS", 60))), 900_000   # snapshot size limit: one Firestore document is 1 MiB
GOOGLE_ID, JWKS_URL = E("GOOGLE_CLIENT_ID"), E("GOOGLE_JWKS_URL", "https://www.googleapis.com/oauth2/v3/certs")   # sign-in is off unless an OAuth client id is configured
VERIFIER, SESSIONS = accounts.Verifier(GOOGLE_ID, JWKS_URL), None
MAX_AGENTS, MAX_ACTIVE_PER_IP = max(2, int(E("SANDBOX_MAX_AGENTS", 5))), max(1, int(E("SANDBOX_MAX_ACTIVE_PER_IP", 3)))
ID_RE, ROLE_RE = re.compile(r"[a-z][a-z0-9-]{1,30}"), re.compile(r"[A-Za-z0-9_-]{1,24}")   # strict on purpose: both end up in a shell command and in other agents' context

def clean_agent(a):
    """One {agent_id, role} from user input -> (agent_id, role) or an error string."""
    if not isinstance(a, dict) or not isinstance(a.get("agent_id"), str): return "each agent needs an agent_id"
    aid, role = a["agent_id"].strip(), (a.get("role") or "member"); role = role.strip() if isinstance(role, str) else ""
    if not ID_RE.fullmatch(aid): return f"agent name {aid[:20]!r}: use 2 to 31 characters, lowercase letters, numbers and dashes, starting with a letter"
    if not ROLE_RE.fullmatch(role): return f"role {role[:20]!r}: use 1 to 24 letters, numbers, dashes or underscores (no spaces)"
    return aid, role

def parse_agents(raw):
    if raw is None: return list(AGENTS), None
    if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_AGENTS: return None, f"a sandbox needs between 2 and {MAX_AGENTS} agents"
    out, seen = [], set()
    for a in raw:
        c = clean_agent(a)
        if isinstance(c, str): return None, c
        if c[0] in seen: return None, f"agent name {c[0]!r} is used twice"
        seen.add(c[0]); out.append(c)
    return out, None

def clean_label(v):
    if v is None or v == "": return "", None
    if not isinstance(v, str) or len(v.strip()) > 40 or any(ord(ch) < 32 for ch in v): return None, "label: at most 40 characters"
    return v.strip(), None
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate", "host", "content-length"}
SBX: dict[str, dict] = {}                              # id -> sandbox
NEW_LOG: dict[str, collections.deque] = {}             # ip -> creation timestamps
client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5))

def _pick(value, hops):
    parts = [x.strip() for x in value.split(",") if x.strip()]
    return (parts[-hops] if len(parts) >= hops else parts[0]) if parts else ""

def ip_of(r: Request):
    """Client address for rate limiting. Behind a trusted proxy (SANDBOX_TRUST_PROXY=1) use SANDBOX_CLIENT_IP_HEADER (default x-forwarded-for) and take the
    Nth entry from the RIGHT (SANDBOX_TRUST_PROXY_HOPS = how many proxies of yours sit in front; entries further left are client-supplied).
    If a custom header (e.g. cf-connecting-ip) is configured but missing, fall back to x-forwarded-for with the same hop count."""
    if TRUST_PROXY:
        ip = _pick(r.headers.get(IP_HEADER, ""), TRUST_HOPS) or (_pick(r.headers.get("x-forwarded-for", ""), TRUST_HOPS) if IP_HEADER != "x-forwarded-for" else "")
        if ip: return ip
    return r.client.host if r.client else "?"

def public_base(r: Request):
    """Absolute URL the visitor used. Behind a TLS-terminating proxy the scheme must come from X-Forwarded-Proto (only trusted with SANDBOX_TRUST_PROXY=1)."""
    if PUBLIC: return PUBLIC
    if TRUST_PROXY:
        proto = r.headers.get("x-forwarded-proto", "").split(",")[0].strip() or r.url.scheme
        return f"{proto}://{r.headers.get('x-forwarded-host', r.headers.get('host', r.url.netloc)).split(',')[0].strip()}"
    return str(r.base_url).rstrip("/")

def store_tokens(sb, toks):
    """Persist {token: {agent_id, role}} for the sandbox. The hub re-reads tokens.json whenever its mtime changes, so this works on a running hub (and on the warm spare)."""
    path = sb["dir"] / "tokens.json"; prev = path.stat().st_mtime if path.exists() else 0
    tmp = sb["dir"] / "tokens.tmp"; tmp.write_text(json.dumps(toks)); tmp.chmod(0o600); os.replace(tmp, path)
    os.utime(path, (max(time.time(), prev + 1),) * 2)          # guarantee a new mtime so the hub notices
    sb["tokens"] = toks

def write_tokens(sb, agents):                                  # a new set of agents: fresh tokens for all of them
    store_tokens(sb, {secrets.token_urlsafe(24): {"agent_id": a, "role": r} for a, r in agents})

def add_token(sb, agent_id, role):                             # one more agent: every existing token keeps working
    tok = secrets.token_urlsafe(24); store_tokens(sb, {**sb["tokens"], tok: {"agent_id": agent_id, "role": role}}); return tok

def sha(t): return hashlib.sha256(t.encode()).hexdigest()

def deadline(sb):
    """When this sandbox ends unless something changes: the earliest of its hard ceiling, the lifetime its owner chose, and `idle` seconds after its last request."""
    return min(sb["hard"], sb["until"], sb["active"] + IDLE)

def touch(sb):
    """A real request reached the sandbox: push the idle deadline out."""
    sb["active"] = time.time(); sb["expires"] = deadline(sb)

def end_reason(sb):
    """('expired', why) for whichever limit the sandbox ran into."""
    now = time.time()
    if now >= sb["hard"]: return "expired", f"it reached the {fmt_span(MAX_AGE)} maximum age"
    if now >= sb["until"]: return "expired", "the lifetime its owner chose ran out"
    return "expired", f"nothing used it for {fmt_span(IDLE)}"

def fmt_span(secs): return f"{secs // 3600} h" if secs % 3600 == 0 and secs >= 3600 else f"{max(1, round(secs / 60))} min"

def parse_lifetime(body):
    """Seconds the owner asked for, from lifetime_minutes (or until_deleted: true = as long as allowed); (value, error)."""
    if body.get("until_deleted") is True: return MAX_AGE, None
    v = body.get("lifetime_minutes")
    if v is None: return min(TTL, MAX_AGE), None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 1: return None, "lifetime_minutes must be a number of at least 1"
    if v * 60 > MAX_AGE: return None, f"the longest a sandbox can live is {fmt_span(MAX_AGE)} (lifetime_minutes up to {MAX_AGE // 60})"
    return int(v * 60), None

def free_port():
    with socket.socket() as s: s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

def db_bytes(sb): return sum(p.stat().st_size for p in sb["dir"].glob("hub.db*") if p.exists())

def destroy_sb(sb):
    try: sb["proc"].terminate(); sb["proc"].wait(timeout=5)
    except Exception: sb["proc"].kill()
    shutil.rmtree(sb["dir"], ignore_errors=True)

def destroy(sid, state="deleted", reason="deleted by its owner"):
    sb = SBX.pop(sid, None)
    if sb: end(sb, state, reason); destroy_sb(sb)

SPARE = None                    # a pre-started sandbox waiting for the next visitor (SANDBOX_WARM=1): on a slow, CPU-starved host a hub takes ~15 s to import
REFILLING = False
BG = set()                      # keep references to background tasks
SPAWN_SEM = None

# ---- registry: a secret-free record of every sandbox that outlives the process (see registry.py) ----
BOOT_ID = secrets.token_hex(4)
REG = registry.MemoryRegistry(); REG_ERRORS = 0; REG_ERROR = ""
STATUS_HITS: dict[str, collections.deque] = {}

def track(coro):
    """Run a registry write in the background; it must never slow down or break the service."""
    async def run():
        global REG_ERRORS, REG_ERROR
        try: await asyncio.wait_for(coro, 10)
        except Exception as e: REG_ERRORS += 1; REG_ERROR = f"{type(e).__name__}: {e}"[:160]
    t = asyncio.create_task(run()); BG.add(t); t.add_done_callback(BG.discard)

async def reg_get(sid):
    global REG_ERRORS, REG_ERROR
    try: return await asyncio.wait_for(REG.get(sid), 6)
    except Exception as e: REG_ERRORS += 1; REG_ERROR = f"{type(e).__name__}: {e}"[:160]; return None

def record(sb, state="active", reason="", ended=0):
    return {"id": sb["id"], "label": sb.get("label", ""), "created_at": int(sb.get("created", 0)), "expires_at": int(sb["expires"]), "state": state, "reason": reason, "ended_at": int(ended),
            "boot_id": BOOT_ID, "agents": [{"agent_id": v["agent_id"], "role": v["role"]} for v in sb["tokens"].values()], "joined": dict(sb.get("joined", {})), "resumable": False, "owner": sb.get("owner", "")}

async def hub_agents(sb):
    """{agent_id: seconds since that agent last used the hub, or None if never} straight from the sandbox's own hub."""
    try:
        r = await client.get(f"http://127.0.0.1:{sb['port']}/api/status", headers={"Authorization": "Bearer " + sb["viewer"]}, timeout=3)
        return {a["agent_id"]: a.get("last_seen_ago") for a in r.json().get("agents", [])}
    except Exception: return {}

async def observe_joins(sb):
    """An agent has 'joined' the first time it authenticates against the hub (teambus-join's doctor step does exactly that). Remember when."""
    seen = await hub_agents(sb); joined = sb.setdefault("joined", {}); now = time.time(); new = False
    for aid, ago in seen.items():
        if ago is not None and aid not in joined: joined[aid] = int(now - ago); new = True
    if new: track(REG.update(sb["id"], joined=dict(joined)))
    return seen

def end(sb, state, reason):
    """Record how a sandbox ended (best effort) and remove it."""
    track(REG.update(sb["id"], state=state, reason=reason, ended_at=int(time.time()), joined=dict(sb.get("joined", {})), agents=record(sb)["agents"], resumable=False))
    track(REG.delete_snapshot(sb["id"]))                       # an ended sandbox must not come back, and its stored data goes with it

async def spawn(): return await launch()

async def launch(sid=None, toks=None, viewer_hash=None, db=None):
    """Start one hub process with its own DB and tokens; returns it once /healthz answers, or None.
    A resumed sandbox passes its old id, its stored (hashed) tokens, the hash of the viewer token its owner already holds, and its database file."""
    global SPAWN_SEM
    SPAWN_SEM = SPAWN_SEM or asyncio.Semaphore(SPAWN_PAR)      # limit simultaneous imports: they would starve each other on a small CPU
    async with SPAWN_SEM:
        sid = sid or secrets.token_hex(6); d = DIR / f"sb-{sid}"; d.mkdir(parents=True, exist_ok=True); port = free_port()
        toks = toks or {secrets.token_urlsafe(24): {"agent_id": a, "role": r} for a, r in AGENTS}; viewer = secrets.token_urlsafe(18)
        (d / "tokens.json").write_text(json.dumps(toks)); (d / "tokens.json").chmod(0o600)
        if db is not None: (d / "hub.db").write_bytes(db)
        env = {**os.environ, "HOST": "127.0.0.1", "PORT": str(port), "TEAMBUS_DB": str(d / "hub.db"), "TEAMBUS_TOKENS": str(d / "tokens.json"), "TEAMBUS_TOKEN": viewer,
               **({"TEAMBUS_TOKEN_SHA256": viewer_hash} if viewer_hash else {})}
        proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], cwd=HERE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        sb = dict(id=sid, dir=d, port=port, proc=proc, expires=None, hits=collections.deque(), tokens=toks, viewer=viewer, viewer_hash=viewer_hash or sha(viewer))   # viewer: what this service uses itself
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline and proc.poll() is None:
            try:
                if (await client.get(f"http://127.0.0.1:{port}/healthz")).status_code == 200: return sb
            except httpx.HTTPError: pass
            await asyncio.sleep(0.25)
        destroy_sb(sb); return None

# ---- sleeping and waking: a free host stops the whole service after 15 idle minutes, so each sandbox's hub data is copied to the registry and restored on demand ----
def can_resume(): return RESUME and getattr(REG, "persistent", False)

def dump_db(path):
    """A consistent compressed copy of a running hub's SQLite file, as base64 text (None if there is no file)."""
    if not path.exists(): return None
    tmp = path.with_suffix(".snap"); src, dst = sqlite3.connect(path, timeout=5), sqlite3.connect(tmp)
    try:
        with dst: src.backup(dst)
    finally: src.close(); dst.close()
    raw = tmp.read_bytes(); tmp.unlink(); return base64.b64encode(zlib.compress(raw, 9)).decode()

def fingerprint(sb):
    return (max((p.stat().st_mtime_ns for p in sb["dir"].glob("hub.db*") if p.exists()), default=0), hash(tuple(sb["tokens"])), int(sb["until"]), len(sb.get("joined", {})), sb.get("owner", ""), sb["manage_hash"])

async def snapshot(sb):
    """Store everything needed to bring this sandbox back: its data, its tokens as hashes only (never the tokens themselves), and how much lifetime it has left. True if stored."""
    if not can_resume() or "manage_hash" not in sb: return False
    fp = fingerprint(sb); data = await asyncio.to_thread(dump_db, sb["dir"] / "hub.db")
    if data is None or len(data) > SNAP_MAX: return False
    now = time.time()
    await REG.put_snapshot(sb["id"], {"id": sb["id"], "taken_at": int(now), "db": data, "remaining": max(0, int(sb["until"] - now)), "hard": int(sb["hard"]), "created": int(sb.get("created", now)),
        "label": sb.get("label", ""), "joined": dict(sb.get("joined", {})), "manage_hash": sb["manage_hash"], "viewer_hash": sb["viewer_hash"], "owner": sb.get("owner", ""),
        "agents": [{"key": k if k.startswith("sha256:") else "sha256:" + sha(k), "agent_id": v["agent_id"], "role": v["role"]} for k, v in sb["tokens"].items()]})
    await REG.update(sb["id"], resumable=True, snapshot_at=int(now)); sb["snap_fp"] = fp; return True

async def snapshotter():
    while True:
        await asyncio.sleep(SNAP_SECS)
        if not can_resume(): continue
        for sb in list(SBX.values()):
            if sb["id"] in SBX and sb.get("snap_fp") != fingerprint(sb):
                try: await asyncio.wait_for(snapshot(sb), 15)
                except Exception as e: note_registry_error(e)

def note_registry_error(e):
    global REG_ERRORS, REG_ERROR
    REG_ERRORS += 1; REG_ERROR = f"{type(e).__name__}: {e}"[:160]

WAKE_LOCKS: dict[str, asyncio.Lock] = {}

async def wake(sid):
    """(sandbox, None) if it is running or has just been woken; (None, None) if there is nothing to resume; (None, response) if it should have been resumable but could not be now."""
    sb = SBX.get(sid)
    if sb or not can_resume(): return sb, None
    async with WAKE_LOCKS.setdefault(sid, asyncio.Lock()):             # two requests for a sleeping sandbox must start one hub, not two
        if sid in SBX: return SBX[sid], None
        rec = await reg_get(sid)
        if not rec or not rec.get("resumable") or rec.get("state") == "active" and rec.get("boot_id") == BOOT_ID or rec.get("state") not in ("sleeping", "active"): return None, None
        try: snap = await asyncio.wait_for(REG.get_snapshot(sid), 10)
        except Exception as e: note_registry_error(e); return None, JSONResponse({"error": "this sandbox is asleep and its stored data could not be read right now; try again in a minute"}, 503, headers={"Retry-After": "20"})
        if not snap: return None, None
        now = time.time()
        if now >= snap["hard"]:
            track(REG.update(sid, state="expired", reason=f"it reached the {fmt_span(MAX_AGE)} maximum age", ended_at=int(now), resumable=False)); track(REG.delete_snapshot(sid)); return None, None
        if len(SBX) >= MAX: return None, JSONResponse({"error": "this sandbox is asleep and the service is full right now; try again in a few minutes"}, 503, headers={"Retry-After": "30"})
        toks = {a["key"]: {"agent_id": a["agent_id"], "role": a["role"]} for a in snap["agents"]}
        try: db = zlib.decompress(base64.b64decode(snap["db"]))
        except Exception: return None, None
        sb = await launch(sid, toks, snap["viewer_hash"], db)
        if not sb: shutil.rmtree(DIR / f"sb-{sid}", ignore_errors=True); return None, JSONResponse({"error": "could not wake this sandbox; try again in a minute"}, 503, headers={"Retry-After": "20"})
        life = min(max(snap["remaining"], 600), max(60, snap["hard"] - now))     # the time it slept does not count against its lifetime; a little is always left to use
        sb.update(ip="", label=snap.get("label", ""), manage_hash=snap["manage_hash"], created=snap["created"], joined=dict(snap.get("joined", {})), hard=snap["hard"], until=now + life, active=now, resumed=True, owner=snap.get("owner", ""))
        sb["expires"] = deadline(sb); sb["snap_fp"] = fingerprint(sb); SBX[sid] = sb
        track(REG.update(sid, state="active", reason="", boot_id=BOOT_ID, expires_at=int(sb["expires"]), ended_at=0)); return sb, None

async def refill():
    global SPARE, REFILLING
    if not WARM or SPARE or REFILLING or len(SBX) + 1 > MAX: return
    REFILLING = True
    try: SPARE = await spawn()
    finally: REFILLING = False

def kick():
    t = asyncio.create_task(refill()); BG.add(t); t.add_done_callback(BG.discard)

async def create(ip, agents=None, label="", lifetime=None, owner=""):
    global SPARE
    now = time.time(); log = NEW_LOG.setdefault(ip, collections.deque())
    while log and now - log[0] > 3600: log.popleft()
    active = sum(1 for b in SBX.values() if b.get("ip") == ip)
    if active >= MAX_ACTIVE_PER_IP: return None, JSONResponse({"error": f"you already have {active} active sandboxes (the limit is {MAX_ACTIVE_PER_IP}); delete one or wait for it to expire"}, 429)
    if len(log) >= PER_IP: return None, JSONResponse({"error": f"limit: {PER_IP} new sandboxes per hour per visitor"}, 429)
    if len(SBX) >= MAX: return None, JSONResponse({"error": "sandbox capacity reached, try again in a few minutes"}, 503)
    log.append(now)
    if SPARE and SPARE["proc"].poll() is None: sb, SPARE = SPARE, None
    else: sb = await spawn()
    if not sb: return None, JSONResponse({"error": "could not start sandbox"}, 500)
    if agents is not None and [(v["agent_id"], v["role"]) for v in sb["tokens"].values()] != agents: write_tokens(sb, agents)
    now = time.time(); life = min(lifetime or TTL, MAX_AGE)
    sb.update(ip=ip, owner=owner, label=label, manage=(m := secrets.token_urlsafe(24)), manage_hash=sha(m), created=now, joined={}, hard=now + MAX_AGE, until=now + life, active=now); sb["expires"] = deadline(sb); SBX[sb["id"]] = sb
    track(REG.put(record(sb))); kick(); return sb, None

def join_line(url, aid, role, tok): return f"teambus-join --hub {shlex.quote(url)} --agent-id {shlex.quote(aid)} --role {shlex.quote(role)} --token {shlex.quote(tok)}"

def describe(sb, base):
    url = f"{base}/s/{sb['id']}"
    return {"id": sb["id"], "label": sb.get("label", ""), "hub_url": url, "expires_in_seconds": int(sb["expires"] - time.time()), "expires_at": int(sb["expires"]),
            "max_expires_at": int(sb["hard"]), "idle_minutes": IDLE // 60, "max_agents": MAX_AGENTS, "resumed": bool(sb.get("resumed")), "owned": bool(sb.get("owner")),
            **({"web_view": f"{url}/?token={sb['viewer']}", "manage_token": sb["manage"]} if "manage" in sb else {}),    # a resumed sandbox only knows hashes: its owner already holds the real tokens
            "agents": [{"agent_id": v["agent_id"], "role": v["role"], **({} if t.startswith("sha256:") else {"token": t, "join": join_line(url, v["agent_id"], v["role"], t)})} for t, v in sb["tokens"].items()]}

async def body_json(request: Request):
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY: return None, JSONResponse({"error": "request too large"}, 413)
    if not raw.strip(): return None, None
    try: return json.loads(raw), None
    except ValueError: return None, JSONResponse({"error": "request body must be JSON"}, 400)

async def new(request: Request):
    body, err = await body_json(request)
    if err: return err
    if body is not None and not isinstance(body, dict): return JSONResponse({"error": "request body must be a JSON object"}, 400)
    body = body or {}
    agents, e = parse_agents(body.get("agents"))
    if e: return JSONResponse({"error": e}, 400)
    label, e = clean_label(body.get("label"))
    if e: return JSONResponse({"error": e}, 400)
    life, e = parse_lifetime(body)
    if e: return JSONResponse({"error": e}, 400)
    who = signed_in(request, write=True)      # a signed-in browser owns what it creates; without the CSRF header it is treated as anonymous
    sb, err = await create(ip_of(request), agents if "agents" in body else None, label, life, who["owner"] if who else "")
    if err: return err
    return JSONResponse(describe(sb, public_base(request)), 201)

async def managed(request: Request):
    """The sandbox named in the URL (woken first if it is asleep), if the bearer token is its manage token; else an error response. Counts against the sandbox's request rate."""
    auth = request.headers.get("authorization", ""); tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    sb, resp = await wake(request.path_params["sid"])
    if resp: return None, resp
    if not sb or sb["expires"] < time.time(): return None, JSONResponse({"error": "sandbox not found or expired"}, 404)
    by_token = bool(tok) and secrets.compare_digest(sha(tok), sb["manage_hash"])
    who = None if by_token else signed_in(request, write=request.method not in ("GET", "HEAD"))
    if not by_token and not (who and sb.get("owner") and who["owner"] == sb["owner"]): return None, JSONResponse({"error": "missing or wrong manage token"}, 401)
    now = time.time(); h = sb["hits"]
    while h and now - h[0] > 60: h.popleft()
    if len(h) >= RPM: return None, JSONResponse({"error": "sandbox rate limit"}, 429, headers={"Retry-After": "10"})
    h.append(now); touch(sb); return sb, None

async def describe_live(sb, base):
    """describe() plus whether each agent has joined, from the hub's own presence data."""
    d = describe(sb, base); seen = await observe_joins(sb)
    for a in d["agents"]: a["joined"] = a["agent_id"] in sb["joined"]; a["joined_at"] = sb["joined"].get(a["agent_id"]); a["last_seen_ago"] = seen.get(a["agent_id"])
    return d

# ---- accounts: Google sign-in, so a sandbox can be found, woken and re-keyed from any browser ----
def signed_in(request: Request, write=False):
    """{'owner', 'email'} of the signed-in browser, or None. Anything that changes state also needs the X-TeamBus header, which a cross-site form or image cannot send."""
    if SESSIONS is None or (write and request.headers.get("x-teambus") != "1"): return None
    return SESSIONS.read(request.cookies.get("tb_session", ""))

def need_account(request: Request, write=False):
    if not GOOGLE_ID: return None, JSONResponse({"error": "sign-in is not configured on this server"}, 404)
    if write and request.headers.get("x-teambus") != "1": return None, JSONResponse({"error": "send the X-TeamBus: 1 header with this request"}, 400)
    who = signed_in(request, write)
    return (who, None) if who else (None, JSONResponse({"error": "not signed in"}, 401))

async def auth_config(request: Request):
    who = signed_in(request)
    return JSONResponse({"enabled": bool(GOOGLE_ID), "client_id": GOOGLE_ID, "signed_in": bool(who), "email": who["email"] if who else ""})

async def auth_google(request: Request):
    if not GOOGLE_ID: return JSONResponse({"error": "sign-in is not configured on this server"}, 404)
    if request.headers.get("x-teambus") != "1": return JSONResponse({"error": "send the X-TeamBus: 1 header with this request"}, 400)
    body, err = await body_json(request)
    if err: return err
    c = await asyncio.to_thread(VERIFIER.verify, (body or {}).get("credential") if isinstance(body, dict) else None)
    if not c: return JSONResponse({"error": "Google did not accept that sign-in"}, 401)
    resp = JSONResponse({"email": c.get("email", ""), "signed_in": True})
    resp.set_cookie("tb_session", SESSIONS.make(accounts.owner_id(c["sub"]), c.get("email", "")), max_age=SESSIONS.ttl, httponly=True, samesite="lax", secure=public_base(request).startswith("https"), path="/")
    return resp

async def auth_logout(request: Request):
    resp = JSONResponse({"signed_in": False}); resp.delete_cookie("tb_session", path="/"); return resp

def summary(rec, live=None):
    j = rec.get("joined") or {}
    return {"id": rec["id"], "label": rec.get("label", ""), "state": "active" if live else "asleep", "created_at": rec.get("created_at"), "expires_at": int(live["expires"]) if live else rec.get("expires_at"),
            "agents": [{"agent_id": a["agent_id"], "role": a["role"], "joined": a["agent_id"] in j} for a in rec.get("agents", [])]}

async def my_sandboxes(request: Request):
    who, err = need_account(request)
    if err: return err
    try: recs = await asyncio.wait_for(REG.list_owner(who["owner"]), 8)
    except Exception as e: note_registry_error(e); return JSONResponse({"error": "could not read your sandboxes right now; try again"}, 503)
    out = {r["id"]: summary(r, SBX.get(r["id"])) for r in recs}
    for sb in SBX.values():                                  # running here and not yet in the registry listing
        if sb.get("owner") == who["owner"] and sb["id"] not in out: out[sb["id"]] = summary(record(sb), sb)
    return JSONResponse({"email": who["email"], "sandboxes": sorted(out.values(), key=lambda x: -(x.get("created_at") or 0))})

async def owned(request: Request, write=False):
    """The signed-in owner's sandbox from the URL, woken if asleep."""
    who, err = need_account(request, write)
    if err: return None, err
    sb, resp = await wake(request.path_params["sid"])
    if resp: return None, resp
    if not sb or sb["expires"] < time.time() or not sb.get("owner") or sb["owner"] != who["owner"]: return None, JSONResponse({"error": "sandbox not found in your account"}, 404)
    touch(sb); return sb, None

async def my_sandbox(request: Request):
    """Open one of your sandboxes from any browser: wakes it, and gives the web-view link (agent tokens are never shown again: reissue them to get fresh join lines)."""
    sb, err = await owned(request)
    if err: return err
    d = await describe_live(sb, public_base(request)); d.pop("manage_token", None); d["web_view"] = f"{public_base(request)}/s/{sb['id']}/?token={sb['viewer']}"
    for a in d["agents"]: a.pop("token", None); a.pop("join", None)
    return JSONResponse(d)

async def my_reissue(request: Request):
    """Fresh tokens for every agent (the old join lines stop working) and a fresh manage token: how an owner recovers access after losing the browser that held them."""
    sb, err = await owned(request, write=True)
    if err: return err
    fresh = {secrets.token_urlsafe(24): {"agent_id": v["agent_id"], "role": v["role"]} for v in sb["tokens"].values()}; store_tokens(sb, fresh)
    m = secrets.token_urlsafe(24); sb["manage"], sb["manage_hash"] = m, sha(m)
    track(snapshot(sb)); d = describe(sb, public_base(request)); d["web_view"] = f"{public_base(request)}/s/{sb['id']}/?token={sb['viewer']}"; return JSONResponse(d)

async def my_claim(request: Request):
    """Attach an anonymous sandbox (one you hold the manage token for) to your account."""
    who, err = need_account(request, True)
    if err: return err
    body, err = await body_json(request)
    if err: return err
    body = body if isinstance(body, dict) else {}; tok = body.get("manage_token"); sid = body.get("id")
    if not isinstance(tok, str) or not isinstance(sid, str): return JSONResponse({"error": "send {id, manage_token}"}, 400)
    sb, resp = await wake(sid)
    if resp: return resp
    if not sb or not secrets.compare_digest(sha(tok), sb["manage_hash"]): return JSONResponse({"error": "sandbox not found or wrong manage token"}, 404)
    if sb.get("owner") and sb["owner"] != who["owner"]: return JSONResponse({"error": "this sandbox already belongs to another account"}, 409)
    sb["owner"] = who["owner"]; track(REG.update(sid, owner=who["owner"])); track(snapshot(sb)); return JSONResponse({"claimed": sid})

async def manage_get(request: Request):
    sb, err = await managed(request)
    return err or JSONResponse(await describe_live(sb, public_base(request)))

async def delete_asleep(request: Request):
    """Owner deletes a sandbox that is asleep: check the token against the stored hash and purge it, without paying for a wake-up. None if it is not asleep."""
    sid = request.path_params["sid"]; auth = request.headers.get("authorization", ""); tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if sid in SBX or not can_resume(): return None
    rec = await reg_get(sid)
    if not rec or not rec.get("resumable"): return None
    try: snap = await asyncio.wait_for(REG.get_snapshot(sid), 10)
    except Exception as e: note_registry_error(e); return None
    if not snap: return None
    who = signed_in(request, write=True)
    if not (tok and secrets.compare_digest(sha(tok), snap["manage_hash"])) and not (who and snap.get("owner") and who["owner"] == snap["owner"]): return JSONResponse({"error": "missing or wrong manage token"}, 401)
    track(REG.update(sid, state="deleted", reason="deleted by its owner", ended_at=int(time.time()), resumable=False)); track(REG.delete_snapshot(sid)); return JSONResponse({"deleted": sid})

async def manage_delete(request: Request):
    if (gone := await delete_asleep(request)) is not None: return gone
    sb, err = await managed(request)
    if err: return err
    await observe_joins(sb); destroy(sb["id"], "deleted", "deleted by its owner"); kick(); return JSONResponse({"deleted": sb["id"]})

async def manage_extend(request: Request):
    sb, err = await managed(request)
    if err: return err
    body, err = await body_json(request)
    if err: return err
    body = body if isinstance(body, dict) else {}; m = body.get("minutes", 30)
    if isinstance(m, bool) or not isinstance(m, (int, float)) or m < 1: return JSONResponse({"error": "minutes must be a number of at least 1"}, 400)
    now = time.time(); new_until = min(sb["hard"], max(sb["until"], now) + m * 60)
    if new_until <= sb["until"]: return JSONResponse({"error": f"already at the maximum: this sandbox ends by {when(sb['hard'])} at the latest"}, 400)
    sb["until"] = new_until; sb["expires"] = deadline(sb); track(REG.update(sb["id"], expires_at=int(sb["expires"])))
    return JSONResponse(await describe_live(sb, public_base(request)))

async def manage_add_agent(request: Request):
    sb, err = await managed(request)
    if err: return err
    body, err = await body_json(request)
    if err: return err
    c = clean_agent(body if isinstance(body, dict) else None)
    if isinstance(c, str): return JSONResponse({"error": c}, 400)
    cur = [(v["agent_id"], v["role"]) for v in sb["tokens"].values()]
    if len(cur) >= MAX_AGENTS: return JSONResponse({"error": f"a sandbox can have at most {MAX_AGENTS} agents"}, 400)
    if any(a == c[0] for a, _ in cur): return JSONResponse({"error": f"agent name {c[0]!r} already exists in this sandbox"}, 400)
    add_token(sb, *c); track(REG.update(sb["id"], agents=record(sb)["agents"])); d = describe(sb, public_base(request))
    return JSONResponse(next(a for a in d["agents"] if a["agent_id"] == c[0]), 201)

def when(ts): return time.strftime("%H:%M UTC", time.gmtime(ts)) if ts else "an unknown time"

def ended_view(rec, base):
    st, reason = rec.get("state", "unknown"), rec.get("reason", "")
    if st == "active": st, reason = "lost", "the server restarted"                     # still 'active' in the record but not running here: it died with an earlier run
    msg = {"expired": f"This sandbox expired at {when(rec.get('expires_at'))} ({reason}).",
           "deleted": f"This sandbox was deleted at {when(rec.get('ended_at'))} ({reason}).",
           "evicted": f"This sandbox was removed at {when(rec.get('ended_at'))}: {reason}.",
           "sleeping": "This sandbox is asleep and could not be woken just now."}.get(st, f"This sandbox was lost: {reason}.")
    j = rec.get("joined") or {}
    return {"id": rec.get("id"), "state": st, "reason": reason, "label": rec.get("label", ""), "created_at": rec.get("created_at"), "expires_at": rec.get("expires_at"), "ended_at": rec.get("ended_at"),
            "agents": [{"agent_id": a["agent_id"], "role": a["role"], "joined": a["agent_id"] in j, "joined_at": j.get(a["agent_id"])} for a in rec.get("agents", [])],
            "error": f"{msg} Create a new one at {base}", "message": f"{msg} Create a new one at {base}"}

async def gone_response(request: Request, sid: str):
    """404 for a sandbox that is not running here, saying why when the registry knows."""
    base = public_base(request); live = SBX.get(sid)
    rec = record(live, *end_reason(live), live["expires"]) if live else await reg_get(sid)     # expired a moment ago, not swept yet
    if rec: return JSONResponse(ended_view(rec, base), 404)
    note = "" if REG.kind != "memory" else " This server keeps no record across restarts, so an old link usually means it restarted (free instances sleep after 15 idle minutes and are replaced on every deploy)."
    m = f"This server has no record of a sandbox with that id.{note} Create a new one at {base}"
    return JSONResponse({"state": "unknown", "error": m, "message": m}, 404)

async def status(request: Request):
    """Public, secret-free: what happened to this sandbox, and which agents have joined. The id is unguessable, like the hub URL."""
    ip = ip_of(request); now = time.time(); h = STATUS_HITS.setdefault(ip, collections.deque())
    while h and now - h[0] > 60: h.popleft()
    if len(h) >= 120: return JSONResponse({"error": "too many status requests"}, 429, headers={"Retry-After": "10"})
    h.append(now); sid = request.path_params["sid"]; sb = SBX.get(sid)
    if sb and sb["expires"] >= now:
        seen = await observe_joins(sb)
        return JSONResponse({"id": sid, "state": "active", "label": sb.get("label", ""), "created_at": int(sb.get("created", 0)), "expires_at": int(sb["expires"]), "max_expires_at": int(sb["hard"]), "expires_in_seconds": int(sb["expires"] - now),
                             "agents": [{"agent_id": v["agent_id"], "role": v["role"], "joined": v["agent_id"] in sb["joined"], "joined_at": sb["joined"].get(v["agent_id"]), "last_seen_ago": seen.get(v["agent_id"])} for v in sb["tokens"].values()],
                             "registry": REG.kind})
    rec = await reg_get(sid) if can_resume() and not sb else None
    if rec and rec.get("resumable") and (rec.get("state") == "sleeping" or rec.get("state") == "active" and rec.get("boot_id") != BOOT_ID):
        j = rec.get("joined") or {}                           # asleep, not gone: reading its status does not wake it; its next real request does
        return JSONResponse({"id": sid, "state": "asleep", "label": rec.get("label", ""), "created_at": rec.get("created_at"), "message": "This sandbox is asleep (the free host stopped). It wakes on its next request, which takes about 15 seconds.",
                             "agents": [{"agent_id": a["agent_id"], "role": a["role"], "joined": a["agent_id"] in j, "joined_at": j.get(a["agent_id"])} for a in rec.get("agents", [])], "registry": REG.kind})
    return await gone_response(request, sid)

async def proxy(request: Request):
    sb, resp = await wake(request.path_params["sid"])
    if resp: return resp
    if not sb or sb["expires"] < time.time(): return await gone_response(request, request.path_params["sid"])
    if not request.path_params.get("rest", "") and not request.url.path.endswith("/"): return RedirectResponse(request.url.path + "/" + (f"?{request.url.query}" if request.url.query else ""))
    now = time.time(); h = sb["hits"]
    while h and now - h[0] > 60: h.popleft()
    if len(h) >= RPM: return JSONResponse({"error": "sandbox rate limit"}, 429, headers={"Retry-After": "10"})
    h.append(now); touch(sb)
    if int(request.headers.get("content-length") or 0) > MAX_BODY: return JSONResponse({"error": "request too large"}, 413)
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY: return JSONResponse({"error": "request too large"}, 413)
    up = client.build_request(request.method, f"http://127.0.0.1:{sb['port']}/{request.path_params.get('rest', '')}", params=request.query_params, content=body,
                              headers={k: v for k, v in request.headers.items() if k.lower() not in HOP})
    r = await client.send(up, stream=True)
    return StreamingResponse(r.aiter_raw(), r.status_code, {k: v for k, v in r.headers.items() if k.lower() not in HOP}, background=BackgroundTask(r.aclose))

PAGE = (HERE / "web" / "sandbox.html").read_text()   # landing page; the __X__ placeholders are filled from the live limits per request
for _ext, _t in ((".woff2", "font/woff2"), (".js", "text/javascript"), (".jpg", "image/jpeg")): mimetypes.add_type(_t, _ext)   # slim container images ship no MIME table
ASSETS = {p.name: p for p in (HERE / "web" / "assets").glob("*") if p.is_file()}   # fonts + preview images; a fixed whitelist, so no path traversal
async def home(request):
    html = PAGE.replace("__MINS__", str(min(TTL, MAX_AGE) // 60)).replace("__MAXH__", f"{MAX_AGE / 3600:g}").replace("__IDLE__", str(IDLE // 60)).replace("__PER_IP__", str(PER_IP)).replace("__MAX_DB_MB__", f"{MAX_DB / 1e6:g}").replace("__MAX_ACTIVE__", str(MAX_ACTIVE_PER_IP)).replace("__MAX_AGENTS__", str(MAX_AGENTS)).replace("__INSTALL__", json.dumps(INSTALL_CMD)).replace("__NOTICE__", json.dumps(NOTICE))
    return HTMLResponse(html)
async def asset(request):
    p = ASSETS.get(request.path_params["name"])
    if not p: return JSONResponse({"error": "not found"}, 404)
    return FileResponse(p, headers={"Cache-Control": "public, max-age=86400"})
async def healthz(request): return JSONResponse({"ok": True, "sandboxes": len(SBX), "spare": SPARE is not None, "registry": REG.kind, "resume": can_resume(), "sign_in": bool(GOOGLE_ID), "registry_errors": REG_ERRORS, **({"registry_last_error": REG_ERROR} if REG_ERROR else {})})
async def debug_ip(request):   # enabled only with SANDBOX_DEBUG_IP=1: shows what the proxy sends, to pick SANDBOX_CLIENT_IP_HEADER / SANDBOX_TRUST_PROXY_HOPS
    h = request.headers
    return JSONResponse({"derived_ip": ip_of(request), "peer": request.client.host if request.client else None, "x-forwarded-for": h.get("x-forwarded-for"), "cf-connecting-ip": h.get("cf-connecting-ip"), "true-client-ip": h.get("true-client-ip"), "x-forwarded-proto": h.get("x-forwarded-proto"), "public_base": public_base(request)})

async def reaper():
    while True:
        await asyncio.sleep(float(E("SANDBOX_REAP_SECS", 5))); now = time.time()
        for sid, sb in list(SBX.items()):
            why = end_reason(sb) if sb["expires"] < now else ("lost", "its process stopped unexpectedly") if sb["proc"].poll() is not None else \
                  ("evicted", f"it grew past the {MAX_DB / 1e6:g} MB data limit") if db_bytes(sb) > MAX_DB else None
            if why: await observe_joins(sb); destroy(sid, *why)
        global SPARE
        if SPARE and SPARE["proc"].poll() is not None: destroy_sb(SPARE); SPARE = None
        kick()

@asynccontextmanager
async def lifespan(app):
    DIR.mkdir(parents=True, exist_ok=True)
    for old in DIR.glob("sb-*"): shutil.rmtree(old, ignore_errors=True)   # leftovers from a crashed run
    global REG, REG_ERRORS, REG_ERROR
    try: REG = registry.make_registry(client)
    except Exception as e: REG, REG_ERRORS, REG_ERROR = registry.MemoryRegistry(), REG_ERRORS + 1, f"registry disabled, using memory: {type(e).__name__}: {e}"[:160]
    async def startup_registry():
        global REG_ERRORS, REG_ERROR
        try: await REG.check()                                      # surfaces "API disabled / no database / missing role" in /healthz right away, with Google's wording
        except Exception as e: REG_ERRORS += 1; REG_ERROR = f"{type(e).__name__}: {e}"[:300]    # a failed probe must not skip the cleanup below
        await REG.mark_lost(BOOT_ID, time.time(), can_resume())                   # sandboxes of earlier runs that were still 'active' died with them
    global SESSIONS
    SESSIONS = accounts.Sessions(accounts.session_key(E("SANDBOX_SESSION_SECRET"), getattr(REG, "sa", {}).get("private_key", "")))
    track(startup_registry())
    task, snap_task = asyncio.create_task(reaper()), asyncio.create_task(snapshotter()); kick()
    yield
    task.cancel(); snap_task.cancel()
    global SPARE
    async def put_to_sleep(sb):
        """Graceful stop (a deploy, or the host's idle shutdown): save the sandbox, and say it is asleep rather than lost."""
        saved = False
        if can_resume():
            try: await observe_joins(sb); saved = await asyncio.wait_for(snapshot(sb), 6)
            except Exception as e: note_registry_error(e)
        try:
            await asyncio.wait_for(REG.update(sb["id"], **({"state": "sleeping", "reason": "the server stopped; it wakes on its next request"} if saved else
                                   {"state": "lost", "reason": "the server stopped", "ended_at": int(time.time()), "resumable": False}), joined=dict(sb.get("joined", {}))), 2)
        except Exception: pass
    try: await asyncio.wait_for(asyncio.gather(*(put_to_sleep(sb) for sb in list(SBX.values())), return_exceptions=True), 8)    # a host gives a stopping service about 30 s: never spend it all waiting for the registry
    except asyncio.TimeoutError: pass
    for sid in list(SBX): destroy_sb(SBX.pop(sid))
    if SPARE: destroy_sb(SPARE); SPARE = None
    await client.aclose()

app = Starlette(routes=[Route("/", home), Route("/healthz", healthz), *([Route("/_ip", debug_ip)] if DEBUG_IP else []), Route("/assets/{name}", asset), Route("/new", new, methods=["POST"]),
                        Route("/status/{sid}", status, methods=["GET"]), Route("/manage/{sid}", manage_get, methods=["GET"]), Route("/manage/{sid}", manage_delete, methods=["DELETE"]), Route("/manage/{sid}/agents", manage_add_agent, methods=["POST"]), Route("/manage/{sid}/extend", manage_extend, methods=["POST"]),
                        Route("/auth/config", auth_config, methods=["GET"]), Route("/auth/google", auth_google, methods=["POST"]), Route("/auth/logout", auth_logout, methods=["POST"]),
                        Route("/me/sandboxes", my_sandboxes, methods=["GET"]), Route("/me/sandboxes/{sid}", my_sandbox, methods=["GET"]), Route("/me/sandboxes/{sid}/reissue", my_reissue, methods=["POST"]), Route("/me/claim", my_claim, methods=["POST"]),
                        Route("/s/{sid}/{rest:path}", proxy, methods=["GET", "POST", "DELETE", "OPTIONS"]), Route("/s/{sid}", proxy, methods=["GET"], name="bare")], lifespan=lifespan)

if __name__ == "__main__":
    uvicorn.run(app, host=E("HOST", "127.0.0.1"), port=int(E("PORT", 8780)), log_level=E("SANDBOX_LOG_LEVEL", "warning"), proxy_headers=False)  # we read X-Forwarded-For ourselves, only when SANDBOX_TRUST_PROXY=1
