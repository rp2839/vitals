#!/usr/bin/env bash
# User-level install: package -> ~/.local/lib/vitals, bins -> ~/.local/bin,
# systemd user units -> ~/.config/systemd/user. Does not touch waybar config.
set -eu
cd "$(dirname "$0")"

LIB="$HOME/.local/lib/vitals"
BIN="$HOME/.local/bin"
UNIT="$HOME/.config/systemd/user"

mkdir -p "$LIB" "$BIN" "$UNIT"
cp -r src/vitals "$LIB/"
for b in vitalsd vitals-bar vitalsctl vitals-panel; do
  sed "s#__VITALS_LIB__#$LIB#" install/bin-$b.py > "$BIN/$b"
  chmod 755 "$BIN/$b"
done
cp systemd/user/vitalsd.service systemd/user/vitals-panel.service "$UNIT/"

cat <<'EOF'
Installed. Next:
  systemctl --user daemon-reload
  systemctl --user enable --now vitalsd vitals-panel
Then merge waybar/vitals.jsonc + waybar/vitals.css into your waybar config
and reload waybar (killall -USR2 waybar).
EOF
