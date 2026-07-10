# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Unit tests for erase_and_flash and update_flash esptool resolution.

These functions shell out to bin/device-install.sh and bin/device-update.sh,
which in turn invoke esptool. The scripts need esptool to be available in their
subprocess PATH. This test ensures the MCP server pre-resolves esptool and
injects its directory into the subprocess environment before calling the script.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from meshtastic_mcp import config, flash, userprefs


def test_erase_and_flash_resolves_esptool_and_adds_to_path() -> None:
    """erase_and_flash must pre-resolve esptool and inject it into subprocess PATH.

    Without this fix, bin/device-install.sh would fail with 'esptool not found'
    even when esptool exists in the PlatformIO penv or other resolver paths.
    """
    captured: dict = {}

    def _stub_subprocess_run(args, **kwargs):
        captured["env"] = kwargs.get("env", {})
        captured["args"] = args
        # Return a successful result
        result = MagicMock()
        result.returncode = 0
        result.stdout = "Device erased and flashed successfully"
        result.stderr = ""
        return result

    @contextmanager
    def _stub_temporary_overrides(overrides=None):
        """Mock userprefs.temporary_overrides to avoid file I/O."""
        yield {}

    esptool_path = Path("/fake/penv/bin/esptool")
    factory_bin = Path("/fake/build/firmware.factory.bin")

    with (
        patch.object(config, "firmware_root", return_value=Path("/fake/firmware")),
        patch.object(config, "esptool_bin", return_value=esptool_path),
        patch.object(flash, "_check_esp32_env", return_value="esp32"),  # mock board check
        patch.object(flash.Path, "is_file", return_value=True),  # script exists
        patch.object(flash.subprocess, "run", side_effect=_stub_subprocess_run),
        patch.object(flash, "_factory_bin_for", return_value=factory_bin),
        patch.object(userprefs, "temporary_overrides", side_effect=_stub_temporary_overrides),
    ):
        flash.erase_and_flash("tbeam", "/dev/ttyUSB0", confirm=True, skip_build=True)

    # Verify the subprocess environment includes esptool's directory in PATH
    env = captured["env"]
    assert "PATH" in env
    assert "/fake/penv/bin" in env["PATH"]
    # esptool's dir should be at the start of PATH (prepended), using the
    # platform PATH separator (':' on POSIX, ';' on Windows).
    assert env["PATH"].startswith("/fake/penv/bin" + os.pathsep)

    # Verify the script was called with correct args
    args = captured["args"]
    assert "device-install.sh" in str(args[0])
    assert "-p" in args
    assert "/dev/ttyUSB0" in args


def test_update_flash_also_resolves_esptool() -> None:
    """update_flash must also pre-resolve esptool like erase_and_flash."""
    captured: dict = {}

    def _stub_subprocess_run(args, **kwargs):
        captured["env"] = kwargs.get("env", {})
        result = MagicMock()
        result.returncode = 0
        result.stdout = "Device OTA updated"
        result.stderr = ""
        return result

    @contextmanager
    def _stub_temporary_overrides(overrides=None):
        """Mock userprefs.temporary_overrides to avoid file I/O."""
        yield {}

    esptool_path = Path("/fake/penv/bin/esptool")
    firmware_bin = Path("/fake/build/firmware.bin")

    with (
        patch.object(config, "firmware_root", return_value=Path("/fake/firmware")),
        patch.object(config, "esptool_bin", return_value=esptool_path),
        patch.object(flash, "_check_esp32_env", return_value="esp32"),  # mock board check
        patch.object(flash.Path, "is_file", return_value=True),
        patch.object(flash.subprocess, "run", side_effect=_stub_subprocess_run),
        patch.object(flash, "_firmware_bin_for", return_value=firmware_bin),
        patch.object(userprefs, "temporary_overrides", side_effect=_stub_temporary_overrides),
    ):
        flash.update_flash("tbeam", "/dev/ttyUSB0", confirm=True, skip_build=True)

    # Verify the subprocess environment includes esptool's directory in PATH
    env = captured["env"]
    assert "PATH" in env
    assert "/fake/penv/bin" in env["PATH"]


def test_erase_and_flash_requires_confirm() -> None:
    """erase_and_flash must require confirm=True (destructive gate)."""
    with pytest.raises(flash.FlashError, match="confirm=True"):
        flash.erase_and_flash("tbeam", "/dev/ttyUSB0", confirm=False)
