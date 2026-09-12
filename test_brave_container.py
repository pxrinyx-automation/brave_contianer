"""Tests for brave_container.py — stdlib unittest, no external deps."""
import contextlib
import io
import os
import unittest

import brave_container as bc


class ContainerForSlotTest(unittest.TestCase):
    def setUp(self):
        self.prefs = {
            "brave": {
                "containers": {
                    "list": [
                        {"id": "id-1", "name": "Personal"},
                        {"id": "id-2", "name": "university"},
                        {"id": "id-3", "name": "bank"},
                        {"id": "id-4", "name": "dev1"},
                        {"id": "id-5", "name": "dev2"},
                        {"id": "id-6", "name": "dev3"},
                    ]
                }
            }
        }

    def test_slot_1_is_first_container(self):
        self.assertEqual(bc.container_for_slot(self.prefs, 1),
                          {"id": "id-1", "name": "Personal"})

    def test_slot_4_is_fourth_container(self):
        self.assertEqual(bc.container_for_slot(self.prefs, 4),
                          {"id": "id-4", "name": "dev1"})

    def test_all_configured_slots_resolve_in_order(self):
        self.assertEqual(
            [bc.container_for_slot(self.prefs, n)["name"] for n in range(1, 7)],
            ["Personal", "university", "bank", "dev1", "dev2", "dev3"])

    def test_slot_past_end_returns_none(self):
        self.assertIsNone(bc.container_for_slot(self.prefs, 7))

    def test_empty_list_returns_none(self):
        empty = {"brave": {"containers": {"list": []}}}
        self.assertIsNone(bc.container_for_slot(empty, 1))

    def test_slot_zero_rejected(self):
        with self.assertRaises(ValueError):
            bc.container_for_slot(self.prefs, 0)

    def test_slot_ten_rejected(self):
        with self.assertRaises(ValueError):
            bc.container_for_slot(self.prefs, 10)


class ParsePsTest(unittest.TestCase):
    # Shape captured from `pgrep -af brave` on this machine: a bash-exec'd
    # wrapper, the real "brave" binary, two crashpad handlers, and a
    # --type=zygote child of the same binary. Only the real binary line
    # (no --type=, not a helper) is a browser process.
    PS_TEXT = "\n".join([
        "159799 /bin/bash /usr/bin/brave-origin-beta",
        "159806 /opt/brave.com/brave-origin-beta/brave",
        "159809 /opt/brave.com/brave-origin-beta/chrome_crashpad_handler "
        "--monitor-self --database=/home/dev/.config/BraveSoftware/Crash Reports",
        "159811 /opt/brave.com/brave-origin-beta/chrome_crashpad_handler "
        "--no-periodic-tasks --database=/home/dev/.config/BraveSoftware/Crash Reports",
        "159819 /opt/brave.com/brave-origin-beta/brave --type=zygote "
        "--no-zygote-sandbox --crashpad-handler-pid=159809",
    ])

    def test_only_real_browser_process_survives(self):
        result = bc.parse_ps(self.PS_TEXT)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["pid"], 159806)
        self.assertEqual(result[0]["exe"],
                          "/opt/brave.com/brave-origin-beta/brave")

    def test_argv_captured_for_survivor(self):
        result = bc.parse_ps(self.PS_TEXT)
        self.assertEqual(result[0]["argv"],
                          ["/opt/brave.com/brave-origin-beta/brave"])

    def test_no_brave_processes_returns_empty(self):
        text = "1234 /usr/bin/firefox\n5678 /bin/bash"
        self.assertEqual(bc.parse_ps(text), [])

    def test_blank_lines_ignored(self):
        text = "\n" + self.PS_TEXT + "\n\n"
        self.assertEqual(len(bc.parse_ps(text)), 1)


class ChannelOfTest(unittest.TestCase):
    def test_beta_directory_ranks_0(self):
        self.assertEqual(
            bc.channel_of("/opt/brave.com/brave-origin-beta/brave"),
            ("beta", 0))

    def test_nightly_directory_ranks_2(self):
        self.assertEqual(
            bc.channel_of("/opt/brave.com/brave-origin-nightly/brave"),
            ("nightly", 2))

    def test_plain_brave_ranks_1_stable(self):
        self.assertEqual(
            bc.channel_of("/opt/brave.com/brave-origin/brave"),
            ("stable", 1))

    def test_official_apt_stable_path(self):
        self.assertEqual(
            bc.channel_of("/opt/brave.com/brave-browser/brave"),
            ("stable", 1))

    def test_official_apt_beta_path(self):
        self.assertEqual(
            bc.channel_of("/opt/brave.com/brave-browser-beta/brave"),
            ("beta", 0))

    def test_unrelated_binary_ranks_3_unknown(self):
        self.assertEqual(
            bc.channel_of("/usr/bin/firefox"),
            ("unknown", 3))


class PickTargetTest(unittest.TestCase):
    def _proc(self, pid, exe):
        return {"pid": pid, "exe": exe, "argv": [exe]}

    def test_beta_wins_over_stable_when_both_running(self):
        running = [
            self._proc(1, "/opt/brave.com/brave-origin/brave"),
            self._proc(2, "/opt/brave.com/brave-origin-beta/brave"),
        ]
        target = bc.pick_target(running, installed=[])
        self.assertEqual(target["pid"], 2)

    def test_nightly_only_running_is_picked(self):
        running = [self._proc(3, "/opt/brave.com/brave-origin-nightly/brave")]
        target = bc.pick_target(running, installed=[])
        self.assertEqual(target["pid"], 3)

    def test_none_running_falls_back_to_best_installed(self):
        installed = [
            "/opt/brave.com/brave-origin/brave-origin",
            "/opt/brave.com/brave-origin-beta/brave-origin-beta",
        ]
        target = bc.pick_target([], installed=installed)
        self.assertEqual(
            target["exe"],
            "/opt/brave.com/brave-origin-beta/brave-origin-beta")
        self.assertFalse(target["running"])

    def test_nothing_installed_raises(self):
        with self.assertRaises(bc.NoBraveFoundError):
            bc.pick_target([], installed=[])


class UserDataDirForTest(unittest.TestCase):
    def test_explicit_user_data_dir_wins(self):
        argv = ["/opt/brave.com/brave-origin-beta/brave",
                "--user-data-dir=/custom/path"]
        self.assertEqual(bc.user_data_dir_for("/opt/brave.com/"
                                               "brave-origin-beta/brave",
                                               argv),
                          "/custom/path")

    def test_real_binary_path_infers_from_parent_dir(self):
        exe = "/opt/brave.com/brave-origin-beta/brave"
        expected = os.path.expanduser(
            "~/.config/BraveSoftware/Brave-Origin-Beta")
        self.assertEqual(bc.user_data_dir_for(exe, [exe]), expected)

    def test_wrapper_path_infers_from_own_basename(self):
        exe = "/opt/brave.com/brave-browser/brave-browser"
        expected = os.path.expanduser("~/.config/BraveSoftware/Brave-Browser")
        self.assertEqual(bc.user_data_dir_for(exe, [exe]), expected)

    def test_nightly_channel(self):
        exe = "/opt/brave.com/brave-origin-nightly/brave"
        expected = os.path.expanduser(
            "~/.config/BraveSoftware/Brave-Origin-Nightly")
        self.assertEqual(bc.user_data_dir_for(exe, [exe]), expected)

    def test_flatpak_path_uses_var_app_location(self):
        exe = "/app/bin/brave"
        expected = os.path.expanduser(
            "~/.var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser")
        self.assertEqual(bc.user_data_dir_for(exe, [exe]), expected)


class ProfileDirForTest(unittest.TestCase):
    def test_explicit_profile_directory_honored(self):
        argv = ["/opt/brave.com/brave-origin-beta/brave",
                "--profile-directory=Profile 2"]
        self.assertEqual(bc.profile_dir_for(argv), "Profile 2")

    def test_defaults_to_default(self):
        argv = ["/opt/brave.com/brave-origin-beta/brave"]
        self.assertEqual(bc.profile_dir_for(argv), "Default")


class BuildArgvTest(unittest.TestCase):
    # Step 0 probe re-run (2026-09-12, corrected): a bare --container=<name>
    # with no URL sets no HAS_CMD_LINE_TABS, so Chromium's forwarded-command
    # -line path (startup_browser_creator_impl.cc DetermineBrowserOpenBehavior)
    # falls through to BrowserOpenBehavior::NEW -- a new *window* -- and
    # brave-core's container-tab attachment is skipped entirely for an empty
    # tab list (brave_startup_tab_provider_impl.cc). A URL is required. The
    # command-line URL allowlist (chrome/browser/ui/startup/url_util.cc
    # ValidateLaunchUrlWebUnsafe) rejects brave://newtab; about:blank is
    # allowed and was confirmed live to land as a container tab in the
    # existing window. A web URL is also allowed and makes the tab useful
    # immediately.
    EXE = "/opt/brave.com/brave-origin-beta/brave"

    def test_new_tab_url_is_google(self):
        self.assertEqual(bc.NEW_TAB_URL, "https://www.google.com/")

    def test_container_switch_always_gets_a_url(self):
        self.assertEqual(bc.build_argv(self.EXE, [], "dev1"),
                          [self.EXE, "--container=dev1", bc.NEW_TAB_URL])

    def test_passthrough_comes_before_container_switch(self):
        result = bc.build_argv(self.EXE, ["--user-data-dir=/x"], "dev1")
        self.assertEqual(
            result,
            [self.EXE, "--user-data-dir=/x", "--container=dev1",
             bc.NEW_TAB_URL])

    def test_explicit_url_overrides_default(self):
        result = bc.build_argv(self.EXE, [], "dev1", url="https://example.com")
        self.assertEqual(result,
                          [self.EXE, "--container=dev1", "https://example.com"])

class LaunchPassthroughTest(unittest.TestCase):
    # A running instance's argv rarely names --user-data-dir explicitly --
    # Brave's channel wrappers select it via a CHROME_VERSION_EXTRA env var
    # (confirmed: /usr/bin/brave-origin-beta sets CHROME_VERSION_EXTRA=beta),
    # which a freshly Popen'd child does not inherit. Launching the exe
    # directly with no --user-data-dir therefore falls back to Chromium's
    # *stable* default profile -- a disconnected new instance/window, not the
    # running one. So launch_passthrough always forces the value, using the
    # same resolution load_prefs already relies on.
    EXE = "/opt/brave.com/brave-origin-beta/brave"

    def test_forces_user_data_dir_even_when_not_explicit(self):
        result = bc.launch_passthrough(self.EXE, [self.EXE])
        self.assertIn(
            f"--user-data-dir={bc.user_data_dir_for(self.EXE, [self.EXE])}",
            result)

    def test_omits_profile_directory_when_default(self):
        result = bc.launch_passthrough(self.EXE, [self.EXE])
        self.assertEqual(len(result), 1)

    def test_includes_profile_directory_when_not_default(self):
        argv = [self.EXE, "--profile-directory=Profile 2"]
        result = bc.launch_passthrough(self.EXE, argv)
        self.assertEqual(result[-1], "--profile-directory=Profile 2")

    def test_honors_explicit_user_data_dir_in_running_argv(self):
        argv = [self.EXE, "--user-data-dir=/custom/path"]
        result = bc.launch_passthrough(self.EXE, argv)
        self.assertIn("--user-data-dir=/custom/path", result)

    def test_both_forced_together_user_data_dir_then_profile(self):
        argv = [self.EXE, "--user-data-dir=/custom/path",
                "--profile-directory=Profile 2"]
        result = bc.launch_passthrough(self.EXE, argv)
        self.assertEqual(
            result,
            ["--user-data-dir=/custom/path", "--profile-directory=Profile 2"])


class GsettingsPlanTest(unittest.TestCase):
    BASE = ("/org/gnome/settings-daemon/plugins/media-keys/"
            "custom-keybindings")
    SCRIPT = "/home/dev/personal_proj/brave_shortcut/brave_container.py"

    def _ghostty(self):
        return {"path": f"{self.BASE}/custom0/", "name": "ghostty",
                 "binding": "<Control><Alt>t", "command": "ghostty"}

    def _keys_1_to_9(self):
        return {n: f"<Control><Shift>{n}" for n in range(1, 10)}

    def test_allocates_custom1_through_9_around_ghostty(self):
        plan = bc.gsettings_plan([self._ghostty()], self.SCRIPT,
                                  self._keys_1_to_9())
        expected_paths = {n: f"{self.BASE}/custom{n}/" for n in range(1, 10)}
        self.assertEqual(plan["paths"], expected_paths)

    def test_never_touches_foreign_custom0(self):
        plan = bc.gsettings_plan([self._ghostty()], self.SCRIPT,
                                  self._keys_1_to_9())
        addressed = {cmd[2] for cmd in plan["commands"] if cmd[1] == "set"
                     and "custom-keybinding:" in cmd[2]}
        self.assertFalse(any("custom0/" in a for a in addressed))

    def test_full_list_value_includes_ghostty_and_ours(self):
        plan = bc.gsettings_plan([self._ghostty()], self.SCRIPT,
                                  self._keys_1_to_9())
        list_cmd = next(c for c in plan["commands"]
                         if c[3] == "custom-keybindings")
        self.assertIn(f"{self.BASE}/custom0/", list_cmd[4])
        self.assertIn(f"{self.BASE}/custom9/", list_cmd[4])

    def test_second_run_reuses_same_paths_no_new_allocation(self):
        first = bc.gsettings_plan([self._ghostty()], self.SCRIPT,
                                   self._keys_1_to_9())
        already_installed = [self._ghostty()] + [
            {"path": path, "name": f"brave-container-{slot}",
             "binding": self._keys_1_to_9()[slot],
             "command": f"{self.SCRIPT} open {slot}"}
            for slot, path in first["paths"].items()
        ]
        second = bc.gsettings_plan(already_installed, self.SCRIPT,
                                    self._keys_1_to_9())
        self.assertEqual(first["paths"], second["paths"])

    def test_foreign_custom5_is_skipped_over(self):
        foreign = {"path": f"{self.BASE}/custom5/", "name": "something-else",
                   "binding": "<Super>x", "command": "whatever"}
        plan = bc.gsettings_plan([self._ghostty(), foreign], self.SCRIPT,
                                  self._keys_1_to_9())
        # indices 0 and 5 are taken; our 9 slots take 1,2,3,4,6,7,8,9,10
        self.assertEqual(plan["paths"][4], f"{self.BASE}/custom4/")
        self.assertEqual(plan["paths"][5], f"{self.BASE}/custom6/")
        self.assertEqual(plan["paths"][9], f"{self.BASE}/custom10/")
        self.assertNotIn(f"{self.BASE}/custom5/", plan["paths"].values())


class UnbindPlanTest(unittest.TestCase):
    BASE = ("/org/gnome/settings-daemon/plugins/media-keys/"
            "custom-keybindings")

    def test_removes_only_brave_container_entries(self):
        existing = [
            {"path": f"{self.BASE}/custom0/", "name": "ghostty",
             "binding": "<Control><Alt>t", "command": "ghostty"},
            {"path": f"{self.BASE}/custom1/", "name": "brave-container-1",
             "binding": "<Control><Shift>1",
             "command": "/x/brave_container.py open 1"},
        ]
        plan = bc.unbind_plan(existing)
        list_cmd = next(c for c in plan["commands"]
                         if c[3] == "custom-keybindings")
        self.assertIn(f"{self.BASE}/custom0/", list_cmd[4])
        self.assertNotIn(f"{self.BASE}/custom1/", list_cmd[4])

    def test_no_brave_entries_is_a_noop_list(self):
        existing = [{"path": f"{self.BASE}/custom0/", "name": "ghostty",
                      "binding": "<Control><Alt>t", "command": "ghostty"}]
        plan = bc.unbind_plan(existing)
        list_cmd = next(c for c in plan["commands"]
                         if c[3] == "custom-keybindings")
        self.assertIn(f"{self.BASE}/custom0/", list_cmd[4])


class MainOpenArgWiringTest(unittest.TestCase):
    """argparse -> cmd_open wiring for `open`, isolated from real Brave I/O
    by stubbing cmd_open itself (cmd_open's own body is real-I/O and
    covered by EndToEndTest)."""

    def setUp(self):
        self.calls = []
        self._orig = bc.cmd_open
        bc.cmd_open = lambda slot, dry_run, url=None: (
            self.calls.append((slot, dry_run, url)), 0)[1]
        self.addCleanup(setattr, bc, "cmd_open", self._orig)

    def test_url_flag_is_parsed_and_forwarded(self):
        bc.main(["open", "4", "--url", "https://example.com"])
        self.assertEqual(self.calls, [(4, False, "https://example.com")])

    def test_url_defaults_to_none_when_omitted(self):
        bc.main(["open", "4"])
        self.assertEqual(self.calls, [(4, False, None)])

    def test_empty_url_is_rejected(self):
        with self.assertRaises(SystemExit) as caught:
            bc.main(["open", "4", "--url", ""])
        self.assertEqual(caught.exception.code, 2)


@unittest.skipUnless(os.environ.get("BRAVE_SHORTCUT_E2E") == "1",
                      "set BRAVE_SHORTCUT_E2E=1 to run against real Brave")
class EndToEndTest(unittest.TestCase):
    """Opt-in: exercises the real I/O shell against whatever Brave is
    actually running/installed on this machine. Never launches Brave —
    --dry-run only."""

    def test_every_slot_dry_run_is_safe(self):
        target = bc.resolve_target()
        prefs, _ = bc.load_prefs(target)
        for slot in range(1, 10):
            container = bc.container_for_slot(prefs, slot)
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(bc.cmd_open(slot, dry_run=True), 0)
            if container is None:
                self.assertIn("no container configured", stderr.getvalue())
                continue
            argv = stdout.getvalue().strip().split()
            self.assertEqual(argv[0], target["exe"])
            self.assertTrue(any(arg.startswith("--user-data-dir=") for arg in argv))
            self.assertEqual(argv[-2], f"--container={container['name']}")
            self.assertEqual(argv[-1], bc.NEW_TAB_URL)


if __name__ == "__main__":
    unittest.main()
