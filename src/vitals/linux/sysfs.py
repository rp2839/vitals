"""Power-supply sysfs reader. `root` is injectable so tests can use fixtures."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _read_int(path: Path) -> Optional[int]:
    try:
        s = path.read_text().strip()
        return int(s) if s else None
    except (OSError, ValueError):
        return None


def _read_str(path: Path) -> Optional[str]:
    try:
        s = path.read_text().strip()
        return s or None
    except OSError:
        return None


@dataclass
class BatterySnapshot:
    """Raw values as read from sysfs (SI-ish units, unconverted)."""
    name: str
    present: Optional[int] = None
    status: Optional[str] = None
    capacity: Optional[int] = None            # %
    capacity_level: Optional[str] = None
    energy_now: Optional[int] = None          # µWh
    energy_full: Optional[int] = None         # µWh
    energy_full_design: Optional[int] = None  # µWh
    charge_now: Optional[int] = None          # mAh (charge-based batteries)
    charge_full: Optional[int] = None
    charge_full_design: Optional[int] = None
    power_now: Optional[int] = None           # µW
    voltage_now: Optional[int] = None         # µV
    cycle_count: Optional[int] = None
    technology: Optional[str] = None
    manufacturer: Optional[str] = None
    model_name: Optional[str] = None


class PowerSupplyReader:
    def __init__(self, root: str | Path = "/sys/class/power_supply"):
        self.root = Path(root)

    def devices(self) -> list[str]:
        try:
            return sorted(p.name for p in self.root.iterdir() if p.is_dir())
        except OSError:
            return []

    def find_battery(self, want: str = "auto") -> Optional[str]:
        """Return battery device name. want='auto' picks the first type=Battery."""
        for name in self.devices():
            if want != "auto" and name != want:
                continue
            if _read_str(self.root / name / "type") == "Battery":
                return name
        return None

    def read_battery(self, name: str) -> Optional[BatterySnapshot]:
        d = self.root / name
        if not d.is_dir():
            return None
        snap = BatterySnapshot(
            name=name,
            present=_read_int(d / "present"),
            status=_read_str(d / "status"),
            capacity=_read_int(d / "capacity"),
            capacity_level=_read_str(d / "capacity_level"),
            energy_now=_read_int(d / "energy_now"),
            energy_full=_read_int(d / "energy_full"),
            energy_full_design=_read_int(d / "energy_full_design"),
            charge_now=_read_int(d / "charge_now"),
            charge_full=_read_int(d / "charge_full"),
            charge_full_design=_read_int(d / "charge_full_design"),
            power_now=_read_int(d / "power_now"),
            voltage_now=_read_int(d / "voltage_now"),
            cycle_count=_read_int(d / "cycle_count"),
            technology=_read_str(d / "technology"),
            manufacturer=_read_str(d / "manufacturer"),
            model_name=_read_str(d / "model_name"),
        )
        return snap

    def ac_online(self, name: str = "auto") -> Optional[bool]:
        """True/False if a Mains device reports, None if absent/unreadable."""
        for dev in self.devices():
            d = self.root / dev
            if _read_str(d / "type") != "Mains":
                continue
            if name != "auto" and dev != name:
                continue
            v = _read_int(d / "online")
            if v is not None:
                return v == 1
        return None
