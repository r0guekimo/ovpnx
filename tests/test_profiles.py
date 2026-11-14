import hashlib
import tempfile
import unittest
from pathlib import Path

from ovpnx import config as cfg
from ovpnx import profiles as prof

KEY_BLOCK = "<key>\n-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n</key>\n"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def platform(self, **kw):
        return cfg.Platform(key="htb", display_name="HTB", profile_dir=self.dir, **kw)


class Scan(Base):
    def test_lists_only_ovpn_sorted_case_insensitive_ext(self):
        for n in ("b.ovpn", "A.OVPN", "notes.txt", "c.ovpn.bak"):
            (self.dir / n).write_text("client\n")
        (self.dir / "folder.ovpn").mkdir()
        res = prof.scan_platform(self.platform())
        self.assertIsNone(res.error)
        self.assertEqual([p.name for p in res.profiles], ["A", "b"])

    def test_missing_and_not_a_directory(self):
        p = cfg.Platform("x", "X", self.dir / "nope")
        self.assertIn("does not exist", prof.scan_platform(p).error)
        f = self.dir / "file"
        f.write_text("x")
        self.assertIn("not a directory", prof.scan_platform(cfg.Platform("x", "X", f)).error)

    def test_files_are_never_modified(self):
        f = self.dir / "a.ovpn"
        f.write_text("client\nauth-user-pass\n" + KEY_BLOCK)
        before = (f.read_bytes(), f.stat().st_mtime_ns)
        prof.scan_platform(self.platform())
        prof.file_sha256(f)
        self.assertEqual((f.read_bytes(), f.stat().st_mtime_ns), before)

    def test_sha256(self):
        f = self.dir / "a.ovpn"
        f.write_bytes(b"hello")
        self.assertEqual(prof.file_sha256(f), hashlib.sha256(b"hello").hexdigest())


class Auth(Base):
    def auth(self, text):
        f = self.dir / "t.ovpn"
        f.write_text(text)
        return prof.detect_auth(f)

    def test_variants(self):
        self.assertIsNone(self.auth("client\nremote x 1194\n" + KEY_BLOCK))
        self.assertEqual(self.auth("client\nauth-user-pass\n"), "prompt")
        self.assertEqual(self.auth("auth-user-pass /home/u/creds.txt\n"), "file")
        self.assertEqual(self.auth("<auth-user-pass>\nu\np\n</auth-user-pass>\n"), "inline")
        self.assertIsNone(self.auth("# auth-user-pass\n; auth-user-pass\n"))


class Find(Base):
    def setUp(self):
        super().setUp()
        self.p = [prof.Profile("htb", n, self.dir / (n + ".ovpn"))
                  for n in ("machines_eu-4", "starting-point", "Starting-Point-2")]

    def find(self, name, **kw):
        return prof.find_profile(self.platform(**kw), self.p, name)

    def test_exact_case_prefix_and_suffix(self):
        self.assertEqual(self.find("machines_eu-4").name, "machines_eu-4")
        self.assertEqual(self.find("MACHINES_EU-4").name, "machines_eu-4")
        self.assertEqual(self.find("machines").name, "machines_eu-4")
        self.assertEqual(self.find("machines_eu-4.ovpn").name, "machines_eu-4")

    def test_exact_wins_over_prefix(self):
        self.assertEqual(self.find("starting-point").name, "starting-point")

    def test_ambiguous_prefix(self):
        with self.assertRaises(prof.ProfileError) as ctx:
            self.find("start")
        self.assertIn("ambiguous", str(ctx.exception))

    def test_unknown_lists_available(self):
        with self.assertRaises(prof.ProfileError) as ctx:
            self.find("zzz")
        self.assertIn("machines_eu-4", str(ctx.exception))

    def test_default_and_single_profile(self):
        self.assertEqual(self.find(None, default_profile="machines_eu-4").name, "machines_eu-4")
        with self.assertRaises(prof.ProfileError):
            self.find(None)
        only = prof.find_profile(self.platform(), self.p[:1], None)
        self.assertEqual(only.name, "machines_eu-4")


class Candidates(Base):
    def test_finds_ovpn_folders_and_wsl_users(self):
        home = self.dir / "home"
        (home / "vpn-profiles" / "HTB").mkdir(parents=True)
        (home / "vpn-profiles" / "HTB" / "a.ovpn").write_text("x")
        mnt = self.dir / "mnt"
        win = mnt / "c" / "Users" / "HP" / "Desktop" / "VPN Profiles"
        win.mkdir(parents=True)
        (win / "b.ovpn").write_text("x")
        (win / "c.ovpn").write_text("x")
        (mnt / "c" / "Users" / "Public").mkdir()
        native = dict(prof.find_candidate_dirs(home, wsl=False, mnt_root=str(mnt)))
        self.assertEqual(list(native.values()), [1])
        both = dict(prof.find_candidate_dirs(home, wsl=True, mnt_root=str(mnt)))
        self.assertEqual(both[win], 2)


if __name__ == "__main__":
    unittest.main()
