# Contributing

## Set up
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
pytest -q test_teambus.py test_sandbox.py      # about 40 s; both suites start real server processes
```
`python3 -m venv` can fail on Debian/Ubuntu without `python3-venv`; `teambus-hub` shows a workaround that needs no sudo.

## Layout
`server.py` hub · `hooks/hook.py` Claude Code hooks · `teambus` CLI · `teambus-hub` / `teambus-join` easy setup · `sandbox.py` public sandbox · `web/` dashboard and landing page · `docs/` guides. See `ARCHITECTURE.md`.

## Guidelines
- Test what you change against the real thing: the tests start a real hub or sandbox, and there are no mocks of the server. Add a test with every behaviour change.
- The dashboard has a browser test: `pip install playwright && playwright install chromium`, start a hub, then `python test_webview.py URL VIEWER_TOKEN ALICE_TOKEN BOB_TOKEN`.
- Anything that renders text from the hub uses `textContent`, never `innerHTML`.
- Keep the vendored libraries in `web/assets/` as they are and update `THIRD-PARTY.txt` if you add one.
- Do not commit tokens, databases, `.claude/` settings or session recordings (`.gitignore` covers them).

## Pull requests
Describe what changed and how you verified it. CI runs the tests on Python 3.12 and 3.13 and builds the Docker image.
