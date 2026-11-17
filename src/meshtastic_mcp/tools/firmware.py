# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Firmware tools: boards, build and flash, userPrefs and the fake NodeDB push.

They register only when a firmware tree is present (`firmware_tool`).
"""

from __future__ import annotations

from typing import Any, cast

from fastmcp import Context
from fastmcp.exceptions import ToolError

from .. import boards, fixtures, flash, jobs
from .. import confirm as confirm_mod
from .. import tasks as tasks_mod
from .. import userprefs as userprefs_mod
from ._app import firmware_tool
from ._shared import _off_loop, _output_tail, _run_job, _to_markdown_table


@firmware_tool()
def list_boards(
    architecture: str | None = None,
    actively_supported_only: bool = False,
    query: str | None = None,
    board_level: str | None = None,
    format: str = "json",
) -> list[dict[str, Any]] | str:
    """Enumerate PlatformIO envs (boards) with Meshtastic metadata.

    Filters: `architecture` ("esp32", "esp32s3", "nrf52840", "rp2040", "stm32", "native"),
    `actively_supported_only` (`custom_meshtastic_actively_supported=true`), `query`
    (case-insensitive substring of display_name, env or hw_model_slug), `board_level`
    ("release" default-track, "pr" for PR CI, "extra" opt-in). `format="table"` gives a
    compact Markdown summary. JSON entries have env, architecture, hw_model,
    hw_model_slug, display_name, actively_supported, support_level, board_level, tags and
    images.
    """
    result = boards.list_boards(
        architecture=architecture,
        actively_supported_only=actively_supported_only,
        query=query,
        board_level=board_level,
    )
    if format == "table":
        summary = [
            {
                "env": b.get("env", ""),
                "arch": b.get("architecture", ""),
                "display_name": b.get("display_name", ""),
                "supported": b.get("actively_supported", ""),
            }
            for b in result
        ]
        return _to_markdown_table(summary)
    return result


@firmware_tool()
def get_board(env: str) -> dict[str, Any]:
    """Full metadata for one PlatformIO env, including raw pio config fields."""
    return boards.get_board(env)


# ---------- Build & flash -------------------------------------------------


@firmware_tool()
def build_start(
    env: str,
    with_manifest: bool = True,
    userprefs: dict[str, Any] | None = None,
    build_flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a firmware build in the background and return immediately.

    Returns a `build_id` (also as `job_id`) within ~1 second. Poll with
    `build_poll(build_id=...)` to check progress and retrieve output. Use this
    instead of `build` when your client does not run MCP tasks: an inline
    `build` blocks for the whole compile, past most client timeouts. Arguments as for
    `build`.
    """
    return flash.build_start(
        env,
        with_manifest=with_manifest,
        userprefs_overrides=userprefs,
        build_flags=build_flags,
    )


@firmware_tool()
def build_poll(build_id: str, tail_lines: int = 50) -> dict[str, Any]:
    """Poll the status of a background build.

    `build_id` is the id `build_start` returned, or the `job_id` from `build`.
    Returns status (running/done/failed/cancelled), elapsed_s, artifacts, and
    the last `tail_lines` lines of build output. Call every few seconds until
    the status is no longer 'running'. Ids do not survive a server restart. A `done` build
    is perishable: another session's build in the shared tree can delete it, so flash it
    at once.
    """
    return flash.build_poll(build_id, tail_lines=tail_lines)


@firmware_tool(**tasks_mod.task_kwargs())
async def build(
    ctx: Context,
    env: str,
    with_manifest: bool = True,
    userprefs: dict[str, Any] | None = None,
    build_flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build firmware for one env with `pio run -e <env>`.

    Runs as an MCP background task when the client supports them (`[tasks]` extra);
    otherwise the call blocks for the whole compile, so use `build_start` + `build_poll`
    there. Canceling the task stops the build; an abandoned call keeps building, and
    `build_poll(build_id=<job_id>)` reads it. Returns `job_id`, exit code, duration,
    artifacts under `.pio/build/<env>/` and the last 200 log lines; a failed build is a
    tool error. `with_manifest` adds the `.mt.json` manifest. `userprefs` bakes
    `USERPREFS_*` values into this build only, and `build_flags` adds `-D` macros for it,
    such as `{"DEBUG_HEAP": 1}` for a heap timeline. A finished build is perishable:
    another session's build in the shared tree can delete it, so flash it at once. See
    docs/firmware-builds.md.
    """
    started = flash.build_start(
        env,
        with_manifest=with_manifest,
        userprefs_overrides=userprefs,
        build_flags=build_flags,
    )
    # Only an explicit task cancel stops the build; an inline call abandoned by a client timeout
    # leaves it running, as the synchronous tool always did.
    final = await jobs.wait(
        started["job_id"],
        tail_lines=200,
        cancel_job=ctx.is_background_task,
        on_progress=ctx.report_progress,
    )
    if final["status"] != "done":
        raise ToolError(
            f"build of {env} {final['status']} with exit code {final['exit_code']}\n"
            f"{final['worker_error'] or ''}\n{_output_tail(final['log_tail'])}".strip()
        )
    return {
        "job_id": final["job_id"],
        "status": final["status"],
        "exit_code": final["exit_code"],
        "artifacts": final["artifacts"],
        "duration_s": final["duration_s"],
        "log_tail": final["log_tail"],
        "worker_error": final["worker_error"],
        "build_flags": dict(build_flags) if build_flags else None,
    }


@firmware_tool()
def clean(env: str) -> dict[str, Any]:
    """Clean one env's build output via `pio run -e <env> -t clean`, for a branch switch or
    a stale-cache build failure."""
    return flash.clean(env)


@firmware_tool(**tasks_mod.task_kwargs())
async def pio_flash(
    ctx: Context,
    env: str,
    port: str,
    confirm: bool = False,
    userprefs: dict[str, Any] | None = None,
    build_flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Flash firmware with `pio run -e <env> -t upload --upload-port <port>`, any arch.

    `port` is the board's serial port as `list_devices` reports it. Requires confirm=True,
    or the user's answer when the client can ask. Runs as a background task where
    supported (`[tasks]` extra), else blocks: use `flash_start` + `flash_poll` there. A
    flash cannot be canceled mid-write, so after a cancel or timeout follow its `job_id`
    (in the result, and logged first) with `flash_poll`. A failed upload is a tool error.
    For a blank ESP32 prefer `erase_and_flash`, for an ESP32 OTA update `update_flash`.
    `userprefs` and `build_flags` apply to the rebuild before upload; pass `build_flags`
    here or that rebuild drops them. See docs/firmware-builds.md.
    """
    pending = await confirm_mod.gate(ctx, f"flash {env} onto {port}", confirm)
    if pending is not None:
        return cast(dict[str, Any], pending)
    return await _run_job(
        ctx,
        "flashes",
        env,
        lambda: flash.flash(
            env,
            port,
            confirm=True,
            userprefs_overrides=userprefs,
            build_flags=build_flags,
        ),
    )


@firmware_tool()
async def flash_start(
    ctx: Context,
    env: str,
    port: str,
    confirm: bool = False,
    userprefs: dict[str, Any] | None = None,
    build_flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a firmware flash in the background and return immediately.

    Returns a `job_id` within ~1 second. Poll with `flash_poll(job_id=...)` until
    status is 'done' or 'failed'. Use this instead of `pio_flash` when your client
    does not run MCP tasks, since the upload routinely exceeds a 60 s call timeout.
    Requires confirm=True, or the user's answer when the client can ask. A busy
    port fails here, not on the first poll.

    Returns:
        {job_id: str, status: "running", log_path: str}
    """
    pending = await confirm_mod.gate(ctx, f"flash {env} onto {port}", confirm)
    if pending is not None:
        return cast(dict[str, Any], pending)
    return await _off_loop(
        lambda: flash.flash_start(
            env,
            port,
            confirm=True,
            userprefs_overrides=userprefs,
            build_flags=build_flags,
        )
    )


@firmware_tool()
def flash_poll(job_id: str, tail_lines: int = 50) -> dict[str, Any]:
    """Poll the status of a flash job.

    `job_id` comes from `flash_start`, or from the result or the logged
    "flashes job <id> started" line of `pio_flash`, `erase_and_flash`,
    `update_flash` and `nrf52_flash_uf2`. A flash keeps running after its call
    is canceled, so poll it to the end before touching the board.
    Returns status (running/done/failed), elapsed_s, exit_code, `error` when the
    cause is known, and the last `tail_lines` lines of output. Call every few seconds
    until terminal. Ids do not survive a server restart.
    """
    return flash.flash_poll(job_id, tail_lines=tail_lines)


@firmware_tool(**tasks_mod.task_kwargs())
async def erase_and_flash(
    ctx: Context,
    env: str,
    port: str,
    confirm: bool = False,
    skip_build: bool = False,
    userprefs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """ESP32 only: erase the whole flash and write the factory image (bin/device-install.sh).

    Writes bootloader, app, OTA and LittleFS from the factory.bin, building it first if it
    is missing (`skip_build=True` requires a prior build). Requires confirm=True, or the
    user's answer when the client can ask (always, with `MESHTASTIC_MCP_CONFIRM=human`).
    Runs as a background task where supported; the flash cannot be canceled mid-write,
    so after a cancel or timeout follow its `job_id` with `flash_poll`. `userprefs` bakes
    values in and forces a rebuild. See docs/firmware-builds.md.
    """
    pending = await confirm_mod.gate(
        ctx, f"erase the whole flash of {port} and flash {env}", confirm, irreversible=True
    )
    if pending is not None:
        return cast(dict[str, Any], pending)
    return await _run_job(
        ctx,
        "flashes",
        env,
        lambda: flash.erase_and_flash(
            env, port, confirm=True, skip_build=skip_build, userprefs_overrides=userprefs
        ),
    )


@firmware_tool(**tasks_mod.task_kwargs())
async def update_flash(
    ctx: Context,
    env: str,
    port: str,
    confirm: bool = False,
    skip_build: bool = False,
    userprefs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """ESP32 only: OTA-update the app partition (bin/device-update.sh), keeping config and
    the node DB.

    Faster than `erase_and_flash`, but cannot recover a broken bootloader. Requires
    confirm=True, or the user's answer when the client can ask. Runs as a background task
    where supported; the flash cannot be canceled mid-write, so after a cancel or timeout
    follow its `job_id` with `flash_poll`. Builds first if needed; `userprefs` bakes values
    into the firmware.bin and forces a rebuild. See docs/firmware-builds.md.
    """
    pending = await confirm_mod.gate(ctx, f"OTA-update {port} with {env}", confirm)
    if pending is not None:
        return cast(dict[str, Any], pending)
    return await _run_job(
        ctx,
        "flashes",
        env,
        lambda: flash.update_flash(
            env, port, confirm=True, skip_build=skip_build, userprefs_overrides=userprefs
        ),
    )


# ---------- USERPREFS discovery & persistence -----------------------------


@firmware_tool()
def userprefs_manifest() -> dict[str, Any]:
    """Full manifest of the `USERPREFS_*` keys the firmware knows: `userPrefs.jsonc` (active
    and commented examples) plus every key `src/**` consumes.

    Each entry has key, active, value, example, declared_in_jsonc, consumed_by and
    inferred_type (brace|number|bool|enum|string|unknown), which mirrors how
    `platformio-custom.py` wraps the value at build time. See docs/firmware-builds.md.
    """
    return userprefs_mod.build_manifest()


@firmware_tool()
def userprefs_get() -> dict[str, Any]:
    """Return the current userPrefs.jsonc state.

    `active` is the dict of uncommented `USERPREFS_*` → value that will be
    baked into the next build. `commented` is the dict of commented example
    defaults (shown for reference).
    """
    state = userprefs_mod.read_state()
    # Drop `order` (internal for round-trip rendering) from the public payload.
    return {
        "path": state["path"],
        "active": state["active"],
        "commented": state["commented"],
    }


@firmware_tool()
def userprefs_set(prefs: dict[str, Any]) -> dict[str, Any]:
    """Merge `prefs` into userPrefs.jsonc persistently (uncommenting keys).

    Existing active values not in `prefs` are kept. To remove a key from the
    active set, call `userprefs_reset` (restores the MCP backup if present)
    or edit the jsonc manually. Values are stringified the way
    platformio-custom.py expects (bool → "true"/"false", int → "42", etc.).
    """
    return userprefs_mod.merge_active(prefs)


@firmware_tool()
def userprefs_reset() -> dict[str, Any]:
    """Restore userPrefs.jsonc from the most recent MCP backup (if any).

    The backup is only created by the legacy `userprefs_set` workflow (not
    currently written automatically). Returns `{restored: bool, ...}` — false
    when no backup is present, in which case the caller should edit the
    jsonc directly.
    """
    return userprefs_mod.reset()


@firmware_tool()
def userprefs_testing_profile(
    psk_seed: str | None = None,
    channel_name: str = "McpTest",
    channel_num: int = 88,
    region: str = "US",
    modem_preset: str = "LONG_FAST",
    short_name: str | None = None,
    long_name: str | None = None,
    disable_mqtt: bool = True,
    disable_position: bool = False,
) -> dict[str, Any]:
    """Generate a USERPREFS dict for provisioning an isolated test-mesh device.

    Baking this into firmware produces devices that:
      - Run on a deterministic non-default LoRa slot (default 88 on US LONG_FAST,
        well off the `hash("LongFast")` slot a stock production device uses)
      - Join a private channel with a name and PSK that differ from public
        defaults — so no accidental mesh-with-production-devices
      - Have MQTT disabled (no uplink/downlink bridge), so test traffic never
        leaks to a public broker
      - Optionally disable GPS for bench-test conditions

    For a multi-device test cluster, pass the same `psk_seed` to every call so
    every device shares the same PSK and lands on the same isolated mesh.

    Returned dict is ready to pass straight to `build`, `pio_flash`,
    `erase_and_flash`, or `update_flash` via their `userprefs` parameter.

    Example:
        profile = userprefs_testing_profile(psk_seed="ci-run-2026-04-16")
        erase_and_flash(env="tbeam", port="/dev/cu.usbmodem...", confirm=True,
                        userprefs=profile)

    Args:
        psk_seed: seed for deterministic 32-byte PSK via SHA-256. None = random
            (fine one-off, useless for multi-device clusters).
        channel_name: primary channel name (≤11 chars). Default "McpTest".
        channel_num: 1-indexed LoRa slot (0 = fall back to name-hash). Default
            88 — mid-upper US band, unlikely to collide with production slots.
        region: short code — one of US, EU_433, EU_868, CN, JP, ANZ, KR, TW,
            RU, IN, NZ_865, TH, UA_433, UA_868, MY_433, MY_919, SG_923, LORA_24.
        modem_preset: one of LONG_FAST, LONG_SLOW, LONG_MODERATE, VERY_LONG_SLOW,
            MEDIUM_SLOW, MEDIUM_FAST, SHORT_SLOW, SHORT_FAST, SHORT_TURBO.
        short_name: optional owner short name (≤4 chars) stamped into the build.
        long_name: optional owner long name stamped into the build.
        disable_mqtt: disable MQTT module + uplink/downlink (default True).
        disable_position: disable GPS + smart-position broadcasts (default False).

    """
    return userprefs_mod.build_testing_profile(
        psk_seed=psk_seed,
        channel_name=channel_name,
        channel_num=channel_num,
        region=region,
        modem_preset=modem_preset,
        short_name=short_name,
        long_name=long_name,
        disable_mqtt=disable_mqtt,
        disable_position=disable_position,
    )


# ---------- Fixture / test-data push --------------------------------------


@firmware_tool()
async def push_fake_nodedb(
    ctx: Context,
    size: int,
    target: str = "portduino",
    port: str | None = None,
    portduino_config: str = "default",
    backup_existing: bool = True,
    confirm: bool = False,
    reboot_after: bool = True,
    custom_seed_jsonl: str | None = None,
) -> dict[str, Any]:
    """Push a fake-NodeDB v25 fixture (250/500/1000/2000 nodes) onto a device.

    `target="portduino"` copies it to `~/.portduino/<portduino_config>/prefs/nodes.proto`
    with no device connection. `target="hardware"` uploads it over XModem to
    `/prefs/nodes.proto` and reboots the device; that needs `port` and confirm=True, or the
    user's answer when the client can ask. Each call stamps fresh timestamps onto the
    committed seed, or onto `custom_seed_jsonl`. See docs/firmware-builds.md.
    """
    if target == "hardware":
        pending = await confirm_mod.gate(
            ctx, f"write a fake {size}-node NodeDB to {port or 'the device'} and reboot it", confirm
        )
        if pending is not None:
            return cast(dict[str, Any], pending)
        confirm = True
    return await _off_loop(
        lambda: fixtures.push_fake_nodedb(
            size=size,
            target=target,  # type: ignore[arg-type]
            port=port,
            portduino_config=portduino_config,
            backup_existing=backup_existing,
            confirm=confirm,
            reboot_after=reboot_after,
            custom_seed_jsonl=custom_seed_jsonl,
        )
    )
