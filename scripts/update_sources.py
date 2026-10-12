#!/usr/bin/env python3
"""Resolve latest upstream refs, then pin all inputs before a build starts."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def latest(url, ref):
    result = subprocess.check_output(["git", "ls-remote", url, ref], text=True).splitlines()
    if len(result) != 1:
        raise SystemExit(f"Expected a single ref: {url} {ref}")
    sha = result[0].split()[0]
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise SystemExit(f"Invalid commit for {url}")
    return sha


def main():
    path = ROOT / "sources.lock.json"
    lock = json.loads(path.read_text())
    for source in [lock["lede"], *lock["feeds"].values(), lock["openclash"]]:
        source["commit"] = latest(source["url"], source["ref"])
    core = lock["openclash_core"]
    core["commit"] = latest(core["repository"], core["ref"])
    core["url"] = f"https://raw.githubusercontent.com/vernesong/OpenClash/{core['commit']}/{core['path']}"
    with tempfile.TemporaryDirectory() as temp:
        archive = Path(temp) / "core.tar.gz"
        subprocess.run(["curl", "--fail", "--location", "--retry", "5", "--connect-timeout", "30",
                        "--output", str(archive), core["url"]], check=True)
        core["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    # Write only after every ref and core download succeeded.
    path.write_text(json.dumps(lock, indent=2) + "\n")
    print(json.dumps(lock, indent=2))


if __name__ == "__main__":
    main()
