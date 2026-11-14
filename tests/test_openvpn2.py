import os
import stat
import tempfile
import unittest
from pathlib import Path

from ovpnx import backends as bk
from ovpnx.backends import openvpn2 as o2
from ovpnx.backends.base import BackendError, Session, failure_hints

from .helpers import ov2_fixture

NAME = "ovpnx-htb-machines_eu-4"
REAL_ARGS = ["openvpn", "--config", "/home/kimo/vpn-profiles/HTB/machines_eu-4.ovpn",
             "--daemon", "ovpnx-probe", "--writepid", "/tmp/x/ovpn.pid",
             "--log", "/tmp/x/ovpn.log", "--allow-compression", "asym", "--verb", "3"]


class LogParsing(unittest.TestCase):
    def test_real_connected_log(self):
        info = o2.parse_log(ov2_fixture("connected.log"))
        self.assertEqual(info.state, "connected")
        self.assertEqual(info.device, "tun0")
        self.assertEqual(info.connected_to, "tcp:154.57.166.111:443")
        self.assertEqual(info.created, "2026-11-14 04:00:28")
        self.assertEqual(info.error, "")

    def test_ping_restart_in_normal_lines_is_not_a_restart(self):
        # these exact lines are in every healthy log (PUSH_REPLY and Timers)
        for line in ("PUSH: Received control message: 'PUSH_REPLY,ping 10,ping-restart 120'",
                     "Timers: ping 10, ping-restart 120"):
            self.assertEqual(o2.parse_log("2026-11-14 04:00:29 Initialization Sequence "
                                          "Completed\n2026-11-14 04:00:29 " + line + "\n").state,
                             "connected")

    def test_real_shutdown_means_stopping(self):
        text = ov2_fixture("connected.log") + ov2_fixture("stopping_tail.log")
        self.assertEqual(o2.parse_log(text).state, "stopping")

    def test_progress_states(self):
        lines = ov2_fixture("connected.log").splitlines()
        upto = lambda marker: "\n".join(lines[:[i for i, l in enumerate(lines) if marker in l][0] + 1])
        self.assertEqual(o2.parse_log("").state, "starting")
        self.assertEqual(o2.parse_log(upto("Attempting to establish")).state, "connecting")
        self.assertEqual(o2.parse_log(upto("TUN/TAP device")).state, "connecting")

    def test_reconnect_cycle(self):
        base = ov2_fixture("connected.log")
        dropped = base + "2026-11-14 05:00:00 [eu] Inactivity timeout (--ping-restart), restarting\n" \
                         "2026-11-14 05:00:00 SIGUSR1[soft,ping-restart] received, process restarting\n"
        self.assertEqual(o2.parse_log(dropped).state, "reconnecting")
        back = dropped + "2026-11-14 05:00:09 Attempting to establish TCP connection\n"
        self.assertEqual(o2.parse_log(back).state, "reconnecting")
        again = back + "2026-11-14 05:00:11 Initialization Sequence Completed\n"
        self.assertEqual(o2.parse_log(again).state, "connected")

    def test_failures(self):
        auth = o2.parse_log("2026-11-14 04:00:29 AUTH: Received control message: AUTH_FAILED\n")
        self.assertEqual((auth.state, auth.error), ("failed", "authentication failed"))
        opt = o2.parse_log("2026-11-14 04:00:28 Options error: Unrecognized option or missing "
                           "or extra parameter(s) in x.ovpn:3: foo (2.7.3)\n")
        self.assertEqual(opt.state, "failed")
        self.assertIn("Unrecognized option", opt.error)
        fatal = o2.parse_log("2026-11-14 04:00:28 Exiting due to fatal error\n")
        self.assertEqual(fatal.state, "failed")

    def test_compression_error_text_reaches_hints(self):
        text = ("OPTIONS ERROR: server pushed compression settings that are not allowed "
                "and will result in a non-working connection.")
        self.assertIn("compression = asym", failure_hints(text, "htb", "none")[0])


class ArgsAndProcesses(unittest.TestCase):
    def test_real_command_line(self):
        a = o2.parse_openvpn_args(REAL_ARGS)
        self.assertEqual((a["daemon"], a["log"]), ("ovpnx-probe", "/tmp/x/ovpn.log"))
        self.assertTrue(a["config"].endswith("machines_eu-4.ovpn"))

    def test_other_shapes(self):
        sysd = o2.parse_openvpn_args(["/usr/sbin/openvpn", "--daemon", "ovpn-client",
                                      "--log-append", "/var/log/o.log", "--config", "c.conf"])
        self.assertEqual((sysd["daemon"], sysd["log"]), ("ovpn-client", "/var/log/o.log"))
        self.assertIsNone(o2.parse_openvpn_args(["openvpn", "--daemon", "--verb", "3"])["daemon"])
        self.assertEqual(o2.parse_openvpn_args(["openvpn", "client.ovpn"])["config"], "client.ovpn")
        self.assertEqual(o2.parse_openvpn_args(["openvpn", "--dev", "tun3"])["dev"], "tun3")
        self.assertEqual(o2.parse_openvpn_args(["openvpn"])["config"], None)

    def test_versions(self):
        self.assertTrue(o2.supports_allow_compression("2.7.3"))
        self.assertTrue(o2.supports_allow_compression("2.5.0"))
        self.assertFalse(o2.supports_allow_compression("2.4.7"))
        self.assertTrue(o2.supports_allow_compression(None))

    def test_process_listing_from_a_fake_proc(self):
        with tempfile.TemporaryDirectory() as root:
            fake_proc(root, 491, "openvpn", REAL_ARGS)
            fake_proc(root, 600, "bash", ["bash"])
            fake_proc(root, 700, "openvpn", ["/usr/sbin/openvpn", "--config", "w.conf"])
            fake_proc(root, 800, "openvpn3-servic", ["openvpn3-service-client"])
            Path(root, "self").mkdir()
            procs = o2.list_processes(root)
            self.assertEqual([p.pid for p in procs], [491, 700])
            self.assertEqual(procs[0].args, REAL_ARGS)
            self.assertEqual(o2.list_processes(os.path.join(root, "missing")), [])


def fake_proc(root, pid, comm, args):
    d = Path(root, str(pid))
    d.mkdir(parents=True, exist_ok=True)
    (d / "comm").write_text(comm + "\n")
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")


def drop_proc(root, pid):
    d = Path(root, str(pid))
    for f in d.iterdir():
        f.unlink()
    d.rmdir()


class Sim:
    """Simulates sudo, the openvpn daemon and /proc for backend tests."""

    def __init__(self, log_text=None, rc=0, uid=1000, version="2.7.3", sudo_ok=True,
                 sudo_prompt_ok=True, die_on_start=False, stubborn=False):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name, "state")
        self.proc = str(Path(self._tmp.name, "proc"))
        Path(self.proc).mkdir()
        self.log_text = ov2_fixture("connected.log") if log_text is None else log_text
        self.rc, self.die_on_start = rc, die_on_start
        self.sudo_ok, self.sudo_prompt_ok, self.stubborn = sudo_ok, sudo_prompt_ok, stubborn
        self.commands, self.launched, self.interactive_calls, self.sleeps = [], [], [], []
        self.log_perm_at_launch = None
        self.backend = o2.OpenVPN2Backend(
            command="/usr/sbin/openvpn", state_dir=str(self.state), version=version,
            run=self.run, launch=self.launch, interactive=self.interactive,
            sleep=self.sleeps.append, proc_root=self.proc, uid=uid)
        self.pid = 491

    def close(self):
        self._tmp.cleanup()

    # fake `sudo -n true` / `kill`
    def run(self, cmd, timeout=5):
        self.commands.append(cmd)
        if cmd[-2:] == ["-n", "true"] or cmd[:3] == ["sudo", "-n", "true"]:
            return (0, "") if self.sudo_ok else (1, "sudo: a password is required")
        if "kill" in cmd:
            pid = int(cmd[-1])
            if not self.stubborn and Path(self.proc, str(pid)).exists():
                drop_proc(self.proc, pid)
            return 0, ""
        return None

    def interactive(self, cmd):
        self.interactive_calls.append(cmd)
        if self.sudo_prompt_ok:
            self.sudo_ok = True
        return 0 if self.sudo_prompt_ok else 1

    # fake `openvpn --daemon`: writes the log and registers a process
    def launch(self, cmd, timeout=30):
        self.launched.append(cmd)
        log = cmd[cmd.index("--log") + 1]
        self.log_perm_at_launch = stat.S_IMODE(os.stat(log).st_mode)
        if self.rc != 0:
            return self.rc, "Options error: bad"
        Path(log).write_text(self.log_text)
        if not self.die_on_start:
            args = [c for c in cmd if c not in ("sudo", "-n")]
            args[0] = "openvpn"
            fake_proc(self.proc, self.pid, "openvpn", args)
        return 0, ""

    def prepare(self, compression="asym", auth=None, name=NAME):
        profile = Path(self._tmp.name, "profiles", "machines_eu-4.ovpn")
        profile.parent.mkdir(exist_ok=True)
        profile.write_text("client\n")
        self.profile = profile
        return self.backend.prepare_config(profile, name, compression, auth)


class Start(unittest.TestCase):
    def sim(self, **kw):
        s = Sim(**kw)
        self.addCleanup(s.close)
        return s

    def test_successful_start(self):
        sim = self.sim()
        handle = sim.prepare()
        session = sim.backend.start(handle, 60)
        cmd = sim.launched[0]
        self.assertEqual(cmd[:4], ["sudo", "-n", "/usr/sbin/openvpn", "--config"])
        self.assertEqual(cmd[4:10], [str(sim.profile), "--cd", str(sim.profile.parent),
                                     "--daemon", NAME, "--log"])
        self.assertTrue(cmd[10].endswith("openvpn2/%s.log" % NAME))
        self.assertEqual(cmd[11:], ["--verb", "3", "--allow-compression", "asym"])
        self.assertEqual((session.backend, session.device, session.pid, session.config_name),
                         ("openvpn2", "tun0", 491, NAME))
        self.assertTrue(session.connected and session.managed)
        self.assertEqual(session.connected_to, "tcp:154.57.166.111:443")
        self.assertEqual(session.path, "pid:491")

    def test_log_is_precreated_private_and_dir_is_private(self):
        sim = self.sim()
        sim.backend.start(sim.prepare(), 60)
        self.assertEqual(sim.log_perm_at_launch, 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(str(sim.state / "openvpn2")).st_mode), 0o700)

    def test_profile_is_never_modified(self):
        sim = self.sim()
        handle = sim.prepare()
        before = (sim.profile.read_bytes(), sim.profile.stat().st_mtime_ns)
        sim.backend.start(handle, 60)
        self.assertEqual((sim.profile.read_bytes(), sim.profile.stat().st_mtime_ns), before)

    def test_compression_flag_rules(self):
        for compression, version, expect in (("none", "2.7.3", False), ("asym", "2.7.3", True),
                                             ("yes", "2.7.3", True), ("asym", "2.4.7", False)):
            sim = self.sim(version=version)
            sim.backend.start(sim.prepare(compression), 60)
            self.assertEqual("--allow-compression" in sim.launched[0], expect, (compression, version))
            if expect:
                self.assertEqual(sim.launched[0][-1], compression)

    def test_root_needs_no_sudo(self):
        sim = self.sim(uid=0)
        sim.backend.start(sim.prepare(), 60)
        self.assertEqual(sim.launched[0][0], "/usr/sbin/openvpn")
        self.assertEqual(sim.commands, [])
        self.assertEqual(sim.interactive_calls, [])

    def test_sudo_prompt_is_used_when_needed(self):
        sim = self.sim(sudo_ok=False)
        sim.backend.start(sim.prepare(), 60)
        self.assertEqual(sim.interactive_calls, [["sudo", "-v"]])
        sim.backend.disconnect(sim.backend.list_sessions()[0])
        self.assertEqual(len(sim.interactive_calls), 1)     # not asked twice

    def test_sudo_refused(self):
        sim = self.sim(sudo_ok=False, sudo_prompt_ok=False)
        handle = sim.prepare()
        with self.assertRaises(BackendError) as ctx:
            sim.backend.start(handle, 60)
        self.assertIn("sudo", str(ctx.exception))
        self.assertEqual(sim.launched, [])

    def test_login_profiles_are_refused_before_anything_runs(self):
        sim = self.sim()
        with self.assertRaises(BackendError) as ctx:
            sim.prepare(auth="prompt")
        self.assertIn("username and password", str(ctx.exception))
        self.assertTrue(any("openvpn3" in h for h in ctx.exception.hints))
        self.assertEqual((sim.launched, sim.commands), ([], []))
        sim.prepare(auth="file")          # credentials file or inline blocks are fine
        sim.prepare(auth="inline")

    def test_prepare_validations(self):
        sim = self.sim()
        with self.assertRaises(BackendError):
            sim.backend.prepare_config("/no/such.ovpn", NAME, "none")
        sim.backend.start(sim.prepare(), 60)
        with self.assertRaises(BackendError) as ctx:
            sim.prepare()
        self.assertIn("already running", str(ctx.exception))

    def test_start_without_prepare(self):
        sim = self.sim()
        with self.assertRaises(BackendError):
            sim.backend.start("openvpn2:" + NAME, 60)

    def test_authentication_failure_cleans_up(self):
        log = "2026-11-14 04:00:29 AUTH: Received control message: AUTH_FAILED\n"
        sim = self.sim(log_text=log)
        with self.assertRaises(BackendError) as ctx:
            sim.backend.start(sim.prepare(), 60)
        self.assertEqual(str(ctx.exception), "authentication failed")
        self.assertIn("AUTH_FAILED", ctx.exception.output)
        self.assertTrue(any("kill" in c for c in sim.commands))
        self.assertEqual(sim.backend.list_sessions(), [])
        self.assertFalse(Path(sim.backend._log_path(NAME)).exists())

    def test_timeout_kills_and_reports(self):
        sim = self.sim(log_text="2026-11-14 04:00:28 Attempting to establish TCP connection\n")
        with self.assertRaises(BackendError) as ctx:
            sim.backend.start(sim.prepare(), 3)
        self.assertIn("within 3 seconds", str(ctx.exception))
        self.assertEqual(len(sim.sleeps), 6)
        self.assertEqual(sim.backend.list_sessions(), [])
        self.assertFalse(Path(sim.backend._log_path(NAME)).exists())

    def test_process_that_dies_immediately(self):
        sim = self.sim(die_on_start=True, log_text="2026-11-14 04:00:28 Exiting due to fatal error\n")
        with self.assertRaises(BackendError) as ctx:
            sim.backend.start(sim.prepare(), 60)
        self.assertIn("Exiting due to fatal error", ctx.exception.output)

    def test_launch_failure_reports_output(self):
        sim = self.sim(rc=1)
        with self.assertRaises(BackendError) as ctx:
            sim.backend.start(sim.prepare(), 60)
        self.assertIn("Options error", ctx.exception.output)
        self.assertFalse(Path(sim.backend._log_path(NAME)).exists())

    def test_launch_cannot_run(self):
        sim = self.sim()
        sim.backend._launch = lambda cmd, timeout=30: None
        with self.assertRaises(BackendError):
            sim.backend.start(sim.prepare(), 60)


class SessionsAndStop(unittest.TestCase):
    def setUp(self):
        self.sim = Sim()
        self.addCleanup(self.sim.close)

    def connect(self):
        return self.sim.backend.start(self.sim.prepare(), 60)

    def test_ours_vs_foreign(self):
        self.connect()
        fake_proc(self.sim.proc, 700, "openvpn",
                  ["/usr/sbin/openvpn", "--daemon", "ovpn-client", "--config", "/etc/o/c.conf",
                   "--dev", "tun1"])
        fake_proc(self.sim.proc, 701, "openvpn", ["/usr/sbin/openvpn", "/home/u/lab.ovpn"])
        ours, foreign, bare = self.sim.backend.list_sessions()
        self.assertTrue(ours.managed and ours.connected)
        self.assertFalse(foreign.managed)
        self.assertFalse(foreign.connected)
        self.assertEqual((foreign.config_name, foreign.device), ("ovpn-client", "tun1"))
        self.assertIn("outside ovpnx", foreign.status)
        self.assertEqual((bare.config_name, bare.device), ("lab", ""))
        self.assertTrue(all(s.backend == "openvpn2" for s in (ours, foreign, bare)))

    def test_foreign_log_is_never_read(self):
        fake_proc(self.sim.proc, 700, "openvpn",
                  ["openvpn", "--daemon", "ovpnx-spoof", "--log", "/etc/passwd"])
        (session,) = self.sim.backend.list_sessions()
        self.assertEqual(session.device, "")
        self.assertIn("outside ovpnx", session.status)

    def test_status_follows_the_log(self):
        session = self.connect()
        log = Path(self.sim.backend._log_path(NAME))
        log.write_text(ov2_fixture("connected.log") +
                       "2026-11-14 05:00:00 SIGUSR1[soft,ping-restart] received, process restarting\n")
        (now,) = self.sim.backend.list_sessions()
        self.assertEqual(now.status, "Connection, Reconnecting")
        self.assertFalse(now.connected)

    def test_disconnect_sends_term_and_waits(self):
        session = self.connect()
        self.sim.backend.disconnect(session)
        self.assertIn(["sudo", "-n", "kill", "-s", "TERM", "491"], self.sim.commands)
        self.assertEqual(self.sim.backend.list_sessions(), [])

    def test_disconnect_of_a_gone_process_is_a_noop(self):
        session = self.connect()
        drop_proc(self.sim.proc, 491)
        before = len(self.sim.commands)
        self.sim.backend.disconnect(session)
        self.assertEqual(len(self.sim.commands), before)

    def test_disconnect_that_will_not_die(self):
        session = self.connect()
        self.sim.stubborn = True
        with self.assertRaises(BackendError) as ctx:
            self.sim.backend.disconnect(session)
        self.assertIn("kill -9 491", ctx.exception.hints[0])

    def test_disconnect_rejects_bad_pids(self):
        for pid in (None, 0, 1):
            with self.assertRaises(BackendError):
                self.sim.backend.disconnect(Session(path="pid:x", pid=pid))

    def test_pid_reuse_cannot_kill_an_unrelated_process(self):
        stale = Session(path="pid:600", config_name=NAME, pid=600, backend="openvpn2")
        fake_proc(self.sim.proc, 600, "bash", ["bash"])      # pid now belongs to bash
        before = len(self.sim.commands)
        self.sim.backend.disconnect(stale)
        self.assertEqual(len(self.sim.commands), before)

    def test_cleanup_and_clean_list(self):
        session = self.connect()
        self.assertEqual(self.sim.backend.list_managed_configs(), [NAME])
        (self.sim.state / "openvpn2" / "notmine.log").write_text("x")
        self.assertEqual(self.sim.backend.list_managed_configs(), [NAME])
        self.sim.backend.disconnect(session)
        self.sim.backend.cleanup_session(session)
        self.assertEqual(self.sim.backend.list_managed_configs(), [])
        self.assertTrue((self.sim.state / "openvpn2" / "notmine.log").exists())
        self.sim.backend.remove_config(name=NAME)             # idempotent
        self.sim.backend.remove_config(name="somebody-elses-name")

    def test_cleanup_ignores_foreign_sessions(self):
        foreign = Session(path="pid:7", config_name="ovpn-client", pid=7, backend="openvpn2")
        self.sim.backend.cleanup_session(foreign)             # must not raise


class Multi(unittest.TestCase):
    class Stub:
        def __init__(self, key, sessions=(), configs=(), fail=False):
            self.key, self.label = key, key.upper()
            self.sessions, self.configs, self.fail = list(sessions), list(configs), fail
            self.calls = []

        def list_sessions(self):
            if self.fail:
                raise BackendError("down")
            return list(self.sessions)

        def prepare_config(self, *a, **k): self.calls.append(("prepare", a)); return "h"
        def start(self, *a, **k): self.calls.append(("start", a)); return "session"
        def disconnect(self, s): self.calls.append(("disconnect", s.path))
        def cleanup_session(self, s): self.calls.append(("cleanup", s.path))
        def remove_config(self, name=None, path=None): self.calls.append(("remove", name))
        def list_managed_configs(self): return list(self.configs)

    def make(self, **kw):
        p = self.Stub("openvpn3", [Session("/p/1", backend="openvpn3")])
        e = self.Stub("openvpn2", [Session("pid:9", backend="openvpn2")], **kw)
        return bk.MultiBackend(p, [e]), p, e

    def test_merges_sessions_and_routes_disconnects(self):
        m, p, e = self.make()
        self.assertEqual([s.path for s in m.list_sessions()], ["/p/1", "pid:9"])
        for s in m.list_sessions():
            m.disconnect(s)
            m.cleanup_session(s)
        self.assertEqual(p.calls, [("disconnect", "/p/1"), ("cleanup", "/p/1")])
        self.assertEqual(e.calls, [("disconnect", "pid:9"), ("cleanup", "pid:9")])

    def test_new_connections_use_the_primary_only(self):
        m, p, e = self.make()
        self.assertEqual(m.prepare_config("f", "n", "none"), "h")
        self.assertEqual(m.start("h", 60), "session")
        self.assertEqual(e.calls, [])
        self.assertEqual((m.key, m.label), ("openvpn3", "OPENVPN3"))

    def test_secondary_failure_is_tolerated_but_primary_is_not(self):
        m, p, e = self.make(fail=True)
        self.assertEqual([s.path for s in m.list_sessions()], ["/p/1"])
        p.fail = True
        with self.assertRaises(BackendError):
            m.list_sessions()

    def test_config_cleanup_goes_to_the_owner(self):
        p = self.Stub("openvpn3", configs=["ovpnx-a"])
        e = self.Stub("openvpn2", configs=["ovpnx-b"])
        m = bk.MultiBackend(p, [e])
        self.assertEqual(m.list_managed_configs(), ["ovpnx-a", "ovpnx-b"])
        m.remove_config(name="ovpnx-b")
        m.remove_config(name="ovpnx-a")
        self.assertEqual(e.calls, [("remove", "ovpnx-b")])
        self.assertEqual(p.calls, [("remove", "ovpnx-a")])


class DetachedRunner(unittest.TestCase):
    def test_returns_output_even_if_a_child_keeps_running(self):
        import sys
        from ovpnx import runner
        code = ("import subprocess,sys;"
                "subprocess.Popen([sys.executable,'-c','import time;time.sleep(5)'],"
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
                "print('daemonized')")
        rc, out = runner.run_detached([sys.executable, "-c", code], timeout=10)
        self.assertEqual((rc, out.strip()), (0, "daemonized"))

    def test_timeout_and_missing_command(self):
        import sys
        from ovpnx import runner
        rc, _ = runner.run_detached([sys.executable, "-c", "import time;time.sleep(30)"], timeout=1)
        self.assertEqual(rc, runner.TIMED_OUT)
        self.assertIsNone(runner.run_detached(["/no/such/binary"]))


class Redaction(unittest.TestCase):
    def test_auth_token_in_push_reply_is_masked(self):
        from ovpnx import log
        line = ("PUSH_REPLY,route 10.0.0.0 255.0.0.0,auth-token SESS_ID_AbCdEf123456,"
                "ifconfig 10.8.0.2 255.255.255.0")
        out = log.redact(line)
        self.assertNotIn("AbCdEf123456", out)
        self.assertIn("route 10.0.0.0", out)
        self.assertIn("ifconfig 10.8.0.2", out)


if __name__ == "__main__":
    unittest.main()
