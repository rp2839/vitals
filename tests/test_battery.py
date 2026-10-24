"""Battery collector: parsing, classification, estimation (pure-python)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import make_ps  # noqa: E402
from vitals.linux.sysfs import PowerSupplyReader  # noqa: E402
from vitals.collectors.battery import (BatteryEstimator, classify_state,
                                       fmt_time_remaining)  # noqa: E402

Path_ = Path

HEALTHY = {
    "present": 1, "status": "Discharging", "capacity": 73,
    "energy_now": 35_100_000, "energy_full": 48_000_000,
    "energy_full_design": 55_000_000, "power_now": 8_400_000,
    "voltage_now": 12_100_000, "cycle_count": 214,
    "technology": "Li-ion", "manufacturer": "TestCo",
}


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class ReaderTests(unittest.TestCase):
    def test_read_all_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_ps(Path_(td), dict(HEALTHY))
            r = PowerSupplyReader(root)
            self.assertEqual(r.find_battery(), "BAT0")
            snap = r.read_battery("BAT0")
            self.assertEqual(snap.energy_now, 35_100_000)
            self.assertEqual(snap.status, "Discharging")
            self.assertTrue(r.ac_online())

    def test_ac_offline_and_missing_battery(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_ps(Path_(td), None, ac_online=0)
            r = PowerSupplyReader(root)
            self.assertIsNone(r.find_battery())
            self.assertFalse(r.ac_online())

    def test_malformed_values_tolerated(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_ps(Path_(td), dict(HEALTHY, power_now=""))
            r = PowerSupplyReader(root)
            snap = r.read_battery("BAT0")
            self.assertIsNone(snap.power_now)


class EstimatorTests(unittest.TestCase):
    def feed_one(self, battery, ac=False, clock=None):
        clock = clock or FakeClock()
        with tempfile.TemporaryDirectory() as td:
            root = make_ps(Path_(td), dict(battery),
                           ac_online=1 if ac else 0)
            r = PowerSupplyReader(root)
            est = BatteryEstimator(mono=clock)
            return est.feed(r.read_battery("BAT0"), r.ac_online())

    def test_units_and_percent_and_health(self):
        st = self.feed_one(HEALTHY, ac=False)
        self.assertAlmostEqual(st["percent"], 100 * 35.1 / 48.0, places=1)
        self.assertAlmostEqual(st["energy_wh"], 35.1, places=2)
        self.assertAlmostEqual(st["health_percent"], 100 * 48 / 55, places=1)
        self.assertAlmostEqual(st["power_w"], 8.4, places=1)
        self.assertEqual(st["power_source"], "power_now")
        self.assertEqual(st["cycle_count"], 214)
        self.assertEqual(st["state"], "discharging")

    def test_time_to_empty(self):
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as td:
            root = make_ps(Path_(td), dict(HEALTHY))
            r = PowerSupplyReader(root)
            est = BatteryEstimator(mono=clock)
            est.feed(r.read_battery("BAT0"), r.ac_online())
            clock.advance(20)  # past the 15s post-transition suppression
            st = est.feed(r.read_battery("BAT0"), r.ac_online())
        # 35.1 Wh at 8.4 W -> ~4.18 h
        self.assertIsNotNone(st["time_to_empty_s"])
        self.assertAlmostEqual(st["time_to_empty_s"] / 3600, 35.1 / 8.4, delta=0.05)

    def test_estimate_suppressed_right_after_start(self):
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as td:
            root = make_ps(Path_(td), dict(HEALTHY))
            r = PowerSupplyReader(root)
            est = BatteryEstimator(mono=clock)
            st = est.feed(r.read_battery("BAT0"), r.ac_online())
            self.assertIsNone(st["time_to_empty_s"])  # <15s after transition

    def test_low_and_critical_thresholds(self):
        low = dict(HEALTHY, energy_now=9_000_000)   # 18.75%
        crit = dict(HEALTHY, energy_now=4_000_000)  # 8.3%
        self.assertEqual(self.feed_one(low)["state"], "low")
        self.assertEqual(self.feed_one(crit)["state"], "critical")
        # charging at 8% is 'charging', not critical
        st = self.feed_one(dict(crit, status="Charging"), ac=True)
        self.assertEqual(st["state"], "charging")

    def test_held_and_full(self):
        st = self.feed_one(dict(HEALTHY, status="Not charging",
                                energy_now=38_400_000), ac=True)  # 80%
        self.assertEqual(st["state"], "held")
        st = self.feed_one(dict(HEALTHY, status="Not charging",
                                energy_now=46_500_000), ac=True)  # ~97%
        self.assertEqual(st["state"], "full")
        st = self.feed_one(dict(HEALTHY, status="Full"), ac=True)
        self.assertEqual(st["state"], "full")

    def test_no_battery_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_ps(Path_(td), None, ac_online=1)
            r = PowerSupplyReader(root)
            est = BatteryEstimator()
            st = est.feed(None, True)
            self.assertEqual(st["state"], "nobattery")
            self.assertEqual(st["quality"], "unavailable")

    def test_charge_based_fallback(self):
        # charge_* are µAh on Linux; energy = µAh x µV / 1e6 = µWh
        b = {"present": 1, "status": "Discharging", "capacity": 50,
             "charge_now": 2_500_000, "charge_full": 5_000_000,
             "charge_full_design": 5_200_000, "voltage_now": 11_000_000,
             "power_now": 10_000_000}
        st = self.feed_one(b)
        self.assertAlmostEqual(st["percent"], 50.0, places=1)
        self.assertAlmostEqual(st["energy_wh"], 27.5, places=1)
        self.assertAlmostEqual(st["energy_full_wh"], 55.0, places=1)
        self.assertAlmostEqual(st["health_percent"], 100 * 5000 / 5200, places=1)

    def test_stale_power_not_used_for_estimates(self):
        # measured power, then power_now disappears: estimates must stop,
        # not ride on the stale EMA (review finding, Codex major)
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as td:
            root = make_ps(Path_(td), dict(HEALTHY))
            r = PowerSupplyReader(root)
            est = BatteryEstimator(mono=clock)
            est.feed(r.read_battery("BAT0"), r.ac_online())
            clock.advance(20)
            (root / "BAT0" / "power_now").write_text("0\n")
            st = est.feed(r.read_battery("BAT0"), r.ac_online())
            self.assertIsNone(st["power_raw_w"])
            self.assertIsNone(st["time_to_empty_s"])
            self.assertTrue(st["estimating"])

    def test_suspend_gap_clears_window(self):
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as td:
            bat = dict(HEALTHY)
            del bat["power_now"]
            root = make_ps(Path_(td), dict(bat))
            r = PowerSupplyReader(root)
            est = BatteryEstimator(mono=clock)
            for _ in range(5):
                est.feed(r.read_battery("BAT0"), r.ac_online())
                clock.advance(5)
            clock.advance(3600)  # suspend
            st = est.feed(r.read_battery("BAT0"), r.ac_online())
            self.assertIsNone(st["power_raw_w"])

    def test_configured_thresholds(self):
        cfg = {"low_percent": 30, "critical_percent": 15}
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as td:
            root = make_ps(Path_(td), dict(HEALTHY, energy_now=13_000_000))  # 27%
            r = PowerSupplyReader(root)
            est = BatteryEstimator(cfg, mono=clock)
            st = est.feed(r.read_battery("BAT0"), r.ac_online())
            self.assertEqual(st["state"], "low")

    def test_device_loss_resets_state(self):
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as td:
            root = make_ps(Path_(td), dict(HEALTHY))
            r = PowerSupplyReader(root)
            est = BatteryEstimator(mono=clock)
            est.feed(r.read_battery("BAT0"), r.ac_online())
            st = est.feed(None, True)
            self.assertEqual(st["state"], "nobattery")
            st = est.feed(r.read_battery("BAT0"), r.ac_online())
            # fresh estimator: no stale power carried over
            self.assertIsNone(st["time_to_empty_s"])

    def test_zero_power_suppresses_estimate(self):
        st = self.feed_one(dict(HEALTHY, power_now=0))
        # power_now=0 -> derived needs >=3 samples over 45s; nothing yet
        self.assertIsNone(st["time_to_empty_s"])

    def test_derived_power_fallback(self):
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as td:
            bat = dict(HEALTHY)
            del bat["power_now"]
            root = make_ps(Path_(td), dict(bat))
            r = PowerSupplyReader(root)
            est = BatteryEstimator(mono=clock)
            e = 35_100_000
            st = None
            for _ in range(6):  # 6 samples, 30s apart -> 150s span
                (root / "BAT0" / "energy_now").write_text(f"{e}\n")
                st = est.feed(r.read_battery("BAT0"), r.ac_online())
                clock.advance(30)
                e -= 70_000  # 0.07 Wh/30s = 8.4 W
            self.assertEqual(st["power_source"], "derived")
            self.assertAlmostEqual(st["power_raw_w"], 8.4, delta=0.5)

    def test_health_above_100_not_clamped(self):
        st = self.feed_one(dict(HEALTHY, energy_full=56_000_000))
        self.assertGreater(st["health_percent"], 100.0)

    def test_charge_taper_above_80(self):
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as td:
            bat = dict(HEALTHY, status="Charging", energy_now=40_000_000,
                      energy_full=48_000_000, power_now=24_000_000)
            root = make_ps(Path_(td), dict(bat))
            r = PowerSupplyReader(root)
            est = BatteryEstimator({"taper_factor": 1.5}, mono=clock)
            st = None
            for _ in range(20):
                st = est.feed(r.read_battery("BAT0"), True)
                clock.advance(5)
            # 83% charged -> taper applied: 8Wh/24W*1.5 = 0.5h
            self.assertIsNotNone(st["time_to_full_s"])
            self.assertAlmostEqual(st["time_to_full_s"] / 3600, 0.5, delta=0.1)


class ClassifyTests(unittest.TestCase):
    def test_classify_table(self):
        self.assertEqual(classify_state("Discharging", False, 55), "discharging")
        self.assertEqual(classify_state("Discharging", False, 20), "low")
        self.assertEqual(classify_state("Discharging", False, 10), "critical")
        self.assertEqual(classify_state("Charging", True, 8), "charging")
        self.assertEqual(classify_state("Full", True, 100), "full")
        self.assertEqual(classify_state("Not charging", True, 80), "held")
        self.assertEqual(classify_state(None, None, None), "unknown")


class FmtTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(fmt_time_remaining(3 * 3600 + 42 * 60), "3:42")
        self.assertEqual(fmt_time_remaining(26 * 3600), "1d 2h")
        self.assertEqual(fmt_time_remaining(45), "<1m")
        self.assertIsNone(fmt_time_remaining(None))


if __name__ == "__main__":
    unittest.main()
