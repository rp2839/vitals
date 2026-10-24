"""Notification episode logic (SPEC §4): startup seeding, hysteresis,
once-per-episode, reset on plug-in."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from vitals import notify  # noqa: E402


def state(pct, status, ac, power=8.0):
    return {"percent": pct, "status": status, "ac_online": ac,
            "power_w": power, "time_to_empty_s": 3000}


class EpisodeTests(unittest.TestCase):
    def setUp(self):
        self.patcher = mock.patch.object(notify, "_send", return_value=True)
        self.send = self.patcher.start()
        self.n = notify.BatteryNotifier(
            {"low": True, "critical": True}, {"low_percent": 20, "critical_percent": 10})

    def tearDown(self):
        self.patcher.stop()

    def feed(self, *states):
        for s in states:
            self.n.update(s)

    def test_no_startup_fire_when_already_low(self):
        # SPEC §4 Startup: no notification for the initial state
        self.feed(state(15, "Discharging", False))
        self.send.assert_not_called()

    def test_low_fires_once_on_crossing(self):
        self.feed(state(50, "Discharging", False))   # startup sample
        self.feed(state(21, "Discharging", False))
        self.feed(state(19, "Discharging", False))   # crossing -> fire
        self.feed(state(18, "Discharging", False))   # no repeat
        self.assertEqual(self.send.call_count, 1)

    def test_hysteresis_reset(self):
        self.feed(state(50, "Discharging", False))
        self.feed(state(19, "Discharging", False))   # fire
        self.feed(state(21, "Discharging", False))   # inside hysteresis: no reset
        self.feed(state(19, "Discharging", False))   # still one episode
        self.assertEqual(self.send.call_count, 1)
        self.feed(state(24, "Discharging", False))   # above 20+3: reset
        self.feed(state(19, "Discharging", False))   # new episode
        self.assertEqual(self.send.call_count, 2)

    def test_plug_in_resets_episode(self):
        self.feed(state(50, "Discharging", False))
        self.feed(state(19, "Discharging", False))   # fire
        self.feed(state(19, "Charging", True))       # reset
        self.feed(state(19, "Discharging", False))   # new episode on unplug
        self.assertEqual(self.send.call_count, 2)

    def test_critical_fires_independently(self):
        self.feed(state(50, "Discharging", False))
        self.feed(state(19, "Discharging", False))   # low
        self.feed(state(9, "Discharging", False))    # critical
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.send.call_args_list[1].args[0], "critical")

    def test_charging_low_does_not_fire(self):
        self.feed(state(50, "Discharging", False))
        self.feed(state(8, "Charging", True))
        self.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
