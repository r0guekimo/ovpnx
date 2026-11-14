import io
import logging
import tempfile
import unittest
from pathlib import Path

from ovpnx import log

PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"


class Redact(unittest.TestCase):
    def test_xml_style_blocks(self):
        out = log.redact("before <key>\nSECRET\n</key> after <TLS-CRYPT>abc</TLS-CRYPT>")
        self.assertNotIn("SECRET", out)
        self.assertNotIn("abc", out)
        self.assertIn("before", out)
        self.assertIn("after", out)

    def test_pem_complete_and_truncated(self):
        self.assertNotIn("MIIEvQ", log.redact("x " + PEM + " y"))
        self.assertNotIn("MIIEvQ", log.redact("x -----BEGIN CERTIFICATE-----\nMIIEvQ"))

    def test_unclosed_block(self):
        self.assertNotIn("HIDDEN", log.redact("start <cert> HIDDEN"))

    def test_key_value_secrets(self):
        out = log.redact("password=hunter2 token: abc123 api_key=zzz user=bob")
        for leak in ("hunter2", "abc123", "zzz"):
            self.assertNotIn(leak, out)
        self.assertIn("user=bob", out)

    def test_long_base64_but_not_paths(self):
        self.assertIn(log.REDACTED, log.redact("A" * 80))
        path = "/mnt/c/Users/HP/Desktop/VPN Profiles/HTB/machines_eu-4.ovpn"
        self.assertEqual(log.redact(path), path)

    def test_plain_text_untouched(self):
        self.assertEqual(log.redact("Connected on tun0 10.10.16.153"), "Connected on tun0 10.10.16.153")


class Output(unittest.TestCase):
    def test_clean_strips_control_chars(self):
        self.assertEqual(log.clean("a\x1b[31mb\nc\td"), "a?[31mb?c?d")

    def test_console_streams_and_no_color(self):
        out, err = io.StringIO(), io.StringIO()
        con = log.Console(out, err)
        con.ok("fine")
        con.warn("careful")
        con.err_("bad")
        self.assertEqual(out.getvalue(), "[+] fine\n")
        self.assertEqual(err.getvalue(), "[!] careful\n[-] bad\n")

    def test_console_sanitizes_filenames(self):
        out = io.StringIO()
        log.Console(out, io.StringIO()).plain("evil\x1b]0;pwned\x07.ovpn")
        self.assertNotIn("\x1b", out.getvalue())


class FileLogging(unittest.TestCase):
    def test_log_file_is_redacted_and_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = log.setup_logging(tmp)
            logger.debug("loaded password=hunter2 %s", PEM)
            for h in logger.handlers:
                h.flush()
            f = Path(tmp) / "logs" / "ovpnx.log"
            text = f.read_text()
            self.assertIn("loaded", text)
            self.assertNotIn("hunter2", text)
            self.assertNotIn("MIIEvQ", text)
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)
            for h in list(logger.handlers):
                logger.removeHandler(h)
                h.close()

    def test_unwritable_state_dir_does_not_raise(self):
        log.setup_logging("/proc/definitely/not/writable")


if __name__ == "__main__":
    unittest.main()
