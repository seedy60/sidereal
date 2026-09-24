#!/usr/bin/env python3
"""Download and vendor the TeamTalk SDK into _tt_vendor/ (cross-platform).

Adapted from seedy60/cider's tools/ttsdk_downloader.py (MIT):
https://github.com/seedy60/cider

What it does:
  1. Scrapes https://bearware.dk/teamtalksdk for the newest vNN.MM folder.
  2. Downloads tt5sdk_{version}_{platform}.7z for this OS/arch.
  3. Extracts it and moves Library/TeamTalk_DLL + Library/TeamTalkPy into
     _tt_vendor/ (the layout tt_sdk.py and the wrapper expect).
  4. Prints the SDK license notice and optionally keeps the archive.

bearware.dk sits behind an HTTP-454 JavaScript "browser check" (a SHA-256
proof of work); this script solves it the same way a browser would and reuses
the returned clearance cookie. Requires `requests` and `py7zr` (both project
dependencies).

Usage:
    uv run python tools/fetch_sdk.py            # install/refresh vendored SDK
    uv run python tools/fetch_sdk.py --platform win64   # force a platform tag
"""
import argparse
import hashlib
import os
import platform
import re
import shutil
import sys
import tempfile
from urllib.parse import urlsplit

import py7zr
import requests

# curl_cffi replicates a real browser's TLS fingerprint. bearware.dk's WAF
# (Simply.com) tar-pits non-browser TLS handshakes: a *valid* PoW solution POSTed
# from a python-requests fingerprint just hangs, while garbage gets an instant
# 454. Impersonating Chrome makes the verification handshake indistinguishable
# from a real browser and lets the download proceed.
try:
    from curl_cffi import requests as _curl_requests
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False

BASE_URL = "https://bearware.dk/teamtalksdk"
VENDOR_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_tt_vendor")
ARCHIVE_NAME = "ttsdk.7z"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


def get_url_suffix_from_platform(platform_tag=None) -> str:
    """Map this machine (or an explicit tag) to the SDK archive platform suffix."""
    if platform_tag:
        return platform_tag
    machine = platform.machine().lower()
    if sys.platform == "win32":
        if machine in ("amd64", "x86_64"):
            return "win64"
        if machine in ("arm64", "aarch64"):
            raise SystemExit("Native Windows on ARM is not supported by the SDK.")
        return "win32"
    if sys.platform == "darwin":
        raise SystemExit(
            "macOS: the SDK's dylib is not usable from Python (upstream wrapper "
            "marks Darwin unsupported); use Linux or Windows."
        )
    if machine in ("amd64", "x86_64"):
        return "ubuntu22_x86_64"
    if machine in ("aarch64", "arm64"):
        return "raspbian_arm64"
    raise SystemExit("Your architecture (%s) is not supported" % (machine or "unknown"))


def _leading_zero_bits(digest_hex: str) -> int:
    bits = 0
    for char in digest_hex:
        value = int(char, 16)
        if value == 0:
            bits += 4
        else:
            bits += 3 if value < 2 else 2 if value < 4 else 1 if value < 8 else 0
        break
    return bits


def _solve_challenge(session: requests.Session, response: requests.Response) -> bool:
    """Solve bearware.dk's proof-of-work firewall and store the clearance cookie.

    The check is a SHA-256 PoW: find a nonce whose hash of "<token>:<nonce>"
    has D leading zero bits, POST it to /.sc-verify/, and reuse the returned
    clearance cookie. Returns True when a challenge was solved.
    """
    challenge = re.search(r'var T="([0-9a-f]+)",TS="(\d+)",D=(\d+);', response.text)
    if not challenge:
        return False
    token, timestamp, difficulty = challenge.group(1), challenge.group(2), int(challenge.group(3))
    print("Solving bearware.dk proof-of-work (difficulty %d bits)..." % difficulty, flush=True)
    nonce = 0
    while _leading_zero_bits(hashlib.sha256(f"{token}:{nonce}".encode()).hexdigest()) < difficulty:
        nonce += 1
    parts = urlsplit(response.url)
    verify = session.post(
        f"{parts.scheme}://{parts.netloc}/.sc-verify/",
        data={"ts": timestamp, "nonce": str(nonce), "token": token},
        timeout=120,  # server verification can take a while on some networks
        headers={"Referer": response.url, "Origin": f"{parts.scheme}://{parts.netloc}"},
    )
    clearance = verify.json().get("cookie") if verify.ok else None
    if not clearance:
        return False
    session.cookies.set("sc_clearance", clearance, domain=parts.netloc)
    return True


def _get(session: requests.Session, target: str, **kwargs) -> requests.Response:
    """GET that transparently clears bearware.dk's firewall (HTTP 454) and retries."""
    kwargs.setdefault("timeout", (15, 300))
    response = session.get(target, **kwargs)
    if response.status_code == 454 and _solve_challenge(session, response):
        response.close()
        response = session.get(target, **kwargs)
    return response


def create_session():
    if _HAS_CURL_CFFI:
        # impersonate="chrome" sets the TLS/JA3+HTTP2 fingerprint AND a matching
        # Chrome User-Agent, so every request looks like a real browser.
        return _curl_requests.Session(impersonate="chrome")
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    return session


def newest_version(session: requests.Session) -> str:
    r = _get(session, BASE_URL)
    r.raise_for_status()
    # The listing page is plain HTML with <li><a href="v5.22/">…</a></li> links.
    versions = re.findall(r'href=["\']?(v\d+\.\d+[^"\']*/?)["\']?', r.text)
    if not versions:
        raise SystemExit("Could not find any TeamTalk SDK versions at " + BASE_URL)

    def version_key(name):
        m = re.match(r"v(\d+)\.(\d+)([a-z]*)", name.rstrip("/"))
        return (int(m.group(1)), int(m.group(2)), m.group(3))

    return max(versions, key=version_key).rstrip("/")


def download(session: requests.Session, version: str, plat: str, dest: str) -> str:
    url = f"{BASE_URL}/{version}/tt5sdk_{version}_{plat}.7z"
    print("Downloading from " + url, flush=True)
    response = _get(session, url, stream=True, timeout=(15, 300))
    try:
        response.raise_for_status()
        total = 0
        with open(dest, "wb") as archive:
            for chunk in response.iter_content(chunk_size=65536):
                archive.write(chunk)
                total += len(chunk)
    finally:
        response.close()
    size_mb = total / (1024 * 1024)
    print("Downloaded %.1f MB -> %s" % (size_mb, dest), flush=True)
    return dest


def _reapply_dll_guard() -> None:
    """Re-apply our isdir guard to the freshly installed upstream wrapper.

    The SDK ships TeamTalk5.py with an unguarded add_dll_directory() call that
    raises FileNotFoundError when TeamTalk_DLL is absent (e.g. system-install
    setups). Patch it back in so vendored == upstream + our one fix.
    """
    wrapper = os.path.join(VENDOR_DIR, "TeamTalkPy", "TeamTalk5.py")
    if not os.path.isfile(wrapper):
        return
    with open(wrapper, "r", encoding="utf-8", newline="") as f:
        src = f.read()
    eol = "\r\n" if "\r\n" in src else "\n"
    old = eol.join([
        "        # Path relative to TeamTalk SDK's DLL location",
        '        os.add_dll_directory(os.path.dirname(os.path.abspath(__file__)) + "\\\\..\\\\TeamTalk_DLL")',
    ])
    new = eol.join([
        "        # Path relative to TeamTalk SDK's DLL location. Only added when it",
        "        # actually exists -- add_dll_directory() raises FileNotFoundError",
        "        # otherwise (e.g. when this wrapper is vendored outside an SDK tree;",
        "        # the app registers the real dll dir itself before importing).",
        "        _sdk_dll_dir = os.path.abspath(",
        '            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "TeamTalk_DLL"))',
        "        if os.path.isdir(_sdk_dll_dir):",
        "            os.add_dll_directory(_sdk_dll_dir)",
    ])
    if old not in src:
        print("NOTE: upstream wrapper already has the isdir guard (or changed shape)", flush=True)
        return
    with open(wrapper, "w", encoding="utf-8", newline="") as f:
        f.write(src.replace(old, new, 1))
    print("Re-applied isdir guard to _tt_vendor/TeamTalkPy/TeamTalk5.py", flush=True)


def _rmtree_retry(path: str, attempts: int = 6) -> None:
    """rmtree that tolerates transient Windows locks (e.g. a loaded DLL)."""
    import stat
    import time

    def _onexc(func, p, exc):
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        func(p)

    for i in range(attempts):
        try:
            shutil.rmtree(path, onexc=_onexc)
            return
        except PermissionError:
            if i == attempts - 1:
                raise SystemExit(
                    "Could not replace %s -- it is locked by a running process.\n"
                    "Close the bot (and any TeamTalk app using the SDK DLL), then re-run."
                    % path)
            time.sleep(1.0)


def extract_and_install(archive_path: str, keep: bool) -> None:
    os.makedirs(VENDOR_DIR, exist_ok=True)
    staging = os.path.join(tempfile.mkdtemp(prefix="ttsdk_"), "x")
    print("Extracting...", flush=True)
    with py7zr.SevenZipFile(archive_path, mode="r") as z:
        z.extractall(staging)

    # The archive contains a single top-level folder (e.g. tt5sdk_v5.22a_win64).
    entries = [e for e in os.listdir(staging) if not e.startswith(".")]
    if len(entries) != 1 or not os.path.isdir(os.path.join(staging, entries[0])):
        raise SystemExit("Unexpected SDK archive layout: %r" % (entries,))
    sdk_root = os.path.join(staging, entries[0])
    library = os.path.join(sdk_root, "Library")
    if not os.path.isdir(library):
        raise SystemExit("No Library/ folder in extracted SDK at %s" % sdk_root)

    for name in ("TeamTalk_DLL", "TeamTalkPy"):
        src = os.path.join(library, name)
        if not os.path.isdir(src):
            raise SystemExit("Missing %s in SDK archive" % name)
        dst = os.path.join(VENDOR_DIR, name)
        if os.path.isdir(dst):
            _rmtree_retry(dst)
        shutil.move(src, dst)
        print("Installed _tt_vendor/%s" % name, flush=True)

    lic_src = os.path.join(sdk_root, "License.txt")
    if os.path.isfile(lic_src):
        lic_dst = os.path.join(VENDOR_DIR, "TTSDK_license.txt")
        shutil.copyfile(lic_src, lic_dst)
        print("Installed _tt_vendor/TTSDK_license.txt", flush=True)

    _reapply_dll_guard()

    shutil.rmtree(os.path.dirname(staging), ignore_errors=True)
    if not keep and os.path.isfile(archive_path):
        os.remove(archive_path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Download and vendor the TeamTalk SDK")
    ap.add_argument("--platform", dest="platform_tag", default=None,
                    help="force archive platform tag (win64, win32, ubuntu22_x86_64, raspbian_arm64)")
    ap.add_argument("--keep", action="store_true", help="keep the downloaded .7z archive")
    ap.add_argument("--archive", dest="archive", default=None,
                    help="install from a local .7z (e.g. downloaded manually in a browser) "
                         "instead of fetching from bearware.dk")
    args = ap.parse_args()

    print("HTTP backend: %s" % (
        "curl_cffi (browser-impersonating)" if _HAS_CURL_CFFI else "requests (plain)"), flush=True)
    if args.archive:
        if not os.path.isfile(args.archive):
            raise SystemExit("Archive not found: %s" % args.archive)
        print("Installing from local archive: %s" % args.archive, flush=True)
        extract_and_install(os.path.abspath(args.archive), keep=True)
    else:
        plat = get_url_suffix_from_platform(args.platform_tag)
        print("Platform tag: %s" % plat, flush=True)
        session = create_session()
        version = newest_version(session)
        print("Newest SDK version: %s" % version, flush=True)
        archive = os.path.join(os.getcwd(), ARCHIVE_NAME)
        download(session, version, plat, archive)
        extract_and_install(archive, args.keep)

    # Sanity check: the vendored pair should now resolve.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import tt_sdk
    try:
        lib_path, _ = tt_sdk.resolve()
        print("OK: native library resolves to %s" % lib_path, flush=True)
    except SystemExit as e:
        print("WARNING: vendored install did not resolve: %s" % e, flush=True)
        sys.exit(1)

    lic = os.path.join(VENDOR_DIR, "TTSDK_license.txt")
    if os.path.isfile(lic):
        with open(lic, "r", errors="replace") as f:
            print("\n--- SDK license notice (full text: %s) ---" % lic, flush=True)
            print(f.read(2000), flush=True)


if __name__ == "__main__":
    main()
