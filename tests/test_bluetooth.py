"""Bluetooth model, snapshot, indicator, rfkill and daemon actions."""
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp()

from vitals import config as config_mod  # noqa: E402
from vitals.clients import bar  # noqa: E402
from vitals.collectors import bluetooth as bt  # noqa: E402
from vitals.linux import rfkill  # noqa: E402

try:
    from vitals import daemon as daemon_mod  # needs PyGObject
except ImportError:  # pragma: no cover
    daemon_mod = None

HCI = "/org/bluez/hci0"
MOUSE = HCI + "/dev_D3_3B_0A_10_24_0D"
PHONES = HCI + "/dev_AA_BB_CC_DD_EE_01"


def managed():
    return {
        "/org/bluez": {"org.bluez.AgentManager1": {}},
        HCI: {bt.ADAPTER: {"Address": "38:8D:3D:89:67:A7", "Alias": "fedora",
                           "Powered": True, "PowerState": "on",
                           "Discovering": False}},
        MOUSE: {bt.DEVICE: {"Address": "D3:3B:0A:10:24:0D", "Alias": "M585/M590",
                            "Icon": "input-mouse", "Paired": True,
                            "Trusted": True, "Connected": True,
                            "Adapter": HCI, "UUIDs": ["00001812-0000-1000-8000-00805f9b34fb"]},
                bt.BATTERY: {"Percentage": 41, "Source": "GATT Battery Service"}},
        MOUSE + "/service0001": {"org.bluez.GattService1": {"Primary": True}},
        PHONES: {bt.DEVICE: {"Address": "AA:BB:CC:DD:EE:01", "Alias": "Headphones",
                             "Paired": True, "Connected": False, "Adapter": HCI,
                             "UUIDs": ["0000110b-0000-1000-8000-00805f9b34fb",
                                       "00001105-0000-1000-8000-00805f9b34fb"]}},
    }


def model():
    m = bt.BluezModel()
    m.reset(managed())
    return m


class ModelTests(unittest.TestCase):
    def test_tracks_only_relevant_interfaces(self):
        m = model()
        self.assertNotIn(MOUSE + "/service0001", m.objects)
        self.assertEqual(m.adapter_path(), HCI)
        self.assertEqual(len(m.devices()), 2)

    def test_signal_relevance(self):
        m = model()
        self.assertFalse(m.properties_changed(
            MOUSE + "/service0001/char0002", "org.bluez.GattCharacteristic1",
            {"Value": [1]}))
        self.assertTrue(m.properties_changed(MOUSE, bt.BATTERY, {"Percentage": 12}))
        self.assertEqual(m.objects[MOUSE][bt.BATTERY]["Percentage"], 12)
        self.assertFalse(m.interfaces_added(MOUSE + "/x", {"org.bluez.MediaTransport1": {}}))

    def test_remove_and_invalidate(self):
        m = model()
        self.assertTrue(m.interfaces_removed(MOUSE, [bt.BATTERY]))
        self.assertNotIn(bt.BATTERY, m.objects[MOUSE])
        m.properties_changed(PHONES, bt.DEVICE, {}, ["RSSI", "Alias"])
        self.assertNotIn("Alias", m.objects[PHONES][bt.DEVICE])
        self.assertTrue(m.interfaces_removed(PHONES, [bt.DEVICE]))
        self.assertNotIn(PHONES, m.objects)
        self.assertFalse(m.interfaces_removed("/nope", [bt.DEVICE]))

    def test_device_path_case_insensitive(self):
        self.assertEqual(model().device_path("d3:3b:0a:10:24:0d"), MOUSE)
        self.assertIsNone(model().device_path("00:00:00:00:00:00"))


class SnapshotTests(unittest.TestCase):
    def test_devices_sorted_with_flags(self):
        s = bt.snapshot(model(), True, "none", {"AA:BB:CC:DD:EE:01": "connecting"})
        self.assertEqual(s["adapter"]["alias"], "fedora")
        names = [d["alias"] for d in s["devices"]]
        self.assertEqual(names, ["M585/M590", "Headphones"])  # connected first
        mouse, phones = s["devices"]
        self.assertEqual(mouse["battery"], 41)
        self.assertFalse(mouse["audio"])
        self.assertTrue(phones["audio"] and phones["opp"])
        self.assertEqual(phones["busy"], "connecting")

    def test_unpaired_unconnected_hidden(self):
        m = model()
        m.properties_changed(PHONES, bt.DEVICE, {"Paired": False})
        self.assertEqual(len(bt.snapshot(m, True, "none")["devices"]), 1)

    def test_unavailable(self):
        s = bt.snapshot(model(), False, "none")
        self.assertIsNone(s["adapter"])
        self.assertEqual(s["devices"], [])


class LastConnectedTests(unittest.TestCase):
    def test_transitions_and_persistence(self):
        path = Path(tempfile.mkdtemp()) / "sub" / "bluetooth.json"
        lc = bt.LastConnected(path)
        # first update: already-connected devices get a time only if unknown
        self.assertTrue(lc.update({"aa:bb:cc:dd:ee:01"}, now=100))
        self.assertEqual(lc.times, {"AA:BB:CC:DD:EE:01": 100})
        self.assertFalse(lc.update({"AA:BB:CC:DD:EE:01"}, now=200))   # still connected
        self.assertFalse(lc.update(set(), now=300))                   # disconnect
        self.assertTrue(lc.update({"AA:BB:CC:DD:EE:01"}, now=400))    # reconnect
        self.assertEqual(bt.LastConnected(path).times, {"AA:BB:CC:DD:EE:01": 400})

    def test_restart_keeps_known_times(self):
        path = Path(tempfile.mkdtemp()) / "bluetooth.json"
        path.write_text('{"AA:BB:CC:DD:EE:01": 50, "junk": "x"}')
        lc = bt.LastConnected(path)
        self.assertEqual(lc.times, {"AA:BB:CC:DD:EE:01": 50})
        self.assertFalse(lc.update({"AA:BB:CC:DD:EE:01"}, now=999))
        path.write_text("not json")
        self.assertEqual(bt.LastConnected(path).times, {})

    def test_snapshot_and_sections_order(self):
        m = model()
        extra = HCI + "/dev_AA_BB_CC_DD_EE_02"
        m.interfaces_added(extra, {bt.DEVICE: {"Address": "AA:BB:CC:DD:EE:02",
                                               "Alias": "Keyboard", "Paired": True,
                                               "Adapter": HCI}})
        never = HCI + "/dev_AA_BB_CC_DD_EE_03"
        m.interfaces_added(never, {bt.DEVICE: {"Address": "AA:BB:CC:DD:EE:03",
                                               "Alias": "Aardvark", "Paired": True,
                                               "Adapter": HCI}})
        snap = bt.snapshot(m, True, "none", last_connected={
            "AA:BB:CC:DD:EE:01": 1000.0, "AA:BB:CC:DD:EE:02": 2000.0})
        connected, paired = bt.sections(snap)
        self.assertEqual([d["alias"] for d in connected], ["M585/M590"])
        self.assertEqual([d["alias"] for d in paired],
                         ["Keyboard", "Headphones", "Aardvark"])
        self.assertEqual(paired[0]["last_connected"], "1970-01-01T00:33:20Z")


class ClassifyTests(unittest.TestCase):
    def snap(self, **adapter):
        m = model()
        if adapter:
            m.properties_changed(HCI, bt.ADAPTER, adapter)
        return m

    def test_states(self):
        cases = [
            (bt.snapshot(model(), False, "none"), "unknown"),
            (bt.snapshot(model(), True, "hard"), "blocked"),
            (bt.snapshot(model(), True, "soft"), "off"),
            (bt.snapshot(self.snap(Powered=False), True, "none"), "off"),
            (bt.snapshot(bt.BluezModel(), True, "absent"), "noadapter"),
            (bt.snapshot(model(), True, "none"), "connected"),
        ]
        for snap, want in cases:
            self.assertEqual(bt.classify(snap)[0], want)
        m = model()
        m.properties_changed(MOUSE, bt.DEVICE, {"Connected": False})
        self.assertEqual(bt.classify(bt.snapshot(m, True, "none"))[0], "on")

    def test_extra_classes(self):
        m = model()
        m.properties_changed(PHONES, bt.DEVICE, {"Connected": True})
        m.properties_changed(MOUSE, bt.BATTERY, {"Percentage": 15})
        m.properties_changed(HCI, bt.ADAPTER, {"Discovering": True})
        _, classes = bt.classify(bt.snapshot(m, True, "none"))
        self.assertEqual(classes, ["connected", "audio", "lowbattery", "busy"])


class ErrorMapTests(unittest.TestCase):
    def test_map(self):
        self.assertEqual(bt.map_error("org.bluez.Error.AuthenticationFailed"), "auth_failed")
        self.assertEqual(bt.map_error("org.bluez.Error.InProgress"), "busy")
        self.assertEqual(bt.map_error("org.bluez.Error.Failed", "Host is down"), "unreachable")
        self.assertEqual(bt.map_error("org.bluez.Error.Failed",
                                      "br-connection-profile-unavailable"),
                         "profile_unavailable")
        self.assertEqual(bt.map_error("org.bluez.Error.Failed", "Blocked through rfkill"),
                         "blocked")
        self.assertEqual(bt.map_error("org.freedesktop.DBus.Error.NoReply"), "timeout")
        self.assertEqual(bt.map_error("org.bluez.Error.Failed", "???"), "unavailable")


class RfkillTests(unittest.TestCase):
    def tree(self, *switches):
        root = Path(tempfile.mkdtemp())
        for i, (kind, soft, hard) in enumerate(switches):
            d = root / f"rfkill{i}"
            d.mkdir()
            (d / "type").write_text(kind + "\n")
            (d / "soft").write_text(f"{soft}\n")
            (d / "hard").write_text(f"{hard}\n")
        return root

    def test_state(self):
        self.assertEqual(rfkill.state(root=self.tree(("bluetooth", 0, 0),
                                                     ("wlan", 1, 0))), "none")
        self.assertEqual(rfkill.state(root=self.tree(("bluetooth", 1, 0))), "soft")
        self.assertEqual(rfkill.state(root=self.tree(("bluetooth", 1, 1))), "hard")
        self.assertEqual(rfkill.state(root=self.tree(("wlan", 0, 0))), "absent")
        self.assertEqual(rfkill.state(root=Path("/nonexistent")), "absent")

    def test_unblock_writes_change_all_event(self):
        with tempfile.NamedTemporaryFile() as f:
            ok, err = rfkill.unblock(dev=f.name)
            self.assertTrue(ok)
            data = Path(f.name).read_bytes()
        self.assertEqual(struct.unpack("=IBBBB", data),
                         (0, rfkill.TYPE_BLUETOOTH, rfkill.OP_CHANGE_ALL, 0, 0))
        self.assertEqual(rfkill.unblock(dev="/nonexistent/rfkill"), (False, "unavailable"))


class RenderTests(unittest.TestCase):
    cfg = dict(config_mod.DEFAULTS["bluetooth"])

    def render(self, snap, **cfg):
        return bar.render_bluetooth({"bluetooth": snap}, {**self.cfg, **cfg})

    def test_connected_single_no_count(self):
        out = self.render(bt.snapshot(model(), True, "none"))
        self.assertEqual(out["text"], bar.BT_CONNECTED)
        self.assertEqual(out["alt"], "connected")
        self.assertIn("M585/M590 · 41%", out["tooltip"])
        self.assertTrue(out["tooltip"].startswith("fedora · On"))

    def test_count_audio_and_battery(self):
        m = model()
        m.properties_changed(PHONES, bt.DEVICE, {"Connected": True})
        out = self.render(bt.snapshot(m, True, "none"), show_battery=True)
        self.assertEqual(out["text"], f"{bar.BT_AUDIO} 2 · 41%")
        self.assertEqual(out["percentage"], 41)

    def test_low_battery_forces_field(self):
        m = model()
        m.properties_changed(MOUSE, bt.BATTERY, {"Percentage": 9})
        out = self.render(bt.snapshot(m, True, "none"))
        self.assertEqual(out["text"], f"{bar.BT_CONNECTED} · 9%")
        self.assertIn("lowbattery", out["class"])

    def test_off_blocked_unknown_noadapter(self):
        self.assertEqual(self.render(bt.snapshot(model(), True, "soft"))["text"], bar.BT_OFF)
        self.assertEqual(self.render(bt.snapshot(model(), True, "hard"))["text"],
                         f"{bar.BT_OFF} blocked")
        self.assertEqual(bar.render_bluetooth({})["text"], f"{bar.BT_OFF} --")
        none = bt.snapshot(bt.BluezModel(), True, "absent")
        self.assertEqual(self.render(none)["text"], bar.BT_OFF)
        self.assertEqual(self.render(none, hide_when_no_adapter=True)["text"], "")

    def test_ascii(self):
        with mock.patch.dict(os.environ, {"VITALS_ASCII": "1"}):
            self.assertEqual(self.render(bt.snapshot(model(), True, "none"))["text"], "BT+")


class FakeBackend:
    def __init__(self):
        self.model = model()
        self.available = True
        self.calls = []

    def set_powered(self, on, done):
        self.calls.append(("power", on, done))

    def device_call(self, mac, method, timeout_ms, done):
        self.calls.append((method, mac, done))

    def stop(self):
        pass


class FakeClient:
    def __init__(self):
        self.sent = []
        self.subscribed = set()
        self.sub_since = 0.0
        self.alive = True

    def send(self, obj):
        self.sent.append(obj)


class FakeBrightness:
    def __init__(self, *_a, **_k):
        self.device = None
        self.available = False

    def state(self):
        return {"value": None, "available": False, "device": None,
                "min": 1.0, "max": 100.0}


@unittest.skipIf(daemon_mod is None, "PyGObject unavailable")
class DaemonBluetoothTests(unittest.TestCase):
    def setUp(self):
        with mock.patch.object(daemon_mod, "Brightness", FakeBrightness), \
                mock.patch.object(daemon_mod.rfkill_mod, "state", return_value="none"):
            self.d = daemon_mod.Daemon(config_mod.Config(config_mod.DEFAULTS))
        self.d.bluez = FakeBackend()
        self.d._on_bt_changed = lambda: None
        self.c = FakeClient()

    def act(self, action, device):
        self.d.handle_request(self.c, "action", "r1",
                              {"action": action, "args": {"device": device}})
        return self.c.sent[-1] if self.c.sent else None

    def test_validation(self):
        self.assertEqual(self.act("bluetooth.explode", "D3:3B:0A:10:24:0D")
                         ["error"]["code"], "unknown_action")
        self.assertEqual(self.act("bluetooth.connect", "not-a-mac")
                         ["error"]["code"], "invalid_value")
        self.assertEqual(self.act("bluetooth.connect", "00:11:22:33:44:55")
                         ["error"]["code"], "invalid_value")
        self.d.handle_request(self.c, "action", "r2",
                              {"action": "bluetooth.connect", "args": ["x"]})
        self.assertEqual(self.c.sent[-1]["error"]["code"], "invalid_value")
        self.assertEqual(self.d.bluez.calls, [])

    def test_connect_busy_then_reply(self):
        self.assertIsNone(self.act("bluetooth.connect", "aa:bb:cc:dd:ee:01"))
        method, mac, done = self.d.bluez.calls[-1]
        self.assertEqual((method, mac), ("Connect", "AA:BB:CC:DD:EE:01"))
        snap = self.d.module_data(["bluetooth"])["bluetooth"]
        self.assertEqual(snap["devices"][1]["busy"], "connecting")
        self.assertEqual(self.act("bluetooth.disconnect", "AA:BB:CC:DD:EE:01")
                         ["error"]["code"], "busy")
        done(False, "unreachable")
        self.assertEqual(self.c.sent[-1]["error"]["code"], "unreachable")
        self.assertEqual(self.d.bt_busy, {})

    def test_power(self):
        self.d.handle_request(self.c, "controls.set", "r1",
                              {"control": "bluetooth.power", "value": "on"})
        self.assertEqual(self.c.sent[-1]["error"]["code"], "invalid_value")
        self.d.handle_request(self.c, "controls.set", "r2",
                              {"control": "bluetooth.power", "value": False})
        _, on, done = self.d.bluez.calls[-1]
        self.assertFalse(on)
        done(True, None)
        self.assertEqual(self.c.sent[-1]["data"],
                         {"control": "bluetooth.power", "value": False})

    def test_power_hard_blocked(self):
        self.d.bt_rfkill = "hard"
        self.d.handle_request(self.c, "controls.set", "r1",
                              {"control": "bluetooth.power", "value": True})
        self.assertEqual(self.c.sent[-1]["error"]["code"], "blocked")
        self.assertFalse(self.d._controls_get()["bluetooth.power"]["available"])

    def test_power_on_soft_blocked_unblocks_first(self):
        self.d.bt_rfkill = "soft"
        with mock.patch.object(daemon_mod.rfkill_mod, "unblock",
                               return_value=(True, None)) as unblock, \
                mock.patch.object(daemon_mod.rfkill_mod, "state", return_value="none"), \
                mock.patch.object(daemon_mod.GLib, "timeout_add",
                                  side_effect=lambda _ms, fn: fn()):
            self.d.handle_request(self.c, "controls.set", "r1",
                                  {"control": "bluetooth.power", "value": True})
        unblock.assert_called_once()
        self.assertEqual(self.d.bt_rfkill, "none")
        self.assertEqual(self.d.bluez.calls[-1][:2], ("power", True))


if __name__ == "__main__":
    unittest.main()
