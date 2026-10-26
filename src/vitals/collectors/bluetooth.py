"""Bluetooth state model (SPEC §8, DESIGN §10.2).

GI-free: BluezModel mirrors the org.bluez object tree from plain Python
values (unpacked D-Bus variants) so it can be driven by the real backend
in vitals.linux.bluez or by test fixtures.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
BATTERY = "org.bluez.Battery1"
TRACKED = (ADAPTER, DEVICE, BATTERY)

MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")

# UUID prefixes (first 8 hex digits of the 128-bit base UUID form)
AUDIO_UUIDS = ("0000110b", "0000111e", "00001108")  # sink, handsfree, headset
OPP_UUID = "00001105"                                 # OBEX Object Push

ADAPTER_PROPS = ("Address", "Alias", "Powered", "PowerState", "Discovering",
                 "Pairable", "Discoverable")
DEVICE_PROPS = ("Address", "Alias", "Name", "Icon", "Paired", "Bonded",
                "Trusted", "Blocked", "Connected", "RSSI", "UUIDs", "Adapter")


def _has_uuid(uuids, prefixes) -> bool:
    return any(str(u).lower().startswith(prefixes) for u in uuids or ())


def map_error(name: str, message: str = "") -> str:
    """Stable protocol error code for a BlueZ D-Bus error name."""
    short = name.rsplit(".", 1)[-1]
    msg = message.lower()
    if short in ("AuthenticationFailed", "AuthenticationRejected",
                 "AuthenticationCanceled", "AuthenticationTimeout"):
        return "auth_failed"
    if short in ("InProgress", "Busy"):
        return "busy"
    if short == "NotReady" or "host is down" in msg or "page timeout" in msg:
        return "unreachable"
    if short in ("NotAvailable", "NotSupported", "ProfileUnavailable") \
            or "profile" in msg:
        return "profile_unavailable"
    if short in ("NotAuthorized", "AccessDenied") or "denied" in msg:
        return "permission_denied"
    if short in ("Timeout", "NoReply") or "timeout" in msg or "timed out" in msg:
        return "timeout"
    if short in ("Blocked",) or "rfkill" in msg:
        return "blocked"
    return "unavailable"


class BluezModel:
    """Mirror of org.bluez objects: path -> {interface: {prop: value}}."""

    def __init__(self):
        self.objects: dict[str, dict[str, dict]] = {}

    def reset(self, managed: dict | None = None) -> None:
        self.objects = {}
        for path, ifaces in (managed or {}).items():
            self.interfaces_added(path, ifaces)

    # Mutators return True when a tracked interface changed, so callers can
    # ignore the GATT/media traffic that shares the same signals.
    def interfaces_added(self, path: str, ifaces: dict) -> bool:
        hit = False
        for iface, props in ifaces.items():
            if iface in TRACKED:
                self.objects.setdefault(path, {}).setdefault(
                    iface, {}).update(props or {})
                hit = True
        return hit

    def interfaces_removed(self, path: str, ifaces) -> bool:
        obj = self.objects.get(path)
        if obj is None:
            return False
        hit = False
        for iface in ifaces:
            hit = obj.pop(iface, None) is not None or hit
        if not obj:
            del self.objects[path]
        return hit

    def properties_changed(self, path: str, iface: str, changed: dict,
                           invalidated=()) -> bool:
        if iface not in TRACKED:
            return False
        props = self.objects.setdefault(path, {}).setdefault(iface, {})
        props.update(changed or {})
        for name in invalidated or ():
            props.pop(name, None)
        return True

    # ------------------------------------------------------------ queries
    def adapter_path(self) -> str | None:
        paths = sorted(p for p, o in self.objects.items() if ADAPTER in o)
        return paths[0] if paths else None

    def adapter(self) -> dict | None:
        p = self.adapter_path()
        return self.objects[p][ADAPTER] if p else None

    def device_path(self, address: str) -> str | None:
        for p, o in self.objects.items():
            d = o.get(DEVICE)
            if d and str(d.get("Address", "")).upper() == address.upper():
                return p
        return None

    def devices(self) -> list[tuple[str, dict, dict | None]]:
        """(path, Device1 props, Battery1 props or None) under the adapter."""
        ap = self.adapter_path()
        out = []
        for p, o in sorted(self.objects.items()):
            d = o.get(DEVICE)
            if d is None:
                continue
            if ap and d.get("Adapter", ap) != ap:
                continue
            out.append((p, d, o.get(BATTERY)))
        return out


class LastConnected:
    """MAC -> wall time of the last Connected false->true transition,
    persisted as JSON (BlueZ does not expose it; SPEC 8.2.3 sorts by it)."""

    def __init__(self, path: Path | None):
        self.path = path
        self.times: dict[str, float] = {}
        self._connected: set[str] | None = None   # None until first update
        if path is not None:
            try:
                data = json.loads(path.read_text())
                self.times = {str(k).upper(): float(v) for k, v in data.items()
                              if isinstance(v, (int, float))}
            except (OSError, ValueError, AttributeError):
                self.times = {}

    def update(self, connected: set[str], now: float | None = None) -> bool:
        """Record newly connected MACs; True (and saved) if anything changed.
        Devices already connected at startup only get a time if unknown."""
        now = time.time() if now is None else now
        connected = {m.upper() for m in connected}
        prev = self._connected
        self._connected = connected
        new = connected - prev if prev is not None else \
            {m for m in connected if m not in self.times}
        if not new:
            return False
        for m in new:
            self.times[m] = now
        self._save()
        return True

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.times))
            os.replace(tmp, self.path)
        except OSError:
            pass


def device_state(dev: dict, battery: dict | None, busy: str | None = None) -> dict:
    pct = battery.get("Percentage") if battery else None
    return {
        "address": dev.get("Address"),
        "alias": dev.get("Alias") or dev.get("Name") or dev.get("Address"),
        "icon": dev.get("Icon"),
        "paired": bool(dev.get("Paired")),
        "trusted": bool(dev.get("Trusted")),
        "blocked": bool(dev.get("Blocked")),
        "connected": bool(dev.get("Connected")),
        "battery": int(pct) if pct is not None else None,
        "rssi": dev.get("RSSI"),
        "audio": _has_uuid(dev.get("UUIDs"), AUDIO_UUIDS),
        "opp": _has_uuid(dev.get("UUIDs"), OPP_UUID),
        "busy": busy,
    }


def snapshot(model: BluezModel, available: bool, rfkill: str,
             busy: dict[str, str] | None = None,
             last_connected: dict[str, float] | None = None) -> dict:
    """The `bluetooth` module state published by the daemon.
    Devices: paired or connected ones (discovered-only devices arrive with
    discovery support)."""
    busy = busy or {}
    last_connected = last_connected or {}
    ad = model.adapter() if available else None
    adapter = None
    if ad is not None:
        adapter = {
            "address": ad.get("Address"),
            "alias": ad.get("Alias"),
            "powered": bool(ad.get("Powered")),
            "power_state": ad.get("PowerState") or
            ("on" if ad.get("Powered") else "off"),
            "discovering": bool(ad.get("Discovering")),
        }
    devices = []
    if ad is not None:
        for _p, d, b in model.devices():
            if d.get("Paired") or d.get("Connected"):
                mac = str(d.get("Address", "")).upper()
                ds = device_state(d, b, busy.get(mac))
                ts = last_connected.get(mac)
                ds["last_connected"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None
                devices.append(ds)
        devices.sort(key=lambda x: (not x["connected"], (x["alias"] or "").lower()))
    return {
        "available": available,
        "rfkill": rfkill,
        "adapter": adapter,
        "devices": devices,
        "transfer": None,
        "prompt": None,
    }


def sections(bt: dict) -> tuple[list[dict], list[dict]]:
    """(connected, paired-but-not-connected) for the panel (SPEC 8.2.2-3).
    Paired devices are most recently connected first, never-connected last,
    then by name, so the order is stable while the panel is open."""
    devices = (bt or {}).get("devices") or []
    connected = [d for d in devices if d.get("connected")]
    paired = [d for d in devices if not d.get("connected") and d.get("paired")]
    connected.sort(key=lambda d: (d.get("alias") or "").lower())
    paired.sort(key=lambda d: (d.get("last_connected") is None,
                               _neg_iso(d.get("last_connected")),
                               (d.get("alias") or "").lower()))
    return connected, paired


def _neg_iso(ts: str | None) -> str:
    # ISO-8601 UTC strings sort chronologically; invert for descending order
    return "".join(chr(0x7F - ord(c)) for c in ts) if ts else ""


def classify(bt: dict, low_battery: int = 15) -> tuple[str, list[str]]:
    """Indicator state and CSS classes for SPEC §8.1."""
    if not bt or not bt.get("available"):
        return "unknown", ["unknown"]
    if bt.get("rfkill") == "hard":
        return "blocked", ["blocked"]
    ad = bt.get("adapter")
    if ad is None:
        return "noadapter", ["noadapter"]
    if not ad.get("powered") or bt.get("rfkill") == "soft":
        return "off", ["off"]
    connected = [d for d in bt.get("devices") or [] if d.get("connected")]
    if connected:
        state, classes = "connected", ["connected"]
        if any(d.get("audio") for d in connected):
            classes.append("audio")
    else:
        state, classes = "on", ["on"]
    if any(d.get("battery") is not None and d["battery"] <= low_battery
           for d in connected):
        classes.append("lowbattery")
    if ad.get("discovering") or any(d.get("busy") for d in bt.get("devices") or []):
        classes.append("busy")
    return state, classes
