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
    assert b"90 minutes" in html and b"7 new sandboxes per hour, 4 at once" in html and b"Up to 6 agents each" in html and b"2 MB" in html and b"https://example.test/i.sh" in html and b",MAXA=6;" in html and b"__" not in html.replace(b"__proto__", b"")   # every placeholder filled from the real config

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
