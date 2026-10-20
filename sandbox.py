"""TeamBus public sandbox: lets anyone spin up a private, short-lived hub (with alice/bob/carol tokens) and try TeamBus without installing a hub.
Each sandbox is its own `server.py` process + SQLite file, so visitors never see each other's data (no cross-visitor prompt injection).
This service creates them (POST /new), reverse-proxies /s/<id>/... to the right one, rate-limits, caps size, and deletes everything on expiry.
Run: python sandbox.py        (env: SANDBOX_PUBLIC_URL, SANDBOX_DIR, SANDBOX_TTL, SANDBOX_MAX, SANDBOX_NEW_PER_IP_HOUR, SANDBOX_RPM, SANDBOX_MAX_DB_MB, SANDBOX_MAX_BODY, SANDBOX_TRUST_PROXY, HOST, PORT)"""
import asyncio, collections, json, mimetypes, os, secrets, shutil, socket, subprocess, sys, time
from contextlib import asynccontextmanager
from pathlib import Path
import httpx, uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route

E = lambda k, d="": os.environ.get(k, d)
HERE = Path(__file__).resolve().parent
DIR = Path(E("SANDBOX_DIR", str(HERE / "sandboxes"))).resolve()
TTL, MAX, PER_IP, RPM = int(E("SANDBOX_TTL", 3600)), int(E("SANDBOX_MAX", 20)), int(E("SANDBOX_NEW_PER_IP_HOUR", 3)), int(E("SANDBOX_RPM", 600))
MAX_DB, MAX_BODY, TRUST_PROXY = int(float(E("SANDBOX_MAX_DB_MB", 5)) * 1e6), int(E("SANDBOX_MAX_BODY", 65536)), E("SANDBOX_TRUST_PROXY") == "1"
START_TIMEOUT, WARM, SPAWN_PAR = float(E("SANDBOX_START_TIMEOUT", 45)), E("SANDBOX_WARM") == "1", max(1, int(E("SANDBOX_SPAWN_CONCURRENCY", 2)))
IP_HEADER, TRUST_HOPS, DEBUG_IP = E("SANDBOX_CLIENT_IP_HEADER", "x-forwarded-for").lower(), max(1, int(E("SANDBOX_TRUST_PROXY_HOPS", 1))), E("SANDBOX_DEBUG_IP") == "1"
PUBLIC = E("SANDBOX_PUBLIC_URL").rstrip("/")
AGENTS = [("alice-frontend", "frontend"), ("bob-backend", "backend"), ("carol-fullstack", "fullstack")]
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate", "host", "content-length"}
SBX: dict[str, dict] = {}                              # id -> sandbox
NEW_LOG: dict[str, collections.deque] = {}             # ip -> creation timestamps
client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5))

def ip_of(r: Request):
    """Client address for rate limiting. Behind a trusted proxy (SANDBOX_TRUST_PROXY=1) take the Nth entry from the RIGHT of the chosen header
    (SANDBOX_CLIENT_IP_HEADER, default x-forwarded-for; SANDBOX_TRUST_PROXY_HOPS = how many proxies of yours sit in front). Entries further left are client-supplied."""
    v = r.headers.get(IP_HEADER, "") if TRUST_PROXY else ""
    parts = [x.strip() for x in v.split(",") if x.strip()]
    if parts: return parts[-TRUST_HOPS] if len(parts) >= TRUST_HOPS else parts[0]
    return r.client.host if r.client else "?"

def public_base(r: Request):
    """Absolute URL the visitor used. Behind a TLS-terminating proxy the scheme must come from X-Forwarded-Proto (only trusted with SANDBOX_TRUST_PROXY=1)."""
    if PUBLIC: return PUBLIC
    if TRUST_PROXY:
        proto = r.headers.get("x-forwarded-proto", "").split(",")[0].strip() or r.url.scheme
        return f"{proto}://{r.headers.get('x-forwarded-host', r.headers.get('host', r.url.netloc)).split(',')[0].strip()}"
    return str(r.base_url).rstrip("/")

def free_port():
    with socket.socket() as s: s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

def db_bytes(sb): return sum(p.stat().st_size for p in sb["dir"].glob("hub.db*") if p.exists())

def destroy_sb(sb):
    try: sb["proc"].terminate(); sb["proc"].wait(timeout=5)
    except Exception: sb["proc"].kill()
    shutil.rmtree(sb["dir"], ignore_errors=True)

def destroy(sid):
    sb = SBX.pop(sid, None)
    if sb: destroy_sb(sb)

SPARE = None                    # a pre-started sandbox waiting for the next visitor (SANDBOX_WARM=1): on a slow, CPU-starved host a hub takes ~15 s to import
REFILLING = False
BG = set()                      # keep references to background tasks
SPAWN_SEM = None

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

async def create(ip):
    global SPARE
    now = time.time(); log = NEW_LOG.setdefault(ip, collections.deque())
    while log and now - log[0] > 3600: log.popleft()
    if len(log) >= PER_IP: return None, JSONResponse({"error": f"limit: {PER_IP} sandboxes per hour per IP"}, 429)
    if len(SBX) >= MAX: return None, JSONResponse({"error": "sandbox capacity reached, try again in a few minutes"}, 503)
    log.append(now)
    if SPARE and SPARE["proc"].poll() is None: sb, SPARE = SPARE, None
    else: sb = await spawn()
    if not sb: return None, JSONResponse({"error": "could not start sandbox"}, 500)
    sb["expires"] = time.time() + TTL; SBX[sb["id"]] = sb
    kick(); return sb, None

def describe(sb, base):
    url = f"{base}/s/{sb['id']}"
    return {"id": sb["id"], "hub_url": url, "expires_in_seconds": int(sb["expires"] - time.time()), "web_view": f"{url}/?token={sb['viewer']}",
            "agents": [{"agent_id": v["agent_id"], "role": v["role"], "token": t,
                        "join": f"teambus-join --hub {url} --agent-id {v['agent_id']} --role {v['role']} --token {t}"} for t, v in sb["tokens"].items()]}

async def new(request: Request):
    sb, err = await create(ip_of(request))
    if err: return err
    return JSONResponse(describe(sb, public_base(request)), 201)

async def proxy(request: Request):
    sb = SBX.get(request.path_params["sid"])
    if not sb or sb["expires"] < time.time(): return JSONResponse({"error": "sandbox not found or expired"}, 404)
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
    html = PAGE.replace("__MINS__", str(TTL // 60)).replace("__PER_IP__", str(PER_IP)).replace("__MAX_DB_MB__", f"{MAX_DB / 1e6:g}")
    return HTMLResponse(html)
async def asset(request):
    p = ASSETS.get(request.path_params["name"])
    if not p: return JSONResponse({"error": "not found"}, 404)
    return FileResponse(p, headers={"Cache-Control": "public, max-age=86400"})
async def healthz(request): return JSONResponse({"ok": True, "sandboxes": len(SBX), "spare": SPARE is not None})
async def debug_ip(request):   # enabled only with SANDBOX_DEBUG_IP=1: shows what the proxy sends, to pick SANDBOX_CLIENT_IP_HEADER / SANDBOX_TRUST_PROXY_HOPS
    h = request.headers
    return JSONResponse({"derived_ip": ip_of(request), "peer": request.client.host if request.client else None, "x-forwarded-for": h.get("x-forwarded-for"), "cf-connecting-ip": h.get("cf-connecting-ip"), "true-client-ip": h.get("true-client-ip"), "x-forwarded-proto": h.get("x-forwarded-proto"), "public_base": public_base(request)})

async def reaper():
    while True:
        await asyncio.sleep(float(E("SANDBOX_REAP_SECS", 5))); now = time.time()
        for sid, sb in list(SBX.items()):
            if sb["expires"] < now or sb["proc"].poll() is not None or db_bytes(sb) > MAX_DB: destroy(sid)
        global SPARE
        if SPARE and SPARE["proc"].poll() is not None: destroy_sb(SPARE); SPARE = None
        kick()

@asynccontextmanager
async def lifespan(app):
    DIR.mkdir(parents=True, exist_ok=True)
    for old in DIR.glob("sb-*"): shutil.rmtree(old, ignore_errors=True)   # leftovers from a crashed run
    task = asyncio.create_task(reaper()); kick()
    yield
    task.cancel()
    global SPARE
    for sid in list(SBX): destroy(sid)
    if SPARE: destroy_sb(SPARE); SPARE = None
    await client.aclose()

app = Starlette(routes=[Route("/", home), Route("/healthz", healthz), *([Route("/_ip", debug_ip)] if DEBUG_IP else []), Route("/assets/{name}", asset), Route("/new", new, methods=["POST"]),
                        Route("/s/{sid}/{rest:path}", proxy, methods=["GET", "POST", "DELETE", "OPTIONS"]), Route("/s/{sid}", proxy, methods=["GET"], name="bare")], lifespan=lifespan)

if __name__ == "__main__":
    uvicorn.run(app, host=E("HOST", "127.0.0.1"), port=int(E("PORT", 8780)), log_level=E("SANDBOX_LOG_LEVEL", "warning"), proxy_headers=False)  # we read X-Forwarded-For ourselves, only when SANDBOX_TRUST_PROXY=1
