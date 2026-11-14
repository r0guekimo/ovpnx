"""Connectivity diagnostics for an active VPN session.

Each check returns a Check with a level (pass / info / warn / fail) and, for
anything that is not a pass, hints about the probable cause. All system access
goes through Probe so the logic can be tested without a VPN.

Design notes learned from real OpenVPN 3 output:
  * a tunnel interface reports "state UNKNOWN" while healthy, so the UP and
    LOWER_UP flags are what count;
  * lab platforms are usually split tunnels (only lab networks are routed
    through the VPN), so a missing default route is not a problem;
  * some networks filter ICMP, so ping failures are warnings, never proof.
"""
import errno
import ipaddress
import re
import shutil
import socket
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .env import EXTRA_BIN_DIRS, find_binary, run_command
from .netinfo import valid_device

PASS, INFO, WARN, FAIL = "pass", "info", "warn", "fail"
DEFAULT_PORTS = (22, 80, 443, 445, 3389, 8080)
DNS_TEST_HOST = "example.com"
_FULL_TUNNEL = {"default", "0.0.0.0/0", "0.0.0.0/1", "128.0.0.0/1"}
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")


@dataclass
class Check:
    name: str
    level: str
    message: str
    hints: List[str] = field(default_factory=list)


@dataclass
class Route:
    dest: str
    via: Optional[str] = None
    dev: Optional[str] = None
    proto: Optional[str] = None
    scope: Optional[str] = None
    src: Optional[str] = None


@dataclass
class RouteGet:
    dev: Optional[str]
    via: Optional[str]
    src: Optional[str]


@dataclass
class AddrInfo:
    ip: str
    prefix: Optional[int] = None
    peer: Optional[str] = None


@dataclass
class PingResult:
    sent: int = 0
    received: int = 0
    avg_ms: Optional[float] = None
    host_unreachable: bool = False
    error: str = ""


# ----------------------------------------------------------------- parsers
def parse_link_flags(text):
    """Flags such as {'UP', 'LOWER_UP'} from `ip link` output, or None."""
    match = re.search(r"<([^>]*)>", text)
    if not match:
        return None
    return {f for f in match.group(1).split(",") if f}


_IP = r"(\d{1,3}(?:\.\d{1,3}){3})"
_ADDR_RE = re.compile(r"\binet\s+%s(?:/(\d+))?(?:\s+peer\s+%s(?:/\d+)?)?" % (_IP, _IP))


def parse_ipv4_addr(text):
    match = _ADDR_RE.search(text)
    if not match:
        return None
    ip, prefix, peer = match.groups()
    return AddrInfo(ip=ip, prefix=int(prefix) if prefix else None, peer=peer)


_ROUTE_KEYS = {"via", "dev", "proto", "scope", "src", "metric", "table"}
_ROUTE_TYPES = {"unreachable", "blackhole", "prohibit", "broadcast", "local",
                "multicast", "throw", "anycast"}


def parse_routes(text):
    """Parse `ip route show` lines into Route objects."""
    routes = []
    for line in text.splitlines():
        tokens = line.split()
        if not tokens or tokens[0] in _ROUTE_TYPES:
            continue
        fields = {}
        i = 1
        while i < len(tokens) - 1:
            if tokens[i] in _ROUTE_KEYS:
                fields[tokens[i]] = tokens[i + 1]
                i += 2
            else:
                i += 1
        routes.append(Route(tokens[0], fields.get("via"), fields.get("dev"),
                            fields.get("proto"), fields.get("scope"), fields.get("src")))
    return routes


def parse_route_get(text):
    """Parse `ip route get` output (first line only)."""
    for line in text.splitlines():
        if not line.strip():
            continue
        dev = re.search(r"\bdev\s+(\S+)", line)
        via = re.search(r"\bvia\s+(\S+)", line)
        src = re.search(r"\bsrc\s+(\S+)", line)
        if not dev:
            return None
        return RouteGet(dev.group(1), via.group(1) if via else None,
                        src.group(1) if src else None)
    return None


def parse_ping(text):
    result = PingResult()
    stats = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", text)
    if stats:
        result.sent, result.received = int(stats.group(1)), int(stats.group(2))
    rtt = re.search(r"=\s*[\d.]+/([\d.]+)/", text)
    if rtt:
        result.avg_ms = float(rtt.group(1))
    low = text.lower()
    result.host_unreachable = "host unreachable" in low
    if not stats:
        result.error = " ".join(text.split())[:200]
    return result


def parse_resolv_conf(text):
    servers = []
    for line in text.splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            servers.append(parts[1])
    return servers


# ------------------------------------------------------------------- probe
def tcp_probe(ip, port, timeout=2.0):
    """Return 'open', 'closed' (refused, so the host is alive), 'filtered',
    'unreachable' or 'error'."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((ip, port))
        return "open"
    except ConnectionRefusedError:
        return "closed"
    except socket.timeout:
        return "filtered"
    except OSError as exc:
        if exc.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN):
            return "unreachable"
        return "error"
    finally:
        sock.close()


class Probe:
    """Everything diagnostics needs from the system, in one injectable place."""

    def __init__(self, run=run_command, which=shutil.which, tcp=tcp_probe,
                 resolv_conf="/etc/resolv.conf", extra_dirs=EXTRA_BIN_DIRS):
        self.run = run
        self.which = which
        self.extra_dirs = extra_dirs
        self.tcp = tcp
        self.resolv_conf = resolv_conf

    def _ip(self, args, timeout=5):
        ip = find_binary("ip", self.which, self.extra_dirs) or "ip"
        result = self.run([ip] + args, timeout=timeout)
        if result is None or result[0] != 0:
            return None
        return result[1]

    def link(self, dev):
        if not valid_device(dev):
            return None
        out = self._ip(["-o", "link", "show", "dev", dev])
        return parse_link_flags(out) if out else None

    def addr(self, dev):
        if not valid_device(dev):
            return None
        out = self._ip(["-4", "-o", "addr", "show", "dev", dev])
        return parse_ipv4_addr(out) if out else None

    def routes(self, dev):
        if not valid_device(dev):
            return []
        out = self._ip(["-4", "route", "show", "dev", dev])
        return parse_routes(out) if out else []

    def route_get(self, ip):
        out = self._ip(["-4", "route", "get", ip])
        return parse_route_get(out) if out else None

    def has_ping(self):
        return find_binary("ping", self.which, self.extra_dirs) is not None

    def ping(self, host, count=3, wait=2):
        binary = find_binary("ping", self.which, self.extra_dirs)
        if not binary:
            return None
        result = self.run([binary, "-n", "-c", str(count), "-W", str(wait), host],
                          timeout=count * wait + 10)
        if result is None:
            return PingResult(error="could not run ping")
        return parse_ping(result[1])

    def nameservers(self):
        try:
            with open(self.resolv_conf, "r", encoding="utf-8", errors="ignore") as fh:
                return parse_resolv_conf(fh.read())
        except OSError:
            return []

    def resolve_name(self, name):
        """True/False if the system resolver works, None if it cannot be tested."""
        getent = find_binary("getent", self.which, self.extra_dirs)
        if not getent:
            return None
        result = self.run([getent, "ahostsv4", name], timeout=8)
        if result is None:
            return None
        return result[0] == 0 and bool(result[1].strip())

    def resolve_host(self, name):
        try:
            return socket.gethostbyname(name)
        except OSError:
            return None


# ------------------------------------------------------------------ checks
def check_session(session, label):
    if session.connected:
        return Check("session", PASS, "Session: %s (%s)" % (label, session.status))
    return Check("session", FAIL,
                 "Session is not connected (status: %s)" % (session.status or "unknown"),
                 ["Wait a moment and run 'ovpnx status', or reconnect with "
                  "'ovpnx disconnect' then 'ovpnx connect ...'."])


def check_interface(probe, dev):
    if not valid_device(dev):
        return Check("interface", FAIL, "The session has no tunnel interface (yet)",
                     ["Check 'ovpnx status'; reconnect if the status never reaches connected."])
    flags = probe.link(dev)
    if flags is None:
        return Check("interface", FAIL, "Interface %s does not exist" % dev,
                     ["The VPN reports this device but the kernel has no such interface. "
                      "Run 'ovpnx disconnect --all' and connect again.",
                      "On WSL2 or minimal systems, make sure the TUN device exists: "
                      "ls -l /dev/net/tun"])
    if "UP" not in flags:
        return Check("interface", FAIL, "Interface %s is administratively down" % dev,
                     ["Reconnect with 'ovpnx disconnect' then 'ovpnx connect ...'."])
    if "LOWER_UP" not in flags:
        return Check("interface", FAIL, "Interface %s is up but has no carrier" % dev,
                     ["The tunnel process is not passing traffic. Reconnect; if it keeps "
                      "happening, try another profile/server."])
    return Check("interface", PASS, "Interface %s is up" % dev)


def check_ip(probe, dev):
    addr = probe.addr(dev)
    if addr is None:
        return Check("ip", FAIL, "No IPv4 address on %s" % dev,
                     ["The tunnel is up but the server did not assign an address. "
                      "Reconnect, or try another profile/server."]), None
    return Check("ip", PASS, "VPN IP: %s" % addr.ip), addr


def pushed_routes(routes):
    """Routes the server added (everything except the kernel's own subnet route)."""
    return [r for r in routes if r.proto != "kernel"]


def check_routes(probe, dev, expected=()):
    routes = probe.routes(dev)
    checks = []
    pushed = pushed_routes(routes)
    if not routes:
        checks.append(Check("routes", FAIL, "No routes through %s" % dev,
                            ["Reconnect; if this persists the profile or server may be broken."]))
    elif not pushed:
        checks.append(Check("routes", WARN,
                            "Only the tunnel's own subnet is routed (no lab routes pushed)",
                            ["Nothing beyond the VPN gateway will be reachable. Try another "
                             "profile/server for this platform."]))
    else:
        full = any(r.dest in _FULL_TUNNEL for r in pushed)
        sample = ", ".join(r.dest for r in pushed[:3])
        more = " ..." if len(pushed) > 3 else ""
        kind = "full tunnel" if full else "split tunnel"
        checks.append(Check("routes", PASS, "Routes: %d via %s (%s; %s%s)"
                            % (len(pushed), dev, kind, sample, more)))
    for cidr in expected:
        host = _probe_host(cidr)
        got = probe.route_get(host)
        if got is not None and got.dev == dev:
            checks.append(Check("expected-route", PASS, "Expected route %s goes through %s"
                                % (cidr, dev)))
        else:
            where = got.dev if got and got.dev else "no route"
            checks.append(Check("expected-route", FAIL,
                                "Expected route %s is missing (%s)" % (cidr, where),
                                ["This server did not push that network. You may be on the "
                                 "wrong VPN server/profile for what you are trying to reach."]))
    return checks, routes


def _probe_host(cidr):
    net = ipaddress.ip_network(cidr, strict=False)
    if net.prefixlen >= 31:
        return str(net.network_address)
    return str(net.network_address + 1)


def find_gateway(routes, addr):
    vias = Counter(r.via for r in routes if r.via)
    if vias:
        return vias.most_common(1)[0][0]
    return addr.peer if addr and addr.peer else None


def check_gateway(probe, routes, addr):
    gateway = find_gateway(routes, addr)
    if not gateway:
        return Check("gateway", INFO, "No VPN gateway address found; skipping ping test")
    if not probe.has_ping():
        return Check("gateway", WARN, "'ping' is not installed; cannot test the gateway",
                     ["Install it (e.g. sudo apt install iputils-ping)."])
    result = probe.ping(gateway)
    if result and result.received > 0:
        avg = ", avg %.1f ms" % result.avg_ms if result.avg_ms is not None else ""
        return Check("gateway", PASS, "Gateway %s reachable (%d/%d replies%s)"
                     % (gateway, result.received, result.sent, avg))
    return Check("gateway", WARN, "Gateway %s did not answer ping" % gateway,
                 ["Some platforms filter ICMP to the gateway, so this alone is not proof "
                  "of a problem.",
                  "If nothing is reachable, reconnect or try another server; "
                  "'ovpnx diagnose <target-ip>' tests a real target."])


def check_dns(probe):
    servers = probe.nameservers()
    if not servers:
        return Check("dns", WARN, "No nameserver configured in /etc/resolv.conf",
                     ["Name lookups will fail. On WSL2 this file is generated by WSL; "
                      "'wsl --shutdown' from Windows regenerates it."])
    worked = probe.resolve_name(DNS_TEST_HOST)
    if worked is None:
        return Check("dns", INFO, "DNS not tested (no 'getent'); nameserver: %s" % servers[0])
    if worked:
        return Check("dns", PASS, "DNS works (%s resolved via %s)" % (DNS_TEST_HOST, servers[0]))
    return Check("dns", WARN, "DNS lookup of %s failed (nameserver %s)"
                 % (DNS_TEST_HOST, servers[0]),
                 ["Lab machines are reached by IP, so the VPN can still work. Hostnames "
                  "like *.htb need an entry in /etc/hosts anyway."])


def run_diagnostics(session, label, probe, expected_routes=()):
    """Base health checks for the session. Stops early when a check makes the
    later ones meaningless."""
    checks = [check_session(session, label)]
    iface = check_interface(probe, session.device)
    checks.append(iface)
    if iface.level == FAIL:
        return checks
    ip_check, addr = check_ip(probe, session.device)
    checks.append(ip_check)
    if ip_check.level == FAIL:
        return checks
    route_checks, routes = check_routes(probe, session.device, expected_routes)
    checks.extend(route_checks)
    checks.append(check_gateway(probe, routes, addr))
    checks.append(check_dns(probe))
    return checks


def summarize(checks):
    fails = sum(1 for c in checks if c.level == FAIL)
    warns = sum(1 for c in checks if c.level == WARN)
    return fails, warns


# ------------------------------------------------------------------ target
def resolve_target(target, probe):
    """Return (ip, None) or (None, Check describing the problem)."""
    try:
        return str(ipaddress.IPv4Address(target)), None
    except ValueError:
        pass
    try:
        ipaddress.IPv6Address(target)
        return None, Check("target", FAIL, "IPv6 targets are not supported yet",
                           ["Use the machine's IPv4 address."])
    except ValueError:
        pass
    if not _HOSTNAME_RE.match(target):
        return None, Check("target", FAIL, "'%s' is not a valid IP address or hostname" % target)
    ip = probe.resolve_host(target)
    if not ip:
        return None, Check("target", FAIL, "Could not resolve '%s'" % target,
                           ["Lab hostnames such as machine.htb are not in public DNS: add "
                            "'<ip> %s' to /etc/hosts, or pass the IP address." % target])
    return ip, None


def probe_ports(probe, ip, ports):
    ports = list(ports)
    if not ports:
        return {}
    with ThreadPoolExecutor(max_workers=min(len(ports), 8)) as pool:
        states = list(pool.map(lambda p: probe.tcp(ip, p), ports))
    return dict(zip(ports, states))


def _ports_with(states: Dict[int, str], wanted):
    return [str(p) for p, s in states.items() if s in wanted]


def check_target(target, dev, probe, ports: Sequence[int] = DEFAULT_PORTS):
    ip, problem = resolve_target(target, probe)
    if problem:
        return [problem]
    shown = ip if ip == target else "%s (%s)" % (target, ip)
    checks = []

    got = probe.route_get(ip)
    if got is None or not got.dev:
        return [Check("target-route", FAIL, "No route to %s" % shown,
                      ["The system has no way to reach this address. Is the VPN "
                       "connected, and is this a lab address?"])]
    if got.dev != dev:
        return [Check("target-route", FAIL,
                      "Traffic to %s leaves via %s, not through the VPN (%s)"
                      % (shown, got.dev, dev),
                      ["This address is not in a network the VPN routes. Wrong IP, or "
                       "you are connected to a different VPN server than this lab needs "
                       "(e.g. Starting Point vs. a machines server).",
                       "Nothing was sent to the target."])]
    checks.append(Check("target-route", PASS, "Route: %s goes through %s%s"
                        % (shown, dev, " via %s" % got.via if got.via else "")))

    ping = probe.ping(ip) if probe.has_ping() else None
    if ping is None:
        checks.append(Check("target-ping", INFO, "Ping skipped ('ping' not installed)"))
    elif ping.received > 0:
        avg = ", avg %.1f ms" % ping.avg_ms if ping.avg_ms is not None else ""
        checks.append(Check("target-ping", PASS, "Ping: %d/%d replies%s"
                            % (ping.received, ping.sent, avg)))
    else:
        detail = " (gateway says: host unreachable)" if ping.host_unreachable else ""
        checks.append(Check("target-ping", INFO, "Ping: no reply%s" % detail))

    states = probe_ports(probe, ip, ports)
    if states:
        parts = []
        for label, wanted in (("open", ("open",)), ("refused", ("closed",)),
                              ("no answer", ("filtered",)),
                              ("unreachable", ("unreachable",)), ("error", ("error",))):
            found = _ports_with(states, wanted)
            if found:
                parts.append("%s %s" % (label, ", ".join(found)))
        alive_ports = bool(_ports_with(states, ("open", "closed")))
        checks.append(Check("target-ports", PASS if alive_ports else INFO,
                            "TCP: " + " | ".join(parts)))
    else:
        alive_ports = False

    icmp_ok = ping is not None and ping.received > 0
    unreachable = (ping is not None and ping.host_unreachable) or bool(
        _ports_with(states, ("unreachable",)))

    if icmp_ok or alive_ports:
        hints = []
        if alive_ports and not icmp_ok and ping is not None:
            hints.append("ICMP is filtered but TCP answers: normal for some machines "
                         "(for example Windows hosts).")
        checks.append(Check("target", PASS, "Target %s is reachable" % shown, hints))
    elif unreachable:
        checks.append(Check(
            "target", FAIL, "Target %s is not reachable: the VPN gateway reports host unreachable"
            % shown,
            ["The VPN works and routes this network, but nothing answers at that address. "
             "Most likely the machine is not spawned/running, or it belongs to a different "
             "VPN server/lab than your profile.",
             "Check on the platform that the machine is active and that the IP is current, "
             "then retry."]))
    else:
        checks.append(Check(
            "target", WARN, "No response from target %s (inconclusive)" % shown,
            ["The machine may still be booting (give it a minute or two after spawning), "
             "or it filters ICMP and the probed ports.",
             "Probe the ports you care about: ovpnx diagnose %s --ports 22,80,8080" % target,
             "The VPN itself looked fine above, so this points at the target, not your "
             "connection."]))
    return checks
