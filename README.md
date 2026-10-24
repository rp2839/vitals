# Vitals

A Hyprland/Linux system status display inspired by iStat Menus 7: compact
menu-bar indicators in waybar, each opening a live dropdown panel with
graphs **and inline controls** to change the underlying settings.

The first module is **Battery & Power**: battery %, charge state, time
estimates, power draw, battery health, history graph, power-profile
switcher and brightness slider.

## Architecture

```
sysfs / inotify / D-Bus (power-profiles-daemon) / brightnessctl
                         |
                     vitalsd          (GLib main loop, systemd --user service)
                         |
          $XDG_RUNTIME_DIR/vitals/control.sock   (newline-JSON protocol)
                    +----+-----+
               vitals-bar   vitals-panel
                    |           |
                 waybar     GTK3 layer-shell dropdown
```

- `vitalsd` — the daemon: collectors, history, controls, notifications.
- `vitals-bar` — waybar `custom/` module bridge (streams one JSON object per line).
- `vitals-panel` — layer-shell dropdown (anchored top-right, autohide, Esc closes).
- `vitalsctl` — CLI: `status`, `panel toggle`, `profile <name|get|cycle>`, `brightness <N|get|+5|-5>`.

Docs: [product spec](docs/SPEC.md) · [technical design](docs/DESIGN.md) ·
[contrib workflow](docs/WORKFLOW.md)

## Requirements

Fedora (or similar), Hyprland + waybar, Python ≥ 3.11 with system
PyGObject (`python3-gobject`), GTK3, `gtk-layer-shell` (GI typelib
`GtkLayerShell-0.1`), `brightnessctl`, `power-profiles-daemon`,
`notify-send`. Nerd Font recommended for bar icons (`VITALS_ASCII=1`
falls back to plain text).

## Install (user-level)

```sh
./install.sh                 # copies package to ~/.local/lib/vitals, bins to ~/.local/bin,
                             # systemd user units to ~/.config/systemd/user
systemctl --user enable --now vitalsd vitals-panel
```

Then merge `waybar/vitals.jsonc` into `~/.config/waybar/config.jsonc`
(add `custom/vitals-battery` to `modules-right`) and append
`waybar/vitals.css` to `~/.config/waybar/style.css`, then
`killall -USR2 waybar`.

## Configuration

`~/.config/vitals/config.toml` (all keys optional, see `src/vitals/config.py`):

```toml
[battery]
low_percent = 20
critical_percent = 10

[panel]
margin_top = 38      # clears waybar
range_default = "6h"

[notifications]
low = true
critical = true
```

## Development

```sh
python3 -m unittest discover -s tests   # unit tests (no GI needed)
python3 bin/vitalsd                     # run daemon from the tree
./bin/vitalsctl status
```

See `docs/WORKFLOW.md` for the branch → agent-review → merge process.

## Status & roadmap

Battery & Power (P0) is implemented; CPU, memory, network, temperatures
follow — see docs/SPEC.md §6. Track work on the GitHub project board.
