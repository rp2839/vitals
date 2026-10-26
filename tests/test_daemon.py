"""Daemon module registry and named-panel visibility (no sockets, no D-Bus)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from vitals import config as config_mod  # noqa: E402

try:
    from vitals import daemon as daemon_mod  # needs PyGObject
except ImportError:  # pragma: no cover
    daemon_mod = None


class FakeBrightness:
    def __init__(self, *_a, **_k):
        self.device = None
        self.available = False

    def state(self):
        return {"value": None, "available": False, "device": None,
                "min": 1.0, "max": 100.0}


class FakeClient:
    def __init__(self, subscribed=()):
        self.sent = []
        self.subscribed = set(subscribed)
        self.sub_since = 0.0

    def send(self, obj):
        self.sent.append(obj)


def make_daemon():
    with mock.patch.object(daemon_mod, "Brightness", FakeBrightness):
        d = daemon_mod.Daemon(config_mod.Config(config_mod.DEFAULTS))
    d._schedule_sample = lambda: None   # no GLib timers in tests
    return d


@unittest.skipIf(daemon_mod is None, "PyGObject unavailable")
class RegistryTests(unittest.TestCase):
    def test_builtin_modules_and_panel(self):
        d = make_daemon()
        self.assertEqual(list(d.modules), ["battery", "power"])
        self.assertEqual(d.panels, {"battery"})
        self.assertIn("battery", d.histories)

    def test_module_data_all_and_filtered(self):
        d = make_daemon()
        self.assertEqual(set(d.module_data()), {"battery", "power"})
        self.assertEqual(set(d.module_data(["power", "nope"])), {"power"})

    def test_registered_module_is_snapshotted_and_flushed(self):
        d = make_daemon()
        d.register_module("demo", lambda: {"x": 1})
        c = FakeClient()
        d.handle_request(c, "snapshot", "r1", {"modules": ["demo"]})
        self.assertEqual(c.sent[-1]["data"], {"demo": {"x": 1}})
        sub = FakeClient(subscribed={"demo"})
        d.clients.add(sub)
        d._dirty = True
        d._flush_updates()
        self.assertEqual(sub.sent[-1]["data"], {"demo": {"x": 1}})

    def test_history_unknown_module(self):
        d = make_daemon()
        c = FakeClient()
        d.handle_request(c, "history.get", "r1", {"module": "demo"})
        self.assertEqual(c.sent[-1]["error"]["code"], "unavailable")


@unittest.skipIf(daemon_mod is None, "PyGObject unavailable")
class PanelTests(unittest.TestCase):
    def setUp(self):
        self.d = make_daemon()
        self.d.register_panel("other")
        self.watcher = FakeClient(subscribed={"panel"})
        self.d.clients.add(self.watcher)
        self.c = FakeClient()

    def events(self):
        return [(e["data"]["panel"], e["data"]["visible"])
                for e in self.watcher.sent if e.get("event") == "panel"]

    def test_toggle_defaults_to_battery(self):
        self.d.handle_request(self.c, "panel.toggle", "r1", {})
        self.assertEqual(self.d.visible_panel, "battery")
        self.assertTrue(self.d.panel_visible)
        self.assertEqual(self.c.sent[-1]["data"],
                         {"panel": "battery", "visible": True})
        self.d.handle_request(self.c, "panel.toggle", "r2", {"panel": "battery"})
        self.assertIsNone(self.d.visible_panel)
        self.assertEqual(self.events(), [("battery", True), ("battery", False)])

    def test_opening_one_panel_closes_the_other(self):
        self.d.handle_request(self.c, "panel.toggle", "r1", {"panel": "battery"})
        self.d.handle_request(self.c, "panel.toggle", "r2", {"panel": "other"})
        self.assertEqual(self.d.visible_panel, "other")
        self.assertFalse(self.d.panel_visible)
        self.assertEqual(self.events(), [("battery", True), ("battery", False),
                                         ("other", True)])

    def test_hiding_a_closed_panel_leaves_the_open_one(self):
        self.d.handle_request(self.c, "panel.set", "r1",
                              {"panel": "other", "visible": True})
        self.d.handle_request(self.c, "panel.set", "r2",
                              {"panel": "battery", "visible": False})
        self.assertEqual(self.d.visible_panel, "other")
        self.assertEqual(self.c.sent[-1]["data"],
                         {"panel": "battery", "visible": False})
        self.assertEqual(self.events(), [("other", True)])

    def test_unknown_panel_rejected(self):
        self.d.handle_request(self.c, "panel.toggle", "r1", {"panel": "nope"})
        self.assertEqual(self.c.sent[-1]["error"]["code"], "invalid_value")
        self.d.handle_request(self.c, "panel.toggle", "r2", {"panel": ["x"]})
        self.assertEqual(self.c.sent[-1]["error"]["code"], "invalid_value")
        self.assertIsNone(self.d.visible_panel)

    def test_subscribe_ack_names_visible_panel(self):
        self.d.handle_request(self.c, "panel.toggle", "r1", {"panel": "other"})
        self.d.handle_request(self.c, "subscribe", "r2",
                              {"modules": ["battery", "panel"]})
        ack = self.c.sent[-1]
        self.assertEqual(ack["panel"], {"panel": "other", "visible": True})
        self.assertEqual(set(ack["data"]), {"battery"})


class ConfigPanelTests(unittest.TestCase):
    def test_panel_overlay(self):
        cfg = config_mod.Config(config_mod._merge(
            config_mod.DEFAULTS,
            {"panel": {"margin_right": 20, "bluetooth": {"margin_right": 120}}}))
        self.assertEqual(cfg.panel("battery")["margin_right"], 20)
        bt = cfg.panel("bluetooth")
        self.assertEqual(bt["margin_right"], 120)
        self.assertEqual(bt["margin_top"], 38)
        self.assertNotIn("bluetooth", cfg.panel("battery"))


if __name__ == "__main__":
    unittest.main()
