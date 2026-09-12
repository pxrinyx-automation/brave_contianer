"""Tests for brave_container.py — stdlib unittest, no external deps."""
import argparse
import ast
import contextlib
import io
import json
import os
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

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

    def test_slot_boundaries_and_types(self):
        self.assertEqual(bc.container_for_slot(self.prefs, 9), None)
        for slot in ("1", 1.0, True, None):
            with self.subTest(slot=slot):
                with self.assertRaises(TypeError):
                    bc.container_for_slot(self.prefs, slot)

    def test_schema_and_record_corruption_is_unavailable(self):
        malformed = (
            None, {}, {"brave": None}, {"brave": {"containers": {}}},
            {"brave": {"containers": {"list": None}}},
            {"brave": {"containers": {"list": "not-a-list"}}},
            {"brave": {"containers": {"list": [None]}}},
            {"brave": {"containers": {"list": [{}]}}},
            {"brave": {"containers": {"list": [{"id": 1, "name": "x"}]}}},
            {"brave": {"containers": {"list": [{"id": "x", "name": 1}]}}},
            {"brave": {"containers": {"list": [{"id": "", "name": "x"}]}}},
            {"brave": {"containers": {"list": [{"id": "x", "name": ""}]}}},
            {"brave": {"containers": {"list": [
                {"id": "x\x00", "name": "x"}]} }},
        )
        for prefs in malformed:
            with self.subTest(prefs=prefs):
                self.assertIsNone(bc.container_for_slot(prefs, 1))

    def test_names_are_returned_without_shell_interpretation(self):
        prefs = {"brave": {"containers": {"list": [
            {"id": "id", "name": "名字;$(touch /tmp/pwned)"},
        ]}}}
        self.assertEqual(bc.container_for_slot(prefs, 1),
                         {"id": "id", "name": "名字;$(touch /tmp/pwned)"})


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

    def test_garbage_pid_rows_non_positive_and_fake_executables_are_ignored(self):
        text = "\n".join([
            "garbage /usr/bin/brave",
            "0 /usr/bin/brave",
            "-1 /usr/bin/brave",
            "12",
            "13\t/usr/bin/brave-browser-beta --flag",
            "14 /opt/brave/brave-not-real",
            "15 /opt/brave/brave-browser-nightly --flag",
            "16 /opt/brave/brave-origin-stable --flag",
            "17 /opt/例え/brave --flag",
            "18 /opt/brave/chrome_crashpad_handler",
            "19 /opt/brave/brave --type=renderer",
        ])
        result = bc.parse_ps(text)
        self.assertEqual([item["pid"] for item in result], [13, 15, 16, 17])

    def test_non_string_process_output_is_fail_closed(self):
        self.assertEqual(bc.parse_ps(None), [])


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


class SortMarkerTest(unittest.TestCase):
    def test_fields_and_target_are_encoded_in_fragment(self):
        marker = bc.build_sort_marker(
            4, "https://example.com/a%20path?q=x&y=two#end", "nonce_123")
        parsed = urlsplit(marker)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path),
                         ("https", "brave-container.invalid", "/"))
        self.assertEqual(parse_qs(parsed.fragment), {
            "v": ["1"],
            "action": ["open"],
            "slot": ["4"],
            "target": ["https://example.com/a%20path?q=x&y=two#end"],
            "nonce": ["nonce_123"],
        })

    def test_nonce_and_slot_validation(self):
        for nonce in ("", "a b", "a/b", "a;b", "é", None, 1):
            with self.subTest(nonce=nonce):
                with self.assertRaises(ValueError):
                    bc.build_sort_marker(1, "https://example.com/", nonce)
        for slot in (0, 10):
            with self.assertRaises(ValueError):
                bc.build_sort_marker(slot, "https://example.com/", "n")


class TargetUrlValidationTest(unittest.TestCase):
    def test_allowed_targets_are_returned_unchanged(self):
        for target in (
                "http://example.com/path", "https://example.com/",
                "https://192.168.1.1/", "file:///tmp/report.html",
                "https://user:pass@example.com:8443/path",
                "https://例え.テスト/",
                "https://[2001:db8::1]/",
                "http://127.0.0.1:8080/a?x=1&y=2#part",
                "https://[::1]:8443/#fragment",
                "http://localhost/",
                "about:blank"):
            with self.subTest(target=target):
                self.assertEqual(bc.target_url(target), target)

    def test_invalid_or_privileged_targets_are_rejected(self):
        for target in (
                "", "example.com", "https://", "https://example.com:bad",
                "file:", "about:settings",
                "brave://settings", "chrome://settings",
                "javascript:alert(1)",
                "https://brave-container.invalid\\@example.com/",
                "https://brave-container.invalid./",
                "https://brave-container%2Einvalid/",
                "https://%62rave-container.invalid/",
                "https://example%2F.com/",
                "https://999.999.999.999/",
                "https://4294967296/",
                "https://0x100000000/",
                "https://0300.0250.0001.0001/",
                "https://127.1/",
                "https://[v1.fe]/",
                "https://[example.com]/",
                "https://brave-container\u3002invalid/",
                "https://brave-container\uff0einvalid/",
                "https://brave-container\uff61invalid/",
                "\x00https://example.com/",
                "https://exa\x00mple.com/",
                "https://brave-container.invalid/#v=1&action=open"):
            with self.subTest(target=target):
                with self.assertRaises(argparse.ArgumentTypeError):
                    bc.target_url(target)

    def test_non_strings_encoded_controls_and_shell_inputs_are_rejected(self):
        for target in (None, 1, b"https://example.com", "https://example.com/%00",
                       "https://example.com/$(id)", "https://example.com/a;b",
                       "https://example.com/a|b", "--no-sandbox"):
            with self.subTest(target=target):
                with self.assertRaises(argparse.ArgumentTypeError):
                    bc.target_url(target)


class CmdOpenTest(unittest.TestCase):
    TARGET = {
        "exe": "/opt/brave/brave",
        "argv": ["/opt/brave/brave"],
        "running": True,
        "pid": 12,
    }
    PREFS = {"brave": {"containers": {"list": [
        {"id": "id-1", "name": "Personal"},
    ]}}}

    def setUp(self):
        patches = (
            mock.patch.object(bc, "resolve_target", return_value=self.TARGET),
            mock.patch.object(bc, "load_prefs",
                              return_value=(self.PREFS, "/tmp/Preferences")),
            mock.patch.object(bc, "launch_passthrough",
                              return_value=["--user-data-dir=/profile"]),
            mock.patch.object(bc.secrets, "token_urlsafe",
                              return_value="fixed-nonce"),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_default_target_is_wrapped_in_marker_for_dry_run(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(bc.cmd_open(1, dry_run=True), 0)
        marker = stdout.getvalue().strip().split()[-1]
        fields = parse_qs(urlsplit(marker).fragment)
        self.assertEqual(fields["slot"], ["1"])
        self.assertEqual(fields["target"], [bc.NEW_TAB_URL])
        self.assertEqual(fields["nonce"], ["fixed-nonce"])

    def test_override_target_is_wrapped_in_marker(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            bc.cmd_open(1, dry_run=True, url="https://example.com/a?q=1&x=2")
        marker = stdout.getvalue().strip().split()[-1]
        self.assertEqual(parse_qs(urlsplit(marker).fragment)["target"],
                         ["https://example.com/a?q=1&x=2"])

    def test_unconfigured_slot_remains_a_no_op_without_nonce(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(bc.cmd_open(7, dry_run=True), 0)
        self.assertIn("no container configured", stderr.getvalue())
        bc.secrets.token_urlsafe.assert_not_called()

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

    def test_similarly_named_entries_are_foreign(self):
        existing = [
            {"path": f"{self.BASE}/custom1/", "name": "brave-container-10",
             "binding": "x", "command": "foreign"},
            {"path": f"{self.BASE}/custom2/", "name": "brave-container-x",
             "binding": "x", "command": "foreign"},
            {"path": f"{self.BASE}/custom3/", "name": "brave-container-1-extra",
             "binding": "x", "command": "foreign"},
            {"path": f"{self.BASE}/custom4/", "name": "brave-container-1",
             "binding": "x", "command": "ours"},
        ]
        paths = ast.literal_eval(bc.unbind_plan(existing)["commands"][0][4])
        self.assertEqual(paths, [f"{self.BASE}/custom1/",
                                 f"{self.BASE}/custom2/",
                                 f"{self.BASE}/custom3/"])


class IoShellTest(unittest.TestCase):
    TARGET = {"exe": "/opt/brave/brave", "argv": ["/opt/brave/brave"],
              "pid": 12, "running": True}

    def test_installed_binary_discovery_filters_and_deduplicates(self):
        by_pattern = {
            bc._BINARY_GLOBS[0]: ["/usr/bin/brave-browser", "/usr/bin/brave-beta"],
            bc._BINARY_GLOBS[1]: ["/usr/bin/brave-origin", "/usr/bin/brave-link"],
        }
        with mock.patch.object(bc.glob, "glob", side_effect=lambda pattern: by_pattern[pattern]), \
                mock.patch.object(bc.os, "access", side_effect=lambda path, mode: path != "/usr/bin/brave-beta"), \
                mock.patch.object(bc.os.path, "realpath", side_effect=lambda path: "/usr/bin/brave-origin" if "link" in path else path):
            self.assertEqual(bc._list_installed_binaries(),
                             ["/usr/bin/brave-browser", "/usr/bin/brave-origin"])

    def test_gather_running_uses_ps_and_parser(self):
        completed = SimpleNamespace(stdout="12 /usr/bin/brave-browser\n")
        with mock.patch.object(bc.subprocess, "run", return_value=completed) as run:
            self.assertEqual(bc._gather_running(), [{
                "pid": 12, "exe": "/usr/bin/brave-browser",
                "argv": ["/usr/bin/brave-browser"],
            }])
        run.assert_called_once_with(["ps", "-eo", "pid=,args="],
                                    capture_output=True, text=True, check=True)

    def test_load_prefs_wraps_os_json_and_encoding_failures(self):
        with mock.patch("builtins.open", side_effect=OSError("missing")):
            with self.assertRaises(bc.PreferencesUnreadableError):
                bc.load_prefs(self.TARGET)
        for failure in (OSError("missing"), json.JSONDecodeError("bad", "{", 0),
                        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")):
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch("builtins.open", mock.mock_open()), \
                    mock.patch.object(bc.json, "load", side_effect=failure):
                with self.assertRaises(bc.PreferencesUnreadableError):
                    bc.load_prefs(self.TARGET)

    def test_load_prefs_returns_data_and_exact_path(self):
        handle = mock.mock_open()
        with mock.patch("builtins.open", handle), \
                mock.patch.object(bc.json, "load", return_value={"ok": True}):
            prefs, path = bc.load_prefs({
                "exe": "/opt/brave.com/brave-origin-beta/brave",
                "argv": ["/opt/brave.com/brave-origin-beta/brave",
                         "--profile-directory=Profile 2"],
            })
        self.assertEqual(prefs, {"ok": True})
        self.assertTrue(path.endswith("/Brave-Origin-Beta/Profile 2/Preferences"))
        handle.assert_called_once_with(path, encoding="utf-8")

    def test_wayland_probe_is_best_effort(self):
        with mock.patch.object(bc.os, "listdir", side_effect=OSError):
            self.assertIsNone(bc._wayland_native(12))
        with mock.patch.object(bc.os, "listdir", return_value=["0", "1"]), \
                mock.patch.object(bc.os, "readlink", side_effect=[OSError, "socket:[wayland-0]"]):
            self.assertTrue(bc._wayland_native(12))
        with mock.patch.object(bc.os, "listdir", return_value=["0"]), \
                mock.patch.object(bc.os, "readlink", side_effect=OSError):
            self.assertFalse(bc._wayland_native(12))

    def test_gsettings_reads_and_parses_gvariant_values(self):
        values = {
            (bc._MEDIA_KEYS_SCHEMA, "custom-keybindings"): "@as ['/p/one/', '/p/two/']",
            (f"{bc._CUSTOM_KEYBINDING_SCHEMA}:/p/one/", "name"): "'one'",
            (f"{bc._CUSTOM_KEYBINDING_SCHEMA}:/p/one/", "binding"): "'<Super>1'",
            (f"{bc._CUSTOM_KEYBINDING_SCHEMA}:/p/one/", "command"): "'/bin/one'",
            (f"{bc._CUSTOM_KEYBINDING_SCHEMA}:/p/two/", "name"): "'two'",
            (f"{bc._CUSTOM_KEYBINDING_SCHEMA}:/p/two/", "binding"): "'<Super>2'",
            (f"{bc._CUSTOM_KEYBINDING_SCHEMA}:/p/two/", "command"): "'/bin/two'",
        }
        with mock.patch.object(bc, "_gsettings_get",
                               side_effect=lambda schema, key: values[(schema, key)]):
            self.assertEqual(bc._existing_custom_keybindings(), [
                {"path": "/p/one/", "name": "one", "binding": "<Super>1",
                 "command": "/bin/one"},
                {"path": "/p/two/", "name": "two", "binding": "<Super>2",
                 "command": "/bin/two"},
            ])

    def test_gsettings_malformed_list_is_rejected(self):
        with mock.patch.object(bc, "_gsettings_get", return_value="not a list"):
            with self.assertRaises(ValueError):
                bc._existing_custom_keybindings()

    def test_gsettings_get_and_run_commands(self):
        completed = SimpleNamespace(stdout="'value'\n")
        with mock.patch.object(bc.subprocess, "run", return_value=completed) as run:
            self.assertEqual(bc._gsettings_get("schema", "key"), "'value'")
        run.assert_called_once_with(["gsettings", "get", "schema", "key"],
                                    capture_output=True, text=True, check=True)
        commands = [["gsettings", "set", "schema", "key", "value"]]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), \
                mock.patch.object(bc.subprocess, "run") as run:
            bc._run_commands(commands, dry_run=True)
            bc._run_commands(commands, dry_run=False)
        self.assertEqual(stdout.getvalue(), "gsettings set schema key value\n")
        run.assert_called_once_with(commands[0], check=True)


class CommandShellTest(unittest.TestCase):
    PREFS = {"brave": {"containers": {"list": [
        {"id": "id", "name": "Personal"},
    ]}}}

    def test_open_real_run_uses_argv_without_shell(self):
        target = {"exe": "/opt/brave/brave", "argv": ["/opt/brave/brave"],
                  "running": True, "pid": 1}
        with mock.patch.object(bc, "resolve_target", return_value=target), \
                mock.patch.object(bc, "load_prefs", return_value=(self.PREFS, "/p")), \
                mock.patch.object(bc, "launch_passthrough", return_value=["--user-data-dir=/p"]), \
                mock.patch.object(bc.secrets, "token_urlsafe", return_value="nonce"), \
                mock.patch.object(bc.subprocess, "Popen") as popen:
            self.assertEqual(bc.cmd_open(1, dry_run=False), 0)
        argv = popen.call_args.args[0]
        self.assertIn("--container=Personal", argv)
        self.assertEqual(popen.call_args.kwargs["start_new_session"], True)
        self.assertIs(popen.call_args.kwargs["stdin"], bc.subprocess.DEVNULL)

    def test_unsafe_target_and_malformed_preferences_never_launch(self):
        with mock.patch.object(bc, "resolve_target", return_value={
                "exe": "/opt/brave/brave", "argv": ["/opt/brave/brave"]}), \
                mock.patch.object(bc, "load_prefs", return_value=({}, "/p")), \
                mock.patch.object(bc.subprocess, "Popen") as popen:
            self.assertEqual(bc.cmd_open(1, dry_run=False), 0)
        with mock.patch.object(bc, "resolve_target", return_value={
                "exe": "/opt/brave/brave", "argv": ["/opt/brave/brave"]}), \
                mock.patch.object(bc, "load_prefs", return_value=(self.PREFS, "/p")), \
                mock.patch.object(bc, "launch_passthrough", return_value=[]), \
                mock.patch.object(bc.subprocess, "Popen") as popen:
            with self.assertRaises(argparse.ArgumentTypeError):
                bc.cmd_open(1, dry_run=False, url="javascript:alert(1)")
            popen.assert_not_called()

    def test_list_and_doctor_tolerate_malformed_preferences(self):
        target = {"exe": "/opt/brave/brave", "argv": ["/opt/brave/brave"],
                  "running": False, "pid": None}
        with mock.patch.object(bc, "resolve_target", return_value=target), \
                mock.patch.object(bc, "load_prefs", return_value=({}, "/p")), \
                mock.patch.object(bc, "_existing_custom_keybindings", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(bc.cmd_list(), 0)
            self.assertEqual(bc.cmd_doctor(), 0)
        self.assertIn("1=(empty)", output.getvalue())

    def test_main_dispatch_and_error_codes(self):
        for command, function, expected in (
                ("list", "cmd_list", 11), ("doctor", "cmd_doctor", 12),
                ("install", "cmd_install", 13), ("uninstall", "cmd_uninstall", 14)):
            with self.subTest(command=command), \
                    mock.patch.object(bc, function, return_value=expected) as handler:
                args = [command]
                self.assertEqual(bc.main(args), expected)
                handler.assert_called_once()
        for error, expected in ((bc.NoBraveFoundError("none"), 3),
                                (bc.PreferencesUnreadableError("bad"), 4),
                                (OSError("io"), 5),
                                (subprocess.CalledProcessError(1, "gsettings"), 5),
                                (ValueError("bad gsettings"), 5)):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(bc, "cmd_list", side_effect=error), \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(bc.main(["list"]), expected)
                self.assertIn("error:", stderr.getvalue())

    def test_install_and_uninstall_build_and_execute_plans(self):
        existing = [{"path": "/custom/0/", "name": "foreign",
                     "binding": "x", "command": "foreign"}]
        with mock.patch.object(bc, "_existing_custom_keybindings",
                               return_value=existing), \
                mock.patch.object(bc, "_run_commands") as run:
            self.assertEqual(bc.cmd_install(dry_run=True), 0)
            self.assertEqual(bc.cmd_uninstall(dry_run=False), 0)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(run.call_args_list[0].args[1])
        self.assertFalse(run.call_args_list[1].args[1])

    def test_doctor_reports_wayland_duplicates_and_conflicts(self):
        target = {"exe": "/opt/brave.com/brave-origin-beta/brave",
                  "argv": ["/opt/brave.com/brave-origin-beta/brave"],
                  "running": True, "pid": 12}
        prefs = {"brave": {"containers": {"list": [
            {"id": "1", "name": "duplicate"},
            {"id": "2", "name": "duplicate"},
        ]}}}
        existing = [
            {"path": "/p/1/", "name": "someone-else",
             "binding": "<Control><Shift>1", "command": "other"},
            {"path": "/p/2/", "name": "brave-container-2",
             "binding": "<Control><Shift>2", "command": "ours"},
        ]
        with mock.patch.object(bc, "resolve_target", return_value=target), \
                mock.patch.object(bc, "load_prefs", return_value=(prefs, "/p/Preferences")), \
                mock.patch.object(bc, "_wayland_native", return_value=True) as wayland, \
                mock.patch.object(bc, "_existing_custom_keybindings", return_value=existing), \
                mock.patch.dict(bc.os.environ, {"XDG_SESSION_TYPE": "wayland"}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(bc.cmd_doctor(), 0)
        wayland.assert_called_once_with(12)
        self.assertIn("wayland-native: True", output.getvalue())
        self.assertIn("duplicate container names", output.getvalue())
        self.assertIn("already bound to", output.getvalue())

    def test_doctor_treats_gsettings_read_failure_as_empty(self):
        target = {"exe": "/opt/brave/brave", "argv": ["/opt/brave/brave"],
                  "running": False, "pid": None}
        with mock.patch.object(bc, "resolve_target", return_value=target), \
                mock.patch.object(bc, "load_prefs", return_value=({}, "/p")), \
                mock.patch.object(bc, "_existing_custom_keybindings",
                                  side_effect=ValueError("bad gsettings")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bc.cmd_doctor(), 0)


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

    def test_out_of_range_and_non_integer_slots_are_rejected_before_dispatch(self):
        for slot in ("0", "10", "not-an-int"):
            with self.subTest(slot=slot), self.assertRaises(SystemExit) as caught:
                bc.main(["open", slot])
            self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.calls, [])


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
            marker = urlsplit(argv[-1])
            self.assertEqual(marker.netloc, "brave-container.invalid")
            fields = parse_qs(marker.fragment)
            self.assertEqual(fields["slot"], [str(slot)])
            self.assertEqual(fields["target"], [bc.NEW_TAB_URL])
            self.assertTrue(fields["nonce"][0])


if __name__ == "__main__":
    unittest.main()
