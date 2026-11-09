"""vitalsd — the Vitals metrics & controls daemon.

Single GLib main loop owning: battery collector (sysfs + inotify + timers),
power-profile control (power-profiles-daemon over D-Bus), brightness control
(brightnessctl), history, and a newline-JSON UNIX-socket API for the waybar
bridge and the panel (protocol: docs/DESIGN.md §3).
"""
from __future__ import annotations

import os
import signal
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Callable

from gi.repository import GLib, Gio

from . import config as config_mod
from . import notify as notify_mod
from . import protocol
from .collectors import bluetooth as bt_mod
from .collectors.battery import BatteryEstimator
from .collectors.power_profiles import PowerProfiles
from .controls.brightness import Brightness
from .history import History
from .linux.inotify import (Inotify, IN_ATTRIB, IN_CREATE, IN_DELETE,
                            IN_DELETE_SELF, IN_MODIFY, IN_MOVED_FROM,
                            IN_MOVED_TO)
from .linux.bluez import BluezBackend
from .linux.sysfs import PowerSupplyReader
from .linux import rfkill as rfkill_mod
from .linux import uevent as uevent_mod

PROTOCOL_VERSION = 1
SAMPLE_ACTIVE_S = 5
SAMPLE_PANEL_S = 2
SAMPLE_IDLE_S = 30
BROADCAST_INTERVAL_MS = 1000
HISTORY_WINDOW_S = 24 * 3600
BRIGHT_TTL_HIDDEN_S = 30.0
BRIGHT_TTL_PANEL_S = 5.0
BT_FLUSH_MS = 100          # coalesce BlueZ signal bursts; <500 ms to the bar

# action name -> (Device1 method, timeout ms, busy label)
BT_DEVICE_ACTIONS = {
    "bluetooth.connect": ("Connect", 30000, "connecting"),
    "bluetooth.disconnect": ("Disconnect", 10000, "disconnecting"),
}


def socket_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return Path(base) / "vitals" / "control.sock"


class Client:
    """One connected socket client."""
    _next = 1

    def __init__(self, daemon: "Daemon", conn: socket.socket):
        self.daemon = daemon
        self.conn = conn
        self.conn.setblocking(False)
        self.parser = protocol.FrameParser()
        self.out: list[bytes] = []
        self.out_watch = None
        self.subscribed: set[str] = set()
        self.sub_since = time.monotonic()
        self.last_sent = 0.0
        self.alive = True
        self.cid = Client._next
        Client._next += 1

    def fileno(self) -> int:
        return self.conn.fileno()

    def on_readable(self) -> bool:
        try:
            data = self.conn.recv(16384)
        except (BlockingIOError, InterruptedError):
            return True
        except OSError:
            return False
        if not data:
            return False
        try:
            frames = self.parser.feed(data)
        except protocol.FrameParser.ProtocolError as e:
            self.send(protocol.error_reply(None, "invalid_request", str(e)))
            return False
        for obj in frames:
            try:
                op, rid = protocol.validate_request(obj)
            except protocol.FrameParser.ProtocolError as e:
                code = "unsupported_version" if "version" in str(e) else "invalid_request"
                self.send(protocol.error_reply(obj.get("id"), code, str(e)))
                continue
            try:
                self.daemon.handle_request(self, op, rid, obj)
            except Exception as e:  # a bad request must never kill the watch
                self.send(protocol.error_reply(rid, "internal",
                                               f"handler error: {e.__class__.__name__}"))
        return True

    def send(self, obj: dict) -> None:
        if not self.alive:
            return
        try:
            data = protocol.encode(obj)
        except (ValueError, TypeError):
            return
        if self.out:
            if len(self.out) > 256:
                self.close()  # slow client: drop it
                return
            self.out.append(data)
            return
        try:
            self.conn.sendall(data)
        except (BlockingIOError, InterruptedError):
            self.out.append(data)
            self._arm_out()
        except OSError:
            self.close()

    def _arm_out(self) -> None:
        if self.out_watch is None:
            self.out_watch = GLib.io_add_watch(
                self.conn, GLib.IO_OUT, self._on_writable)

    def _on_writable(self, *_a) -> bool:
        while self.out:
            try:
                self.conn.send(self.out[0])
                self.out.pop(0)
            except (BlockingIOError, InterruptedError):
                return True
            except OSError:
                self.close()
                return False
        if self.out_watch is not None:
            GLib.source_remove(self.out_watch)
            self.out_watch = None
        return False

    def close(self) -> None:
        if not self.alive:
            return
        self.alive = False
        if self.out_watch is not None:
            GLib.source_remove(self.out_watch)
            self.out_watch = None
        self.daemon.clients.discard(self)
        try:
            self.conn.close()
        except OSError:
            pass


class Daemon:
    def __init__(self, cfg):
        self.cfg = cfg
        self.reader = PowerSupplyReader()
        self.estimator = BatteryEstimator(cfg["battery"])
        self.history = History(maxlen=7200)
        self.profiles = PowerProfiles(on_change=self._on_controls_changed)
        self.brightness = Brightness(cfg.get("brightness", "device", "auto"))
        self.notifier = notify_mod.BatteryNotifier(
            cfg["notifications"], cfg["battery"])
        self.bluez = BluezBackend(on_change=self._on_bt_changed)
        self.bt_rfkill = rfkill_mod.state()
        self.bt_busy: dict[str, str] = {}   # MAC -> busy label (SPEC 8.2)
        self.bt_last = bt_mod.LastConnected(
            config_mod.state_dir() / "bluetooth.json")
        self._bt_flush_pending = False
        self.clients: set[Client] = set()
        self.battery_state: dict = {}
        # module name -> callable returning that module's full current state
        self.modules: dict[str, Callable[[], dict]] = {}
        self.histories: dict[str, History] = {}
        # panel names the daemon accepts; at most one is visible at a time
        self.panels: set[str] = set()
        self.visible_panel: str | None = None
        self.seq = 0
        self._sample_handle = None
        self._sample_pending = False
        self._inotify: Inotify | None = None
        self._inotify_wd = None
        self._uevent_fd = None
        self._uevent_watch = None
        self._dirty = False
        self._last_history_ts = 0.0
        self._last_sample_mono = 0.0
        self._server_fd: socket.socket | None = None
        self._server_watch = None
        self._loop = None
        # brightness cache (subprocess-backed; do not spawn per flush)
        self._bright_cache: dict = {"value": None, "available": self.brightness.available,
                                    "device": self.brightness.device,
                                    "min": 1.0, "max": 100.0}
        self._bright_cache_ts = 0.0
        # power-source tracking for panel rows (SPEC §3.4)
        self._last_ac: bool | None = None
        self._ac_since = time.time()

        self.register_module("battery", self._battery_data, history=self.history)
        self.register_module("power", self._power_data)
        self.register_module("bluetooth", self._bluetooth_data)
        self.register_panel("battery")
        self.register_panel("bluetooth")

    # ------------------------------------------------------------- registry
    def register_module(self, name: str, data: Callable[[], dict],
                        history: History | None = None) -> None:
        self.modules[name] = data
        if history is not None:
            self.histories[name] = history

    def register_panel(self, name: str) -> None:
        self.panels.add(name)

    @property
    def panel_visible(self) -> bool:
        """True while the battery panel is open (drives battery sampling)."""
        return self.visible_panel == "battery"

    # ------------------------------------------------------------- lifecycle
    def start(self, loop) -> int:
        self._loop = loop
        path = socket_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        if path.exists():
            # stale? if something answers, another daemon owns the socket
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.5)
                probe.connect(str(path))
                probe.close()
                print("vitalsd: another daemon is already running", file=sys.stderr)
                return 1
            except OSError:
                try:
                    path.unlink()
                except OSError:
                    pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_mask = os.umask(0o177)  # socket created 0600 (no bind/chmod race)
        try:
            srv.bind(str(path))
        finally:
            os.umask(old_mask)
        srv.listen(16)
        srv.setblocking(False)
        self._server_fd = srv
        self._server_watch = GLib.io_add_watch(srv, GLib.IO_IN, self._on_accept)

        self.profiles.start()
        self.bluez.start()
        self._setup_inotify()
        self._setup_uevent()
        self.sample_battery()          # first sample immediately
        self._schedule_sample()
        GLib.timeout_add(BROADCAST_INTERVAL_MS, self._flush_updates)
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_signal)
        return 0

    def _on_signal(self, *_a) -> bool:
        self.shutdown()
        if self._loop:
            self._loop.quit()
        return False

    def shutdown(self) -> None:
        for c in list(self.clients):
            c.close()
        self.bluez.stop()
        if self._inotify:
            try:
                self._inotify.close()
            except OSError:
                pass
            self._inotify = None
        if self._server_fd:
            self._server_fd.close()
            self._server_fd = None
        try:
            socket_path().unlink()
        except OSError:
            pass

    # ------------------------------------------------------------- sampling
    def _setup_inotify(self) -> None:
        root = self.reader.root
        try:
            self._inotify = Inotify()
            self._inotify_wd = self._inotify.add_watch(
                str(root), IN_CREATE | IN_DELETE | IN_MOVED_FROM | IN_MOVED_TO
                | IN_ATTRIB | IN_MODIFY)
            GLib.io_add_watch(self._inotify.fd, GLib.IO_IN, self._on_inotify)
        except OSError:
            if self._inotify:
                try:
                    self._inotify.close()
                except OSError:
                    pass
            self._inotify = None

    def _setup_uevent(self) -> None:
        try:
            s = uevent_mod.open_uevent_socket()
        except OSError:
            return
        self._uevent_fd = s
        self._uevent_watch = GLib.io_add_watch(s, GLib.IO_IN, self._on_uevent)

    def _on_uevent(self, *_a) -> bool:
        try:
            data = self._uevent_fd.recv(8192)
        except (BlockingIOError, OSError):
            return True
        if data and uevent_mod.matches(data, ("power_supply",)):
            self.sample_battery()
            self._reschedule_sample()
            self._flush_updates()  # immediate: <500ms plug/unplug target
        if data and uevent_mod.matches(data, ("rfkill",)):
            self.bt_rfkill = rfkill_mod.state()
            self._on_bt_changed()
        return True

    def _on_inotify(self, *_a) -> bool:
        if not self._inotify:
            return False
        events = self._inotify.read_events()
        if events and not self._sample_pending:
            self._sample_pending = True
            GLib.timeout_add(100, self._debounced_sample)
        return True

    def _debounced_sample(self) -> bool:
        self._sample_pending = False
        self.sample_battery()
        self._reschedule_sample()
        return False

    def _schedule_sample(self) -> None:
        st = (self.battery_state.get("status") or "").lower()
        if self.panel_visible:
            interval_ms = SAMPLE_PANEL_S * 1000
        elif st in ("discharging", "charging"):
            interval_ms = SAMPLE_ACTIVE_S * 1000
        else:
            interval_ms = SAMPLE_IDLE_S * 1000
        if self._sample_handle:
            GLib.source_remove(self._sample_handle)
        self._sample_handle = GLib.timeout_add(
            interval_ms, self._on_sample_timer)

    def _reschedule_sample(self) -> None:
        self._schedule_sample()

    def _on_sample_timer(self) -> bool:
        self.sample_battery()
        self._schedule_sample()
        return False

    def sample_battery(self) -> None:
        want = self.cfg.get("battery", "device", "auto")
        name = self.reader.find_battery(want)
        snap = self.reader.read_battery(name) if name else None
        ac = self.reader.ac_online()
        prev_state = self.battery_state.get("state")
        prev_ac = self._last_ac
        now = time.monotonic()
        state = self.estimator.feed(snap, ac)
        self.battery_state = state
        self._dirty = True
        if ac != self._last_ac:
            self._ac_since = time.time()
            self._last_ac = ac
        # history: one point per >=5s with real data; explicit gap marker on loss
        if state.get("percent") is not None and now - self._last_history_ts >= 5:
            self._last_history_ts = now
            self.history.append(state["observed_at"], {
                "percent": state.get("percent"),
                "power_w": state.get("power_w"),
                "charging": 1 if (state.get("status") or "").lower() == "charging" else 0,
            })
        elif state.get("percent") is None and now - self._last_history_ts >= 5:
            self._last_history_ts = now
            self.history.append(state["observed_at"], {})  # gap marker
        self.notifier.update(state)
        # plug/unplug and state transitions flush immediately
        if state.get("state") != prev_state or ac != prev_ac:
            self._flush_updates()

    def _on_controls_changed(self) -> None:
        self._dirty = True

    def _on_bt_changed(self) -> None:
        if self.bluez.available:
            self.bt_last.update({str(d.get("Address", "")) for _p, d, _b
                                 in self.bluez.model.devices() if d.get("Connected")})
        self._dirty = True
        if not self._bt_flush_pending:
            self._bt_flush_pending = True
            GLib.timeout_add(BT_FLUSH_MS, self._bt_flush)

    def _bt_flush(self) -> bool:
        self._bt_flush_pending = False
        self._flush_updates()
        return False

    # ------------------------------------------------------------- broadcast
    def _brightness_state(self, force: bool = False) -> dict:
        ttl = BRIGHT_TTL_PANEL_S if self.panel_visible else BRIGHT_TTL_HIDDEN_S
        now = time.monotonic()
        if force or now - self._bright_cache_ts >= ttl:
            self._bright_cache = self.brightness.state()
            self._bright_cache_ts = now
        return self._bright_cache

    def _battery_data(self) -> dict:
        b = dict(self.battery_state)
        b["on_source_s"] = int(time.time() - self._ac_since)
        avg = self.history.series(["power_w"], 600, max_points=120)
        vals = [v for _, v in avg.get("power_w", []) if v is not None]
        b["avg_power_10m_w"] = round(sum(vals) / len(vals), 1) if vals else None
        return b

    def _power_data(self) -> dict:
        ps = self.profiles.state()
        bs = self._brightness_state()
        return {
            "active_profile": ps["active_profile"],
            "profiles": ps["profiles"],
            "hold": ps["hold"],
            "profile_available": ps["available"],
            "brightness_percent": bs["value"],
            "brightness_available": bs["available"],
            "brightness_device": bs["device"],
        }

    def _bluetooth_data(self) -> dict:
        return bt_mod.snapshot(self.bluez.model, self.bluez.available,
                               self.bt_rfkill, self.bt_busy, self.bt_last.times)

    def module_data(self, modules: list[str] | None = None) -> dict:
        """State for the requested registered modules (all when None);
        unknown names are ignored."""
        names = self.modules if modules is None else modules
        return {m: self.modules[m]() for m in names if m in self.modules}

    def _flush_updates(self) -> bool:
        if not self._dirty:
            return True
        now = time.monotonic()
        keep_dirty = False
        for c in list(self.clients):
            mods = [m for m in self.modules if m in c.subscribed]
            if not mods:
                continue
            if now - c.sub_since < 0.5:
                keep_dirty = True  # subscriber's ack is its snapshot; keep dirty
                continue
            self.seq += 1
            c.send({"v": 1, "event": "update", "seq": self.seq,
                    "data": self.module_data(mods)})
        self._dirty = keep_dirty
        return True

    # ------------------------------------------------------------- requests
    def handle_request(self, c: Client, op: str, rid, req: dict) -> None:
        if op == "ping":
            c.send(protocol.reply(rid, True, data={"pong": True}))
        elif op == "snapshot":
            self.seq += 1
            c.send(protocol.reply(rid, True, seq=self.seq,
                                  data=self.module_data(req.get("modules"))))
        elif op == "subscribe":
            mods = req.get("modules") or list(self.modules)
            if not isinstance(mods, list):
                c.send(protocol.error_reply(rid, "invalid_request",
                                            "modules must be a list"))
                return
            c.subscribed = set(mods)
            c.sub_since = time.monotonic()
            self.seq += 1
            c.send(protocol.reply(rid, True, seq=self.seq,
                                  data=self.module_data(
                                      [m for m in self.modules if m in c.subscribed]),
                                  panel={"panel": self.visible_panel,
                                         "visible": self.visible_panel is not None}))
        elif op == "controls.get":
            c.send(protocol.reply(rid, True, data=self._controls_get()))
        elif op == "controls.set":
            if req.get("control") == "bluetooth.power":
                self._bt_set_power(c, rid, req.get("value"))
            else:
                self._controls_set(c, rid, req)
        elif op == "action":
            self._action(c, rid, req)
        elif op == "history.get":
            self._history_get(c, rid, req)
        elif op in ("panel.toggle", "panel.set"):
            name = req.get("panel", "battery")
            if not isinstance(name, str) or name not in self.panels:
                c.send(protocol.error_reply(rid, "invalid_value",
                                            f"unknown panel {name!r}"))
                return
            if op == "panel.toggle":
                vis = self.visible_panel != name
            else:
                vis = bool(req.get("visible"))
            self._set_visible_panel(name if vis else
                                    (None if self.visible_panel == name
                                     else self.visible_panel))
            c.send(protocol.reply(rid, True, data={
                "panel": name, "visible": self.visible_panel == name}))
        else:
            c.send(protocol.error_reply(rid, "invalid_request",
                                        f"unknown op {op!r}"))

    def _controls_get(self) -> dict:
        ad = self.bluez.model.adapter() if self.bluez.available else None
        ps = self.profiles.state()
        bs = self.brightness.state()
        return {
            "power.profile": {"value": ps["active_profile"],
                              "choices": ps["profiles"],
                              "available": ps["available"],
                              "writable": "unknown"},
            "display.brightness": {"value": bs["value"], "min": bs["min"],
                                   "max": bs["max"],
                                   "available": bs["available"],
                                   "writable": "unknown",
                                   "device": bs["device"]},
            "bluetooth.power": {"value": bool(ad and ad.get("Powered"))
                                and self.bt_rfkill not in ("soft", "hard"),
                                "available": ad is not None
                                and self.bt_rfkill != "hard",
                                "writable": "unknown"},
        }

    def _controls_set(self, c: Client, rid, req: dict) -> None:
        control = req.get("control")
        value = req.get("value")
        if control == "power.profile":
            if not isinstance(value, str):
                c.send(protocol.error_reply(rid, "invalid_value",
                                            "profile must be a string"))
                return
            ok, err = self.profiles.set_profile(value)
        elif control == "display.brightness":
            if not isinstance(value, (int, float)):
                c.send(protocol.error_reply(rid, "invalid_value",
                                            "brightness must be a number"))
                return
            ok, err = self.brightness.set_percent(float(value))
        else:
            c.send(protocol.error_reply(rid, "unknown_control",
                                        f"unknown control {control!r}"))
            return
        if ok:
            self._dirty = True
            if control == "display.brightness":
                self._brightness_state(force=True)
                value = self._brightness_state()["value"]
            c.send(protocol.reply(rid, True, data={"control": control,
                                                   "value": value}))
        else:
            c.send(protocol.error_reply(rid, err or "unavailable",
                                        f"{control} change failed"))

    # ------------------------------------------------------------ bluetooth
    def _bt_set_power(self, c: Client, rid, value) -> None:
        if not isinstance(value, bool):
            c.send(protocol.error_reply(rid, "invalid_value",
                                        "bluetooth.power must be a boolean"))
            return
        if value and self.bt_rfkill == "hard":
            c.send(protocol.error_reply(rid, "blocked",
                                        "Bluetooth is blocked by a hardware switch"))
            return

        def done(ok: bool, err: str | None) -> None:
            if ok:
                self._on_bt_changed()
                c.send(protocol.reply(rid, True, data={
                    "control": "bluetooth.power", "value": value}))
            else:
                c.send(protocol.error_reply(rid, err or "unavailable",
                                            "Bluetooth power change failed"))

        if value and self.bt_rfkill == "soft":
            ok, err = rfkill_mod.unblock()
            if not ok:
                done(False, err)
                return
            self.bt_rfkill = rfkill_mod.state()
            def power_on() -> bool:
                self.bluez.set_powered(True, done)
                return False

            # give bluetoothd a moment to see the unblock before powering on
            GLib.timeout_add(300, power_on)
            return
        self.bluez.set_powered(value, done)

    def _action(self, c: Client, rid, req: dict) -> None:
        action = req.get("action")
        args = req.get("args") or {}
        if action not in BT_DEVICE_ACTIONS:
            c.send(protocol.error_reply(rid, "unknown_action",
                                        f"unknown action {action!r}"))
            return
        mac = args.get("device") if isinstance(args, dict) else None
        mac = mac.upper() if isinstance(mac, str) else ""
        if not bt_mod.MAC_RE.match(mac) or self.bluez.model.device_path(mac) is None:
            c.send(protocol.error_reply(rid, "invalid_value",
                                        "device must be a known MAC address"))
            return
        if mac in self.bt_busy:
            c.send(protocol.error_reply(rid, "busy",
                                        f"device is {self.bt_busy[mac]}"))
            return
        method, timeout_ms, label = BT_DEVICE_ACTIONS[action]
        self.bt_busy[mac] = label
        self._on_bt_changed()

        def done(ok: bool, err: str | None) -> None:
            self.bt_busy.pop(mac, None)
            self._on_bt_changed()
            if ok:
                c.send(protocol.reply(rid, True, data={"action": action,
                                                       "device": mac}))
            else:
                c.send(protocol.error_reply(rid, err or "unavailable",
                                            f"{action} failed"))

        self.bluez.device_call(mac, method, timeout_ms, done)

    def _history_get(self, c: Client, rid, req: dict) -> None:
        module = req.get("module", "battery")
        history = self.histories.get(module)
        if history is None:
            c.send(protocol.error_reply(rid, "unavailable",
                                        f"no history for {module!r}"))
            return
        fields = req.get("fields") or ["percent", "power_w"]
        if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
            c.send(protocol.error_reply(rid, "invalid_value",
                                        "fields must be a list of strings"))
            return
        try:
            window = float(req.get("window_s", 3600))
            max_points = int(req.get("max_points", 1440))
        except (TypeError, ValueError):
            c.send(protocol.error_reply(rid, "invalid_value",
                                        "window_s/max_points must be numbers"))
            return
        if not (0 < window <= HISTORY_WINDOW_S) or not (0 < max_points <= 1440):
            c.send(protocol.error_reply(rid, "invalid_value",
                                        "window_s/max_points out of range"))
            return
        series = history.series(fields, window, max_points)
        c.send(protocol.reply(rid, True, data={"module": module,
                                               "window_s": window,
                                               "series": series}))

    def _set_visible_panel(self, name: str | None) -> None:
        """Open `name` (closing any other panel) or close all with None."""
        prev = self.visible_panel
        if name == prev:
            return
        self.visible_panel = name
        if prev is not None:
            self._broadcast_panel(prev, False)
        if name is not None:
            self._broadcast_panel(name, True)
        if name == "battery":
            self._brightness_state(force=True)
        if "battery" in (prev, name):
            self._schedule_sample()

    def _broadcast_panel(self, name: str, visible: bool) -> None:
        for c in list(self.clients):
            if "panel" in c.subscribed:
                c.send({"v": 1, "event": "panel",
                        "data": {"panel": name, "visible": visible}})

    # ------------------------------------------------------------- accept
    def _on_accept(self, srv, *_a) -> bool:
        try:
            conn, _ = srv.accept()
        except OSError:
            return True
        # peer credential check
        try:
            cred = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                   struct.calcsize("iII"))
            pid, uid, gid = struct.unpack("iII", cred)
            if uid != os.getuid():
                conn.close()
                return True
        except OSError:
            conn.close()
            return True
        try:
            client = Client(self, conn)
        except OSError:
            return True
        self.clients.add(client)
        GLib.io_add_watch(client, GLib.IO_IN | GLib.IO_HUP,
                          self._on_client_io)
        return True

    def _on_client_io(self, client: Client, _cond) -> bool:
        if not client.alive:
            return False
        if not client.on_readable():
            client.close()
            return False
        return True


def main() -> int:
    cfg = config_mod.load()
    daemon = Daemon(cfg)
    loop = GLib.MainLoop()
    rc = daemon.start(loop)
    if rc:
        return rc
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        daemon.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
