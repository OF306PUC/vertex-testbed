#!/usr/bin/env bash
# Syntax-check a firmware's C without a Zephyr toolchain.
#
#   bash test/common/syntax_check.sh test/loopback-uart-ble-b/firmware
#
# Uses crude stub headers (test/common/zstubs) whose types are minimal but whose
# NAMES are exact. That is enough for gcc to catch undeclared identifiers,
# missing declarations and use-before-declare -- the two errors that reached
# `west build` came from edits whose declarations never landed, and the
# PROTO_*-prefix symbol checker could not see a static variable.
#
# Not a build. It proves the code is well-formed, not that it links or runs.
set -uo pipefail

FW="${1:?usage: syntax_check.sh <firmware-dir>}"
STUBS="$(cd "$(dirname "${BASH_SOURCE[0]}")/zstubs" && pwd)"
SRC="$FW/src"
[ -d "$SRC" ] || { echo "no such directory: $SRC" >&2; exit 2; }

fail=0
for c in "$SRC"/*.c; do
    out=$(gcc -fsyntax-only -std=c99 -Wall -Wextra \
              -Wno-unused-parameter -Wno-unused-variable \
              -I"$STUBS" -I"$SRC" "$c" 2>&1)
    rc=$?
    errs=$(printf '%s\n' "$out" | grep -c ' error: ' || true)
    if [ "$rc" -ne 0 ] || [ "$errs" -gt 0 ]; then
        echo "  FAIL $(basename "$c")"
        printf '%s\n' "$out" | grep -E ' (error|warning): ' | sed 's/^/        /'
        fail=$((fail + 1))
    else
        warns=$(printf '%s\n' "$out" | grep -c ' warning: ' || true)
        printf '  ok   %-14s %s\n' "$(basename "$c")" \
            "$([ "$warns" -gt 0 ] && echo "($warns warning(s))" || echo "")"
        [ "$warns" -gt 0 ] && printf '%s\n' "$out" | grep ' warning: ' | sed 's/^/        /'
    fi
done

if [ "$fail" -gt 0 ]; then
    echo "  -> $fail file(s) would fail west build"
    exit 1
fi
echo "  -> $(basename "$(dirname "$FW")")/$(basename "$FW"): syntax clean"
