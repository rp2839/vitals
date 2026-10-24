"""vitals-panel — GTK3 + gtk-layer-shell dropdown panel (iStat-style).

A single layer-shell surface anchored top-right. It is a thin client: all
data comes from vitalsd over the JSON socket protocol; controls write
through the daemon. Visibility is driven by `panel` events (toggled by
`vitalsctl panel toggle battery` from the waybar module).
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gdk, GLib, Gtk, GtkLayerShell, Pango  # noqa: E402

from .bar import connect, fmt_time  # noqa: E402

RANGES = {"1h": 3600, "6h": 6 * 3600}

CSS = b"""
window.vitals-panel { background: #1d2021; color: #d5c4a1; border-radius: 10px;
                      border: 1px solid #3c3836; }
.vitals-panel .head-pct { font-size: 30px; font-weight: 800; }
.vitals-panel .head-state { font-size: 12px; color: #a89984; }
.vitals-panel .head-time { font-size: 14px; font-weight: 600; }
.vitals-panel .big { font-size: 15px; font-weight: 700; }
.vitals-panel .dim { color: #928374; font-size: 11px; }
.vitals-panel .label { color: #928374; font-size: 11px; }
.vitals-panel .value { font-size: 12px; }
.vitals-panel .section { margin: 6px 14px; }
.vitals-panel .sep { min-height: 1px; background: #3c3836; margin: 4px 14px; }
.vitals-panel button.profile { padding: 2px 10px; margin: 0 2px;
                               border-radius: 6px; border: 1px solid #3c3836;
                               background: #282828; color: #d5c4a1; }
.vitals-panel button.profile.active { background: #689d6a; color: #1d2021;
                                       font-weight: 700; }
.vitals-panel button.profile:disabled { color: #665c54; }
.vitals-panel button.close { padding: 0 8px; }
.vitals-panel trough highlight { background: #689d6a; }
.vitals-panel .wearbar trough highlight.low { background: #fb4934; }
"""


def _load_config():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from vitals import config as config_mod
    return config_mod.load()


class DaemonClient:
    """Socket client integrated with the GLib main loop."""

    def __init__(self, on_message, on_connected=None, on_lost=None):
        self.on_message = on_message
        self.on_connected = on_connected
        self.on_lost = on_lost
        self.sock: socket.socket | None = None
        self.buf = b""
        self._retry = 2000
        self._pending: list[dict] = []
        self._connect()

    def _connect(self) -> bool:
        try:
            self.sock = connect()
            self.sock.setblocking(False)
        except OSError:
            self.sock = None
            GLib.timeout_add(self._retry, self._connect)
            self._retry = min(self._retry * 2, 10000)
            return False
        self.buf = b""
        GLib.io_add_watch(self.sock, GLib.IO_IN | GLib.IO_HUP, self._on_io)
        if self.on_connected:
            self.on_connected()
        self._retry = 2000
        for msg in self._pending:
            self.send(msg)
        return False

    def _on_io(self, _src, cond) -> bool:
        if cond & (GLib.IO_HUP | GLib.IO_ERR):
            self._lost()
            return False
        try:
            data = self.sock.recv(16384)
        except (BlockingIOError, InterruptedError):
            return True
        except OSError:
            self._lost()
            return False
        if not data:
            self._lost()
            return False
        self.buf += data
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                self.on_message(json.loads(line))
            except json.JSONDecodeError:
                pass
        return True

    def _lost(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.on_lost:
            self.on_lost()
        GLib.timeout_add(self._retry, self._connect)

    def send(self, obj: dict) -> None:
        if self.sock is None:
            self._pending.append(obj)
            return
        try:
            self.sock.sendall((json.dumps(obj) + "\n").encode())
        except OSError:
            self._pending.append(obj)
            self._lost()


class Graph(Gtk.DrawingArea):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.series: dict[str, list] = {}
        self.window_s = RANGES["6h"]
        self.set_size_request(-1, 92)
        self.connect("draw", self.on_draw)

    def set_series(self, series: dict, window_s: float) -> None:
        self.series = series
        self.window_s = window_s
        self.queue_draw()

    def on_draw(self, _w, cr):
        alloc = self.get_allocation()
        w, h = alloc.width, alloc.height
        # bg
        cr.set_source_rgb(0.125, 0.141, 0.129)
        cr.paint()
        # threshold guides at 20% / 10%
        for th, col in ((20, (0.98, 0.74, 0.18)), (10, (0.90, 0.22, 0.20))):
            y = h - (th / 100.0) * h
            cr.set_source_rgba(*col, 0.25)
            cr.set_line_width(1)
            cr.move_to(0, y)
            cr.line_to(w, y)
            cr.stroke()
        pts = self.series.get("percent") or []
        if len(pts) < 2:
            cr.set_source_rgb(0.57, 0.51, 0.46)
            cr.select_font_face("sans")
            cr.set_font_size(11)
            cr.move_to(8, h / 2)
            cr.show_text("collecting history…")
            return
        t0 = time.time() - self.window_s
        t1 = time.time()

        def xy(t, v):
            x = (t - t0) / (t1 - t0) * w
            y = h - (max(0.0, min(100.0, v)) / 100.0) * h
            return x, y

        charging = any((v or 0) > 0.5 for _, v in (self.series.get("charging") or []))
        line_col = (0.40, 0.62, 0.42) if charging else (0.83, 0.77, 0.65)
        # filled area under percent line
        first = True
        for t, v in pts:
            if v is None:
                first = True
                continue
            x, y = xy(t, v)
            if first:
                path_start = (x, h)
                cr.move_to(x, h)
                cr.line_to(x, y)
                first = False
                last = (x, y)
            else:
                cr.line_to(x, y)
                last = (x, y)
        cr.line_to(last[0], h)
        cr.close_path()
        cr.set_source_rgba(*line_col, 0.18)
        cr.fill_preserve()
        cr.set_line_width(1.6)
        cr.set_source_rgb(*line_col)
        cr.stroke()
        # power overlay (faint, scaled to max)
        pw = [(t, v) for t, v in (self.series.get("power_w") or []) if v is not None]
        if len(pw) >= 2:
            mx = max(v for _, v in pw) or 1.0
            first = True
            for t, v in pw:
                x, _ = xy(t, 0)
                y = h - (v / mx) * (h - 6)
                if first:
                    cr.move_to(x, y)
                    first = False
                else:
                    cr.line_to(x, y)
            cr.set_line_width(1.0)
            cr.set_source_rgba(0.86, 0.42, 0.24, 0.55)
            cr.stroke()


class Panel:
    def __init__(self):
        self.cfg = _load_config()
        pcfg = self.cfg["panel"]
        self.width = int(pcfg.get("width", 360))
        self.range_key = pcfg.get("range_default", "6h")
        if self.range_key not in RANGES:
            self.range_key = "6h"
        self.visible = False
        self._brightness_drag = False
        self._setting = False
        self._last_ui_update = 0.0

        self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.win.get_style_context().add_class("vitals-panel")
        self.win.set_default_size(self.width, -1)
        GtkLayerShell.init_for_window(self.win)
        GtkLayerShell.set_namespace(self.win, "vitals-panel")
        GtkLayerShell.set_layer(self.win, GtkLayerShell.Layer.TOP)
        GtkLayerShell.set_anchor(self.win, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_anchor(self.win, GtkLayerShell.Edge.RIGHT, True)
        GtkLayerShell.set_margin(self.win, GtkLayerShell.Edge.TOP,
                                 int(pcfg.get("margin_top", 38)))
        GtkLayerShell.set_margin(self.win, GtkLayerShell.Edge.RIGHT,
                                 int(pcfg.get("margin_right", 8)))
        GtkLayerShell.set_keyboard_mode(
            self.win, GtkLayerShell.KeyboardMode.ON_DEMAND)
        self.win.connect("key-press-event", self.on_key)
        self.win.connect("delete-event", lambda *a: True)
        # close on focus-out once the user has actually interacted (DESIGN §1);
        # this gtk-layer-shell build has no set_autohide binding
        self._had_focus = False
        self.win.connect("focus-in-event", self._on_focus_in)
        self.win.connect("focus-out-event", self._on_focus_out)

        self._build_ui()
        self._load_css()

        self.dc = DaemonClient(on_message=self.on_msg,
                               on_connected=self.on_connected,
                               on_lost=self.on_daemon_lost)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.win.add(root)

        # -- header
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        head.get_style_context().add_class("section")
        head.set_margin_top(10)
        root.pack_start(head, False, False, 0)
        self.lbl_pct = Gtk.Label(label="--%")
        self.lbl_pct.get_style_context().add_class("head-pct")
        head.pack_start(self.lbl_pct, False, False, 0)
        hcol = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.lbl_state = Gtk.Label(label="Unknown")
        self.lbl_state.get_style_context().add_class("head-state")
        self.lbl_state.set_xalign(0)
        hcol.pack_start(self.lbl_state, False, False, 0)
        self.lbl_volt = Gtk.Label(label="")
        self.lbl_volt.get_style_context().add_class("dim")
        self.lbl_volt.set_xalign(0)
        hcol.pack_start(self.lbl_volt, False, False, 0)
        head.pack_start(hcol, True, True, 0)
        self.lbl_time = Gtk.Label(label="")
        self.lbl_time.get_style_context().add_class("head-time")
        self.lbl_time.set_valign(Gtk.Align.CENTER)
        head.pack_end(self.lbl_time, False, False, 0)

        # -- live power
        pw = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pw.get_style_context().add_class("section")
        root.pack_start(pw, False, False, 0)
        self.lbl_power = Gtk.Label(label="—")
        self.lbl_power.get_style_context().add_class("big")
        pw.pack_start(self.lbl_power, False, False, 0)
        self.lbl_power_src = Gtk.Label(label="")
        self.lbl_power_src.get_style_context().add_class("dim")
        pw.pack_start(self.lbl_power_src, False, False, 0)

        # -- graph + range buttons
        grow = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        grow.get_style_context().add_class("section")
        self.graph = Graph(self)
        grow.pack_start(self.graph, False, False, 0)
        rbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        rbox.set_halign(Gtk.Align.END)
        for key in ("1h", "6h"):
            b = Gtk.Button(label=key)
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.get_style_context().add_class("profile")
            if key == self.range_key:
                b.get_style_context().add_class("active")
            b.connect("clicked", lambda _b, k=key: self.set_range(k))
            rbox.pack_start(b, False, False, 0)
        grow.pack_start(rbox, False, False, 0)
        root.pack_start(grow, False, False, 0)
        self.range_buttons = rbox

        root.pack_start(self._sep(), False, False, 0)

        # -- time estimates
        self.grid_times = self._kv_grid()
        g = self.grid_times[0]
        self.t_remaining = Gtk.Label(label="—"); self.t_on_since = Gtk.Label(label="—")
        self.t_avg = Gtk.Label(label="—")
        g.attach(self._lbl("Time remaining"), 0, 0, 1, 1)
        g.attach(self.t_remaining, 1, 0, 1, 1)
        g.attach(self._lbl("Time on battery"), 0, 1, 1, 1)
        g.attach(self.t_on_since, 1, 1, 1, 1)
        g.attach(self._lbl("Avg draw (10 min)"), 0, 2, 1, 1)
        g.attach(self.t_avg, 1, 2, 1, 1)
        root.pack_start(self.grid_times[1], False, False, 0)

        root.pack_start(self._sep(), False, False, 0)

        # -- battery health
        g, box = self._kv_grid()
        self.h_health = Gtk.Label(label="—")
        self.h_design = Gtk.Label(label="—")
        self.h_cycles = Gtk.Label(label="—")
        g.attach(self._lbl("Health"), 0, 0, 1, 1)
        g.attach(self.h_health, 1, 0, 1, 1)
        g.attach(self._lbl("Design / full"), 0, 1, 1, 1)
        g.attach(self.h_design, 1, 1, 1, 1)
        g.attach(self._lbl("Cycles"), 0, 2, 1, 1)
        g.attach(self.h_cycles, 1, 2, 1, 1)
        root.pack_start(box, False, False, 0)

        root.pack_start(self._sep(), False, False, 0)

        # -- power source
        g, box = self._kv_grid()
        self.p_source = Gtk.Label(label="—")
        g.attach(self._lbl("Power source"), 0, 0, 1, 1)
        g.attach(self.p_source, 1, 0, 1, 1)
        root.pack_start(box, False, False, 0)

        # -- profile selector
        prof_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        prof_box.get_style_context().add_class("section")
        prof_box.set_halign(Gtk.Align.CENTER)
        prof_box.set_margin_top(6)
        self.profile_buttons: dict[str, Gtk.Button] = {}
        self.profile_box = prof_box
        root.pack_start(prof_box, False, False, 0)
        self.lbl_hold = Gtk.Label(label="")
        self.lbl_hold.get_style_context().add_class("dim")
        self.lbl_hold.set_halign(Gtk.Align.CENTER)
        root.pack_start(self.lbl_hold, False, False, 0)

        # -- brightness slider
        br_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        br_box.get_style_context().add_class("section")
        self.br_box = br_box
        sun = Gtk.Label(label="☀")
        sun.get_style_context().add_class("dim")
        br_box.pack_start(sun, False, False, 0)
        self.scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 1, 100, 1)
        self.scale.set_draw_value(False)
        self.scale.set_hexpand(True)
        self.scale.connect("value-changed", self.on_brightness_changed)
        self.scale.connect("button-press-event", self._on_scale_press)
        self.scale.connect("button-release-event", self._on_scale_release)
        br_box.pack_start(self.scale, True, True, 0)
        root.pack_start(br_box, False, False, 0)

        # -- footer
        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        foot.get_style_context().add_class("section")
        foot.set_margin_bottom(8)
        self.lbl_updated = Gtk.Label(label="")
        self.lbl_updated.get_style_context().add_class("dim")
        foot.pack_start(self.lbl_updated, True, True, 0)
        close = Gtk.Button(label="✕")
        close.get_style_context().add_class("close")
        close.connect("clicked", lambda *_a: self.hide())
        foot.pack_end(close, False, False, 0)
        root.pack_start(foot, False, False, 0)

    def _sep(self):
        s = Gtk.Box()
        s.get_style_context().add_class("sep")
        return s

    def _lbl(self, text):
        l = Gtk.Label(label=text)
        l.get_style_context().add_class("label")
        l.set_xalign(0)
        return l

    def _kv_grid(self):
        g = Gtk.Grid(row_spacing=2, column_spacing=14)
        g.get_style_context().add_class("section")
        box = g
        return g, box

    def _load_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # ------------------------------------------------------------ protocol
    def on_connected(self) -> None:
        self.dc.send({"v": 1, "id": "p1", "op": "subscribe",
                      "modules": ["battery", "power", "panel"]})
        self._fetch_history()

    def on_daemon_lost(self) -> None:
        self.win.hide()
        self.visible = False
        self.lbl_state.set_text("daemon offline")

    def on_msg(self, msg: dict) -> None:
        if msg.get("event") == "panel":
            vis = msg.get("data", {}).get("visible")
            if vis:
                self.show()
            else:
                self.hide()
            return
        if msg.get("ok") and isinstance(msg.get("panel"), dict):
            # subscribe ack carries current daemon-side visibility
            if msg["panel"].get("visible"):
                self.show()
            return
        data = msg.get("data")
        if not data:
            return
        if isinstance(data, dict) and data.get("module") == "battery" \
                and "series" in data:
            self.graph.set_series(data["series"], data.get("window_s", 3600))
            return
        if "battery" in data or "power" in data:
            self.update(data.get("battery", {}), data.get("power", {}))

    # -------------------------------------------------------------- update
    def update(self, b: dict, p: dict) -> None:
        state = b.get("state", "unknown")
        pct = b.get("percent")
        status = (b.get("status") or "").lower()
        self.lbl_pct.set_text("--%" if pct is None else f"{pct:.0f}%")
        labels = {"discharging": "Discharging", "charging": "Charging",
                  "full": "Fully charged", "held": "Plugged in · not charging",
                  "low": "Discharging · low", "critical": "Discharging · critical",
                  "nobattery": "No battery", "unknown": "Unknown"}
        self.lbl_state.set_text(labels.get(state, status or "Unknown"))
        if pct is not None and state in ("discharging", "low", "critical"):
            t = fmt_time(b.get("time_to_empty_s"))
            self.lbl_time.set_text(f"{t} left" if t else "estimating…")
        elif state == "charging":
            t = fmt_time(b.get("time_to_full_s"))
            self.lbl_time.set_text(f"{t} to full" if t else "estimating…")
        elif state == "full":
            self.lbl_time.set_text("Charged")
        elif state == "held":
            self.lbl_time.set_text("Held")
        else:
            self.lbl_time.set_text("")

        pw = b.get("power_w")
        if pw is None:
            self.lbl_power.set_text("—")
            self.lbl_volt.set_text("")
        else:
            word = "charging at" if state == "charging" else "drawing"
            self.lbl_power.set_text(f"{pw:.1f} W {word}")
            v, a = b.get("voltage_v"), b.get("current_a")
            self.lbl_volt.set_text(f"{v:.1f} V · {a:.2f} A" if v and a else "")
        src = "Battery" if state in ("discharging", "low", "critical") else "AC adapter"
        self.lbl_power_src.set_text(src)

        # time estimates
        if state in ("discharging", "low", "critical"):
            self.t_remaining.set_text(fmt_time(b.get("time_to_empty_s")) or "estimating…")
        elif state == "charging":
            self.t_remaining.set_text(fmt_time(b.get("time_to_full_s")) or "estimating…")
        else:
            self.t_remaining.set_text("—" if state != "full" else "Full")
        on_s = b.get("on_source_s")
        if on_s is not None:
            label = "Time on battery" if not b.get("ac_online") else "Time on AC"
            self.grid_times[0].get_child_at(0, 1).set_text(label)
            self.t_on_since.set_text(fmt_time(on_s) or "—")
        avg = b.get("avg_power_10m_w")
        self.t_avg.set_text(f"{avg:.1f} W" if avg is not None else "—")

        # health
        health = b.get("health_percent")
        if health is not None:
            wear = 100.0 - health
            warn = " · ⚠" if wear >= 20 else ""
            self.h_health.set_text(f"Health {health:.0f}% · Wear {wear:.0f}%{warn}")
        else:
            self.h_health.set_text("n/a")
        d, f = b.get("energy_full_design_wh"), b.get("energy_full_wh")
        self.h_design.set_text(
            f"{d:.1f} Wh / {f:.1f} Wh" if d and f else "n/a")
        self.h_cycles.set_text(str(b.get("cycle_count")
                                   if b.get("cycle_count") else "n/a"))

        # power source
        if b.get("ac_online"):
            extra = {"charging": "Charging", "full": "Full",
                     "held": f"Held at {pct:.0f}%" if pct else "Held"}.get(state, "")
            self.p_source.set_text(f"AC adapter{' · ' + extra if extra else ''}")
        else:
            self.p_source.set_text("Battery")

        # profile buttons
        profiles = p.get("profiles") or []
        active = p.get("active_profile")
        if list(self.profile_buttons.keys()) != profiles and profiles:
            for child in self.profile_box.get_children():
                self.profile_box.remove(child)
            self.profile_buttons = {}
            for name in profiles:
                btn = Gtk.Button(label=name.replace("-", " ").title())
                btn.get_style_context().add_class("profile")
                btn.connect("clicked",
                            lambda _b, n=name: self.set_profile(n))
                self.profile_buttons[name] = btn
                self.profile_box.pack_start(btn, False, False, 0)
            self.profile_box.show_all()
        for name, btn in self.profile_buttons.items():
            ctx = btn.get_style_context()
            if name == active:
                ctx.add_class("active")
            else:
                ctx.remove_class("active")
            btn.set_sensitive(p.get("profile_available", True))
        self.lbl_hold.set_text(f"Held by {p['hold']}" if p.get("hold") else "")

        # brightness
        bp = p.get("brightness_percent")
        self.br_box.set_visible(bool(p.get("brightness_available")))
        if bp is not None and not self._brightness_drag:
            self._setting = True
            self.scale.set_value(bp)
            self._setting = False

        self.lbl_updated.set_text(time.strftime("updated %H:%M:%S"))

    # ------------------------------------------------------------- actions
    def set_profile(self, name: str) -> None:
        self.dc.send({"v": 1, "id": "x1", "op": "controls.set",
                      "control": "power.profile", "value": name})

    def on_brightness_changed(self, scale: Gtk.Scale) -> None:
        if self._setting:
            return
        self._pending_bright = float(scale.get_value())
        if self._bright_timer:
            GLib.source_remove(self._bright_timer)
        self._bright_timer = GLib.timeout_add(120, self._commit_bright)

    _bright_timer = None
    _pending_bright = None

    def _commit_bright(self) -> bool:
        self._bright_timer = None
        if self._pending_bright is None:
            return False
        self.dc.send({"v": 1, "id": "x2", "op": "controls.set",
                      "control": "display.brightness",
                      "value": self._pending_bright})
        self._pending_bright = None
        return False

    def _on_scale_press(self, *_a):
        self._brightness_drag = True

    def _on_scale_release(self, *_a):
        self._brightness_drag = False

    def set_range(self, key: str) -> None:
        self.range_key = key
        for b in self.range_buttons.get_children():
            ctx = b.get_style_context()
            ctx.remove_class("active")
            if b.get_label() == key:
                ctx.add_class("active")
        self._fetch_history()

    def _fetch_history(self) -> None:
        self.dc.send({"v": 1, "id": "h1", "op": "history.get",
                      "module": "battery",
                      "fields": ["percent", "power_w", "charging"],
                      "window_s": RANGES[self.range_key], "max_points": 360})

    # --------------------------------------------------------- visibility
    def show(self) -> None:
        self.visible = True
        self.win.show_all()
        self.win.present()
        self._fetch_history()

    def hide(self, notify_daemon: bool = True) -> None:
        was = self.visible
        self.visible = False
        self._had_focus = False
        self.win.hide()
        if was and notify_daemon:
            # keep daemon-side visibility in sync (single-click reopen)
            self.dc.send({"v": 1, "id": "v1", "op": "panel.set",
                          "panel": "battery", "visible": False})

    def on_key(self, _w, event) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        return False

    def _on_focus_in(self, *_a):
        self._had_focus = True

    def _on_focus_out(self, *_a):
        if self.visible and self._had_focus:
            self.hide()
        return False


def main() -> int:
    Panel()  # wires itself to the daemon; starts hidden until a toggle event
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
