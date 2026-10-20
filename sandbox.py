"""TeamBus public sandbox: lets anyone spin up a private, short-lived hub (with alice/bob/carol tokens) and try TeamBus without installing a hub.
Each sandbox is its own `server.py` process + SQLite file, so visitors never see each other's data (no cross-visitor prompt injection).
This service creates them (POST /new), reverse-proxies /s/<id>/... to the right one, rate-limits, caps size, and deletes everything on expiry.
Run: python sandbox.py        (env: SANDBOX_PUBLIC_URL, SANDBOX_DIR, SANDBOX_TTL, SANDBOX_MAX, SANDBOX_NEW_PER_IP_HOUR, SANDBOX_RPM, SANDBOX_MAX_DB_MB, SANDBOX_MAX_BODY, SANDBOX_TRUST_PROXY, HOST, PORT)"""
import asyncio, collections, json, mimetypes, os, re, shlex, secrets, shutil, socket, subprocess, sys, time
from contextlib import asynccontextmanager
from pathlib import Path
import httpx, uvicorn
import registry
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
INSTALL_CMD = E("SANDBOX_INSTALL_CMD", "curl -fsSL https://raw.githubusercontent.com/Beastburner/teambus/main/install.sh | sh")
IP_HEADER, TRUST_HOPS, DEBUG_IP = E("SANDBOX_CLIENT_IP_HEADER", "x-forwarded-for").lower(), max(1, int(E("SANDBOX_TRUST_PROXY_HOPS", 1))), E("SANDBOX_DEBUG_IP") == "1"
PUBLIC = E("SANDBOX_PUBLIC_URL").rstrip("/")
AGENTS = [("alice-frontend", "frontend"), ("bob-backend", "backend"), ("carol-fullstack", "fullstack")]
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
            "boot_id": BOOT_ID, "agents": [{"agent_id": v["agent_id"], "role": v["role"]} for v in sb["tokens"].values()], "joined": dict(sb.get("joined", {}))}

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
    track(REG.update(sb["id"], state=state, reason=reason, ended_at=int(time.time()), joined=dict(sb.get("joined", {})), agents=record(sb)["agents"]))

async def spawn():
    """Start one hub process with its own DB and tokens; returns it once /healthz answers, or None."""
    global SPAWN_SEM
    SPAWN_SEM = SPAWN_SEM or asyncio.Semaphore(SPAWN_PAR)      # limit simultaneous imports: they would starve each other on a small CPU
    async with SPAWN_SEM:
        sid = secrets.token_hex(6); d = DIR / f"sb-{sid}"; d.mkdir(parents=True); port = free_port()
        toks = {secrets.token_urlsafe(24): {"agent_id": a, "role": r} for a, r in AGENTS}; viewer = secrets.token_urlsafe(18)
        (d / "tokens.json").write_text(json.dumps(toks)); (d / "tokens.json").chmod(0o600)
        env = {**os.environ, "HOST": "127.0.0.1", "PORT": str(port), "TEAMBUS_DB": str(d / "hub.db"), "TEAMBUS_TOKENS": str(d / "tokens.json"), "TEAMBUS_TOKEN": viewer}
        proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], cwd=HERE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        sb = dict(id=sid, dir=d, port=port, proc=proc, expires=None, hits=collections.deque(), tokens=toks, viewer=viewer)
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline and proc.poll() is None:
            try:
                if (await client.get(f"http://127.0.0.1:{port}/healthz")).status_code == 200: return sb
            except httpx.HTTPError: pass
            await asyncio.sleep(0.25)
        destroy_sb(sb); return None

async def refill():
    global SPARE, REFILLING
    if not WARM or SPARE or REFILLING or len(SBX) + 1 > MAX: return
    REFILLING = True
    try: SPARE = await spawn()
    finally: REFILLING = False

def kick():
    t = asyncio.create_task(refill()); BG.add(t); t.add_done_callback(BG.discard)

async def create(ip, agents=None, label=""):
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
    sb.update(expires=time.time() + TTL, ip=ip, label=label, manage=secrets.token_urlsafe(24), created=time.time(), joined={}); SBX[sb["id"]] = sb
    track(REG.put(record(sb))); kick(); return sb, None

def join_line(url, aid, role, tok): return f"teambus-join --hub {shlex.quote(url)} --agent-id {shlex.quote(aid)} --role {shlex.quote(role)} --token {shlex.quote(tok)}"

def describe(sb, base):
    url = f"{base}/s/{sb['id']}"
    return {"id": sb["id"], "label": sb.get("label", ""), "hub_url": url, "expires_in_seconds": int(sb["expires"] - time.time()), "expires_at": int(sb["expires"]),
            "web_view": f"{url}/?token={sb['viewer']}", "max_agents": MAX_AGENTS, "manage_token": sb["manage"],
            "agents": [{"agent_id": v["agent_id"], "role": v["role"], "token": t, "join": join_line(url, v["agent_id"], v["role"], t)} for t, v in sb["tokens"].items()]}

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
    sb, err = await create(ip_of(request), agents if "agents" in body else None, label)
    if err: return err
    return JSONResponse(describe(sb, public_base(request)), 201)

def managed(request: Request):
    """The sandbox named in the URL, if the bearer token is its manage token; else an error response. Counts against the sandbox's request rate."""
    sb = SBX.get(request.path_params["sid"])
    if not sb or sb["expires"] < time.time(): return None, JSONResponse({"error": "sandbox not found or expired"}, 404)
    auth = request.headers.get("authorization", ""); tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not tok or not secrets.compare_digest(tok, sb["manage"]): return None, JSONResponse({"error": "missing or wrong manage token"}, 401)
    now = time.time(); h = sb["hits"]
    while h and now - h[0] > 60: h.popleft()
    if len(h) >= RPM: return None, JSONResponse({"error": "sandbox rate limit"}, 429, headers={"Retry-After": "10"})
    h.append(now); return sb, None

async def describe_live(sb, base):
    """describe() plus whether each agent has joined, from the hub's own presence data."""
    d = describe(sb, base); seen = await observe_joins(sb)
    for a in d["agents"]: a["joined"] = a["agent_id"] in sb["joined"]; a["joined_at"] = sb["joined"].get(a["agent_id"]); a["last_seen_ago"] = seen.get(a["agent_id"])
    return d

async def manage_get(request: Request):
    sb, err = managed(request)
    return err or JSONResponse(await describe_live(sb, public_base(request)))

async def manage_delete(request: Request):
    sb, err = managed(request)
    if err: return err
    await observe_joins(sb); destroy(sb["id"], "deleted", "deleted by its owner"); kick(); return JSONResponse({"deleted": sb["id"]})

async def manage_add_agent(request: Request):
    sb, err = managed(request)
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
    msg = {"expired": f"This sandbox expired at {when(rec.get('expires_at'))}. Sandboxes last {TTL // 60} minutes.",
           "deleted": f"This sandbox was deleted at {when(rec.get('ended_at'))} ({reason}).",
           "evicted": f"This sandbox was removed at {when(rec.get('ended_at'))}: {reason}."}.get(st, f"This sandbox was lost: {reason}.")
    j = rec.get("joined") or {}
    return {"id": rec.get("id"), "state": st, "reason": reason, "label": rec.get("label", ""), "created_at": rec.get("created_at"), "expires_at": rec.get("expires_at"), "ended_at": rec.get("ended_at"),
            "agents": [{"agent_id": a["agent_id"], "role": a["role"], "joined": a["agent_id"] in j, "joined_at": j.get(a["agent_id"])} for a in rec.get("agents", [])],
            "error": f"{msg} Create a new one at {base}", "message": f"{msg} Create a new one at {base}"}

async def gone_response(request: Request, sid: str):
    """404 for a sandbox that is not running here, saying why when the registry knows."""
    base = public_base(request); live = SBX.get(sid)
    rec = record(live, "expired", f"its {TTL // 60}-minute lifetime ran out", live["expires"]) if live else await reg_get(sid)     # expired a moment ago, not swept yet
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
        return JSONResponse({"id": sid, "state": "active", "label": sb.get("label", ""), "created_at": int(sb.get("created", 0)), "expires_at": int(sb["expires"]), "expires_in_seconds": int(sb["expires"] - now),
                             "agents": [{"agent_id": v["agent_id"], "role": v["role"], "joined": v["agent_id"] in sb["joined"], "joined_at": sb["joined"].get(v["agent_id"]), "last_seen_ago": seen.get(v["agent_id"])} for v in sb["tokens"].values()],
                             "registry": REG.kind})
    return await gone_response(request, sid)

async def proxy(request: Request):
    sb = SBX.get(request.path_params["sid"])
    if not sb or sb["expires"] < time.time(): return await gone_response(request, request.path_params["sid"])
    if not request.path_params.get("rest", "") and not request.url.path.endswith("/"): return RedirectResponse(request.url.path + "/" + (f"?{request.url.query}" if request.url.query else ""))
    now = time.time(); h = sb["hits"]
    while h and now - h[0] > 60: h.popleft()
    if len(h) >= RPM: return JSONResponse({"error": "sandbox rate limit"}, 429, headers={"Retry-After": "10"})
    h.append(now)
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
    html = PAGE.replace("__MINS__", str(TTL // 60)).replace("__PER_IP__", str(PER_IP)).replace("__MAX_DB_MB__", f"{MAX_DB / 1e6:g}").replace("__MAX_ACTIVE__", str(MAX_ACTIVE_PER_IP)).replace("__MAX_AGENTS__", str(MAX_AGENTS)).replace("__INSTALL__", json.dumps(INSTALL_CMD))
    return HTMLResponse(html)
async def asset(request):
    p = ASSETS.get(request.path_params["name"])
    if not p: return JSONResponse({"error": "not found"}, 404)
    return FileResponse(p, headers={"Cache-Control": "public, max-age=86400"})
async def healthz(request): return JSONResponse({"ok": True, "sandboxes": len(SBX), "spare": SPARE is not None, "registry": REG.kind, "registry_errors": REG_ERRORS, **({"registry_last_error": REG_ERROR} if REG_ERROR else {})})
async def debug_ip(request):   # enabled only with SANDBOX_DEBUG_IP=1: shows what the proxy sends, to pick SANDBOX_CLIENT_IP_HEADER / SANDBOX_TRUST_PROXY_HOPS
    h = request.headers
    return JSONResponse({"derived_ip": ip_of(request), "peer": request.client.host if request.client else None, "x-forwarded-for": h.get("x-forwarded-for"), "cf-connecting-ip": h.get("cf-connecting-ip"), "true-client-ip": h.get("true-client-ip"), "x-forwarded-proto": h.get("x-forwarded-proto"), "public_base": public_base(request)})

async def reaper():
    while True:
        await asyncio.sleep(float(E("SANDBOX_REAP_SECS", 5))); now = time.time()
        for sid, sb in list(SBX.items()):
            why = ("expired", f"its {TTL // 60}-minute lifetime ran out") if sb["expires"] < now else ("lost", "its process stopped unexpectedly") if sb["proc"].poll() is not None else \
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
    track(REG.mark_lost(BOOT_ID, time.time()))                       # sandboxes of earlier runs that were still 'active' died with them
    task = asyncio.create_task(reaper()); kick()
    yield
    task.cancel()
    global SPARE
    for sid in list(SBX):
        sb = SBX[sid]
        try: await asyncio.wait_for(REG.update(sid, state="lost", reason="the server stopped", ended_at=int(time.time()), joined=dict(sb.get("joined", {}))), 4)
        except Exception: pass
        destroy_sb(SBX.pop(sid))
    if SPARE: destroy_sb(SPARE); SPARE = None
    await client.aclose()

app = Starlette(routes=[Route("/", home), Route("/healthz", healthz), *([Route("/_ip", debug_ip)] if DEBUG_IP else []), Route("/assets/{name}", asset), Route("/new", new, methods=["POST"]),
                        Route("/status/{sid}", status, methods=["GET"]), Route("/manage/{sid}", manage_get, methods=["GET"]), Route("/manage/{sid}", manage_delete, methods=["DELETE"]), Route("/manage/{sid}/agents", manage_add_agent, methods=["POST"]),
                        Route("/s/{sid}/{rest:path}", proxy, methods=["GET", "POST", "DELETE", "OPTIONS"]), Route("/s/{sid}", proxy, methods=["GET"], name="bare")], lifespan=lifespan)

if __name__ == "__main__":
    uvicorn.run(app, host=E("HOST", "127.0.0.1"), port=int(E("PORT", 8780)), log_level=E("SANDBOX_LOG_LEVEL", "warning"), proxy_headers=False)  # we read X-Forwarded-For ourselves, only when SANDBOX_TRUST_PROXY=1
