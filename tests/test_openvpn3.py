import unittest

from ovpnx.backends import base
from ovpnx.backends import openvpn3 as ov3
from ovpnx.backends.base import BackendError

from .helpers import fixture

CP = "/net/openvpn/v3/configuration/0c7acd11xb48fx4a84xb457x9bf052d984b8"
SP = "/net/openvpn/v3/sessions/ef5a10a1s40f9s41d0s800dse97707b0bde5"


class Fake:
    """Scripted stand-in for the openvpn3 CLI. Records every call."""

    def __init__(self, responses):
        self.calls = []
        self.responses = responses   # subcommand -> (rc, out) | list | callable

    def __call__(self, cmd, timeout=20):
        self.calls.append(cmd[1:])
        r = self.responses[cmd[1]]
        if callable(r):
            r = r(cmd[2:])
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        return r

    def subcommands(self):
        return [c[0] for c in self.calls]


def backend(responses, stream=None):
    fake = Fake(responses)
    return ov3.OpenVPN3Backend("openvpn3", run=fake, stream=stream or (lambda *a, **k: None),
                               sleep=lambda s: None), fake


class Parsing(unittest.TestCase):
    def test_connected_session_from_real_output(self):
        (s,) = ov3.parse_sessions(fixture("sessions_connected.txt"))
        self.assertEqual(s.path, SP)
        self.assertEqual(s.config_name, "ovpnx-probe")
        self.assertEqual(s.device, "tun0")
        self.assertEqual(s.pid, 1084)
        self.assertEqual(s.owner, "kimo")
        self.assertEqual(s.created, "2026-11-14 01:29:30")
        self.assertEqual(s.connected_to, "tcp:154.57.166.111:443")
        self.assertEqual(s.status, "Connection, Client connected")
        self.assertTrue(s.connected)
        self.assertTrue(s.managed)

    def test_empty(self):
        self.assertEqual(ov3.parse_sessions(fixture("sessions_empty.txt")), [])

    def test_two_sessions_and_missing_device(self):
        text = fixture("sessions_connected.txt") + (
            "-----------------------------------------------------------------------------\n"
            "        Path: /net/openvpn/v3/sessions/aaaa\n"
            "     Created: 2026-11-14 02:00:00                       PID: 99\n"
            "       Owner: kimo                                   Device:\n"
            " Config name: work vpn\n"
            "      Status: Connection, Connecting\n"
            "-----------------------------------------------------------------------------\n")
        first, second = ov3.parse_sessions(text)
        self.assertEqual(first.device, "tun0")
        self.assertEqual(second.config_name, "work vpn")
        self.assertEqual(second.device, "")
        self.assertFalse(second.connected)
        self.assertFalse(second.managed)

    def test_disconnected_is_not_connected(self):
        s = base.Session(path="p", status="Connection, Client disconnected")
        self.assertFalse(s.connected)

    def test_paths(self):
        self.assertEqual(ov3.parse_config_path(fixture("config_import.txt")), CP)
        self.assertEqual(ov3.parse_session_path(fixture("session_start_ok.txt")), SP)
        self.assertIsNone(ov3.parse_config_path("Error"))

    def test_config_names(self):
        self.assertEqual(ov3.parse_config_names(fixture("configs_list_empty.txt")), [])
        self.assertEqual(ov3.parse_config_names(fixture("configs_list_one.txt")), ["ovpnx-probe"])


class Naming(unittest.TestCase):
    def test_make_and_split(self):
        name = base.make_config_name("htb", "machines eu/4")
        self.assertEqual(name, "ovpnx-htb-machines_eu_4")
        self.assertEqual(base.split_config_name("ovpnx-my-lab-eu-4", ["my", "my-lab"]),
                         ("my-lab", "eu-4"))
        self.assertIsNone(base.split_config_name("work-vpn", ["htb"]))
        self.assertIsNone(base.split_config_name("ovpnx-zzz-a", ["htb"]))


class Hints(unittest.TestCase):
    def test_compression_none_suggests_asym(self):
        out = "Compression Error: server pushed compression settings that are not allowed"
        h = base.failure_hints(out, "htb", "none")
        self.assertIn("compression = asym", h[0])
        self.assertIn("[platform:htb]", h[0])

    def test_compression_already_set(self):
        h = base.failure_hints("compression ... not allowed", "htb", "asym")
        self.assertIn("already 'asym'", h[0])

    def test_auth_and_timeout(self):
        self.assertTrue(base.failure_hints("AUTH_FAILED", "x", "none"))
        self.assertTrue(base.failure_hints("Connection timeout", "x", "none"))
        self.assertEqual(base.failure_hints("something else", "x", "none"), [])


class Service(unittest.TestCase):
    def test_service_ok(self):
        b, _ = backend({"sessions-list": (0, "No sessions available\n")})
        self.assertTrue(b.service_ok())

    def test_service_down(self):
        b, _ = backend({"sessions-list": (1, "dbus error")})
        self.assertFalse(b.service_ok())
        dead = ov3.OpenVPN3Backend(run=lambda cmd, timeout=20: None)
        self.assertFalse(dead.service_ok())


class Configs(unittest.TestCase):
    def test_exists_true_false_and_error(self):
        b, _ = backend({"config-manage": (0, fixture("config_exists_present.txt"))})
        self.assertTrue(b.config_exists("x"))
        b, _ = backend({"config-manage": (8, fixture("config_exists_absent.txt"))})
        self.assertFalse(b.config_exists("x"))
        b, _ = backend({"config-manage": (1, "More than one configuration found")})
        with self.assertRaises(BackendError):
            b.config_exists("x")

    def test_prepare_fresh_with_compression(self):
        b, fake = backend({
            "config-manage": [(8, fixture("config_exists_absent.txt")), (0, "Set override\n")],
            "config-import": (0, fixture("config_import.txt")),
        })
        path = b.prepare_config("/p/with space/a.ovpn", "ovpnx-htb-a", "asym")
        self.assertEqual(path, CP)
        self.assertEqual(fake.calls[1], ["config-import", "--config", "/p/with space/a.ovpn",
                                         "--name", "ovpnx-htb-a", "--tag", "ovpnx"])
        self.assertEqual(fake.calls[2], ["config-manage", "--path", CP,
                                         "--allow-compression", "asym"])

    def test_prepare_none_sets_no_override(self):
        b, fake = backend({
            "config-manage": (8, fixture("config_exists_absent.txt")),
            "config-import": (0, fixture("config_import.txt")),
        })
        b.prepare_config("/p/a.ovpn", "ovpnx-thm-a", "none")
        self.assertEqual(fake.subcommands(), ["config-manage", "config-import"])

    def test_prepare_replaces_existing_copy(self):
        b, fake = backend({
            "config-manage": (0, fixture("config_exists_present.txt")),
            "config-remove": (0, "Configuration removed.\n"),
            "config-import": (0, fixture("config_import.txt")),
        })
        b.prepare_config("/p/a.ovpn", "ovpnx-htb-a", "none")
        self.assertEqual(fake.subcommands(), ["config-manage", "config-remove", "config-import"])
        self.assertEqual(fake.calls[1], ["config-remove", "--config", "ovpnx-htb-a", "--force"])

    def test_import_failure(self):
        b, _ = backend({"config-manage": (8, fixture("config_exists_absent.txt")),
                        "config-import": (1, "Option error: bad option")})
        with self.assertRaises(BackendError) as ctx:
            b.prepare_config("/p/a.ovpn", "ovpnx-htb-a", "none")
        self.assertIn("bad option", ctx.exception.output)

    def test_compression_failure_removes_import(self):
        b, fake = backend({
            "config-manage": [(8, fixture("config_exists_absent.txt")), (1, "bad value")],
            "config-import": (0, fixture("config_import.txt")),
            "config-remove": (0, "ok"),
        })
        with self.assertRaises(BackendError):
            b.prepare_config("/p/a.ovpn", "ovpnx-htb-a", "asym")
        self.assertEqual(fake.calls[-1], ["config-remove", "--path", CP, "--force"])

    def test_list_managed_filters_foreign(self):
        table = fixture("configs_list_one.txt").replace(
            "------------------------------------------------------------------------------\n"
            "ovpnx-probe", "------------------------------------------------------------------------------\n"
            "mine-vpn                                                  -\novpnx-probe", 1)
        b, _ = backend({"configs-list": (0, table)})
        self.assertEqual(b.list_managed_configs(), ["ovpnx-probe"])


class Connection(unittest.TestCase):
    def test_start_success_uses_config_path_and_timeout(self):
        seen = {}

        def stream(cmd, echo=False, timeout=None):
            seen.update(cmd=cmd, echo=echo, timeout=timeout)
            return 0, fixture("session_start_ok.txt")

        b, _ = backend({"sessions-list": (0, fixture("sessions_connected.txt"))}, stream)
        session = b.start(CP, 60, interactive=False)
        self.assertEqual(session.device, "tun0")
        self.assertEqual(seen["cmd"], ["openvpn3", "session-start", "--config-path", CP,
                                       "--timeout", "60"])
        self.assertFalse(seen["echo"])
        self.assertEqual(seen["timeout"], 90)

    def test_interactive_has_no_python_timeout(self):
        seen = {}

        def stream(cmd, echo=False, timeout=None):
            seen.update(echo=echo, timeout=timeout)
            return 0, fixture("session_start_ok.txt")

        b, _ = backend({"sessions-list": (0, fixture("sessions_connected.txt"))}, stream)
        b.start(CP, 60, interactive=True)
        self.assertTrue(seen["echo"])
        self.assertIsNone(seen["timeout"])

    def test_start_failure_keeps_output(self):
        out = "Compression Error: server pushed compression settings that are not allowed"
        b, _ = backend({}, lambda *a, **k: (1, out))
        with self.assertRaises(BackendError) as ctx:
            b.start(CP, 60)
        self.assertEqual(ctx.exception.output, out)

    def test_start_without_session_path(self):
        b, _ = backend({}, lambda *a, **k: (0, "weird output"))
        with self.assertRaises(BackendError):
            b.start(CP, 60)

    def test_never_connected(self):
        connecting = fixture("sessions_connected.txt").replace("Client connected", "Connecting")
        b, _ = backend({"sessions-list": (0, connecting)},
                       lambda *a, **k: (0, fixture("session_start_ok.txt")))
        with self.assertRaises(BackendError) as ctx:
            b.start(CP, 60)
        self.assertIn("Connecting", str(ctx.exception))

    def test_disconnect_waits_until_gone(self):
        b, fake = backend({
            "session-manage": (0, "Initiated session shutdown.\n"),
            "sessions-list": [(0, fixture("sessions_connected.txt")),
                              (0, fixture("sessions_empty.txt"))],
        })
        (session,) = ov3.parse_sessions(fixture("sessions_connected.txt"))
        b.disconnect(session)
        self.assertEqual(fake.calls[0], ["session-manage", "--path", SP, "--disconnect"])
        self.assertEqual(fake.subcommands().count("sessions-list"), 2)

    def test_disconnect_failure(self):
        b, _ = backend({"session-manage": (1, "nope")})
        with self.assertRaises(BackendError):
            b.disconnect(base.Session(path=SP))


class Selection(unittest.TestCase):
    def env(self, sudo=True, uid=1000, systemd=True, **backends):
        from ovpnx.env import Backend, Environment
        return Environment("linux", False, None, None, "x", systemd, uid, sudo, True, "3.12",
                           {k: Backend(k, "/bin/" + k, "2.7.3") for k in backends})

    def select(self, preference, env, service_ok=True):
        from unittest import mock
        from ovpnx import backends as bk
        with mock.patch.object(bk.OpenVPN3Backend, "service_ok", return_value=service_ok):
            return bk.select_backend(preference, env, state_dir="/tmp/ovpnx-test")

    def test_auto_prefers_openvpn3_and_also_watches_openvpn2(self):
        from ovpnx import backends as bk
        chosen = self.select("auto", self.env(openvpn3=1, openvpn2=1))
        self.assertIsInstance(chosen, bk.MultiBackend)
        self.assertEqual((chosen.key, chosen.extras[0].key), ("openvpn3", "openvpn2"))

    def test_openvpn3_alone_is_not_wrapped(self):
        from ovpnx import backends as bk
        self.assertIsInstance(self.select("auto", self.env(openvpn3=1)), bk.OpenVPN3Backend)

    def test_only_openvpn2_installed_uses_it(self):
        from ovpnx import backends as bk
        chosen = self.select("auto", self.env(openvpn2=1))
        self.assertIsInstance(chosen, bk.OpenVPN2Backend)

    def test_auto_falls_back_to_openvpn2_when_openvpn3_service_is_down(self):
        chosen = self.select("auto", self.env(openvpn3=1, openvpn2=1), service_ok=False)
        self.assertEqual(chosen.key, "openvpn2")

    def test_forced_openvpn2_ignores_openvpn3(self):
        chosen = self.select("openvpn2", self.env(openvpn3=1, openvpn2=1))
        self.assertEqual(chosen.key, "openvpn2")

    def test_forced_openvpn3_never_falls_back(self):
        with self.assertRaises(BackendError) as ctx:
            self.select("openvpn3", self.env(openvpn3=1, openvpn2=1, systemd=False),
                        service_ok=False)
        self.assertIn("do not respond", str(ctx.exception))
        self.assertTrue(any("systemd" in h for h in ctx.exception.hints))
        with self.assertRaises(BackendError):
            self.select("openvpn3", self.env(openvpn2=1))

    def test_missing_installs(self):
        with self.assertRaises(BackendError) as ctx:
            self.select("openvpn2", self.env(openvpn3=1))
        self.assertIn("not installed", str(ctx.exception))
        with self.assertRaises(BackendError) as ctx:
            self.select("auto", self.env())
        self.assertIn("No OpenVPN", str(ctx.exception))

    def test_openvpn2_needs_root_or_sudo(self):
        with self.assertRaises(BackendError) as ctx:
            self.select("auto", self.env(sudo=False, openvpn2=1))
        self.assertIn("sudo", str(ctx.exception))
        self.assertEqual(self.select("auto", self.env(sudo=False, uid=0, openvpn2=1)).key,
                         "openvpn2")
        # as a secondary (view-only) backend it must not block OpenVPN 3
        chosen = self.select("auto", self.env(sudo=False, openvpn3=1, openvpn2=1))
        self.assertEqual(chosen.key, "openvpn3")


if __name__ == "__main__":
    unittest.main()
