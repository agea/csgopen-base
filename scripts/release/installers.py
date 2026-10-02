#!/usr/bin/env python3
"""Generate release-pinned download installers and copyable release instructions."""

import argparse
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def generate(output, repository, tag, commit, build, download_base=None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid repository name')
    if not re.fullmatch(r'build-[0-9]+-[a-f0-9]{8}', tag):
        raise ValueError('Invalid release tag')
    if not re.fullmatch(r'[a-f0-9]{40}', commit) or not re.fullmatch(r'[0-9]+', str(build)):
        raise ValueError('Invalid release commit or build')
    if tag != f'build-{build}-{commit[:8]}':
        raise ValueError('Release tag does not match build and commit')
    base = download_base or f'https://github.com/{repository}/releases/download/{tag}'
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for extension in ('sh', 'ps1'):
        template = (ROOT / f'scripts/release/install.{extension}').read_text()
        script = template.replace('@TAG@', tag).replace('@BASE_URL@', base)
        # Keep LF on every CI host, including Windows.
        (output / f'eclipse-recoil-install.{extension}').write_bytes(script.encode('utf-8'))
    (output / 'release-notes.md').write_text(f'''Eclipse Recoil — Be kind, reload

## Install Build {build}

Copy the commands below into a terminal. The installer downloads the correct
client, checks SHA-256, joins split archives and extracts the game. You do not
need to select or download the archive parts yourself.

### macOS or Linux

Open Terminal and run:

```bash
curl -fL '{base}/eclipse-recoil-install.sh' -o eclipse-recoil-install.sh && bash eclipse-recoil-install.sh
```

The same script detects Apple Silicon / Intel on macOS and ARM64 / x86_64 on
Linux. No `chmod`, Homebrew or compiler is needed.

### Windows x86_64

Open PowerShell and run:

```powershell
Invoke-WebRequest -UseBasicParsing -Uri '{base}/eclipse-recoil-install.ps1' -OutFile eclipse-recoil-install.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\\eclipse-recoil-install.ps1
```

The execution policy option applies only to that installer process.

The game is extracted into a new folder in your current directory. The
installer prints the exact launcher path: open **Eclipse Recoil.app** on macOS,
run **eclipse-recoil.sh** on Linux, or open **Eclipse Recoil.bat** on Windows.
To retry an interrupted download, run the installer again; verified files are
reused. An existing game installation is never overwritten.

## Requirements

macOS 15+, Linux with glibc 2.35+, or Windows 10+ x86_64, with an OpenGL
3.3-capable graphics driver. macOS apps are ad-hoc signed and not notarized;
macOS may require approval in Privacy & Security when opening the game.
All game assets and runtime libraries are included. No dedicated server is included.

Manual downloads remain available under Assets. For split archives, download
all parts for your platform plus its `.files.sha256` and extraction helper.
See the [release guide](https://github.com/{repository}/blob/{commit}/doc/csgopen/releases.md)
for manual extraction, requirements and profile locations.

Commit: `{commit}`
''')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--build', required=True)
    args = parser.parse_args()
    generate(args.output, args.repository, args.tag, args.commit, args.build)
