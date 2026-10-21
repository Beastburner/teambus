#!/bin/sh
# Chooses what this image runs.
#   default                -> the hub (server.py); it refuses to start without auth configured
#   TEAMBUS_MODE=sandbox   -> the public sandbox (sandbox.py)
#   on Render (RENDER is set by the platform) with no hub auth configured -> the sandbox, with defaults sized for a free instance
#                             (512 MB / 0.1 CPU). Anything you set in the dashboard overrides these.
mode="${TEAMBUS_MODE:-}"
if [ -z "$mode" ]; then
  if [ -n "$RENDER" ] && [ -z "$TEAMBUS_TOKEN" ] && [ ! -f "${TEAMBUS_TOKENS:-/data/tokens.json}" ] && [ "$TEAMBUS_NOAUTH" != "1" ]; then mode=sandbox; else mode=hub; fi
  if [ "$mode" = sandbox ]; then
    if [ -n "$FIREBASE_SERVICE_ACCOUNT" ] || [ -n "$FIREBASE_SERVICE_ACCOUNT_FILE" ]; then
      notice="Free hosting: the server sleeps after about 15 idle minutes. Your sandbox is saved and wakes on its next request; the first one takes about 15 seconds."
    else
      notice="Free hosting: the server sleeps after about 15 idle minutes and every sandbox is wiped when it does, and on each deploy. Create a new one if a link stops working."
    fi
    export SANDBOX_TRUST_PROXY="${SANDBOX_TRUST_PROXY:-1}" SANDBOX_WARM="${SANDBOX_WARM:-1}" SANDBOX_START_TIMEOUT="${SANDBOX_START_TIMEOUT:-90}" \
           SANDBOX_MAX="${SANDBOX_MAX:-8}" SANDBOX_TTL="${SANDBOX_TTL:-1800}" SANDBOX_NEW_PER_IP_HOUR="${SANDBOX_NEW_PER_IP_HOUR:-6}" SANDBOX_MAX_ACTIVE_PER_IP="${SANDBOX_MAX_ACTIVE_PER_IP:-3}" SANDBOX_DEBUG_IP="${SANDBOX_DEBUG_IP:-0}" \
           SANDBOX_NOTICE="${SANDBOX_NOTICE-$notice}" \
           SANDBOX_CLIENT_IP_HEADER="${SANDBOX_CLIENT_IP_HEADER:-cf-connecting-ip}" SANDBOX_TRUST_PROXY_HOPS="${SANDBOX_TRUST_PROXY_HOPS:-3}"
    echo "start.sh: running on Render with no hub auth configured, so starting the public sandbox (set TEAMBUS_MODE=hub to override)"
  fi
fi
case "$mode" in
  sandbox) exec python sandbox.py ;;
  hub)     exec python server.py ;;
  *)       echo "unknown TEAMBUS_MODE='$mode' (use hub or sandbox)" >&2; exit 2 ;;
esac
