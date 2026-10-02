# Eclipse Recoil client releases

*Be kind, reload*

## Quick install

Starting with the next release, two small installers handle the download,
SHA-256 verification, joining split archives and extraction automatically.
You do not need to choose or download archive parts yourself.

For the **latest release**, open Terminal on **macOS or Linux** and run:

```bash
curl -fL https://github.com/agea/eclipse-recoil/releases/latest/download/eclipse-recoil-install.sh -o eclipse-recoil-install.sh && bash eclipse-recoil-install.sh
```

The same Bash script detects macOS Apple Silicon / Intel and Linux ARM64 /
x86_64. Running it with `bash` does not require `chmod` or opening a `.command`
file in Finder. Homebrew, Python and a compiler are not needed.

On **Windows x86_64**, open PowerShell and run:

```powershell
Invoke-WebRequest -UseBasicParsing -Uri https://github.com/agea/eclipse-recoil/releases/latest/download/eclipse-recoil-install.ps1 -OutFile eclipse-recoil-install.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\eclipse-recoil-install.ps1
```

The execution policy option applies only to that installer process. Windows
ARM64 and 32-bit systems are not supported by these packages.

The installer creates a folder named
`eclipse-recoil-build-<number>-<commit>-<platform>-<architecture>` in your
current directory and prints the exact game launcher path inside its `game`
folder. Open the app on macOS, run the launcher on Linux, or open the `.bat`
on Windows. To choose another destination, run
`bash eclipse-recoil-install.sh "/path/to/game"`, or on Windows add
`-Destination "C:\Games\Eclipse Recoil"` to the PowerShell command.

Each release page includes copyable commands for **that specific build**.
The downloaded installer embeds its release tag, so a newer release published
during installation cannot mix files from different builds. Re-running after
a failed download reuses files whose checksums already match. Existing game
installations are rejected rather than overwritten. Successful installation
removes the downloaded parts and temporary joined archive; no admin privileges
or automatic game launch are required. The app's macOS approval requirements
are described below.

## Packages and manual downloads

Every push to `master` runs `.github/workflows/release.yml`. Five native client
builds run independently; the release is published only after all five builds,
package checks and SHA-256 checks succeed. The workflow builds `make -C src
client`, never the dedicated-server target. The embedded local game server
remains part of the client so offline matches still work.

| Download | Runner / toolchain | Start the game |
| --- | --- | --- |
| `eclipse-recoil-macos-arm64.zip` | macOS 15, Apple Silicon / Clang | Open `Eclipse Recoil.app` |
| `eclipse-recoil-macos-x86_64.zip` | macOS 15, Intel / Clang | Open `Eclipse Recoil.app` |
| `eclipse-recoil-linux-x86_64.tar.gz` | Ubuntu 22.04 x86_64 / GCC | Run `./eclipse-recoil.sh` |
| `eclipse-recoil-linux-arm64.tar.gz` | Ubuntu 22.04 ARM64 / GCC | Run `./eclipse-recoil.sh` |
| `eclipse-recoil-windows-x86_64.zip` | Windows 2022 / MSYS2 UCRT64 GCC | Open `Eclipse Recoil.bat` |

Each package includes the game, recorded asset submodule contents,
configuration, documentation, branding and runtime
libraries. Git metadata and dedicated-server binaries are excluded. The
launcher applies Eclipse Recoil branding and starts an offline TDM match on
Echo. A `.files.sha256` manifest accompanies each target; `release.json` inside
it records the source commit, build number, architecture and included library names.

The menu, loading screens and window title display **Build N**, using the same
workflow run number embedded in the binary and recorded in `release.json`.
For example, release run 4 displays **Build 4** on every platform. The number
is assigned automatically; failed or unpublished runs may leave gaps. Local
builds without `PLATFORM_BUILD` display **Development build**. The upstream
engine version remains available as `versionstring` for diagnostics and does
not determine the release label.

For manual downloads, the complete asset set makes current archives larger
than GitHub's 2 GiB per-file limit. These archives are split into 1500 MiB parts (`.001`, `.002`,
etc.) without changing or removing game content. Download **all files for your
platform and architecture** into one directory, including its checksum manifest
and extraction helper:

- macOS: run `sh eclipse-recoil-macos-arm64-extract.command` in Terminal
  (use `x86_64` for Intel).
- Linux: run `sh eclipse-recoil-linux-x86_64-extract.sh` (use `arm64` for ARM).
- Windows: download both `-extract.bat` and `-extract.ps1`, then open the `.bat`.

The helper checks every download, joins the parts and extracts into a new
`eclipse-recoil-<platform>-<architecture>-extracted` directory. It rejects an
existing destination to avoid replacing an installation. Open the app or
launcher listed above from that directory. If a future archive fits in one
file, no helper is needed: extract the `.zip` or `.tar.gz` directly. The scripts
use operating-system tools; no separate archiver or compiler is required.

## Requirements and profiles

macOS packages are separate native apps, not a universal binary. They require
macOS 15 or newer and include relocated Homebrew libraries, including SDL3 when
Homebrew's SDL2 compatibility layer needs it. Icons are generated
from the supplied PNG. The apps are ad-hoc signed to allow the relocated code
to run, but are not Developer ID signed or notarized. A downloaded app may
require approval in macOS Privacy & Security before its first launch.

Linux packages require glibc 2.35 or newer and the matching CPU architecture.
Game libraries are included; glibc, display-server libraries and graphics
drivers come from the user's operating system. A graphical desktop and an
OpenGL 3.3-capable graphics driver are required. ARM64 means 64-bit AArch64;
32-bit ARM systems are not supported by this workflow.

Windows packages require Windows 10 or newer on Intel/AMD x86_64 and an
OpenGL 3.3-capable graphics driver. Required UCRT64 dependency DLLs are included
next to the client executable. The executable uses the supplied application
icon; the workflow generates its ICO sizes automatically.

Settings and logs stay outside the extracted installation:

- macOS: `~/Library/Application Support/Eclipse Recoil`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/eclipse-recoil`
- Windows: `%APPDATA%\Eclipse Recoil`

Set `ECLIPSE_RECOIL_HOME` to override the profile directory. The launcher manages
`localinit.cfg` and `autoexec.cfg` in that directory; normal player preferences
continue to save in the engine's configuration files. Runtime dependency
license notices are included under `licenses/runtime`; asset licenses stay
with their respective asset directories.

## Publication and manual runs

Release tags are `build-<workflow run number>-<short commit>`. A release is
created as a draft, all five packages, their parts/helpers and checksums are
uploaded together with `eclipse-recoil-install.sh` and
`eclipse-recoil-install.ps1`, and only then is it published. The publication
job generates the installers and release instructions from the release's
recorded tag, build and commit. Installer integration tests run on the build
hosts before packaging. It becomes the latest release if its commit is still
the head of `master`, so a slower older build cannot replace a newer release. Retrying
the same run reuses its draft tag and replaces draft assets; an already
published release is left intact. Each push creates a new release rather than
overwriting a rolling tag. Older releases remain available.

The workflow uses the repository's `GITHUB_TOKEN`, with write permission only
in the publication job. No personal access token or signing secrets are needed.
GitHub Actions must be enabled for the repository. Dependency installation
requires network access. Asset submodules are checked out at their recorded
commits, never at their latest remote branches.

Use **Actions → Release game client → Run workflow** for a manual build. A run
on `master` also publishes a release. Runs on other branches build downloadable
Actions artifacts without publishing. Actions artifacts expire after seven
days; release downloads remain available. Each release asset stays below
GitHub's 2 GiB limit by splitting larger archives before uploading.

## Local packaging check

After a native client build with dependencies installed:

```sh
python3 scripts/release/package.py --platform macos --arch arm64 \
  --binary src/eclipse-recoil_native --commit "$(git rev-parse HEAD)" --build 1
```

For Linux use `--platform linux`, the host's architecture and
`--binary src/redeclipse_linux`. Windows packaging runs in UCRT64 with the
same packages listed in the workflow. The packager checks executable
architecture and dependency closure and fails on unresolved libraries. Output
is placed under `.csgopen/release/`; staging files are removed after archiving
to conserve runner disk space. An existing staging directory from a failed run
is rejected to avoid silently mixing builds. Use `--output` for a fresh test
directory.

The macOS build and package can be tested locally on the development Mac.
Linux, Intel macOS and Windows execution still need their respective CI hosts
and subsequent gameplay checks; workflow syntax validation does not prove that
those native builds have succeeded.
