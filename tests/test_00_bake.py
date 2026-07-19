# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Session-bake module — runs first in the tier order to flash both hub roles
with the session `test_profile`.

Ordered first by `pytest_collection_modifyitems` in `conftest.py` (bucket
-1) because `baked_mesh` only *verifies* state — it does not reflash. Without
the explicit order pin, the top-level path `tests/test_00_bake.py` falls
into the fallback bucket and sorts AFTER every tier, silently turning
`--force-bake` into a no-op for the tier tests.

Skipped entirely when `--assume-baked` is passed. All downstream hardware
tests either depend on `baked_mesh` (which verifies state) or do their own
per-test bake (provisioning/fleet tiers), so failing here gives one clear
actionable failure instead of a cascade of mismatches.

Hardware-specific env names live in the per-board `BENCH_ROLES` map in
`tests/_bench.py`; override per board by setting `MESHTASTIC_MCP_ENV_<ROLE>` env
vars (e.g. `MESHTASTIC_MCP_ENV_HELTEC_T114=heltec-mesh-node-t114`).
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest

from meshtastic_mcp import admin, boards, flash, hw_tools, info, port_recovery, uhubctl

from . import _bench

# Role → PlatformIO env, from the single source of truth in tests/_bench.py.
# Override per-role via `MESHTASTIC_MCP_ENV_<ROLE>`.
_DEFAULT_ENVS = _bench.role_envs()


# Heap-status logging is gated on the firmware build flag `-DDEBUG_HEAP=1`
# (src/Power.cpp), NOT a USERPREFS key — so it rides the build, not the
# userprefs profile. Baked into the test build by default so the e2e
# mesh/message runs always capture heap telemetry; set
# `MESHTASTIC_MCP_NO_HEAP_DEBUG=1` to drop it (e.g. if an image runs tight on
# flash).
def _test_build_flags() -> dict[str, Any] | None:
    if os.environ.get("MESHTASTIC_MCP_NO_HEAP_DEBUG"):
        return None
    return {"DEBUG_HEAP": 1}


_ESP32_ARCHES = {
    "esp32",
    "esp32-s2",
    "esp32s2",
    "esp32-s3",
    "esp32s3",
    "esp32-c3",
    "esp32c3",
    "esp32-c6",
    "esp32c6",
}
_NRF52_ARCHES = {"nrf52", "nrf52840"}


def _wait_port_free(
    port: str, *, timeout_s: float = 15.0, role: str = "", unwedge: bool = True
) -> str:
    """Return a serial path that opens EXCLUSIVELY (the lock esptool/nrfutil/pio
    take), auto-unwedging the device if it's held or hung. The returned path MAY
    differ from ``port`` — a power-cycle re-enumerates the device on a (possibly)
    new ``/dev`` path, so callers MUST use the return value.

    Escalation lives in :mod:`meshtastic_mcp.port_recovery`: probe → wait →
    diagnose holder (lsof) → uhubctl power-cycle the device's own hub slot →
    re-resolve. ``unwedge=False`` keeps it a passive settle-wait (e.g. mid-erase,
    where a power-cycle would be wrong) and raises with an ``lsof`` hint on
    timeout."""
    try:
        return port_recovery.ensure_port_free(
            port, role=role, wait_s=timeout_s, allow_power_cycle=unwedge
        )
    except port_recovery.PortRecoveryError as exc:
        holders = port_recovery.who_holds_port(port)
        raise AssertionError(
            f"{exc!s}\nHolders ({port}): "
            f"{holders or 'none — a wedged device (EINVAL); needs USB re-enumerate'}. "
            f"Common culprits: a lingering `meshtastic-mcp` subprocess (.mcp.json) "
            f"or a stale `pio device monitor`."
        ) from exc


# DFU-entry rounds: each round is a touch_1200bps call (which itself retries
# the touch twice); between rounds the board's own hub slot is power-cycled.
# A fresh boot makes the Adafruit bootloader reliably receptive to the touch —
# observed on the bench: the T114 (HT-n5262) refused DFU entry two nights
# running until it was power-cycled right before the touch.
_DFU_TOUCH_ROUNDS = 3
_DFU_REENUM_TIMEOUT_S = 30.0


def _prepare_nrf52_for_upload(port: str) -> str:
    """Kick the RAK4631 (or similar nRF52 USB-DFU board) into bootloader mode
    via 1200bps touch, then return the port where pio should upload.

    Adafruit bootloader on RAK4631 interprets 1200bps-open-close as 'enter
    DFU'. The device re-enumerates with a distinct USB VID/PID
    (0x239A/0x0029) at a different `/dev/cu.usbmodem*` path.

    `touch_1200bps` does the heavy lifting: bounded open/close, polls for the
    Adafruit-bootloader PID specifically, retries the touch up to twice. A
    board whose app-mode USB stack has gone stale can ignore those touches
    entirely, so on failure the board's own hub slot is power-cycled (fresh
    boot → receptive bootloader) and the touch round repeats, up to
    ``_DFU_TOUCH_ROUNDS``. Fails loudly if the device never enters DFU — no
    point trying pio upload against an app-mode device, it'll just hang.
    """
    # Remember this board's physical hub slot before the touch. With several
    # nRF52 boards present (one may already be sitting in DFU), the global
    # bootloader scan in `touch_1200bps` can return a DIFFERENT board's
    # bootloader — so after the touch we re-pin to whatever is on THIS slot.
    hub, slot = port_recovery.hub_slot_for_port(port)
    result: dict[str, Any] = {}
    for round_no in range(1, _DFU_TOUCH_ROUNDS + 1):
        result = flash.touch_1200bps(port=port, settle_ms=500, retries=2)
        if result.get("ok") or round_no == _DFU_TOUCH_ROUNDS:
            break
        # Off/on the board's own slot between rounds, then re-resolve the
        # app-mode port (re-enumeration may move the /dev path) and let the
        # app boot before touching again. Best-effort: without a resolvable
        # slot or uhubctl, the next round is a plain re-touch.
        if hub is None or slot is None:
            continue
        try:
            uhubctl.cycle(hub, slot, delay_s=2)
        except Exception as exc:
            print(f"[bake] {port}: inter-round power-cycle unavailable ({exc}); re-touching")
            continue
        deadline = time.monotonic() + _DFU_REENUM_TIMEOUT_S
        while time.monotonic() < deadline:
            candidate = port_recovery.port_on_slot(hub, slot)
            if candidate:
                port = candidate
                break
            time.sleep(0.5)
        time.sleep(2.0)  # app boot settle — a touch mid-boot is ignored
    if not result.get("ok"):
        raise AssertionError(
            f"nRF52 at {port} did not enter DFU bootloader after "
            f"{_DFU_TOUCH_ROUNDS} touch rounds (2 touches each) with "
            f"power-cycles between rounds. Manual recovery: double-tap the "
            f"reset button on the board, then re-run."
        )
    new_port = result["new_port"]
    if hub is not None and slot is not None:
        # The touched board re-enumerates (bootloader VID/PID) on its OWN slot;
        # prefer that path so we never upload to a different nRF52.
        on_slot = port_recovery.port_on_slot(hub, slot)
        if on_slot:
            new_port = on_slot
    # Small settle so pio/nrfutil sees a fully-ready CDC endpoint.
    time.sleep(1.0)
    return new_port


def _env_for(role: str) -> str:
    override = os.environ.get(f"MESHTASTIC_MCP_ENV_{role.upper()}")
    if override:
        return override
    if role not in _DEFAULT_ENVS:
        pytest.fail(
            f"no default PlatformIO env for role {role!r}. "
            f"Set MESHTASTIC_MCP_ENV_{role.upper()} to the env name."
        )
    return _DEFAULT_ENVS[role]


def _bake_role(
    role: str,
    port: str,
    test_profile: dict[str, Any],
    force_bake: bool,
) -> None:
    """Bake + boot + verify for a single role. Skips if already baked unless
    `--force-bake` was passed."""
    env = _env_for(role)

    # If not forcing, check if already baked with session profile.
    if not force_bake:
        try:
            live = info.device_info(port=port, timeout_s=8.0)
            # Quick heuristic: region matches and primary channel matches.
            expected_region_short = test_profile["USERPREFS_CONFIG_LORA_REGION"].rsplit("_", 1)[-1]
            if (
                live.get("region") == expected_region_short
                and live.get("primary_channel") == test_profile["USERPREFS_CHANNEL_0_NAME"]
            ):
                pytest.skip(
                    f"{role} at {port} already baked with session profile "
                    f"(pass --force-bake to reflash)"
                )
        except Exception:
            # If we can't query, fall through and bake anyway.
            pass

    # All architectures go through `pio run -t upload` — pio knows the right
    # protocol per variant (esptool for ESP32, adafruit-nrfutil for nRF52,
    # picotool for RP2040). We don't use `bin/device-install.sh` for ESP32
    # because it requires the external `mt-esp32s3-ota.bin` helper that's
    # downloaded from releases, not generated by the build.
    #
    # IMPORTANT: `pio run -t upload` on ESP32 only overwrites the APP
    # partition — the LittleFS partition (config + NodeDB) survives. That
    # means USERPREFS-baked defaults never take effect on a device that was
    # already provisioned, because NodeDB init prefers the saved config. To
    # force USERPREFS to apply cleanly, we erase the full chip first on
    # ESP32 boards. nRF52 DFU naturally wipes the user partition, so no
    # erase needed there.
    rec = boards.get_board(env)
    arch = rec.get("architecture") or ""
    # Make sure nothing else (TUI startup poll, MCP-host zombie, pio monitor)
    # is holding the port before we hand it to a subprocess. Self-heals the
    # [Errno 35] port-busy flake that otherwise fails the bake in ~0.1s.
    # Auto-unwedge a held/hung port before we hand it to a flash subprocess.
    # May return a NEW path (power-cycle re-enumerates the device).
    port = _wait_port_free(port, role=role)
    if arch in _NRF52_ARCHES:
        upload_port = _prepare_nrf52_for_upload(port)
    elif arch in _ESP32_ARCHES:
        # Full chip erase — wipes NVS + LittleFS so USERPREFS defaults apply.
        erase_result = hw_tools.esptool_erase_flash(port=port, confirm=True)
        assert erase_result["exit_code"] == 0, (
            f"{role}: esptool erase_flash failed:\n{erase_result.get('stderr_tail', '')}"
        )
        upload_port = port
    else:
        upload_port = port

    # Post-erase, pre-upload: full chip erase on ESP32 drops the CDC
    # endpoint for a moment while the bootloader re-enters download mode.
    # Wait for the port to settle before pio reopens it for upload —
    # otherwise a fast machine can race and hit the same errno 35.
    if arch in _ESP32_ARCHES:
        # Mid-erase the CDC drops briefly — settle-wait only, do NOT power-cycle
        # (that would interrupt the erase). unwedge=False.
        upload_port = _wait_port_free(upload_port, role=role, timeout_s=10.0, unwedge=False)

    # NOTE: no `userprefs_overrides=` here. The session-scoped
    # `_session_userprefs` autouse fixture in conftest.py has already baked
    # the test profile into userPrefs.jsonc for the duration of the session
    # and will restore the original file at session end. A local
    # `temporary_overrides` here would be a no-op (file is already baked)
    # AND would cause the session fixture's teardown to see different
    # stat / mtime than it snapshotted — keep the mutation in one place.
    result = flash.flash(
        env=env,
        port=upload_port,
        confirm=True,
        build_flags=_test_build_flags(),
    )
    assert result["exit_code"] == 0, (
        f"{role} bake failed: exit={result['exit_code']}\n"
        f"stdout tail:\n{result.get('stdout_tail', '')}\n"
        f"stderr tail:\n{result.get('stderr_tail', '')}"
    )

    # Post-flash: for nRF52, the DFU process only overwrites the app
    # partition — the NVS region holding the existing NodeDB/config is
    # untouched, so the firmware will prefer the saved config over the
    # baked USERPREFS defaults. Trigger a full factory reset to wipe NVS
    # so USERPREFS takes effect on the next boot.
    #
    # ESP32 devices had their full flash erased BEFORE upload via
    # esptool_erase_flash, so they don't need this post-flash reset.
    if arch in _NRF52_ARCHES:
        # Give the device time to come up from DFU.
        time.sleep(8.0)
        # nRF52 DFU re-enumerates the device to a fresh /dev/cu.usbmodem* path,
        # so the pre-flash `port` is stale — re-resolve it (and clear any leaked
        # registry lock on the old path) before polling. Both the loop below and
        # the factory_reset that follows must target the live path.
        from meshtastic_mcp import registry

        from ._port_discovery import resolve_port_by_role

        registry.clear_port_lock(port)
        try:
            port = resolve_port_by_role(role, timeout_s=45.0)
        except Exception:
            pass  # fall back to the original port; the loop below will surface it
        # Wait for meshtastic to be responsive; `device_info` may take a
        # few seconds on the first post-flash boot.
        for _ in range(20):
            try:
                info.device_info(port=port, timeout_s=6.0)
                break
            except Exception:
                time.sleep(1.5)
        else:
            raise AssertionError(f"{role}: device didn't respond after DFU flash")
        # Trigger full factory reset (wipes NVS + identity)
        admin.factory_reset(port=port, confirm=True, full=True)
        # Wait for the device to reboot and come back with fresh config
        # populated from USERPREFS defaults.
        time.sleep(10.0)
        for _ in range(30):
            try:
                live = info.device_info(port=port, timeout_s=6.0)
                if live.get("my_node_num"):
                    break
            except Exception:
                pass
            time.sleep(2.0)
        else:
            raise AssertionError(f"{role}: device didn't return after factory_reset")


@pytest.mark.timeout(600)
def test_bake(
    baked_single_role: str,
    hub_devices: dict[str, str],
    test_profile: dict[str, Any],
    request: pytest.FixtureRequest,
) -> None:
    """Flash one bench role with the session test profile.

    Auto-parametrized by `pytest_generate_tests` over every detected role, so
    each connected board is provisioned with ITS correct firmware (the env is
    resolved per role from `tests/_bench.py`) — instead of the old model that
    flashed one hard-coded env onto whichever same-VID board enumerated first
    and left the rest unprovisioned.
    """
    role = baked_single_role
    if role not in hub_devices:
        pytest.skip(f"role {role!r} not detected on hub")
    _bake_role(
        role=role,
        port=hub_devices[role],
        test_profile=test_profile,
        force_bake=request.config.getoption("--force-bake"),
    )
