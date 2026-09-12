#!/usr/bin/env python3
"""Open a new Brave tab in a numbered container slot (Ctrl+Shift+1-9).

Linux / GNOME Wayland only. Stdlib only, no external deps.
See README.md for install, probe results, and known limits.
"""

import argparse
import ast
import glob
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
from urllib.parse import urlencode, urlsplit


# ---------------------------------------------------------------------------
# Pure core: no I/O, fully unit-tested in test_brave_container.py.
# ---------------------------------------------------------------------------

_HELPER_EXE_NAMES = {"bash", "sh", "chrome_crashpad_handler"}
_BRAVE_EXE_NAMES = {
    "brave", "brave-browser", "brave-browser-beta",
    "brave-browser-dev", "brave-browser-nightly", "brave-browser-stable",
    "brave-origin", "brave-origin-beta", "brave-origin-dev",
    "brave-origin-nightly", "brave-origin-stable",
}

_MEDIA_KEYS_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
_CUSTOM_KEYBINDING_SCHEMA = _MEDIA_KEYS_SCHEMA + ".custom-keybinding"
_CUSTOM_KEYBINDINGS_BASE = ("/org/gnome/settings-daemon/plugins/media-keys/"
                             "custom-keybindings")
_NAME_PREFIX = "brave-container-"

SHORTCUT_KEYS = {n: f"<Control><Shift>{n}" for n in range(1, 10)}


class NoBraveFoundError(Exception):
    """No Brave binary is running or installed anywhere we looked."""


def parse_ps(text):
    """Parse `ps -eo pid=,args=` (or `pgrep -af ...`) style text and return
    the real Brave browser processes: [{"pid", "exe", "argv"}], one per
    running instance. Drops wrapper/helper/child processes:
    - lines with --type= (renderer, zygote, gpu, utility, ...)
    - shell wrappers and crashpad handlers
    - anything whose exe name doesn't look like a brave binary
    """
    if not isinstance(text, str):
        return []
    processes = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = line.split(None, 1)
        if len(fields) != 2:
            continue
        pid_str, rest = fields
        if not re.fullmatch(r"[0-9]+", pid_str):
            continue
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if pid <= 0:
            continue
        argv = rest.split()
        if not argv:
            continue
        if "--type=" in rest:
            continue
        exe = argv[0]
        exe_name = os.path.basename(exe).lower()
        if exe_name in _HELPER_EXE_NAMES or exe_name not in _BRAVE_EXE_NAMES:
            continue
        processes.append({"pid": pid, "exe": exe, "argv": argv})
    return processes


def channel_of(exe):
    """Classify a Brave exe path into (channel_name, rank). Lower rank wins
    when several instances are running: beta > stable > nightly > unknown."""
    exe_lower = exe.lower()
    if "brave" not in exe_lower:
        return ("unknown", 3)
    if "-beta" in exe_lower:
        return ("beta", 0)
    if "-nightly" in exe_lower:
        return ("nightly", 2)
    return ("stable", 1)


def pick_target(running, installed):
    """Pick which Brave instance to send --container to. Prefers a running
    instance (beta > stable > nightly > unknown); falls back to launching
    the best-ranked installed binary cold. Raises NoBraveFoundError if
    nothing is running and nothing is installed."""
    if running:
        best = min(running, key=lambda p: channel_of(p["exe"])[1])
        return {**best, "running": True}
    if installed:
        best_exe = min(installed, key=lambda exe: channel_of(exe)[1])
        return {"pid": None, "exe": best_exe, "argv": [best_exe],
                "running": False}
    raise NoBraveFoundError("no Brave binary running or installed")


def _switch_value(argv, name):
    """Return the value of --name=value in argv, or None."""
    prefix = f"--{name}="
    for arg in argv:
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return None


def user_data_dir_for(exe, argv):
    """Where Brave keeps this instance's profiles. Honors an explicit
    --user-data-dir in the running command line; otherwise infers the
    channel dir name from the exe path (Chromium convention: each
    segment of the channel token title-cased, hyphens kept) and maps it
    under ~/.config/BraveSoftware/. Flatpak installs use a different
    prefix; that path is a documented best-effort guess (channel not
    distinguished — Flatpak Brave ships stable only in practice)."""
    explicit = _switch_value(argv, "user-data-dir")
    if explicit is not None:
        return explicit

    if "/app/" in exe:
        return os.path.expanduser(
            "~/.var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser")

    exe_name = os.path.basename(exe)
    if exe_name == "brave":
        channel_token = os.path.basename(os.path.dirname(exe))
    else:
        channel_token = exe_name
    dir_name = "-".join(part.capitalize() for part in channel_token.split("-"))
    return os.path.expanduser(f"~/.config/BraveSoftware/{dir_name}")


def profile_dir_for(argv):
    """Which profile within the user-data-dir (e.g. "Default",
    "Profile 2"). Honors an explicit --profile-directory; else "Default"."""
    return _switch_value(argv, "profile-directory") or "Default"


def _validate_slot(slot):
    if isinstance(slot, bool) or not isinstance(slot, int):
        raise TypeError("slot must be an integer")
    if not 1 <= slot <= 9:
        raise ValueError(f"slot must be 1-9, got {slot}")


def _slot_arg(value):
    try:
        slot = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("slot must be an integer") from None
    try:
        _validate_slot(slot)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None
    return slot


NEW_TAB_URL = "https://www.google.com/"
_MARKER_HOST = "brave-container.invalid"


def target_url(value):
    """Validate a final tab target accepted by the marker protocol."""
    if not isinstance(value, str):
        raise argparse.ArgumentTypeError("invalid target URL")
    if (not value or value != value.strip()
            or value.startswith("-")
            or any(c == "\\" or c.isspace() or ord(c) < 32 or ord(c) == 127
                   for c in value)
            or any(c in value for c in (";", "|", "$", "`", "<", ">"))
            or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", value,
                         re.IGNORECASE)):
        raise argparse.ArgumentTypeError("invalid target URL")
    parsed = None
    hostname = ""
    canonical_hostname = ""
    authority_valid = True
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # Force validation of malformed/non-numeric ports.
        valid = (
            (parsed.scheme in {"http", "https"} and bool(parsed.hostname))
            or (parsed.scheme == "file" and bool(parsed.path))
            or value == "about:blank"
        )
        hostname = parsed.hostname or ""
        try:
            canonical_hostname = hostname.encode("idna").decode("ascii").lower()
        except (UnicodeError, ValueError):
            authority_valid = False
        hostport = parsed.netloc.rsplit("@", 1)[-1]
        if (parsed.scheme in {"http", "https"} and hostport.startswith("[")):
            try:
                ipaddress.IPv6Address(hostname)
            except ipaddress.AddressValueError:
                authority_valid = False
        else:
            host_without_dot = canonical_hostname.rstrip(".")
            last_label = host_without_dot.rsplit(".", 1)[-1]
            if re.fullmatch(r"(?:[0-9]+|0[xX][0-9a-fA-F]+)", last_label):
                try:
                    authority_valid = (
                        str(ipaddress.IPv4Address(host_without_dot))
                        == canonical_hostname)
                except ipaddress.AddressValueError:
                    authority_valid = False
    except (ValueError, UnicodeError):
        valid = False

    if (not valid or not authority_valid or "%" in hostname
            or canonical_hostname.rstrip(".") == _MARKER_HOST):
        raise argparse.ArgumentTypeError(
            "URL must use http, https, file, or be about:blank")
    return value


def build_sort_marker(slot, target, nonce):
    """Build the extension marker URL for one shortcut-managed tab."""
    _validate_slot(slot)
    if (not isinstance(nonce, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", nonce)):
        raise ValueError("nonce must contain only ASCII letters, digits, _ or -")
    fragment = urlencode({
        "v": 1,
        "action": "open",
        "slot": slot,
        "target": target_url(target),
        "nonce": nonce,
    })
    return f"https://{_MARKER_HOST}/#{fragment}"


def build_argv(exe, passthrough, name, url=None):
    """Build the argv to hand to Popen. `passthrough` carries
    --user-data-dir / --profile-directory (see launch_passthrough). A URL
    is always included: without one, Chromium's forwarded-command-line path
    sets no HAS_CMD_LINE_TABS and opens a new *window*
    (startup_browser_creator_impl.cc DetermineBrowserOpenBehavior), and
    brave-core skips container attachment entirely for an empty tab list
    (brave_startup_tab_provider_impl.cc) -- confirmed live on this machine."""
    return [exe, *passthrough, f"--container={name}",
            NEW_TAB_URL if url is None else url]


def container_for_slot(prefs, n):
    """Return {"id", "name"} for the Nth container (1-indexed), or None
    if there is no Nth container. n must be in 1..9."""
    _validate_slot(n)
    try:
        containers = prefs["brave"]["containers"]["list"]
        if not isinstance(containers, list) or n > len(containers):
            return None
        entry = containers[n - 1]
        if not isinstance(entry, dict):
            return None
        container_id = entry.get("id")
        name = entry.get("name")
        if (not isinstance(container_id, str) or not container_id
                or not isinstance(name, str) or not name
                or "\x00" in container_id or "\x00" in name):
            return None
        return {"id": container_id, "name": name}
    except (AttributeError, IndexError, KeyError, TypeError):
        return None


def _custom_index(path):
    m = re.search(r"/custom(\d+)/$", path)
    return int(m.group(1)) if m else None


def gsettings_plan(existing, script_path, keys):
    """Compute the gsettings commands to bind `keys` (dict {slot: binding
    string}) to `script_path open <slot>`, given the `existing` list of
    custom-keybinding entries ({"path","name","binding","command"}) as
    currently read from gsettings.

    Never touches an entry whose name isn't "brave-container-N": foreign
    bindings (ghostty, etc.) keep their path and stay in the list
    untouched. Re-running with the same existing+keys reuses the same
    paths for our own entries (idempotent) instead of piling up new
    custom-keybinding indices.

    Returns {"paths": {slot: path}, "commands": [argv, ...]} ready to run
    with subprocess (or print, for --dry-run).
    """
    existing_by_name = {e["name"]: e for e in existing}
    used_indices = {idx for e in existing
                    if (idx := _custom_index(e["path"])) is not None}

    paths = {}
    next_index = 0
    for slot in sorted(keys):
        name = f"{_NAME_PREFIX}{slot}"
        if name in existing_by_name:
            paths[slot] = existing_by_name[name]["path"]
            continue
        while next_index in used_indices:
            next_index += 1
        used_indices.add(next_index)
        paths[slot] = f"{_CUSTOM_KEYBINDINGS_BASE}/custom{next_index}/"

    existing_paths = [e["path"] for e in existing]
    new_paths = [p for p in paths.values() if p not in existing_paths]
    full_list = existing_paths + new_paths

    commands = [["gsettings", "set", _MEDIA_KEYS_SCHEMA,
                 "custom-keybindings", str(full_list)]]
    for slot in sorted(keys):
        path = paths[slot]
        addr = f"{_CUSTOM_KEYBINDING_SCHEMA}:{path}"
        commands.append(["gsettings", "set", addr, "name",
                          f"{_NAME_PREFIX}{slot}"])
        commands.append(["gsettings", "set", addr, "binding", keys[slot]])
        commands.append(["gsettings", "set", addr, "command",
                          f"{script_path} open {slot}"])
    return {"paths": paths, "commands": commands}


def unbind_plan(existing):
    """Compute the gsettings commands to remove only our own
    "brave-container-N" entries, leaving every foreign binding (and its
    path/name/binding/command) exactly as it was."""
    kept = [e["path"] for e in existing
            if (isinstance(e, dict) and isinstance(e.get("path"), str)
                and not (isinstance(e.get("name"), str)
                         and re.fullmatch(r"brave-container-[1-9]",
                                          e["name"])))]
    return {"commands": [["gsettings", "set", _MEDIA_KEYS_SCHEMA,
                           "custom-keybindings", str(kept)]]}


# ---------------------------------------------------------------------------
# Thin I/O shell: talks to processes, files, and gsettings. Exercised by
# the Verification steps in README.md / the plan, plus the opt-in
# end-to-end test in test_brave_container.py.
# ---------------------------------------------------------------------------

_BINARY_GLOBS = ("/usr/bin/brave-browser*", "/usr/bin/brave-origin*")


class PreferencesUnreadableError(Exception):
    """Brave's Preferences file is missing, unreadable, or not valid JSON
    (e.g. caught mid atomic-write)."""


def _list_installed_binaries():
    found = []
    seen_real = set()
    for pattern in _BINARY_GLOBS:
        for path in glob.glob(pattern):
            if not os.access(path, os.X_OK):
                continue
            real = os.path.realpath(path)
            if real in seen_real:
                continue
            seen_real.add(real)
            found.append(path)
    return found


def _gather_running():
    ps = subprocess.run(["ps", "-eo", "pid=,args="],
                         capture_output=True, text=True, check=True)
    return parse_ps(ps.stdout)


def resolve_target():
    """Pick the Brave instance to talk to: a running one (beta > stable >
    nightly), or the best installed binary to launch cold. Raises
    NoBraveFoundError if Brave isn't running or installed at all."""
    return pick_target(_gather_running(), _list_installed_binaries())


def launch_passthrough(exe, argv):
    """Args to force onto the launched command so it targets the exact
    same profile as the detected instance. Always includes
    --user-data-dir: Brave's channel wrappers select the profile dir via a
    CHROME_VERSION_EXTRA env var (e.g. /usr/bin/brave-origin-beta sets
    CHROME_VERSION_EXTRA=beta), not a command-line switch, and a freshly
    Popen'd child does not inherit it -- launching the exe path directly
    with no --user-data-dir falls back to Chromium's *stable* default
    profile, a disconnected new instance (confirmed live on this
    machine). --profile-directory is added only when not "Default"."""
    passthrough = [f"--user-data-dir={user_data_dir_for(exe, argv)}"]
    profile = profile_dir_for(argv)
    if profile != "Default":
        passthrough.append(f"--profile-directory={profile}")
    return passthrough


def load_prefs(target):
    user_data_dir = user_data_dir_for(target["exe"], target["argv"])
    profile = profile_dir_for(target["argv"])
    prefs_path = os.path.join(user_data_dir, profile, "Preferences")
    try:
        with open(prefs_path, encoding="utf-8") as f:
            return json.load(f), prefs_path
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PreferencesUnreadableError(f"{prefs_path}: {e}") from e


def _wayland_native(pid):
    """Best-effort check that `pid` holds an open Wayland socket fd (i.e.
    it is not falling back to XWayland). Returns None if we can't tell
    (e.g. no permission to read /proc/<pid>/fd)."""
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return None
    for fd in fds:
        try:
            link = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        if "wayland-" in link:
            return True
    return False


def _duplicate_container_names(prefs):
    try:
        containers = prefs["brave"]["containers"]["list"]
    except (AttributeError, KeyError, TypeError):
        return []
    if not isinstance(containers, list):
        return []
    names = [c["name"] for c in containers
             if isinstance(c, dict) and isinstance(c.get("name"), str)]
    return sorted({n for n in names if names.count(n) > 1})


def _gsettings_get(schema, key):
    out = subprocess.run(["gsettings", "get", schema, key],
                          capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _existing_custom_keybindings():
    """Read the current custom-keybindings list and each entry's
    name/binding/command straight from gsettings (the live source of
    truth), as the {"path","name","binding","command"} shape
    gsettings_plan/unbind_plan expect."""
    raw = _gsettings_get(_MEDIA_KEYS_SCHEMA, "custom-keybindings").strip()
    if raw.startswith("@as"):
        raw = raw[3:].strip()
    try:
        paths = ast.literal_eval(raw)
    except (SyntaxError, ValueError) as e:
        raise ValueError("invalid gsettings custom-keybindings value") from e
    if (not isinstance(paths, list)
            or not all(isinstance(path, str) for path in paths)):
        raise ValueError("invalid gsettings custom-keybindings value")
    paths = [path for path in paths if path]
    existing = []
    for path in paths:
        addr = f"{_CUSTOM_KEYBINDING_SCHEMA}:{path}"
        values = []
        for key in ("name", "binding", "command"):
            raw_value = _gsettings_get(addr, key).strip()
            try:
                value = ast.literal_eval(raw_value)
            except (SyntaxError, ValueError) as e:
                raise ValueError(f"invalid gsettings {key} value") from e
            if not isinstance(value, str):
                raise ValueError(f"invalid gsettings {key} value")
            values.append(value)
        name, binding, command = values
        existing.append({"path": path, "name": name, "binding": binding,
                          "command": command})
    return existing


def _run_commands(commands, dry_run):
    for cmd in commands:
        if dry_run:
            print(" ".join(cmd))
        else:
            subprocess.run(cmd, check=True)


def cmd_open(slot, dry_run, url=None):
    target = resolve_target()
    prefs, _ = load_prefs(target)
    container = container_for_slot(prefs, slot)
    if container is None:
        print(f"slot {slot}: no container configured, nothing to do",
              file=sys.stderr)
        return 0
    passthrough = launch_passthrough(target["exe"], target["argv"])
    final_target = NEW_TAB_URL if url is None else url
    marker = build_sort_marker(
        slot, final_target, secrets.token_urlsafe(12))
    argv = build_argv(
        target["exe"], passthrough, container["name"], url=marker)
    if dry_run:
        print(" ".join(argv))
        return 0
    subprocess.Popen(argv, start_new_session=True,
                      stdin=subprocess.DEVNULL,
                      stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL)
    return 0


def cmd_list():
    target = resolve_target()
    prefs, _ = load_prefs(target)
    for slot in range(1, 10):
        container = container_for_slot(prefs, slot)
        label = container["name"] if container else "(empty)"
        print(f"{slot}={label}")
    return 0


def cmd_doctor():
    print(f"session type: {os.environ.get('XDG_SESSION_TYPE', '?')}")
    try:
        target = resolve_target()
    except NoBraveFoundError as e:
        print(f"brave: {e}")
        return 3
    channel, _ = channel_of(target["exe"])
    print(f"brave: pid={target.get('pid')} exe={target['exe']} "
          f"channel={channel} running={target['running']}")
    if target["running"] and target.get("pid"):
        native = _wayland_native(target["pid"])
        print(f"wayland-native: {native}")
    try:
        prefs, prefs_path = load_prefs(target)
    except PreferencesUnreadableError as e:
        print(f"preferences: {e}")
        return 4
    print(f"preferences: {prefs_path}")
    for slot in range(1, 10):
        container = container_for_slot(prefs, slot)
        print(f"  slot {slot}: {container['name'] if container else '(empty)'}")
    dupes = _duplicate_container_names(prefs)
    if dupes:
        print(f"warning: duplicate container names (first match wins): "
              f"{', '.join(dupes)}")
    try:
        existing = _existing_custom_keybindings()
    except (OSError, ValueError, subprocess.CalledProcessError):
        existing = []
    claimed = {e["binding"]: e["name"] for e in existing}
    for slot, binding in SHORTCUT_KEYS.items():
        holder = claimed.get(binding)
        if holder and holder != f"{_NAME_PREFIX}{slot}":
            print(f"warning: {binding} already bound to {holder!r}")
    return 0


def cmd_install(dry_run):
    script_path = os.path.abspath(__file__)
    existing = _existing_custom_keybindings()
    plan = gsettings_plan(existing, script_path, SHORTCUT_KEYS)
    _run_commands(plan["commands"], dry_run)
    return 0


def cmd_uninstall(dry_run):
    existing = _existing_custom_keybindings()
    plan = unbind_plan(existing)
    _run_commands(plan["commands"], dry_run)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="brave_container.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_open = sub.add_parser("open", help="open a new tab in slot N")
    p_open.add_argument("slot", type=_slot_arg)
    p_open.add_argument("--dry-run", action="store_true")
    p_open.add_argument("--url", type=target_url, default=None,
                         help="override the new-tab URL (default: %s)"
                         % NEW_TAB_URL)

    sub.add_parser("list", help="show slot -> container mapping")
    sub.add_parser("doctor", help="diagnose session/brave/keybinding state")

    p_install = sub.add_parser("install", help="bind Ctrl+Shift+1-9")
    p_install.add_argument("--dry-run", action="store_true")

    p_uninstall = sub.add_parser("uninstall", help="remove our bindings")
    p_uninstall.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "open":
            return cmd_open(args.slot, args.dry_run, url=args.url)
        if args.cmd == "list":
            return cmd_list()
        if args.cmd == "doctor":
            return cmd_doctor()
        if args.cmd == "install":
            return cmd_install(args.dry_run)
        if args.cmd == "uninstall":
            return cmd_uninstall(args.dry_run)
    except NoBraveFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    except PreferencesUnreadableError as e:
        print(f"error: {e}", file=sys.stderr)
        return 4
    except (OSError, subprocess.CalledProcessError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 5
    return 1


if __name__ == "__main__":
    sys.exit(main())
