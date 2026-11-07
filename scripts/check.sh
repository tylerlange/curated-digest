#!/usr/bin/env bash
# The one gate before pushing: `make check` runs this. Kept as a script so a
# locked-down host without make can run it directly.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
python3 -m compileall -q curated_digest tests tools/build-loop
bash -n verify.sh
./verify.sh                                              # runs the unit suite first
python3 -m unittest discover -s tools/build-loop/tests   # build-tooling tests
python3 scripts/scan_identifiers.py
