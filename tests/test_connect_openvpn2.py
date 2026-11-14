"""connect / status / disconnect through the CLI with the real OpenVPN 2 backend.

sudo, the openvpn daemon and /proc are simulated (see Sim); nothing touches the system.
"""
import unittest
from pathlib import Path

from .test_connect import Base
from .test_openvpn2 import Sim, fake_proc

NAME = "ovpnx-htb-starting-point"


class CliWithOpenVPN2(Base):
    def setUp(self):
        super().setUp()
        self.sim = Sim()
        self.addCleanup(self.sim.close)

    def cli(self, *argv, **kw):
        return self.run_cli(self.sim.backend, *argv, **kw)

    def test_full_cycle(self):
        code, out, err = self.cli("connect", "htb", "starting-point")
        self.assertEqual(code, 0, err)
        for expected in ("VPN connected", "Interface: tun0", "VPN IP: 10.10.16.153",
                         "Server: tcp:154.57.166.111:443", "Compression policy: asym"):
            self.assertIn(expected, out)
        self.assertIn("--allow-compression", self.sim.launched[0])
        self.assertEqual(self.saved_state()["session_path"], "pid:491")

        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        for expected in ("Platform: Hack The Box", "Profile: starting-point",
                         "OpenVPN backend: OpenVPN 2", "Status: Connection, Client connected",
                         "Interface: tun0", "Started: 2026-11-14 04:00:28"):
            self.assertIn(expected, out)

        code, out, _ = self.cli("disconnect")
        self.assertEqual(code, 0)
        self.assertIn("Disconnected: Hack The Box / starting-point", out)
        self.assertEqual(self.sim.backend.list_sessions(), [])
        self.assertFalse(Path(self.sim.backend._log_path(NAME)).exists())
        self.assertIsNone(self.saved_state())

        code, out, _ = self.cli("clean")
        self.assertIn("Nothing to clean", out)

    def test_login_profile_is_refused_with_advice(self):
        code, out, err = self.cli("connect", "htb", "machines_eu-4")
        self.assertEqual(code, 1)
        self.assertIn("username and password", err)
        self.assertIn("backend = openvpn3", out)
        self.assertEqual(self.sim.launched, [])

    def test_foreign_openvpn_blocks_a_second_vpn(self):
        fake_proc(self.sim.proc, 700, "openvpn",
                  ["/usr/sbin/openvpn", "--daemon", "ovpn-client", "--config", "/etc/o/c.conf"])
        code, out, err = self.cli("connect", "htb", "starting-point")
        self.assertEqual(code, 1)
        self.assertIn("already active", err)
        self.assertIn("ovpn-client", out + err)
        self.assertEqual(self.sim.launched, [])

    def test_disconnect_all_stops_a_foreign_openvpn(self):
        fake_proc(self.sim.proc, 700, "openvpn",
                  ["/usr/sbin/openvpn", "--daemon", "ovpn-client", "--config", "/etc/o/c.conf"])
        code, out, _ = self.cli("disconnect")
        self.assertEqual(code, 1)
        self.assertIn("--all", out)
        self.assertEqual(self.sim.commands, [])
        code, out, _ = self.cli("disconnect", "--all")
        self.assertEqual(code, 0)
        self.assertIn(["sudo", "-n", "kill", "-s", "TERM", "700"], self.sim.commands)

    def test_failed_login_explains_and_cleans_up(self):
        sim = Sim(log_text="2026-11-14 04:00:29 AUTH: Received control message: AUTH_FAILED\n")
        self.addCleanup(sim.close)
        code, out, err = self.run_cli(sim.backend, "connect", "htb", "starting-point")
        self.assertEqual(code, 1)
        self.assertIn("authentication failed", err)
        self.assertIn("Authentication failed: check your username/password", out)
        self.assertEqual(sim.backend.list_sessions(), [])
        self.assertIsNone(self.saved_state())

    def test_compression_error_hint_for_openvpn2(self):
        self.write_conf(compression="none")
        sim = Sim(log_text="2026-11-14 04:00:29 OPTIONS ERROR: server pushed compression "
                           "settings that are not allowed and will result in a non-working "
                           "connection.\n2026-11-14 04:00:29 Exiting due to fatal error\n")
        self.addCleanup(sim.close)
        code, out, err = self.run_cli(sim.backend, "connect", "htb", "starting-point")
        self.assertEqual(code, 1)
        self.assertIn("compression = asym", out)
        self.assertNotIn("--allow-compression", sim.launched[0])

    def test_interrupt_during_connect_cleans_up(self):
        self.sim.backend._sleep = self._interrupt
        self.sim.log_text = "2026-11-14 04:00:28 Attempting to establish TCP connection\n"
        code, _, err = self.cli("connect", "htb", "starting-point")
        self.assertEqual(code, 130)
        self.assertEqual(self.sim.backend.list_sessions(), [])
        self.assertFalse(Path(self.sim.backend._log_path(NAME)).exists())

    @staticmethod
    def _interrupt(_):
        raise KeyboardInterrupt


if __name__ == "__main__":
    unittest.main()
