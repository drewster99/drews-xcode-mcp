# XPC LaunchAgent Host — Findings and Migration Path

Whether this server should stop being a per-client `uvx` process and become a thin
stdio bridge to a single signed LaunchAgent. The architecture was built and measured
end-to-end in a separate proof of concept (`~/cursor/addertest-xpc-mcp`); this
records what that proved, what it did **not** prove, and what adopting it here would
actually cost.

> Measured 2026-07-24 → 2026-07-28 on macOS 26.5.2 (build 25F84), against
> `addertest-xpc-mcp` 0.1.1 (2), signed Developer ID `P8MA38JTXY` and notarized.
> TCC behaviour is undocumented and version-sensitive — re-measure before relying on
> any of it on a new major macOS.

---

## The problem

Two properties this server cannot have today, both consequences of being a
`uvx`-spawned Python process (`drews-xcode-mcp = "drews_xcode_mcp:main"`, 1.3.18).

**Permissions attach to the client, not to us.** TCC grants go to the *responsible
process*, which for a `uvx` server is whichever MCP client launched it. Visible right
now in the system TCC database:

| `kTCCServiceScreenCapture` client | Value |
|---|---|
| `com.apple.Terminal` | 2 (granted) |
| `com.google.Chrome` | 2 |
| `com.microsoft.teams2` | 2 |
| `com.nuclearcyborg.maccontrol` | 2 |

Screenshots work from a Claude Code session in Terminal because *Terminal* holds
Screen Recording. Run the same server from Cursor, VS Code, or a second client and
that client must be granted separately. Every client, every permission, again.

**Every client is its own actor.** N clients means N server processes racing over one
Xcode instance, one simulator set, one build directory. Independent processes cannot
queue or reject each other; a single long-lived host can.

---

## What was proven

All of the following was measured, not reasoned about.

| # | Finding | Evidence |
|---|---|---|
| 1 | **Automation composes to the containing app.** A nested `SMAppService` LaunchAgent in `Contents/Helpers/` sent an AppleEvent; TCC recorded the grant against the outer app. | User TCC db: `kTCCServiceAppleEvents \| com.nuclearcyborg.addertest \| 2` — never the `.host` identity |
| 2 | **Screen Recording composes identically.** Same shape, different service. Process ran as `com.nuclearcyborg.addertest.host` and said so; TCC still attributed the outer app. | System TCC db: `kTCCServiceScreenCapture \| com.nuclearcyborg.addertest \| client_type 0` |
| 3 | **Grants survive updates.** The installed `.app` was replaced five times across the session with independently-signed builds of the same identity. | Every subsequent `frontmost_app` returned immediately — no re-prompt at any point |
| 4 | **Host restarts are invisible to clients.** The host was killed repeatedly mid-session; launchd respawned it on XPC demand and the bridge reconnected. | Host pid moved 53737 → 57636 → 64679 → 71224 → 79217 with no client-visible error |
| 5 | **Asking for Screen Recording shows no dialog.** From a faceless `LSUIElement` agent, `CGRequestScreenCaptureAccess()` registered the app in System Settings silently. | `auth_reason 4` (system-set); no consent process was spawned — the two candidates predated the call by 13 hours and 12 days respectively |

Finding 2 was the one that mattered. `kTCCServiceScreenCapture` lives in the
**system** TCC database while `kTCCServiceAppleEvents` lives in the **user** one —
different store, different lifecycle — so composition genuinely could not be inferred
from finding 1. Three of our four screenshot tools depend on it —
`take_app_screenshot`, `take_window_screenshot` and `take_xcode_screenshot` all shell
out to `screencapture`. `take_simulator_screenshot` goes through `simctl io` and needs
no grant at all.

Finding 3 makes the whole thing shippable: **bundle identifier and Developer ID
identity become permanent, load-bearing API** from the first user grant onward.
Change either and every user re-grants everything.

Finding 5 is a mixed blessing. Nothing can block an MCP client's `initialize`
handshake — but there is also no prompt to click, so onboarding must *tell* the user
to go enable the toggle.

---

## What does not work

Things tried and abandoned, recorded so they are not retried.

- **Unregistering duplicate bundles from LaunchServices does not hold.** A full
  cleanup left two registered paths — the app and its nested host, which is the floor.
  One release build took the total straight back to nine. Only removing the bundle
  from disk is durable.
- **LaunchServices ignores both the extension and a dot-prefix.** A backup named
  `AdderTest.app.pre-icon-fix` registered anyway — discovery is by `Info.plist`.
  Anything bundle-shaped in a scanned directory becomes a second claimant of the
  identifier.
- **Giving the nested helper its own icon does not fix the blank permission prompt.**
  Tested and falsified; the Automation prompt still rendered no icon, and the change
  cost 33.6% of the wheel (281,078 → 375,534 bytes) for a duplicated 145 KB `.icns`.
  Cause still unknown.
- **TCC cannot be debugged from the unified log.** `log show --predicate 'subsystem ==
  "com.apple.TCC"'` returns zero entries — fully redacted without an Apple
  configuration profile. Every permission question costs a `tccutil reset` and a human
  click.

---

## Open questions — settle these before committing

**1. Does a subprocess inherit the composed identity?** This is the decisive one, and
it is bigger than it first looks. Both proofs used *in-process* calls — `NSAppleScript`
for Automation, CoreGraphics for Screen Recording. Almost nothing in this server works
that way. Screenshots shell out:

```python
subprocess.run(['screencapture', '-x', '-l', str(window_id), screenshot_path], ...)
```

and *every* Xcode interaction shells out to `osascript`. So the question is not "will
screenshots still work" — it is whether the composed identity survives into a child
process **at all**, and therefore whether essentially the entire tool surface keeps its
grant.

TCC generally attributes a child to its responsible parent, which is the reason to
expect this works. But that is a different mechanism from the bundle-identity
composition proven above, and it **was not measured**. Do not assume it.

This question also decides between the shapes below. If subprocess attribution holds,
shape C works as written. If it does not, C collapses — the TCC-gated work has to be
reimplemented in-process in the host (`NSAppleScript`, `ScreenCaptureKit`), which is
shape A or a Swift-primitives hybrid.

*Test:* add two tools to the POC host — one shelling out to `screencapture`, one to
`osascript` — call each, and read which client the TCC databases record. An afternoon,
and it derisks the entire project.

**2. Accessibility.** Untested here. Relevant if UI-driving tools land (and directly
relevant to `drews-mac-control-mcp`, which shares this shape). `kTCCServiceAccessibility`
has its own rules again — do not assume a third time.

**3. Periodic re-authorization.** Recent macOS re-confirms Screen Recording on a
schedule. Not observed over a long enough window to know how it presents for a
faceless agent.

---

## Three shapes, and a recommendation

| | Single grant | Single actor | Needs Q1 to hold | Cost |
|---|---|---|---|---|
| **A. Full Swift port** — every tool reimplemented in the host | ✅ | ✅ | No — calls go in-process | Enormous. Rewrites the entire tool surface. |
| **B. Hybrid** — Swift host owns only TCC-gated primitives in-process; Python stays per-client and calls it over XPC | ✅ | ❌ | No | Moderate. Still N Python processes racing. |
| **C. Bundled Python host** — ship the existing Python server *inside* the signed `.app`, run it as the LaunchAgent | ✅ | ✅ | **Yes** | Moderate, and concentrated in packaging rather than in tool code. |

**C is the one to pursue — conditional on open question 1.** TCC identity derives from
the containing app bundle, not from the language the code is written in. If the
LaunchAgent's `BundleProgram` is a bundled Python interpreter (or a small Swift
launcher that `exec`s it), every existing tool runs under the app's identity unchanged
— and gets both properties without rewriting `drews_xcode_mcp` at all.

The condition is not incidental. C's whole premise is that the Python server keeps
shelling out to `osascript` and `screencapture` exactly as it does today, and inherits
the app's grant while doing so. If subprocess attribution does not compose, that
premise is false and C offers nothing over the status quo. **Settle question 1 first;
it is the difference between a packaging project and a rewrite.**

The POC already cleared the obstacle that would otherwise sink C: **a wheel cannot
carry symlinks**, which is fatal for an embedded `Python.framework`
(`Versions/Current → A`). Nesting an *inner* zip sidesteps it permanently, because the
archive is opaque to pip and extraction is done by `ditto`, which does preserve
symlinks. That mechanism is proven and shipping in the POC.

The real risk in C is code signing: every native extension `.so` in the bundled
environment must be signed and survive hardened runtime, and CPython may need
`com.apple.security.cs.allow-unsigned-executable-memory` or relaxed library
validation. **Prototype the signing of a bundled interpreter before committing to
this path** — it is the step most likely to fail.

---

## Migration path

Phased so each stage is independently verifiable and abandonable.

### Phase 0 — de-risk

1. **Settle open question 1 (subprocess attribution) in the POC.** An afternoon, and
   it chooses the shape: hold, and this is a packaging project; fail, and it is a
   rewrite. Nothing else should start before this answer exists.
2. Prototype signing + notarizing an `.app` containing a bundled CPython with at least
   one native extension. If this fails, fall back to shape B and re-scope.

### Phase 1 — freeze the identity contract

3. Choose the bundle identifier. It is permanent. Suggest `com.nuclearcyborg.drewsxcodemcp`.
4. Record in `CLAUDE.md` that bundle identifier and signing identity may never change,
   with the reason (finding 3). This is an architectural constraint, not a preference.

### Phase 2 — packaging

5. Build the `.app`: registrar as main executable (`SMAppService.agent` resolves the
   plist against `Bundle.main`, which is *not* the helper), bridge and host in
   `Contents/Helpers/`.
6. Port `build-release.sh` from the POC. It already encodes the expensive lessons:
   build ad-hoc then re-sign inside-out with `--options runtime --timestamp`, because
   Xcode injects `get-task-allow` and omits the secure timestamp outside an archive
   flow; and `notarytool submit --wait` exits 0 even on rejection, so status must be
   parsed.
7. Both the app **and** the nested host need the AppleEvents entitlement and an
   `NSAppleEventsUsageDescription`. Without them on the composed identity, tccd
   refuses to prompt at all — "Policy disallows prompt" — and the event fails -1743
   forever with nothing in System Settings to recover it.

### Phase 3 — delivery

8. Python becomes bootstrap only: verify installed version, reconcile duplicate
   installs, `execv` the bridge. Port `bootstrap.py` wholesale — it is small and its
   edge cases are already paid for.
9. **Budget for notarization in every release.** Today a release is instant; after
   this it is a 2–4 minute round trip needing network and credentials. Structure the
   version scheme so the app can be reused when only Python changed.

### Phase 4 — cutover

10. Keep the `uvx` entry point and command name identical. Users should notice
    nothing but the permission prompt moving from their client to the app.
11. Onboarding must direct users to System Settings for Screen Recording, since
    finding 5 means no dialog will appear.
12. Retire the old path once the shim's install base has moved.

---

## Implementation conventions to match

- **One canonical install location**, `~/Applications/<App>.app`. Never
  `/Applications` — it is group-writable by admins only, and an MCP server started
  inside a client session cannot escalate.
- **Enforce single-installation on every start**, not only when installing: a machine
  can carry the correct version *and* a duplicate simultaneously, which a version
  check cannot see. Use `mdfind "kMDItemCFBundleIdentifier == '<id>'"` — milliseconds,
  against an index macOS already maintains, versus megabytes of `lsregister -dump`
  whose format is not contractual.
- **Remove only what you created.** An abandoned staging directory is ours; anything
  else is the user's file and gets reported, never deleted.
- **Stage installs in a `.noindex` directory.** A staging directory inside
  `~/Applications` holds a complete bundle for the length of the install, and an
  interrupted install strands it permanently.
- **Compare paths with `os.path.samefile`, not `resolve()`.** Spotlight may report the
  canonical install below `/System/Volumes/Data`, and `resolve()` leaves that form
  untouched — measured. Only device and inode settle it.
- **Never leave an expanded `.app` in an indexed directory.** The POC's `dist/`
  staging copy re-registered itself repeatedly and produced duplicate launcher tiles.
- **Reject rather than coerce at the tool boundary.** `JSONSerialization` decodes both
  `true` and `1` to `NSNumber`, and `as? Bool` accepts either — verified. A tool whose
  argument mutates system state must check the underlying CFBoolean type.
