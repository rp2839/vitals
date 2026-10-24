#!/usr/bin/env bash
# Bounded end-to-end smoke check. Exits non-zero on any failure.
# Every step has a hard timeout so nothing can hang CI or a session.
set -u
cd "$(dirname "$0")/.."
fail() { echo "SMOKE FAIL: $1" >&2; kill $DPID 2>/dev/null; exit 1; }

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
SOCK="$XDG_RUNTIME_DIR/vitals/control.sock"

python3 bin/vitalsd >/tmp/vitals-smoke.log 2>&1 & DPID=$!
for i in $(seq 1 40); do [ -S "$SOCK" ] && break; sleep 0.25; done
[ -S "$SOCK" ] || fail "daemon did not create $SOCK"

timeout 10 ./bin/vitalsctl status            || fail "vitalsctl status"
timeout 10 ./bin/vitalsctl brightness get    || fail "brightness get"
timeout 10 ./bin/vitalsctl panel toggle      || fail "panel toggle"
timeout 10 ./bin/vitalsctl panel hide        || fail "panel hide"

# bar bridge must emit exactly one JSON object per line within 10s
timeout 10 python3 bin/vitals-bar > /tmp/vitals-bar-smoke.out || true
head -1 /tmp/vitals-bar-smoke.out | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); assert set(d) >= {'text','class'}" \
    || fail "bar output not single-line waybar JSON"

# protocol: controls.get over the socket
timeout 10 python3 - <<'EOF' || fail "controls.get"
import json, os, socket
s = socket.socket(socket.AF_UNIX); s.settimeout(5)
s.connect(os.environ["XDG_RUNTIME_DIR"] + "/vitals/control.sock")
s.sendall(b'{"v":1,"id":"c","op":"controls.get"}\n')
r = json.loads(s.recv(65536))
assert r["ok"] and "power.profile" in r["data"], r
EOF

kill $DPID 2>/dev/null
echo "SMOKE OK"
