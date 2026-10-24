"""vitals-bar — waybar custom-module bridge.

Streams one waybar JSON object per line on stdout (return-type: json idiom).
Reconnects to the daemon with bounded backoff and emits an `unknown` state
while the daemon is unreachable.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time

SOCK_ENV = "VITALS_SOCKET"

# Nerd Font (material) battery glyphs, codepoints F0783..F0798
_BATT = [chr(0xF0783 + (100 - p) // 10) for p in range(100, -1, -10)]  # 100..0
_CHRG = [chr(0xF078E + (100 - p) // 10) for p in range(100, -1, -10)]
ICON_ALERT = chr(0xF07BA)      # battery_alert
ICON_UNKNOWN = chr(0xF0791)    # battery_unknown (approx)
ICON_PLUG = chr(0xF0BA5)       # power plug
ICON_FULL = chr(0xF078E)       # charging/full glyph

_ASCII = {"discharging": "BAT", "charging": "AC^", "full": "AC=", "held": "AC-",
          "low": "BAT!", "critical": "BAT!!", "nobattery": "AC",
          "unknown": "--"}


def _icon(state: str, percent) -> str:
    if os.environ.get("VITALS_ASCII"):
        return _ASCII.get(state, "--")
    p = 0 if percent is None else int(percent)
    step = min(10, max(0, (p + 5) // 10)) * 10
    if state in ("charging",):
        return _CHRG[(100 - step) // 10]
    if state == "full":
        return ICON_FULL
    if state in ("low", "critical"):
        return ICON_ALERT
    if state == "nobattery":
        return ICON_PLUG
    if state == "unknown":
        return ICON_UNKNOWN
    return _BATT[(100 - step) // 10]


def fmt_time(seconds) -> str | None:
    if seconds is None:
        return None
    if seconds < 60:
        return "<1m"
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    if h >= 24:
        return f"{h // 24}d {h % 24}h"
    if h == 0:
        return f"{m}m"
    return f"{h}:{m:02d}"


def render(battery: dict, power: dict) -> dict:
    state = battery.get("state") or "unknown"
    pct = battery.get("percent")
    icon = _icon(state, pct)
    parts: list[str] = [icon]
    text_pct = "--" if pct is None else f"{pct:.0f}%"
    if state == "nobattery":
        parts = [icon]
    else:
        parts.append(text_pct)
    # time
    if state in ("discharging", "low", "critical"):
        t = fmt_time(battery.get("time_to_empty_s"))
        if t:
            parts.append(f"· {t}")
    elif state == "charging":
        t = fmt_time(battery.get("time_to_full_s"))
        if t:
            parts.append(f"· {t}")
    elif state == "held":
        parts.append("· held")
    # optional watts (VITALS_BAR_WATTS=1)
    if os.environ.get("VITALS_BAR_WATTS") and battery.get("power_w") is not None \
            and state not in ("full", "nobattery"):
        parts.append(f"· {battery['power_w']:.1f}W")

    # tooltip
    lines = []
    status = battery.get("status")
    if status:
        head = f"{status}"
        if pct is not None:
            head += f" · {pct:.0f}%"
        lines.append(head)
    if battery.get("power_w") is not None:
        src = "Battery" if state in ("discharging", "low", "critical") else "AC"
        lines.append(f"{battery['power_w']:.1f} W ({src})")
    t = fmt_time(battery.get("time_to_empty_s") or battery.get("time_to_full_s"))
    if t:
        lines.append(f"{'remaining' if state != 'charging' else 'to full'}: {t}")
    if power.get("active_profile"):
        lines.append(f"Profile: {power['active_profile']}")
    if battery.get("health_percent") is not None:
        lines.append(f"Health {battery['health_percent']:.0f}%")

    classes = [state]
    if state in ("low", "critical"):
        classes.append("warning" if state == "low" else "critical")
    return {
        "text": " ".join(parts),
        "tooltip": "\n".join(lines) or "Vitals: battery unavailable",
        "class": classes,
        "alt": state,
        "percentage": int(pct) if pct is not None else 0,
    }


def connect() -> socket.socket:
    path = os.environ.get(SOCK_ENV) or os.path.join(
        os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
        "vitals", "control.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(path)
    s.settimeout(None)
    return s


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _unknown(msg: str) -> dict:
    return {"text": f"{_icon('unknown', None) if not os.environ.get('VITALS_ASCII') else '--'} --",
            "tooltip": msg, "class": ["unknown"], "alt": "unknown",
            "percentage": 0}


def session(sock: socket.socket) -> None:
    buf = b""
    sock.sendall(json.dumps({"v": 1, "id": "s1", "op": "subscribe",
                             "modules": ["battery", "power"]}).encode() + b"\n")
    last_line = None
    battery, power = {}, {}
    while True:
        data = sock.recv(8192)
        if not data:
            raise ConnectionError("daemon closed connection")
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            data_part = msg.get("data") or {}
            if "battery" in data_part:
                battery.update(data_part["battery"])
            if "power" in data_part:
                power.update(data_part["power"])
            if msg.get("ok") or msg.get("event") == "update":
                out = render(battery, power)
                js = json.dumps(out, separators=(",", ":"))
                if js != last_line:
                    last_line = js
                    sys.stdout.write(js + "\n")
                    sys.stdout.flush()


def main() -> int:
    backoff = 1.0
    while True:
        try:
            sock = connect()
            backoff = 1.0
            try:
                session(sock)
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
            _emit(_unknown("vitalsd: connection lost"))
        except OSError:
            _emit(_unknown("vitalsd: not running"))
        time.sleep(backoff)
        backoff = min(backoff * 2, 10.0)


if __name__ == "__main__":
    sys.exit(main())
