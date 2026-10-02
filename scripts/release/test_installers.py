"""Exercise generated installers against small local HTTP release fixtures."""

from collections import Counter
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import threading
import unittest
import zipfile

import installers

COMMIT = 'abcdef12' + '0' * 32
TAG = 'build-4-abcdef12'


class InstallerChecks(unittest.TestCase):
    def setUp(self):
        state = installers.ROOT / '.csgopen/installer-tests'
        state.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=state)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.assets = self.root / 'assets'
        self.assets.mkdir()
        self.requests = Counter()
        self.corrupt = set()
        owner = self

        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                filename = self.path.rsplit('/', 1)[-1]
                owner.requests[filename] += 1
                if filename in owner.corrupt:
                    body = b'corrupted download'
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    super().do_GET()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(self.assets)))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.generated = self.root / 'generated'
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        installers.generate(self.generated, 'agea/eclipse-recoil', TAG, COMMIT, '4', self.base)
        self.destination = self.root / 'folder with spaces'

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def fixture(self, platform, arch, split):
        name = f'eclipse-recoil-{platform}-{arch}'
        archive = f'{name}.tar.gz' if platform == 'linux' else f'{name}.zip'
        if platform == 'macos':
            launcher = 'Eclipse Recoil.app/Contents/MacOS/EclipseRecoil'
        else:
            launcher = f'{name}/' + ('Eclipse Recoil.bat' if platform == 'windows' else 'eclipse-recoil.sh')
        body = b'fixture launcher\n' * 100
        stream = io.BytesIO()
        if platform == 'linux':
            with tarfile.open(fileobj=stream, mode='w:gz') as output:
                info = tarfile.TarInfo(launcher)
                info.size = len(body)
                info.mode = 0o755
                output.addfile(info, io.BytesIO(body))
        else:
            with zipfile.ZipFile(stream, 'w') as output:
                output.writestr(launcher, body)
        data = stream.getvalue()
        files = []
        if split:
            size = max(1, len(data) // 3)
            for offset in range(0, len(data), size):
                file = f'{archive}.{len(files) + 1:03d}'
                (self.assets / file).write_bytes(data[offset:offset + size])
                files.append(file)
            suffixes = ('ps1', 'bat') if platform == 'windows' else ('command',) if platform == 'macos' else ('sh',)
            for suffix in suffixes:
                file = f'{name}-extract.{suffix}'
                (self.assets / file).write_bytes(b'legacy helper fixture\n')
                files.append(file)
        else:
            files.append(archive)
            (self.assets / archive).write_bytes(data)
        manifest = self.assets / f'{name}.files.sha256'
        manifest.write_bytes(''.join(f'{hashlib.sha256((self.assets / file).read_bytes()).hexdigest()}  {file}\n'
                                    for file in files).encode())
        return archive, manifest, launcher, files

    def run_installer(self, platform='linux', arch='x86_64', silicon=False):
        environment = dict(os.environ)
        if platform == 'windows':
            shell = shutil.which('powershell') or shutil.which('pwsh')
            if os.name != 'nt' or not shell:
                self.skipTest('Requires native Windows PowerShell and tar.exe')
            environment['PROCESSOR_ARCHITECTURE'] = 'AMD64'
            environment.pop('PROCESSOR_ARCHITEW6432', None)
            command = [shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                       str(self.generated / 'eclipse-recoil-install.ps1'), '-Destination', str(self.destination)]
        else:
            if os.name == 'nt':
                self.skipTest('Native Bash installer check runs on macOS/Linux')
            if platform == 'macos' and not shutil.which('ditto'):
                self.skipTest('Requires macOS ditto for native app extraction')
            fakebin = self.root / 'fakebin'
            fakebin.mkdir(exist_ok=True)
            uname = fakebin / 'uname'
            uname.write_text(f'#!/bin/sh\nif [ "$1" = -s ]; then echo {"Darwin" if platform == "macos" else "Linux"}; else echo {arch}; fi\n')
            uname.chmod(0o755)
            sysctl = fakebin / 'sysctl'
            sysctl.write_text(f'#!/bin/sh\necho {1 if silicon else 0}\n')
            sysctl.chmod(0o755)
            if not shutil.which('sha256sum'):
                checksum = fakebin / 'sha256sum'
                checksum.write_text('#!/bin/sh\nexec shasum -a 256 "$@"\n')
                checksum.chmod(0o755)
            environment['PATH'] = str(fakebin) + os.pathsep + environment['PATH']
            command = ['/bin/bash', str(self.generated / 'eclipse-recoil-install.sh'), str(self.destination)]
        return subprocess.run(command, env=environment, capture_output=True, text=True, timeout=60)

    def assert_installed(self, platform, arch, split, detection_arch=None, silicon=False):
        _, _, launcher, _ = self.fixture(platform, arch, split)
        result = self.run_installer(platform, detection_arch or arch, silicon)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        target = self.destination / 'game' / launcher
        self.assertEqual(target.read_bytes(), b'fixture launcher\n' * 100)
        self.assertFalse((self.destination / '.downloads').exists())
        self.assertFalse((self.destination / '.extracting').exists())
        self.assertIn('Ready!', result.stdout)

    def test_linux_x86_split_archive(self):
        self.assert_installed('linux', 'x86_64', True)

    def test_linux_arm_single_archive(self):
        self.assert_installed('linux', 'arm64', False, detection_arch='aarch64')

    def test_macos_arm_split_archive_from_rosetta_terminal(self):
        self.assert_installed('macos', 'arm64', True, detection_arch='x86_64', silicon=True)

    def test_macos_intel_single_archive(self):
        self.assert_installed('macos', 'x86_64', False)

    def test_windows_split_archive(self):
        self.assert_installed('windows', 'x86_64', True)

    def test_windows_single_archive(self):
        self.assert_installed('windows', 'x86_64', False)

    def native_platform(self):
        return 'windows' if os.name == 'nt' else 'linux'

    def test_checksum_failure_then_retry_reuses_verified_parts(self):
        platform = self.native_platform()
        _, _, launcher, files = self.fixture(platform, 'x86_64', True)
        self.corrupt.add(files[1])
        result = self.run_installer(platform)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Checksum mismatch', result.stdout + result.stderr)
        self.assertFalse((self.destination / 'game').exists())
        self.assertFalse((self.destination / '.extracting').exists())
        self.corrupt.clear()
        result = self.run_installer(platform)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.destination / 'game' / launcher).is_file())
        self.assertEqual(self.requests[files[0]], 1)
        self.assertEqual(self.requests[files[1]], 2)

    def test_existing_installation_is_preserved(self):
        platform = self.native_platform()
        self.fixture(platform, 'x86_64', False)
        game = self.destination / 'game'
        game.mkdir(parents=True)
        sentinel = game / 'sentinel'
        sentinel.write_text('keep me')
        result = self.run_installer(platform)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_text(), 'keep me')
        self.assertFalse(self.requests)

    def test_manifest_rejects_path_traversal_before_any_archive_download(self):
        platform = self.native_platform()
        _, manifest, _, _ = self.fixture(platform, 'x86_64', False)
        manifest.write_text('0' * 64 + '  ../outside.zip\n')
        result = self.run_installer(platform)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unexpected file', result.stdout + result.stderr)
        self.assertEqual(sum(self.requests.values()), 1)

    def test_manifest_rejects_missing_split_part(self):
        platform = self.native_platform()
        _, manifest, _, _ = self.fixture(platform, 'x86_64', True)
        manifest.write_text('\n'.join(manifest.read_text().splitlines()[1:]) + '\n')
        result = self.run_installer(platform)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Missing or unordered', result.stdout + result.stderr)
        self.assertEqual(sum(self.requests.values()), 1)

    def test_failed_extraction_leaves_no_game_or_staging(self):
        platform = self.native_platform()
        archive, manifest, _, _ = self.fixture(platform, 'x86_64', False)
        invalid = b'not an archive'
        (self.assets / archive).write_bytes(invalid)
        manifest.write_text(hashlib.sha256(invalid).hexdigest() + f'  {archive}\n')
        result = self.run_installer(platform)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.destination / 'game').exists())
        self.assertFalse((self.destination / '.extracting').exists())

    def test_unsupported_architecture_stops_before_download(self):
        if os.name == 'nt':
            self.skipTest('Bash architecture check')
        result = self.run_installer('linux', 'i686')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unsupported architecture', result.stderr)
        self.assertFalse(self.requests)

    def test_generation_pins_release_and_notes(self):
        installers.generate(self.generated, 'agea/eclipse-recoil', TAG, COMMIT, '4')
        for suffix in ('sh', 'ps1'):
            script = (self.generated / f'eclipse-recoil-install.{suffix}').read_text()
            self.assertIn(f'/releases/download/{TAG}', script)
            self.assertNotIn('@BASE_URL@', script)
            self.assertNotIn('/latest/', script)
        notes = (self.generated / 'release-notes.md').read_text()
        self.assertIn('Install Build 4', notes)
        self.assertIn('bash eclipse-recoil-install.sh', notes)
        self.assertIn('powershell -NoProfile', notes)
        self.assertIn(f'/blob/{COMMIT}/doc/csgopen/releases.md', notes)

    def test_generation_rejects_mismatched_or_unsafe_metadata(self):
        for repository, tag, commit, build in (
                ('agea/eclipse-recoil', TAG, COMMIT, '5'),
                ("agea/repo'", TAG, COMMIT, '4'),
                ('agea/eclipse-recoil', "tag'", COMMIT, '4')):
            with self.subTest(repository=repository, tag=tag, build=build), self.assertRaises(ValueError):
                installers.generate(self.generated, repository, tag, commit, build)


if __name__ == '__main__':
    unittest.main()
