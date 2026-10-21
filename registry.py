"""Sandbox registry: a small record of every sandbox this service has issued, kept OUTSIDE the instance so it survives restarts.

Why: a free host sleeps and restarts, which silently deletes every running sandbox. Without a record, a stale join link is a bare 404 and nobody can tell
"expired" from "deleted" from "the server restarted" from "never existed", or check that a join worked. The record answers that.

It never holds secrets: no agent tokens, no manage token, no viewer token. Only ids, labels, agent names and roles, timestamps, state and join times.

Backends
  MemoryRegistry     default. Lives and dies with the process, so after a restart every old id is honestly "unknown".
  FirestoreRegistry  Google Firestore through its REST API (no SDK), authenticated as a service account. Set FIREBASE_SERVICE_ACCOUNT (the JSON) or
                     FIREBASE_SERVICE_ACCOUNT_FILE. FIRESTORE_BASE_URL overrides the endpoint (tests, emulators).
Registry failures never break the service: callers treat every call as best effort."""
import json, os, time
import httpx

COLLECTION = "sandboxes"


def to_fs(v):
    """Python value -> Firestore REST value."""
    if v is None: return {"nullValue": None}
    if isinstance(v, bool): return {"booleanValue": v}
    if isinstance(v, int): return {"integerValue": str(v)}
    if isinstance(v, float): return {"doubleValue": v}
    if isinstance(v, str): return {"stringValue": v}
    if isinstance(v, (list, tuple)): return {"arrayValue": {"values": [to_fs(x) for x in v]} if v else {}}
    if isinstance(v, dict): return {"mapValue": {"fields": {str(k): to_fs(x) for k, x in v.items()}} if v else {}}
    raise TypeError(f"cannot store {type(v).__name__}")


def from_fs(f):
    """Firestore REST value -> Python value."""
    if "stringValue" in f: return f["stringValue"]
    if "integerValue" in f: return int(f["integerValue"])
    if "doubleValue" in f: return float(f["doubleValue"])
    if "booleanValue" in f: return f["booleanValue"]
    if "nullValue" in f: return None
    if "arrayValue" in f: return [from_fs(x) for x in f["arrayValue"].get("values", [])]
    if "mapValue" in f: return {k: from_fs(x) for k, x in f["mapValue"].get("fields", {}).items()}
    if "timestampValue" in f: return f["timestampValue"]
    return None


def _error_body(r):
    try: j = r.json(); j = j[0] if isinstance(j, list) and j else j; return (j or {}).get("error") or {}
    except Exception: return {}


def _is_missing_document(r):
    """Google's reply for a document that simply is not there: 404, message 'Document "projects/.../documents/<collection>/<id>" not found.'
    Anything else with a 404 (no database in the project, wrong project) is a real error. The path in that message contains the word 'databases',
    so match the whole shape, not keywords."""
    e = _error_body(r); m = (e.get("message") or "").strip().lower()
    return r.status_code == 404 and e.get("status", "NOT_FOUND") == "NOT_FOUND" and m.startswith("document ") and m.endswith("not found.")


def describe_error(r):
    """Google's own explanation, not just the status code: what /healthz shows when the registry cannot be used."""
    e = _error_body(r); reason = next((d.get("reason") for d in e.get("details", []) if isinstance(d, dict) and d.get("reason")), "")
    msg = " ".join((e.get("message") or r.text or "").split())[:300]
    return f"Firestore HTTP {r.status_code} {e.get('status', '')} {reason}: {msg}".replace("  ", " ")


class MemoryRegistry:
    kind = "memory"
    persistent = False                                          # dies with the process, so a sandbox can never be resumed from it

    def __init__(self): self.docs, self.snaps = {}, {}
    async def list_owner(self, owner): return [dict(d) for d in self.docs.values() if owner and d.get("owner") == owner and d.get("state") in ("active", "sleeping")]
    async def put_snapshot(self, sid, rec): self.snaps[sid] = dict(rec)
    async def get_snapshot(self, sid): return dict(self.snaps[sid]) if sid in self.snaps else None
    async def delete_snapshot(self, sid): self.snaps.pop(sid, None)
    async def put(self, rec): self.docs[rec["id"]] = dict(rec)
    async def get(self, sid): return dict(self.docs[sid]) if sid in self.docs else None
    async def update(self, sid, **fields):
        if sid in self.docs: self.docs[sid].update(fields)
    async def mark_lost(self, boot_id, now, resume=False): return 0           # nothing outlives the process, so there is nothing stale to find
    async def check(self): return "ok"


class FirestoreRegistry:
    kind = "firestore"
    persistent = True

    def __init__(self, sa, client, base=None, collection=COLLECTION):
        self.sa, self.client, self.collection = sa, client, collection
        self.project = sa["project_id"]; self.token_uri = sa.get("token_uri") or "https://oauth2.googleapis.com/token"
        self.base = (base or "https://firestore.googleapis.com").rstrip("/") + f"/v1/projects/{self.project}/databases/(default)/documents"
        self._tok, self._exp = None, 0

    async def _token(self):
        if self._tok and time.time() < self._exp - 60: return self._tok
        import jwt                                              # PyJWT + cryptography ship with the MCP dependency
        now = int(time.time())
        assertion = jwt.encode({"iss": self.sa["client_email"], "scope": "https://www.googleapis.com/auth/datastore", "aud": self.token_uri, "iat": now, "exp": now + 3600},
                               self.sa["private_key"], algorithm="RS256")
        r = await self.client.post(self.token_uri, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}, timeout=8)
        r.raise_for_status(); d = r.json(); self._tok, self._exp = d["access_token"], now + int(d.get("expires_in", 3600)); return self._tok

    async def _call(self, method, url, **kw):
        h = {"Authorization": "Bearer " + await self._token()}
        r = await self.client.request(method, url, headers=h, timeout=6, **kw)
        if _is_missing_document(r): return None
        if r.status_code >= 400: raise RuntimeError(describe_error(r))
        return r.json() if r.content else {}

    def _doc(self, sid): return f"{self.base}/{self.collection}/{sid}"
    def _snap(self, sid): return f"{self.base}/{self.collection}_snapshots/{sid}"

    async def list_owner(self, owner):
        """This account's sandboxes that can still be used (running or asleep)."""
        if not owner: return []
        body = {"structuredQuery": {"from": [{"collectionId": self.collection}], "limit": 50, "where": {"fieldFilter": {"field": {"fieldPath": "owner"}, "op": "EQUAL", "value": {"stringValue": owner}}}}}
        res = await self._call("POST", self.base + ":runQuery", json=body) or []
        recs = [{k: from_fs(v) for k, v in r["document"].get("fields", {}).items()} for r in res if r.get("document")]
        return [r for r in recs if r.get("state") in ("active", "sleeping")]

    async def put_snapshot(self, sid, rec):
        await self._call("PATCH", self._snap(sid), json={"fields": {k: to_fs(v) for k, v in rec.items()}})

    async def get_snapshot(self, sid):
        d = await self._call("GET", self._snap(sid))
        return None if d is None else {k: from_fs(v) for k, v in d.get("fields", {}).items()}

    async def delete_snapshot(self, sid): await self._call("DELETE", self._snap(sid))

    async def check(self):
        """Cheap startup probe: can we authenticate and read? Raises with Google's reason (API disabled, no database, missing role) if not."""
        await self._call("GET", self._doc("health-probe")); return "ok"

    async def put(self, rec):
        await self._call("PATCH", self._doc(rec["id"]), json={"fields": {k: to_fs(v) for k, v in rec.items()}})

    async def get(self, sid):
        d = await self._call("GET", self._doc(sid))
        return None if d is None else {k: from_fs(v) for k, v in d.get("fields", {}).items()}

    async def update(self, sid, **fields):
        q = "&".join(f"updateMask.fieldPaths={k}" for k in fields)
        await self._call("PATCH", self._doc(sid) + "?" + q, json={"fields": {k: to_fs(v) for k, v in fields.items()}})

    async def mark_lost(self, boot_id, now, resume=False):
        """Called once at startup: any sandbox still 'active' that belongs to an earlier run of the service died with that run.
        With resume, one that has a snapshot is not lost: it is asleep and wakes on its next request."""
        body = {"structuredQuery": {"from": [{"collectionId": self.collection}], "limit": 300,
                                    "where": {"fieldFilter": {"field": {"fieldPath": "state"}, "op": "EQUAL", "value": {"stringValue": "active"}}}}}
        res = await self._call("POST", self.base + ":runQuery", json=body) or []
        n = 0
        for row in res:
            doc = row.get("document")
            if not doc: continue
            f = {k: from_fs(v) for k, v in doc.get("fields", {}).items()}
            if f.get("boot_id") != boot_id and resume and f.get("resumable"):
                await self.update(f["id"], state="sleeping", reason="the server restarted; it wakes on its next request"); n += 1
            elif f.get("boot_id") != boot_id:
                await self.update(f["id"], state="lost", reason="the server restarted (free instances sleep after 15 idle minutes and are replaced on every deploy)", ended_at=int(now)); n += 1
        return n


def make_registry(client, env=None):
    env = os.environ if env is None else env
    raw = env.get("FIREBASE_SERVICE_ACCOUNT", "")
    if not raw and env.get("FIREBASE_SERVICE_ACCOUNT_FILE"):
        with open(env["FIREBASE_SERVICE_ACCOUNT_FILE"]) as f: raw = f.read()
    if not raw.strip(): return MemoryRegistry()
    sa = json.loads(raw)
    for k in ("project_id", "client_email", "private_key"):
        if not sa.get(k): raise ValueError(f"FIREBASE_SERVICE_ACCOUNT is missing {k!r}")
    return FirestoreRegistry(sa, client, env.get("FIRESTORE_BASE_URL") or None, env.get("SANDBOX_REGISTRY_COLLECTION") or COLLECTION)
