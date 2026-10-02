#!/bin/bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
STATE="$ROOT/.csgopen"
SOURCE=${1:-}
VPK=${2:-}
SCALE=${3:-0.25}
DISPLACEMENT_LOD=${4:-2}
PROP_TRIANGLE_BUDGET=${5:-300000}
BLENDER_BIN=${BLENDER_BIN:-/Applications/Blender.app/Contents/MacOS/Blender}
PLUMBER_DIR=${PLUMBER_DIR:-$STATE/tools/plumber111/plumber}
SOURCE_PROPS=${CSGOPEN_SOURCE_PROPS:-1}

fail() { echo "Eclipse Recoil Source BSP converter: $*" >&2; exit 1; }

[[ -n "$SOURCE" ]] || fail "usage: $0 /path/to/map.bsp [pak01_dir.vpk] [scale] [displacement-lod] [prop-triangle-budget]"
[[ -f "$SOURCE" ]] || fail "source not found: $SOURCE"
[[ "$SOURCE" == *.bsp ]] || fail "source must be a .bsp file"
[[ -x "$ROOT/src/eclipse-recoil_native" ]] || fail "build the client first: scripts/csgopen/dev.sh build"

if [[ -z "$VPK" ]]; then
    CANDIDATE=$(cd "$(dirname "$SOURCE")/.." && pwd)/pak01_dir.vpk
    if [[ -f "$CANDIDATE" ]]; then VPK=$CANDIDATE; fi
fi
[[ -z "$VPK" || -f "$VPK" ]] || fail "VPK directory not found: $VPK"

MAP=$(basename "$SOURCE" .bsp)
mkdir -p "$STATE/map-convert" "$STATE/logs"
STAGE=$(mktemp -d "$STATE/map-convert/$MAP-source.XXXXXX")
PROFILE="$STAGE/profile"
PACKAGE="$STAGE/data"
LOG="$STATE/logs/sourceimport-$MAP.log"
PROP_LOG="$STATE/logs/sourceprops-$MAP.log"

PROP_MANIFEST=""
if [[ "$SOURCE_PROPS" != 0 ]]; then
    [[ -n "$VPK" ]] || fail "static props require pak01_dir.vpk (or set CSGOPEN_SOURCE_PROPS=0)"
    [[ -x "$BLENDER_BIN" ]] || fail "Blender not found at $BLENDER_BIN (set BLENDER_BIN or CSGOPEN_SOURCE_PROPS=0)"
    [[ -f "$PLUMBER_DIR/plumber.abi3.so" ]] || fail "Plumber addon not found at $PLUMBER_DIR (set PLUMBER_DIR or CSGOPEN_SOURCE_PROPS=0)"
    "$BLENDER_BIN" --background --factory-startup \
        --python "$ROOT/scripts/csgopen/sourceprops_blender.py" -- \
        --bsp "$SOURCE" --vpk "$VPK" --stage "$STAGE" \
        --plumber "$PLUMBER_DIR" --triangle-budget "$PROP_TRIANGLE_BUDGET" \
        --scale "$SCALE" >"$PROP_LOG" 2>&1
    PROP_MANIFEST="$STAGE/$MAP-props.json"
    [[ -f "$PROP_MANIFEST" ]] || fail "static-prop manifest missing; inspect $PROP_LOG"
fi

CONVERT=(python3 "$ROOT/scripts/csgopen/sourcebsp.py" --scale "$SCALE" --displacement-lod "$DISPLACEMENT_LOD")
if [[ -n "$VPK" ]]; then CONVERT+=(--vpk "$VPK"); fi
if [[ -n "$PROP_MANIFEST" ]]; then CONVERT+=(--props-manifest "$PROP_MANIFEST"); fi
CONVERT+=("$SOURCE" "$STAGE")
"${CONVERT[@]}"

cd "$ROOT"
"$ROOT/src/eclipse-recoil_native" \
    "-h$PROFILE" \
    "-p$PACKAGE" \
    "-g$LOG" \
    -sm -ss0 -dw640 -dh480 -df0 \
    '-xexec "build-map.cfg"'

rg -q "SOURCEIMPORT_DONE $MAP" "$LOG" || fail "conversion marker missing; inspect $LOG"
[[ -f "$PROFILE/maps/$MAP.mpz" ]] || fail "generated MPZ missing"
cp "$PROFILE/maps/$MAP.mpz" "$PACKAGE/maps/$MAP.mpz"
cp "$PROFILE/maps/$MAP.cfg" "$PACKAGE/maps/$MAP.cfg"
if [[ -f "$PROFILE/maps/$MAP.png" ]]; then cp "$PROFILE/maps/$MAP.png" "$PACKAGE/maps/$MAP.png"; fi

echo "Converted Source BSP package: $PACKAGE"
echo "Manifest: $STAGE/$MAP.json"
echo "Generated MPZ: $PACKAGE/maps/$MAP.mpz"
echo "Log: $LOG"
if [[ -n "$PROP_MANIFEST" ]]; then echo "Static-prop log: $PROP_LOG"; fi
echo "Play with:"
echo "  $ROOT/src/eclipse-recoil_native -h$PROFILE -p$PACKAGE -bconfig/csgopen/branding.cfg -sm -ss0 -dw1280 -dh720 -df0 '-xexec \"play.cfg\"'"
