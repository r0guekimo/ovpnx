import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ovpnx import cli, shortcuts
from tests.test_cli import run_cli

CONFIG = """[platform:htb]
profile_dir = {d}
[platform:thm]
profile_dir = {d}
"""


class ShortcutFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "bin"
        self.addCleanup(self.tmp.cleanup)

    def test_names(self):
        self.assertEqual(shortcuts.names_for(["htb", "thm"]),
                         ["htb-start", "htb-stop", "thm-start", "thm-stop"])

    def test_install_creates_executables_with_marker(self):
        created, updated, skipped = shortcuts.install(self.dir, ["htb-start"])
        self.assertEqual((created, updated, skipped), (["htb-start"], [], []))
        path = self.dir / "htb-start"
        self.assertTrue(os.access(str(path), os.X_OK))
        self.assertIn(shortcuts.MARKER, path.read_text())
        self.assertTrue(shortcuts.is_ours(path))

    def test_install_twice_updates(self):
        shortcuts.install(self.dir, ["htb-start"])
        _, updated, _ = shortcuts.install(self.dir, ["htb-start"])
        self.assertEqual(updated, ["htb-start"])

    def test_never_overwrites_foreign_file(self):
        self.dir.mkdir()
        (self.dir / "htb-start").write_text("#!/bin/sh\necho mine\n")
        created, updated, skipped = shortcuts.install(self.dir, ["htb-start"])
        self.assertEqual(skipped, ["htb-start"])
        self.assertIn("mine", (self.dir / "htb-start").read_text())

    def test_never_overwrites_foreign_symlink(self):
        self.dir.mkdir()
        (self.dir / "htb-start").symlink_to("/bin/true")
        _, _, skipped = shortcuts.install(self.dir, ["htb-start"])
        self.assertEqual(skipped, ["htb-start"])

    def test_remove_only_ours(self):
        shortcuts.install(self.dir, ["htb-start"])
        (self.dir / "htb-stop").write_text("echo mine\n")
        removed, skipped = shortcuts.remove(self.dir, ["htb-start", "htb-stop", "thm-start"])
        self.assertEqual(removed, ["htb-start"])
        self.assertEqual(skipped, ["htb-stop"])
        self.assertTrue((self.dir / "htb-stop").exists())

    def test_render_checkout_sets_pythonpath(self):
        text = shortcuts.render("htb-start", python="/usr/bin/python3", parent="/home/u/ovpnx/src")
        self.assertIn("PYTHONPATH=/home/u/ovpnx/src", text)
        self.assertIn('prog="htb-start"', text)

    def test_render_installed_has_no_pythonpath(self):
        text = shortcuts.render("htb-stop", python="/opt/v/bin/python",
                                parent="/opt/v/lib/python3.13/site-packages")
        self.assertNotIn("PYTHONPATH", text)

    def test_path_with_spaces_is_quoted(self):
        text = shortcuts.render("htb-start", python="/my dir/python", parent="/a b/src")
        self.assertIn("'/my dir/python'", text)
        self.assertIn("'/a b/src'", text)

    def test_on_path(self):
        self.assertTrue(shortcuts.on_path("/usr/bin", {"PATH": "/x:/usr/bin"}))
        self.assertFalse(shortcuts.on_path("/usr/bin", {"PATH": "/x"}))

    def test_generated_script_really_dispatches(self):
        """Run a generated launcher; htb-stop --help must reach "ovpnx disconnect --help"."""
        src = str(Path(cli.__file__).resolve().parent.parent)
        shortcuts.install(self.dir, ["htb-stop"], python=sys.executable, parent=src)
        proc = subprocess.run([str(self.dir / "htb-stop"), "--help"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, env=dict(os.environ, PATH="/usr/bin:/bin"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("disconnect", proc.stdout)


class ShortcutCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cfgfile = root / "config.ini"
        self.cfgfile.write_text(CONFIG.format(d=root))
        self.bin = root / "bin"
        self.state = str(root / "state")

    def run_cmd(self, *extra):
        return run_cli(["--config", str(self.cfgfile), "install-shortcuts",
                        "--dir", str(self.bin)] + list(extra), self.state)

    def test_install_and_remove(self):
        code, out, err = self.run_cmd()
        self.assertEqual(code, 0)
        for n in ("htb-start", "htb-stop", "thm-start", "thm-stop"):
            self.assertTrue((self.bin / n).exists(), n)
        self.assertIn("not on your PATH", out + err)
        code, out, _ = self.run_cmd("--remove")
        self.assertEqual(code, 0)
        self.assertEqual(list(self.bin.iterdir()), [])

    def test_foreign_files_reported(self):
        self.bin.mkdir()
        for n in ("htb-start", "htb-stop", "thm-start", "thm-stop"):
            (self.bin / n).write_text("x")
        code, out, err = self.run_cmd()
        self.assertEqual(code, 1)
        self.assertIn("Skipped", out + err)

    def test_missing_config(self):
        code, _, _ = run_cli(["--config", str(Path(self.tmp.name) / "none.ini"),
                              "install-shortcuts"], self.state)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
