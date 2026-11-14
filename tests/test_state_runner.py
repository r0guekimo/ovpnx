import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

from ovpnx import netinfo, runner, state


class State(unittest.TestCase):
    def test_roundtrip_private_and_filtered(self):
        with tempfile.TemporaryDirectory() as tmp:
            state.save_state(tmp, {"platform": "htb", "profile": "a", "password": "nope"})
            self.assertEqual(state.load_state(tmp), {"platform": "htb", "profile": "a"})
            mode = state.state_path(tmp).stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_missing_corrupt_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(state.load_state(tmp))
            state.state_path(tmp).write_text("{not json")
            self.assertIsNone(state.load_state(tmp))
            state.state_path(tmp).write_text("[1, 2]")
            self.assertIsNone(state.load_state(tmp))
            state.clear_state(tmp)
            state.clear_state(tmp)       # second call must not raise
            self.assertFalse(state.state_path(tmp).exists())


class Netinfo(unittest.TestCase):
    LINE = "5: tun0    inet 10.10.16.153/23 scope global tun0\\       valid_lft forever\n"

    def test_parses_ip_output(self):
        self.assertEqual(netinfo.interface_ipv4("tun0", run=lambda c: (0, self.LINE)),
                         "10.10.16.153")

    def test_failures(self):
        self.assertIsNone(netinfo.interface_ipv4("tun0", run=lambda c: (1, "")))
        self.assertIsNone(netinfo.interface_ipv4("tun0", run=lambda c: None))
        self.assertIsNone(netinfo.interface_ipv4("tun0", run=lambda c: (0, "no inet here")))

    def test_rejects_odd_device_names(self):
        called = []
        run = lambda c: called.append(c) or (0, self.LINE)
        self.assertIsNone(netinfo.interface_ipv4("tun0; rm -rf", run=run))
        self.assertIsNone(netinfo.interface_ipv4("", run=run))
        self.assertEqual(called, [])


class Streaming(unittest.TestCase):
    def py(self, code):
        return [sys.executable, "-c", code]

    def test_collects_and_echoes(self):
        out = io.StringIO()
        rc, text = runner.run_streaming(self.py("print('hello'); print('bye')"),
                                        echo=True, out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(text, "hello\nbye\n")
        self.assertEqual(out.getvalue(), "hello\nbye\n")

    def test_silent_by_default_and_merges_stderr(self):
        out = io.StringIO()
        rc, text = runner.run_streaming(
            self.py("import sys; sys.stderr.write('oops'); sys.exit(3)"), out=out)
        self.assertEqual((rc, text, out.getvalue()), (3, "oops", ""))

    def test_echo_strips_escape_sequences(self):
        out = io.StringIO()
        runner.run_streaming(self.py("print('a\\x1b[31mb')"), echo=True, out=out)
        self.assertNotIn("\x1b", out.getvalue())

    def test_timeout_kills_process(self):
        rc, _ = runner.run_streaming(self.py("import time; time.sleep(30)"), timeout=1)
        self.assertEqual(rc, runner.TIMED_OUT)

    def test_missing_command(self):
        self.assertIsNone(runner.run_streaming(["/no/such/binary-xyz"]))


if __name__ == "__main__":
    unittest.main()
