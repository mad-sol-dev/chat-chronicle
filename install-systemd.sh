#!/usr/bin/env bash
# Installs the systemd user units for this checkout: the web interface and the hourly backup.
#
#   ./install-systemd.sh            write the units, enable and start them
#   ./install-systemd.sh --remove   stop, disable and delete them
#
# The units are written (not linked) to ~/.config/systemd/user with this checkout's path and the
# python3 found in PATH filled in. CHRONICLE_ARCHIVE, CHRONICLE_INDEX and CHRONICLE_USER, if set
# when this runs, are written into the units too. Run it again after moving the checkout.
set -euo pipefail

units=(chat-chronicle.service chat-chronicle-backup.service chat-chronicle-backup.timer)
dest="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if [ "${1:-}" = "--remove" ]; then
  systemctl --user disable --now chat-chronicle.service chat-chronicle-backup.timer 2>/dev/null || true
  for u in "${units[@]}"; do rm -f "$dest/$u"; done
  systemctl --user daemon-reload
  echo "Units removed. The archive is left as it is."
  exit 0
fi

checkout="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python="$(command -v python3 || true)"
if [ -z "$python" ]; then
  echo "python3 not found in PATH." >&2
  exit 1
fi
if ! "$python" - <<'EOF'
import sqlite3, sys
if sys.version_info < (3, 10):
    sys.exit("Python 3.10 or newer is needed, found %s." % sys.version.split()[0])
try:
    sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
except sqlite3.OperationalError:
    sys.exit("This Python's SQLite has no FTS5.")
EOF
then
  exit 1
fi

env_lines=""
for v in CHRONICLE_ARCHIVE CHRONICLE_INDEX CHRONICLE_USER; do
  if [ -n "${!v:-}" ]; then
    env_lines+="Environment=\"$v=${!v}\""$'\n'
  fi
done

mkdir -p "$dest"
for u in "${units[@]}"; do
  ENV_LINES="$env_lines" awk -v checkout="$checkout" -v python="$python" '
    /^#@ENV@$/ { printf "%s", ENVIRON["ENV_LINES"]; next }
    { gsub(/@CHECKOUT@/, checkout); gsub(/@PYTHON@/, python); print }
  ' "$checkout/systemd/$u" > "$dest/$u"
done

systemctl --user daemon-reload
systemctl --user enable --now chat-chronicle.service chat-chronicle-backup.timer
echo "Installed to $dest:"
printf '  %s\n' "${units[@]}"
echo "Web interface: http://127.0.0.1:5120/"
