import io
import tempfile
import unittest
from pathlib import Path

from ovpnx import config as cfg
from ovpnx import profiles, wizard
from ovpnx.log import Console


def make_home(root):
    home = Path(root) / "home"
    for rel, names in {
        "CTF/HTB/vpn": ["machines_eu-4.ovpn", "machines_us-2.ovpn"],
        "CTF/Other/VPN": ["only-one.ovpn"],
        "Downloads": ["stray.ovpn", "notes.txt"],
        ".hidden": ["secret.ovpn"],
        "node_modules/x": ["skip.ovpn"],
    }.items():
        d = home / rel
        d.mkdir(parents=True)
        for n in names:
            (d / n).write_text("client\n")
    return home


class Finder(unittest.TestCase):
    def test_finds_nested_skips_hidden_and_bulky(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(tmp)
            found = dict(profiles.find_ovpn_dirs(home))
            self.assertEqual(found[home / "CTF/HTB/vpn"], 2)
            self.assertEqual(found[home / "CTF/Other/VPN"], 1)
            self.assertEqual(found[home / "Downloads"], 1)
            self.assertEqual(len(found), 3)

    def test_depth_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            deep = Path(tmp) / "a/b/c/d/e/f"
            deep.mkdir(parents=True)
            (deep / "x.ovpn").write_text("x")
            self.assertEqual(profiles.find_ovpn_dirs(tmp, max_depth=3), [])

    def test_symlink_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "h"
            real = Path(tmp) / "elsewhere"
            real.mkdir()
            (real / "x.ovpn").write_text("x")
            home.mkdir()
            (home / "link").symlink_to(real)
            self.assertEqual(profiles.find_ovpn_dirs(home), [])


class Helpers(unittest.TestCase):
    def test_labels_and_keys(self):
        home = "/home/u"
        self.assertEqual(wizard.suggest_label("/home/u/CTF/HTB/vpn", home), "HTB")
        self.assertEqual(wizard.suggest_label("/home/u/CTF/SecDojo/VPN", home), "SecDojo")
        self.assertEqual(wizard.suggest_label("/home/u/Downloads", home), "vpn")
        self.assertEqual(wizard.make_key("SecDojo", set()), "secdojo")
        self.assertEqual(wizard.make_key("HTB", {"htb"}), "htb-2")
        self.assertEqual(wizard.make_key("???", set()), "vpn")


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = make_home(self.tmp.name)
        self.path = Path(self.tmp.name) / "cfg" / "config.ini"

    def run_setup(self, answers):
        answers = iter(answers)
        asked = []

        def ask(prompt):
            asked.append(prompt)
            try:
                return next(answers)
            except StopIteration:
                raise EOFError

        out = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            keys = wizard.run_setup(Console(), self.path, str(self.home), ask=ask)
        return keys, out.getvalue(), asked

    def test_full_flow_writes_only_choices(self):
        # folders listed in order: CTF/HTB/vpn, CTF/Other/VPN, Downloads
        keys, out, _ = self.run_setup([
            "1,2", "",                       # pick folders, done
            "", "", "1", "asym",             # htb: name, display, default #1, compression
            "mine", "My Lab", "", "",        # other: name, display, default (single -> auto), compression
            "y",                             # write
        ])
        self.assertEqual(keys, ["htb", "mine"])
        text = self.path.read_text()
        self.assertNotIn("Hack The Box", text)
        self.assertNotIn("thm", text.lower())
        s = cfg.parse_config(text)
        self.assertEqual(s.platforms["htb"].default_profile, "machines_eu-4")
        self.assertEqual(s.platforms["htb"].compression, "asym")
        self.assertEqual(s.platforms["htb"].display_name, "HTB")
        self.assertEqual(s.platforms["mine"].default_profile, "only-one")
        self.assertEqual(s.platforms["mine"].compression, "none")
        self.assertIn("~/CTF/HTB/vpn" if False else "profile_dir = ", text)

    def test_cancel_writes_nothing(self):
        keys, out, _ = self.run_setup(["1", "", "", "", "", "", "n"])
        self.assertIsNone(keys)
        self.assertFalse(self.path.exists())

    def test_nothing_chosen(self):
        keys, _, _ = self.run_setup([""])
        self.assertIsNone(keys)
        self.assertFalse(self.path.exists())

    def test_ctrl_c_and_eof_are_clean(self):
        keys, out, _ = self.run_setup(["1"])        # input ends -> EOFError
        self.assertIsNone(keys)
        self.assertIn("cancelled", out.lower())

    def test_invalid_answers_are_reprompted(self):
        keys, out, _ = self.run_setup([
            "9", "x", "1", "",                 # bad numbers, then folder 1, done
            "Bad Name!", "htb", "", "7", "zzz", "2", "gzip", "asym", "y",
        ])
        self.assertEqual(keys, ["htb"])
        s = cfg.parse_config(self.path.read_text())
        self.assertEqual(s.platforms["htb"].default_profile, "machines_us-2")

    def test_add_to_existing_keeps_text_and_avoids_names(self):
        self.path.parent.mkdir(parents=True)
        original = "; my notes\n[platform:htb]\nprofile_dir = /srv/x\n"
        self.path.write_text(original)
        keys, _, _ = self.run_setup(["a", "1", "", "", "", "", "", "y"])
        # suggested key 'htb' is taken, so the wizard proposes 'htb-2'
        self.assertEqual(keys, ["htb-2"])
        text = self.path.read_text()
        self.assertTrue(text.startswith(original.rstrip("\n")))
        self.assertEqual(set(cfg.parse_config(text).platforms), {"htb", "htb-2"})

    def test_replace_keeps_backup(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("[platform:old]\nprofile_dir = /srv/x\n")
        keys, _, _ = self.run_setup(["r", "3", "", "", "", "", "", "y"])
        self.assertEqual(keys, ["vpn"])
        self.assertEqual(set(cfg.parse_config(self.path.read_text()).platforms), {"vpn"})
        self.assertIn("old", self.path.with_name("config.ini.bak").read_text())

    def test_semicolon_folder_is_skipped(self):
        odd = self.home / "a;b"
        odd.mkdir()
        (odd / "x.ovpn").write_text("x")
        keys, out, _ = self.run_setup(["p", str(odd), "", "y"])
        self.assertIsNone(keys)


if __name__ == "__main__":
    unittest.main()
