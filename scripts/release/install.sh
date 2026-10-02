#!/bin/bash
# Generated for one release; run with bash, without chmod or Finder approval.
set -euo pipefail
RELEASE='@TAG@'
BASE_URL='@BASE_URL@'
fail() { echo "Eclipse Recoil: $*" >&2; exit 1; }
[[ $# -le 1 ]] || fail 'Usage: bash eclipse-recoil-install.sh [destination]'
command -v curl >/dev/null || fail 'curl is required.'
case "$(uname -s)" in
    Darwin)
        platform=macos
        machine=$(uname -m)
        # Prefer the native Apple Silicon client even from a Rosetta terminal.
        if [[ $(sysctl -n hw.optional.arm64 2>/dev/null || true) == 1 ]]; then machine=arm64; fi
        command -v ditto >/dev/null || fail 'ditto is required.'
        command -v shasum >/dev/null || fail 'shasum is required.'
        ;;
    Linux)
        platform=linux
        machine=$(uname -m)
        command -v tar >/dev/null || fail 'tar is required.'
        command -v sha256sum >/dev/null || fail 'sha256sum is required.'
        ;;
    *) fail 'This installer supports macOS and Linux. Use the PowerShell installer on Windows.' ;;
esac
case "$machine" in
    arm64|aarch64) arch=arm64 ;;
    x86_64|amd64) arch=x86_64 ;;
    *) fail "Unsupported architecture: $machine (64-bit ARM or x86 required)." ;;
esac
name="eclipse-recoil-$platform-$arch"
archive="$name.zip"
[[ $platform != linux ]] || archive="$name.tar.gz"
destination=${1:-"$PWD/eclipse-recoil-$RELEASE-$platform-$arch"}
mkdir -p "$destination"
destination=$(cd "$destination" && pwd)
cache="$destination/.downloads"
staging="$destination/.extracting"
[[ ! -e "$destination/game" ]] || fail "Already installed: $destination/game. Choose a new destination."
[[ ! -e "$staging" ]] || fail "Temporary extraction directory exists: $staging"
mkdir -p "$cache"
cleanup() {
    rm -f "$cache"/*.partial "$cache/joined-$archive"
    rm -rf "$staging"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
fetch() {
    curl --fail --location --retry 3 --connect-timeout 30 --progress-bar \
        "$BASE_URL/$1" --output "$2.partial"
    mv "$2.partial" "$2"
}
hash() {
    if [[ $platform == macos ]]; then shasum -a 256 "$1"; else sha256sum "$1"; fi | cut -d ' ' -f 1
}
echo "Eclipse Recoil $RELEASE — $platform / $arch"
echo "Destination: $destination/game"
echo 'Downloading file list...'
manifest="$cache/$name.files.sha256"
fetch "$name.files.sha256" "$manifest"
names=() digests=() parts=()
whole=0
while IFS= read -r line || [[ -n $line ]]; do
    [[ $line =~ ^([[:xdigit:]]{64})\ \ ([^[:space:]]+)$ ]] || fail 'Invalid checksum manifest.'
    digest=$(printf '%s' "${BASH_REMATCH[1]}" | tr A-F a-f)
    file=${BASH_REMATCH[2]}
    for previous in "${names[@]-}"; do [[ $previous != "$file" ]] || fail "Duplicate file: $file"; done
    case "$file" in
        "$archive") whole=1 ;;
        "$archive".[0-9][0-9][0-9])
            expected=$(printf '%s.%03d' "$archive" "$((${#parts[@]} + 1))")
            [[ $file == "$expected" ]] || fail 'Missing or unordered archive parts.'
            parts+=("$file")
            ;;
        "$name-extract.command"|"$name-extract.sh") ;;
        *) fail "Unexpected file in manifest: $file" ;;
    esac
    names+=("$file")
    digests+=("$digest")
done < "$manifest"
if [[ $whole == 1 ]]; then
    [[ ${#parts[@]} == 0 ]] || fail 'Manifest mixes a complete archive and split parts.'
else
    [[ ${#parts[@]} -gt 0 ]] || fail 'No archive in manifest.'
fi
for ((index=0; index<${#names[@]}; index++)); do
    file=${names[index]}
    target="$cache/$file"
    if [[ -f $target && $(hash "$target") == "${digests[index]}" ]]; then
        echo "Using verified download: $file"
    else
        echo "Downloading: $file"
        fetch "$file" "$target"
        [[ $(hash "$target") == "${digests[index]}" ]] || fail "Checksum mismatch: $file. Run the installer again to retry."
    fi
done
if [[ $whole == 1 ]]; then
    source_archive="$cache/$archive"
else
    echo 'Joining archive parts...'
    source_archive="$cache/joined-$archive"
    : > "$source_archive"
    for file in "${parts[@]}"; do cat "$cache/$file" >> "$source_archive"; done
fi
echo 'Extracting game...'
mkdir "$staging"
if [[ $platform == macos ]]; then
    ditto -x -k "$source_archive" "$staging"
    launcher="$staging/Eclipse Recoil.app"
else
    tar -xzf "$source_archive" -C "$staging"
    launcher="$staging/$name/eclipse-recoil.sh"
fi
[[ -e $launcher ]] || fail 'The archive does not contain the expected game launcher.'
mv "$staging" "$destination/game"
rm -rf "$cache"
if [[ $platform == macos ]]; then
    echo "Ready! Open: $destination/game/Eclipse Recoil.app"
else
    printf 'Ready! Start the game with:\n  %q\n' "$destination/game/$name/eclipse-recoil.sh"
fi
