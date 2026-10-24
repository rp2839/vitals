"""Protocol framing & validation + bar rendering states."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from vitals import protocol  # noqa: E402
from vitals.clients import bar  # noqa: E402


class FrameTests(unittest.TestCase):
    def test_partial_and_multiple_frames(self):
        p = protocol.FrameParser()
        data = protocol.encode({"a": 1}) + protocol.encode({"b": 2})
        out = p.feed(data[:3])
        self.assertEqual(out, [])
        out = p.feed(data[3:])
        self.assertEqual(out, [{"a": 1}, {"b": 2}])

    def test_bad_json_raises(self):
        p = protocol.FrameParser()
        with self.assertRaises(protocol.FrameParser.ProtocolError):
            p.feed(b"{not json}\n")

    def test_oversize_raises(self):
        p = protocol.FrameParser()
        with self.assertRaises(protocol.FrameParser.ProtocolError):
            p.feed(b"x" * (protocol.MAX_FRAME + 1))

    def test_validate_request(self):
        op, rid = protocol.validate_request({"v": 1, "id": 7, "op": "ping"})
        self.assertEqual((op, rid), ("ping", 7))
        with self.assertRaises(protocol.FrameParser.ProtocolError):
            protocol.validate_request({"v": 2, "op": "ping"})
        with self.assertRaises(protocol.FrameParser.ProtocolError):
            protocol.validate_request({"v": 1})

    def test_error_reply_codes(self):
        r = protocol.error_reply("x", "bogus_code", "m")
        self.assertEqual(r["error"]["code"], "internal")


class RenderTests(unittest.TestCase):
    def test_discharging(self):
        out = bar.render({"state": "discharging", "percent": 72.4,
                          "status": "Discharging", "power_w": 8.4,
                          "time_to_empty_s": 3 * 3600 + 42 * 60},
                         {"active_profile": "balanced"})
        self.assertIn("72%", out["text"])
        self.assertIn("3:42", out["text"])
        self.assertEqual(out["class"], ["discharging"])
        self.assertEqual(out["percentage"], 72)
        self.assertIn("balanced", out["tooltip"])

    def test_low_critical_classes(self):
        out = bar.render({"state": "low", "percent": 18, "status": "Discharging"}, {})
        self.assertIn("low", out["class"])
        out = bar.render({"state": "critical", "percent": 7, "status": "Discharging"}, {})
        self.assertIn("critical", out["class"])

    def test_unknown_daemon_down(self):
        out = bar.render({}, {})
        self.assertEqual(out["alt"], "unknown")
        self.assertIn("--", out["text"])

    def test_output_is_single_json_line(self):
        out = bar.render({"state": "full", "percent": 100, "status": "Full"}, {})
        js = json.dumps(out)
        self.assertNotIn("\n", js)
        self.assertEqual(json.loads(js)["class"], ["full"])

    def test_no_battery_shows_plug(self):
        out = bar.render({"state": "nobattery", "percent": None,
                          "status": None}, {})
        self.assertEqual(out["class"], ["nobattery"])


if __name__ == "__main__":
    unittest.main()
