#!/bin/sh
# Codemod run script (EXAMPLE_SPEC.md §4): argv[1] = one lib/vauth/*.c file.
# curl carries no .clang-tidy, so the check set is pinned here: a single
# mechanical, fixit-capable check appropriate for C.
set -eu
unit="$1"
"$(dirname "$0")/configure.sh"
# Non-zero exit just means diagnostics were emitted; the postmod compile is
# the verification that counts.
run-clang-tidy -quiet -p build \
  -checks='-*,readability-braces-around-statements' \
  -fix "^$PWD/$unit\$" || true
