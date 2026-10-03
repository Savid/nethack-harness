#!/usr/bin/env python3
"""Build nethack-harness.pyz, a single-file zipapp, reproducibly.

    python3 tools/build_pyz.py --commit SHA --out dist/nethack-harness.pyz

The same sources and commit always give the same bytes: files are added in sorted order with a fixed
timestamp and permissions, and the commit is stamped into nethack_harness/_commit.py inside the archive.
"""
import argparse
import os
import stat
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAMP = (1980, 1, 1, 0, 0, 0)
MAIN = (
    "import sys\n"
    "from nethack_harness.control import main\n"
    "sys.exit(main() or 0)\n"
)


def sources():
    pkg = os.path.join(ROOT, "nethack_harness")
    for name in sorted(os.listdir(pkg)):
        if name.endswith(".py") and name != "_commit.py":
            yield "nethack_harness/" + name, os.path.join(pkg, name)


def add(zf, name, data):
    info = zipfile.ZipInfo(name, STAMP)
    info.compress_type = zipfile.ZIP_STORED      # no compressor version can change the bytes
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.create_system = 3
    zf.writestr(info, data)


def build(out, commit):
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "wb") as f:
        f.write(b"#!/usr/bin/env python3\n")
        with zipfile.ZipFile(f, "w") as zf:
            add(zf, "__main__.py", MAIN.encode())
            for name, path in sources():
                with open(path, "rb") as src:
                    add(zf, name, src.read())
            add(zf, "nethack_harness/_commit.py", ("COMMIT = %r\n" % commit).encode())
            add(zf, "LICENSE", open(os.path.join(ROOT, "LICENSE"), "rb").read())
    os.chmod(tmp, 0o755)
    os.replace(tmp, out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--commit", required=True, help="the commit the sources come from")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist", "nethack-harness.pyz"))
    a = ap.parse_args()
    build(a.out, a.commit.strip()[:40])
    print(a.out)


if __name__ == "__main__":
    sys.exit(main())
