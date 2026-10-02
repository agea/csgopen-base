#!/bin/bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
STATE="$ROOT/.csgopen"
SOURCE=${1:-}
MAP=${2:-}
SPAWN_FLOOR=${3:-nearest}

fail() { echo "CSGOpen map converter: $*" >&2; exit 1; }

[[ -n "$SOURCE" ]] || fail "usage: $0 /path/to/map.pk3 [bsp-name] [nearest|lowest]"
[[ -f "$SOURCE" ]] || fail "source not found: $SOURCE"
[[ -x "$ROOT/src/eclipse-recoil_native" ]] || fail "build the client first: scripts/csgopen/dev.sh build"

if [[ -z "$MAP" ]]; then
    MAP=$(basename "$SOURCE")
    MAP=${MAP%.*}
fi
[[ "$MAP" =~ ^[A-Za-z0-9_-]+$ ]] || fail "invalid BSP name: $MAP"
[[ "$SPAWN_FLOOR" == nearest || "$SPAWN_FLOOR" == lowest ]] || fail "invalid spawn-floor policy: $SPAWN_FLOOR"

mkdir -p "$STATE/map-convert" "$STATE/logs"
STAGE=$(mktemp -d "$STATE/map-convert/$MAP.XXXXXX")
PROFILE="$STAGE/profile"
PACKAGE="$STAGE/data"
LOG="$STATE/logs/q3import-$MAP.log"

args=("$ROOT/scripts/csgopen/q3bsp.py" --eclipse-stage --spawn-floor "$SPAWN_FLOOR")
if [[ $# -gt 1 ]]; then args+=(--map "$MAP"); fi
args+=("$SOURCE" "$STAGE")
python3 "${args[@]}"

cd "$ROOT"
"$ROOT/src/eclipse-recoil_native" \
    "-h$PROFILE" \
    "-p$PACKAGE" \
    "-g$LOG" \
    -sm -ss0 -dw640 -dh480 -df0 \
    '-xexec "build-map.cfg"'

rg -q "Q3IMPORT_DONE $MAP" "$LOG" || fail "conversion marker missing; inspect $LOG"
[[ -f "$PROFILE/maps/$MAP.mpz" ]] || fail "generated MPZ missing"

cp "$PROFILE/maps/$MAP.mpz" "$PACKAGE/maps/$MAP.mpz"
cp "$PROFILE/maps/$MAP.cfg" "$PACKAGE/maps/$MAP.cfg"
if [[ -f "$PROFILE/maps/$MAP.png" ]]; then cp "$PROFILE/maps/$MAP.png" "$PACKAGE/maps/$MAP.png"; fi

echo "Converted map package: $PACKAGE"
echo "Generated MPZ: $PACKAGE/maps/$MAP.mpz"
echo "Log: $LOG"
echo "Play with:"
echo "  $ROOT/src/eclipse-recoil_native -h$PROFILE -p$PACKAGE -bconfig/csgopen/branding.cfg -sm -ss0 -dw1280 -dh720 -df0 '-xexec \"play.cfg\"'"
