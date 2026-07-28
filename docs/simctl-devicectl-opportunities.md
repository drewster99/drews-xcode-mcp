# simctl / devicectl — Untapped Capabilities

A survey of `xcrun simctl` and `xcrun devicectl` functionality that this MCP
server does **not** currently expose, filtered to capabilities that are *not*
already covered by our Xcode AppleScript control. Intended as a backlog /
design reference for future tools.

> Captured against the toolchain installed on this machine. Subcommand sets
> shift between Xcode releases — re-run `xcrun simctl help` and
> `xcrun devicectl <subcommand> --help` before implementing.

---

## What we already cover (for contrast)

| Area | Mechanism today |
|---|---|
| Build / run / test / clean / stop | Xcode AppleScript |
| Run-destination list / set / get-active | Xcode / xcodebuild |
| Runtime console output | Parsed from `.xcresult` after an Xcode run |
| Simulator screenshot (stills) | `simctl io <udid> screenshot` (`take_simulator_screenshot`) |
| Booted-simulator list | `simctl list devices booted` (`list_booted_simulators`) |

Everything below is **not** exposed today.

---

## `simctl` — simulator state & lifecycle

None of this is reachable through Xcode AppleScript. This is the "set up test
conditions" surface — the highest-leverage area for an AI-driven build/test loop.

### Tier 1 — high value, low complexity, zero overlap

| Proposed tool | Command | Why it's useful |
|---|---|---|
| `record_simulator_video` | `io <udid> recordVideo [--codec=h264\|hevc] [--display=…] <file>` (SIGINT to stop) | We only capture stills. Record a repro or a full flow as a movie — natural complement to the screenshot tools. `simctl` writes `Recording started` to stderr once the first frame lands. |
| `open_url_in_simulator` | `openurl <udid> <url>` | Drive the app to a screen; test universal links & custom URL schemes. Impossible via Xcode. |
| `erase_simulator` | `erase <udid>` | Clean-slate a sim for deterministic test runs. |
| `boot_simulator` / `shutdown_simulator` | `boot <udid>` / `shutdown <udid>` / `reboot <udid>` | Control which sim is up without launching Xcode. |
| `set_simulator_permission` | `privacy <udid> grant\|revoke\|reset <service> [bundleid]` | Pre-grant camera / photos / location / contacts / microphone / motion / etc. so automated flows don't stall on a permission alert. `reset` prompts on next use. |
| `get_app_container_path` | `get_app_container <udid> <bundleid> [app\|data\|groups]` | Locate the installed app's container, then reuse existing `get_directory_tree` / `get_directory_listing` to inspect it. |

### Tier 2 — screenshot polish & scenario injection

| Proposed tool | Command | Why it's useful |
|---|---|---|
| `set_simulator_status_bar` | `status_bar <udid> override --time … --dataNetwork … --wifiBars … --cellularBars … --batteryState … --batteryLevel …` (and `clear`) | Clean, deterministic status bar for **App Store screenshots** (fixed time, full bars, 100% battery). |
| `set_simulator_appearance` | `ui <udid> appearance light\|dark` | Light/dark screenshot + behavior passes. |
| `set_simulator_content_size` | `ui <udid> content_size <category\|increment\|decrement>` | Dynamic Type / accessibility sizing. |
| `set_simulator_contrast` | `ui <udid> increase_contrast enabled\|disabled` | Accessibility pass. |
| `send_simulator_push` | `push <udid> [bundleid] (<json file> \| -)` | Test remote-notification handling. Payload ≤ 4096 bytes; `aps` key required; may embed `Simulator Target Bundle`. |
| `set_simulator_location` | `location <udid> set <lat>,<lon>` / `run <scenario>` / `start …waypoints…` | GPS point, named scenario, or interpolated route for location-dependent features. |
| `add_simulator_media` | `addmedia <udid> <paths…>` | Inject photos / live photos / videos / contacts into the library. |
| `simulator_pasteboard` | `pbcopy` / `pbpaste` / `pbsync` | Read/write the simulator pasteboard. |

### Tier 3 — app lifecycle without a rebuild

| Proposed tool | Command | Notes |
|---|---|---|
| `install_app_in_simulator` | `install <udid> <path.app>` | |
| `uninstall_app_from_simulator` | `uninstall <udid> <bundleid>` | |
| `launch_app_in_simulator` | `launch [--console\|--console-pty] [--stdout=…] [--stderr=…] [--terminate-running-process] <udid> <bundleid> [args…]` | Env via `SIMCTL_CHILD_*` prefix. |
| `terminate_app_in_simulator` | `terminate <udid> <bundleid>` | |

> **Overlap caveat:** launching a *freshly built* app with console output is
> already what our Xcode run tools do. The *new* value here is fast
> **re-launch / terminate / process control and env+arg injection without
> triggering a rebuild** — a different workflow, not the same one.

### Other simctl subcommands (lower priority)
`diagnose`, `spawn`, `getenv`, `keychain`, `clone`, `create`, `delete`,
`rename`, `upgrade`, `runtime`, `icloud_sync`, `status_bar list/clear`,
`appinfo`, `listapps`, `install_app_data`, `personalization`.

---

## `devicectl` — physical hardware (an entirely new platform surface)

Our whole toolset is Xcode + simulator. Real connected devices are only reachable
today by the user manually picking one as an Xcode run destination. `devicectl` is
the CLI for them. It supports stable JSON output via `--json-output <path>` (use
`-` for stdout), which is the recommended integration path.

This is a bigger lift than the simctl work (device discovery, pairing/trust,
Developer Disk Image, JSON parsing), but it's the only category that is a
*completely new capability class* rather than an enhancement.

### Genuine gaps (can't be done any other way today)

| Proposed tool | Command | Why it's useful |
|---|---|---|
| `screenshot_device` | `device capture …` | **Screenshot a real device.** True gap: `take_simulator_screenshot` is sim-only, and the window/app screenshot tools capture *macOS* windows. We have no way to screenshot physical hardware. |
| `list_connected_devices` | `list devices --json-output -` | Enumerate connected physical devices with stable JSON. |

### Additional devicectl capabilities

| Area | Command |
|---|---|
| Install / uninstall a build on hardware | `device install app <path>` / `device uninstall app <bundleid>` |
| Launch / control processes | `device process launch [--console] [--start-stopped] [-e <env-json>] <bundleid> [args…]`, `terminate`, `signal`, `suspend`, `resume`, `sendMemoryWarning`, `awaitTermination` |
| Open URL on device | `device process openURL <url>` |
| Device introspection | `device info details\|apps\|processes\|displays\|lockState\|appearance\|files` |
| File transfer | `device copy` |
| Device state | `device reboot`, `device orientation`, `device settings`, `device pasteboard` |
| Diagnostics | `devicectl diagnose`, `device sysdiagnose` |
| Pairing / DDI management | `manage pair\|unpair`, `manage ddis`, `manage loggingProfile` |

---

## Suggested rollout order

1. **Tier-1 simulator batch** — `record_simulator_video`, `open_url_in_simulator`,
   `erase_simulator` + `boot`/`shutdown`, `set_simulator_permission`,
   `get_app_container_path`. No new hardware handling; slots cleanly next to the
   existing sim tools.
2. **Tier-2 screenshot/scenario batch** — status bar, appearance, content size,
   push, location. Directly improves screenshot quality and test coverage.
3. **Tier-3 app lifecycle** — install/launch/terminate without a rebuild.
4. **devicectl phase** (distinct effort) — lead with `screenshot_device` (`capture`)
   and `list_connected_devices`, then install/launch/introspect on hardware.

## Implementation conventions to match

Follow the existing tool modules (e.g. `take_simulator_screenshot.py`):

- `@mcp.tool(annotations=TOOL_READONLY)` for read-only tools; use the mutating
  annotation for state-changing ones.
- `@apply_config` decorator.
- Raise `XCodeMCPError` with actionable messages; map known stderr strings
  (`Invalid device`, `not booted`, …) to friendly errors as
  `take_simulator_screenshot` does.
- UDID-or-first-booted resolution via `_get_booted_simulators()` /
  `get_screenshot_path()` from `utils/screenshot.py`.
- `show_result_notification` / `show_error_notification` for optional
  macOS notifications.
- Prefer `--json-output -` for `devicectl` and parse JSON rather than screen-scraping.
- Register new modules in `drews_xcode_mcp/tools/__init__.py`.
