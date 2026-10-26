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

from .. import config as config_mod
from ..collectors.bluetooth import classify as bt_classify

SOCK_ENV = "VITALS_SOCKET"

# Nerd Font (material) glyphs — codepoints verified against the literals in
# docs/SPEC.md §2.2 on this machine's gtk-layer-shell/nerd-font setup.
# Levels: battery_100..battery_0 = U+F0079..U+F0083.
_LEVELS = [chr(0xF0079 + i) for i in range(11)]        # index 0=100% .. 10=0%
ICON_FULL = chr(0xF0085)      # plug-battery (full)
ICON_PLUG = chr(0xF06A5)      # power plug (held / no battery)
ICON_UNKNOWN = chr(0xF0091)   # battery unknown
ICON_ALERT = chr(0xF00BA)     # battery alert (low / critical)
ICON_CHARGING = chr(0xF0084)  # battery charging

_ASCII = {"discharging": "BAT", "charging": "AC^", "full": "AC=", "held": "AC-",
          "low": "BAT!", "critical": "BAT!!", "nobattery": "AC",
          "unknown": "--"}


def _level_icon(p: float) -> str:
    step = min(100, max(0, int(round(p / 10.0) * 10)))
    return _LEVELS[(100 - step) // 10]


def icon_for(state: str, percent) -> str:
    if os.environ.get("VITALS_ASCII"):
        return _ASCII.get(state, "--")
    if state in ("held", "nobattery"):
        return ICON_PLUG
    if state == "full":
        return ICON_FULL
    if state in ("low", "critical"):
        return ICON_ALERT
    if state == "unknown":
        return ICON_UNKNOWN
    if state == "charging":
        return ICON_CHARGING
    p = 0 if percent is None else float(percent)
    return _level_icon(p)


def fmt_time(seconds) -> str | None:
    """SPEC §2.3 format: H:MM (0:41), 1d 2h above 24h, <1m."""
    if seconds is None:
        return None
    if seconds < 60:
        return "<1m"
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    if h >= 24:
        return f"{h // 24}d {h % 24}h"
    return f"{h}:{m:02d}"


def render(battery: dict, power: dict) -> dict:
    state = battery.get("state") or "unknown"
    pct = battery.get("percent")
    icon = icon_for(state, pct)
    parts: list[str] = [icon]
    text_pct = "--" if pct is None else f"{pct:.0f}%"
    if state == "nobattery":
        parts = [icon, "AC"]
    else:
        parts.append(text_pct)
    # time
    if state in ("discharging", "low", "critical"):
        t = fmt_time(battery.get("time_to_empty_s"))
        parts.append(f"· {t}" if t else "· …")
    elif state == "charging":
        t = fmt_time(battery.get("time_to_full_s"))
        parts.append(f"· {t}" if t else "· …")
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
    return {"text": f"{icon_for('unknown', None) if not os.environ.get('VITALS_ASCII') else '--'} --",
            "tooltip": msg, "class": ["unknown"], "alt": "unknown",
            "percentage": 0}


def render_battery(state: dict, _cfg=None) -> dict:
    return render(state.get("battery", {}), state.get("power", {}))


# Bluetooth glyphs (Nerd Font material): bluetooth, -audio, -connect, -off,
# -transfer = U+F00AF..U+F00B4 (SPEC §8.1)
BT_ON = chr(0xF00AF)
BT_AUDIO = chr(0xF00B0)
BT_CONNECTED = chr(0xF00B1)
BT_OFF = chr(0xF00B2)
_BT_ASCII = {"on": "BT", "connected": "BT+", "off": "BT-", "blocked": "BT-",
             "noadapter": "BT-", "unknown": "BT?"}


def render_bluetooth(state: dict, cfg: dict | None = None) -> dict:
    """SPEC §8.1 indicator. cfg: the [bluetooth] config section."""
    cfg = cfg if cfg is not None else config_mod.DEFAULTS["bluetooth"]
    bt = state.get("bluetooth") or {}
    low = int(cfg.get("low_battery_percent", 15))
    st, classes = bt_classify(bt, low)
    connected = [d for d in bt.get("devices") or [] if d.get("connected")]
    if os.environ.get("VITALS_ASCII"):
        icon = _BT_ASCII.get(st, "BT?")
    elif st == "connected":
        icon = BT_AUDIO if "audio" in classes else BT_CONNECTED
    elif st == "on":
        icon = BT_ON
    else:
        icon = BT_OFF
    parts = [icon]
    if st == "blocked":
        parts.append("blocked")
    elif st == "unknown":
        parts.append("--")
    if len(connected) >= 2:
        parts.append(str(len(connected)))
    levels = [d["battery"] for d in connected if d.get("battery") is not None]
    if levels and (cfg.get("show_battery") or "lowbattery" in classes):
        parts.append(f"· {min(levels)}%")
    text = " ".join(parts)
    if st == "noadapter" and cfg.get("hide_when_no_adapter"):
        text = ""

    ad = bt.get("adapter") or {}
    head = {"on": "On", "connected": "On", "off": "Off",
            "blocked": "Blocked by hardware switch", "noadapter": "No adapter",
            "unknown": "Bluetooth unavailable"}[st]
    lines = [f"{ad['alias']} · {head}" if ad.get("alias") else f"Bluetooth · {head}"]
    for d in connected:
        bat = f" · {d['battery']}%" if d.get("battery") is not None else ""
        lines.append(f"{d.get('alias')}{bat}")
    return {"text": text, "tooltip": "\n".join(lines), "class": classes,
            "alt": st, "percentage": min(levels) if levels else 0}


def _unknown_bluetooth(msg: str) -> dict:
    out = render_bluetooth({})
    out["tooltip"] = msg
    return out


# bar module name -> (daemon modules it subscribes to, renderer taking
# (state, config section or None), output while the daemon is unreachable)
MODULES = {
    "battery": (("battery", "power"), render_battery, lambda m: _unknown(m)),
    "bluetooth": (("bluetooth",), render_bluetooth, _unknown_bluetooth),
}


def session(sock: socket.socket, module: str = "battery",
            cfg: dict | None = None) -> None:
    mods, render_fn, _ = MODULES[module]
    buf = b""
    sock.sendall(json.dumps({"v": 1, "id": "s1", "op": "subscribe",
                             "modules": list(mods)}).encode() + b"\n")
    last_line = None
    state: dict[str, dict] = {m: {} for m in mods}
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
            for m in mods:
                if m in data_part:
                    state[m].update(data_part[m])
            if msg.get("ok") or msg.get("event") == "update":
                out = render_fn(state, cfg)
                js = json.dumps(out, separators=(",", ":"))
                if js != last_line:
                    last_line = js
                    sys.stdout.write(js + "\n")
                    sys.stdout.flush()


def main() -> int:
    module = sys.argv[1] if len(sys.argv) > 1 else "battery"
    if module not in MODULES:
        print(f"vitals-bar: unknown module {module!r} "
              f"(known: {', '.join(MODULES)})", file=sys.stderr)
        return 2
    unknown = MODULES[module][2]
    cfg = config_mod.load().get_section(module)
    backoff = 1.0
    while True:
        try:
            sock = connect()
            backoff = 1.0
            try:
                session(sock, module, cfg)
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
            _emit(unknown("vitalsd: connection lost"))
        except OSError:
            _emit(unknown("vitalsd: not running"))
        time.sleep(backoff)
        backoff = min(backoff * 2, 10.0)


if __name__ == "__main__":
    sys.exit(main())
