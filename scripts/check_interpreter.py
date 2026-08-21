#!/usr/bin/env python3
"""Is this interpreter complete, and does it match the other hosts'?

Run it *with the interpreter under test*:

    /usr/local/bin/python3.11 scripts/check_interpreter.py
    .venv/bin/python3 scripts/check_interpreter.py --fingerprint

CPython's configure step skips any optional extension module whose development
library is absent, prints the list once, and **succeeds**. So a source build looks
fine and fails later at an import that has nothing to do with the missing library:
pi1's was `No module named '_bz2'`, raised from `networkx`.

This checks positively instead -- it imports every optional extension and names the
Debian package that supplies each missing one, so the whole set is known before a
run rather than one module at a time.

`--fingerprint` prints a one-line digest of the available module set. Two hosts in
one experiment should produce the same digest; a difference is a difference between
the machines being compared, and nothing in the collected data would show it.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import platform
import sys

#: Optional CPython extension modules and the Debian dev package that enables each.
#: `vertex` marks the ones this platform actually needs -- the rest are worth
#: having for consistency between hosts, but their absence will not stop a run.
OPTIONAL = [
    # module,        apt package,               needed by vertex
    ("_bz2",         "libbz2-dev",              "networkx"),
    ("_lzma",        "liblzma-dev",             "networkx"),
    ("_ctypes",      "libffi-dev",              "vertex.radio.hci (sockaddr_hci)"),
    ("zlib",         "zlib1g-dev",              "pip, wheels"),
    ("_ssl",         "libssl-dev",              "pip over https"),
    ("_hashlib",     "libssl-dev",              "pip, hashlib"),
    ("_socket",      "(always built)",          "the whole control plane"),
    ("_sqlite3",     "libsqlite3-dev",          ""),
    ("readline",     "libreadline-dev",         ""),
    ("_curses",      "libncurses-dev",          ""),
    ("_uuid",        "uuid-dev",                ""),
    ("_dbm",         "libgdbm-dev",             ""),
    ("_gdbm",        "libgdbm-compat-dev",      ""),
    ("_tkinter",     "tk-dev",                  ""),
    ("_decimal",     "libmpdec-dev (bundled)",  ""),
]


def probe() -> list[tuple[str, str, str, bool]]:
    out = []
    for name, pkg, needed in OPTIONAL:
        try:
            importlib.import_module(name)
            ok = True
        except Exception:
            ok = False
        out.append((name, pkg, needed, ok))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fingerprint", action="store_true",
                    help="print only a digest of the available module set")
    args = ap.parse_args()

    results = probe()
    present = sorted(n for n, _, _, ok in results if ok)
    digest = hashlib.sha256(",".join(present).encode()).hexdigest()[:12]

    if args.fingerprint:
        print(f"{platform.python_version()} {digest} {len(present)}/{len(results)}")
        return 0

    print(f"  interpreter  {sys.executable}")
    print(f"  version      {platform.python_version()} "
          f"({' '.join(platform.python_build())})")
    print(f"  prefix       {sys.prefix}")
    if sys.prefix != sys.base_prefix:
        print(f"  base         {sys.base_prefix}   <- venv")
    print()

    missing_needed, missing_other = [], []
    for name, pkg, needed, ok in results:
        mark = "ok  " if ok else "MISS"
        tag = f"  <- {needed}" if needed and not ok else ""
        print(f"  {mark} {name:<12} {pkg}{tag}")
        if not ok:
            (missing_needed if needed else missing_other).append((name, pkg))

    print()
    print(f"  module set   {len(present)}/{len(results)} present, digest {digest}")
    print("               compare this digest between hosts; they should match")

    if missing_needed:
        pkgs = sorted({p for _, p in missing_needed if p.startswith("lib") or p.endswith("-dev")})
        print()
        print("  FAIL these are needed by vertex and are absent:")
        for name, pkg in missing_needed:
            print(f"    {name:<12} install {pkg}, then rebuild the interpreter")
        if pkgs:
            print(f"\n    sudo apt install {' '.join(pkgs)}")
        print("    Installing the package is not enough on its own -- CPython only")
        print("    picks up an extension at configure time, so the build must be redone.")
    if missing_other:
        print()
        print("  these are absent but vertex does not use them; they still make this")
        print("  host differ from one where they are present:")
        print("    " + ", ".join(n for n, _ in missing_other))

    return 1 if missing_needed else 0


if __name__ == "__main__":
    sys.exit(main())
