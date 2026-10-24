# Vitals — project brief (shared context for collaborators)

## What we are building
**Vitals** is a Linux/Hyprland system status display inspired by **iStat Menus 7**
(macOS): a set of compact menu-bar indicators, each opening a rich dropdown
panel with live graphs and *inline controls to change the underlying settings*.

The first module is **Battery & Power** (iStat Menus "Battery" analogue):
- Menu-bar indicator: battery %, charge state, time-to-empty/full, power draw (W),
  optional CPU temp.
- Dropdown panel: battery health (design vs current full capacity, cycle count),
  charge/discharge history graph, time estimates, power-source state, and
  **controls**: power-profile switcher (power-saver / balanced / performance),
  screen brightness, and future hooks (fan/thermal, suspend hints).

## Target machine (verified facts — design against these)
- Fedora (kernel 7.2.11), Hyprland **0.56.2 Lua-config build**, waybar **0.15** (GTK3).
- Bar: waybar, started by hyprland.lua; config `~/.config/waybar/config.jsonc` + `style.css`.
- Hyprland CLI dispatch is broken (Lua build); IPC socket2 events use `event>>data`.
- Battery: `/sys/class/power_supply/BAT0` (energy_now, energy_full, energy_full_design,
  power_now, status, cycle_count, voltage_now, capacity_level) and `AC` (online).
  No `upower` daemon running (only upower-libs installed) — sysfs + inotify is the
  reliable source; upower optional.
- `power-profiles-daemon` active on system bus (`net.hadess.PowerProfiles`),
  `powerprofilesctl` and `brightnessctl` available.
- Python 3.14 available; no `socat`/`nc`. Fedora desktop, systemd user services OK.

## Hard requirements
1. Menu-bar integration must work with the existing waybar setup (a `custom/` module
   with `return-type: json` streaming one JSON object per line is the proven idiom
   on this box — see existing `wbar-ws` pattern).
2. Dropdown panel: click on the indicator opens a live panel with graphs + working
   controls (profile switch, brightness). Must be a layer-shell popup or similar;
   must not steal focus in a disruptive way; must work under Hyprland.
3. Architecture must be modular: a small daemon/core + per-metric modules so CPU,
   memory, network, disks, fans can be added later like iStat Menus.
4. Low resource use (this lives 24/7). Event-driven (inotify/netlink/dbus signals)
   over polling wherever practical.
5. Written so it can be packaged/run as a systemd **user** service.

## Deliverables expected from collaborators
- Product spec: module list, battery/power feature list with priorities (P0/P1/P2),
  panel UX layout, indicator states.
- Technical design: process architecture (daemon vs per-widget), data sources per
  metric, IPC between bar module and panel, tech-stack choice with trade-offs
  (e.g. Python+GTK layer-shell vs eww vs Qt), directory layout, testing approach.

## Constraints on collaborators
You are read-only. Return complete markdown documents; the orchestrator writes them
into `docs/`. Be concrete and opinionated; pick ONE recommendation, list alternatives
briefly.
