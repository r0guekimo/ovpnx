import os
import tempfile
import unittest

from ovpnx import env


class WslDetection(unittest.TestCase):
    def test_wsl2_kernel(self):
        self.assertEqual(env.detect_wsl("5.15.133.1-microsoft-standard-WSL2", {}), (True, 2))

    def test_wsl1_kernel(self):
        self.assertEqual(env.detect_wsl("4.4.0-19041-Microsoft", {}), (True, 1))

    def test_native(self):
        self.assertEqual(env.detect_wsl("6.8.0-generic", {}), (False, None))

    def test_env_var_fallback(self):
        self.assertEqual(env.detect_wsl("custom", {"WSL_INTEROP": "/run/x"}), (True, None))


class Parsing(unittest.TestCase):
    def test_os_release(self):
        text = 'NAME="Kali GNU/Linux"\nPRETTY_NAME="Kali GNU/Linux Rolling"\n'
        self.assertEqual(env.parse_os_release(text), "Kali GNU/Linux Rolling")
        self.assertEqual(env.parse_os_release(""), "unknown distro")

    def test_versions(self):
        v3 = "OpenVPN3/Linux v27.1 (openvpn3)\nOpenVPN core 3.11.7 linux"
        self.assertEqual(env.parse_version(v3, r"OpenVPN3/Linux v?(\d+(?:\.\d+)*)"), "27.1")
        v2 = "OpenVPN 2.7.3 x86_64-pc-linux-gnu [SSL (OpenSSL)]"
        self.assertEqual(env.parse_version(v2, r"^OpenVPN (\d+(?:\.\d+)+)"), "2.7.3")
        self.assertIsNone(env.parse_version("garbage", r"^OpenVPN (\d+)"))


class Detect(unittest.TestCase):
    def _detect(self, installed, osrelease="6.8.0", wslinfo=None):
        tmp = tempfile.mkdtemp()
        rel = os.path.join(tmp, "osrelease")
        with open(rel, "w") as fh:
            fh.write(osrelease)

        def which(name):
            return "/usr/bin/" + name if name in installed else None

        def run(cmd):
            name = os.path.basename(cmd[0])
            if name == "openvpn3":
                return 0, "OpenVPN3/Linux v27.1 (openvpn3)\n"
            if name == "openvpn":
                return 1, "OpenVPN 2.7.3 x86_64-pc-linux-gnu\n"
            if name == "wslinfo":
                return 0, (wslinfo or "") + "\n"
            return None

        return env.detect_environment(
            run=run, which=which, environ={}, osrelease_path=rel,
            os_release_path=os.path.join(tmp, "none"),
            systemd_path=tmp, platform_name="linux", extra_dirs=(),
        )

    def test_native_with_both_backends(self):
        e = self._detect({"openvpn3", "openvpn", "ip", "sudo"})
        self.assertEqual(e.kind, "native Linux")
        self.assertEqual(e.backends["openvpn3"].version, "27.1")
        self.assertEqual(e.backends["openvpn2"].version, "2.7.3")
        self.assertTrue(e.systemd and e.has_ip and e.has_sudo)

    def test_wsl2_networking_mode(self):
        e = self._detect({"wslinfo"}, "5.15-microsoft-standard-WSL2", "mirrored")
        self.assertEqual(e.kind, "WSL2")
        self.assertEqual(e.wsl_networking, "mirrored")
        self.assertEqual(e.backends, {})

    def test_non_linux_flagged(self):
        e = self._detect(set())
        e.platform_name = "darwin"
        self.assertFalse(e.is_linux)
        self.assertIn("unsupported", e.kind)


if __name__ == "__main__":
    unittest.main()
