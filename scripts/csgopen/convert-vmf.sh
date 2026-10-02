#!/bin/bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
STATE="$ROOT/.csgopen"
SOURCE=${1:-}
SCALE=${2:-0.25}

fail() { echo "CSGOpen VMF converter: $*" >&2; exit 1; }

[[ -n "$SOURCE" ]] || fail "usage: $0 /path/to/map.vmf [scale]"
[[ -f "$SOURCE" ]] || fail "source not found: $SOURCE"
[[ "$SOURCE" == *.vmf ]] || fail "source must be a .vmf file"
[[ -x "$ROOT/src/redeclipse_native" ]] || fail "build the client first: scripts/csgopen/dev.sh build"

MAP=$(basename "$SOURCE" .vmf)
MAP=${MAP%_d}
mkdir -p "$STATE/map-convert" "$STATE/logs"
STAGE=$(mktemp -d "$STATE/map-convert/$MAP.XXXXXX")
PROFILE="$STAGE/profile"
PACKAGE="$STAGE/data"
LOG="$STATE/logs/vmfimport-$MAP.log"

python3 "$ROOT/scripts/csgopen/vmf.py" --scale "$SCALE" "$SOURCE" "$STAGE"

cd "$ROOT"
"$ROOT/src/redeclipse_native" \
    "-h$PROFILE" \
    "-p$PACKAGE" \
    "-g$LOG" \
    -sm -ss0 -dw640 -dh480 -df0 \
    '-xexec "build-map.cfg"'

rg -q "VMFIMPORT_DONE $MAP" "$LOG" || fail "conversion marker missing; inspect $LOG"
[[ -f "$PROFILE/maps/$MAP.mpz" ]] || fail "generated MPZ missing"
cp "$PROFILE/maps/$MAP.mpz" "$PACKAGE/maps/$MAP.mpz"
cp "$PROFILE/maps/$MAP.cfg" "$PACKAGE/maps/$MAP.cfg"
if [[ -f "$PROFILE/maps/$MAP.png" ]]; then cp "$PROFILE/maps/$MAP.png" "$PACKAGE/maps/$MAP.png"; fi

echo "Converted VMF package: $PACKAGE"
echo "Generated MPZ: $PACKAGE/maps/$MAP.mpz"
echo "Log: $LOG"
echo "Play with:"
echo "  $ROOT/src/redeclipse_native -h$PROFILE -p$PACKAGE -bconfig/csgopen/branding.cfg -sm -ss0 -dw1280 -dh720 -df0 '-xexec \"play.cfg\"'"
