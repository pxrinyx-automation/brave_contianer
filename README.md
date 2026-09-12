# brave_shortcut

Open a new Brave tab in container slot N with `Ctrl+Shift+N` (N = 1-9),
then keep shortcut-managed tabs in stable slot order. This targets Linux with
GNOME + native Wayland and supports Brave Stable, Beta, Nightly, and Origin
variants without a hardcoded binary path.

## How it works

Brave 1.92+ ships built-in [Containers](https://brave.com/blog/containers/),
but the native `IDC_NEW_TAB_IN_CONTAINER_1..9` shortcuts
([brave-browser#56745](https://github.com/brave/brave-browser/issues/56745))
aren't in any shipped build yet. Brave *does* ship a working
`--container=<name>` CLI switch
(`components/containers/core/browser/command_line_container.cc` — matches
by **name**, not id: names aren't guaranteed unique, but ids are reserved
for temporary containers). Chromium's process singleton forwards that
switch to the already-running instance for the target profile, so a new
tab opens in the live window — no synthetic input, no XWayland, no
browser patching. An unpacked MV3 extension handles only the live tab-strip
ordering that the launcher cannot control.

```
Ctrl+Shift+N  ->  GNOME custom keybinding  ->  brave_container.py open N
                                                 -> detect running/installed Brave
                                                 -> read Preferences, find Nth container
                                                 -> validate final target
                                                 -> brave --user-data-dir=<dir> --container=<name> marker-URL

marker tab    ->  extension sees created/updated tab and re-reads it
              ->  store slot/sequence/nonce in storage.session
              ->  normalize managed tabs in that Brave window
              ->  navigate the same tab to the final target
```

The marker is an internal handoff URL of the form
`https://brave-container.invalid/#v=1&action=open&slot=N&target=...&nonce=...`.
The Python launcher still resolves the profile and container. The extension
validates the marker, assigns its session sequence, moves the tab, and finally
opens Google or the `--url` override. If a move temporarily fails, it still
opens the target and retains the record for the next normalization.

Slot N = the **Nth entry** in Brave's own container list
(`brave://settings/braveContent`), read live every time — reorder
containers there and the keys follow, matching brave-core#38435's
"live ordinal lookup" design intent (avoids the stale-mapping bug
Firefox Multi-Account Containers had when containers were reordered).

## Postmortem: Ctrl+Shift+N opened a new window, not a tab (fixed 2026-09-12)

Two independent bugs, both in `build_argv`/`cmd_open`'s launched command
line, both confirmed live and from Chromium/brave-core source:

1. **No `--user-data-dir` forced.** Brave's channel wrappers
   (`/usr/bin/brave-origin-beta`, etc.) pick the profile directory via a
   `CHROME_VERSION_EXTRA` env var, not a switch — a freshly spawned child
   doesn't inherit it. Launching the exe path directly with no
   `--user-data-dir` silently fell back to Chromium's *stable* default
   profile: a brand-new, disconnected browser instance (its own window,
   its own singleton socket, no containers) — never even reaching the
   real running instance. `launch_passthrough()` now always forces
   `--user-data-dir` (and `--profile-directory` when not `Default`),
   reusing the same resolution `load_prefs()` already relies on.
2. **No URL in the command line.** Even with the profile fixed, a bare
   `--container=<name>` sets no `HAS_CMD_LINE_TABS`
   (`chrome/browser/ui/startup/startup_tab_provider.cc`), so Chromium's
   forwarded-command-line path
   (`startup_browser_creator_impl.cc::DetermineBrowserOpenBehavior`)
   returns `BrowserOpenBehavior::NEW` — a new window — and brave-core
   skips container attachment entirely for an empty tab list
   (`brave_startup_tab_provider_impl.cc`): `--container` was a silent
   no-op. `build_argv()` now always appends a URL.

**Original Step 0 probe was invalid.** It measured renderer-process count
(12→13) as a proxy for "new tab opened", but a new *window* also adds a
renderer — the proxy couldn't distinguish the two, and said nothing about
whether the container was applied. Lesson: verify the property you
actually care about (visually confirm tab vs. window, and the container
badge), not a correlate of it.

**`brave://newtab` doesn't work as the URL.** Command-line URLs are
filtered by `chrome/browser/ui/startup/url_util.cc`
(`ValidateLaunchUrlWebUnsafe`): only web-safe schemes, `file://`, an
approved settings page, and exactly `about:blank` are allowed;
`chrome://`/`brave://` need headless mode + `--allow-chrome-scheme-url`,
which brave-core doesn't patch around. So the new-tab-page target is
unreachable through `--container`. The launcher instead defaults to
`https://www.google.com/`, a web-safe URL that opens as a real tab in the
right container and existing window. Use `open N --url <address>` to
override it; an empty `--url` is rejected so a required tab URL cannot be
removed.

## Install

Install the extension before the GNOME bindings; otherwise a shortcut opens a
marker error page instead of its final target.

1. Open `brave://extensions`, enable **Developer mode**, and choose **Load
   unpacked**.
2. Select this repository's `extension/` directory (the directory containing
   `manifest.json`).
3. If you will use `file:` targets, open the extension's **Details** and enable
   **Allow access to file URLs**.
4. Open `brave://extensions/shortcuts` and verify **Brave Container Tab
   Scheduler** has `Ctrl+Shift+0` assigned to **Normalize shortcut-managed tabs
   in every window**. Assign it there if Brave reports a conflict.
5. Test and install the GNOME bindings:

```bash
python3 -m unittest test_brave_container
node --test extension/scheduler.test.mjs
./brave_container.py doctor
./brave_container.py install --dry-run
./brave_container.py install
```

Press `Ctrl+Shift+1` .. `Ctrl+Shift+9`.

## Tab ordering

Tabs carrying a valid marker are managed; normally those tabs are created by
the numbered shortcuts. Among unpinned managed tabs in a window, the invariant
is ascending `(slot, sequence)`, where `sequence` is the order in which the
extension processes new markers. Thus opening slots `1 2 3`, then another slot
`1`, produces managed order `1 1 2 3`; repeated tabs in one slot retain their
original order.

Manual tabs are never passed to `tabs.move()` and may remain between managed
tabs. Pinned tabs are excluded; unpinning a managed tab triggers normalization.
Dragging a managed tab triggers normalization back to the invariant, while
dragging an unmanaged tab does not trigger a sort. Close, replacement, and
cross-window attachment update the session record and normalize the affected
window or windows.

`Ctrl+Shift+0` is an extension command, not a GNOME-global binding. While Brave
is focused, it normalizes every normal Brave window independently. It never
turns ordinary tabs into managed tabs.

## Commands

| Command | What it does |
|---|---|
| `open N [--dry-run] [--url URL]` | Open a managed tab (default `https://www.google.com/`) in slot N's container, or print the marker-launch argv |
| `list` | Show slot -> container name for the detected Brave |
| `doctor` | Session type, detected Brave (pid/channel/user-data-dir), Wayland-native check, container list, keybinding conflicts |
| `install [--dry-run]` | Bind `Ctrl+Shift+1-9` via GNOME's `custom-keybindings` gsettings |
| `uninstall [--dry-run]` | Remove only this script's bindings |

Accepted final targets use `http:`, `https:`, or `file:`, or are exactly
`about:blank`. Empty, malformed, privileged-scheme, and recursive marker URLs
are rejected before Brave is launched.

## Channel detection

No hardcoded binary. `open`/`list`/`doctor` detect the **running** Brave
browser process (via `ps`, filtering out helpers: crashpad handlers,
zygote/renderer/gpu `--type=` children, shell wrappers). If several
channels run at once: **Beta > Stable > Nightly > unknown**. If none is
running, the best-ranked *installed* binary
(`/usr/bin/brave-browser*`, `/usr/bin/brave-origin*`) is launched cold.

Focus-based targeting (pick whichever Brave window currently has
keyboard focus) is **not implemented** — Wayland gives ordinary
processes no way to query focused-window ownership without a
GNOME Shell extension. Out of scope; channel-rank fallback is used
instead.

## Known limits

- **Session-only ordering state**: managed-tab records live in
  `chrome.storage.session`. Browser restart, extension reload/update, or
  disabling the extension clears them. Already-navigated tabs then become
  ordinary tabs; only newly opened marker tabs are managed.
- **Tab groups**: existing Brave/Chromium tab groups are unsupported. The
  scheduler orders tabs without preserving group membership or boundaries.
- **Markers are not authenticated**: the nonce provides uniqueness, not
  authorization. Any page that deliberately navigates its own tab to a
  syntactically valid marker can enroll and reorder that tab. This is the
  tradeoff for coordinating the launcher and extension without a native bridge.
- **Key conflicts**: `Ctrl+Shift+1-9` is grabbed globally once installed
  — any other app relying on those chords (some terminals' tab
  switching) stops receiving them while GNOME owns the binding. `doctor`
  reports keys already claimed by something else. To use a different
  chord, edit `SHORTCUT_KEYS` in `brave_container.py` (e.g.
  `<Control><Alt>{n}`) and re-run `install`.
- **Duplicate container names**: `--container=` matches by name; Brave
  does not enforce unique names. `doctor` warns if two containers share
  a name (the first match in the list wins).
- **Preferences read while Brave is writing**: Brave writes `Preferences`
  atomically, so a torn read is rare; `open`/`list`/`doctor` report it as
  "preferences unreadable" (exit 4) rather than crashing — just retry.
- **Flatpak**: best-effort only (`~/.var/app/com.brave.Browser/config/...`,
  channel not distinguished). The native DEB/RPM install is what's
  actually tested here; Brave itself recommends against Flatpak/Snap for
  this reason (weaker sandbox, policy quirks).

## Rollback

```bash
./brave_container.py uninstall            # removes only our bindings
```
Or delete this directory — nothing here ever writes to Brave's own
profile; `Preferences` is only ever read.

## Troubleshooting

If a shortcut leaves a `brave-container.invalid` error page open, the launcher
worked but the extension did not consume its marker. Check that `extension/`
is loaded and enabled at `brave://extensions`; use **Reload** there after
changing extension files. If Brave says “Manifest file is missing or
unreadable,” select the `extension/` directory itself, not the repository root.
Reloading or re-enabling the extension clears its session state, so reopen tabs
with the numbered shortcuts if they must be managed again.

If a `file:` target stays on the marker page or fails to open, enable **Allow
access to file URLs** in the extension's **Details**, then reopen it with the
numbered shortcut.

## If Brave ships native container shortcuts later

Check `brave://accelerators` / `brave://settings/system/shortcuts` for
`New Tab in Container 1..9`-style commands. If present, prefer those
(they persist across Brave restarts as `brave.accelerators` prefs and
don't depend on GNOME or this script): run `./brave_container.py
uninstall` first so the two don't both claim `Ctrl+Shift+N`.

## Tests

```bash
python3 -m unittest -v test_brave_container
node --test extension/scheduler.test.mjs
BRAVE_SHORTCUT_E2E=1 python3 -m unittest -v test_brave_container.EndToEndTest
```
The Python E2E test is opt-in and read/dry-run only — it never launches Brave.
It covers slots 1-9; unconfigured slots safely do nothing. The Node suite uses
the built-in test runner and a fake Chrome API; it installs no dependencies.

For a final live check, start Brave first, record its browser PID, then run
`./brave_container.py open 1`. Confirm the original PID is still the only
main Brave process and visually confirm a Google tab in the existing window
has the Personal-container badge; leave that tab open. Re-read GNOME's
custom bindings afterward: `custom0` must remain Ghostty and `custom1`-
`custom9` must remain slots 1-9.
