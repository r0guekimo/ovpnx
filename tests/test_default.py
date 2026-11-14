import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovpnx import cli
from ovpnx import config as cfg
from tests.test_cli import run_cli

TEXT = """; my notes
[general]
backend = auto

[platform:htb]
display_name = HTB
profile_dir = /x/htb
; default_profile = commented-out
compression = asym

; second platform
[Platform:SecDojo]
display_name = SecDojo
profile_dir = /x/sec
default_profile = old ; keep me?
"""


class EditText(unittest.TestCase):
    def test_insert_after_last_option(self):
        out = cfg.set_platform_option(TEXT, "htb", "default_profile", "eu")
        lines = out.split("\n")
        i = lines.index("compression = asym")
        self.assertEqual(lines[i + 1], "default_profile = eu")
        self.assertIn("; default_profile = commented-out", out)     # untouched
        self.assertIn("; second platform", out)
        self.assertEqual(cfg.parse_config(out).platforms["htb"].default_profile, "eu")

    def test_replace_is_case_insensitive_on_header(self):
        out = cfg.set_platform_option(TEXT, "secdojo", "default_profile", "new")
        s = cfg.parse_config(out)
        self.assertEqual(s.platforms["secdojo"].default_profile, "new")
        self.assertIsNone(s.platforms["htb"].default_profile)
        self.assertNotIn("old", out)

    def test_remove(self):
        out = cfg.set_platform_option(TEXT, "secdojo", "default_profile", None)
        self.assertIsNone(cfg.parse_config(out).platforms["secdojo"].default_profile)
        self.assertEqual(cfg.set_platform_option(TEXT, "htb", "default_profile", None), TEXT)

    def test_only_the_target_changes(self):
        out = cfg.set_platform_option(TEXT, "htb", "default_profile", "eu")
        self.assertEqual(out.replace("default_profile = eu\n", "", 1), TEXT)

    def test_unknown_platform(self):
        with self.assertRaises(KeyError):
            cfg.set_platform_option(TEXT, "nope", "default_profile", "x")

    def test_section_without_options_and_no_trailing_newline(self):
        out = cfg.set_platform_option("[platform:a]", "a", "default_profile", "x")
        self.assertEqual(out, "[platform:a]\ndefault_profile = x")

    def test_atomic_write_keeps_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.ini"
            p.write_text("old")
            p.chmod(0o600)
            cfg.write_text_atomic(p, "new")
            self.assertEqual(p.read_text(), "new")
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertFalse((Path(tmp) / "c.ini.tmp").exists())


class Command(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for plat, names in {"htb": ["machines_eu-4", "machines_us-2"],
                            "sec": ["N8N-1", "Nebula-2"]}.items():
            (root / plat).mkdir()
            for n in names:
                (root / plat / (n + ".ovpn")).write_text("client\n")
        self.conf = root / "config.ini"
        self.conf.write_text(
            "[platform:htb]\nprofile_dir = %s/htb\ndefault_profile = machines_eu-4\n"
            "[platform:sec]\nprofile_dir = %s/sec\n" % (root, root))
        self.state = str(root / "state")

    def run_cmd(self, *args, answers=None, tty=False):
        argv = ["--config", str(self.conf), "default"] + list(args)
        feed = iter(answers or [])

        def fake_input(prompt=""):
            try:
                return next(feed)
            except StopIteration:
                raise EOFError

        with mock.patch.object(cli, "_interactive", return_value=tty), \
                mock.patch("builtins.input", fake_input):
            return run_cli(argv, self.state)

    def default_of(self, key):
        return cfg.load_config(self.conf).platforms[key].default_profile

    def test_direct_with_partial_name(self):
        code, out, _ = self.run_cmd("htb", "us")
        self.assertEqual(code, 0)
        self.assertEqual(self.default_of("htb"), "machines_us-2")
        self.assertIn("htb-start", out)

    def test_direct_set_on_platform_without_default(self):
        self.assertEqual(self.run_cmd("sec", "neb")[0], 0)
        self.assertEqual(self.default_of("sec"), "Nebula-2")

    def test_clear(self):
        self.assertEqual(self.run_cmd("htb", "--clear")[0], 0)
        self.assertIsNone(self.default_of("htb"))

    def test_unknown_platform_and_profile(self):
        self.assertEqual(self.run_cmd("zzz", "x")[0], 1)
        code, out, err = self.run_cmd("htb", "nothing")
        self.assertEqual(code, 1)
        self.assertEqual(self.default_of("htb"), "machines_eu-4")      # unchanged

    def test_needs_terminal_without_arguments(self):
        code, out, err = self.run_cmd()
        self.assertEqual(code, 1)
        self.assertIn("terminal", out + err)
        code, out, err = self.run_cmd("htb")
        self.assertEqual(code, 1)

    def test_menu_picks_platform_then_profile(self):
        code, out, _ = self.run_cmd(answers=["2", "2"], tty=True)       # sec, 2nd profile
        self.assertEqual(code, 0, out)
        self.assertEqual(self.default_of("sec"), "Nebula-2")
        self.assertIn("current default", self.run_cmd(answers=["1", ""], tty=True)[1])

    def test_menu_by_name_and_zero_clears(self):
        self.assertEqual(self.run_cmd(answers=["htb", "0"], tty=True)[0], 0)
        self.assertIsNone(self.default_of("htb"))

    def test_menu_enter_keeps_and_ctrl_c_is_clean(self):
        before = self.conf.read_text()
        self.run_cmd(answers=["htb", ""], tty=True)
        self.assertEqual(self.conf.read_text(), before)
        code, out, _ = self.run_cmd(answers=[], tty=True)               # EOF at first prompt
        self.assertEqual(code, 0)
        self.assertEqual(self.conf.read_text(), before)

    def test_menu_rejects_bad_answers_then_accepts(self):
        code, out, err = self.run_cmd("htb", answers=["9", "nope", "1"], tty=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.default_of("htb"), "machines_eu-4")
        self.assertIn("Not in the list", out + err)

    def test_single_platform_skips_platform_question(self):
        self.conf.write_text("[platform:htb]\nprofile_dir = %s/htb\n" % self.tmp.name)
        code, out, _ = self.run_cmd(answers=["2"], tty=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.default_of("htb"), "machines_us-2")
        self.assertNotIn("Which platform", out)

    def test_comments_survive(self):
        self.conf.write_text("; keep this\n" + self.conf.read_text())
        self.run_cmd("htb", "us")
        self.assertTrue(self.conf.read_text().startswith("; keep this\n"))


if __name__ == "__main__":
    unittest.main()
