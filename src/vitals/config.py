"""Configuration loading (~/.config/vitals/config.toml)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS = {
    "battery": {
        "device": "auto",          # "auto" or e.g. "BAT0"
        "low_percent": 20,
        "critical_percent": 10,
        "taper_factor": 1.5,       # charge-time taper multiplier above 80%
        "power_floor_w": 0.5,      # below this, time estimates are suppressed
    },
    "panel": {
        # shared defaults; a [panel.<name>] table overrides them per panel
        "margin_top": 38,          # clears waybar
        "margin_right": 8,
        "width": 360,
        "range_default": "6h",     # 1h | 6h
    },
    "notifications": {
        "low": True,
        "critical": True,
        "unplugged": False,
        "plugged": False,
        "full": False,
    },
    "brightness": {
        "device": "auto",          # "auto" or a brightnessctl device name
    },
    "history": {
        "persist": False,
    },
}


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return Path(base) / "vitals" / "config.toml"


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, data: dict):
        self._d = data

    def __getitem__(self, section: str) -> dict:
        return self._d[section]

    def get(self, section: str, key: str, default=None):
        return self._d.get(section, {}).get(key, default)

    def panel(self, name: str) -> dict:
        """[panel] scalars overlaid with the [panel.<name>] table, if any."""
        base = self._d.get("panel", {})
        out = {k: v for k, v in base.items() if not isinstance(v, dict)}
        own = base.get(name)
        if isinstance(own, dict):
            out.update(own)
        return out


def load(path: Path | None = None) -> Config:
    data = DEFAULTS
    p = path or config_path()
    try:
        with open(p, "rb") as f:
            data = _merge(DEFAULTS, tomllib.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:  # bad config must not kill the daemon
        import sys
        print(f"vitals: ignoring bad config {p}: {e}", file=sys.stderr)
    return Config(data)
