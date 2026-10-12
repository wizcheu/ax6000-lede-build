"""Verified downloads and bounded extraction of firmware build inputs."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(url, dest, expected=None):
    """Replace dest only after a complete HTTPS download and optional hash check."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not url.startswith("https://"):
        raise ValueError("Source downloads require HTTPS")
    if expected is not None and not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Invalid SHA256")
    with tempfile.TemporaryDirectory(dir=dest.parent) as temp:
        candidate = Path(temp) / "download"
        subprocess.run([
            "curl", "--fail", "--location", "--proto", "=https", "--proto-redir", "=https",
            "--retry", "5", "--connect-timeout", "30", "--speed-limit", "1024",
            "--speed-time", "60", "--max-time", "600", "--retry-max-time", "180",
            "--silent", "--show-error", "--output", str(candidate), url,
        ], check=True)
        if expected is not None and sha256(candidate) != expected:
            raise ValueError(f"SHA256 mismatch: {url}")
        candidate.replace(dest)
    return dest


def fetch_text(url):
    with tempfile.TemporaryDirectory() as temp:
        return fetch(url, Path(temp) / "text").read_text()


def locked_download(source, cache, suffix):
    digest = source["sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid locked SHA256")
    dest = Path(cache) / (digest + suffix)
    if not dest.is_file() or sha256(dest) != digest:
        fetch(source["url"], dest, digest)
    return dest


def write_json(path, value):
    """Atomically publish a snapshot; failures leave the previous file intact."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as temp:
        candidate = Path(temp) / "snapshot.json"
        candidate.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        candidate.replace(path)


def package_version(makefile):
    match = re.search(r"^PKG_VERSION\s*:?=\s*([\w.+-]+)\s*$", makefile, re.M)
    if not match:
        raise ValueError("Cannot resolve literal OpenClash PKG_VERSION")
    return match[1]


def extract_core(archive, dest):
    dest = Path(dest)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if (len(members) != 1 or members[0].name != "clash" or
                not members[0].isfile() or not 20 <= members[0].size <= 100 * 1024**2):
            raise ValueError("Unexpected core archive contents")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tar.extractfile(members[0]) as src, dest.open("wb") as dst:
            shutil.copyfileobj(src, dst)
    with dest.open("rb") as stream:
        elf = stream.read(20)
    if elf[:6] != b"\x7fELF\x02\x01" or int.from_bytes(elf[18:20], "little") != 183:
        raise ValueError("Core is not an AArch64 ELF executable")
    dest.chmod(0o755)
    return sha256(dest)


def extract_dashboard(archive, dest):
    """Accept only bounded regular files under dist/, never ZIP paths or links."""
    dest = Path(dest)
    with zipfile.ZipFile(archive) as zipped:
        entries = zipped.infolist()
        if len(entries) > 1000 or sum(x.file_size for x in entries) > 50 * 1024**2:
            raise ValueError("Dashboard archive is unexpectedly large")
        seen = set()
        for entry in entries:
            name = entry.filename
            parts = name.rstrip("/").split("/")
            kind = stat.S_IFMT(entry.external_attr >> 16)
            if (parts[0] != "dist" or any(x in ("", ".", "..") for x in parts) or
                    "\\" in name or entry.flag_bits & 1 or name in seen or
                    kind not in (0, stat.S_IFREG, stat.S_IFDIR) or
                    (len(parts) == 1 and not entry.is_dir())):
                raise ValueError(f"Unsafe dashboard archive entry: {name}")
            seen.add(name)
        for entry in entries:
            target = dest.joinpath(*PurePosixPath(entry.filename).parts[1:])
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(entry) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                target.chmod(0o644)
    index = (dest / "index.html").read_text()
    assets = re.findall(r'(?:src|href)="\./([^"?#]+)"', index)
    if not any(x.startswith("assets/") and x.endswith(".js") for x in assets):
        raise ValueError("Dashboard HTML has no application entry point")
    for asset in assets:
        path = PurePosixPath(asset)
        if path.is_absolute() or ".." in path.parts or not (dest / path).is_file():
            raise ValueError(f"Missing or invalid dashboard asset: {asset}")
    return {p.relative_to(dest).as_posix(): sha256(p) for p in sorted(dest.rglob("*")) if p.is_file()}
