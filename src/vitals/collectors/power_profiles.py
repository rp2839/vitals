"""Power-profile control via power-profiles-daemon (system D-Bus, Gio).

Probes the compatibility name net.hadess.PowerProfiles first, then the
canonical org.freedesktop.UPower.PowerProfiles. Read/set ActiveProfile via
org.freedesktop.DBus.Properties; live-syncs via PropertiesChanged.
"""
from __future__ import annotations

from typing import Callable, Optional

from gi.repository import Gio, GLib

BUS_NAMES = [
    ("net.hadess.PowerProfiles", "/net/hadess/PowerProfiles",
     "net.hadess.PowerProfiles"),
    ("org.freedesktop.UPower.PowerProfiles",
     "/org/freedesktop/UPower/PowerProfiles",
     "org.freedesktop.UPower.PowerProfiles"),
]
DBUS_PROPS = "org.freedesktop.DBus.Properties"
DBUS = "org.freedesktop.DBus"


def _profile_names(prof) -> list[str]:
    """Handle both formats: a(sbi) tuples (legacy) and a{a{sv}} dicts (>=0.30)."""
    out = []
    for entry in prof or []:
        if isinstance(entry, dict):
            name = entry.get("Profile") or entry.get("ProfileMode")
            if name:
                out.append(str(name))
        elif isinstance(entry, (list, tuple)) and entry:
            out.append(str(entry[0]))
        elif isinstance(entry, str):
            out.append(entry)
    return out


def _first_holder(holds) -> Optional[str]:
    """ActiveProfileHolds is aa{sv}: list of dicts with ApplicationId."""
    try:
        for entry in holds or []:
            if isinstance(entry, dict):
                app = entry.get("ApplicationId")
                if app:
                    return str(app)
    except Exception:
        pass
    return None


class PowerProfiles:
    def __init__(self, on_change: Callable[[], None] | None = None):
        self.on_change = on_change
        self.bus: Optional[Gio.DBusConnection] = None
        self.bus_name = self.path = self.iface = None
        self.active: Optional[str] = None
        self.profiles: list[str] = []
        self.hold: Optional[str] = None
        self.available = False
        self._sub_id = None
        self._name_sub = None

    def start(self) -> None:
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error:
            return
        # watch name (dis)appearance first: discovery may succeed later
        self._name_sub = self.bus.signal_subscribe(
            DBUS, "org.freedesktop.DBus", "NameOwnerChanged",
            "/org/freedesktop/DBus", None, Gio.DBusSignalFlags.NONE,
            self._on_name_owner)
        self._discover()

    def _discover(self) -> bool:
        for name, path, iface in BUS_NAMES:
            try:
                prof = self._props_get(name, path, iface, "Profiles")
                self.bus_name, self.path, self.iface = name, path, iface
                self.profiles = _profile_names(prof)
                self.available = True
                self._refresh()
                if self._sub_id is None:
                    self._sub_id = self.bus.signal_subscribe(
                        self.bus_name, DBUS_PROPS, "PropertiesChanged",
                        self.path, None, Gio.DBusSignalFlags.NONE,
                        self._on_props)
                return True
            except GLib.Error:
                continue
        self.available = False
        return False

    def _props_get(self, bus_name, path, iface, prop):
        res = self.bus.call_sync(
            bus_name, path, DBUS_PROPS, "Get",
            GLib.Variant("(ss)", (iface, prop)),
            GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 2000, None)
        return res.unpack()[0]

    def _refresh(self) -> None:
        try:
            self.active = self._props_get(self.bus_name, self.path, self.iface,
                                          "ActiveProfile")
            prof = self._props_get(self.bus_name, self.path, self.iface, "Profiles")
            self.profiles = _profile_names(prof)
            try:
                hold = self._props_get(self.bus_name, self.path, self.iface,
                                       "ActiveProfileHolds")
                self.hold = _first_holder(hold)
            except GLib.Error:
                self.hold = None
        except GLib.Error:
            self.available = False

    def _on_props(self, conn, sender, path, iface, sig, params) -> None:
        changed = params.unpack()[1]
        if "ActiveProfile" in changed:
            self.active = changed["ActiveProfile"]
        if "Profiles" in changed:
            self.profiles = _profile_names(changed["Profiles"])
        if self.on_change:
            self.on_change()

    def _on_name_owner(self, conn, sender, path, iface, sig, params) -> None:
        name, old, new = params.unpack()
        if name not in [n for n, _, _ in BUS_NAMES]:
            return
        if new:
            if self._discover():
                if self.on_change:
                    self.on_change()
        elif name == self.bus_name:
            self.available = False
            if self.on_change:
                self.on_change()

    def set_profile(self, profile: str) -> tuple[bool, Optional[str]]:
        if not self.available:
            return False, "unavailable"
        if profile not in self.profiles:
            return False, "invalid_value"
        try:
            self.bus.call_sync(
                self.bus_name, self.path, DBUS_PROPS, "Set",
                GLib.Variant("(ssv)", (self.iface, "ActiveProfile",
                                       GLib.Variant("s", profile))),
                None, Gio.DBusCallFlags.NONE, 3000, None)
        except GLib.Error as e:
            msg = str(e)
            if "Interact" in msg or "auth" in msg.lower() or "denied" in msg.lower():
                return False, "permission_denied"
            return False, "unavailable"
        self._refresh()
        if self.active != profile:
            return False, "unavailable"
        return True, None

    def state(self) -> dict:
        return {
            "active_profile": self.active if self.available else None,
            "profiles": list(self.profiles),
            "hold": self.hold,
            "available": self.available,
        }

    def stop(self) -> None:
        if self.bus and self._sub_id:
            self.bus.signal_unsubscribe(self._sub_id)
        if self.bus and self._name_sub:
            self.bus.signal_unsubscribe(self._name_sub)
