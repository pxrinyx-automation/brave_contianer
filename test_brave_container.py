"""Tests for brave_container.py — stdlib unittest, no external deps."""
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
    # Step 0 probe (2026-09-12): bare `--container=<name>` opens a new tab
    # in the running instance (renderer count 12 -> 13, "Opening in
    # existing browser session"). No URL argument needed.
    EXE = "/opt/brave.com/brave-origin-beta/brave"

    def test_bare_container_switch_by_name(self):
        self.assertEqual(bc.build_argv(self.EXE, [], "dev1"),
                          [self.EXE, "--container=dev1"])

    def test_passthrough_comes_before_container_switch(self):
        result = bc.build_argv(self.EXE, ["--user-data-dir=/x"], "dev1")
        self.assertEqual(result,
                          [self.EXE, "--user-data-dir=/x", "--container=dev1"])

    def test_explicit_url_appended_last(self):
        result = bc.build_argv(self.EXE, [], "dev1", url="brave://newtab")
        self.assertEqual(result,
                          [self.EXE, "--container=dev1", "brave://newtab"])


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


@unittest.skipUnless(os.environ.get("BRAVE_SHORTCUT_E2E") == "1",
                      "set BRAVE_SHORTCUT_E2E=1 to run against real Brave")
class EndToEndTest(unittest.TestCase):
    """Opt-in: exercises the real I/O shell against whatever Brave is
    actually running/installed on this machine. Never launches Brave —
    --dry-run only."""

    def test_open_dry_run_produces_plausible_argv(self):
        target = bc.resolve_target()
        prefs, _ = bc.load_prefs(target)
        container = bc.container_for_slot(prefs, 1)
        self.assertIsNotNone(container, "slot 1 has no container configured")
        passthrough = bc._passthrough_args(target["argv"])
        argv = bc.build_argv(target["exe"], passthrough, container["name"])
        self.assertEqual(argv[0], target["exe"])
        self.assertEqual(argv[-1], f"--container={container['name']}")


if __name__ == "__main__":
    unittest.main()
