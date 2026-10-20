"""Drives the web view in a real Chromium against a running hub.
usage: python test_webview.py HUB_URL VIEWER_TOKEN ALICE_TOKEN BOB_TOKEN [screenshot.png]   (uses the Chromium from `playwright install chromium`; set CHROMIUM_PATH to use another browser)   (alice-frontend / bob-backend must exist)   (self-signed certs accepted)"""
import json, os, sys, time, urllib.request, ssl
from playwright.sync_api import sync_playwright
URL, VIEW, AGENT, BOB = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3], sys.argv[4]; SHOT = sys.argv[5] if len(sys.argv) > 5 else None
ctx_ssl = ssl._create_unverified_context()
def post(tok, ep, **kw): return json.load(urllib.request.urlopen(urllib.request.Request(f"{URL}/api/{ep}", json.dumps(kw).encode(), {"Content-Type": "application/json", "Authorization": "Bearer " + tok}), context=ctx_ssl))
def check(name, ok, extra=""): print(("PASS " if ok else "FAIL ") + name + (f"  [{extra}]" if extra else "")); check.bad += (not ok)
check.bad = 0
post(BOB, "contract", name="seed contract", spec="field renamed", files=["a.js"]); post(BOB, "build", ok=False, cmd="make test", log_tail="FAILED: boom"); post(BOB, "build", ok=True, cmd="make lint", log_tail="")  # seed: the test must not depend on earlier sessions
q = post(AGENT, "ask", to_agent="bob-backend", question="Is the fullName rename final? <b>bold?</b>")
post(AGENT, "post_intent", files=["web/x.js"], summary="<img src=x onerror=\"window.__xss=1\"> sneaky", symbols=[])
with sync_playwright() as p:
    b = p.chromium.launch(**({"executable_path": os.environ["CHROMIUM_PATH"]} if os.environ.get("CHROMIUM_PATH") else {}), args=["--no-sandbox"]); pg = b.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 1000}).new_page()
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(URL + "/"); check("no token: server refuses the page itself", "unauthorized" in pg.content())
    pg.goto(URL + "/?token=wrong"); check("wrong token refused", "unauthorized" in pg.content())
    pg.goto(f"{URL}/?token={VIEW}"); pg.wait_for_selector("#agents li .dot"); pg.wait_for_function("document.querySelectorAll('#feed li').length > 1")
    txt = lambda sel: pg.inner_text(sel)
    check("title + token-gated page loads", pg.title() == "TeamBus")
    check("team lists agents with presence", "alice-frontend" in txt("#agents") and "bob-backend" in txt("#agents") and pg.locator("#agents .dot.on").count() >= 1, txt("#agents").replace("\n", " | ")[:90])
    check("open question shown (and html shown literally)", "<b>bold?</b>" in txt("#questions") and pg.locator("#questions b").count() == 0)
    check("contract listed", "seed contract" in txt("#contracts"), txt("#contracts")[:60].replace("\n", " "))
    check("failed build is visible", "FAIL" in txt("#builds") and "pass" in txt("#builds"))
    check("intent summary is inert text (no XSS)", pg.evaluate("window.__xss") is None and "sneaky" in txt("#intents") and pg.locator("#intents img").count() == 0)
    check("live feed populated", "contract" in txt("#feed") and "intent" in txt("#feed"))
    # live update: a teammate answers the question -> it leaves the open list within a poll or two, with no reload
    post(BOB, "ask", reply_to=q["id"], question="yes, final")
    pg.wait_for_function("!document.getElementById('questions').innerText.includes('bold?')", timeout=12000); check("page updates live (answered question disappears, no reload)", True)
    if SHOT: pg.screenshot(path=SHOT, full_page=True)
    r = pg.request.post(f"{URL}/api/post_intent", headers={"Authorization": "Bearer " + VIEW, "Content-Type": "application/json"}, data=json.dumps({"files": ["z"], "summary": "z"}))
    check("viewer token is read-only (POST -> 403)", r.status == 403, str(r.status))
    check("no JS errors on the page", not errs, "; ".join(errs))
    b.close()
sys.exit(1 if check.bad else 0)
