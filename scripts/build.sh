#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$root/logs"
# Deliberately independent of refresh_sources: every build uses the newest
# stable Mono and Aeris releases, even when reusing a prepared work tree.
python3 "$root/scripts/prepare.py" --refresh-themes 2>&1 | tee "$root/logs/themes-update.log"
cd "$root/work/lede"
make defconfig
python3 "$root/scripts/validate.py" config
# The source archive may have changed while this build tree was idle.
make package/luci-theme-mono/clean package/luci-theme-aeris/clean
jobs="${BUILD_JOBS:-$(nproc)}"
# LEDE download.pl honors CURL_OPTIONS. Abandon stalled mirrors without
# disabling TLS or source hash checks; large downloads can still run for 20 min.
export CURL_OPTIONS="${CURL_OPTIONS:---speed-limit 1024 --speed-time 60 --max-time 1200 --retry-max-time 180 --silent --show-error}"
make download -j8 V=s 2>&1 | tee "$root/logs/download.log"
# Keep the first error and retry serially once for readable diagnostics.
if ! make -j"$jobs" V=s 2>&1 | tee "$root/logs/build.log"; then
  make -j1 V=s 2>&1 | tee "$root/logs/build-retry.log"
fi
python3 "$root/scripts/validate.py" image
