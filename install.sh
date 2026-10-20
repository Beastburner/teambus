#!/bin/sh
# Install TeamBus on this machine (Linux or macOS; Windows via WSL).
#   curl -fsSL https://raw.githubusercontent.com/Beastburner/teambus/main/install.sh | sh
# Clones the repo to ~/.teambus (override: TEAMBUS_HOME) and links `teambus`, `teambus-join` and `teambus-hub` into ~/.local/bin (override: TEAMBUS_BIN).
# Run it again any time to update. Other settings: TEAMBUS_REPO (git URL), TEAMBUS_REF (branch or tag, default main).
set -eu
REPO="${TEAMBUS_REPO:-https://github.com/Beastburner/teambus.git}"; REF="${TEAMBUS_REF:-main}"
DIR="${TEAMBUS_HOME:-$HOME/.teambus}"; BIN="${TEAMBUS_BIN:-$HOME/.local/bin}"
say() { printf '%s\n' "$*"; }; die() { printf 'teambus install: %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || die "git is required (https://git-scm.com/downloads)"
command -v python3 >/dev/null 2>&1 || die "python3 is required (3.9 or newer)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die "python3 3.9 or newer is required (found $(python3 -V 2>&1))"
case "$(uname -s)" in Linux|Darwin) ;; *) die "Linux or macOS only (on Windows, use WSL)";; esac

if [ -d "$DIR/.git" ]; then
  say "Updating $DIR ..."; git -C "$DIR" fetch --quiet --depth 1 origin "$REF" && git -C "$DIR" reset --quiet --hard FETCH_HEAD
elif [ -e "$DIR" ]; then die "$DIR exists and is not a TeamBus checkout; set TEAMBUS_HOME to another folder"
else say "Installing into $DIR ..."; git clone --quiet --depth 1 --branch "$REF" "$REPO" "$DIR"; fi

mkdir -p "$BIN"
for c in teambus teambus-join teambus-hub; do chmod +x "$DIR/$c"; ln -sf "$DIR/$c" "$BIN/$c"; done
say "Linked teambus, teambus-join and teambus-hub into $BIN"

case ":$PATH:" in *":$BIN:"*) ;; *)
  say ""; say "$BIN is not on your PATH yet. Add this line to ~/.bashrc or ~/.zshrc, then open a new terminal:"
  say "  export PATH=\"$BIN:\$PATH\"" ;; esac
command -v claude >/dev/null 2>&1 || { say ""; say "Note: the Claude Code CLI ('claude') was not found; teambus-join needs it (https://claude.com/claude-code)."; }

say ""; say "Done. Next:"
say "  - Join a hub: run the one-line 'teambus-join --hub ...' command you were given, inside your project folder."
say "  - Or run your own hub for your team: teambus-hub start     (see $DIR/docs/team-setup.md)"
say "  - Update later: run this installer again.   Remove: rm -rf $DIR $BIN/teambus $BIN/teambus-join $BIN/teambus-hub"
