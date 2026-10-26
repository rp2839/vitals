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
## 10. Bluetooth module

Product behaviour is in SPEC §8, and the tracking issue is #26. Facts checked on the target machine (2026-10-26):
- BlueZ 5.87 is active on the system bus, with a single adapter `hci0`.
- The M585/M590 mouse exposes `org.bluez.Battery1`.
- obexd is running on the session bus as `org.bluez.obex`.
- `pactl` and `wpctl` are present.
- blueman 2.4.6 is installed, but `blueman-applet` is not running, so **no pairing agent is currently registered**.
- The BlueZ D-Bus policy (`/usr/share/dbus-1/system.d/bluetooth.conf`) allows any local user to call `org.bluez`.
- The waybar config uses the built-in `bluetooth` module, with `on-click: blueman-manager`.

### 10.1 Prerequisite: more than one module and panel

The current code assumes a single module everywhere:
- The daemon's `module_data`, `_flush_updates` and `_history_get` default to and filter on `("battery", "power")`.
- `panel.toggle` and `panel.set` ignore the `panel` field and always report `"battery"`.
- `vitals-panel` builds a single battery window.

This has to land as a separate PR before any Bluetooth code:

1. **Module registry in the daemon.** Each module registers a name, a `data()` callback and its control and action providers. Snapshot, subscribe and flush iterate over the registry instead of a literal tuple. Battery and power become the first two entries.
2. **Panel visibility keyed by name.** The daemon tracks `visible_panel: str | None`, and only one panel is open at a time. `panel.toggle {"panel":"bluetooth"}` opening Bluetooth closes Battery. The `panel` event carries the name.
3. **One `vitals-panel` process with one layer-shell window per panel.** Each window is created lazily on its first open and then kept, hidden. Shared pieces move into the base: `DaemonClient`, CSS, anchoring, focus-out grace and Esc handling. The battery content becomes `BatteryView` and the new content is `BluetoothView`. A second GTK process per panel would double resident memory for no isolation benefit.
4. **Per-panel anchor margin in config.** `[panel.bluetooth] margin_right = …`. As §1 explains, layer-shell cannot locate a waybar module, so each indicator's offset is configured.
5. **The bar bridge already takes a module argument** (`vitals-bar battery`). It gains `bluetooth`, and its formatter moves behind the same registry.

### 10.2 Acquisition: BlueZ over the system bus

All BlueZ access goes through one `BluezBackend` class in `vitals/linux/bluez.py`, so collectors, controls and tests never touch Gio directly. It uses Gio asynchronous D-Bus only.

- On start, call `org.freedesktop.DBus.ObjectManager.GetManagedObjects` on `org.bluez` at `/`.
- From then on, keep the model current from `InterfacesAdded`, `InterfacesRemoved` and `PropertiesChanged`, subscribed once with a sender filter on `org.bluez`. There is no polling.
- Watch the name owner of `org.bluez`. When `bluetoothd` restarts, mark the module `unavailable`, then re-run `GetManagedObjects` when the name reappears.
- The model holds:
  - **Adapter** (`Adapter1`): `Address`, `Alias`, `Powered`, `PowerState`, `Discovering`, `Pairable`, `Discoverable`.
  - **Device** (`Device1`): `Address`, `Alias`, `Icon`, `Paired`, `Bonded`, `Trusted`, `Blocked`, `Connected`, `RSSI`, `UUIDs`.
  - **Battery** (`Battery1`): `Percentage`, `Source`.
- **rfkill.** Read `/sys/class/rfkill/rfkill*/{type,soft,hard}` for `type == bluetooth`, and watch it through the existing uevent netlink socket (`rfkill` subsystem). `Adapter1.PowerState` reports `off-blocked` for a soft block, but the hard-block distinction needs sysfs.
- **Last connected.** BlueZ doesn't expose a last-connected time. The daemon records the time of `Connected → true` transitions in `$XDG_STATE_HOME/vitals/bluetooth.json`, keyed by address, and uses it for sorting (SPEC 8.2.3).

Snapshot shape (abbreviated):

```json
{"bluetooth":{"available":true,"adapter":{"address":"38:8D:3D:89:67:A7","alias":"fedora","powered":true,"power_state":"on","rfkill":"none","discovering":false},"devices":[{"address":"D3:3B:0A:10:24:0D","alias":"M585/M590","icon":"input-mouse","paired":true,"trusted":true,"blocked":false,"connected":true,"battery":41,"rssi":null,"audio":false,"opp":false,"busy":null,"last_connected":"2026-10-26T08:12:03Z"}],"transfer":null,"prompt":null}}
```

`busy` is `null`, `"connecting"`, `"disconnecting"`, `"pairing"` or `"removing"`. Every view renders spinners and insensitive buttons from this one field, so the bar, a re-opened panel and a reconnecting panel all agree.

Discovered, unpaired devices are only included while discovery is running, and are capped at 64 by RSSI. BlueZ removes stale ones about 30 s after discovery stops.

### 10.3 Actions: a new protocol operation

`controls.set` sets one scalar value, which suits adapter power. Most Bluetooth operations instead target a device, take seconds, and may need a pairing prompt. They use a new allowlisted `action` operation:

```json
{"v":1,"id":"r9","op":"action","action":"bluetooth.connect","args":{"device":"D3:3B:0A:10:24:0D"}}
```

| Action | Backend call | Timeout |
|---|---|---|
| `bluetooth.connect` / `disconnect` | `Device1.Connect` / `Disconnect` | 30 s / 10 s |
| `bluetooth.pair` | `Device1.Pair`, then `Trusted=true`, then `Connect` | 60 s (prompts included) |
| `bluetooth.cancel_pair` | `Device1.CancelPairing` | 5 s |
| `bluetooth.forget` | `Adapter1.RemoveDevice(path)` | 10 s |
| `bluetooth.set_trusted` / `set_blocked` / `rename` | `Properties.Set` on `Trusted` / `Blocked` / `Alias` | 5 s |
| `bluetooth.discovery` `{"on":bool}` | `SetDiscoveryFilter({"Transport":"auto"})`, `Pairable=true`, then `StartDiscovery` / `StopDiscovery` | 5 s |
| `bluetooth.audio_profile` `{"profile":…}` | §10.6 | 5 s |
| `bluetooth.send_file` `{"path":…}` / `cancel_transfer` | §10.8 | n/a |

Adapter power is a control, `bluetooth.power` (boolean), set through `Adapter1.Powered`. When the adapter is soft-blocked, the daemon first unblocks it by writing an `RFKILL_OP_CHANGE` event (`struct rfkill_event`, type `RFKILL_TYPE_BLUETOOTH`, `soft=0`) to `/dev/rfkill`. This is what `rfkill unblock bluetooth` does. The sysfs `soft` attribute is root-only (`-rw-r--r-- root`), but `/dev/rfkill` carries an ACL (`crw-rw-r--+`), which is presumably the systemd-logind uaccess grant for the active session. Verify this on the target (see §10.10). If the write is denied, the panel reports that the adapter is blocked and offers **Open blueman**. Vitals does not use blueman's polkit rfkill helper.

Rules:
- **Device arguments.** `device` must be a MAC address that is already in the model. The daemon maps it to the object path itself, so clients never send D-Bus paths.
- **One operation per device.** A second action on a busy device fails with `busy`.
- **Error codes.** BlueZ errors map to stable codes in the reply: `org.bluez.Error.AuthenticationFailed` → `auth_failed`, `…Failed: Host is down` → `unreachable`, `…AlreadyConnected` → success, and `…InProgress` → `busy`.
- **Discovery ownership.** Discovery is tied to the client that started it. If that panel connection drops, or the daemon receives `panel.set visible=false` for Bluetooth, the daemon stops discovery and sets `Pairable` back to its previous value. A 60 s daemon-side timer stops it even if the client forgets.

`vitalsctl` gains `bluetooth power toggle|on|off` (used by the right-click) and `bluetooth connect|disconnect <mac>` for scripting.

### 10.4 Agent placement

The pairing agent lives in the **daemon**, not the panel. A device can start pairing at any time, including when no panel is open. The agent also has to survive panel restarts, and the daemon already owns the system-bus connection.

### 10.5 Pairing agent

- Export `org.bluez.Agent1` at `/org/vitals/agent`.
- Register it with `AgentManager1.RegisterAgent(path, "KeyboardDisplay")`, then call `RequestDefaultAgent`. `KeyboardDisplay` lets BlueZ choose the best association model for each device.
- Register again whenever the `org.bluez` name reappears.
- Agent method calls are handled asynchronously. The daemon keeps the `Gio.DBusMethodInvocation`, emits a `prompt` event, and returns from the method call without replying yet:

```json
{"v":1,"event":"prompt","data":{"id":"p3","kind":"confirm_passkey","device":"AA:BB:…","alias":"WH-1000XM5","passkey":"123456"}}
```

- The panel answers with `{"op":"prompt.reply","id":"p3","accept":true,"value":null}`. The daemon then returns the stored invocation, or `org.bluez.Error.Rejected` / `Canceled`.
- If nobody answers within 30 s, the daemon replies `Rejected` itself. BlueZ's `Cancel()` clears the prompt.
- Only one prompt can be outstanding at a time. A second one is rejected, which BlueZ does not do in practice.

| Agent1 method | `kind` | Reply value |
|---|---|---|
| `RequestConfirmation` | `confirm_passkey` | none |
| `RequestPinCode` / `RequestPasskey` | `enter_pin` / `enter_passkey` | string (1–16 chars) / uint32 (0–999999), validated by the daemon |
| `DisplayPinCode` / `DisplayPasskey` | `display_pin` / `display_passkey` | none, replied immediately; `entered` updates the prompt |
| `RequestAuthorization` | `authorize_pairing` | none |
| `AuthorizeService` | `authorize_service` | auto-accept when the device is `Trusted`, otherwise prompt |

- **Prompt while the panel is closed.** The daemon sets the Bluetooth panel visible (which opens it) and sends the SPEC 8.3 notification.
- **Prompt with no panel connected.** If no panel process is connected at all, the daemon rejects the request instead of leaving the device waiting.

**Coexistence.** BlueZ has a single default agent, and whichever process requests it last wins. `/etc/xdg/autostart/blueman.desktop` exists, and Hyprland does not run XDG autostart today. The README should still say to disable `blueman-applet` if it is ever started (a `Hidden=true` override in `~/.config/autostart/`). **Open blueman** in the footer launches only the manager, and the manager registers no agent.

### 10.6 Audio profile switching

Audio cards are owned by PipeWire, not BlueZ. Profile switching sits behind an `AudioProfiles` boundary in `vitals/linux/pipewire.py` that the later Audio module (#13) takes over:

- Find the card named `bluez_card.<MAC with underscores>` with `pactl --format=json list cards`. This works through pipewire-pulse, which is present. The list gives the available profiles and the active one.
- Map profile names to display labels:
  - `a2dp-sink*` → **Hi-Fi (A2DP)**
  - `headset-head-unit*` → **Headset (HFP)**
  - `off` → **Off**
  - Any other profile is listed under its PipeWire description.
- Switch with `pactl set-card-profile <card> <profile>`, run with Gio.Subprocess and an argument vector.
- Refresh the card list when an audio device connects, when the panel opens, and after a switch. There is no `pactl subscribe` while the panel is hidden, which keeps the idle cost at zero.

A device counts as audio when its `UUIDs` contain Audio Sink (`0000110b…`) or Handsfree / Headset (`0000111e…`, `00001108…`).

### 10.7 Panel focus holds

The 0.5 s focus-out grace (#24) closes the panel whenever focus leaves it. A pairing prompt, the file chooser and the forget confirmation all need focus to stay put. `BasePanel` therefore keeps a hold counter:
- `hold()` cancels any pending hide and suppresses focus-out dismissal.
- `release()` re-arms dismissal. If the panel no longer has focus when the last hold is released, the normal 0.5 s timer starts.
- Esc and the indicator click still close the panel during a hold. Closing it cancels the outstanding prompt, which the daemon then rejects.

**File chooser.** Use `Gtk.FileChooserNative`, so the xdg-desktop-portal chooser is used where one exists. Its window is a normal toplevel and will be tiled by Hyprland. That is acceptable for a short-lived dialog, and it is the only place this module creates a normal window. The panel holds focus for the dialog's lifetime.

### 10.8 Sending files (OBEX)

- obexd runs on the **session** bus, and the daemon is a user service, so it can call obexd directly.
- `org.bluez.obex.Client1.CreateSession(mac, {"Target": "opp"})`, then `ObjectPush1.SendFile(path)`. The call returns a `Transfer1` object.
- Watch that object's `Status`, `Transferred` and `Size` through `PropertiesChanged`, and publish them as the module's `transfer` field.
- Cancel calls `Transfer1.Cancel`. Remove the session when the transfer completes or fails.
- The path comes from the panel's file chooser. The daemon accepts only absolute paths to regular files that the user can read, and never follows anything else from the client.
- Only one transfer runs at a time.
- A device offers Send file when its `UUIDs` include OBEX Object Push (`00001105…`).

### 10.9 Resource use

- With the panel hidden, the cost is one D-Bus match rule set, the rfkill uevent and the agent object. There are no timers.
- `RSSI` changes arrive only during discovery, and discovery only runs while the Add device section is open.
- **Battery levels.** BlueZ refreshes `Battery1` (GATT Battery Service) when the device notifies. Vitals does not poll devices, so some peripherals may only report a new level on reconnect.

### 10.10 Testing

**Automated tests**
- `BluezBackend` is the seam. Tests use a `FakeBluez` that holds an object tree dict and emits the same callbacks.
- Fixtures cover:
  - Adapter missing, powered off, and soft and hard rfkill.
  - Devices with and without `Battery1`.
  - `bluetoothd` restarts (name owner lost and regained).
  - A device removed while busy.
  - Every BlueZ error-to-code mapping.
- Agent tests drive each `Agent1` method with a fake invocation. They check event emission, the reply value, the 30 s timeout (injected clock), `Cancel()`, prompts arriving while the panel is closed, and the no-panel rejection.
- Action tests check MAC validation, the per-device `busy` guard, and discovery ownership: discovery stops when the client disconnects or the panel closes, and on the 60 s timer.
- `AudioProfiles` and OBEX tests use a fake subprocess and a fake obex bus. They check profile mapping from recorded `pactl --format=json list cards` output.
- No test may change the real adapter, pairings or audio profile.

**Manual acceptance on the target machine**
- Run through every criterion in SPEC 8.5.
- Pair one device of each type available: the mouse (Just Works), headphones (A2DP and HFP switching), and a phone (passkey confirmation and an OBEX send).
- Restart `bluetooth.service` with the panel open, and check that the panel recovers and the agent re-registers (`busctl --system tree org.bluez` and a fresh pairing).
- Check that the `/dev/rfkill` unblock works from the user service. Run `getfacl /dev/rfkill`; it printed nothing when this design was written, so the ACL holder is unconfirmed. If the write is denied, record the failure here and fall back as described in §10.3.
