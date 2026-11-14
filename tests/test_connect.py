"""CLI behaviour for connect / disconnect / status / clean using a fake backend.

Nothing here touches the real system: select_backend, detect_environment,
the `ip` lookup and time.sleep are all patched.
"""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovpnx import cli
from ovpnx import state as state_mod
from ovpnx.backends.base import BackendError, Session
from ovpnx.env import Backend, Environment

CFG_PATH = "/net/openvpn/v3/configuration/abc"


class FakeBackend:
    key = "openvpn3"
    label = "OpenVPN 3"

    def __init__(self, sessions=None, fail=None, configs=None):
        self.sessions = list(sessions or [])
        self.fail = fail
        self.configs = list(configs or [])
        self.calls = []
        self._name = None

    def list_sessions(self):
        return list(self.sessions)

    def prepare_config(self, path, name, compression):
        self._name = name
        self.calls.append(("prepare", str(path), name, compression))
        return CFG_PATH

    def start(self, config_path, timeout, interactive=False):
        self.calls.append(("start", config_path, timeout, interactive))
        if self.fail:
            raise BackendError("the connection failed", output=self.fail)
        session = Session(path="/net/openvpn/v3/sessions/s1", config_name=self._name,
                          device="tun0", status="Connection, Client connected",
                          connected_to="tcp:1.2.3.4:443", created="2026-11-14 01:29:30")
        self.sessions.append(session)
        return session

    def disconnect(self, session):
        self.calls.append(("disconnect", session.path))
        self.sessions = [s for s in self.sessions if s.path != session.path]

    def remove_config(self, name=None, path=None):
        self.calls.append(("remove", name or path))

    def list_managed_configs(self):
        return list(self.configs)

    def names(self):
        return [c[0] for c in self.calls]


def stub_env():
    return Environment("linux", False, None, None, "Test", True, 1000, True, True, "3.12",
                       {"openvpn3": Backend("openvpn3", "/usr/bin/openvpn3", "27.1")})


def managed(name="ovpnx-htb-starting-point", path="/s/1", status="Connection, Client connected"):
    return Session(path=path, config_name=name, device="tun0", status=status,
                   connected_to="tcp:1.2.3.4:443", created="2026-11-14 01:29:30")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state_home = str(self.tmp / "state")
        htb = self.tmp / "HTB"
        htb.mkdir()
        (htb / "starting-point.ovpn").write_text("client\n")
        (htb / "machines_eu-4.ovpn").write_text("client\nauth-user-pass\n")
        self.conf = self.tmp / "c.ini"
        self.write_conf()
        self.htb = htb

    def write_conf(self, general="", compression="asym"):
        self.conf.write_text(
            "[general]\n%s\n[platform:htb]\ndisplay_name = Hack The Box\n"
            "profile_dir = %s\ncompression = %s\ndefault_profile = starting-point\n"
            % (general, self.tmp / "HTB", compression))

    def run_cli(self, backend, *argv, prog="ovpnx", backend_error=None):
        out, err = io.StringIO(), io.StringIO()
        select = mock.Mock(side_effect=backend_error) if backend_error else \
            mock.Mock(return_value=backend)
        patches = [
            mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.state_home}),
            mock.patch("ovpnx.cli.select_backend", select),
            mock.patch("ovpnx.cli.detect_environment", return_value=stub_env()),
            mock.patch("ovpnx.cli.netinfo.interface_ipv4", return_value="10.10.16.153"),
            mock.patch("ovpnx.cli.time.sleep"),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            try:
                code = cli.main(["--config", str(self.conf)] + list(argv), prog=prog)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def saved_state(self):
        return state_mod.load_state(Path(self.state_home) / "ovpnx")


class Connect(Base):
    def test_happy_path(self):
        be = FakeBackend()
        code, out, err = self.run_cli(be, "connect", "htb", "starting-point")
        self.assertEqual(code, 0, err)
        for expected in ("Platform: Hack The Box", "Profile: starting-point",
                         "OpenVPN backend: OpenVPN 3 27.1", "VPN connected",
                         "Interface: tun0", "VPN IP: 10.10.16.153",
                         "Server: tcp:1.2.3.4:443", "Connection established successfully"):
            self.assertIn(expected, out)
        kind, path, name, compression = be.calls[0]
        self.assertEqual(path, str(self.htb / "starting-point.ovpn"))
        self.assertEqual((name, compression), ("ovpnx-htb-starting-point", "asym"))
        self.assertEqual(be.calls[1], ("start", CFG_PATH, 60, False))
        st = self.saved_state()
        self.assertEqual((st["platform"], st["profile"]), ("htb", "starting-point"))

    def test_default_profile_prefix_and_timeout_setting(self):
        self.write_conf("connect_timeout = 90")
        be = FakeBackend()
        code, _, _ = self.run_cli(be, "connect", "htb")
        self.assertEqual(code, 0)
        self.assertEqual(be.calls[1][2], 90)
        be2 = FakeBackend()
        self.run_cli(be2, "connect", "htb", "mach")
        self.assertEqual(be2.calls[0][2], "ovpnx-htb-machines_eu-4")

    def test_login_profile_runs_interactively(self):
        be = FakeBackend()
        self.run_cli(be, "connect", "htb", "machines_eu-4")
        self.assertTrue(be.calls[1][3])

    def test_compression_none_is_not_applied(self):
        self.write_conf(compression="none")
        be = FakeBackend()
        self.run_cli(be, "connect", "htb", "starting-point")
        self.assertEqual(be.calls[0][3], "none")

    def test_shortcut_prog(self):
        be = FakeBackend()
        code, out, _ = self.run_cli(be, "starting-point", prog="htb-start")
        self.assertEqual(code, 0)
        self.assertIn("Profile: starting-point", out)

    def test_refuses_second_session(self):
        be = FakeBackend(sessions=[managed("ovpnx-thm-eu", "/s/9")])
        code, out, err = self.run_cli(be, "connect", "htb", "starting-point")
        self.assertEqual(code, 1)
        self.assertIn("already active", err)
        self.assertEqual(be.calls, [])

    def test_allow_multiple_setting(self):
        self.write_conf("allow_multiple = true")
        be = FakeBackend(sessions=[managed("ovpnx-thm-eu", "/s/9")])
        code, _, _ = self.run_cli(be, "connect", "htb", "starting-point")
        self.assertEqual(code, 0)

    def test_already_connected_to_same_profile(self):
        be = FakeBackend(sessions=[managed()])
        code, out, _ = self.run_cli(be, "connect", "htb", "starting-point")
        self.assertEqual(code, 0)
        self.assertIn("Already connected", out)
        self.assertEqual(be.calls, [])

    def test_failure_cleans_up_and_explains_compression(self):
        self.write_conf(compression="none")
        be = FakeBackend(fail="Compression Error: server pushed compression settings "
                              "that are not allowed")
        code, out, err = self.run_cli(be, "connect", "htb", "starting-point")
        self.assertEqual(code, 1)
        self.assertIn("the connection failed", err)
        self.assertIn("compression = asym", out)
        self.assertEqual(be.calls[-1], ("remove", "ovpnx-htb-starting-point"))
        self.assertIsNone(self.saved_state())

    def test_failure_output_is_redacted(self):
        be = FakeBackend(fail="login failed password=hunter2")
        _, out, err = self.run_cli(be, "connect", "htb", "starting-point")
        self.assertNotIn("hunter2", out + err)

    def test_interrupt_cleans_up(self):
        be = FakeBackend()
        be.start = mock.Mock(side_effect=KeyboardInterrupt)
        code, _, err = self.run_cli(be, "connect", "htb", "starting-point")
        self.assertEqual(code, 130)
        self.assertIn(("remove", "ovpnx-htb-starting-point"), be.calls)

    def test_bad_inputs(self):
        be = FakeBackend()
        code, _, err = self.run_cli(be, "connect", "nope")
        self.assertEqual(code, 1)
        self.assertIn("Unknown platform", err)
        code, _, err = self.run_cli(be, "connect", "htb", "zzz")
        self.assertEqual(code, 1)
        self.assertIn("no profile named", err)
        self.assertEqual(be.calls, [])

    def test_backend_unavailable(self):
        err_ = BackendError("No OpenVPN installation found.", hints=["Install it"])
        code, out, err = self.run_cli(None, "connect", "htb", "starting-point", backend_error=err_)
        self.assertEqual(code, 1)
        self.assertIn("No OpenVPN", err)
        self.assertIn("Install it", out)


class Disconnect(Base):
    def test_disconnects_managed_and_removes_config(self):
        be = FakeBackend(sessions=[managed()])
        state_mod.save_state(Path(self.state_home) / "ovpnx", {
            "platform": "htb", "profile": "starting-point", "session_path": "/s/1"})
        code, out, _ = self.run_cli(be, "disconnect")
        self.assertEqual(code, 0)
        self.assertIn("Disconnected: Hack The Box / starting-point", out)
        self.assertEqual(be.calls, [("disconnect", "/s/1"),
                                    ("remove", "ovpnx-htb-starting-point")])
        self.assertIsNone(self.saved_state())

    def test_nothing_to_disconnect(self):
        code, out, _ = self.run_cli(FakeBackend(), "disconnect")
        self.assertEqual(code, 0)
        self.assertIn("No active VPN session", out)

    def test_foreign_session_needs_all(self):
        foreign = Session(path="/s/7", config_name="work-vpn", device="tun1",
                          status="Connection, Client connected")
        be = FakeBackend(sessions=[foreign])
        code, out, err = self.run_cli(be, "disconnect")
        self.assertEqual(code, 1)
        self.assertIn("--all", out)
        self.assertEqual(be.calls, [])
        code, out, _ = self.run_cli(be, "disconnect", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(be.calls, [("disconnect", "/s/7")])    # config not ours: not removed

    def test_htb_stop_shortcut(self):
        be = FakeBackend(sessions=[managed()])
        code, _, _ = self.run_cli(be, prog="htb-stop")
        self.assertEqual(code, 0)
        self.assertEqual(be.names()[0], "disconnect")

    def test_disconnect_failure_reported(self):
        be = FakeBackend(sessions=[managed()])
        be.disconnect = mock.Mock(side_effect=BackendError("could not disconnect the session"))
        code, _, err = self.run_cli(be, "disconnect")
        self.assertEqual(code, 1)
        self.assertIn("could not disconnect", err)

    def test_works_without_config_file(self):
        be = FakeBackend(sessions=[managed()])
        self.conf.unlink()
        code, out, _ = self.run_cli(be, "disconnect")
        self.assertEqual(code, 0)
        self.assertIn("Disconnected:", out)


class Status(Base):
    def test_connected_details(self):
        be = FakeBackend(sessions=[managed()])
        code, out, _ = self.run_cli(be, "status")
        self.assertEqual(code, 0)
        for expected in ("Platform: Hack The Box", "Profile: starting-point",
                         "Status: Connection, Client connected", "Interface: tun0",
                         "VPN IP: 10.10.16.153", "Server: tcp:1.2.3.4:443",
                         "Started: 2026-11-14 01:29:30"):
            self.assertIn(expected, out)

    def test_not_connected_clears_stale_state(self):
        state_mod.save_state(Path(self.state_home) / "ovpnx", {"platform": "htb"})
        code, out, _ = self.run_cli(FakeBackend(), "status")
        self.assertEqual(code, 1)
        self.assertIn("Not connected", out)
        self.assertIsNone(self.saved_state())

    def test_foreign_session_is_flagged(self):
        foreign = Session(path="/s/7", config_name="work-vpn", device="tun1",
                          status="Connection, Client connected")
        code, out, err = self.run_cli(FakeBackend(sessions=[foreign]), "status")
        self.assertEqual(code, 0)
        self.assertIn("not started by ovpnx", err)
        self.assertIn("Profile: work-vpn", out)

    def test_not_fully_connected_is_a_warning(self):
        be = FakeBackend(sessions=[managed(status="Connection, Connecting")])
        code, out, err = self.run_cli(be, "status")
        self.assertIn("Status: Connection, Connecting", err)


class Clean(Base):
    def test_removes_unused_keeps_active(self):
        be = FakeBackend(sessions=[managed("ovpnx-htb-a")],
                         configs=["ovpnx-htb-a", "ovpnx-thm-old"])
        code, out, _ = self.run_cli(be, "clean")
        self.assertEqual(code, 0)
        self.assertEqual(be.calls, [("remove", "ovpnx-thm-old")])
        self.assertIn("Keeping ovpnx-htb-a", out)

    def test_nothing_to_clean(self):
        code, out, _ = self.run_cli(FakeBackend(), "clean")
        self.assertEqual(code, 0)
        self.assertIn("Nothing to clean", out)


if __name__ == "__main__":
    unittest.main()
