# Vitals — Technical Design

## 1. Architecture

Vitals uses one Python daemon, **`vitalsd`**, which owns all collectors, controls, history and canonical state. Two thin clients consume its UNIX socket API:

- **`vitals-bar`** subscribes to battery updates and writes Waybar-compatible JSON to stdout.
- **`vitals-panel`** displays a GTK3 layer-shell dropdown, graphs and controls.

A small **`vitalsctl`** command requests panel toggles and supports diagnostics. Neither UI client independently samples hardware.

```text
sysfs / inotify / system D-Bus / brightnessctl
                         │
                     vitalsd
              collectors + history
                         │
          $XDG_RUNTIME_DIR/vitals/control.sock
                    ┌────┴─────┐
               vitals-bar   vitals-panel
                    │           │
                 Waybar     GTK3 layer-shell
```

The daemon runs as a systemd user service. The panel runs as a separate, single-instance user service, initially hidden. This isolates GTK failures and keeps GTK dependencies out of the daemon. The panel subscribes to panel-visibility events; `vitalsctl panel toggle` sends a request to the daemon, which notifies the panel.

On reconnect, the panel receives the current visibility state. Disconnecting from the daemon hides the panel and disables controls until reconnection.

### Hyprland presentation

Use **GTK3 with gtk-layer-shell**, without Hyprland dispatch commands or window rules. This avoids the broken CLI dispatch path documented for this Lua-config Hyprland build.

The panel is a layer-shell surface, rather than an ordinary floating window:

- Layer: `TOP`, above normal application windows.
- Namespace: `vitals-panel`.
- Anchor: top and right by default; configurable for the existing bar’s placement.
- Exclusive zone: zero; the panel reserves no workspace area.
- Top margin: configured to clear Waybar.
- Width: approximately 380 logical pixels, constrained to the selected monitor.
- Keyboard mode: `ON_DEMAND`, never `EXCLUSIVE`.

Waybar’s click command passes output and placement information through configuration. Layer-shell cannot discover the exact position of a Waybar custom module; positioning therefore uses a configured output, edge and margin. A single-monitor setup may use the default GDK monitor.

Opening the panel must not demand exclusive keyboard focus. Clicking a control may focus the panel naturally. Escape closes it when it has keyboard focus; a close button always works, and clicking the indicator again toggles it closed. Focus-out dismissal applies only after the panel has acquired focus. Do not introduce a fullscreen input-capture surface merely to detect outside clicks.

If the configured output disappears, hide the panel and resolve a remaining monitor before the next open.

## 2. Technology choice

Use **Python 3.14**, GLib/Gio, GTK3, gtk-layer-shell and Cairo.

The brief verifies Python, Waybar, powerprofilesctl and brightnessctl. Read-only package inspection additionally found `python3-gobject`, GTK3, `gtk-layer-shell` and `python3-cairo`. GTK4 is installed, but `gtk4-layer-shell` is absent.

| Option | Graphs and controls | Hyprland integration | Resource and deployment implications |
|---|---|---|---|
| **GTK3 + PyGObject + gtk-layer-shell** | Cairo drawing areas; native sliders and combo boxes | Established layer-shell bindings, including on-demand keyboard mode | Dependencies present; chosen |
| GTK4 + PyGObject | Good native controls and custom drawing | Requires separate gtk4-layer-shell dependency and integration | Reasonable future migration; adds a currently missing component |
| eww | Convenient declarative widgets; graph support depends on widget composition | Layer-shell support, but focus behavior requires configuration | Additional runtime and expression/configuration layer; installed availability unverified |
| Qt | Strong controls and graph options | Requires compatible layer-shell integration | Additional Python bindings and toolkit dependencies; availability unverified |

The daemon uses a GLib main loop, Gio asynchronous D-Bus calls, UNIX socket readiness sources and timer sources. This permits event-driven collection without maintaining competing asyncio and GLib loops. Small blocking operations, such as subprocess completion, use asynchronous Gio APIs.

Cairo draws history directly in a GTK drawing area. No browser, plotting server or heavyweight chart package is needed. Redraw only on incoming data, resize or pointer interaction.

The Waybar bridge follows the existing streaming custom-module idiom: one complete JSON object per line, flushed immediately. No `socat` or `nc` dependency is required.

## 3. UNIX socket protocol

### Transport and security

Socket location:

```text
$XDG_RUNTIME_DIR/vitals/control.sock
```

Create the directory with mode `0700` and the socket with mode `0600`. Check Linux `SO_PEERCRED` and accept only the daemon owner’s UID.

Use UTF-8 newline-delimited JSON. Each request and response occupies one line. Maximum inbound frame size is 64 KiB; disconnect malformed or oversized clients. JSON numbers must be finite; unavailable values use `null`.

Every request includes protocol version `v`, request `id` and operation `op`. Responses echo `id`. Events have no request ID. All examples below are illustrative.

### Snapshot

Request:

```json
{"v":1,"id":"r1","op":"snapshot","modules":["battery","power"]}
```

Response:

```json
{"v":1,"id":"r1","ok":true,"seq":42,"data":{"battery":{"status":"Discharging","ac_online":false,"percent":73.2,"energy_wh":35.1,"energy_full_wh":48.0,"energy_full_design_wh":55.0,"health_percent":87.3,"cycle_count":214,"power_w":8.4,"power_source":"power_now","time_to_empty_s":15043,"time_to_full_s":null,"quality":"ok"},"power":{"active_profile":"balanced","profiles":["power-saver","balanced","performance"],"brightness_percent":62.0}}}
```

Snapshots also carry `observed_at` as UTC RFC3339 text and `age_s` for each module. `seq` increases during a daemon lifetime; a new connection begins a new synchronization session.

### Subscribe

```json
{"v":1,"id":"r2","op":"subscribe","modules":["battery","power"],"min_interval_ms":1000}
```

Return an acknowledgement containing a full snapshot, then full module replacements:

```json
{"v":1,"event":"update","seq":43,"data":{"battery":{"status":"Discharging","percent":73.1,"power_w":8.1,"quality":"ok"}}}
```

In implementation, each included module contains its entire current state; the abbreviated event above illustrates the envelope. Full replacements avoid ambiguous patch semantics.

Clamp requested delivery intervals to a supported minimum. Coalesce updates for slow clients, bound output queues, and disconnect clients that cannot drain them. Slow clients must not block collection.

### Get and set controls

```json
{"v":1,"id":"r3","op":"controls.get"}
```

```json
{"v":1,"id":"r3","ok":true,"data":{"power.profile":{"value":"balanced","choices":["power-saver","balanced","performance"],"available":true,"writable":"unknown"},"display.brightness":{"value":62.0,"min":1.0,"max":100.0,"available":true,"writable":"unknown"}}}
```

`writable` is `true`, `false` or `"unknown"`; authorization may only become known when an operation is attempted.

```json
{"v":1,"id":"r4","op":"controls.set","control":"power.profile","value":"power-saver"}
```

```json
{"v":1,"id":"r4","ok":true,"data":{"control":"power.profile","value":"power-saver"}}
```

Brightness uses the same operation:

```json
{"v":1,"id":"r5","op":"controls.set","control":"display.brightness","value":45.0}
```

Success means the backend accepted the request and a subsequent read confirmed the reported value. Serialize writes per control; use bounded timeouts.

```json
{"v":1,"id":"r5","ok":false,"error":{"code":"permission_denied","message":"Brightness change was denied."}}
```

Other stable error codes include `invalid_request`, `unsupported_version`, `unknown_control`, `invalid_value`, `unavailable` and `timeout`. Never accept arbitrary shell commands, paths or D-Bus method names from clients.

### Panel and history

```json
{"v":1,"id":"r6","op":"panel.toggle","panel":"battery","output":null}
```

```json
{"v":1,"event":"panel","data":{"panel":"battery","visible":true,"output":null}}
```

The panel subscribes to a separate `panel` topic and can request explicit `panel.set` visibility to close itself.

History is fetched separately, avoiding large routine snapshots:

```json
{"v":1,"id":"r7","op":"history.get","module":"battery","fields":["percent","power_w"],"window_s":3600,"max_points":360}
```

Return timestamped numeric samples, including `null` gaps. Bound responses to 1,440 points per series. Downsample longer windows into time buckets with mean, minimum and maximum values.

## 4. Battery and power acquisition

### Sources and units

Read the verified battery directory:

```text
/sys/class/power_supply/BAT0/
```

| File | Meaning | Conversion |
|---|---|---|
| `energy_now` | Current stored energy | µWh ÷ 1,000,000 → Wh |
| `energy_full` | Current learned full capacity | µWh → Wh |
| `energy_full_design` | Original design capacity | µWh → Wh |
| `power_now` | Instantaneous power magnitude | µW ÷ 1,000,000 → W |
| `status` | Charging, Discharging, Full, Not charging, Unknown | Validated string |
| `cycle_count` | Battery cycles | Integer; zero is valid |
| `voltage_now` | Battery voltage | µV ÷ 1,000,000 → V |
| `capacity_level` | Firmware qualitative capacity state | String |

Read external power from:

```text
/sys/class/power_supply/AC/online
```

Compute percentage as `100 × energy_now / energy_full` when full capacity is positive. Clamp the displayed percentage to 0–100 while retaining diagnostic information about inconsistent input.

If a `capacity` file exists, it may supply a fallback percentage; its presence is not assumed. `capacity_level` is a qualitative indicator, not a percentage substitute.

Health is:

```text
health_percent = 100 × energy_full / energy_full_design
```

Do not clamp health to 100%; learned capacity can exceed the design rating. Missing or invalid design capacity yields `null`.

Read files independently and tolerate disappearance or permission errors. Sysfs is not a transactional snapshot; detect inconsistent combinations and retry once rather than reporting implausible estimates.

### Events and sampling

Use a standard-library `ctypes` wrapper for Linux inotify, integrated as a GLib file-descriptor source. Watch the class directory and resolved battery/AC directories for creation, deletion and relevant changes. Rescan devices after structural changes, watch invalidation or queue overflow.

**Inotify does not reliably report changing sysfs attribute values.** It is a topology and opportunistic refresh mechanism, not the sole measurement clock.

Use bounded periodic reads:

- Battery energy, power, status and AC online: every five seconds.
- While the panel is visible: every two seconds.
- Full/design capacity and cycle count: at startup, every ten minutes and after state transitions.
- Rediscover missing devices with capped exponential backoff.

Debounce event bursts for approximately 100 ms. A future kernel power-supply uevent listener can reduce transition latency; it does not replace periodic energy sampling.

### Measured and derived power

Prefer a valid `power_now`. Report its source explicitly.

When unavailable or persistently unusable, derive power from energy samples using monotonic elapsed time:

```text
signed_power_w = (energy_previous_wh - energy_current_wh) × 3600 / elapsed_s
```

Positive means discharge; negative means charging. The UI exposes a positive magnitude together with battery status.

Energy values can be quantized. Derive a slope over approximately 60–120 seconds rather than dividing adjacent tiny changes. Reject intervals crossing suspend, AC transitions or charging-state changes. Require several usable samples before publishing a derived estimate.

Do not replace plausible measured power with derived power merely because the two briefly differ.

### Smoothing and time estimates

Keep raw power for diagnostics and history. Apply a time-aware exponential moving average with approximately a 30-second time constant for the main readout and estimates:

```text
alpha = 1 - exp(-elapsed_s / 30)
smoothed = alpha × current + (1 - alpha) × previous
```

Reset smoothing on charging/discharging transitions and after resume.

For stable discharge:

```text
time_to_empty_s = energy_now_wh / smoothed_discharge_w × 3600
```

For stable charging:

```text
time_to_full_s =
    max(energy_full_wh - energy_now_wh, 0) / smoothed_charge_w × 3600
```

Require stable state, sufficient samples and power above a configurable floor, initially 0.5 W. Otherwise show “Estimating…” or an unavailable value.

Time-to-full is approximate because charging tapers. Full batteries show “Full”; plugged-in batteries that are not charging show that state without inventing a completion time. AC status and battery status remain separate fields.

Subscribe to logind sleep/resume signals where available. After resume, sample immediately and reset derivative windows.

## 5. Controls

### Power profiles

Use Gio on the **system bus** and the compatibility interface verified by the brief:

```text
Bus name:  net.hadess.PowerProfiles
Path:      /net/hadess/PowerProfiles
Interface: net.hadess.PowerProfiles
```

Read `ActiveProfile`, `Profiles` and available degradation/inhibition properties through `org.freedesktop.DBus.Properties`. Change profiles with `Properties.Set`, passing `ActiveProfile` as a string variant.

Listen for `PropertiesChanged` and bus-name ownership changes. Populate the combo from `Profiles`; never assume performance mode exists or is currently usable.

The inspected Fedora service also uses the newer canonical name `org.freedesktop.UPower.PowerProfiles`. Probe the compatibility interface first, then the canonical interface and its corresponding object path if necessary. Introspection determines the supported endpoint.

After a write, read back the active profile and publish the confirmed state. Changes from `powerprofilesctl` must update the UI through signals.

### Authorization

The inspected power-profiles-daemon policy grants its switch-profile action to an active local session and denies inactive sessions by default. The installed action name is:

```text
org.freedesktop.UPower.PowerProfiles.switch-profile
```

Resolve policy by the installed daemon version rather than assuming a legacy action name.

Run Vitals as the logged-in user. Do not run it as root or install a blanket polkit exemption. Because the writer is a systemd user service, verify actual authorization from that service on the target session; user-service identity and session association can affect policy evaluation.

Expose authorization errors in the panel and journal. If authentication is required, the session needs a working polkit agent. Do not repeatedly retry a denied write.

### Brightness

Use the verified **`brightnessctl`** executable as an asynchronous backend:

1. Enumerate backlight-class devices using machine-readable output.
2. Select an explicitly configured device, or the sole available backlight.
3. Read current and maximum raw brightness.
4. Convert percentage requests to integer device values.
5. Execute `brightnessctl` with an argument vector and an explicit device.
6. Read back the resulting brightness.

Do not select keyboard LEDs as display backlights. If multiple display backlights exist, require device selection in configuration and show the ambiguity in diagnostics.

Use a 1–100% slider initially to avoid accidentally blacking out the display. Debounce movement by about 120 ms and always commit the final value on release.

Brightness permissions depend on the installed udev/logind rules and caller context. Test writes from the user service. If denied, surface the error and document the distribution-supported permission setup; do not change sysfs permissions or invoke `sudo` automatically.

Watch the selected brightness attribute opportunistically and refresh while the panel is visible so external changes appear.

## 6. History and persistence

Use bounded in-memory ring buffers per metric:

- Native battery samples: latest two hours.
- One-minute aggregate samples: latest 24 hours.
- Each record includes UTC time, monotonic timing context, values and quality.

Store explicit gaps across suspend, device loss and daemon restarts. Never connect graph lines across missing intervals.

Optional persistence is disabled by default. When enabled, append one-minute aggregates to:

```text
${XDG_STATE_HOME:-~/.local/state}/vitals/history/YYYY-MM-DD.jsonl
```

Each record has a schema version, timestamp, boot identifier, module name and values. Persist metrics only; omit machine identifiers and raw command output.

Create directories with mode `0700` and files with mode `0600`. Retain seven days with a configurable total-size cap, initially 20 MiB. Rotate by UTC date and prune at startup and daily.

Batch writes approximately once per minute. A truncated final line after a crash is ignored. Disk failures disable persistence with a diagnostic; collection and controls continue.

## 7. Repository and installation

```text
vitals/
├── pyproject.toml
├── src/vitals/
│   ├── daemon.py
│   ├── protocol.py
│   ├── state.py
│   ├── history.py
│   ├── config.py
│   ├── collectors/
│   │   ├── base.py
│   │   ├── battery.py
│   │   └── power_profiles.py
│   ├── controls/
│   │   └── brightness.py
│   ├── linux/
│   │   ├── inotify.py
│   │   └── sysfs.py
│   └── clients/
│       ├── bar.py
│       ├── ctl.py
│       └── panel/
│           ├── app.py
│           ├── battery.py
│           ├── graphs.py
│           └── style.css
├── bin/
│   ├── vitalsd
│   ├── vitals-bar
│   ├── vitalsctl
│   └── vitals-panel
├── systemd/user/
│   ├── vitalsd.service
│   └── vitals-panel.service
├── waybar/
│   ├── vitals.jsonc
│   └── vitals.css
├── config/config.toml
├── tests/
│   ├── fixtures/power_supply/
│   ├── test_battery.py
│   ├── test_history.py
│   └── test_protocol.py
└── docs/
    ├── BRIEF.md
    └── DESIGN.md
```

Declare executable entry points in `pyproject.toml`; `bin/` contains thin development wrappers. Prefer Fedora packaging using system Python and distribution GI packages. For development, use a virtual environment with access to system site packages so PyGObject remains available.

Configuration lives at:

```text
${XDG_CONFIG_HOME:-~/.config}/vitals/config.toml
```

### Daemon service

For a user-local installation:

```ini
[Unit]
Description=Vitals metrics and controls
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=%h/.local/bin/vitalsd
Restart=on-failure
RestartSec=2
RuntimeDirectory=vitals
RuntimeDirectoryMode=0700
UMask=0077

[Install]
WantedBy=graphical-session.target
```

The panel unit uses the same session lifecycle, `Requires=vitalsd.service`, `After=vitalsd.service` and `ExecStart=%h/.local/bin/vitals-panel`. It waits hidden for visibility requests.

At Hyprland session startup, import `WAYLAND_DISPLAY`, `DISPLAY`, `XDG_CURRENT_DESKTOP` and `XDG_SESSION_TYPE` into the systemd user manager before starting the panel. If this session does not activate `graphical-session.target`, start both units explicitly from the existing Lua startup mechanism. No Hyprland dispatcher is needed.

Install units under `~/.config/systemd/user/`, reload the user manager and enable/start the services as appropriate to the session setup.

### Waybar configuration

Merge this module into the existing configuration and add `custom/vitals-battery` to the desired module list:

```jsonc
{
  "custom/vitals-battery": {
    "exec": "$HOME/.local/bin/vitals-bar battery",
    "return-type": "json",
    "restart-interval": 3,
    "exec-on-event": false,
    "on-click": "$HOME/.local/bin/vitalsctl panel toggle",
    "tooltip": true,
    "escape": true
  }
}
```

Do not set `interval`; the bridge is a continuous producer.

Example stdout line:

```json
{"text":"73% · 4h 10m · 8.4 W","tooltip":"Discharging\nBattery health: 87%\nProfile: balanced","class":["discharging"],"percentage":73}
```

The bridge must:

- Flush every line and reserve stdout for protocol output.
- Log diagnostics to stderr.
- Reconnect with bounded backoff after daemon restarts.
- Emit an unavailable state promptly on connection loss.
- Escape generated tooltip content appropriately for the supported Waybar configuration.
- Update on state changes and avoid duplicate output.

Optional CPU temperature is added only when a temperature collector exists.

## 8. Collector extensibility

Collectors publish typed data and quality; they do not format UI strings or access GTK widgets.

```python
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

type Scalar = str | int | float | bool | None

@dataclass(frozen=True)
class Sample:
    module: str
    monotonic_ns: int
    observed_at: str
    values: Mapping[str, Scalar]
    quality: str  # ok, warming_up, stale, unavailable

class CollectorContext(Protocol):
    def publish(self, sample: Sample) -> None: ...
    def every(self, seconds: float, callback: Callable[[], None]) -> object: ...
    def watch_fd(self, fd: int, callback: Callable[[], None]) -> object: ...
    def cancel(self, handle: object) -> None: ...

class Collector(Protocol):
    name: str

    def start(self, context: CollectorContext) -> None:
        """Register sources and publish initial state."""

    def refresh(self) -> None:
        """Request an immediate, nonblocking refresh."""

    def set_interest(self, *, panel_visible: bool) -> None:
        """Adjust bounded sampling frequency."""

    def stop(self) -> None:
        """Remove sources and release resources."""
```

Control providers have a separate interface for discovery, validation and asynchronous writes. Collector failures mark only their module unavailable.

Future modules fit this model:

| Module | Primary acquisition |
|---|---|
| CPU | `/proc/stat` deltas; bounded periodic sampling |
| Memory | `/proc/meminfo`; bounded periodic sampling |
| Network | Rtnetlink events for links; counter deltas for throughput |
| Disk | `/proc/diskstats`; mount-change notifications |
| Temperature/fans | Discovered hwmon attributes; conservative sampling |

Event-driven collection applies where useful notifications exist. CPU utilization and throughput still require timed counter reads.

## 9. Testing and machine verification

### Automated tests

Inject a sysfs root, monotonic clock and wall clock. Fixtures must cover:

- Normal charging, discharging, full and plugged-in/not-charging states.
- µWh/µW conversions and energy-derived percentage.
- Missing, malformed, empty and disappearing attributes.
- Zero full/design capacity, zero cycle count and health above 100%.
- Quantized energy, derivative warm-up and state-transition resets.
- Suspend gaps, near-zero power and unavailable estimates.
- Inotify overflow and device rediscovery.
- History retention, downsampling and truncated JSONL recovery.

Use a fake D-Bus service and fake subprocess adapter to verify control validation, readback, authorization failures and external changes. Tests must not change the real machine’s profile or brightness.

Exercise socket framing, partial reads, multiple frames, malformed JSON, version errors, oversized messages, bounded queues and reconnection. Validate that every Waybar output line parses independently.

### Manual acceptance on the target machine

The brief is authoritative for BAT0, AC and the existing Waybar setup. Those live sysfs and home-directory paths were unavailable in the worker environment, so they require target-session verification.

- Compare percentage, energy, power, health and cycles against the specified sysfs files.
- Unplug and reconnect AC; verify state and smoothing reset within the sampling interval.
- Confirm estimated time stabilizes and never displays invalid or negative values.
- Open the panel through Waybar under Hyprland 0.56.2; verify placement, scaling, repeated toggles and monitor removal.
- Verify opening does not force exclusive keyboard focus; sliders, combo boxes, Escape and close button work.
- Change profiles from the panel and `powerprofilesctl`; verify bidirectional updates and service-context authorization.
- Change brightness from the panel and external keys; confirm the correct backlight changes and the UI follows.
- Stop power-profiles-daemon temporarily during an explicit test; verify graceful unavailable state and reconnection.
- Restart `vitalsd`; verify bridge/panel recovery and one daemon instance.
- Suspend/resume; verify graph gaps and fresh estimates.
- Run with persistence disabled and enabled; verify retention and file permissions.
- Measure idle CPU, memory and wakeups with the panel hidden and visible.

An initial performance target is less than 0.5% average of one CPU core with the panel hidden, no busy loops, and no more than one regular battery sampling wakeup every five seconds. Record actual daemon, bridge and panel RSS separately; these are acceptance targets, not measured claims.