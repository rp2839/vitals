# Vitals — Product Specification

Version 0.1 draft. Target: Fedora, Hyprland 0.56.2 (Lua config), waybar 0.15.

## 1. Vision & scope

**Vision.** Vitals is a set of compact status indicators for the Hyprland bar. Each indicator opens a rich, live dropdown panel with graphs, and the panel lets you change the underlying setting inline. It is modelled on iStat Menus 7: glanceable in the bar, detailed on click, and actionable in the panel.

**Vitals is:**
- A small user-level daemon plus per-metric modules. The Battery & Power module ships first.
- Event-driven and cheap enough to run 24/7. It uses sysfs with inotify, D-Bus signals and timers only when a graph is visible.
- A waybar citizen. The bar indicator is a `custom/` module that streams JSON, and the panel is a layer-shell popup.
- Installable as a systemd **user** service.

**Vitals is not:**
- A replacement for waybar, a shell or a desktop environment.
- A full-system monitor like btop, or a logging or metrics stack (Prometheus and the like). History is in-memory and short, with an optional small on-disk ring buffer.
- A battery *charge-limit* tool or a power-tuning daemon. It reads and surfaces state. It changes only the power profile and brightness, through existing daemons.
- A multi-DE or multi-bar product in v0.1. Sway, GNOME and other bars are out of scope. Nothing in the design should prevent them later.
- Dependent on `upower`. sysfs is the source of truth, and upower is never required.

## 2. Menu-bar indicator spec: Battery & Power

### 2.1 Content layout

The indicator is one waybar module, `custom/vitals-battery`. Its default text is:

`<icon> <percent>%`

The user can add optional fields through config. They are appended in this order, separated by a thin space:
1. Time remaining: `· 3:42`
2. Power draw: `· 8.4W`
3. CPU temperature: `· 54°`

**Default:** icon + percent + time. The user can turn any field on or off, and the order is fixed.

The JSON line is `{"text","tooltip","class","alt","percentage"}`. `class` drives the CSS state. `alt` is the state name. `percentage` is the integer battery charge.

### 2.2 Visual states

Icons are Nerd Font glyphs. A Nerd Font is a documented requirement, with a plain-text fallback `BAT`.

| State | Condition | Icon | Text | CSS class |
|---|---|---|---|---|
| **Discharging** | status=Discharging, >20% | Battery glyph in 10% steps (`󰁹` to `󰁺`) | `72% · 3:42` | `discharging` |
| **Charging** | status=Charging | Battery-charging glyph in 10% steps | `72% · 1:05` | `charging` |
| **Full / plugged** | status=Full, or Not charging with AC online and ≥ 95% | Plug-battery glyph (`󰂅`) | `100%` with no time | `full` |
| **Plugged, not charging** | AC online, status=Not charging, <95% (e.g. a firmware charge limit) | Plug glyph (`󰚥`) | `80% · held` | `held` |
| **Low** | Discharging, ≤20% and >10% | Alert-outline glyph | `18% · 0:41` | `low` (amber) |
| **Critical** | Discharging, ≤10% | Alert glyph | `7% · 0:14` | `critical` (red, blinks) |
| **No battery** | BAT0 absent | Plug glyph (`󰚥`) | `AC`, or the module is hidden if `hide_when_no_battery` is set | `nobattery` |
| **Unknown / error** | sysfs unreadable, or daemon down | `󰂑` | `--` | `unknown` |

Rules:
- **Low and critical apply only while discharging.** A charging battery at 8% shows the `charging` state.
- **Critical blink.** The blink is a CSS animation of about 1 s, defined in the shipped `style.css` snippet. Users can disable it with `animation: none`.
- **Colors.** Colors are not hardcoded in the output. Only classes are emitted, and the snippet provides defaults. Defaults are amber (`#f9a825`) for low and red (`#e53935`) for critical.
- **Thresholds.** Low is 20% and critical is 10%. Both are configurable. The values are inclusive upper bounds: ≤20 is low, ≤10 is critical.

### 2.3 Time-to-empty and time-to-full rules

- **Computation.** Time to empty = `energy_now / smoothed_power`. Time to full = `(energy_full - energy_now) / smoothed_power`. When charging, the estimate also applies the CC/CV taper factor described below.
- **Smoothing.** `smoothed_power` is an EMA of `power_now` with a time constant of about 60 s. Instantaneous power is never used for the estimate, because it makes the time jump.
- **Format.** `H:MM` is the default (`3:42`). `1d 2h` is shown at 24 h or more. Under 1 minute, show `<1m`.
- **Suppression.**
  - Show `…` (calculating) for the first 15 s after a state change or daemon start, or while `power_now` is 0 or under 0.5 W.
  - Show no time when full.
  - Show no time when the estimate exceeds 99 h.
- **Charge taper.** Above 80% the estimate is scaled by a configurable taper factor (default 1.5×), since CC/CV charging slows near full. If `power_now` falls below 0.5 W while charging, show `…`.
- **Tooltip.** The tooltip is multi-line: state, `72% · 3:42 remaining`, draw in W, source (Battery or AC), active profile, and health as `Health 91%`.

### 2.4 Update behavior

- **Event-driven.**
  - Watch `power_supply/BAT0/uevent` and the `AC/online` file by inotify. Also listen on the kernel uevent netlink or udev for `power_supply` change events. Plug and unplug events must reach the bar in under 500 ms.
  - Listen to the `PropertiesChanged` signal on `net.hadess.PowerProfiles` for the profile. The brightness source (backlight sysfs `actual_brightness`) is likewise watched.
- **Timer.** Sysfs `power_now` and `capacity` don't always raise inotify events. The daemon samples every **5 s** while discharging or charging, or while a panel is open, and every **30 s** while full or on AC and idle.
- **Emission.** One JSON line is emitted only when the rendered output changes. Identical consecutive lines are suppressed, so waybar is not woken for nothing.
- **Click and scroll.**
  - Left click opens or toggles the panel. A second click closes it.
  - Right click cycles the power profile.
  - Middle click is reserved.
  - Scroll over the indicator changes brightness in 5% steps.
- **Resource budget.** Under 0.5% of one core on average. RSS under 40 MB for the daemon and under 120 MB with the panel open. The panel is not resident when closed (see the tech design).

## 3. Dropdown panel spec: Battery & Power

The panel is a layer-shell popup anchored under the indicator, about 340 px wide with variable height. It is on the `overlay` layer. Keyboard interactivity is `on_demand`, so it does not take focus until the user interacts with a control. Esc, a click outside the panel, or a second click on the indicator closes it. Data refreshes every 1 s while the panel is open.

Sections, top to bottom:

### 3.1 Header
- Large percent (`72%`) and a state label: `Discharging`, `Charging`, `Fully charged`, `Plugged in · not charging`.
- Large battery glyph with a fill bar tinted by state (green, amber or red).
- Right side: time remaining or time to full (`3 h 42 m left`), or `Charged` when full.

### 3.2 Live power draw
- Current power in W with one decimal (`8.4 W`) and a direction word: `drawing` or `charging at`.
- A smaller secondary line: `12.1 V · 0.69 A`, derived from `voltage_now` and `power_now`.
- When on AC, show the charger rate (positive W into the battery) and, if known, the system draw.

### 3.3 History sparkline
- A filled line graph of **charge %** over time, with a second faint line for **power in W** (toggle, default on).
- Range selector: `1h` / `6h` / `24h` (default `6h`). The 24 h range is memory-only in v0.1.
- Charging periods get a green tint on the area under the line. Discharging periods get a neutral tint. The low and critical thresholds appear as faint horizontal guide lines.
- Hover shows a crosshair with `time · % · W`.
- Sample interval is 30 s into a ring buffer of 2880 points (24 h). The panel reads it from the daemon.

### 3.4 Time estimates
Three rows:
- `Time remaining` or `Time to full`
- `Time on battery` (since unplug) or `Time on AC`
- `Average draw (last 10 min)`

### 3.5 Battery health
- **Design capacity:** `energy_full_design` in Wh (e.g. `57.0 Wh`).
- **Full capacity now:** `energy_full` in Wh (`52.1 Wh`).
- **Wear:** `1 - energy_full / energy_full_design` as a percent, with a thin bar. Label: `Health 91% · Wear 9%`. A warning color applies when wear is 20% or more.
- **Cycle count:** `cycle_count`, or `n/a` if absent or 0 on firmware that doesn't report it.
- Also shown if available: technology and manufacturer (from sysfs), and the `capacity_level` string.
- This section is read-only.

### 3.6 Power source
- `Power source: Battery` or `AC adapter`, with a plug glyph.
- On AC, add the charge state (`Charging`, `Held at 80%`, `Full`).
- Read-only. Charge-limit control is a non-goal in v0.1 (see P2).

### 3.7 Power profile (control)
- A three-segment selector: **Power Saver · Balanced · Performance**. The active segment is highlighted.
- Clicking a segment calls `net.hadess.PowerProfiles` `ActiveProfile` over D-Bus (or `powerprofilesctl set`).
- If the profile is held by another app (`ActiveProfileHolds`), show a small note: `Held by <app>`.
- If Performance is unavailable (no `performance` in `Profiles`), hide or disable that segment with a tooltip.
- External changes (e.g. `powerprofilesctl set` in a terminal) update the selector live through the signal.

### 3.8 Brightness (control)
- Slider 1–100% with a sun glyph on each end. Dragging calls `brightnessctl` (or writes through logind `SetBrightness`) with debouncing at about 30 ms, and the value updates live.
- Scroll over the slider changes it in 5% steps. The minimum is clamped at 1% so the screen cannot go black.
- Hidden when no backlight device exists.

### 3.9 Footer
Small buttons: `Refresh` (re-sample now), `Settings` (opens the config file in `$EDITOR`/`xdg-open`, which is a placeholder in v0.1). A last-updated stamp is optional.

## 4. Warning & notification behavior

Notifications use the freedesktop `org.freedesktop.Notifications` D-Bus interface, so they work with dunst, mako, swaync and others. The primary path is `notify-send`, or a direct D-Bus call. Compatibility rules:

- **Replace, don't stack.** Use a stable replace ID (`--replace-id` through the D-Bus API, or `x-dunst-stack-tag`/`x-canonical-private-synchronous: vitals-battery` hints). Only one battery notification is visible at a time.
- **App name** `Vitals`, **icon** `battery-caution` / `battery-low` / `battery-full-charged` (freedesktop icon names).
- **Each notification fires once per episode.** An episode begins when a threshold is crossed downward while discharging, and it resets when the battery is plugged in or the level rises above the threshold plus 3% hysteresis.

| Event | Trigger | Urgency | Text | Timeout |
|---|---|---|---|---|
| **Low battery** | ≤20% while discharging | `normal` | "Battery low — 20% (0:41 left). Plug in soon." | 10 s |
| **Critical battery** | ≤10% while discharging | `critical` | "Battery critical — 10% (0:14 left). Plug in now." | Sticky until plugged in or replaced |
| **Very critical (optional)** | ≤5% | `critical` | "Battery 5% — system may suspend." | Sticky |
| **Unplugged** | AC online → offline | `low` | "Running on battery — 87% (3:42 left)" | 4 s |
| **Plugged in** | AC offline → online | `low` | "AC connected — charging at 24 W" | 3 s |
| **Fully charged** | Status becomes Full, or ≥100%, on AC | `low` | "Battery full — you can unplug." | 6 s |

Notes:
- **Suppression.** Unplugged, plugged and full notifications are **off by default except low and critical**. The unplug and plug events have flap protection. A change within 2 s of the opposite one is dropped, so a loose connector does not spam.
- **Startup.** No notification is fired for the initial state when the daemon starts.
- **Do Not Disturb.** Critical notifications bypass DND, since their urgency is `critical`. The rest respect it through the daemon's own handling.
- **Fallback.** If no notification daemon owns the bus name, log a warning once and continue. Never crash.
- **Optional action.** The notification can carry a `Switch to Power Saver` action button, if the daemon supports actions. It is off by default.
- **Auto-actions.** Automatically switching to power-saver at low battery is P2 and off by default. It is never silent.

## 5. Feature list (Battery & Power)

### P0 — required for v0.1
1. Streaming waybar JSON indicator with all states in 2.2 (discharging, charging, full, held, low, critical, no battery, unknown).
2. Percent, icon and time estimate with smoothing and suppression rules.
3. Event-driven updates through inotify on sysfs, with the 5 s and 30 s fallback timer. Plug and unplug in under 500 ms.
4. Click-to-open layer-shell panel that does not grab focus until used, and closes on Esc, outside click or a second click.
5. Panel sections: header, live power draw, time estimates, battery health (design vs full, wear %, cycles), power source.
6. Charge/power history sparkline (1h / 6h), memory-only.
7. Power-profile selector that works, with live external-change sync.
8. Brightness slider that works.
9. Low and critical notifications with replace and once-per-episode behavior.
10. systemd user unit, a waybar config snippet and a CSS snippet.
11. Graceful no-battery behavior (desktop or VM).

### P1 — soon after
1. Unplug, plug and fully-charged notifications (configurable, default on for unplug).
2. 24 h range, with the history persisted across restarts in a small ring file under `$XDG_STATE_HOME/vitals/`.
3. Optional bar fields: watts and CPU temperature, with a configurable field order.
4. Right-click profile cycle and scroll-to-change brightness on the indicator.
5. Config file (`~/.config/vitals/config.toml`) with thresholds, fields, and range and notification toggles.
6. Held-by-app note for power-profile holds.
7. Hover crosshair in the graph, and a wear-warning color.
8. Per-state tooltips.

### P2 — later
1. Charge-limit control (`charge_control_end_threshold`) with a polkit helper, where the hardware supports it.
2. Auto power-saver at a battery threshold, and auto-profile on unplug or plug.
3. Per-app energy attribution (top consumers).
4. Fan and thermal hooks, and suspend hints ("will suspend at 5%").
5. Health trend over weeks (full capacity over time).
6. Multi-battery support (BAT1), and peripherals' batteries such as a mouse or headset.
7. Theme integration (pywal, matugen), and light-theme panel.
8. CSV export of history.

## 6. Roadmap: later modules

Each module has a bar indicator and a panel. **Settings controls** means the panel offers inline controls that change the underlying setting, not just display.

| Module | Scope (one line) | Panel settings controls? |
|---|---|---|
| **CPU** | Total and per-core usage, load, frequency, top processes, and history graph. | Yes — CPU governor / energy-performance preference, and a core on/off toggle (later). |
| **Memory** | Used, cached, swap and pressure (PSI), with the top memory consumers. | No in v1. Possibly a button to drop caches or a swappiness slider later. |
| **Network** | Up and down throughput graph, per-interface IPs, Wi-Fi SSID and signal, and public IP. | Yes — Wi-Fi on/off, connection switch and a VPN toggle (through NetworkManager). |
| **Disks** | Per-mount usage, SMART health, and read/write throughput. | No (read-only, with an "open in file manager" action). |
| **Temperatures & fans** | Sensor list (CPU, GPU, NVMe, battery) with history, and fan RPM. | Yes — fan curve or profile where `hwmon` or the firmware exposes it. |
| **Bluetooth** | Adapter state, paired devices and their battery levels. | Yes — adapter power toggle, and device connect and disconnect. |
| **Audio** | Output and input devices, volume, and mute. | Yes — volume sliders, mute, and default-device switching (PipeWire/WirePlumber). |

Ordering: CPU → Memory → Network → Temperatures → Audio → Bluetooth → Disks. CPU, memory and network come first because they share the sampling and graph infrastructure with the battery module.

## 7. Success criteria for v0.1

**Functional**
1. All eight states in 2.2 render correctly. Verified with a fake sysfs tree (fixtures) in automated tests, and by manual plug and unplug on the target machine.
2. Plug and unplug changes appear in the bar in under 500 ms.
3. Clicking the indicator opens the panel in under 300 ms. The panel does not steal focus until a control is used. Esc, outside click or a second click closes it.
4. The profile selector changes the real profile, verified with `powerprofilesctl get`, and reflects an external change within 1 s.
5. The brightness slider changes the real backlight, verified with `brightnessctl get`, and never goes below 1%.
6. Health figures match a manual calculation from `energy_full` and `energy_full_design` within rounding. Cycle count matches sysfs.
7. Low (20%) and critical (10%) notifications appear once per episode through `notify-send`-compatible daemons, with replacement and no stacking. Tested with dunst, and with mako if available.
8. A machine without a battery produces no errors, and shows `AC` or hides the module.

**Non-functional**
9. The daemon averages under 0.5% of one core over 1 hour of idle use, and its RSS is under 40 MB. The panel is closed and not resident.
10. It runs as `systemctl --user` service with `Restart=on-failure`, and survives waybar restarts and daemon crashes. The bar shows `unknown` during the gap and recovers on its own.
11. No dependency on `upower`. Works with the daemon absent.
12. Install from a clean checkout in under 5 minutes using the README: copy the config snippets, enable the unit, and reload waybar.

**Quality**
13. Unit tests cover state classification, the time estimator (smoothing, suppression and taper), and the notification episode logic.
14. A README documents the waybar snippet, the CSS classes, the Nerd Font requirement and the known limitations.