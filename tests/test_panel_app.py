"""PanelApp message routing with fake views (no windows are created)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from vitals.clients import panel as panel_mod  # needs GTK3 + layer-shell
except (ImportError, ValueError) as e:  # pragma: no cover
    panel_mod = None
    _skip = str(e)


class FakeView:
    history_module = None

    def __init__(self, app):
        self.app = app
        self.visible = False
        self.data_calls = 0
        self.history = []

    def on_data(self):
        self.data_calls += 1

    def on_history(self, data):
        self.history.append(data)

    def show(self):
        self.visible = True

    def hide(self, notify_daemon=True):
        self.visible = False

    def on_daemon_lost(self):
        self.visible = False


class FakeA(FakeView):
    name = "a"
    modules = ("battery", "power")
    history_module = "battery"


class FakeB(FakeView):
    name = "b"
    modules = ("other",)


class FakeDC:
    def __init__(self):
        self.sent = []

    def send(self, obj):
        self.sent.append(obj)


@unittest.skipIf(panel_mod is None, "GTK3/gtk-layer-shell unavailable")
class PanelAppTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(panel_mod, "PANELS", {"a": FakeA, "b": FakeB})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = panel_mod.PanelApp.__new__(panel_mod.PanelApp)
        self.app.views = {}
        self.app.cache = {}
        self.app.dc = FakeDC()

    def test_subscribes_to_union_of_modules(self):
        self.app.on_connected()
        self.assertEqual(self.app.dc.sent[-1]["modules"],
                         ["battery", "other", "power", "panel"])

    def test_views_created_lazily_and_one_visible(self):
        self.assertEqual(self.app.views, {})
        self.app.on_msg({"event": "panel", "data": {"panel": "a", "visible": True}})
        self.assertTrue(self.app.views["a"].visible)
        self.app.on_msg({"event": "panel", "data": {"panel": "b", "visible": True}})
        self.assertFalse(self.app.views["a"].visible)
        self.assertTrue(self.app.views["b"].visible)
        self.app.on_msg({"event": "panel", "data": {"panel": "zzz", "visible": True}})
        self.assertNotIn("zzz", self.app.views)

    def test_ack_snapshot_cached_before_show(self):
        self.app.on_msg({"ok": True, "id": "p1",
                         "data": {"battery": {"percent": 50}, "power": {}},
                         "panel": {"panel": "a", "visible": True}})
        v = self.app.views["a"]
        self.assertTrue(v.visible)
        self.assertEqual(self.app.cache["battery"], {"percent": 50})
        self.assertEqual(v.data_calls, 1)   # rendered on creation from cache

    def test_updates_routed_by_module(self):
        self.app.set_visible("a", True)
        self.app.set_visible("b", True)
        a, b = self.app.views["a"], self.app.views["b"]
        self.app.on_msg({"event": "update", "data": {"other": {"x": 1}}})
        self.assertEqual((a.data_calls, b.data_calls), (0, 1))
        # control replies carry non-module data and are ignored
        self.app.on_msg({"ok": True, "data": {"control": "power.profile",
                                              "value": "balanced"}})
        self.assertEqual((a.data_calls, b.data_calls), (0, 1))

    def test_history_routed_to_owner(self):
        self.app.set_visible("a", True)
        self.app.on_msg({"ok": True, "data": {"module": "battery",
                                              "series": {}, "window_s": 60}})
        self.assertEqual(len(self.app.views["a"].history), 1)


if __name__ == "__main__":
    unittest.main()
