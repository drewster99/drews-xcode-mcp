# TODO

## Planned Features & Fixes

### Testing & Discovery
- [ ] Add filesystem fallback to `get_xcode_projects` when mdfind returns empty results
- [ ] Implement selective test execution with `-only-testing` flag support
- [ ] Add `get_available_destinations` tool to query run destinations
- [ ] Create mock-based test suite that doesn't trigger Xcode UI alerts
- [ ] Fix test projects not being found immediately after creation (Spotlight indexing delay)

### Build & Run Enhancements
- [ ] Add destination parameter to `run_project_tests` for specific device/simulator targeting
- [ ] Support test filtering by class/method in `run_project_tests`
- [ ] Add timeout configuration for build and test operations
- [ ] Implement parallel test execution support
- [ ] Allow fetching the build log of an **in-flight** scheme action, so a build
      still in progress can be inspected without waiting for it to finish (see
      "In-Flight Build Log Retrieval" below)

### Project Creation
- [ ] Make `create_project` toolchain-aware instead of emitting a format frozen in source (see "Project Template Generation — Toolchain Awareness" below)

### Documentation & Quality
- [ ] Create CHANGELOG.md with version history
- [ ] Add ROADMAP.md for future feature planning
- [ ] Create TEST_GUIDE.md with comprehensive testing instructions
- [ ] Improve error messages with troubleshooting suggestions
- [ ] Document which operations require Xcode to be open vs closed

### Code Quality
- [ ] Add proper error handling for "Can't get workspace document" errors
- [ ] Improve AppleScript error messages with more context
- [ ] Add retry logic for transient Xcode automation failures
- [ ] Cache recent project paths to improve discovery speed

---

## Implementation Details

### Project Template Generation — Toolchain Awareness

**Current behavior (as of this writing)**: `create_project` does NOT use Xcode at
all to scaffold a project, and does NOT consult the `xcode-select`-selected
toolchain. The entire project is synthesized in-process from hardcoded Python
string constants in `drews_xcode_mcp/utils/project_templates.py`. There is no
`xcrun`, no `DEVELOPER_DIR`, no read of Xcode's `.xctemplate` bundles, and no
filesystem copy — the only filesystem call in that module is `open(path, 'x')`
(a write). `generate_project()` fills the templates with fresh UUIDs and writes
each file directly.

The eight baked-in template constants and what they produce:
| Constant | Produces |
|---|---|
| `PBXPROJ_TEMPLATE` | `project.pbxproj` |
| `WORKSPACE_DATA_TEMPLATE` | `project.xcworkspace/contents.xcworkspacedata` |
| `APP_SWIFT_TEMPLATE` | `{Identifier}App.swift` |
| `CONTENT_VIEW_TEMPLATE` | `ContentView.swift` |
| `ASSETS_CONTENTS_JSON` | `Assets.xcassets/Contents.json` |
| `ACCENT_COLOR_CONTENTS_JSON` | `AccentColor.colorset/Contents.json` |
| `APP_ICON_IOS_CONTENTS_JSON` / `APP_ICON_MACOS_CONTENTS_JSON` | `AppIcon.appiconset/Contents.json` |

Real Xcode templates (for reference) live at:
`<selected Xcode>.app/Contents/Developer/Library/Xcode/Templates/…`
(resolve the active Xcode with `xcode-select -p` / `xcrun --find`).

**Problem**: The generated format is FROZEN in source, independent of whatever
Xcode is selected:
- `objectVersion = 77` + `PBXFileSystemSynchronizedRootGroup` → requires Xcode 16+.
  On an older selected Xcode the project may fail to open or prompt to upgrade.
- Default deployment target hardcoded to `26.0`, default bundle id
  `com.example.{identifier}`.
- Only a SwiftUI App template (iOS/macOS). No other product types, no tests
  target, no Storyboard/UIKit, no Swift package, etc.
- As Xcode evolves the pbxproj format (object version bumps, new isa types),
  these strings silently drift out of date and must be hand-edited.

This is intentional decoupling (project creation works with zero dependency on
where/whether Xcode's templates exist), but it trades freshness and fidelity for
that independence.

**Goal**: Let `create_project` respect the selected toolchain (and/or broaden the
template set) without giving up the "works offline / no Xcode UI" property where
possible.

**Options (roughly increasing fidelity / effort)**:
1. **Version-adaptive hardcoded templates (smallest change)**. Detect the active
   Xcode version (`xcodebuild -version` / parse `version.plist` under
   `xcode-select -p`) and pick an `objectVersion` + group style that matches
   (e.g. emit a pre-77 file-group layout for Xcode < 16). Keep generation
   in-process. Pros: still no Xcode UI, no template parsing. Cons: we maintain N
   format variants by hand.
2. **Derive defaults from the toolchain**. Even if we keep our own pbxproj
   strings, pull the default deployment target and available SDKs from the
   selected Xcode (`xcrun --sdk iphoneos --show-sdk-version`, etc.) instead of
   hardcoding `26.0`. Low effort, removes the most surprising hardcode.
3. **Drive Xcode's real templates** via the toolchain. Investigate scaffolding
   from `…/Library/Xcode/Templates/…` (the same `.xctemplate` bundles Xcode's
   "New Project" uses) so output matches exactly what a user gets in the IDE.
   This is non-trivial: the template format (`TemplateInfo.plist`, ancestors,
   option substitution) is undocumented and Xcode applies it through internal
   machinery, not a public CLI. Highest fidelity, highest risk/maintenance.
4. **Expand product types** regardless of source: add a tests target, a macOS
   AppKit/SwiftUI choice, a Swift Package option, etc. — orthogonal to where the
   format comes from, but worth tracking here.

**Recommendation**: Start with options 1 + 2 (version-adaptive `objectVersion`
and toolchain-derived deployment target/SDK). They remove the real correctness
risk (a frozen format that breaks on older/newer Xcode) while preserving the
no-Xcode-needed generation path. Treat option 3 as research-only until there's a
demonstrated need for exact IDE parity.

**Validation when implemented**:
- Generate on machines with different `xcode-select` targets and confirm the
  project opens cleanly in each (no upgrade prompt, no "damaged project").
- Round-trip: open the generated project, let Xcode re-save, and diff the
  pbxproj to see what Xcode would have changed.
- Keep a regression test that opens each generated variant headlessly.

**Key files**: `drews_xcode_mcp/utils/project_templates.py` (the templates +
`generate_project`), `drews_xcode_mcp/tools/create_project.py` (validation +
entry point), `drews_xcode_mcp/security.py` (`validate_parent_for_new_project`).

### Selective Test Execution (GET_RUN_DESTINATIONS research)

**Problem**: AppleScript's `test workspaceDoc` command doesn't support filtering specific tests. Need to use `xcodebuild test -only-testing:TestBundle/Class/testMethod`.

**Key Finding**: AppleScript's `active run destination of workspaceDoc` property always returns `missing value` - it's broken/unimplemented.

**Solution**: Use `xcodebuild -showdestinations` to get available destinations:
```bash
xcodebuild -showdestinations -project <path> -scheme <scheme>
# Returns: { platform:iOS Simulator, id:ABC123, OS:17.0, name:iPhone 15 Pro }
```

**Implementation approach**:
1. Query destinations using `xcodebuild -showdestinations`
2. Parse output to extract destination info
3. Select first destination as default (or let user specify)
4. Build xcodebuild command with `-destination` flag

Example code for parsing destinations:
```python
def get_available_destinations(project_path: str, scheme: str):
    is_workspace = project_path.endswith('.xcworkspace')
    flag = '-workspace' if is_workspace else '-project'

    cmd = ['xcodebuild', '-showdestinations', flag, project_path, '-scheme', scheme]
    result = subprocess.run(cmd, capture_output=True, text=True)

    # Parse: { platform:iOS Simulator, id:ABC123, name:iPhone 15 Pro }
    # Return: [{'platform': 'iOS Simulator', 'id': 'ABC123', 'destination_string': 'id=ABC123'}]
```

### Xcode Project Discovery Issues

**Problem**: `get_xcode_projects` uses mdfind (Spotlight) which has indexing delays for newly created/copied projects.

**Current behavior**:
- Tests copy projects to working directory
- mdfind doesn't immediately index these
- Projects aren't found, tests fail

**Solution**: Implement filesystem fallback
```python
def get_xcode_projects_with_fallback(search_path):
    # Try mdfind first (fast for indexed files)
    results = mdfind_projects(search_path)

    if not results and os.path.exists(search_path):
        # Fallback to os.walk for newly created projects
        results = []
        for root, dirs, files in os.walk(search_path):
            for d in dirs:
                if d.endswith(('.xcodeproj', '.xcworkspace')):
                    results.append(os.path.join(root, d))

    return results
```

### Test Infrastructure Notes

**Current test structure**:
```
test_projects/
├── fromXcode/           # Original projects from Xcode (DO NOT MODIFY)
├── templates/           # Modified copies for testing
│   ├── SimpleApp/       # Basic command line app
│   ├── BrokenApp/       # App with compile errors
│   └── ConsoleApp/      # App with console output
└── working/             # Temporary test execution
```

**Known issues**:
1. Build tests fail with "Can't get workspace document" when project not open
2. Xcode shows UI alerts about missing projects during tests
3. Some AppleScript commands require workspace to be loaded first

**Test runner fix that works**:
```python
# Set ALLOWED_FOLDERS in MCP module directly
import drews_xcode_mcp.__main__ as mcp_server
mcp_server.ALLOWED_FOLDERS = {str(self.working_dir)}
```

### Build Error Handling

**Current implementation**: Returns JSON with structured errors/warnings
```json
{
    "full_log_path": "/tmp/xcode-mcp-server/logs/build-{hash}.txt",
    "summary": {"total_errors": N, "total_warnings": M},
    "errors_and_warnings": "error: ...\nwarning: ..."
}
```

**Improvements needed**:
- Better parsing of Swift vs Objective-C errors
- Group errors by file
- Extract fix-it suggestions

### Console Output Moved in Xcode 27 (ConsoleSessionSection)

**Symptom**: runs under Xcode 27 beta returned no runtime output at all, while
the same project under Xcode 26.6 returned it normally.

**Cause**: Xcode 27 writes result bundles at legacy format version **3.60** and
moved runtime console output into a new `ConsoleSessionSection`. The
`ConsoleLogSection` that `xcresulttool get log --type console` reads is left as a
~117-byte stub. That command still *succeeds* and returns `{"items": []}`, so
nothing looked like an error — the output was simply gone.

Evidence from one AgentSmith project, two runs minutes apart:

| bundle format | Xcode | `ConsoleLogSection` | `ConsoleSessionSection` | `--type console` |
|---|---|---|---|---|
| 3.58 | 26.6 release | 45,087 B | *absent* | 45 items |
| 3.60 | 27 beta | 117 B (stub) | 325,728 B | 0 items |

Across all 16 local Launch bundles the split was exact: every 3.60 bundle
returned 0 items from the supported command; only 3.58 bundles returned any.

**Current handling** (`utils/xcresult.py`): when `get log --type console` yields
nothing, look for a `consoleSessionRef` on the action result and read that
section via `xcresulttool get object --legacy`. Bundles from older Xcode have no
such ref and cost one extra lookup only on runs that genuinely produced no
output. Within a session, `log` entries map back onto the old `kind` values via
`OSLogType` (16 → error, 17 → fault), `data` entries are raw stdout/stderr split
into lines, and LLDB `progress` events are dropped — a single run carried 324 of
them, and the old console section never included them.

**Why this needs revisiting**:
- `get object --legacy` is explicitly deprecated ("will be removed in a future
  release"). It is currently the only route to `ConsoleSessionSection`.
- This may simply be a beta bug. The supported command is still tried first, so
  if a later Xcode populates `ConsoleLogSection` again, the deprecated path stops
  being reached with no code change — but that should be confirmed and the
  fallback removed once it is.
- Watch for a supported replacement (a `--type` value that exposes the session,
  or a non-legacy object API). Prefer it as soon as one exists.
- The session format carries richer per-entry data than the old one (`pid`,
  `tid`, `senderWallTime`, `senderImageName`, `processSessionUUID`). Only
  message/subsystem/category are used today; timestamps in particular could
  improve the runtime output.

### In-Flight Build Log Retrieval

**Idea**: expose the `build log` of a scheme action that has *not* finished, so a
long build can be watched or diagnosed while it runs (progress, which target is
compiling, first error as soon as it appears) instead of only after completion.

**It works today, and is deliberately unused.** `build log of <scheme action
result>` is readable while `status` is `running` — verified against a live Xcode
build in progress, which returned 347,344 characters of partially-written log.
So this needs no new Xcode capability, only a tool to surface it.

**Why the current code skips it.** `utils/scheme_action.py` fetches the build log
only for statuses in need of explanation — everything except `succeeded`,
`running`, and `not yet started` (see `_STATUSES_NEEDING_NO_EXPLANATION` and the
AppleScript guard it generates). Two reasons, both about *this* use of the log
rather than the log itself:

1. **Correctness.** That code decides whether a build failed. A partial log can
   contain errors for files already processed while the build is still going,
   so a verdict drawn from it would be premature.
2. **Cost.** The log is large and crosses an `osascript` boundary. Fetching
   hundreds of KB on every successful run to answer a question the status
   already settles is pure waste.

Neither objection applies to a tool whose *purpose* is showing in-progress
output — there the partial log is the point.

**Where `running` shows up in the existing tools** (relevant if this is built):
- `run_project_until_terminated` — the timeout path issues `stop workspaceDoc`
  then waits up to 20s; an app that ignores the stop leaves the action `running`
  when the report is read.
- `run_project_with_user_interaction` — the same 20s give-up in force-stop
  verification, plus the `last scheme action result` fallback, which can point at
  a different, concurrently running action.

**Design sketch**:
- A `get_build_progress` (or similar) tool taking a project path, returning the
  current status plus a tail of the in-flight log, with `max_lines` to bound the
  payload rather than shipping the whole thing.
- Tail it AppleScript-side if possible — pulling 300KB+ across `osascript` just
  to discard all but the last 50 lines is the cost problem above, restated.
- Reuse `build_action_result_report_applescript` for the lookup-by-id path so
  progress is reported for a specific action rather than whatever ran last.

### Runtime Output Structure

**Current format**: JSON with intelligent filtering
```json
{
    "full_log_path": "/tmp/xcode-mcp-server/logs/runtime-{hash}.txt",
    "errors": ["error lines with context"],
    "warnings": ["warning lines with context"],
    "matching_lines": ["lines matching regex filter"],
    "summary": {"total_lines": N, "errors": X, "warnings": Y}
}
```

**Note**: Errors and warnings are always included with full context, never lost to filtering.

### Recent Projects Support

**Already implemented** in `get_xcode_projects`:
- Reads from `~/Library/Preferences/com.apple.dt.Xcode.plist`
- `IDERecentProjectDocumentURLs` key contains recent projects
- Shown first in results when `include_recents=True`

### Notification System Status

**Decision**: Not implementing typed notification system from NOTIFICATIONS_PLAN.md

**Current state**:
- Simple notification functions in `utils/applescript.py`
- All notifications use "Drew's Xcode MCP" title
- Global `NOTIFICATIONS_ENABLED` flag works
- History tracking implemented for debugging

### Important File Paths

**xcresult bundles**:
- Runtime logs: `~/Library/Developer/Xcode/DerivedData/*/Logs/Launch/*.xcresult`
- Test results: `~/Library/Developer/Xcode/DerivedData/*/Logs/Test/*.xcresult`
- Build logs: Captured directly from Xcode UI via AppleScript

**Parsing xcresults**:
```bash
# Get runtime console output
xcrun xcresulttool get --path <xcresult> --id <logRef_id>

# Get test results
xcrun xcresulttool get --path <xcresult> --format json
```

### AppleScript Gotchas

1. **Workspace loading**: Must wait for `loaded of workspaceDoc is true`
2. **String escaping**: Backslashes and quotes must be escaped
3. **Build/test results**: Poll `completed of buildResult is true` to know the
   action is *over* — but `completed` says nothing about whether it *worked*. It
   goes true for every terminal outcome, success and failure alike. Read
   `status of buildResult` for the outcome. This matters most for `run`, which
   in Xcode means build-and-run: a run whose build fails still completes, so
   polling `completed` alone reports a failed build as a successful run. See
   `utils/scheme_action.py`.
4. **`error message` is usually `missing value`**: Xcode populates it only when
   `status` is `error occurred`. Coercing it with `as string` yields the literal
   text `"missing value"` rather than raising, so callers must treat that string
   as "absent".
5. **Project paths**: Remove trailing slashes, use absolute paths
6. **Timeout handling**: AppleScript operations can hang, need subprocess timeouts

### Security Model

**Current implementation**:
- `ALLOWED_FOLDERS` environment variable or CLI args
- All paths validated: absolute, exist, directory, no '..'
- Default to `$HOME` if not specified
- Every tool validates project path against allowed folders

