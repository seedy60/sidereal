"""Locate and preload the native TeamTalk SDK library, cross-platform.

Order of preference:
  1. ``_tt_vendor/TeamTalk_DLL/``   (the wrapper- and ABI-matched SDK pair,
     installed by tools/fetch_sdk.py — works on every OS)
  2. ``$TT_SDK_DIR``                (explicit user override pointing at a
     TeamTalk SDK "Library/TeamTalk_DLL" directory, or any dir holding the lib)
  3. Well-known system installs (TeamTalk client app on Windows, brew on
     macOS, distro/lib paths on Linux).

On Windows the library is loaded explicitly by absolute path via
``ctypes.WinDLL`` (an ``add_dll_directory`` alone can lose to PATH and make the
OS silently load the TeamTalk *client* app's DLL, which may be a different
build). On POSIX the directory is registered on DYLD/DLL-style paths and the
library is preloaded with ``ctypes.CDLL`` so the wrapper's later
``cdll.LoadLibrary`` reuses the same handle instead of re-resolving.
"""
import ctypes
import os
import platform
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDOR_DIR = os.path.join(_HERE, "_tt_vendor", "TeamTalk_DLL")

# Library file name per platform (build arch is decided at download time).
if sys.platform == "win32":
    _LIB_NAMES = ("TeamTalk5.dll",)
elif sys.platform == "darwin":
    _LIB_NAMES = ("libTeamTalk5.dylib",)
else:
    _LIB_NAMES = ("libTeamTalk5.so",)

_WIN_FALLBACK_DIRS = (
    r"C:\Program Files\TeamTalk5",
    r"C:\Program Files (x86)\TeamTalk5",
)

# POSIX system dirs that hold the native library (checked with the lib name).
_POSIX_FALLBACK_DIRS = (
    "/usr/local/lib",
    "/usr/lib",
    "/opt/homebrew/lib",
)

_resolved = None  # (lib_path, lib_dir) or None


def _first_file(dirs, names):
    for d in dirs:
        if not d:
            continue
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p, d
    return None


def _arch_suffix():
    """Platform tag used in tt5sdk_{version}_{platform}.7z archives."""
    m = platform.machine().lower()
    if sys.platform == "win32":
        # Native Windows-on-ARM runs x64 DLLs only under emulation; the SDK
        # doesn't ship an ARM64 build, so require real AMD64.
        if m == "amd64":
            return "win64"
        raise SystemExit("Unsupported Windows architecture: %s" % (m or "unknown"))
    if sys.platform == "darwin":
        raise SystemExit(
            "macOS: the TeamTalk SDK has no supported dylib for this Python "
            "(upstream wrapper marks Darwin unsupported); use Linux or Windows."
        )
    if m in ("x86_64", "amd64"):
        return "ubuntu22_x86_64"
    if m in ("aarch64", "arm64"):
        return "raspbian_arm64"
    raise SystemExit("Unsupported architecture: %s" % (m or "unknown"))


def resolve():
    """Return (lib_path, lib_dir) for the best available native library.

    Raises SystemExit with an actionable message when nothing usable exists.
    """
    global _resolved
    if _resolved is not None:
        return _resolved

    candidates = []  # (source, lib_path, lib_dir)

    vendor = _first_file([_VENDOR_DIR], _LIB_NAMES)
    if vendor:
        candidates.append(("vendored",) + vendor)

    env_dir = os.environ.get("TT_SDK_DIR")
    if env_dir:
        found = _first_file([env_dir], _LIB_NAMES)
        if found:
            candidates.append(("TT_SDK_DIR",) + found)

    if sys.platform == "win32":
        found = _first_file(_WIN_FALLBACK_DIRS, _LIB_NAMES)
        if found:
            candidates.append(("system",) + found)
    else:
        found = _first_file(_POSIX_FALLBACK_DIRS, _LIB_NAMES)
        if found:
            candidates.append(("system",) + found)

    if not candidates:
        raise SystemExit(
            "TeamTalk5 native library not found.\n"
            "  Fix: run  uv run python tools/fetch_sdk.py  to download and\n"
            "  vendor the matching SDK into _tt_vendor/TeamTalk_DLL, or set\n"
            "  TT_SDK_DIR to a directory containing " + ", ".join(_LIB_NAMES) + "."
        )

    source, lib_path, lib_dir = candidates[0]
    _resolved = (lib_path, lib_dir)
    return _resolved


def load(verbose=False):
    """Preload the native library so the ctypes wrapper reuses this handle.

    Returns (lib_path, source). Raises SystemExit when no library resolves and
    OSError when the library exists but fails to load (bad arch, missing
    runtime deps, etc.).
    """
    lib_path, lib_dir = resolve()
    source = None
    if lib_dir == _VENDOR_DIR:
        source = "vendored"
    elif os.environ.get("TT_SDK_DIR") and os.path.abspath(lib_dir) == os.path.abspath(os.environ["TT_SDK_DIR"]):
        source = "TT_SDK_DIR"
    else:
        source = "system"

    if sys.platform == "win32":
        ctypes.WinDLL(lib_path)
    else:
        # Register the directory first so dependent libs in the same tree
        # resolve; the preload makes the wrapper's later LoadLibrary reuse it.
        if sys.platform == "darwin":
            existing = os.environ.get("DYLD_LIBRARY_PATH")
            os.environ["DYLD_LIBRARY_PATH"] = (
                lib_dir if not existing else lib_dir + os.pathsep + existing
            )
        else:
            existing = os.environ.get("LD_LIBRARY_PATH")
            os.environ["LD_LIBRARY_PATH"] = (
                lib_dir if not existing else lib_dir + os.pathsep + existing
            )
        ctypes.CDLL(lib_path)

    if verbose:
        print("Loaded %s from %s (%s)" % (os.path.basename(lib_path), lib_path, source))
    return lib_path, source


def platform_tag():
    """SDK archive platform tag for this machine, e.g. 'win64'."""
    return _arch_suffix()


if __name__ == "__main__":
    # Diagnostic: print what would load without importing the wrapper.
    try:
        p, d = resolve()
        print("Resolved: %s" % p)
        print("Dir:      %s" % d)
    except SystemExit as e:
        print(e)
        sys.exit(1)
