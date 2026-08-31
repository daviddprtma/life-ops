#!/usr/bin/env python3
"""Build and package the LifeOps Executa as a standalone per-platform binary.

Run from ``executas/lifeops``::

    uv sync --locked
    uv pip install pyinstaller
    uv run python build_binary.py                          # auto-detect this host
    uv run python build_binary.py --platform linux-x86_64  # explicit target

Output, in ``dist/``:

    tool-dev-lifeops-<version>-<platform>.tar.gz   (Unix targets)
    tool-dev-lifeops-<version>-<platform>.zip      (Windows target)
    <archive>.sha256                               (coreutils format)

Each archive holds exactly one executable at its root, named to match the
``entrypoint`` declared for that platform in ``executa.json``.  ``dist/`` is
also where ``binary_artifacts.path`` points, so ``anna-app executa
upload-binaries`` picks these up directly.

PyInstaller cannot cross-compile, and the dependency tree carries native wheels
(``pydantic-core``, ``jiter``), so each target must be built on a host of the
same OS and architecture.  The GitHub Actions workflow does exactly that, one
runner per platform.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

if sys.version_info < (3, 11):  # tomllib
    sys.exit("build_binary.py needs Python 3.11+ to run (the built binary still targets 3.10+).")

import tomllib

HERE = Path(__file__).resolve().parent
ENTRY_SCRIPT = HERE / "lifeops_plugin.py"
PYPROJECT = HERE / "pyproject.toml"
EXECUTA_JSON = HERE / "executa.json"
DIST_DIR = HERE / "dist"
BUILD_DIR = HERE / "build" / "pyinstaller"

# The four targets this project ships.  Anna's platform keys are "{os}-{arch}";
# note the documented asymmetry -- macOS and Windows ARM are spelled `arm64`,
# but Linux ARM is spelled `aarch64`.
SUPPORTED_PLATFORMS = (
    "linux-x86_64",
    "darwin-x86_64",
    "darwin-arm64",
    "windows-x86_64",
)

_OS_KEYS = {"linux": "linux", "darwin": "darwin", "win32": "windows", "cygwin": "windows"}
_ARCH_KEYS = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "x64": "x86_64",
    "arm64": "arm64",
    "aarch64": "arm64",
}


def log(message: str) -> None:
    print(f"[build] {message}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Metadata
# ─────────────────────────────────────────────────────────────────────────────


def read_metadata() -> tuple[str, str]:
    """Return (executa name, version) from pyproject.toml -- the single source of truth."""
    with PYPROJECT.open("rb") as handle:
        project = tomllib.load(handle)["project"]

    version = project["version"]

    # Artifact names must match the console script Anna launches, not the
    # PEP 503 package name (`tool-dev-lifeops` happens to be both here).
    scripts = project.get("scripts") or {}
    if len(scripts) != 1:
        sys.exit(f"expected exactly one [project.scripts] entry, found {sorted(scripts)}")
    name = next(iter(scripts))

    return name, version


def assert_version_agrees(version: str) -> None:
    """Fail if lifeops_plugin.VERSION has drifted from pyproject.toml.

    A mismatch would mislabel every archive while `describe` kept reporting the
    old number, so it is worth failing the build over.
    """
    source = ENTRY_SCRIPT.read_text(encoding="utf-8")
    for line in source.splitlines():
        if line.startswith("VERSION"):
            declared = line.split("=", 1)[1].strip().strip("\"'")
            if declared != version:
                sys.exit(
                    f"version mismatch: pyproject.toml says {version!r} but "
                    f"{ENTRY_SCRIPT.name} declares VERSION = {declared!r}"
                )
            return
    sys.exit(f"could not find a VERSION assignment in {ENTRY_SCRIPT.name}")


def detect_platform() -> str:
    os_key = _OS_KEYS.get(sys.platform)
    if os_key is None:
        sys.exit(f"unsupported host OS {sys.platform!r}")

    machine = platform.machine().lower()
    arch = _ARCH_KEYS.get(machine)
    if arch is None:
        sys.exit(f"unsupported host architecture {platform.machine()!r}")

    if os_key == "linux" and arch == "arm64":
        arch = "aarch64"  # Anna spells Linux ARM `aarch64`

    return f"{os_key}-{arch}"


def entrypoint_name(name: str, platform_key: str) -> str:
    """The executable's filename inside the archive.

    Must match the ``entrypoint`` declared for this platform in executa.json.
    """
    return f"{name}.exe" if platform_key.startswith("windows-") else name


# ─────────────────────────────────────────────────────────────────────────────
# Build
# ─────────────────────────────────────────────────────────────────────────────


def run_pyinstaller(name: str, platform_key: str) -> Path:
    """Freeze the plugin into a single executable and return its path."""
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)

    argv = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--console",
        "--name",
        name,
        "--clean",
        "--noconfirm",
        "--log-level",
        "WARN",
        "--noupx",
        "--distpath",
        str(BUILD_DIR / "dist"),
        "--workpath",
        str(BUILD_DIR / "work"),
        "--specpath",
        str(BUILD_DIR),
        # The openai SDK reads its own version through importlib.metadata, and
        # httpx2/httpcore2 import truststore conditionally -- neither survives
        # PyInstaller's static analysis unaided.
        "--copy-metadata",
        "openai",
        "--collect-submodules",
        "openai",
        "--hidden-import",
        "truststore",
    ]

    # `strip` is a binutils tool; it is absent on the Windows runners.
    if not platform_key.startswith("windows-"):
        argv.append("--strip")

    argv.append(str(ENTRY_SCRIPT))

    log(f"pyinstaller -> {platform_key}")
    subprocess.run(argv, cwd=HERE, check=True)

    built = BUILD_DIR / "dist" / entrypoint_name(name, platform_key)
    if not built.is_file():
        sys.exit(f"PyInstaller reported success but {built} is missing")

    if platform_key.startswith("darwin-"):
        _codesign_adhoc(built)

    log(f"built {built.name} ({built.stat().st_size / 1_048_576:.1f} MiB)")
    return built


def _codesign_adhoc(binary: Path) -> None:
    """Re-apply an ad-hoc signature on macOS.

    Stripping invalidates whatever signature PyInstaller applied, and an
    unsigned binary is killed outright on Apple Silicon.  Matches what Anna's
    own example build script does.  A real Developer ID signature plus
    notarisation is still needed for distribution outside Anna's installer.
    """
    result = subprocess.run(
        ["codesign", "--force", "--sign", "-", str(binary)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        log("applied ad-hoc code signature")
    else:
        log(f"WARNING: codesign failed: {result.stderr.strip()}")


def smoke_test(binary: Path) -> None:
    """Drive the built binary over stdio the way the Anna host will.

    This is what actually proves PyInstaller captured every import -- a missing
    hidden import surfaces here as a traceback rather than in production.
    """
    requests = (
        json.dumps({"jsonrpc": "2.0", "method": "describe", "id": 1}),
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "invoke",
                "id": 2,
                "params": {"tool": "ping", "arguments": {}},
            }
        ),
    )

    # The plugin builds an OpenAI client at import time and the SDK raises when
    # the key is absent, so give it a placeholder.  `describe` and `ping` make
    # no network calls, so no real credential is involved.
    env = {"OPENAI_API_KEY": "sk-smoke-test-not-a-real-key"}

    log("smoke-testing the binary (describe + ping)")
    result = subprocess.run(
        [str(binary)],
        input="\n".join(requests) + "\n",
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, **env},
    )

    frames = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                frames.append(json.loads(line))
            except json.JSONDecodeError:
                sys.exit(f"binary wrote non-JSON to stdout: {line[:200]!r}")

    if not frames:
        sys.exit(
            "binary produced no JSON-RPC output "
            f"(exit {result.returncode})\nstderr:\n{result.stderr[:2000]}"
        )

    by_id = {frame.get("id"): frame for frame in frames}

    describe = by_id.get(1, {}).get("result") or {}
    if "tools" not in describe or not describe.get("name"):
        sys.exit(f"describe did not return a manifest: {frames}")

    ping = by_id.get(2, {}).get("result") or {}
    if not (ping.get("data") or {}).get("pong"):
        sys.exit(f"ping did not return pong: {frames}")

    log(f"smoke test passed (name={describe['name']} version={describe.get('version')})")


# ─────────────────────────────────────────────────────────────────────────────
# Packaging
# ─────────────────────────────────────────────────────────────────────────────


def make_archive(binary: Path, name: str, version: str, platform_key: str) -> Path:
    """Pack the single executable at the archive root. Bare binaries are rejected by Anna."""
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{name}-{version}-{platform_key}"
    arcname = entrypoint_name(name, platform_key)

    if platform_key.startswith("windows-"):
        archive = DIST_DIR / f"{stem}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            info = zipfile.ZipInfo.from_file(binary, arcname)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o755 << 16
            with binary.open("rb") as src, zf.open(info, "w") as dst:
                shutil.copyfileobj(src, dst)
    else:
        archive = DIST_DIR / f"{stem}.tar.gz"

        def normalise(info: tarfile.TarInfo) -> tarfile.TarInfo:
            # Anna needs the executable bit; zeroing ownership keeps the
            # archive independent of whichever runner produced it.
            info.mode = 0o755
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            return info

        with tarfile.open(archive, "w:gz") as tf:
            tf.add(binary, arcname=arcname, filter=normalise)

    log(f"packaged {archive.name} (entrypoint {arcname!r} at archive root)")
    return archive


def write_checksum(archive: Path) -> Path:
    """Write a `sha256sum -c`-compatible sidecar next to the archive."""
    digest = hashlib.sha256()
    with archive.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    sidecar = archive.with_suffix(archive.suffix + ".sha256")
    # newline="" suppresses Windows CRLF translation: the release job runs
    # `sha256sum -c` on Linux, and a \r turns the filename into one that
    # cannot be opened.
    sidecar.write_text(f"{digest.hexdigest()}  {archive.name}\n", encoding="utf-8", newline="")
    log(f"wrote {sidecar.name}")
    return sidecar


def check_declared_entrypoint(platform_key: str, arcname: str) -> None:
    """Warn if executa.json disagrees with what we just packaged (goal 5)."""
    if not EXECUTA_JSON.is_file():
        return

    manifest = json.loads(EXECUTA_JSON.read_text(encoding="utf-8"))
    artifact = (manifest.get("distribution", {}).get("binary_artifacts") or {}).get(platform_key)
    if not artifact:
        log(f"WARNING: executa.json declares no binary_artifacts entry for {platform_key}")
        return

    declared = artifact.get("entrypoint")
    if declared != arcname:
        log(
            f"WARNING: executa.json declares entrypoint {declared!r} for "
            f"{platform_key}, but the archive contains {arcname!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--platform",
        choices=SUPPORTED_PLATFORMS,
        help="target platform key (default: auto-detect this host)",
    )
    parser.add_argument(
        "--skip-smoke-test",
        action="store_true",
        help="skip driving the built binary over stdio (not recommended)",
    )
    args = parser.parse_args()

    platform_key = args.platform or detect_platform()
    if platform_key not in SUPPORTED_PLATFORMS:
        sys.exit(
            f"host platform {platform_key!r} is not one of the shipped targets: "
            f"{', '.join(SUPPORTED_PLATFORMS)}"
        )

    if args.platform and args.platform != detect_platform():
        sys.exit(
            f"cannot build {args.platform} on a {detect_platform()} host -- PyInstaller "
            "does not cross-compile. Run this on a matching runner."
        )

    name, version = read_metadata()
    assert_version_agrees(version)
    log(f"{name} {version} -> {platform_key}")

    binary = run_pyinstaller(name, platform_key)
    if not args.skip_smoke_test:
        smoke_test(binary)

    archive = make_archive(binary, name, version, platform_key)
    write_checksum(archive)
    check_declared_entrypoint(platform_key, entrypoint_name(name, platform_key))

    log(f"done -> {archive.relative_to(HERE)}")


if __name__ == "__main__":
    main()
