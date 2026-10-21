"""Accounts for the sandbox service: Google sign-in, nothing stored but a pseudonymous owner id.

  Verifier   checks a Google ID token (RS256 signature against Google's published keys, audience = your OAuth client id, issuer, expiry, verified email)
  Sessions   our own signed session value for a cookie (HMAC-SHA256, expiry inside the signed payload), so a browser stays signed in for days without re-asking Google
  owner_id   sub -> a one-way id. The Google account id and email are never stored; the email only lives in the signed cookie, in the user's own browser.
"""
import base64, hashlib, hmac, json, os, secrets, time
import jwt

ISSUERS = {"https://accounts.google.com", "accounts.google.com"}


def owner_id(sub): return hashlib.sha256(("tb-owner:" + sub).encode()).hexdigest()[:32]


GOOGLE_JWKS = "https://www.googleapis.com/oauth2/v3/certs"
FIREBASE_JWKS = "https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com"


class Verifier:
    """Two ways to prove who someone is, both ending in the same signed session:
      mode "google"    a Google ID token for your OAuth client id (GOOGLE_CLIENT_ID)
      mode "firebase"  a Firebase Authentication ID token for your Firebase project, signed in with the Google provider (uses the project's own authorised domains)"""
    def __init__(self, client_id="", jwks_url="", project=""):
        self.mode = "firebase" if project else "google" if client_id else ""
        self.audience = project or client_id
        self.issuers = {f"https://securetoken.google.com/{project}"} if project else ISSUERS
        self.jwks_url = jwks_url or (FIREBASE_JWKS if project else GOOGLE_JWKS); self._client = None

    @property
    def enabled(self): return bool(self.mode)

    def verify(self, token):
        """The token's claims if it is a valid, unexpired ID token for this app with a verified email; else None. Blocking: run it in a thread."""
        if not self.enabled or not isinstance(token, str) or token.count(".") != 2 or len(token) > 4096: return None
        try:
            self._client = self._client or jwt.PyJWKClient(self.jwks_url, cache_keys=True, lifespan=3600, timeout=6)
            key = self._client.get_signing_key_from_jwt(token).key
            c = jwt.decode(token, key, algorithms=["RS256"], audience=self.audience, leeway=60, options={"require": ["exp", "iat", "sub", "aud", "iss"]})
        except Exception: return None
        if c.get("iss") not in self.issuers or not c.get("email_verified") or not c.get("sub"): return None
        if self.mode == "firebase" and (c.get("firebase") or {}).get("sign_in_provider") != "google.com": return None     # no anonymous, phone or password sign-ins
        return c


class Sessions:
    def __init__(self, key, days=30): self.key, self.ttl = key, days * 86400

    def make(self, owner, email=""):
        body = base64.urlsafe_b64encode(json.dumps({"o": owner, "e": email[:120], "x": int(time.time()) + self.ttl}, separators=(",", ":")).encode()).decode().rstrip("=")
        return body + "." + hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()

    def read(self, value):
        """{'owner', 'email'} for a valid unexpired session value, else None."""
        try:
            body, sig = value.rsplit(".", 1)
            if not hmac.compare_digest(sig, hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()): return None
            d = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            return {"owner": d["o"], "email": d.get("e", "")} if d["x"] > time.time() else None
        except Exception: return None


def session_key(secret="", service_account_key=""):
    """A stable signing key (so a restart does not sign everyone out): the explicit secret, else derived from the service-account key; else random for this run."""
    seed = secret or service_account_key
    return hashlib.sha256(("tb-session:" + seed).encode()).digest() if seed else secrets.token_bytes(32)
