"""`ovpnx diagnose` end to end with a fake backend and a fake probe."""
import unittest
from unittest import mock

from ovpnx import diagnostics as d
from ovpnx.backends.base import Session

from .test_connect import Base, FakeBackend, managed, stub_env
from .test_diagnostics import FakeProbe

VPN = d.RouteGet("tun0", "10.10.16.1", "10.10.16.153")


class DiagnoseCli(Base):
    def diagnose(self, backend, probe, *argv, env=None):
        extra = [mock.patch("ovpnx.cli.Probe", return_value=probe)]
        if env is not None:
            extra.append(mock.patch("ovpnx.cli.detect_environment", return_value=env))
        return self.run_cli(backend, "diagnose", *argv, extra=extra)

    def test_no_session(self):
        code, out, err = self.diagnose(FakeBackend(), FakeProbe())
        self.assertEqual(code, 1)
        self.assertIn("nothing to diagnose", err)
        self.assertIn("ovpnx connect", out)

    def test_healthy_session(self):
        code, out, err = self.diagnose(FakeBackend(sessions=[managed()]), FakeProbe())
        self.assertEqual(code, 0, err)
        for expected in ("Session: Hack The Box / starting-point", "Interface tun0 is up",
                         "VPN IP: 10.10.16.153", "Gateway 10.10.16.1 reachable",
                         "DNS works", "looks healthy", "ovpnx diagnose <target-ip>"):
            self.assertIn(expected, out)
        self.assertEqual(err, "")

    def test_problem_gives_exit_1_and_hint(self):
        probe = FakeProbe(flags=None)
        code, out, err = self.diagnose(FakeBackend(sessions=[managed()]), probe)
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)
        self.assertIn("1 problem found", err)
        self.assertIn("/dev/net/tun", out)

    def test_warnings_keep_exit_0(self):
        probe = FakeProbe(ping_=d.PingResult(sent=3, received=0))
        code, out, err = self.diagnose(FakeBackend(sessions=[managed()]), probe)
        self.assertEqual(code, 0)
        self.assertIn("did not answer ping", err)
        self.assertIn("with 1 warning", err)

    def test_expected_routes_come_from_platform_config(self):
        self.conf.write_text(self.conf.read_text() + "expected_routes = 10.129.0.0/16\n")
        probe = FakeProbe(route_gets={"10.129.0.1": VPN})
        code, out, _ = self.diagnose(FakeBackend(sessions=[managed()]), probe)
        self.assertEqual(code, 0)
        self.assertIn("Expected route 10.129.0.0/16 goes through tun0", out)
        probe = FakeProbe()
        code, _, err = self.diagnose(FakeBackend(sessions=[managed()]), probe)
        self.assertEqual(code, 1)
        self.assertIn("Expected route 10.129.0.0/16 is missing", err)

    def test_target_reachable(self):
        probe = FakeProbe(route_gets={"10.129.1.1": VPN}, tcp_states={22: "open"})
        code, out, _ = self.diagnose(FakeBackend(sessions=[managed()]), probe, "10.129.1.1")
        self.assertEqual(code, 0)
        self.assertIn("Target: 10.129.1.1", out)
        self.assertIn("Target 10.129.1.1 is reachable", out)
        self.assertNotIn("Test one with", out)

    def test_target_host_unreachable_is_exit_1(self):
        probe = FakeProbe(route_gets={"10.129.1.1": VPN},
                          ping_=d.PingResult(sent=3, received=0, host_unreachable=True))
        code, out, err = self.diagnose(FakeBackend(sessions=[managed()]), probe, "10.129.1.1")
        self.assertEqual(code, 1)
        self.assertIn("host unreachable", err)
        self.assertIn("not spawned", out)

    def test_target_silence_is_a_warning_not_a_failure(self):
        probe = FakeProbe(route_gets={"10.129.1.1": VPN},
                          ping_=d.PingResult(sent=3, received=0))
        code, _, err = self.diagnose(FakeBackend(sessions=[managed()]), probe, "10.129.1.1")
        self.assertEqual(code, 0)
        self.assertIn("inconclusive", err)

    def test_target_skipped_when_interface_is_down(self):
        probe = FakeProbe(flags=None)
        code, _, err = self.diagnose(FakeBackend(sessions=[managed()]), probe, "10.129.1.1")
        self.assertEqual(code, 1)
        self.assertIn("Skipping the target test", err)
        self.assertEqual(probe.tcp_calls, [])

    def test_custom_ports(self):
        probe = FakeProbe(route_gets={"10.129.1.1": VPN})
        self.diagnose(FakeBackend(sessions=[managed()]), probe, "10.129.1.1", "--ports", "21,8000")
        self.assertEqual(sorted(p for _, p in probe.tcp_calls), [21, 8000])

    def test_bad_ports_are_a_usage_error(self):
        for bad in ("abc", "0", "70000", ""):
            code, _, err = self.diagnose(FakeBackend(sessions=[managed()]), FakeProbe(),
                                         "10.129.1.1", "--ports", bad)
            self.assertEqual(code, 2, bad)

    def test_ports_without_target_warns_but_runs(self):
        code, out, err = self.diagnose(FakeBackend(sessions=[managed()]), FakeProbe(),
                                       "--ports", "22")
        self.assertEqual(code, 0)
        self.assertIn("--ports only applies together with a target", err)

    def test_wsl_note(self):
        env = stub_env()
        env.is_wsl, env.wsl_version = True, 2
        code, out, _ = self.diagnose(FakeBackend(sessions=[managed()]), FakeProbe(), env=env)
        self.assertIn("only this Linux distribution", out)

    def test_foreign_session_is_still_diagnosed(self):
        foreign = Session(path="/s/7", config_name="work-vpn", device="tun0",
                          status="Connection, Client connected")
        code, out, _ = self.diagnose(FakeBackend(sessions=[foreign]), FakeProbe())
        self.assertEqual(code, 0)
        self.assertIn("Session: work-vpn", out)

    def test_prefers_session_from_state_and_warns_on_several(self):
        a = managed("ovpnx-htb-starting-point", "/s/1")
        b = managed("ovpnx-htb-machines_eu-4", "/s/2")
        code, out, err = self.diagnose(FakeBackend(sessions=[a, b]), FakeProbe())
        self.assertEqual(code, 0)
        self.assertIn("2 sessions are active", err)

    def test_works_without_config_file(self):
        self.conf.unlink()
        code, out, _ = self.diagnose(FakeBackend(sessions=[managed()]), FakeProbe())
        self.assertEqual(code, 0)


class ConfigExpectedRoutes(unittest.TestCase):
    def test_parse_and_validate(self):
        from ovpnx import config as cfg
        s = cfg.parse_config("[platform:a]\nprofile_dir=/x\n"
                             "expected_routes = 10.129.0.0/16, 10.10.10.5\n")
        self.assertEqual(s.platforms["a"].expected_routes, ["10.129.0.0/16", "10.10.10.5/32"])
        self.assertEqual(cfg.parse_config("[platform:a]\nprofile_dir=/x\n")
                         .platforms["a"].expected_routes, [])
        for bad in ("nonsense", "10.0.0.0/99", "fe80::/64"):
            with self.assertRaises(cfg.ConfigError) as ctx:
                cfg.parse_config("[platform:a]\nprofile_dir=/x\nexpected_routes=%s\n" % bad)
            self.assertIn("expected_routes", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
