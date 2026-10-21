# Accounts: sign in to find, wake and re-key your sandboxes

Without an account, the only key to a sandbox is what the creating browser stored (its manage token and join lines). Clear that browser, switch devices, and the sandbox is orphaned. With **Sign in with Google** the sandbox belongs to you:

- **My sandboxes** lists the ones tied to your account from any browser, running or asleep.
- **Opening one wakes it** and gives you its web-view link.
- **Get new join lines / Reissue** makes fresh tokens for every agent (the old join lines stop working) and a new manage token. A woken sandbox only keeps hashes of its tokens, so this is how you get join lines again.
- **Extend, add an agent and delete** work without the manage token.
- **Add to my account** claims an anonymous sandbox you still hold the manage token for.

Anonymous use keeps working exactly as before; sign-in is optional and only appears when the server has a client id.

## What is stored
Only a one-way owner id (a SHA-256 of your Google account id). Your Google id and email are **not** stored on the server or in Firestore; the email lives only in the signed login cookie in your own browser. Sandboxes record their owner id, so listing "mine" is a lookup by that id.

## Set up (you do this in your own Google account; about 10 minutes)
1. In the Google Cloud console, in the project you already use for Firebase: **APIs & Services > OAuth consent screen**: choose *External*, give the app a name and your email. While it is in *Testing* only the test users you add can sign in; publish it for anyone.
2. **APIs & Services > Credentials > Create credentials > OAuth client ID > Web application.** Under *Authorized JavaScript origins* add your sandbox's origin, for example `https://teambus.onrender.com` (and `http://localhost:8780` to try it locally). No redirect URI is needed.
3. Copy the **Client ID** (ends in `.apps.googleusercontent.com`). It is not a secret and there is no client secret to configure: the server only checks Google's signature on ID tokens.
4. Put it in the service's environment as `GOOGLE_CLIENT_ID` and redeploy.
(I could not click through the console while writing this, so button names may differ slightly.)

Check: `GET /healthz` shows `"sign_in": true`, and the dialog shows a *Sign in with Google* button.

## How it works, and how it is protected
- The page asks Google for an ID token. The server verifies it: RS256 signature against Google's published keys, audience = your client id, issuer = Google, not expired, **email verified**. Anything else (wrong audience, another key, `alg: none`, expired, unverified email) is refused.
- It then sets its own session cookie: signed (HMAC-SHA256) with the expiry inside the signed value, `HttpOnly`, `SameSite=Lax`, `Secure` on https, 30 days. A tampered cookie is just no cookie.
- Anything that **changes** state with the cookie (create in your account, delete, extend, reissue, claim, sign in) also needs the `X-TeamBus: 1` header. A cross-site form or image cannot send it, so another website cannot act as you. Without the header, a create request is treated as anonymous.
- The signing key is derived from your Firebase service-account key, so logins survive restarts; without Firebase set `SANDBOX_SESSION_SECRET`, or logins last until the next restart.
- Accounts keep a sandbox's *discoverability* across restarts only with the Firestore registry and resume (see [firebase.md](firebase.md)); with the in-memory registry a restart still ends every sandbox.

## Limits
- Google is the only provider. No passwords, no email links.
- The Google sign-in button loads Google's script (`accounts.google.com`) only when you open the dialog, never on page load.
- Per-visitor limits are still per IP; there is no per-account quota yet.
