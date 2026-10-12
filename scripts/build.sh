#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$root/logs"
cd "$root/work/lede"
jobs="${BUILD_JOBS:-$(nproc)}"
make download -j8 V=s 2>&1 | tee "$root/logs/download.log"
# Keep the first error and retry serially once for readable diagnostics.
if ! make -j"$jobs" V=s 2>&1 | tee "$root/logs/build.log"; then
  make -j1 V=s 2>&1 | tee "$root/logs/build-retry.log"
fi
python3 "$root/scripts/validate.py" image
