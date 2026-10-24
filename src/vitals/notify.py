"""Freedesktop notifications via notify-send (works with dunst/mako/swaync).

Episode logic: each event fires once per episode; episodes reset on the
opposite transition (with hysteresis), so no stacking or re-firing.
"""
from __future__ import annotations

import subprocess
from typing import Optional

APP = "Vitals"


def _send(urgency: str, title: str, body: str, icon: Optional[str] = None,
          timeout_ms: int = 10000) -> bool:
    args = ["notify-send", "-a", APP, "-u", urgency,
            "--hint", "string:x-canonical-private-synchronous:vitals-battery",
            "--hint", "string:x-dunst-stack-tag:vitals-battery",
            "-t", str(timeout_ms)]
    if icon:
        args += ["-i", icon]
    args += [title, body]
    try:
        subprocess.run(args, capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False  # never crash on notifications


class BatteryNotifier:
    def __init__(self, cfg: dict, battery_cfg: dict | None = None):
        self.cfg = cfg
        bc = battery_cfg or {}
        self.low_percent = bc.get("low_percent", 20)
        self.critical_percent = bc.get("critical_percent", 10)
        self.low_fired = False
        self.crit_fired = False
        self.last_ac: Optional[bool] = None
        self.last_status: Optional[str] = None
        self.started = False
        self.no_daemon_logged = False

    def update(self, state: dict) -> None:
        pct = state.get("percent")
        ac = state.get("ac_online")
        status = (state.get("status") or "").lower()
        t = state.get("time_to_empty_s")
        tstr = ""
        if t:
            h, m = int(t) // 3600, (int(t) % 3600) // 60
            tstr = f" ({h}:{m:02d} left)"
        if not self.started:
            self.started = True
            # seed episodes from the startup state: an already-low battery at
            # daemon start must not fire a notification (SPEC §4 Startup)
            if pct is not None and status == "discharging":
                self.low_fired = pct <= self.low_percent
                self.crit_fired = pct <= self.critical_percent
            self.last_ac, self.last_status = ac, status
            return
        if pct is None:
            return

        discharging = status == "discharging"
        # low / critical episodes (only while discharging)
        if not discharging or pct > self.low_percent + 3:
            self.low_fired = False
        if not discharging or pct > self.critical_percent + 3:
            self.crit_fired = False
        if discharging and pct <= self.low_percent and not self.low_fired:
            self.low_fired = True
            if not _send("normal", "Battery low",
                         f"Battery low — {pct:.0f}%{tstr}. Plug in soon.",
                         icon="battery-caution", timeout_ms=10000):
                self._log_no_daemon()
        if discharging and pct <= self.critical_percent and not self.crit_fired:
            self.crit_fired = True
            _send("critical", "Battery critical",
                  f"Battery critical — {pct:.0f}%{tstr}. Plug in now.",
                  icon="battery-low", timeout_ms=0)

        # AC transitions (flap protection: ignore if state flips back)
        if ac != self.last_ac and ac is not None:
            if ac and self.cfg.get("plugged"):
                w = state.get("power_w")
                _send("low", "AC connected",
                      f"AC connected — charging at {w:.0f} W" if w else "AC connected.",
                      timeout_ms=3000)
            elif (not ac) and self.cfg.get("unplugged"):
                _send("low", "Running on battery",
                      f"Running on battery — {pct:.0f}%{tstr}", timeout_ms=4000)
        # fully charged
        if (status == "full" and self.last_status != "full" and ac
                and self.cfg.get("full")):
            _send("low", "Battery full", "Battery full — you can unplug.",
                  icon="battery-full-charged", timeout_ms=6000)

        self.last_ac, self.last_status = ac, status

    def _log_no_daemon(self) -> None:
        if not self.no_daemon_logged:
            self.no_daemon_logged = True
            import sys
            print("vitals: notify-send failed; no notification daemon?",
                  file=sys.stderr)
