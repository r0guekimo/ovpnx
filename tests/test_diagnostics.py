import socket
import unittest

from ovpnx import diagnostics as d
from ovpnx.backends.base import Session

from .helpers import net_fixture

VPN_ROUTES = d.parse_routes(net_fixture("ip_route_tun0.txt"))
ADDR = d.parse_ipv4_addr(net_fixture("ip_addr.txt"))
FLAGS_UP = {"POINTOPOINT", "MULTICAST", "NOARP", "UP", "LOWER_UP"}


def connected(device="tun0"):
    return Session(path="/s/1", config_name="ovpnx-htb-a", device=device,
                   status="Connection, Client connected")


class FakeProbe:
    """Healthy by default (mirrors the real HTB/WSL2 capture); override per test."""

    def __init__(self, **kw):
        self.flags = FLAGS_UP
        self.addr_ = ADDR
        self.routes_ = VPN_ROUTES
        self.route_gets = {}
        self.ping_ = d.PingResult(sent=3, received=3, avg_ms=45.9)
        self.ping_available = True
        self.servers = ["10.255.255.254"]
        self.dns = True
        self.host_ips = {}
        self.tcp_states = {}
        self.tcp_calls = []
        self.pings = []
        self.__dict__.update(kw)

    def link(self, dev): return self.flags
    def addr(self, dev): return self.addr_
    def routes(self, dev): return self.routes_
    def route_get(self, ip): return self.route_gets.get(ip)
    def has_ping(self): return self.ping_available

    def ping(self, host):
        self.pings.append(host)
        return self.ping_ if self.ping_available else None

    def nameservers(self): return self.servers
    def resolve_name(self, name): return self.dns
    def resolve_host(self, name): return self.host_ips.get(name)

    def tcp(self, ip, port):
        self.tcp_calls.append((ip, port))
        return self.tcp_states.get(port, "filtered")


def levels(checks):
    return [(c.name, c.level) for c in checks]


class Parsers(unittest.TestCase):
    def test_link_flags_real(self):
        flags = d.parse_link_flags(net_fixture("ip_link.txt"))
        self.assertTrue({"UP", "LOWER_UP", "POINTOPOINT"} <= flags)
        self.assertIsNone(d.parse_link_flags("Device does not exist"))

    def test_addr_real_and_peer_form(self):
        self.assertEqual((ADDR.ip, ADDR.prefix, ADDR.peer), ("10.10.16.153", 23, None))
        peer = d.parse_ipv4_addr("5: tun1 inet 10.8.0.2 peer 10.8.0.1/32 scope global tun1")
        self.assertEqual((peer.ip, peer.peer), ("10.8.0.2", "10.8.0.1"))
        self.assertIsNone(d.parse_ipv4_addr("nothing"))

    def test_routes_real(self):
        self.assertEqual(len(VPN_ROUTES), 6)
        pushed = d.pushed_routes(VPN_ROUTES)
        self.assertEqual(len(pushed), 5)
        self.assertIn("10.129.0.0/16", [r.dest for r in pushed])
        kernel = [r for r in VPN_ROUTES if r.proto == "kernel"][0]
        self.assertEqual((kernel.dest, kernel.src), ("10.10.16.0/23", "10.10.16.153"))

    def test_routes_all_has_default_on_eth0(self):
        routes = d.parse_routes(net_fixture("ip_route_all.txt"))
        default = [r for r in routes if r.dest == "default"][0]
        self.assertEqual((default.via, default.dev), ("172.20.224.1", "eth0"))

    def test_route_get(self):
        vpn = d.parse_route_get(net_fixture("route_get_vpn.txt"))
        self.assertEqual((vpn.dev, vpn.via, vpn.src), ("tun0", "10.10.16.1", "10.10.16.153"))
        net = d.parse_route_get(net_fixture("route_get_internet.txt"))
        self.assertEqual(net.dev, "eth0")
        self.assertIsNone(d.parse_route_get("RTNETLINK answers: Network is unreachable"))

    def test_ping_real(self):
        r = d.parse_ping(net_fixture("ping_ok.txt"))
        self.assertEqual((r.sent, r.received, r.avg_ms), (3, 3, 45.877))
        self.assertFalse(r.host_unreachable)

    def test_ping_failure_shapes(self):
        # iputils output shapes (not captured from the lab: written from the format)
        unreachable = ("PING 10.129.1.1 (10.129.1.1) 56(84) bytes of data.\n"
                       "From 10.10.16.1 icmp_seq=1 Destination Host Unreachable\n\n"
                       "--- 10.129.1.1 ping statistics ---\n"
                       "3 packets transmitted, 0 received, +3 errors, 100% packet loss\n")
        r = d.parse_ping(unreachable)
        self.assertEqual((r.sent, r.received, r.host_unreachable), (3, 0, True))
        timeout = "3 packets transmitted, 0 received, 100% packet loss, time 2051ms\n"
        r = d.parse_ping(timeout)
        self.assertEqual((r.received, r.host_unreachable), (0, False))
        r = d.parse_ping("ping: connect: Network is unreachable")
        self.assertEqual(r.sent, 0)
        self.assertIn("unreachable", r.error)

    def test_resolv_conf_real(self):
        self.assertEqual(d.parse_resolv_conf(net_fixture("resolv_conf.txt")), ["10.255.255.254"])
        self.assertEqual(d.parse_resolv_conf("nameserver 1.1.1.1 # x\nnameserver 8.8.8.8"),
                         ["1.1.1.1", "8.8.8.8"])


class BaseChecks(unittest.TestCase):
    def run_(self, probe=None, session=None, expected=()):
        return d.run_diagnostics(session or connected(), "Hack The Box / a",
                                 probe or FakeProbe(), expected)

    def test_healthy_matches_real_capture(self):
        checks = self.run_()
        self.assertEqual(d.summarize(checks), (0, 0))
        self.assertEqual([c.name for c in checks],
                         ["session", "interface", "ip", "routes", "gateway", "dns"])
        text = " | ".join(c.message for c in checks)
        for expected in ("Interface tun0 is up", "VPN IP: 10.10.16.153",
                         "split tunnel", "Gateway 10.10.16.1 reachable",
                         "DNS works"):
            self.assertIn(expected, text)

    def test_state_unknown_is_not_a_problem(self):
        # real tun0 has no 'state UP'; only the flags matter
        self.assertEqual(d.check_interface(FakeProbe(), "tun0").level, d.PASS)

    def test_session_not_connected(self):
        s = connected()
        s.status = "Connection, Connecting"
        self.assertEqual(levels(self.run_(session=s))[0], ("session", d.FAIL))

    def test_interface_problems_stop_the_run(self):
        for flags, text in ((None, "does not exist"), ({"LOWER_UP"}, "administratively down"),
                            ({"UP"}, "no carrier")):
            checks = self.run_(FakeProbe(flags=flags))
            self.assertEqual(checks[-1].level, d.FAIL)
            self.assertIn(text, checks[-1].message)
            self.assertEqual(len(checks), 2)
        checks = self.run_(session=connected(device=""))
        self.assertIn("no tunnel interface", checks[-1].message)

    def test_missing_ip_stops_the_run(self):
        checks = self.run_(FakeProbe(addr_=None))
        self.assertEqual(levels(checks)[-1], ("ip", d.FAIL))

    def test_only_kernel_route_warns(self):
        only = [r for r in VPN_ROUTES if r.proto == "kernel"]
        checks = self.run_(FakeProbe(routes_=only))
        self.assertIn(("routes", d.WARN), levels(checks))

    def test_no_routes_fails(self):
        self.assertIn(("routes", d.FAIL), levels(self.run_(FakeProbe(routes_=[]))))

    def test_full_tunnel_detected(self):
        routes = VPN_ROUTES + [d.Route("0.0.0.0/1", "10.10.16.1", "tun0"),
                               d.Route("128.0.0.0/1", "10.10.16.1", "tun0")]
        checks, _ = d.check_routes(FakeProbe(routes_=routes), "tun0")
        self.assertIn("full tunnel", checks[0].message)

    def test_expected_routes(self):
        ok = d.RouteGet("tun0", "10.10.16.1", "10.10.16.153")
        probe = FakeProbe(route_gets={"10.129.0.1": ok})
        checks, _ = d.check_routes(probe, "tun0", ["10.129.0.0/16"])
        self.assertEqual(checks[-1].level, d.PASS)
        probe = FakeProbe(route_gets={"10.50.0.1": d.RouteGet("eth0", None, None)})
        checks, _ = d.check_routes(probe, "tun0", ["10.50.0.0/16", "10.60.0.0/16"])
        self.assertEqual([c.level for c in checks[1:]], [d.FAIL, d.FAIL])
        self.assertIn("missing (eth0)", checks[1].message)
        self.assertIn("missing (no route)", checks[2].message)

    def test_probe_host_for_cidrs(self):
        self.assertEqual(d._probe_host("10.129.0.0/16"), "10.129.0.1")
        self.assertEqual(d._probe_host("10.1.2.3/32"), "10.1.2.3")
        self.assertEqual(d._probe_host("10.1.2.2/31"), "10.1.2.2")

    def test_gateway_variants(self):
        self.assertEqual(d.find_gateway(VPN_ROUTES, ADDR), "10.10.16.1")
        peer = d.AddrInfo("10.8.0.2", None, "10.8.0.1")
        self.assertEqual(d.find_gateway([], peer), "10.8.0.1")
        self.assertIsNone(d.find_gateway([], ADDR))
        self.assertEqual(d.check_gateway(FakeProbe(), [], ADDR).level, d.INFO)
        no_ping = d.check_gateway(FakeProbe(ping_available=False), VPN_ROUTES, ADDR)
        self.assertEqual(no_ping.level, d.WARN)
        silent = FakeProbe(ping_=d.PingResult(sent=3, received=0))
        c = d.check_gateway(silent, VPN_ROUTES, ADDR)
        self.assertEqual(c.level, d.WARN)
        self.assertIn("filter ICMP", c.hints[0])

    def test_dns_variants(self):
        self.assertEqual(d.check_dns(FakeProbe(servers=[])).level, d.WARN)
        self.assertEqual(d.check_dns(FakeProbe(dns=False)).level, d.WARN)
        self.assertEqual(d.check_dns(FakeProbe(dns=None)).level, d.INFO)


class Target(unittest.TestCase):
    VPN = d.RouteGet("tun0", "10.10.16.1", "10.10.16.153")

    def probe(self, **kw):
        kw.setdefault("route_gets", {"10.129.1.1": self.VPN})
        return FakeProbe(**kw)

    def check(self, target="10.129.1.1", **kw):
        p = self.probe(**kw)
        return d.check_target(target, "tun0", p, (22, 80, 443)), p

    def final(self, checks):
        return checks[-1]

    def test_reachable_by_ping(self):
        checks, _ = self.check()
        self.assertEqual(self.final(checks).level, d.PASS)
        self.assertEqual([c.name for c in checks][:2], ["target-route", "target-ping"])

    def test_icmp_filtered_but_tcp_alive(self):
        checks, _ = self.check(ping_=d.PingResult(sent=3, received=0),
                               tcp_states={22: "open", 80: "closed"})
        last = self.final(checks)
        self.assertEqual(last.level, d.PASS)
        self.assertIn("ICMP is filtered", last.hints[0])
        ports = [c for c in checks if c.name == "target-ports"][0]
        self.assertIn("open 22", ports.message)
        self.assertIn("refused 80", ports.message)

    def test_refused_port_proves_host_alive(self):
        checks, _ = self.check(ping_=d.PingResult(sent=3, received=0),
                               tcp_states={443: "closed"})
        self.assertEqual(self.final(checks).level, d.PASS)

    def test_host_unreachable_from_gateway_is_a_fail_with_cause(self):
        checks, _ = self.check(ping_=d.PingResult(sent=3, received=0, host_unreachable=True))
        last = self.final(checks)
        self.assertEqual(last.level, d.FAIL)
        self.assertIn("host unreachable", last.message)
        self.assertIn("not spawned", last.hints[0])

    def test_tcp_unreachable_counts_as_host_unreachable(self):
        checks, _ = self.check(ping_=d.PingResult(sent=3, received=0),
                               tcp_states={22: "unreachable"})
        self.assertEqual(self.final(checks).level, d.FAIL)

    def test_silence_is_inconclusive_warning(self):
        checks, _ = self.check(ping_=d.PingResult(sent=3, received=0))
        last = self.final(checks)
        self.assertEqual(last.level, d.WARN)
        self.assertIn("inconclusive", last.message)
        self.assertIn("--ports", last.hints[1])

    def test_ping_missing_still_uses_tcp(self):
        checks, _ = self.check(ping_available=False, tcp_states={80: "open"})
        self.assertEqual(self.final(checks).level, d.PASS)
        self.assertIn("target-ping", [c.name for c in checks])

    def test_route_outside_vpn_sends_nothing(self):
        checks, p = self.check("1.1.1.1", route_gets={"1.1.1.1": d.RouteGet("eth0", None, None)})
        self.assertEqual(levels(checks), [("target-route", d.FAIL)])
        self.assertIn("Nothing was sent", checks[0].hints[-1])
        self.assertEqual((p.pings, p.tcp_calls), ([], []))

    def test_no_route(self):
        checks, p = self.check("10.200.0.1")
        self.assertEqual(levels(checks), [("target-route", d.FAIL)])
        self.assertEqual(p.tcp_calls, [])

    def test_hostname_resolution(self):
        checks, p = self.check("box.htb", host_ips={"box.htb": "10.129.1.1"})
        self.assertEqual(self.final(checks).level, d.PASS)
        self.assertIn("box.htb (10.129.1.1)", checks[0].message)
        checks, _ = self.check("box.htb")
        self.assertEqual(levels(checks), [("target", d.FAIL)])
        self.assertIn("/etc/hosts", checks[0].hints[0])

    def test_bad_targets_never_reach_a_command(self):
        for bad in ("10.0.0.1; rm -rf /", "$(id)", "a b", "::1"):
            checks, p = self.check(bad)
            self.assertEqual(levels(checks), [("target", d.FAIL)])
            self.assertEqual((p.pings, p.tcp_calls), ([], []))

    def test_probes_only_requested_ports(self):
        _, p = self.check()
        self.assertEqual(sorted(p.tcp_calls), [("10.129.1.1", 22), ("10.129.1.1", 80),
                                               ("10.129.1.1", 443)])


class RealProbe(unittest.TestCase):
    """Probe wraps commands correctly (fake runner returns captured output)."""

    def make(self, outputs):
        calls = []

        def run(cmd, timeout=5):
            calls.append(cmd)
            for key, value in outputs.items():
                if key in " ".join(cmd):
                    return value
            return None

        return d.Probe(run=run, which=lambda n: "/usr/bin/" + n, extra_dirs=()), calls

    def test_commands_and_parsing(self):
        probe, calls = self.make({
            "link show": (0, net_fixture("ip_link.txt")),
            "addr show": (0, net_fixture("ip_addr.txt")),
            "route show": (0, net_fixture("ip_route_tun0.txt")),
            "route get": (0, net_fixture("route_get_vpn.txt")),
            "-c 3": (0, net_fixture("ping_ok.txt")),
            "ahostsv4": (0, "93.184.216.34 STREAM example.com\n"),
        })
        self.assertIn("UP", probe.link("tun0"))
        self.assertEqual(probe.addr("tun0").ip, "10.10.16.153")
        self.assertEqual(len(probe.routes("tun0")), 6)
        self.assertEqual(probe.route_get("10.129.1.1").dev, "tun0")
        self.assertEqual(probe.ping("10.10.16.1").received, 3)
        self.assertTrue(probe.resolve_name("example.com"))
        self.assertIn(["/usr/bin/ip", "-o", "link", "show", "dev", "tun0"], calls)
        self.assertIn(["/usr/bin/ping", "-n", "-c", "3", "-W", "2", "10.10.16.1"], calls)

    def test_failures_degrade_gracefully(self):
        probe, _ = self.make({})
        self.assertIsNone(probe.link("tun0"))
        self.assertIsNone(probe.addr("tun0"))
        self.assertEqual(probe.routes("tun0"), [])
        self.assertIsNone(probe.route_get("10.0.0.1"))
        self.assertEqual(probe.ping("10.0.0.1").error, "could not run ping")
        self.assertIsNone(probe.resolve_name("example.com"))

    def test_bad_device_names_are_rejected(self):
        probe, calls = self.make({})
        for bad in ("tun0; id", "", "a" * 20):
            self.assertIsNone(probe.link(bad))
            self.assertIsNone(probe.addr(bad))
            self.assertEqual(probe.routes(bad), [])
        self.assertEqual(calls, [])

    def test_no_ping_binary(self):
        probe = d.Probe(run=lambda c, timeout=5: None, which=lambda n: None,
                        extra_dirs=())
        self.assertFalse(probe.has_ping())
        self.assertIsNone(probe.ping("10.0.0.1"))

    def test_nameservers_from_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".conf") as fh:
            fh.write(net_fixture("resolv_conf.txt"))
            fh.flush()
            self.assertEqual(d.Probe(resolv_conf=fh.name).nameservers(), ["10.255.255.254"])
        self.assertEqual(d.Probe(resolv_conf="/nonexistent").nameservers(), [])


class Replay(unittest.TestCase):
    """The whole pipeline (Probe + parsers + checks) on the captured HTB/WSL2 session."""

    def test_captured_session_is_healthy(self):
        outputs = {
            "link show": net_fixture("ip_link.txt"),
            "addr show": net_fixture("ip_addr.txt"),
            "route show": net_fixture("ip_route_tun0.txt"),
            "route get 10.129.0.1": net_fixture("route_get_vpn.txt"),
            "-c 3": net_fixture("ping_ok.txt"),
            "ahostsv4": "93.184.216.34 STREAM example.com\n",
        }

        def run(cmd, timeout=5):
            line = " ".join(cmd)
            for key, text in outputs.items():
                if key in line:
                    return 0, text
            return None

        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".conf") as fh:
            fh.write(net_fixture("resolv_conf.txt"))
            fh.flush()
            probe = d.Probe(run=run, which=lambda n: "/usr/bin/" + n, extra_dirs=(),
                            resolv_conf=fh.name)
            checks = d.run_diagnostics(connected(), "Hack The Box / machines_eu-4", probe,
                                       ["10.129.0.0/16"])
        self.assertEqual(d.summarize(checks), (0, 0), [c.message for c in checks])
        self.assertEqual([c.name for c in checks],
                         ["session", "interface", "ip", "routes", "expected-route",
                          "gateway", "dns"])
        self.assertIn("5 via tun0 (split tunnel; 10.10.8.0/22, 10.10.110.0/24, 10.13.37.0/24 ...)",
                      checks[3].message)
        self.assertIn("avg 45.9 ms", checks[5].message)


class TcpProbe(unittest.TestCase):
    def test_open_closed_and_filtered_states(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        port = server.getsockname()[1]
        self.assertEqual(d.tcp_probe("127.0.0.1", port), "open")

        spare = socket.socket()
        spare.bind(("127.0.0.1", 0))
        closed_port = spare.getsockname()[1]
        spare.close()
        self.assertEqual(d.tcp_probe("127.0.0.1", closed_port), "closed")


if __name__ == "__main__":
    unittest.main()
