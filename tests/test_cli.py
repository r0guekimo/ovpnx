import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovpnx import cli


def run_cli(argv, state_dir, prog="ovpnx"):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, {"XDG_STATE_HOME": state_dir}):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(argv, prog=prog)
            except SystemExit as exc:      # argparse errors
                code = exc.code
    return code, out.getvalue(), err.getvalue()


class Shortcuts(unittest.TestCase):
    def test_start_and_stop(self):
        self.assertEqual(cli.expand_shortcut("htb-start", ["eu"]), ["connect", "htb", "eu"])
        self.assertEqual(cli.expand_shortcut("/usr/bin/thm-start", []), ["connect", "thm"])
        self.assertEqual(cli.expand_shortcut("htb-stop", []), ["disconnect"])
        self.assertEqual(cli.expand_shortcut("my-lab_2-start.py", []), ["connect", "my-lab_2"])

    def test_global_options_stay_in_front(self):
        self.assertEqual(cli.expand_shortcut("htb-start", ["-v", "eu"]),
                         ["-v", "connect", "htb", "eu"])
        self.assertEqual(cli.expand_shortcut("htb-start", ["--config", "/c.ini", "eu"]),
                         ["--config", "/c.ini", "connect", "htb", "eu"])
        self.assertEqual(cli.expand_shortcut("thm-start", ["--config=/c.ini", "-v"]),
                         ["--config=/c.ini", "-v", "connect", "thm"])
        self.assertEqual(cli.expand_shortcut("htb-stop", ["--verbose", "--all"]),
                         ["--verbose", "disconnect", "--all"])

    def test_not_shortcuts(self):
        for name in ("ovpnx", "__main__.py", "start", "-start", "htb-restart"):
            self.assertIsNone(cli.expand_shortcut(name, []))


class Commands(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        htb = self.tmp / "HTB"
        htb.mkdir()
        (htb / "starting-point.ovpn").write_text("client\n")
        (htb / "machines_eu-4.ovpn").write_text("client\nauth-user-pass\n")
        self.conf = self.tmp / "c.ini"
        self.conf.write_text(
            "[platform:htb]\ndisplay_name=Hack The Box\nprofile_dir=%s\n"
            "default_profile=starting-point\n[platform:thm]\nprofile_dir=%s\n"
            % (htb, self.tmp / "missing"))

    def run_(self, *argv):
        return run_cli(["--config", str(self.conf)] + list(argv), str(self.tmp / "state"))

    def test_list_marks_default_and_login(self):
        code, out, err = self.run_("list", "htb")
        self.assertEqual(code, 0)
        self.assertIn("starting-point  (default)", out)
        self.assertIn("machines_eu-4  (asks for login)", out)

    def test_list_reports_missing_dir_with_error_code(self):
        code, out, err = self.run_("list")
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)

    def test_list_unknown_platform(self):
        code, out, err = self.run_("list", "nope")
        self.assertEqual(code, 1)
        self.assertIn("Unknown platform", err)

    def test_list_without_config_guides_user(self):
        code, out, err = run_cli(["--config", str(self.tmp / "none.ini"), "list"],
                                 str(self.tmp / "state"))
        self.assertEqual(code, 1)
        self.assertIn("ovpnx config --init", out)

    def test_invalid_config_lists_problems(self):
        self.conf.write_text("[general]\nbackend=nope\n[platform:a]\nprofile_dir=/x\n")
        code, out, err = self.run_("list")
        self.assertEqual(code, 1)
        self.assertIn("backend must be one of", out)

    def test_config_init_show_and_force(self):
        new = self.tmp / "fresh" / "config.ini"
        state = str(self.tmp / "state")
        code, out, _ = run_cli(["--config", str(new), "config", "--init"], state)
        self.assertEqual(code, 0)
        self.assertTrue(new.exists())
        code, _, err = run_cli(["--config", str(new), "config", "--init"], state)
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        code, _, _ = run_cli(["--config", str(new), "config", "--init", "--force"], state)
        self.assertEqual(code, 0)
        code, out, _ = run_cli(["--config", str(new), "config"], state)
        self.assertEqual(code, 0)
        self.assertIn("platform htb (Hack The Box)", out)

    def test_config_path(self):
        code, out, _ = self.run_("config", "--path")
        self.assertEqual(out.strip(), str(self.conf))

    def test_env_runs(self):
        code, out, err = run_cli(["env"], str(self.tmp / "state"))
        self.assertIn(code, (0, 1))
        self.assertIn("System:", out)
        self.assertIn("Python:", out)

    def test_diagnose_is_not_implemented_yet(self):
        code, _, err = self.run_("diagnose")
        self.assertEqual(code, cli.EXIT_TODO)
        self.assertIn("not implemented", err)

    def test_no_command_is_usage_error(self):
        code, _, err = run_cli([], str(self.tmp / "state"))
        self.assertEqual(code, 2)

    def test_log_written_without_secrets_dir_permissions(self):
        self.run_("list", "htb")
        logf = self.tmp / "state" / "ovpnx" / "logs" / "ovpnx.log"
        self.assertTrue(logf.exists())


if __name__ == "__main__":
    unittest.main()
