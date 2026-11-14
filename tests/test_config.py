import os
import tempfile
import unittest
from pathlib import Path

from ovpnx import config as cfg


class Paths(unittest.TestCase):
    def test_xdg_and_fallback(self):
        env = {"HOME": "/home/u", "XDG_CONFIG_HOME": "/x/cfg", "XDG_STATE_HOME": "/x/st"}
        self.assertEqual(cfg.config_dir(env), Path("/x/cfg/ovpnx"))
        self.assertEqual(cfg.state_dir(env), Path("/x/st/ovpnx"))
        env = {"HOME": "/home/u"}
        self.assertEqual(cfg.config_dir(env), Path("/home/u/.config/ovpnx"))
        self.assertEqual(cfg.state_dir(env), Path("/home/u/.local/state/ovpnx"))

    def test_relative_xdg_ignored(self):
        env = {"HOME": "/home/u", "XDG_CONFIG_HOME": "relative"}
        self.assertEqual(cfg.config_dir(env), Path("/home/u/.config/ovpnx"))

    def test_override_order(self):
        env = {"HOME": "/h", "OVPNX_CONFIG": "/env.ini"}
        self.assertEqual(cfg.config_path(None, env), Path("/env.ini"))
        self.assertEqual(cfg.config_path("/flag.ini", env), Path("/flag.ini"))


class Parsing(unittest.TestCase):
    def test_default_template_is_valid(self):
        s = cfg.parse_config(cfg.DEFAULT_CONFIG)
        self.assertEqual(s.backend, "auto")
        self.assertFalse(s.allow_multiple)
        self.assertEqual(set(s.platforms), {"htb", "thm"})
        self.assertEqual(s.platforms["htb"].compression, "asym")
        self.assertEqual(s.platforms["thm"].compression, "none")
        self.assertEqual(s.platforms["htb"].display_name, "Hack The Box")
        self.assertEqual(s.warnings, [])

    def test_spaces_quotes_and_inline_comment(self):
        s = cfg.parse_config(
            '[platform:Lab]\nprofile_dir = "/mnt/c/Users/x/VPN Profiles" ; my dir\n'
            "default_profile = eu.ovpn\n"
        )
        p = s.platforms["lab"]
        self.assertEqual(str(p.profile_dir), "/mnt/c/Users/x/VPN Profiles")
        self.assertEqual(p.default_profile, "eu")
        self.assertEqual(p.display_name, "LAB")

    def test_tilde_expansion(self):
        s = cfg.parse_config("[platform:a]\nprofile_dir = ~/x\n")
        self.assertTrue(os.path.isabs(str(s.platforms["a"].profile_dir)))
        self.assertNotIn("~", str(s.platforms["a"].profile_dir))

    def _problems(self, text):
        with self.assertRaises(cfg.ConfigError) as ctx:
            cfg.parse_config(text)
        return " | ".join(ctx.exception.problems)

    def test_bad_backend(self):
        self.assertIn("backend", self._problems(
            "[general]\nbackend = wireguard\n[platform:a]\nprofile_dir=/x\n"))

    def test_bad_compression(self):
        self.assertIn("compression", self._problems(
            "[platform:a]\nprofile_dir=/x\ncompression=lz4\n"))

    def test_relative_dir_rejected(self):
        self.assertIn("absolute", self._problems("[platform:a]\nprofile_dir=rel/x\n"))

    def test_missing_dir_and_bad_name(self):
        out = self._problems("[platform:a]\n[platform:bad name!]\nprofile_dir=/x\n")
        self.assertIn("profile_dir is required", out)
        self.assertIn("platform name", out)

    def test_no_platforms(self):
        self.assertIn("no platforms", self._problems("[general]\nbackend=auto\n"))

    def test_bad_boolean(self):
        self.assertIn("allow_multiple", self._problems(
            "[general]\nallow_multiple=maybe\n[platform:a]\nprofile_dir=/x\n"))

    def test_connect_timeout(self):
        base = "[platform:a]\nprofile_dir=/x\n"
        self.assertEqual(cfg.parse_config(base).connect_timeout, 60)
        self.assertEqual(cfg.parse_config("[general]\nconnect_timeout = 120\n" + base)
                         .connect_timeout, 120)
        for bad in ("abc", "2", "9999"):
            self.assertIn("connect_timeout", self._problems(
                "[general]\nconnect_timeout = %s\n" % bad + base))

    def test_duplicate_platform_after_lowercasing(self):
        out = self._problems("[platform:HTB]\nprofile_dir=/x\n[platform:htb]\nprofile_dir=/y\n")
        self.assertIn("defined twice", out)

    def test_unknown_keys_warn_not_fail(self):
        s = cfg.parse_config(
            "[general]\nbackendd=auto\n[platform:a]\nprofile_dir=/x\ncompresion=asym\n[oops]\n")
        self.assertEqual(len(s.warnings), 3)

    def test_percent_in_path_is_literal(self):
        s = cfg.parse_config("[platform:a]\nprofile_dir=/x/100%/y\n")
        self.assertEqual(str(s.platforms["a"].profile_dir), "/x/100%/y")


class Files(unittest.TestCase):
    def test_write_load_and_no_clobber(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "config.ini"
            cfg.write_default_config(path)
            self.assertEqual(set(cfg.load_config(path).platforms), {"htb", "thm"})
            with self.assertRaises(FileExistsError):
                cfg.write_default_config(path)
            path.write_text("changed")
            cfg.write_default_config(path, force=True)
            self.assertIn("[general]", path.read_text())

    def test_missing_file(self):
        with self.assertRaises(cfg.ConfigNotFound):
            cfg.load_config("/nonexistent/ovpnx.ini")


if __name__ == "__main__":
    unittest.main()
