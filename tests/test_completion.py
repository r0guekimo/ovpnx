import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovpnx import cli, completion
from ovpnx import config as cfg
from tests.test_cli import run_cli

PROFILES = ["machines_eu-4", "machines_us-2", "starting-point", "Starting-Point-2"]


def settings():
    return cfg.parse_config("[platform:htb]\nprofile_dir=/x\n[platform:thm]\nprofile_dir=/y\n")


class Completer(unittest.TestCase):
    def comp(self, *words):
        return completion.complete(list(words), cli.build_parser(),
                                   lambda config: settings(), lambda plat: PROFILES)

    def test_subcommands(self):
        out = self.comp("")
        self.assertIn("connect", out)
        self.assertIn("setup", out)
        self.assertIn("install-completion", out)
        self.assertFalse([o for o in out if o.startswith("_")])
        self.assertEqual(self.comp("co"), ["config", "connect"])

    def test_platforms_then_profiles(self):
        self.assertEqual(self.comp("connect", ""), ["htb", "thm"])
        self.assertEqual(self.comp("connect", "h"), ["htb"])
        self.assertEqual(self.comp("connect", "htb", "mach"), ["machines_eu-4", "machines_us-2"])
        self.assertEqual(self.comp("connect", "htb", ""), PROFILES)
        self.assertEqual(self.comp("connect", "htb", "machines_eu-4", ""), [])
        self.assertEqual(self.comp("connect", "nope", ""), [])

    def test_no_substring_suggestions(self):
        self.assertEqual(self.comp("connect", "htb", "us"), [])

    def test_case_handling(self):
        self.assertEqual(self.comp("connect", "htb", "start"), ["starting-point"])
        self.assertEqual(self.comp("connect", "htb", "Start"), ["Starting-Point-2"])
        self.assertEqual(sorted(self.comp("connect", "htb", "STAR")),
                         ["Starting-Point-2", "starting-point"])

    def test_shortcut_style_after_expansion(self):
        words = cli.expand_shortcut("htb-start", ["machines_u"])
        self.assertEqual(self.comp(*words), ["machines_us-2"])
        words = cli.expand_shortcut("htb-start", ["-v", ""])
        self.assertEqual(self.comp(*words), PROFILES)
        words = cli.expand_shortcut("htb-stop", ["-"])
        self.assertIn("--all", self.comp(*words))

    def test_flags_and_values(self):
        self.assertEqual(self.comp("diagnose", "--p"), ["--ports"])
        self.assertEqual(self.comp("diagnose", "--ports", ""), [])
        self.assertEqual(self.comp("install-shortcuts", "--dir", ""), [completion.FILES])
        self.assertEqual(self.comp("--config", ""), [completion.FILES])
        self.assertEqual(self.comp("install-completion", "--shell", ""), ["bash", "zsh"])
        self.assertEqual(self.comp("-"), [o for o in completion.GLOBAL_OPTS if o.startswith("-")])

    def test_global_options_before_subcommand(self):
        self.assertEqual(self.comp("-v", "connect", ""), ["htb", "thm"])
        self.assertEqual(self.comp("--config", "/tmp/c.ini", "connect", ""), ["htb", "thm"])
        self.assertEqual(self.comp("--config=/tmp/c.ini", "list", ""), ["htb", "thm"])

    def test_no_config_means_no_suggestions_not_errors(self):
        out = completion.complete(["connect", ""], cli.build_parser(),
                                  lambda config: None, lambda plat: [])
        self.assertEqual(out, [])

    def test_unknown_subcommand(self):
        self.assertEqual(self.comp("bogus", ""), [])


class Rc(unittest.TestCase):
    def test_add_is_idempotent_and_remove_restores(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = Path(tmp) / ".bashrc"
            rc.write_text("export A=1\nalias x=y\n")
            completion.rc_add(rc, "/h/.local/share/ovpnx/completion.bash")
            completion.rc_add(rc, "/h/.local/share/ovpnx/completion.bash")
            text = rc.read_text()
            self.assertEqual(text.count(completion.BEGIN), 1)
            self.assertTrue(text.startswith("export A=1\nalias x=y\n"))
            self.assertTrue(completion.rc_has_block(rc))
            self.assertTrue(completion.rc_remove(rc))
            self.assertEqual(rc.read_text(), "export A=1\nalias x=y\n")
            self.assertFalse(completion.rc_remove(rc))

    def test_add_creates_missing_rc(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = Path(tmp) / "new" / ".zshrc"
            completion.rc_add(rc, "/p/completion.zsh")
            self.assertIn("completion.zsh", rc.read_text())

    def test_path_with_spaces_is_quoted(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = Path(tmp) / ".bashrc"
            completion.rc_add(rc, "/my home/completion.bash")
            self.assertIn("'/my home/completion.bash'", rc.read_text())

    def test_detect_shell(self):
        self.assertEqual(completion.detect_shell({"SHELL": "/usr/bin/zsh"}), "zsh")
        self.assertEqual(completion.detect_shell({"SHELL": "/bin/bash"}), "bash")
        self.assertIsNone(completion.detect_shell({"SHELL": "/usr/bin/fish"}))
        self.assertIsNone(completion.detect_shell({}))


class Scripts(unittest.TestCase):
    def test_scripts_register_names(self):
        for shell in ("bash", "zsh"):
            text = completion.script_text(shell, ["ovpnx", "htb-start"], python="/usr/bin/python3",
                                          parent="/opt/v/lib/python3.13/site-packages")
            self.assertIn("ovpnx htb-start", text)
            self.assertIn("_complete --", text)
            self.assertNotIn("PYTHONPATH", text)

    def test_checkout_gets_pythonpath(self):
        text = completion.script_text("bash", ["ovpnx"], python="/usr/bin/python3",
                                      parent="/home/u/ovpnx/src")
        self.assertIn("PYTHONPATH=/home/u/ovpnx/src", text)

    @unittest.skipUnless(shutil.which("bash"), "bash not installed")
    def test_bash_script_really_completes(self):
        src = str(Path(cli.__file__).resolve().parent.parent)
        with tempfile.TemporaryDirectory() as tmp:
            prof = Path(tmp) / "p"
            prof.mkdir()
            for n in ("machines_eu-4", "machines_us-2", "my lab"):
                (prof / (n + ".ovpn")).write_text("client\n")
            conf = Path(tmp) / "c.ini"
            conf.write_text("[platform:htb]\nprofile_dir = %s\n" % prof)
            script = Path(tmp) / "completion.bash"
            script.write_text(completion.script_text("bash", ["ovpnx", "htb-start"],
                                                     python=sys.executable, parent=src))

            def run(words, cword):
                code = ('source %s; COMP_WORDS=(%s); COMP_CWORD=%d; _ovpnx_complete; '
                        'printf "%%s\\n" "${COMPREPLY[@]}"' % (script, words, cword))
                env = dict(os.environ, OVPNX_CONFIG=str(conf), PATH="/usr/bin:/bin")
                out = subprocess.run(["bash", "-c", code], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, universal_newlines=True, env=env)
                self.assertEqual(out.returncode, 0, out.stderr)
                return out.stdout.split("\n")[:-1]

            self.assertEqual(run("htb-start mach", 1), ["machines_eu-4", "machines_us-2"])
            self.assertEqual(run("htb-start my", 1), ["my\\ lab"])
            self.assertEqual(run("ovpnx connect h", 2), ["htb"])


class Commands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cfgfile = root / "config.ini"
        self.cfgfile.write_text("[platform:htb]\nprofile_dir = %s\n" % root)
        self.env = {"HOME": str(root), "XDG_DATA_HOME": str(root / "data"),
                    "XDG_STATE_HOME": str(root / "state"), "SHELL": "/bin/bash"}
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cmd(self, *args):
        return run_cli(["--config", str(self.cfgfile)] + list(args), self.env["XDG_STATE_HOME"])

    def test_install_is_automatic_and_remove_cleans(self):
        code, out, err = self.run_cmd("install-completion")
        self.assertEqual(code, 0, out + err)
        script = Path(self.env["XDG_DATA_HOME"]) / "ovpnx" / "completion.bash"
        self.assertIn("htb-start", script.read_text())
        rc = Path(self.env["HOME"]) / ".bashrc"
        self.assertIn(completion.BEGIN, rc.read_text())
        self.assertIn("exec bash", out)
        code, out, _ = self.run_cmd("install-completion")      # idempotent
        self.assertEqual(rc.read_text().count(completion.BEGIN), 1)
        code, out, _ = self.run_cmd("install-completion", "--remove")
        self.assertEqual(code, 0)
        self.assertFalse(script.exists())
        self.assertNotIn(completion.BEGIN, rc.read_text())

    def test_also_sets_up_other_shell_with_rc_file(self):
        (Path(self.env["HOME"]) / ".zshrc").write_text("# mine\n")
        code, out, _ = self.run_cmd("install-completion")
        self.assertEqual(code, 0)
        self.assertIn("bash and zsh", out)
        self.assertTrue((Path(self.env["XDG_DATA_HOME"]) / "ovpnx" / "completion.zsh").exists())
        self.assertIn("# mine", (Path(self.env["HOME"]) / ".zshrc").read_text())
        self.run_cmd("install-completion", "--remove")
        self.assertEqual((Path(self.env["HOME"]) / ".zshrc").read_text().strip(), "# mine")

    def test_unknown_shell_is_explained(self):
        with mock.patch.dict(os.environ, {"SHELL": "/usr/bin/fish"}):
            code, out, err = self.run_cmd("install-completion")
        self.assertEqual(code, 1)
        self.assertIn("--shell", out + err)

    def test_install_shortcuts_enables_completion(self):
        self.cfgfile.write_text(self.cfgfile.read_text() +
                                "[platform:thm]\nprofile_dir = %s\n" % self.tmp.name)
        self.run_cmd("install-shortcuts", "--dir", str(Path(self.tmp.name) / "bin"))
        script = Path(self.env["XDG_DATA_HOME"]) / "ovpnx" / "completion.bash"
        self.assertIn("thm-start", script.read_text())
        self.assertIn(completion.BEGIN, (Path(self.env["HOME"]) / ".bashrc").read_text())

    def test_hidden_complete_command(self):
        os.environ["OVPNX_CONFIG"] = str(self.cfgfile)
        self.addCleanup(os.environ.pop, "OVPNX_CONFIG", None)
        code, out, _ = self.run_cmd("_complete", "--", "htb-start", "")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "")        # no profiles in that folder, no errors
        code, out, _ = self.run_cmd("_complete", "--", "ovpnx", "connect", "")
        self.assertEqual(out.split(), ["htb"])

    def test_hidden_command_not_in_help(self):
        code, out, _ = run_cli(["--help"], self.env["XDG_STATE_HOME"])
        self.assertNotIn("_complete", out)
        self.assertIn("install-completion", out)


if __name__ == "__main__":
    unittest.main()
