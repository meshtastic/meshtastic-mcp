# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Resolves the firmware repo root and the binaries we invoke.

Everything that needs a path (the firmware root, `pio`, `esptool`, etc.) goes
through this module so the rest of the package never calls `shutil.which` or
parses environment variables directly.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterable
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when a required path or binary cannot be resolved."""


def firmware_root() -> Path:
    """Resolve the root of the Meshtastic firmware repo.

    Resolution order:
      1. `MESHTASTIC_FIRMWARE_ROOT` env var.
      2. Walk up from `cwd` looking for a directory with `platformio.ini`.
    """
    env = os.environ.get("MESHTASTIC_FIRMWARE_ROOT")
    if env:
        root = Path(env).expanduser().resolve()
        if not (root / "platformio.ini").is_file():
            raise ConfigError(f"MESHTASTIC_FIRMWARE_ROOT={env!r} does not contain platformio.ini")
        return root

    cur = Path.cwd().resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / "platformio.ini").is_file():
            return candidate
    raise ConfigError(
        "Could not locate Meshtastic firmware root. Set MESHTASTIC_FIRMWARE_ROOT "
        "to the directory containing platformio.ini."
    )


def firmware_root_or_none() -> Path | None:
    """Like `firmware_root()` but returns None instead of raising.

    Used for capability detection: the firmware build/flash tools register only
    when a firmware tree is present. The portable core never needs this.
    """
    try:
        return firmware_root()
    except ConfigError:
        return None


def _first_existing(paths: Iterable[Path]) -> Path | None:
    for p in paths:
        if p and p.is_file() and os.access(p, os.X_OK):
            return p
    return None


def pio_bin() -> Path:
    """Resolve the `pio` binary.

    Order: MESHTASTIC_PIO_BIN → ~/.platformio/penv/bin/pio (PlatformIO keeps
    this one current) → `pio` on PATH → `platformio` on PATH.
    """
    env = os.environ.get("MESHTASTIC_PIO_BIN")
    if env:
        p = Path(env).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return p
        raise ConfigError(f"MESHTASTIC_PIO_BIN={env!r} is not an executable file")

    penv = Path.home() / ".platformio" / "penv" / "bin" / "pio"
    if penv.is_file() and os.access(penv, os.X_OK):
        return penv

    for name in ("pio", "platformio"):
        w = shutil.which(name)
        if w:
            return Path(w)

    raise ConfigError(
        "Could not find `pio`. Install PlatformIO (https://platformio.org/install/cli) "
        "or set MESHTASTIC_PIO_BIN."
    )


def _pio_penv_python() -> Path | None:
    """Return the PlatformIO virtualenv python, if it exists."""
    p = Path.home() / ".platformio" / "penv" / "bin" / "python"
    return p if p.is_file() and os.access(p, os.X_OK) else None


def _ensure_wrapper(name: str, cmd: str) -> Path:
    """Write a thin shell wrapper to the MCP data dir and return its path.

    Used when a tool is available via a specific Python interpreter but not
    on the system PATH (e.g. PlatformIO's bundled esptool).
    """
    from platformdirs import user_data_dir

    data_root = os.environ.get("MESHTASTIC_MCP_DATA_DIR") or user_data_dir("meshtastic-mcp")
    bin_dir = Path(data_root) / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / name
    wrapper.write_text(f'#!/bin/sh\nexec {cmd} "$@"\n')
    wrapper.chmod(0o755)
    return wrapper


def _hw_tool(
    env_var: str,
    names: tuple[str, ...],
    install_hint: str,
    pio_pkg: str | None = None,
) -> Path:
    """Shared resolver for esptool / nrfutil / picotool.

    Resolution order:
      1. Explicit env var override (MESHTASTIC_ESPTOOL_BIN etc.)
      2. Firmware repo .venv/bin/<name>, then the running interpreter's own bin
         dir (so `pip install <tool>` into the active venv just works)
      3. System PATH
      4. PlatformIO penv: `python -m <module>` wrapper (esptool only)
      5. PlatformIO package dir `~/.platformio/packages/<pio_pkg>/<name>`
    """
    env = os.environ.get(env_var)
    if env:
        p = Path(env).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return p
        raise ConfigError(f"{env_var}={env!r} is not an executable file")

    bin_dirs: list[Path] = []
    try:
        bin_dirs.append(firmware_root() / ".venv" / "bin")
    except ConfigError:
        pass
    bin_dirs.append(Path(sys.executable).parent)  # the venv running the harness

    for bin_dir in bin_dirs:
        for name in names:
            p = bin_dir / name
            if p.is_file() and os.access(p, os.X_OK):
                return p

    for name in names:
        w = shutil.which(name)
        if w:
            return Path(w)

    # Last resort: PlatformIO ships esptool in its own penv. Detect and
    # materialise a thin wrapper so callers get a plain executable path.
    pio_py = _pio_penv_python()
    if pio_py is not None:
        for name in names:
            module = name.replace(".py", "").replace("-", "_")
            try:
                import subprocess

                result = subprocess.run(
                    [str(pio_py), "-m", module, "version"],
                    capture_output=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    return _ensure_wrapper(names[0], f"{pio_py} -m {module}")
            except Exception:
                pass

    if pio_pkg:
        pio_dir = Path.home() / ".platformio" / "packages" / pio_pkg
        for name in names:
            p = pio_dir / name
            if p.is_file() and os.access(p, os.X_OK):
                return p

    raise ConfigError(
        f"Could not find `{names[0]}`. {install_hint} Or set {env_var} to an absolute path."
    )


def esptool_bin() -> Path:
    return _hw_tool(
        "MESHTASTIC_ESPTOOL_BIN",
        ("esptool", "esptool.py"),
        "Install via `pip install esptool`.",
        pio_pkg="tool-esptoolpy",
    )


def nrfutil_bin() -> Path:
    return _hw_tool(
        "MESHTASTIC_NRFUTIL_BIN",
        ("nrfutil", "adafruit-nrfutil"),
        "Install via `pip install adafruit-nrfutil` or download Nordic nRF Util.",
    )


def picotool_bin() -> Path:
    return _hw_tool(
        "MESHTASTIC_PICOTOOL_BIN",
        ("picotool",),
        "Install via `brew install picotool` or build from https://github.com/raspberrypi/picotool.",
    )


def android_root() -> Path:
    """Resolve the Meshtastic-Android source root (MESHTASTIC_ANDROID_ROOT).

    Unlike firmware_root() there is no auto-discovery fallback — the Android
    source tree has no unique sentinel visible from cwd. Set the env var or
    run `meshtastic-mcp provision` to clone one.
    """
    env = os.environ.get("MESHTASTIC_ANDROID_ROOT")
    if env:
        root = Path(env).expanduser().resolve()
        if not (root / "gradlew").is_file():
            raise ConfigError(f"MESHTASTIC_ANDROID_ROOT={env!r} does not contain gradlew")
        return root
    raise ConfigError(
        "MESHTASTIC_ANDROID_ROOT is not set. Point it at a Meshtastic-Android "
        "checkout or run `meshtastic-mcp provision` to clone one."
    )


def android_root_or_none() -> Path | None:
    """Like `android_root()` but returns None instead of raising."""
    try:
        return android_root()
    except ConfigError:
        return None


def apple_root() -> Path:
    """Resolve the Meshtastic-Apple source root (MESHTASTIC_APPLE_ROOT).

    Set the env var to an existing Meshtastic-Apple checkout (the directory
    that contains Meshtastic.xcworkspace), or run `meshtastic-mcp provision`
    to clone one.
    """
    env = os.environ.get("MESHTASTIC_APPLE_ROOT")
    if env:
        root = Path(env).expanduser().resolve()
        if not (root / "Meshtastic.xcworkspace").is_dir():
            raise ConfigError(
                f"MESHTASTIC_APPLE_ROOT={env!r} does not contain Meshtastic.xcworkspace"
            )
        return root
    raise ConfigError(
        "MESHTASTIC_APPLE_ROOT is not set. Point it at a Meshtastic-Apple "
        "checkout or run `meshtastic-mcp provision` to clone one."
    )


def apple_root_or_none() -> Path | None:
    """Like `apple_root()` but returns None instead of raising."""
    try:
        return apple_root()
    except ConfigError:
        return None


def uhubctl_bin() -> Path:
    return _hw_tool(
        "MESHTASTIC_UHUBCTL_BIN",
        ("uhubctl",),
        "Install via `brew install uhubctl` (macOS) or `apt install uhubctl` "
        "(Debian/Ubuntu). On Linux without the udev rules, or on older macOS "
        "with certain hubs, you may need to run via `sudo`: "
        "https://github.com/mvp/uhubctl#linux-usb-permissions",
    )
