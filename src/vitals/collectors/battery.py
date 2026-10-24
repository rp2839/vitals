"""Battery collection & estimation.

`BatteryEstimator` is pure Python (no GLib) so it is unit-testable: feed it
BatterySnapshots with a monotonic clock and it produces the public battery
state dict used by the daemon protocol.
"""
from __future__ import annotations

import math
import time
from collections import deque
from typing import Optional

from ..linux.sysfs import BatterySnapshot

# display EMA time constant (s) for the power readout
DISPLAY_TAU = 30.0
# estimate EMA time constant (s) for time-to-empty/full
ESTIMATE_TAU = 60.0
# derived-power window (s)
DERIVED_WINDOW = 120.0
# minimum span (s) of samples before a derived slope is trusted
DERIVED_MIN_SPAN = 45.0
# a gap larger than this between samples implies suspend/loss: drop the window
SUSPEND_GAP_S = 30.0
# seconds after a state transition during which estimates are suppressed
SUPPRESS_AFTER_TRANSITION = 15.0
# max plausible estimate (s)
MAX_ESTIMATE_S = 99 * 3600


def classify_state(status: Optional[str], ac_online: Optional[bool],
                   percent: Optional[float], low_percent: float = 20,
                   critical_percent: float = 10) -> str:
    """Map to the spec's visual states (SPEC.md 2.2)."""
    if status is None:
        return "unknown"
    s = status.lower()
    plugged = bool(ac_online)
    if s == "discharging":
        if percent is not None and percent <= critical_percent:
            return "critical"
        if percent is not None and percent <= low_percent:
            return "low"
        return "discharging"
    if s == "charging":
        return "charging"
    if s == "full":
        return "full"
    # "not charging" and variants
    if plugged:
        if percent is not None and percent >= 95:
            return "full"
        return "held"
    return "unknown"


def fmt_time_remaining(seconds: Optional[float]) -> Optional[str]:
    """H:MM, 1d 2h above 24h, '<1m' under a minute, None passthrough."""
    if seconds is None:
        return None
    if seconds < 60:
        return "<1m"
    hours = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    if hours >= 24:
        return f"{hours // 24}d {hours % 24}h"
    return f"{hours}:{mins:02d}"


class BatteryEstimator:
    """Stateful estimator. Call feed() with each fresh snapshot."""

    def __init__(self, cfg: dict | None = None,
                 mono: callable = time.monotonic):
        cfg = cfg or {}
        self.low_percent = cfg.get("low_percent", 20)
        self.critical_percent = cfg.get("critical_percent", 10)
        self.taper_factor = cfg.get("taper_factor", 1.5)
        self.power_floor_w = cfg.get("power_floor_w", 0.5)
        self._mono = mono

        self._disp_power: Optional[float] = None   # W, display EMA
        self._est_power: Optional[float] = None    # W, estimate EMA
        self._power_ts: Optional[float] = None
        self._power_fresh = False                  # raw power valid this sample
        self._last_status: Optional[str] = None
        self._last_ac: Optional[bool] = None
        self._transition_ts: float = mono() - 1e6
        self._last_sample_ts: Optional[float] = None
        # (mono_ts, energy_wh) samples for derived power
        self._energy_win: deque[tuple[float, float]] = deque(maxlen=256)
        self.quality = "warming_up"

    # ---------------------------------------------------------------- utils
    @staticmethod
    def _ema(prev: Optional[float], cur: float, dt: float, tau: float) -> float:
        if prev is None or dt <= 0:
            return cur
        alpha = 1.0 - math.exp(-dt / tau)
        return alpha * cur + (1.0 - alpha) * prev

    def _reset_smoothing(self) -> None:
        self._disp_power = None
        self._est_power = None
        self._power_ts = None
        self._power_fresh = False
        self._energy_win.clear()
        self._transition_ts = self._mono()

    # ------------------------------------------------------------------ api
    def feed(self, snap: Optional[BatterySnapshot], ac_online: Optional[bool],
             wall: Optional[float] = None) -> dict:
        """Return the public battery state dict (protocol `battery` module)."""
        now = self._mono()
        wall = wall if wall is not None else time.time()
        dt_sample = (now - self._last_sample_ts
                     if self._last_sample_ts is not None else None)
        self._last_sample_ts = now
        out: dict = {
            "status": None, "state": "unknown", "ac_online": ac_online,
            "percent": None, "energy_wh": None, "energy_full_wh": None,
            "energy_full_design_wh": None, "health_percent": None,
            "cycle_count": None, "voltage_v": None, "current_a": None,
            "power_w": None, "power_raw_w": None, "power_source": None,
            "time_to_empty_s": None, "time_to_full_s": None,
            "estimating": False,
            "capacity_level": None, "technology": None, "manufacturer": None,
            "quality": "unavailable", "observed_at": wall,
        }
        if snap is None or (snap.present is not None and snap.present == 0):
            # device gone: drop all derived state so a reappearing battery
            # (or a VM hot-plug) never inherits stale EMAs
            self._reset_smoothing()
            self.quality = "unavailable"
            out["state"] = "nobattery" if ac_online is not False else "nobattery"
            if ac_online is None:
                out["state"] = "nobattery"  # readable sysfs, no battery present
            return out

        # --- energy / capacity (prefer energy_*, fall back to charge_*)
        e_now_uwh, e_full_uwh, e_design_uwh = (snap.energy_now, snap.energy_full,
                                              snap.energy_full_design)
        # charge_* are µAh; µAh x µV / 1e6 = µWh
        if e_now_uwh is None and snap.charge_now is not None and snap.voltage_now:
            e_now_uwh = snap.charge_now * snap.voltage_now // 1_000_000
        if e_full_uwh is None and snap.charge_full is not None and snap.voltage_now:
            e_full_uwh = snap.charge_full * snap.voltage_now // 1_000_000
        if e_design_uwh is None and snap.charge_full_design is not None and snap.voltage_now:
            e_design_uwh = snap.charge_full_design * snap.voltage_now // 1_000_000

        e_wh = e_now_uwh / 1e6 if e_now_uwh is not None else None
        full_wh = e_full_uwh / 1e6 if e_full_uwh is not None else None
        design_wh = e_design_uwh / 1e6 if e_design_uwh is not None else None

        percent: Optional[float] = None
        if e_wh is not None and full_wh and full_wh > 0:
            percent = 100.0 * e_wh / full_wh
        elif snap.capacity is not None:
            percent = float(snap.capacity)
        if percent is not None:
            percent = max(0.0, min(100.0, percent))

        health = None
        if full_wh and design_wh and design_wh > 0:
            health = 100.0 * full_wh / design_wh  # deliberately not clamped

        # --- transitions reset smoothing/estimates
        status = snap.status
        if status != self._last_status or ac_online != self._last_ac:
            self._reset_smoothing()
            self._last_status = status
            self._last_ac = ac_online

        # --- suspend / long-gap detection
        if dt_sample is not None and dt_sample > SUSPEND_GAP_S:
            self._energy_win.clear()
            self._est_power = None
            self._disp_power = None
            self._power_ts = None
            self._transition_ts = now  # re-enter warm-up after suspend

        # --- power: measured or derived
        raw_w: Optional[float] = None
        source: Optional[str] = None
        if snap.power_now is not None and snap.power_now > 0:
            raw_w = snap.power_now / 1e6
            source = "power_now"
        # derived slope (fallback and sanity)
        if e_wh is not None:
            self._energy_win.append((now, e_wh))
            self._prune_energy_win(now)
        derived_w = self._derived_power()
        if raw_w is None and derived_w is not None:
            raw_w = abs(derived_w)
            source = "derived"
        self._power_fresh = raw_w is not None
        if raw_w is not None:
            dt = now - self._power_ts if self._power_ts is not None else 0.0
            self._disp_power = self._ema(self._disp_power, raw_w, dt, DISPLAY_TAU)
            self._est_power = self._ema(self._est_power, raw_w, dt, ESTIMATE_TAU)
            self._power_ts = now

        voltage_v = snap.voltage_now / 1e6 if snap.voltage_now else None
        current_a = None
        if voltage_v and raw_w is not None and voltage_v > 0.1:
            current_a = raw_w / voltage_v

        # --- time estimates (require *current* fresh power, not stale EMA)
        est_w = self._est_power if self._power_fresh else None
        tte: Optional[float] = None
        ttf: Optional[float] = None
        stable = (now - self._transition_ts) >= SUPPRESS_AFTER_TRANSITION
        usable = (est_w is not None and est_w >= self.power_floor_w and stable
                  and snap.status is not None)
        s = (status or "").lower()
        if usable:
            if s == "discharging" and e_wh is not None:
                tte = e_wh / est_w * 3600.0
            elif s == "charging" and e_wh is not None and full_wh is not None:
                remaining_wh = max(full_wh - e_wh, 0.0)
                taper = self.taper_factor if (percent or 0) > 80 else 1.0
                ttf = remaining_wh / est_w * taper * 3600.0
            if tte is not None and (tte > MAX_ESTIMATE_S or tte <= 0):
                tte = None
            if ttf is not None and (ttf > MAX_ESTIMATE_S or (ttf <= 0 and (percent or 0) < 99.5)):
                ttf = None

        estimating = (s in ("discharging", "charging") and tte is None
                      and ttf is None)

        state = classify_state(status, ac_online, percent,
                               self.low_percent, self.critical_percent)

        out.update({
            "status": status,
            "state": state,
            "percent": round(percent, 1) if percent is not None else None,
            "energy_wh": round(e_wh, 2) if e_wh is not None else None,
            "energy_full_wh": round(full_wh, 2) if full_wh is not None else None,
            "energy_full_design_wh": round(design_wh, 2) if design_wh is not None else None,
            "health_percent": round(health, 1) if health is not None else None,
            "cycle_count": snap.cycle_count,
            "voltage_v": round(voltage_v, 2) if voltage_v is not None else None,
            "current_a": round(current_a, 2) if current_a is not None else None,
            "power_w": round(self._disp_power, 1) if self._disp_power is not None else None,
            "power_raw_w": round(raw_w, 2) if raw_w is not None else None,
            "power_source": source,
            "time_to_empty_s": int(tte) if tte is not None else None,
            "time_to_full_s": int(ttf) if ttf is not None else None,
            "estimating": estimating,
            "capacity_level": snap.capacity_level,
            "technology": snap.technology,
            "manufacturer": snap.manufacturer,
            "quality": "ok" if percent is not None else "stale",
        })
        self.quality = out["quality"]
        return out

    def _prune_energy_win(self, now: float) -> None:
        cutoff = now - DERIVED_WINDOW
        while self._energy_win and self._energy_win[0][0] < cutoff:
            self._energy_win.popleft()

    def _derived_power(self) -> Optional[float]:
        """Signed W (+ = discharge) from the energy slope over the window."""
        if len(self._energy_win) < 3:
            return None
        t0, e0 = self._energy_win[0]
        t1, e1 = self._energy_win[-1]
        span = t1 - t0
        if span < DERIVED_MIN_SPAN or span <= 0:
            return None
        return (e0 - e1) * 3600.0 / span
