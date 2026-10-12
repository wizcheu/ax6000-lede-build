#!/usr/bin/env python3
"""Resolve current upstream versions, verify assets, then atomically lock the build."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import tempfile

from source_utils import (extract_core, extract_dashboard, fetch_text, locked_download,
                          package_version, write_json)

ROOT = Path(__file__).resolve().parents[1]
OPENCLASH_RAW = "https://raw.githubusercontent.com/vernesong/OpenClash"
ZASHBOARD_REPO = "Zephyruso/zashboard"


def latest(url, ref):
    result = subprocess.check_output(
        ["git", "ls-remote", url, ref], text=True, timeout=120).splitlines()
    if len(result) != 1:
        raise ValueError(f"Expected a single ref: {url} {ref}")
    sha = result[0].split()[0]
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError(f"Invalid commit for {url}")
    return sha


def resolve(lock, cache):
    # A detached copy ensures a failed update cannot partially change the caller's lock.
    lock = json.loads(json.dumps(lock))
    for source in [lock["lede"], *lock["feeds"].values(), lock["openclash"]]:
        source["commit"] = latest(source["url"], source["ref"])
    plugin = lock["openclash"]
    plugin["version"] = package_version(fetch_text(
        f"{OPENCLASH_RAW}/{plugin['commit']}/luci-app-openclash/Makefile"))

    core = lock["openclash_core"]
    core["commit"] = latest(core["repository"], core["ref"])
    base = f"{OPENCLASH_RAW}/{core['commit']}"
    # Keep the existing regular Meta Alpha channel, not the separate Smart core.
    core["path"] = "master/meta/clash-linux-arm64.tar.gz"
    core["channel"] = "OpenClash master/meta (Mihomo Alpha)"
    core["version"] = fetch_text(f"{base}/master/core_version").splitlines()[0].strip()
    if not re.fullmatch(r"[\w.+-]+", core["version"]):
        raise ValueError("Invalid Mihomo version")
    core["url"] = f"{base}/{core['path']}"
    core["checksums_url"] = f"{base}/master/meta/checksums.txt"
    checksums = fetch_text(core["checksums_url"])
    matches = re.findall(r"^([a-f0-9]{64})\s+\*?(?:\./)?clash-linux-arm64\.tar\.gz\s*$",
                         checksums, re.M)
    if len(matches) != 1:
        raise ValueError("Missing or ambiguous official core checksum")
    core["sha256"] = matches[0]

    release = json.loads(fetch_text(f"https://api.github.com/repos/{ZASHBOARD_REPO}/releases/latest"))
    tag = release["tag_name"]
    if release.get("draft") or release.get("prerelease") or not re.fullmatch(r"v?\d+\.\d+\.\d+", tag):
        raise ValueError("Expected a stable Zashboard release")
    assets = [a for a in release["assets"] if a["name"] == "dist-cdn-fonts.zip"]
    if len(assets) != 1 or not re.fullmatch(r"sha256:[a-f0-9]{64}", assets[0].get("digest") or ""):
        raise ValueError("Zashboard asset or GitHub SHA256 digest unavailable")
    asset = assets[0]
    expected_url = f"https://github.com/{ZASHBOARD_REPO}/releases/download/{tag}/{asset['name']}"
    if asset["browser_download_url"] != expected_url:
        raise ValueError("Unexpected Zashboard download URL")
    dashboard = lock["zashboard"] = {
        "repository": f"https://github.com/{ZASHBOARD_REPO}", "channel": "latest stable release",
        "tag": tag, "version": tag.removeprefix("v"), "asset": asset["name"],
        "url": expected_url, "sha256": asset["digest"].removeprefix("sha256:"),
    }
    # Validate publisher hashes and archive formats before publishing any new lock.
    with tempfile.TemporaryDirectory() as temp:
        core["binary_sha256"] = extract_core(
            locked_download(core, cache, ".tar.gz"), Path(temp) / "clash_meta")
        extract_dashboard(locked_download(dashboard, cache, ".zip"), Path(temp) / "zashboard")
    lock["schema_version"] = 2
    lock["resolved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return lock


def main():
    path = ROOT / "sources.lock.json"
    lock = resolve(json.loads(path.read_text()), ROOT / "downloads")
    write_json(path, lock)
    print(json.dumps(lock, indent=2))


if __name__ == "__main__":
    main()
