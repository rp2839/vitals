"""BlueZ access over the system bus (DESIGN §10.2).

The only module that talks to org.bluez. Keeps a BluezModel current from
ObjectManager and PropertiesChanged signals (no polling), follows
bluetoothd restarts through NameOwnerChanged, and runs device/adapter
calls asynchronously, reporting (ok, error_code) to a callback.
"""
from __future__ import annotations

from typing import Callable, Optional

from gi.repository import Gio, GLib

from ..collectors.bluetooth import ADAPTER, DEVICE, BluezModel, map_error

BUS_NAME = "org.bluez"
DBUS = "org.freedesktop.DBus"
DBUS_PROPS = "org.freedesktop.DBus.Properties"
OBJ_MANAGER = "org.freedesktop.DBus.ObjectManager"

Done = Callable[[bool, Optional[str]], None]


class BluezBackend:
    def __init__(self, on_change: Callable[[], None] | None = None):
        self.on_change = on_change
        self.model = BluezModel()
        self.available = False
        self.bus: Optional[Gio.DBusConnection] = None
        self._subs: list[int] = []

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error:
            return
        sub = self.bus.signal_subscribe
        self._subs = [
            sub(DBUS, DBUS, "NameOwnerChanged", "/org/freedesktop/DBus",
                BUS_NAME, Gio.DBusSignalFlags.NONE, self._on_name_owner),
            sub(BUS_NAME, OBJ_MANAGER, "InterfacesAdded", "/", None,
                Gio.DBusSignalFlags.NONE, self._on_added),
            sub(BUS_NAME, OBJ_MANAGER, "InterfacesRemoved", "/", None,
                Gio.DBusSignalFlags.NONE, self._on_removed),
            sub(BUS_NAME, DBUS_PROPS, "PropertiesChanged", None, None,
                Gio.DBusSignalFlags.NONE, self._on_props),
        ]
        self._load()

    def stop(self) -> None:
        if self.bus:
            for s in self._subs:
                self.bus.signal_unsubscribe(s)
        self._subs = []

    def _changed(self) -> None:
        if self.on_change:
            self.on_change()

    def _load(self) -> None:
        def done(conn, res):
            try:
                managed = conn.call_finish(res).unpack()[0]
            except GLib.Error:
                self.available = False
                self.model.reset()
            else:
                self.available = True
                self.model.reset(managed)
            self._changed()

        self.bus.call(BUS_NAME, "/", OBJ_MANAGER, "GetManagedObjects", None,
                      GLib.VariantType("(a{oa{sa{sv}}})"),
                      Gio.DBusCallFlags.NONE, 5000, None, done)

    # -------------------------------------------------------------- signals
    def _on_name_owner(self, _c, _s, _p, _i, _sig, params) -> None:
        name, _old, new = params.unpack()
        if name != BUS_NAME:
            return
        if new:
            self._load()
        else:
            self.available = False
            self.model.reset()
            self._changed()

    def _on_added(self, _c, _s, _p, _i, _sig, params) -> None:
        path, ifaces = params.unpack()
        if self.model.interfaces_added(path, ifaces):
            self._changed()

    def _on_removed(self, _c, _s, _p, _i, _sig, params) -> None:
        path, ifaces = params.unpack()
        if self.model.interfaces_removed(path, ifaces):
            self._changed()

    def _on_props(self, _c, _s, path, _i, _sig, params) -> None:
        iface, changed, invalidated = params.unpack()
        if self.model.properties_changed(path, iface, changed, invalidated):
            self._changed()

    # -------------------------------------------------------------- calls
    def _call(self, path: str, iface: str, method: str,
              args: GLib.Variant | None, timeout_ms: int, done: Done) -> None:
        if not self.available or self.bus is None:
            done(False, "unavailable")
            return

        def finish(conn, res):
            try:
                conn.call_finish(res)
            except GLib.Error as e:
                name = Gio.DBusError.get_remote_error(e) or ""
                if name.endswith("AlreadyConnected"):
                    done(True, None)
                else:
                    done(False, map_error(name, e.message))
                return
            done(True, None)

        self.bus.call(BUS_NAME, path, iface, method, args, None,
                      Gio.DBusCallFlags.NONE, timeout_ms, None, finish)

    def set_powered(self, on: bool, done: Done) -> None:
        path = self.model.adapter_path()
        if path is None:
            done(False, "unavailable")
            return
        self._call(path, DBUS_PROPS, "Set",
                   GLib.Variant("(ssv)", (ADAPTER, "Powered",
                                          GLib.Variant("b", on))),
                   5000, done)

    def device_call(self, address: str, method: str, timeout_ms: int,
                    done: Done) -> None:
        """Connect / Disconnect on Device1 for a known address."""
        path = self.model.device_path(address)
        if path is None:
            done(False, "invalid_value")
            return
        self._call(path, DEVICE, method, None, timeout_ms, done)
