# Team setup without Docker (Tailscale / VPN / trusted LAN)

**Hub owner** (one always-on machine):
```bash
./teambus-hub start                      # first run builds its own Python env (no sudo), then runs in the background
./teambus-hub add alice-frontend frontend
./teambus-hub add bob-backend backend    # each prints a one-line invite containing that person's token: send it privately
```
Other commands: `invite NAME`, `list`, `status`, `url` (web-view link), `logs`, `stop`. State lives in `~/.teambus-hub` (override with `TEAMBUS_HUB_DIR`).
The address in invites is `$TEAMBUS_HOST`, else the Tailscale IP, else the first LAN IP. Port: `TEAMBUS_PORT` (default 8765).

**Each teammate**: install TeamBus once (`curl -fsSL https://raw.githubusercontent.com/Beastburner/teambus/main/install.sh | sh`, see the README), then inside their own project paste the invite:
`teambus-join --hub http://100.x.y.z:8765 --agent-id alice-frontend --role frontend --token …`
It checks `python3` + `claude`, checks the hub is reachable, runs `teambus init` (token files are added to `.git/info/exclude`) and `teambus doctor`. Safe to re-run.

**Different networks?** Everyone needs to be on the same Tailscale tailnet (or other VPN), because the hub speaks plain HTTP and has no rate limiting: never port-forward it to the internet. For no-install teammates use the HTTPS deployment in `deploy.md`.
