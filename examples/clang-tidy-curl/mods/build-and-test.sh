#!/bin/sh
# Postmod script (EXAMPLE_SPEC.md §4): verify the modded file still compiles.
# Default: build every object compiled from the modded TU (brace fixes are
# strictly local, so that covers the change). CODEMODS_FULL_TESTS=1 builds
# the whole tree instead; curl's runtests.pl harness is deliberately not run
# (perl-heavy and network/environment sensitive).
set -eu
unit="$1"
"$(dirname "$0")/configure.sh"

if [ "${CODEMODS_FULL_TESTS:-0}" = "1" ]; then
  cmake --build build -j
  exit 0
fi

python3 - "$unit" <<'EOF'
import json, os, subprocess, sys

unit = sys.argv[1]
build = os.path.abspath("build")
entries = json.load(open(os.path.join(build, "compile_commands.json")))
target = os.path.abspath(unit)
objs = [os.path.relpath(e["output"], build)
        for e in entries if e["file"] == target]
if not objs:
    print(f"{unit} is not in the compile database; nothing to compile-check")
    sys.exit(0)
print(f"compile-checking {len(objs)} object(s) for {unit}")
sys.exit(subprocess.run(["ninja", "-C", build, *objs]).returncode)
EOF
