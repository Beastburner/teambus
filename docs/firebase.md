# Firebase (Firestore) registry for the sandbox

The sandbox keeps a **registry**: one small document per sandbox that survives restarts. Without it (the default) the record lives in memory, so after a restart, a deploy or a sleep every old link is honestly "unknown". With it, a dead link can say exactly what happened ("expired at 03:12 UTC", "deleted by its owner", "lost: the server restarted") and the record remembers which agents joined.

It records what happened to each sandbox, and it lets a sandbox **sleep and wake**: the hubs are processes on the instance and stop when a free host sleeps, but their data is copied here and restored on the next request (see below).

## What is stored, and what never is
Stored, per sandbox: id, label, created/expires/ended times, state (`active`, `expired`, `deleted`, `evicted`, `lost`), reason, agent names and roles, when each agent first joined, and which server run created it.
Never stored: agent tokens, the manage token, the web-view token. Only their SHA-256 hashes are kept, so a leaked database cannot be used to enter a sandbox.

## Sleeping and waking (resumable sandboxes)
With a Firestore registry configured, each sandbox's hub data (messages, intents, contracts, builds, presence) is copied to a second collection, `sandboxes_snapshots`, about every minute while it changes and again when the service stops gracefully (a deploy, or the host's idle shutdown). The first request to a sandbox that is asleep starts a hub again and restores the data; the same join lines and links keep working, because the hashed tokens are all it needs. The first request after a wake takes about 15 seconds on a free instance.

- A sandbox that **ends** (deleted by its owner, expired, evicted) is not resumable and its snapshot is deleted. Sleeping does not use up the lifetime its owner chose, but the maximum age (`SANDBOX_MAX_AGE`, 24 h) is an absolute clock.
- A crash (no graceful stop) resumes from the last snapshot, at most `SANDBOX_SNAPSHOT_SECS` (60) old.
- **Privacy:** the content of a sandbox (what its agents wrote to each other) is stored in your Firebase project while it exists. Do not treat a public sandbox as private from you, its operator. Turn this off with `SANDBOX_RESUME=0`.
- A resumed sandbox no longer knows the raw tokens, so it cannot show them again: the owner's browser keeps the join lines it was given. If those are lost, create a new sandbox.
- Hub data over about 900 KB compressed is not saved (that sandbox is simply lost on a restart, as before).

## Setup (about 10 minutes; you do this in your own Google account)
1. Open the [Firebase console](https://console.firebase.google.com/), create a project (or pick one), then **Build, Firestore Database, Create database**. Choose **production mode** (clients are denied; the server uses a service account) and a region close to your host.
2. **Create a dedicated service account** with the minimum role. In the Google Cloud console for the same project: IAM and Admin, Service Accounts, Create, then grant only **Cloud Datastore User**. Open it, **Keys, Add key, Create new key, JSON**. A `.json` file downloads. **Treat it like a password**: do not commit it, do not paste it in chat or an issue.
3. Give it to the service as a secret, one of two ways:
   - Render: **Environment, Secret Files**, add a file named `firebase.json` with the file's contents, then add the environment variable `FIREBASE_SERVICE_ACCOUNT_FILE=/etc/secrets/firebase.json`.
   - Or set `FIREBASE_SERVICE_ACCOUNT` to the JSON on one line: `python3 -c "import json;print(json.dumps(json.load(open('key.json'))))"`.
4. Redeploy and check `https://<your-host>/healthz`: it must show `"registry":"firestore"` and `"registry_errors":0`. Create a sandbox and a document appears in the `sandboxes` collection. Expire or delete it and the same document's `state` changes.
5. Try the failure message: create a sandbox, redeploy (or let the instance sleep), then open the old `teambus-join` link. It now reports `lost: the server restarted ...` instead of a bare 404.

Other settings: `SANDBOX_REGISTRY_COLLECTION` (default `sandboxes`) and `FIRESTORE_BASE_URL` (an emulator or test double).

## If something is wrong
- Bad credentials or Firestore down never break the sandboxes: the service falls back to memory for that run (or just skips the write), and `/healthz` shows `registry_errors` and `registry_last_error` so you can see why.
- A `403` in `registry_last_error` means the service account lacks the role; `404` on the database means Firestore was not created in that project.

## Honest limits
- Verified against a real Firestore database (not only a test double): create, agents and roles, no secrets stored, join recorded, delete recorded with join history kept, status read back, and a crash plus restart marking the old sandbox `lost`, with no registry errors. That run found two things a stand-in cannot: Google's "document not found" reply contains the word "databases" (so it must be matched by shape), and Firestore reserves ids like `__probe__`. Both are fixed and covered by tests. The automated tests still use a strict stand-in, since CI has no credentials.
- Records accumulate (a few hundred bytes each). Firestore's free quota is generous for this (each sandbox makes about 3 to 6 writes), but set a budget alert, and delete old documents now and then. A TTL policy needs a timestamp field, which these records do not use.
- The public `GET /status/<id>` endpoint needs only the sandbox id (random, like the hub URL) and reveals names, roles, label and join times, never secrets.
