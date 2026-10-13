"""Verified downloads and bounded extraction of firmware build inputs."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from urllib.parse import urlsplit

THEMES = ("mono", "aeris")


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
        auth = []
        token = os.environ.get("GITHUB_TOKEN")
        if urlsplit(url).hostname == "api.github.com" and token:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", token):
                raise ValueError("Invalid GitHub API token format")
            # Keep the token out of process arguments and exception messages.
            config = Path(temp) / "github-auth.conf"
            config.write_text(f'header = "Authorization: Bearer {token}"\n')
            config.chmod(0o600)
            auth = ["--config", str(config)]
        subprocess.run([
            "curl", "--fail", "--location", "--proto", "=https", "--proto-redir", "=https",
            "--retry", "5", "--connect-timeout", "30", "--speed-limit", "1024",
            "--speed-time", "60", "--max-time", "600", "--retry-max-time", "180",
            "--silent", "--show-error", *auth, "--output", str(candidate), url,
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
        raise ValueError("Cannot resolve literal package PKG_VERSION")
    return match[1]


def extract_theme(archive, dest, theme, version):
    """Validate the release source archive before copying its package inputs."""
    if theme not in THEMES:
        raise ValueError(f"Unsupported firmware theme: {theme}")
    dest = Path(dest)
    prefix = f"luci-theme-{theme}"
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if len(members) > 2000 or sum(m.size for m in members) > 25 * 1024**2:
            raise ValueError(f"{theme} source archive is unexpectedly large")
        seen = set()
        files = {}
        for member in members:
            parts = member.name.rstrip("/").split("/")
            if (parts[0] != prefix or any(p in ("", ".", "..") for p in parts) or
                    "\\" in member.name or member.name in seen or
                    not (member.isfile() or member.isdir())):
                raise ValueError(f"Unsafe {theme} archive entry: {member.name}")
            seen.add(member.name)
            if member.isfile():
                files["/".join(parts[1:])] = member
        required = {"Makefile", "LICENSE", "NOTICE", f"htdocs/luci-static/{theme}/{theme}.css",
                    f"ucode/template/themes/{theme}/header.ut", f"ucode/template/themes/{theme}/footer.ut",
                    f"ucode/template/themes/{theme}/sysauth.ut",
                    f"root/etc/uci-defaults/30_luci-theme-{theme}"}
        if not required <= files.keys():
            raise ValueError(f"{theme} package inputs missing: {required - files.keys()}")
        makefile = tar.extractfile(files["Makefile"]).read().decode()
        if (package_version(makefile) != version or
                not re.search(rf"^PKG_NAME:={prefix}$", makefile, re.M)):
            raise ValueError(f"{theme} source version/name does not match release")
        # Preview pages, tests and repository metadata are not build inputs.
        for name, member in files.items():
            if name not in {"Makefile", "LICENSE", "NOTICE"} and not name.startswith(("htdocs/", "ucode/", "root/")):
                continue
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(member).read())
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    # LuCI adds version queries to templates; verify the untransformed runtime
    # assets and registration script against source hashes in the final image.
    hashes = {}
    for source, target in (("htdocs", "www"), ("root", "")):
        for path in sorted((dest / source).rglob("*")):
            if path.is_file():
                name = "/".join(filter(None, (target, path.relative_to(dest / source).as_posix())))
                hashes[name] = sha256(path)
    return hashes


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


def extract_tailcat(archive, dest, license_dest=None):
    """Accept the official three-file archive and a static AArch64 executable."""
    dest = Path(dest)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if (len(members) != 3 or {m.name for m in members} != {"tailcat", "LICENSE", "README.md"} or
                any(not m.isfile() or not 0 < m.size <= (100 if m.name == "tailcat" else 2) * 1024**2
                    for m in members)):
            raise ValueError("Unexpected Tailcat archive contents")
        binary = tar.extractfile("tailcat").read()
        if (len(binary) < 64 or binary[:6] != b"\x7fELF\x02\x01" or
                int.from_bytes(binary[18:20], "little") != 183):
            raise ValueError("Tailcat is not an AArch64 ELF executable")
        phoff = int.from_bytes(binary[32:40], "little")
        phsize = int.from_bytes(binary[54:56], "little")
        phnum = int.from_bytes(binary[56:58], "little")
        if not (phoff >= 64 and phsize == 56 and 0 < phnum < 128 and
                phoff + phsize * phnum <= len(binary)):
            raise ValueError("Invalid Tailcat ELF program headers")
        if any(int.from_bytes(binary[p:p + 4], "little") in (2, 3)
               for p in range(phoff, phoff + phsize * phnum, phsize)):
            raise ValueError("Tailcat must be statically linked")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(binary)
        dest.chmod(0o755)
        if license_dest is not None:
            license_dest = Path(license_dest)
            license_dest.parent.mkdir(parents=True, exist_ok=True)
            license_dest.write_bytes(tar.extractfile("LICENSE").read())
            license_dest.chmod(0o644)
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
