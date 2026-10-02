#!/usr/bin/env python3
"""Exercise native map-package HTTP serving on loopback with isolated fixtures."""

import http.client
from pathlib import Path
import socket
import subprocess
import time
import unittest
import zipfile
import zlib


ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".csgopen/server-maps-test"


def free_port(kind):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MapPackageServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = STATE / "server"
        cls.packages = STATE / "packages"
        cls.profile.mkdir(parents=True, exist_ok=True)
        cls.packages.mkdir(parents=True, exist_ok=True)
        # Include all byte values and embedded NULs across many HTTP chunks.
        cls.marker = bytes(range(256)) * 32768
        cls.archive = cls.packages / "package_fixture.zip"
        with zipfile.ZipFile(cls.archive, "w", compression=zipfile.ZIP_STORED) as archive:
            for ext in ("mpz", "cfg", "png"):
                archive.write(ROOT / f"data/maps/echo.{ext}", f"maps/package_fixture.{ext}")
            archive.writestr("csgopen/imported/package_fixture/marker.bin", cls.marker)
        cls.payload = cls.archive.read_bytes()
        cls.crc = zlib.crc32(cls.payload)
        cls.port = free_port(socket.SOCK_STREAM)
        gameport = free_port(socket.SOCK_DGRAM)
        cls.log = STATE / "server.log"
        (cls.profile / "servinit.cfg").write_text(f'''exec "config/csgopen/tdm.cfg"
sv_defaultmap ""
sv_mainmaps "package_fixture"
sv_rotatemaps 2
sv_rotatemapsfilter 0
sv_mappackages 1
sv_mappackagedir "{cls.packages}"
sv_savevars
serverip "127.0.0.1"
serverport {gameport}
serverlanport 0
servermaster ""
masterserver 0
httpserverip "127.0.0.1"
httpserverport {cls.port}
httpserver 1
''')
        cls.output = cls.log.open("w")
        cls.server = subprocess.Popen([
            str(ROOT / "src/eclipse-recoil_server_native"),
            f"-h{cls.profile}", "-si127.0.0.1", "-sm", "-ss1", f"-sp{gameport}",
        ], cwd=ROOT, stdout=cls.output, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cls.server.poll() is not None:
                cls.output.close()
                raise RuntimeError(cls.log.read_text())
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            cls.server.terminate()
            cls.server.wait(timeout=10)
            cls.output.close()
            raise RuntimeError("Native HTTP server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(timeout=10)
        cls.output.close()

    def request(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.getheader("Content-Length"), response.read()
        finally:
            connection.close()

    def test_complete_binary_package(self):
        status, length, payload = self.request(f"/map-package?name=package_fixture&crc={self.crc}")
        self.assertEqual(status, 200)
        self.assertEqual(int(length), len(self.payload))
        self.assertEqual(payload, self.payload)
        self.assertEqual(zlib.crc32(payload), self.crc)

    def test_wrong_version_is_rejected(self):
        status, _, body = self.request(f"/map-package?name=package_fixture&crc={self.crc ^ 1}")
        self.assertEqual((status, body), (404, b""))

    def test_paths_and_maps_outside_rotation_are_rejected(self):
        for name in ("../package_fixture", "%2e%2e%2fpackage_fixture", "de_bank", "package_fixture.zip"):
            with self.subTest(name=name):
                status, _, _ = self.request(f"/map-package?name={name}&crc={self.crc}")
                self.assertEqual(status, 404)

    def test_missing_name_is_rejected(self):
        status, _, _ = self.request("/map-package")
        self.assertEqual(status, 404)

    def test_package_cannot_override_config_or_escape_its_map(self):
        for bad_entry in ("config/autoexec.cfg", "maps/another_map.cfg", "csgopen/imported/package_bad/../escaped.obj"):
            with self.subTest(entry=bad_entry):
                archive = self.packages / "package_bad.zip"
                with zipfile.ZipFile(archive, "w") as package:
                    package.writestr("maps/package_bad.mpz", b"fixture")
                    package.writestr("maps/package_bad.cfg", b"// fixture")
                    package.writestr(bad_entry, b"unexpected contents")
                profile = STATE / "invalid-server"
                profile.mkdir(exist_ok=True)
                (profile / "servinit.cfg").write_text(f'''exec "config/csgopen/tdm.cfg"
sv_mainmaps "package_bad"
sv_defaultmap ""
sv_rotatemaps 2
sv_rotatemapsfilter 0
sv_mappackages 1
sv_mappackagedir "{self.packages}"
serverip "127.0.0.1"
serverport {free_port(socket.SOCK_DGRAM)}
servermaster ""
serverlanport 0
masterserver 0
httpserver 0
''')
                result = subprocess.run([
                    str(ROOT / "src/eclipse-recoil_server_native"), f"-h{profile}",
                    "-si127.0.0.1", "-sm", "-ss1",
                ], cwd=ROOT, capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid map package", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
