"""Backlight control via the brightnessctl CLI (v0.5 on this box: no JSON)."""
from __future__ import annotations

import re
import subprocess
from typing import Optional

_TIMEOUT = 3.0
# devices that are backlights for keyboards, not displays
_NON_DISPLAY = re.compile(r"kbd|keyboard|caps|scroll|numb", re.I)


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["brightnessctl", *args], capture_output=True,
                          text=True, timeout=_TIMEOUT)


def list_devices() -> list[str]:
    """Backlight-class device names via `brightnessctl -l -m` (CSV lines:
    name,class,current,percent,max)."""
    try:
        out = _run(["-l", "-m"]).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    devs = []
    for line in out.splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 2 and parts[1] == "backlight":
            devs.append(parts[0])
    return devs


def pick_device(want: str = "auto") -> Optional[str]:
    devs = list_devices()
    if not devs:
        return None
    if want != "auto":
        return want if want in devs else None
    display = [d for d in devs if not _NON_DISPLAY.search(d)]
    return display[0] if display else None


class Brightness:
    def __init__(self, device: str = "auto"):
        self.device = pick_device(device)
        self.available = self.device is not None
        self._min = 1.0
        self._max = 100.0

    def _raw(self) -> tuple[Optional[int], Optional[int]]:
        """(current, max) raw brightness values."""
        try:
            cur = int(_run(["-d", self.device, "get"]).stdout.strip())
            mx = int(_run(["-d", self.device, "max"]).stdout.strip())
            return cur, mx
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            return None, None

    def percent(self) -> Optional[float]:
        if not self.available:
            return None
        cur, mx = self._raw()
        if cur is None or not mx:
            return None
        return max(0.0, min(100.0, 100.0 * cur / mx))

    def set_percent(self, pct: float) -> tuple[bool, Optional[str]]:
        if not self.available:
            return False, "unavailable"
        pct = max(1.0, min(100.0, float(pct)))  # never allow blackout
        cur, mx = self._raw()
        if not mx:
            return False, "unavailable"
        target = max(1, round(mx * pct / 100.0))
        try:
            _run(["-d", self.device, "set", str(target)])
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False, "unavailable"
        got, _ = self._raw()
        if got is None:
            return False, "unavailable"
        return True, None

    def state(self) -> dict:
        return {
            "value": self.percent(),
            "min": self._min, "max": self._max,
            "available": self.available,
            "device": self.device,
        }
