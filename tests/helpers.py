"""Shared helpers to build fake /sys/class/power_supply trees."""
from pathlib import Path


def make_ps(root: Path, battery: dict | None = None, ac_online: int | None = 1,
            bat_name: str = "BAT0"):
    """Write a fixture power_supply tree under root."""
    root.mkdir(parents=True, exist_ok=True)
    if ac_online is not None:
        ac = root / "AC"
        ac.mkdir(exist_ok=True)
        (ac / "type").write_text("Mains\n")
        (ac / "online").write_text(f"{ac_online}\n")
    if battery is not None:
        bat = root / bat_name
        bat.mkdir(exist_ok=True)
        (bat / "type").write_text("Battery\n")
        for k, v in battery.items():
            (bat / k).write_text(f"{v}\n")
    return root
