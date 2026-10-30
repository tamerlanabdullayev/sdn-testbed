#!/usr/bin/env python3
"""
code_hash.py: one SHA-256 over the testbed's code, recorded by every run
(traffic_meta.json, controller_meta.json, summary column code_hash), so runs
made with different code can be told apart.

    scripts/code_hash.py           # print the hash
    scripts/code_hash.py --list    # print the files it covers (one per line)

Covered: every file under controller/ (Python apps, adapters, the ONOS app's
pom.xml and src/), topology/, scripts/ and analysis/, plus the built ONOS app
controller/onos-instrumented-l2/target/instrumented-l2-1.0.0.oar (what
ONOS runs). Excluded: __pycache__/, *.pyc, the rest of target/. The hash is
SHA-256 over "<relative path>\\0<SHA-256 of the file>\\n" for the files in
sorted order, so it changes when any covered file is added, removed, renamed
or edited, and not when only timestamps change. Standard library only.
"""

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE_DIRS = ("controller", "topology", "scripts", "analysis")
ONOS_TARGET = Path("controller/onos-instrumented-l2/target")
ONOS_OAR = ONOS_TARGET / "instrumented-l2-1.0.0.oar"


def code_files(root=ROOT):
    root = Path(root)
    files = []
    for d in CODE_DIRS:
        for p in (root / d).rglob("*"):
            rel = p.relative_to(root)
            if (not p.is_file() or "__pycache__" in rel.parts or p.suffix == ".pyc"
                    or (ONOS_TARGET in rel.parents and rel != ONOS_OAR)):
                continue
            files.append(rel)
    return sorted(files, key=lambda r: r.as_posix())


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def code_hash(root=ROOT):
    h = hashlib.sha256()
    for rel in code_files(root):
        h.update(f"{rel.as_posix()}\0{file_sha256(Path(root) / rel)}\n".encode())
    return h.hexdigest()


if __name__ == "__main__":
    if "--list" in sys.argv[1:]:
        print("\n".join(r.as_posix() for r in code_files()))
    else:
        print(code_hash())
