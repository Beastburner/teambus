"""Tests for sandbox.py (public sandbox manager). Starts the real manager as a subprocess with small limits; no mocks."""
import asyncio, json, os, socket, subprocess, sys, tempfile, time, shutil, urllib.request, urllib.error
from pathlib import Path
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

HERE = Path(__file__).resolve().parent

def free_port():
    with socket.socket() as s: s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

class Mgr:
    def __init__(s, **env):
        s.dir = tempfile.mkdtemp(prefix="sbx-test-"); s.port = free_port(); s.url = f"http://127.0.0.1:{s.port}"
        s.proc = subprocess.Popen([sys.executable, str(HERE / "sandbox.py")], env={**os.environ, "PORT": str(s.port), "SANDBOX_DIR": s.dir, "SANDBOX_REAP_SECS": "1", **{k: str(v) for k, v in env.items()}}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try: urllib.request.urlopen(s.url + "/healthz", timeout=1); return
            except OSError: time.sleep(0.25)
        raise RuntimeError("manager did not start")
    def stop(s): s.proc.terminate(); s.proc.wait(10); shutil.rmtree(s.dir, ignore_errors=True)
    def children(s):
        try: return [int(x) for x in Path(f"/proc/{s.proc.pid}/task/{s.proc.pid}/children").read_text().split()]
        except OSError: return []
    def sandbox_dirs(s): return list(Path(s.dir).glob("sb-*"))

def req(url, method="GET", token=None, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(url, data, {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as x: return x.status, x.read()
    except urllib.error.HTTPError as e: return e.code, e.read()

def jreq(*a, **k): st, b = req(*a, **k); return st, (json.loads(b) if b.startswith((b"{", b"[")) else b.decode())

@pytest.fixture
def mgr():
    made = []
    def make(**env): m = Mgr(**env); made.append(m); return m
    yield make
    for m in made: m.stop()

def tok(sb, agent): return next(a["token"] for a in sb["agents"] if a["agent_id"] == agent)

def test_create_use_through_proxy_and_web_view(mgr):
    m = mgr(); st, sb = jreq(m.url + "/new", "POST"); assert st == 201 and len(sb["agents"]) == 3 and sb["hub_url"].startswith(m.url + "/s/")
    assert "teambus-join --hub " + sb["hub_url"] in sb["agents"][0]["join"]
    st, me = jreq(sb["hub_url"] + "/api/whoami", "POST", tok(sb, "bob-backend"), {}); assert (st, me["agent_id"]) == (200, "bob-backend")
    st, _ = jreq(sb["hub_url"] + "/api/post_intent", "POST", tok(sb, "bob-backend"), {"files": ["a.js"], "summary": "x", "symbols": []}); assert st == 200
    st, html = req(sb["hub_url"] + "/?token=" + sb["web_view"].split("token=")[1]); assert st == 200 and b"TeamBus" in html            # web view served under the prefix
    st, status = jreq(sb["hub_url"] + "/api/status", token=sb["web_view"].split("token=")[1]); assert st == 200 and status["intents"][0]["agent_id"] == "bob-backend"   # relative api/status resolves under /s/<id>/
    assert req(sb["hub_url"])[0] == 401   # bare /s/<id> redirects to /s/<id>/ (urllib follows it) and reaches the hub, which refuses the token-less request

def test_mcp_through_proxy(mgr):
    m = mgr(); _, sb = jreq(m.url + "/new", "POST")
    async def go():
        async with streamablehttp_client(sb["hub_url"] + "/mcp", headers={"Authorization": "Bearer " + tok(sb, "carol-fullstack")}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize(); res = await s.call_tool("whoami", {}); return res.content[0].text
    assert "carol-fullstack" in asyncio.run(go())

def test_sandboxes_are_isolated(mgr):
    m = mgr(); _, a = jreq(m.url + "/new", "POST"); _, b = jreq(m.url + "/new", "POST")
    assert a["id"] != b["id"] and tok(a, "alice-frontend") != tok(b, "alice-frontend")
    assert jreq(b["hub_url"] + "/api/whoami", "POST", tok(a, "alice-frontend"), {})[0] == 401          # A's token is useless in B
    jreq(a["hub_url"] + "/api/ask", "POST", tok(a, "alice-frontend"), {"to_agent": "bob-backend", "question": "secret-in-A"})
    assert "secret-in-A" not in json.dumps(jreq(b["hub_url"] + "/api/status", token=b["web_view"].split("token=")[1])[1])   # nothing leaks across
    assert jreq(m.url + "/s/doesnotexist/api/status", token="x")[0] == 404

def test_per_ip_creation_limit(mgr):
    m = mgr(SANDBOX_NEW_PER_IP_HOUR=2); assert [jreq(m.url + "/new", "POST")[0] for _ in range(3)] == [201, 201, 429]

def test_global_capacity_cap(mgr):
    m = mgr(SANDBOX_MAX=2, SANDBOX_NEW_PER_IP_HOUR=10); assert [jreq(m.url + "/new", "POST")[0] for _ in range(3)] == [201, 201, 503]

def test_forwarded_header_ignored_unless_trusted(mgr):
    m = mgr(SANDBOX_NEW_PER_IP_HOUR=1)
    for i in range(2):   # spoofing X-Forwarded-For must not give a fresh quota
        r = urllib.request.Request(m.url + "/new", b"", {"X-Forwarded-For": f"9.9.9.{i}"}, method="POST")
        try: st = urllib.request.urlopen(r).status
        except urllib.error.HTTPError as e: st = e.code
    assert st == 429

def test_request_size_and_rate_limits(mgr):
    m = mgr(SANDBOX_MAX_BODY=2000, SANDBOX_RPM=6); _, sb = jreq(m.url + "/new", "POST"); t = tok(sb, "alice-frontend")
    assert jreq(sb["hub_url"] + "/api/ask", "POST", t, {"to_agent": "bob-backend", "question": "x" * 5000})[0] == 413
    codes = [jreq(sb["hub_url"] + "/api/whoami", "POST", t, {})[0] for _ in range(8)]
    assert 429 in codes and codes[0] == 200

def test_expiry_kills_process_and_deletes_data(mgr):
    m = mgr(SANDBOX_TTL=3); _, sb = jreq(m.url + "/new", "POST"); assert len(m.children()) == 1 and len(m.sandbox_dirs()) == 1
    for _ in range(20):
        if not m.children(): break
        time.sleep(0.5)
    assert jreq(sb["hub_url"] + "/api/whoami", "POST", tok(sb, "alice-frontend"), {})[0] == 404 and m.children() == [] and m.sandbox_dirs() == []

def test_db_size_cap_evicts(mgr):
    m = mgr(SANDBOX_MAX_DB_MB=0.03, SANDBOX_RPM=1000); _, sb = jreq(m.url + "/new", "POST"); t = tok(sb, "alice-frontend"); gone = False
    for i in range(60):
        if jreq(sb["hub_url"] + "/api/ask", "POST", t, {"to_agent": "bob-backend", "question": "y" * 8000})[0] == 404: gone = True; break
        time.sleep(0.2)
    assert gone and m.sandbox_dirs() == []

def test_shutdown_cleans_up(mgr):
    m = mgr(); jreq(m.url + "/new", "POST"); kids = m.children(); assert kids
    m.proc.terminate(); m.proc.wait(10); time.sleep(1)
    assert all(not Path(f"/proc/{p}").exists() for p in kids) and m.sandbox_dirs() == []

def test_forwarded_header_honoured_when_trusted(mgr):
    m = mgr(SANDBOX_NEW_PER_IP_HOUR=1, SANDBOX_TRUST_PROXY=1, SANDBOX_MAX=5); out = []
    for ip in ("1.1.1.1", "2.2.2.2", "2.2.2.2"):   # behind our own reverse proxy: the last hop is the real client; same client twice -> limited
        r = urllib.request.Request(m.url + "/new", b"", {"X-Forwarded-For": ip}, method="POST")
        try: out.append(urllib.request.urlopen(r).status)
        except urllib.error.HTTPError as e: out.append(e.code)
    assert out == [201, 201, 429]

def test_landing_page_is_served_with_live_limits(mgr):
    m = mgr(SANDBOX_TTL=5400, SANDBOX_NEW_PER_IP_HOUR=7, SANDBOX_MAX_DB_MB=2, SANDBOX_MAX_ACTIVE_PER_IP=4, SANDBOX_MAX_AGENTS=6, SANDBOX_INSTALL_CMD="curl -fsSL https://example.test/i.sh | sh"); st, html = req(m.url + "/")
    assert st == 200 and b"TeamBus sandbox" in html and b'id="b"' in html
    assert b"90 minutes" in html and b"7 new sandboxes per hour, 4 at once" in html and b"Up to 6 agents each" in html and b"2 MB" in html and b"https://example.test/i.sh" in html and b",MAXA=6,IDLE_M=120,MAXMIN=24*60;" in html and b"__" not in html.replace(b"__proto__", b"")   # every placeholder filled from the real config

def test_assets_served_from_a_fixed_whitelist(mgr):
    m = mgr()
    for name, ctype in (("Geist-Variable.woff2", "font/woff2"), ("InstrumentSerif-Regular.woff2", "font/woff2"), ("Caveat-Bold.woff2", "font"), ("shot-contracts.jpg", "image/jpeg"), ("gsap.min.js", "javascript"), ("three.module.min.js", "javascript")):
        r = urllib.request.urlopen(m.url + "/assets/" + name); assert r.status == 200 and ctype in r.headers["content-type"] and len(r.read()) > 10000
    for bad in ("..%2Fsandbox.py", "%2e%2e/server.py", "nope.txt", "..%2F..%2Fetc%2Fpasswd"): assert req(m.url + "/assets/" + bad)[0] == 404

def _post(url, headers=None):
    r = urllib.request.Request(url, b"", headers or {}, method="POST")
    try:
        with urllib.request.urlopen(r) as x: return x.status, json.loads(x.read())
    except urllib.error.HTTPError as e: return e.code, None

def test_proxy_hops_pick_the_right_entry_from_the_right(mgr):
    m = mgr(SANDBOX_TRUST_PROXY=1, SANDBOX_TRUST_PROXY_HOPS=2, SANDBOX_NEW_PER_IP_HOUR=1, SANDBOX_MAX=9)
    # two proxies of ours in front: the real client is the 2nd from the right; anything further left is client-supplied and must not matter
    codes = [_post(m.url + "/new", {"X-Forwarded-For": f"{spoof}, 5.5.5.5, 10.0.0.1"})[0] for spoof in ("1.1.1.1", "2.2.2.2")]
    assert codes == [201, 429]
    assert _post(m.url + "/new", {"X-Forwarded-For": "1.1.1.1, 6.6.6.6, 10.0.0.1"})[0] == 201

def test_custom_client_ip_header(mgr):
    m = mgr(SANDBOX_TRUST_PROXY=1, SANDBOX_CLIENT_IP_HEADER="CF-Connecting-IP", SANDBOX_NEW_PER_IP_HOUR=1, SANDBOX_MAX=9)
    assert [_post(m.url + "/new", {"CF-Connecting-IP": ip, "X-Forwarded-For": f"9.9.9.{i}"})[0] for i, ip in enumerate(("7.7.7.7", "7.7.7.7", "8.8.8.8"))] == [201, 429, 201]

def test_public_url_uses_forwarded_scheme_behind_a_tls_proxy(mgr):
    m = mgr(SANDBOX_TRUST_PROXY=1); st, d = _post(m.url + "/new", {"X-Forwarded-Proto": "https", "Host": "sandbox.example.com"})
    assert st == 201 and d["hub_url"].startswith("https://sandbox.example.com/s/") and d["web_view"].startswith("https://sandbox.example.com/s/")
    m2 = mgr(); st, d = _post(m2.url + "/new", {"X-Forwarded-Proto": "https"})
    assert d["hub_url"].startswith("http://")    # untrusted: X-Forwarded-Proto is ignored

def test_ip_debug_route_is_off_unless_enabled(mgr):
    assert req(mgr().url + "/_ip")[0] == 404
    m = mgr(SANDBOX_DEBUG_IP=1, SANDBOX_TRUST_PROXY=1); st, b = req(m.url + "/_ip", None); assert st == 200 and b"derived_ip" in b

def test_warm_spare_is_ready_and_handed_out_then_refilled(mgr):
    m = mgr(SANDBOX_WARM=1, SANDBOX_MAX=4, SANDBOX_NEW_PER_IP_HOUR=9)
    for _ in range(60):
        if json.loads(req(m.url + "/healthz")[1])["spare"]: break
        time.sleep(.5)
    assert json.loads(req(m.url + "/healthz")[1])["spare"] and len(m.children()) == 1      # one idle hub already running
    t = time.time(); st, sb = jreq(m.url + "/new", "POST"); assert st == 201 and time.time() - t < 1.5   # handed out without a start-up wait
    for _ in range(60):
        if json.loads(req(m.url + "/healthz")[1])["spare"]: break
        time.sleep(.5)
    assert json.loads(req(m.url + "/healthz")[1])["spare"] and len(m.children()) == 2      # and the next spare is warming
    assert jreq(sb["hub_url"] + "/api/whoami", "POST", tok(sb, "alice-frontend"), {})[0] == 200
    m.proc.terminate(); m.proc.wait(10); time.sleep(1); assert m.sandbox_dirs() == []     # shutdown removes the spare too

def test_capacity_counts_only_claimed_sandboxes_not_the_spare(mgr):
    m = mgr(SANDBOX_WARM=1, SANDBOX_MAX=2, SANDBOX_NEW_PER_IP_HOUR=9)
    codes = []
    for _ in range(3):
        codes.append(jreq(m.url + "/new", "POST")[0]); time.sleep(.5)
    assert codes == [201, 201, 503]

def test_missing_custom_ip_header_falls_back_to_forwarded_for_hops(mgr):
    # the Render chain is "client, cloudflare, render-internal": with CF-Connecting-IP absent, the client is 3rd from the right
    m = mgr(SANDBOX_TRUST_PROXY=1, SANDBOX_CLIENT_IP_HEADER="cf-connecting-ip", SANDBOX_TRUST_PROXY_HOPS=3, SANDBOX_NEW_PER_IP_HOUR=1, SANDBOX_MAX=9)
    chain = lambda c: {"X-Forwarded-For": f"junk, {c}, 162.159.114.99, 10.25.213.21"}
    assert [_post(m.url + "/new", chain(c))[0] for c in ("49.36.72.231", "49.36.72.231", "203.0.113.5")] == [201, 429, 201]
    # and each visitor behind the same Render-internal hop is no longer lumped together
    assert _post(m.url + "/new", {"CF-Connecting-IP": "198.51.100.9", "X-Forwarded-For": "x, 198.51.100.9, 162.159.114.99, 10.25.213.21"})[0] == 201


# ---- custom agents and roles, several sandboxes per visitor, manage endpoints ----
def call(method, url, body=None, token=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(url, data, {"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=15) as x: return x.status, json.loads(x.read() or b"null")
    except urllib.error.HTTPError as e:
        try: j = json.loads(e.read())
        except Exception: j = None
        return e.code, j

AG2 = [{"agent_id": "dana-web", "role": "web"}, {"agent_id": "eli-api", "role": "api"}]

def test_custom_agents_roles_and_label(mgr):
    m = mgr(); st, sb = call("POST", m.url + "/new", {"label": "demo run", "agents": AG2}); assert st == 201
    assert [(a["agent_id"], a["role"]) for a in sb["agents"]] == [("dana-web", "web"), ("eli-api", "api")] and sb["label"] == "demo run" and sb["manage_token"]
    assert "--agent-id dana-web --role web --token " in sb["agents"][0]["join"] and sb["expires_at"] > time.time()
    st, me = call("POST", sb["hub_url"] + "/api/whoami", {}, sb["agents"][1]["token"]); assert (st, me) == (200, {"agent_id": "eli-api", "role": "api"})
    st, status = call("GET", sb["hub_url"] + "/api/status", token=sb["web_view"].split("token=")[1]); assert sorted(a["agent_id"] for a in status["agents"]) == ["dana-web", "eli-api"]   # no alice/bob/carol left over

def test_default_agents_when_no_body_or_no_agents_key(mgr):
    m = mgr(); assert [a["agent_id"] for a in call("POST", m.url + "/new")[1]["agents"]] == ["alice-frontend", "bob-backend", "carol-fullstack"]
    assert [a["role"] for a in call("POST", m.url + "/new", {"label": "x"})[1]["agents"]] == ["frontend", "backend", "fullstack"]

@pytest.mark.parametrize("body", [
    {"agents": [AG2[0]]}, {"agents": [{"agent_id": f"agent-{i}", "role": "r"} for i in range(6)]}, {"agents": "alice"}, {"agents": [AG2[0], AG2[0]]},
    {"agents": [{"agent_id": "Bad Id", "role": "r"}, AG2[1]]}, {"agents": [{"agent_id": "a; rm -rf ~", "role": "r"}, AG2[1]]}, {"agents": [{"agent_id": "$(id)", "role": "r"}, AG2[1]]},
    {"agents": [{"agent_id": "dana-web", "role": "my role"}, AG2[1]]}, {"agents": [{"agent_id": "dana-web", "role": "x" * 25}, AG2[1]]}, {"agents": [{"agent_id": "d", "role": "r"}, AG2[1]]},
    {"agents": [{"role": "r"}, AG2[1]]}, {"label": "x" * 41}, {"label": "tab\there"}, [1, 2]])
def test_invalid_requests_are_rejected_before_anything_starts(mgr, body):
    m = mgr(); st, j = call("POST", m.url + "/new", body); assert st == 400 and j["error"] and m.sandbox_dirs() == []

def test_non_json_body_is_a_400(mgr):
    m = mgr(); assert call("POST", m.url + "/new", raw=b"not json")[0] == 400

def test_active_limit_per_visitor_and_delete_frees_the_slot(mgr):
    m = mgr(SANDBOX_MAX_ACTIVE_PER_IP=2, SANDBOX_NEW_PER_IP_HOUR=20, SANDBOX_MAX=9)
    a = call("POST", m.url + "/new")[1]; b = call("POST", m.url + "/new")[1]; st, j = call("POST", m.url + "/new")
    assert st == 429 and "2 active sandboxes" in j["error"] and len(m.children()) == 2
    assert call("DELETE", m.url + "/manage/" + a["id"], token=a["manage_token"])[0] == 200
    assert call("POST", sb_url := m.url + "/new")[0] == 201                                  # the freed slot is usable again
    assert call("POST", a["hub_url"] + "/api/whoami", {}, a["agents"][0]["token"])[0] == 404  # the deleted one is really gone
    assert b["id"] in [p.name[3:] for p in m.sandbox_dirs()] and a["id"] not in [p.name[3:] for p in m.sandbox_dirs()]

def test_manage_endpoints_require_that_sandboxes_own_token(mgr):
    m = mgr(); a = call("POST", m.url + "/new")[1]; b = call("POST", m.url + "/new")[1]
    assert call("GET", m.url + "/manage/" + a["id"])[0] == 401                                              # no token
    assert call("GET", m.url + "/manage/" + a["id"], token="nope")[0] == 401
    assert call("GET", m.url + "/manage/" + a["id"], token=b["manage_token"])[0] == 401                     # another sandbox's token
    assert call("GET", m.url + "/manage/" + a["id"], token=a["agents"][0]["token"])[0] == 401               # an agent token is not a manage token
    assert call("DELETE", m.url + "/manage/" + a["id"], token=b["manage_token"])[0] == 401 and len(m.children()) == 2
    assert call("GET", m.url + "/manage/doesnotexist", token=a["manage_token"])[0] == 404
    st, d = call("GET", m.url + "/manage/" + a["id"], token=a["manage_token"]); assert st == 200 and d["id"] == a["id"] and len(d["agents"]) == 3

def test_add_an_agent_to_a_running_sandbox(mgr):
    m = mgr(); sb = call("POST", m.url + "/new", {"agents": AG2})[1]
    st, ag = call("POST", m.url + "/manage/" + sb["id"] + "/agents", {"agent_id": "frank-ops", "role": "ops"}, sb["manage_token"])
    assert st == 201 and ag["agent_id"] == "frank-ops" and "--agent-id frank-ops --role ops" in ag["join"]
    assert call("POST", sb["hub_url"] + "/api/whoami", {}, ag["token"])[1] == {"agent_id": "frank-ops", "role": "ops"}          # live, no restart
    assert call("POST", sb["hub_url"] + "/api/whoami", {}, sb["agents"][0]["token"])[0] == 200                                    # existing tokens still work
    assert call("POST", m.url + "/manage/" + sb["id"] + "/agents", {"agent_id": "frank-ops", "role": "x"}, sb["manage_token"])[0] == 400   # duplicate name
    assert call("POST", m.url + "/manage/" + sb["id"] + "/agents", {"agent_id": "Bad Name", "role": "x"}, sb["manage_token"])[0] == 400
    assert call("POST", m.url + "/manage/" + sb["id"] + "/agents", {"agent_id": "gus-qa", "role": "qa"})[0] == 401                        # no manage token

def test_agent_count_is_capped_when_adding(mgr):
    m = mgr(SANDBOX_MAX_AGENTS=3); sb = call("POST", m.url + "/new")[1]       # already 3
    st, j = call("POST", m.url + "/manage/" + sb["id"] + "/agents", {"agent_id": "extra-one", "role": "x"}, sb["manage_token"]); assert st == 400 and "at most 3" in j["error"]

def test_custom_agents_on_the_warm_spare_are_instant_and_replace_the_default_tokens(mgr):
    m = mgr(SANDBOX_WARM=1, SANDBOX_MAX=4, SANDBOX_NEW_PER_IP_HOUR=9)
    for _ in range(60):
        if json.loads(req(m.url + "/healthz")[1])["spare"]: break
        time.sleep(.5)
    t = time.time(); st, sb = call("POST", m.url + "/new", {"agents": AG2}); assert st == 201 and time.time() - t < 2
    assert call("POST", sb["hub_url"] + "/api/whoami", {}, sb["agents"][0]["token"])[1]["agent_id"] == "dana-web"
    status = call("GET", sb["hub_url"] + "/api/status", token=sb["web_view"].split("token=")[1])[1]; assert sorted(a["agent_id"] for a in status["agents"]) == ["dana-web", "eli-api"]


# ---- registry: what happened to a sandbox, who joined, and survival of restarts ----
import http.server, threading, signal, urllib.parse
import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
sys.path.insert(0, str(HERE)); import registry as registry_mod


def wait_for(fn, timeout=20, step=.25):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v: return v
        time.sleep(step)
    return fn()


class FakeFirestore:
    """A strict stand-in for Google Firestore's REST API: verifies the service-account JWT (RS256), requires a bearer token on every call,
    and implements document PATCH (replace, or merge with updateMask), GET and a structured runQuery with a state equality filter."""
    def __init__(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(); self.pub = key.public_key()
        self.docs, self.token_requests, self.bad_auth, self.down, self.requests, self.mode = {}, 0, 0, False, [], None
        fs = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def reply(self, code, obj):
                b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            def body(self): return self.rfile.read(int(self.headers.get("Content-Length") or 0))
            def route(self, method):
                if fs.down: return self.reply(503, {"error": "down"})
                u = urllib.parse.urlparse(self.path); fs.requests.append((method, u.path))
                if u.path == "/token" and method == "POST":
                    form = urllib.parse.parse_qs(self.body().decode()); fs.token_requests += 1
                    try:
                        c = pyjwt.decode(form["assertion"][0], fs.pub, algorithms=["RS256"], audience=f"http://127.0.0.1:{fs.port}/token")
                        assert c["iss"] == "svc@proj.iam.gserviceaccount.com" and c["scope"] == "https://www.googleapis.com/auth/datastore" and form["grant_type"][0].endswith("jwt-bearer")
                    except Exception as e: return self.reply(400, {"error": "invalid_grant", "detail": str(e)})
                    return self.reply(200, {"access_token": "tok-1", "expires_in": 3600})
                if self.headers.get("Authorization") != "Bearer tok-1": fs.bad_auth += 1; return self.reply(401, {"error": "unauthenticated"})
                if fs.mode == "disabled": return self.reply(403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "Cloud Firestore API has not been used in project proj before or it is disabled. Enable it by visiting https://console.developers.google.com/apis/api/firestore.googleapis.com/overview?project=proj then retry.",
                                                                              "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED", "domain": "googleapis.com"}]}})
                if fs.mode == "nodb": return self.reply(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "The database (default) does not exist for project proj. Please visit https://console.cloud.google.com/datastore/setup?project=proj to add a Cloud Datastore or Cloud Firestore database."}})
                pre = "/v1/projects/proj/databases/(default)/documents"
                rid = u.path.rsplit("/", 1)[-1].split(":")[0]
                if u.path.startswith(pre + "/") and (rid == "health-probe" and getattr(fs, "reject_probe", False) or rid.startswith("__") and rid.endswith("__")):       # Firestore reserves ids like __name__ (the real service rejects them with a 400)
                    return self.reply(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": f'Resource id "{rid}" is invalid because it is reserved.'}})
                if u.path == pre + ":runQuery" and method == "POST":
                    f = json.loads(self.body())["structuredQuery"]["where"]["fieldFilter"]; assert f["op"] == "EQUAL" and f["field"]["fieldPath"] == "state"
                    rows = [{"document": {"name": f"{pre}/sandboxes/{i}", "fields": d}} for i, d in fs.docs.items() if d.get("state") == f["value"]]
                    return self.reply(200, rows or [{"readTime": "2026-01-01T00:00:00Z"}])
                if u.path.startswith(pre + "/sandboxes/"):
                    sid = u.path.rsplit("/", 1)[1]
                    if method == "GET": return self.reply(200, {"name": u.path, "fields": fs.docs[sid]}) if sid in fs.docs else self.reply(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": f'Document "projects/proj/databases/(default)/documents/sandboxes/{sid}" not found.'}})
                    if method == "PATCH":
                        fields = json.loads(self.body())["fields"]; masks = urllib.parse.parse_qs(u.query).get("updateMask.fieldPaths")
                        if masks: fs.docs.setdefault(sid, {}).update({k: fields[k] for k in masks})
                        else: fs.docs[sid] = fields
                        return self.reply(200, {"name": u.path, "fields": fs.docs[sid]})
                self.reply(404, {"error": "no such route " + u.path})
            def do_GET(self): self.route("GET")
            def do_POST(self): self.route("POST")
            def do_PATCH(self): self.route("PATCH")

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); self.port = self.srv.server_address[1]; threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def env(self):
        return {"FIREBASE_SERVICE_ACCOUNT": json.dumps({"type": "service_account", "project_id": "proj", "client_email": "svc@proj.iam.gserviceaccount.com", "private_key": self.pem, "token_uri": f"http://127.0.0.1:{self.port}/token"}),
                "FIRESTORE_BASE_URL": f"http://127.0.0.1:{self.port}"}
    def doc(self, sid): return {k: registry_mod.from_fs(v) for k, v in self.docs[sid].items()} if sid in self.docs else None
    def stop(self): self.srv.shutdown()


@pytest.fixture
def fake():
    f = FakeFirestore(); yield f; f.stop()


def test_firestore_value_encoding_round_trips():
    rec = {"id": "a1", "n": 7, "f": 1.5, "t": True, "none": None, "agents": [{"agent_id": "x", "role": "y"}], "joined": {}, "empty": [], "m": {"k": 3}}
    assert registry_mod.from_fs({"mapValue": {"fields": {k: registry_mod.to_fs(v) for k, v in rec.items()}}}) == rec
    assert registry_mod.to_fs(5) == {"integerValue": "5"} and registry_mod.to_fs([]) == {"arrayValue": {}}


def test_status_tracks_joins_and_never_leaks_secrets(mgr):
    m = mgr(); sb = call("POST", m.url + "/new", {"label": "j", "agents": AG2})[1]
    st, s = call("GET", m.url + "/status/" + sb["id"]); assert st == 200 and s["state"] == "active" and s["label"] == "j" and [a["joined"] for a in s["agents"]] == [False, False]
    assert call("POST", sb["hub_url"] + "/api/whoami", {}, sb["agents"][1]["token"])[0] == 200            # what teambus-join's doctor step does
    s = call("GET", m.url + "/status/" + sb["id"])[1]; assert [(a["agent_id"], a["joined"]) for a in s["agents"]] == [("dana-web", False), ("eli-api", True)] and s["agents"][1]["joined_at"]
    blob = json.dumps(s); assert not any(t in blob for t in [a["token"] for a in sb["agents"]] + [sb["manage_token"], sb["web_view"].split("token=")[1]])    # public, so no secrets
    d = call("GET", m.url + "/manage/" + sb["id"], token=sb["manage_token"])[1]; assert [a["joined"] for a in d["agents"]] == [False, True]    # the owner's view says the same


def test_status_explains_deleted_expired_and_unknown(mgr):
    m = mgr(SANDBOX_TTL=3, SANDBOX_NEW_PER_IP_HOUR=20, SANDBOX_MAX_ACTIVE_PER_IP=9)
    a = call("POST", m.url + "/new")[1]; b = call("POST", m.url + "/new")[1]
    call("DELETE", m.url + "/manage/" + a["id"], token=a["manage_token"])
    st, s = call("GET", m.url + "/status/" + a["id"]); assert st == 404 and s["state"] == "deleted" and "deleted" in s["message"] and "Create a new one at" in s["message"]
    st, p = call("GET", a["hub_url"] + "/healthz"); assert st == 404 and p["state"] == "deleted" and p["error"] == p["message"]            # the hub URL itself explains, which is what teambus-join prints
    wait_for(lambda: call("GET", m.url + "/status/" + b["id"])[1].get("state") == "expired", 15)
    s = call("GET", m.url + "/status/" + b["id"])[1]; assert s["state"] == "expired" and "lifetime" in s["reason"] and "expired at" in s["message"]
    st, u = call("GET", m.url + "/status/0123456789ab"); assert st == 404 and u["state"] == "unknown" and "restarted" in u["message"]       # in-memory registry: an unknown id usually means a restart
    assert json.loads(req(m.url + "/healthz")[1])["registry"] == "memory"


def test_firestore_registry_records_the_lifecycle_without_secrets(mgr, fake):
    m = mgr(**fake.env()); assert json.loads(req(m.url + "/healthz")[1])["registry"] == "firestore"
    sb = call("POST", m.url + "/new", {"label": "fs", "agents": AG2})[1]; sid = sb["id"]
    d = wait_for(lambda: fake.doc(sid)); assert d and d["state"] == "active" and d["label"] == "fs" and d["agents"] == [{"agent_id": "dana-web", "role": "web"}, {"agent_id": "eli-api", "role": "api"}] and d["joined"] == {} and d["boot_id"]
    raw = json.dumps(fake.docs[sid]); assert not any(t in raw for t in [a["token"] for a in sb["agents"]] + [sb["manage_token"], sb["web_view"].split("token=")[1]])     # nothing secret is ever stored
    assert call("POST", sb["hub_url"] + "/api/whoami", {}, sb["agents"][0]["token"])[0] == 200; call("GET", m.url + "/status/" + sid)
    assert wait_for(lambda: "dana-web" in (fake.doc(sid) or {}).get("joined", {})), "the join was recorded"
    call("POST", m.url + "/manage/" + sid + "/agents", {"agent_id": "frank-ops", "role": "ops"}, sb["manage_token"])
    assert wait_for(lambda: len((fake.doc(sid) or {}).get("agents", [])) == 3)
    call("DELETE", m.url + "/manage/" + sid, token=sb["manage_token"])
    d = wait_for(lambda: (fake.doc(sid) or {}).get("state") == "deleted" and fake.doc(sid)); assert d["reason"] == "deleted by its owner" and d["ended_at"] and "dana-web" in d["joined"]   # joins survive the end
    assert fake.token_requests == 1 and fake.bad_auth == 0                          # one JWT exchange, reused for every call


def test_firestore_registry_survives_restarts_and_the_join_script_explains_it(mgr, fake, tmp_path):
    m1 = mgr(**fake.env()); sb = call("POST", m1.url + "/new", {"label": "before restart"})[1]; sid = sb["id"]
    assert wait_for(lambda: (fake.doc(sid) or {}).get("state") == "active")
    kids = m1.children(); m1.proc.kill(); m1.proc.wait(10)                          # a crash or redeploy: no shutdown hook runs, the record still says "active"
    for k in kids:
        try: os.kill(k, signal.SIGTERM)
        except OSError: pass
    assert fake.doc(sid)["state"] == "active"
    m2 = mgr(**fake.env())                                                          # the replacement instance (a new boot) cleans up after the old one
    assert wait_for(lambda: fake.doc(sid)["state"] == "lost"), fake.docs[sid]
    assert "restarted" in fake.doc(sid)["reason"] and fake.doc(sid)["ended_at"]
    st, s = call("GET", m2.url + "/status/" + sid); assert st == 404 and s["state"] == "lost" and s["label"] == "before restart" and "restarted" in s["message"]
    st, p = call("GET", f"{m2.url}/s/{sid}/healthz"); assert st == 404 and p["state"] == "lost"
    stub = tmp_path / "bin"; stub.mkdir(); (stub / "claude").write_text("#!/bin/sh\nexit 0\n"); (stub / "claude").chmod(0o755)
    r = subprocess.run([f"{HERE}/teambus-join", "--hub", f"{m2.url}/s/{sid}", "--agent-id", "a", "--role", "r", "--token", "t"], capture_output=True, text=True, env={**os.environ, "PATH": f"{stub}:{os.environ['PATH']}", "HOME": str(tmp_path)})
    assert r.returncode == 1 and "was lost" in r.stderr and "restarted" in r.stderr and "Create a new one at" in r.stderr     # the person joining sees WHY
    assert not (tmp_path / ".claude").exists()                                       # and nothing was changed on their machine


def test_registry_trouble_never_breaks_sandboxes(mgr, fake):
    bad = mgr(FIREBASE_SERVICE_ACCOUNT="{not json"); h = json.loads(req(bad.url + "/healthz")[1])
    assert h["registry"] == "memory" and "registry disabled" in h["registry_last_error"] and call("POST", bad.url + "/new")[0] == 201      # misconfigured: falls back, still serves
    m = mgr(**fake.env()); fake.down = True; t = time.time()
    st, sb = call("POST", m.url + "/new"); assert st == 201 and time.time() - t < 3                                     # Firestore down: creating is not slowed
    assert wait_for(lambda: json.loads(req(m.url + "/healthz")[1])["registry_errors"] >= 1), "the failure is visible in /healthz"
    assert call("POST", sb["hub_url"] + "/api/whoami", {}, sb["agents"][0]["token"])[0] == 200 and call("GET", m.url + "/status/" + sb["id"])[1]["state"] == "active"   # live state needs no registry
    fake.down = False; assert call("DELETE", m.url + "/manage/" + sb["id"], token=sb["manage_token"])[0] == 200


@pytest.mark.parametrize("mode, expect", [("disabled", ["SERVICE_DISABLED", "Cloud Firestore API has not been used", "PERMISSION_DENIED"]), ("nodb", ["does not exist for project", "NOT_FOUND"])])
def test_healthz_reports_googles_own_reason_when_firestore_cannot_be_used(mgr, fake, mode, expect):
    fake.mode = mode; m = mgr(**fake.env())
    h = wait_for(lambda: (lambda j: j if j.get("registry_errors") else None)(json.loads(req(m.url + "/healthz")[1])), 15)
    assert h and h["registry"] == "firestore" and all(x in h["registry_last_error"] for x in expect), h      # the startup probe says WHY, so nobody has to guess from a bare 403
    st, sb = call("POST", m.url + "/new"); assert st == 201 and call("GET", m.url + "/status/" + sb["id"])[1]["state"] == "active"     # and sandboxes carry on regardless


def test_an_ordinary_missing_document_is_not_reported_as_an_error(mgr, fake):
    m = mgr(**fake.env()); st, s = call("GET", m.url + "/status/0123456789ab")
    assert st == 404 and s["state"] == "unknown" and json.loads(req(m.url + "/healthz")[1])["registry_errors"] == 0               # "no such sandbox" is an answer, not a failure


def test_startup_probe_and_restart_cleanup_survive_a_real_shaped_missing_document_reply(mgr, fake):
    # Google answers a probe of a missing document with 404 + 'Document "projects/.../databases/(default)/documents/..." not found.'
    # (it contains the word "databases"); that must read as "no such document", not as "no database", or the startup cleanup never runs.
    fake.docs["old1"] = {k: registry_mod.to_fs(v) for k, v in {"id": "old1", "state": "active", "boot_id": "a-previous-run", "agents": [], "joined": {}}.items()}
    m = mgr(**fake.env()); assert wait_for(lambda: fake.doc("old1")["state"] == "lost"), "the cleanup step ran after the probe"
    h = json.loads(req(m.url + "/healthz")[1]); assert h["registry_errors"] == 0 and "registry_last_error" not in h


def test_missing_document_classifier_matches_googles_real_messages():
    class R:
        def __init__(s, code, body): s.status_code, s._b, s.text = code, body, json.dumps(body)
        def json(s): return s._b
    doc = R(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": 'Document "projects/teambus/databases/(default)/documents/zz/nope" not found.'}})
    nodb = R(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "The database (default) does not exist for project teambus. Please visit https://console.cloud.google.com/datastore/setup?project=teambus"}})
    disabled = R(403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "Cloud Firestore API has not been used in project teambus before or it is disabled."}})
    assert registry_mod._is_missing_document(doc) and not registry_mod._is_missing_document(nodb) and not registry_mod._is_missing_document(disabled)


def test_a_failing_probe_does_not_skip_the_restart_cleanup(mgr, fake):
    # even if the health probe is rejected for any reason, sandboxes left active by an earlier run must still be marked lost
    fake.docs["old2"] = {k: registry_mod.to_fs(v) for k, v in {"id": "old2", "state": "active", "boot_id": "earlier-run", "agents": [], "joined": {}}.items()}
    orig = fake.mode; fake.reject_probe = True
    m = mgr(**fake.env()); assert wait_for(lambda: fake.doc("old2")["state"] == "lost"), "cleanup still ran"


# ---- the hook, doctor and status say WHY TeamBus cannot be used ----
def _hook_start(url, token="x"):
    r = subprocess.run([sys.executable, str(HERE / "hooks" / "hook.py"), "start"], input="{}", capture_output=True, text=True, timeout=20,
                       env={**os.environ, "TEAMBUS_URL": url, "TEAMBUS_TOKEN": token, "CLAUDE_PROJECT_DIR": tempfile.mkdtemp()})
    assert r.returncode == 0                                               # never breaks a session
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def test_session_start_says_why_the_sandbox_is_gone_and_other_failures_in_words(mgr):
    m = mgr(); sb = call("POST", m.url + "/new")[1]; dead = sb["hub_url"]
    call("DELETE", m.url + "/manage/" + sb["id"], token=sb["manage_token"])
    t = _hook_start(dead); assert "TeamBus is offline (This sandbox was deleted" in t and "Create a new one at" in t and "do not retry its tools" in t
    assert "no record of a sandbox" in _hook_start(f"{m.url}/s/0123456789ab")                          # a link the server never issued
    live = call("POST", m.url + "/new")[1]; assert "rejected the token" in _hook_start(live["hub_url"], "wrong-token")
    assert "hub is unreachable" in _hook_start("http://127.0.0.1:1")
    ok = _hook_start(live["hub_url"], live["agents"][0]["token"]); assert "identifies you as agent 'alice-frontend'" in ok and "offline" not in ok      # a healthy sandbox is unchanged


def test_doctor_and_status_explain_a_gone_sandbox(mgr, tmp_path):
    m = mgr(); sb = call("POST", m.url + "/new")[1]; call("DELETE", m.url + "/manage/" + sb["id"], token=sb["manage_token"])
    env = {**os.environ, "HOME": str(tmp_path), "TEAMBUS_URL": sb["hub_url"], "TEAMBUS_TOKEN": "x", "AGENT_ID": "alice-frontend", "AGENT_ROLE": "frontend"}
    d = subprocess.run([sys.executable, str(HERE / "teambus"), "doctor", "--dir", str(tmp_path)], capture_output=True, text=True, env=env, timeout=30)
    assert "[FAIL] hub reachable" in d.stdout and "This sandbox was deleted" in d.stdout and "Create a new one at" in d.stdout, d.stdout
    s = subprocess.run([sys.executable, str(HERE / "teambus"), "status", "--dir", str(tmp_path)], capture_output=True, text=True, env=env, timeout=30)
    assert s.returncode != 0 and "This sandbox was deleted" in s.stderr, s.stderr


def test_notice_is_shown_only_when_configured(mgr):
    assert b'NOTICE=""' in req(mgr().url + "/")[1]
    assert b'NOTICE="Heads up: this host sleeps."' in req(mgr(SANDBOX_NOTICE="Heads up: this host sleeps.").url + "/")[1]


@pytest.mark.skipif(shutil.which("python") is None, reason="start.sh runs `python`, as the Docker images provide")
def test_start_sh_on_render_defaults_to_the_sandbox_with_a_free_hosting_notice(tmp_path):
    def run(extra):
        port = free_port(); env = {**os.environ, "RENDER": "true", "PORT": str(port), "HOST": "127.0.0.1", "SANDBOX_DIR": str(tmp_path / f"s{port}"), **extra}
        for k in ("TEAMBUS_TOKEN", "TEAMBUS_TOKENS", "TEAMBUS_NOAUTH", "SANDBOX_NOTICE"):
            if k not in extra: env.pop(k, None)
        p = subprocess.Popen(["sh", str(HERE / "start.sh")], cwd=HERE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            def up():
                try: return req(f"http://127.0.0.1:{port}/healthz")[0] == 200
                except OSError: return p.poll() is not None          # still starting (refused), or it exited
            wait_for(up, 30)
            st, html = req(f"http://127.0.0.1:{port}/"); return st, html.decode()
        finally: p.terminate(); p.wait(10)
    st, html = run({}); assert st == 200 and "Free hosting: the server sleeps after about 15 idle minutes" in html           # the sandbox started, warning included
    st, html = run({"SANDBOX_NOTICE": ""}); assert st == 200 and "Free hosting" not in html and 'NOTICE=""' in html          # an empty value hides it


# ---- owner-chosen lifetime, extend, idle timeout, hard ceiling ----
def test_owner_chooses_the_lifetime_and_it_is_capped(mgr):
    m = mgr(SANDBOX_TTL=1800, SANDBOX_MAX_AGE=7200, SANDBOX_NEW_PER_IP_HOUR=20, SANDBOX_MAX_ACTIVE_PER_IP=9)
    d = call("POST", m.url + "/new")[1]; assert 1700 < d["expires_in_seconds"] <= 1800                                    # default
    d = call("POST", m.url + "/new", {"lifetime_minutes": 90})[1]; assert 5300 < d["expires_in_seconds"] <= 5400
    d = call("POST", m.url + "/new", {"until_deleted": True})[1]; assert 7100 < d["expires_in_seconds"] <= 7200 and d["max_expires_at"] == d["expires_at"]   # "until I delete" still has the ceiling
    st, e = call("POST", m.url + "/new", {"lifetime_minutes": 121}); assert st == 400 and "longest" in e["error"]
    for bad in (0, -5, "10", True): assert call("POST", m.url + "/new", {"lifetime_minutes": bad})[0] == 400

def test_extend_adds_time_up_to_the_ceiling(mgr):
    m = mgr(SANDBOX_TTL=600, SANDBOX_MAX_AGE=1800, SANDBOX_NEW_PER_IP_HOUR=20, SANDBOX_MAX_ACTIVE_PER_IP=9)
    d = call("POST", m.url + "/new")[1]; url = m.url + "/manage/" + d["id"] + "/extend"
    assert call("POST", url, {"minutes": 10})[0] == 401                                                                   # needs the manage token
    st, x = call("POST", url, {"minutes": 10}, d["manage_token"]); assert st == 200 and x["expires_at"] - d["expires_at"] in (599, 600, 601)
    st, x = call("POST", url, {"minutes": 600}, d["manage_token"]); assert st == 200 and x["expires_at"] == x["max_expires_at"]   # clamped to the ceiling, not rejected
    st, e = call("POST", url, {"minutes": 5}, d["manage_token"]); assert st == 400 and "maximum" in e["error"]             # nothing left to add
    assert call("POST", url, {"minutes": 0}, d["manage_token"])[0] == 400

def test_an_unused_sandbox_expires_early_but_use_keeps_it_alive(mgr):
    m = mgr(SANDBOX_TTL=3600, SANDBOX_IDLE=3, SANDBOX_NEW_PER_IP_HOUR=20, SANDBOX_MAX_ACTIVE_PER_IP=9)
    idle = call("POST", m.url + "/new")[1]; busy = call("POST", m.url + "/new")[1]
    for _ in range(8): call("POST", busy["hub_url"] + "/api/whoami", {}, tok(busy, "alice-frontend")); time.sleep(0.6)      # ~5s of use, longer than the 3s idle limit
    assert call("GET", m.url + "/status/" + busy["id"])[1]["state"] == "active"
    s = wait_for(lambda: call("GET", m.url + "/status/" + idle["id"])[1].get("state") == "expired", 15) and call("GET", m.url + "/status/" + idle["id"])[1]
    assert s["state"] == "expired" and "nothing used it" in s["reason"]

def test_ceiling_beats_activity(mgr):
    m = mgr(SANDBOX_TTL=3600, SANDBOX_MAX_AGE=3, SANDBOX_IDLE=3600)
    d = call("POST", m.url + "/new")[1]
    wait_for(lambda: call("GET", m.url + "/status/" + d["id"])[1].get("state") == "expired", 15)
    assert "maximum age" in call("GET", m.url + "/status/" + d["id"])[1]["reason"]
