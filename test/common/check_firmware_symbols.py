#!/usr/bin/env python3
"""Verify every PROTO_T_* and function a firmware uses is actually declared.

The host tests compile proto.c and agent.c, but not main.c, uart_link.c or
ble_scan.c -- those need Zephyr headers. So a missing #define in main.c survives
`make -C tests` and only fails at `west build`, which is a slower loop and often
on someone else's machine.

This is a cheap static substitute: extract the identifiers each .c file uses and
confirm each is declared in a header it includes.

    python3 test/common/check_firmware_symbols.py test/loopback-uart-ble-b/firmware
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Provided by Zephyr, libc, or the compiler -- not our headers' job to declare.
EXTERNAL = re.compile(r"^(k_|bt_|uart_|ring_buf_|net_buf_|sys_|z_|atomic_|LOG_|"
                      r"CONFIG_|BT_|UART_|K_|ARG_UNUSED|IS_ENABLED|memcpy|memset|"
                      r"snprintf|printf|strlen|abs|fabs|sqrt|sin|rand|SYS_)")


def declared_symbols(src: Path) -> set[str]:
    """Everything our headers define or declare."""
    out: set[str] = set()
    for h in src.glob("*.h"):
        text = h.read_text()
        out |= set(re.findall(r"^\s*#define\s+([A-Za-z_]\w*)", text, re.M))
        # prototypes: `int foo(...);`
        out |= set(re.findall(r"^\s*(?:extern\s+)?[\w\s*]+?\b(\w+)\s*\([^;{]*\)\s*;",
                              text, re.M))
        # definitions in the header: `static inline uint16_t foo(...)\n{`
        out |= set(re.findall(r"^\s*static\s+inline\s+[\w\s*]+?\b(\w+)\s*\([^;{]*\)\s*\{",
                              text, re.M | re.S))
        out |= set(re.findall(r"^\s*(?:typedef\s+)?(?:struct|enum|union)\s+(\w+)",
                              text, re.M))
        out |= set(re.findall(r"^\s*(\w+)\s*[,=]", text, re.M))     # enum members
    return out


def used_symbols(path: Path) -> set[str]:
    text = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    used = set(re.findall(r"\b(PROTO_[A-Z0-9_]+)\b", text))
    used |= set(re.findall(r"\b(ble_\w+|uart_link_\w+|agent_\w+|proto_\w+)\s*\(", text))
    return used


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    fw = Path(argv[1])
    src = fw / "src"
    if not src.is_dir():
        print(f"no such directory: {src}", file=sys.stderr)
        return 2

    declared = declared_symbols(src)
    # Definitions in .c files count as declared for their own translation unit.
    for c in src.glob("*.c"):
        declared |= set(re.findall(r"^\s*(?:static\s+)?[\w\s*]+?\b(\w+)\s*\([^;{]*\)\s*\{",
                                   c.read_text(), re.M))

    failures = 0
    for c in sorted(src.glob("*.c")):
        missing = sorted(s for s in used_symbols(c)
                         if s not in declared and not EXTERNAL.match(s))
        if missing:
            failures += len(missing)
            print(f"  {c.name}: undeclared {missing}")

    if failures:
        print(f"\n{failures} undeclared symbol(s) -- these are `west build` errors")
        return 1
    print(f"  {fw.name}: all PROTO_* and module symbols declared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
