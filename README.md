# brave_shortcut

Open a new Brave tab in container slot N with `Ctrl+Shift+N` (N = 1-9),
on Linux with GNOME + native Wayland. All Brave channels (Stable, Beta,
Nightly, Origin variants) — no hardcoded binary path.

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
browser patching.

```
Ctrl+Shift+N  ->  GNOME custom keybinding  ->  brave_container.py open N
                                                 -> detect running/installed Brave
                                                 -> read Preferences, find Nth container
                                                 -> brave --container=<name>
```

Slot N = the **Nth entry** in Brave's own container list
(`brave://settings/braveContent`), read live every time — reorder
containers there and the keys follow, matching brave-core#38435's
"live ordinal lookup" design intent (avoids the stale-mapping bug
Firefox Multi-Account Containers had when containers were reordered).

## Step 0 probe (recorded 2026-09-12)

Confirmed against this machine's running Brave Origin Beta:
bare `--container=<name>` (no URL argument) opens a new tab page in the
existing window — renderer process count went 12→13 on
`brave-origin-beta --container=dev2`. So `build_argv()` never needs a URL.

## Install

```bash
python3 -m unittest test_brave_container   # run the test suite first
./brave_container.py doctor                # sanity-check your system
./brave_container.py install --dry-run     # review the gsettings commands
./brave_container.py install               # apply
```

Press `Ctrl+Shift+1` .. `Ctrl+Shift+9`.

## Commands

| Command | What it does |
|---|---|
| `open N [--dry-run]` | Open a new tab in slot N's container (or print the argv) |
| `list` | Show slot -> container name for the detected Brave |
| `doctor` | Session type, detected Brave (pid/channel/user-data-dir), Wayland-native check, container list, keybinding conflicts |
| `install [--dry-run]` | Bind `Ctrl+Shift+1-9` via GNOME's `custom-keybindings` gsettings |
| `uninstall [--dry-run]` | Remove only this script's bindings |

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

## If Brave ships native container shortcuts later

Check `brave://accelerators` / `brave://settings/system/shortcuts` for
`New Tab in Container 1..9`-style commands. If present, prefer those
(they persist across Brave restarts as `brave.accelerators` prefs and
don't depend on GNOME or this script): run `./brave_container.py
uninstall` first so the two don't both claim `Ctrl+Shift+N`.

## Tests

```bash
python3 -m unittest -v test_brave_container            # pure-function unit tests
BRAVE_SHORTCUT_E2E=1 python3 -m unittest -v test_brave_container.EndToEndTest
```
The E2E test is opt-in and read/dry-run only — it never launches Brave.
