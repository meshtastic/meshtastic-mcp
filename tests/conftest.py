# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Root conftest for the MCP server test harness.

Organizes the fixture graph used by every test tier:

    session_seed ── test_profile ─┐
    hub_devices ──────────────────┴─ baked_mesh (verifies) ── baked_single (parametrized)
    hub_devices ──────────────────── no_region_profile (provisioning negative test)
    (per-test)  ──────────────────── serial_capture, device_state_dump, wait_until

CLI flags (see `pytest_addoption`):
    --force-bake       always reflash at session start, even if state matches
    --assume-baked     trust the operator; skip test_00_bake collection entirely
    --hub-profile=...  path to a YAML file mapping role → {vid, pid_contains}
    --no-teardown-rebake  skip the session-end rebake that provisioning/fleet perform

Coverage hooks:
    - Failure artifacts (serial capture, device_info, get_config) are attached
      to pytest-html reports via `pytest_runtest_makereport`.
    - Tool-surface coverage (which of the 37 MCP tools got exercised) is
      accumulated in `tests/tool_coverage.py` and written to
      `tool_coverage.json` at session end.
"""

from __future__ import annotations

import atexit
import json
import os
import pathlib
import sys
import time
from collections.abc import Callable
from typing import Any

import pytest

# Ensure the MCP server is on `sys.path` without requiring installation in
# development mode for every checkout (we DO install in .venv but this makes
# `pytest tests/` work from a fresh clone too). The path mutation must
# happen before `meshtastic_mcp.*` imports below — hence the `noqa: E402`
# markers on those imports (ruff's "module-level import not at top of file"
# rule doesn't understand path-bootstrapping patterns).
_HERE = pathlib.Path(__file__).resolve().parent
_MCP_SRC = _HERE.parent / "src"
if str(_MCP_SRC) not in sys.path:
    sys.path.insert(0, str(_MCP_SRC))

# Default firmware root: the repo this mcp-server/ lives inside.
os.environ.setdefault("MESHTASTIC_FIRMWARE_ROOT", str(_HERE.parent.parent))

from meshtastic_mcp import (
    admin,
    info,
    serial_session,
    userprefs,
)
from meshtastic_mcp import config as mcp_config
from meshtastic_mcp import devices as devices_module

from . import (
    _bench,
    tool_coverage,
)

# ---------- CLI options ---------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Inject per-role bench pins into the environment so power/recovery
    (`uhubctl.resolve_target`) and the bake can address each board
    unambiguously.

    Three of the four bench boards share the nRF52 VID 0x239a, so VID alone
    can't tell them apart — the hub-slot LOCATION pins are what disambiguate.
    `resolve_target` checks `MESHTASTIC_UHUBCTL_LOCATION_<ROLE>` /
    `_PORT_<ROLE>` *before* its VID table, so setting them here makes recovery
    work per board without touching production `uhubctl.ROLE_VIDS`.

    Operator-set env vars win — we only fill blanks — so manual pins or a
    `--hub-profile` still override.
    """
    for role, spec in _bench.BENCH_ROLES.items():
        env = spec.get("env")
        if env:
            os.environ.setdefault(f"MESHTASTIC_MCP_ENV_{role.upper()}", env)
        hubport = _bench.location_hub_port(spec.get("location"))
        if hubport:
            hub, port = hubport
            os.environ.setdefault(f"MESHTASTIC_UHUBCTL_LOCATION_{role.upper()}", hub)
            os.environ.setdefault(f"MESHTASTIC_UHUBCTL_PORT_{role.upper()}", str(port))


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("meshtastic", "Meshtastic MCP test options")
    group.addoption(
        "--force-bake",
        action="store_true",
        help="Flash both hub roles at session start, even if devices appear baked.",
    )
    group.addoption(
        "--assume-baked",
        action="store_true",
        help="Skip `test_00_bake.py` and trust devices are already baked.",
    )
    group.addoption(
        "--hub-profile",
        default=None,
        help="YAML file mapping role → {vid, pid_contains} for non-default hardware.",
    )
    group.addoption(
        "--no-teardown-rebake",
        action="store_true",
        help="Skip session-end rebake after provisioning/fleet tests mutate state.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect `test_00_bake.py` when --assume-baked is passed, and sort
    items so that admin/ + provisioning/ (tests that mutate device state
    via reboot or factory_reset) run AFTER the read-only mesh/telemetry
    tests.

    Why the reorder: admin/test_owner_survives_reboot reboots both
    devices; provisioning/test_baked_prefs_survive_factory_reset does a
    factory_reset. Both wipe the in-memory PKI public-key table. Directed
    sends with wantAck=True then NAK with Routing.Error=39
    (PKI_SEND_FAIL_PUBLIC_KEY) because TX lost RX's key, and the firmware
    NodeInfo cooldown (10 min) + 12-h reply suppression make re-exchange
    slow enough to fail within a test budget. Running mesh/telemetry
    first against the pre-reboot state is both faster and more reliable;
    admin/provisioning then runs against a clean mesh and exercises its
    own invariants without contaminating other tiers.
    """
    if config.getoption("--assume-baked"):
        for item in items:
            if "test_00_bake" in item.nodeid:
                item.add_marker(pytest.mark.skip(reason="skipped by --assume-baked"))

    def sort_key(item: pytest.Item) -> tuple[int, str]:
        path = str(getattr(item, "fspath", "") or item.nodeid)
        # Session-start bake runs FIRST. `baked_mesh` only verifies state —
        # nothing else actually reflashes — so if test_00_bake doesn't run
        # before the tier tests, `--force-bake` silently becomes a no-op for
        # the tier tests and only flashes at the very end of the session.
        # Top-level nodeid ("tests/test_00_bake.py") otherwise falls into the
        # fallback bucket and sorts after every tier.
        if "test_00_bake" in item.nodeid:
            return (-1, item.nodeid)
        # Tiers that don't mutate device state run first.
        if "/unit/" in path or "tests/unit" in path:
            return (0, item.nodeid)
        if "/mesh/" in path or "tests/mesh" in path:
            return (1, item.nodeid)
        if "/telemetry/" in path or "tests/telemetry" in path:
            return (2, item.nodeid)
        if "/monitor/" in path or "tests/monitor" in path:
            return (3, item.nodeid)
        # Recovery tier: explicitly cycles device power via uhubctl. Slots
        # between monitor (read-only) and ui (state-preserving) so any tier
        # after it starts from a known re-enumerated + re-verified state.
        if "/recovery/" in path or "tests/recovery" in path:
            return (4, item.nodeid)
        # UI tier slots here — read-only w.r.t. mesh state, only mutates
        # the on-screen UI (BACK×5 guard restores home before each test).
        if "/ui/" in path or "tests/ui" in path:
            return (5, item.nodeid)
        if "/fleet/" in path or "tests/fleet" in path:
            return (6, item.nodeid)
        # State-mutating tiers run last.
        if "/admin/" in path or "tests/admin" in path:
            return (7, item.nodeid)
        if "/provisioning/" in path or "tests/provisioning" in path:
            return (8, item.nodeid)
        # Top-level + anything else falls between.
        return (9, item.nodeid)

    items.sort(key=sort_key)

    # Tiering: `firmware`-marked tests need a Meshtastic checkout (variants/pio/userprefs).
    # Auto-skip them when no firmware tree is present so the portable unit tier stays green
    # on a bare `pip install meshtastic-mcp`. Run them with MESHTASTIC_FIRMWARE_ROOT set.
    if mcp_config.firmware_root_or_none() is None:
        skip_fw = pytest.mark.skip(reason="firmware tier: MESHTASTIC_FIRMWARE_ROOT not set")
        for item in items:
            if item.get_closest_marker("firmware"):
                item.add_marker(skip_fw)


# ---------- Session-scoped fixtures ---------------------------------------


@pytest.fixture(scope="session")
def session_seed(request: pytest.FixtureRequest) -> str:
    """Deterministic PSK seed for this pytest session.

    Logged in the HTML report header so two runs can be correlated — and so a
    flaky-looking test can be reproduced exactly by passing the seed back via
    an env var (future extension).
    """
    # Pytest session `starttime` isn't directly exposed on the pytest API we
    # care about, so derive from process start time — unique enough for human
    # purposes and stable across the session.
    seed = os.environ.get("MESHTASTIC_MCP_SEED") or f"pytest-{int(time.time())}"
    return seed


@pytest.fixture(scope="session")
def test_profile(session_seed: str) -> dict[str, Any]:
    """The canonical isolated-mesh test profile for this session.

    `enable_ui_log=True` stamps `USERPREFS_UI_TEST_LOG` so the firmware
    emits `Screen: frame N/M name=... reason=...` log lines per UI
    transition — consumed by the `tests/ui/` tier. Harmless on boards
    without a screen (the `#ifdef` sits behind `HAS_SCREEN`).
    """
    return userprefs.build_testing_profile(
        psk_seed=session_seed,
        channel_name="McpTest",
        channel_num=88,
        region="US",
        modem_preset="LONG_FAST",
        enable_ui_log=True,
    )


@pytest.fixture(scope="session", autouse=True)
def _session_userprefs(test_profile: dict[str, Any]) -> Any:
    """Snapshot `userPrefs.jsonc`, apply the session test profile, restore at
    session end. Guards against the suite leaving test-profile USERPREFS
    values baked into the file — if that happened, any firmware build a
    contributor ran next would silently inherit the test PSK / test channel
    name / test admin key etc.

    Layered safety:
      1. In-memory snapshot taken before any mutation; teardown writes it back.
      2. Sidecar `userPrefs.jsonc.mcp-session-bak` on disk — belt to the
         in-memory suspenders. If Python segfaults or SIGKILLs, the next
         session self-heals from this file at startup.
      3. `atexit.register()` fallback: if pytest exits abnormally (Ctrl-C
         mid-test, fatal exception before teardown), the atexit hook still
         restores from the in-memory snapshot.
      4. Startup self-heal: if the sidecar exists at session start, a prior
         session crashed without cleanup — the sidecar IS the truth; restore
         from it before taking this session's snapshot. That way a crash
         during test A doesn't propagate dirty state into test B's baseline.

    Autouse + depends on `test_profile` so it applies on every run (even
    unit-only) — cheap, unified code path, no ordering surprises.
    """
    # Decoupled: the userprefs sidecar lives in the firmware tree. Without a firmware
    # checkout (portable core / firmware-free unit tier), there is nothing to snapshot.
    if mcp_config.firmware_root_or_none() is None:
        yield
        return

    path = userprefs.jsonc_path()
    backup_path = path.with_name(path.name + ".mcp-session-bak")

    if not path.is_file():
        # Nothing to snapshot; yield no-op and skip restore.
        yield
        return

    # (4) Startup self-heal — prior session crashed without teardown.
    if backup_path.is_file():
        try:
            sidecar_bytes = backup_path.read_bytes()
            current_bytes = path.read_bytes()
            if sidecar_bytes != current_bytes:
                path.write_bytes(sidecar_bytes)
                print(
                    f"[userprefs] recovered {path.name} from "
                    f"{backup_path.name} (prior session exited without "
                    f"cleanup)",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(
                f"[userprefs] startup self-heal failed: {exc!r}",
                file=sys.stderr,
            )

    # (1) + (2) Snapshot + sidecar.
    original_bytes = path.read_bytes()
    original_stat = path.stat()
    try:
        backup_path.write_bytes(original_bytes)
    except Exception as exc:
        print(f"[userprefs] could not write sidecar: {exc!r}", file=sys.stderr)

    # (3) atexit fallback — fires even if pytest aborts before fixture teardown.
    restored = {"done": False}

    def _atexit_restore() -> None:
        if restored["done"]:
            return
        try:
            path.write_bytes(original_bytes)
        except Exception:
            pass
        try:
            if backup_path.is_file():
                backup_path.unlink()
        except Exception:
            pass
        restored["done"] = True

    atexit.register(_atexit_restore)

    # Apply the session test profile on top of the snapshot. The firmware
    # reads userPrefs.jsonc at build time via `bin/platformio-custom.py`,
    # so every `pio run` during the session picks up the test values.
    # Delegate to `userprefs.merge_active` — the public API that already
    # parses, merges, validates, and writes — rather than reaching into
    # the private parser/renderer machinery from here.
    try:
        userprefs.merge_active(test_profile)
        # Bump mtime so any pre-existing `.pio/build/*/` cache is invalidated.
        now = time.time()
        os.utime(path, (now, now))
    except Exception as exc:
        # Non-fatal: tests that depend on the baked profile will fail loudly;
        # tests that don't (unit) still run. But the restore below is
        # unconditional, so we can't leave a half-written file behind.
        print(
            f"[userprefs] failed to apply test profile: {exc!r} — file left at original state",
            file=sys.stderr,
        )
        try:
            path.write_bytes(original_bytes)
        except Exception:
            pass

    try:
        yield
    finally:
        restore_ok = False
        try:
            path.write_bytes(original_bytes)
            os.utime(path, (original_stat.st_atime, original_stat.st_mtime))
            restore_ok = True
        except Exception as exc:
            # Don't `return` out of finally (that swallows any in-flight
            # exception from the yielded body); use a flag so the cleanup
            # control-flow stays linear and exceptions propagate normally.
            print(
                f"[userprefs] teardown restore failed: {exc!r} — "
                f"sidecar {backup_path} retained for manual recovery",
                file=sys.stderr,
            )
        if restore_ok:
            try:
                if backup_path.is_file():
                    backup_path.unlink()
            except Exception:
                pass
        # Mark done either way: on success, cleanup is complete; on failure,
        # the sidecar is intentionally left for next-run self-heal and we
        # don't want the atexit hook to fight us.
        restored["done"] = True
        try:
            atexit.unregister(_atexit_restore)
        except Exception:
            pass


@pytest.fixture(scope="session")
def no_region_profile(session_seed: str) -> dict[str, Any]:
    """Variant of `test_profile` with the LoRa region stripped.

    Used only by the negative `unset_region_blocks_tx` test. That test MUST
    re-bake `test_profile` in its own teardown so downstream shared-state
    tests still see a correctly-configured mesh.
    """
    profile = userprefs.build_testing_profile(
        psk_seed=session_seed,
        channel_name="McpTest",
        channel_num=88,
        region="US",  # placeholder; we delete the key below
        modem_preset="LONG_FAST",
    )
    profile.pop("USERPREFS_CONFIG_LORA_REGION", None)
    return profile


@pytest.fixture(scope="session")
def hub_profile(request: pytest.FixtureRequest) -> dict[str, dict[str, Any]]:
    """Role → {vid, pid_contains} map for detecting connected hardware.

    Default covers the common nRF52840 + ESP32-S3 lab hub. Override via
    `--hub-profile=path/to/hub.yaml`. Example YAML:

        nrf52:
          vid: 0x239a
          pid_contains: null
        esp32s3:
          vid: 0x303a
          pid_contains: null
    """
    path = request.config.getoption("--hub-profile")
    if path:
        import yaml

        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    # Default: the reference bench, from the single source of truth in
    # tests/_bench.py. Four distinct boards (three share VID 0x239a), each
    # pinned to its hub-slot location so they're told apart unambiguously.
    return _bench.hub_profile()


def _hex_to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    return None


def _match_role_port(spec: dict[str, Any], found: list[dict]) -> str | None:
    """Resolve one hub_profile role spec to a connected `/dev` path.

    Prefers the role's pinned hub-slot ``location`` (stable across the
    app↔bootloader USB PID flip and unambiguous when several boards share a
    VID); falls back to VID (+ optional ``pid_contains``) for specs with no
    location (e.g. a ``--hub-profile`` yaml). Returns None if absent.
    """
    location = spec.get("location")
    vids = (spec["vid"], *tuple(spec.get("alt_vids", ())))
    pid_contains = spec.get("pid_contains")
    for dev in found:
        port = dev.get("port")
        if not port:
            continue
        if location is not None:
            # Do NOT fall back to VID here — we want the board on THIS slot,
            # not any same-VID sibling.
            if _bench.device_location(port) == location:
                return port
            continue
        if _hex_to_int(dev.get("vid")) not in vids:
            continue
        if pid_contains is not None:
            pid_raw = dev.get("pid")
            pid_hex = (
                pid_raw
                if isinstance(pid_raw, str)
                else (hex(pid_raw) if isinstance(pid_raw, int) else None)
            )
            if pid_hex is None or pid_contains not in pid_hex:
                continue
        return port
    return None


@pytest.fixture(scope="session")
def hub_devices(hub_profile: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Map of `role → port` for boards detected on the hub.

    Each role is matched to a SPECIFIC physical board by hub-slot location
    (see `_match_role_port`), so three same-VID nRF52 boards resolve to three
    distinct roles instead of collapsing into one. If a role isn't detected,
    it's simply absent; fixtures that require a role check presence and
    `pytest.skip` with an actionable message.
    """
    # include_unknown=True so non-whitelisted VIDs (e.g. CP2102 at 0x10c4) that
    # are configured as hub roles still match.
    found = devices_module.list_devices(include_unknown=True)
    resolved: dict[str, str] = {}
    for role, spec in hub_profile.items():
        # Skip legacy `*_alt` aliases if a yaml profile still uses them.
        canonical = role.split("_alt", 1)[0]
        if canonical in resolved:
            continue
        port = _match_role_port(spec, found)
        if port is not None:
            resolved[canonical] = port
    return resolved


def _reset_transmit_history_state(role: str, port: str) -> str:
    """Wipe `/prefs/transmit_history.dat` + in-memory throttle cache via
    delete_file_request + reboot. Returns the post-reboot port (nRF52
    re-enumerates). Best-effort — errors log to stderr + return original
    port so a flaky start doesn't block the session.
    """
    import threading

    from meshtastic_mcp import port_recovery

    from ._port_discovery import resolve_port_by_role

    # Recover the device before we talk to it: free a held port AND power-cycle a
    # wedged/unresponsive one (a stale fd, or firmware that won't answer). On a
    # bench with several same-role devices the slot is resolved per-device from
    # USB topology, so this never cycles the wrong one. Best-effort.
    try:
        port = port_recovery.ensure_port_responsive(port, role=role)
    except Exception as exc:
        print(
            f"[transmit-history-reset] {role} @ {port} recovery skipped: {exc!r}",
            file=sys.stderr,
        )

    def _clear(p: str) -> None:
        from meshtastic.protobuf import admin_pb2  # type: ignore[import-untyped]

        from meshtastic_mcp.connection import connect

        with connect(port=p) as iface:
            msg = admin_pb2.AdminMessage()
            msg.delete_file_request = "/prefs/transmit_history.dat"
            iface.localNode._sendAdmin(msg)
            time.sleep(1.0)
            # Reboot clears in-memory cache; otherwise the 5-min auto-flush
            # rewrites the file with pre-reset timestamps.
            iface.localNode.reboot(3)
            iface.noProto = True  # rebooting — don't block the close on the TX queue

    # Bound the connect+send hard. meshtastic's `_sendToRadio` drains the TX queue
    # with an UNBOUNDED `while not _queueHasFreeSpace(): sleep(0.5)` loop, so a
    # single stuck send to an unresponsive device would otherwise hang this
    # session-scoped autouse fixture for 600s (pytest-timeout) and ERROR every
    # test in the run. Run it on a daemon thread and abandon after 45s — the
    # bake's own ensure_port_free will recover the port if the leaked thread
    # still holds it.
    box: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            _clear(port)
        except BaseException as exc:
            box["exc"] = exc

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=45.0)
    if t.is_alive():
        # The abandoned thread is stuck inside connect() holding the port's
        # registry lock and will never release it — drop the lock so later
        # in-process connect()s (e.g. the bake's post-flash device_info) aren't
        # blocked with "port is busy" for the whole session.
        from meshtastic_mcp import registry

        registry.clear_port_lock(port)
        print(
            f"[transmit-history-reset] {role} @ {port} did not finish in 45s — "
            f"abandoning (device unresponsive); continuing session.",
            file=sys.stderr,
        )
        return port
    if "exc" in box:
        print(
            f"[transmit-history-reset] {role} @ {port} clear failed: {box['exc']!r}",
            file=sys.stderr,
        )
        return port

    time.sleep(8.0)
    try:
        fresh = resolve_port_by_role(role, timeout_s=45.0)
    except Exception as exc:
        print(
            f"[transmit-history-reset] {role} didn't reappear: {exc!r}",
            file=sys.stderr,
        )
        return port
    for _ in range(20):
        try:
            if info.device_info(port=fresh, timeout_s=5.0).get("my_node_num"):
                return fresh
        except Exception:
            time.sleep(1.5)
    return fresh


@pytest.fixture(scope="session", autouse=True)
def _session_clear_transmit_history(hub_devices: dict[str, str]) -> None:
    """Wipe transmit_history.dat on each device at session start.

    Without this, the firmware's per-portnum last-broadcast cache
    (`src/mesh/TransmitHistory.h`) carries throttle state across sessions
    and suppresses early broadcasts. Mutates `hub_devices` in place with
    post-reboot ports since nRF52 re-enumerates.
    """
    if not hub_devices:
        yield
        return
    # Iterate over a snapshot — _reset_transmit_history_state can mutate
    # hub_devices mid-loop via the update below, and dict-iteration isn't
    # safe during mutation.
    for role, port in list(hub_devices.items()):
        fresh_port = _reset_transmit_history_state(role, port)
        if fresh_port != port:
            hub_devices[role] = fresh_port
    yield


@pytest.fixture(scope="session")
def baked_mesh(
    hub_devices: dict[str, str],
    test_profile: dict[str, Any],
    session_seed: str,
    request: pytest.FixtureRequest,
) -> dict[str, Any]:
    """Verify that both roles are baked with the session `test_profile`.

    Does NOT reflash. `test_00_bake.py` is responsible for applying the bake;
    this fixture just checks the result by connecting to each device and
    comparing the live config to the expected profile.

    Raises with an actionable error if state is missing or mismatched:
        "device nrf52 at /dev/cu.X not baked with session profile —
         run test_00_bake.py first or pass --force-bake"

    Returns a per-role dict with `{port, iface_fresh: callable, my_node_num}`.
    """
    # Verify every role that's present — don't require a fixed set.
    # Tests that NEED a specific role (mesh_pair, bidirectional) check
    # presence in their own fixtures and skip there with an actionable
    # message. That keeps single-device tests runnable on a one-device
    # hub without needing a --hub-profile override.
    if not hub_devices:
        pytest.skip("no hub roles detected. Attach a device or override with --hub-profile.")

    expected_region = test_profile["USERPREFS_CONFIG_LORA_REGION"]
    expected_preset = test_profile["USERPREFS_LORACONFIG_MODEM_PRESET"]
    expected_slot = test_profile["USERPREFS_LORACONFIG_CHANNEL_NUM"]
    expected_channel_name = test_profile["USERPREFS_CHANNEL_0_NAME"]

    out: dict[str, Any] = {}
    per_role_errors: dict[str, str] = {}
    for role in sorted(hub_devices):
        port = hub_devices[role]
        # Recover a device left wedged by a prior tier (the CP210x esp32s3 goes
        # EINVAL after the recovery-tier power-cycles) before we verify it —
        # best-effort, same pattern as transmit-history-reset / baked_single.
        try:
            from meshtastic_mcp import port_recovery

            port = port_recovery.ensure_port_responsive(port, role=role)
        except Exception:
            pass
        try:
            live = info.device_info(port=port, timeout_s=12.0)
        except Exception as exc:
            # Per-role failure — drop this role from the baked set and let
            # any test parametrized against it skip with the actionable
            # message. Other roles still proceed.
            per_role_errors[role] = f"device_info failed: {exc!r}"
            continue
        # `device_info` surfaces region/primary_channel but not modem preset
        # or channel_num directly; pull those via a separate get_config call.
        try:
            lora_cfg = admin.get_config(section="lora", port=port)["config"]["lora"]
        except Exception as exc:
            per_role_errors[role] = f"get_config(lora) failed: {exc!r}"
            continue
        channel_num = int(lora_cfg.get("channel_num", 0))
        modem_preset = lora_cfg.get("modem_preset")
        region_short = live.get("region")
        primary = live.get("primary_channel")

        mismatches = []
        if region_short and not expected_region.endswith(str(region_short)):
            mismatches.append(f"region={region_short} (expected {expected_region})")
        # `modem_preset` is omitted from the protobuf→JSON dump when it's the
        # default (LONG_FAST, value 0). Missing + expected-LONG_FAST = match.
        if modem_preset is None:
            if not expected_preset.endswith("_LONG_FAST"):
                mismatches.append(f"modem_preset=<default LONG_FAST> (expected {expected_preset})")
        elif not expected_preset.endswith(str(modem_preset)):
            mismatches.append(f"modem_preset={modem_preset} (expected {expected_preset})")
        if channel_num != expected_slot:
            mismatches.append(f"channel_num={channel_num} (expected {expected_slot})")
        if primary and primary != expected_channel_name:
            mismatches.append(f"primary_channel={primary!r} (expected {expected_channel_name!r})")

        if mismatches:
            per_role_errors[role] = "not baked with session profile: " + "; ".join(mismatches)
            continue

        out[role] = {
            "port": port,
            "my_node_num": live.get("my_node_num"),
            "firmware_version": live.get("firmware_version"),
        }

        # NOTE: we intentionally do NOT auto-enable `security.debug_log_api_enabled`
        # here. Firmware's `emitLogRecord` (src/mesh/StreamAPI.cpp:196) shares the
        # `fromRadioScratch` / `txBuf` buffers with the main packet-emission path;
        # LOG_ calls that race in-flight FromRadio emissions corrupt the byte
        # stream, triggering protobuf DecodeError in meshtastic-python and killing
        # the SerialInterface. Operators who want log capture can opt in via the
        # `set_debug_log_api` MCP tool (or `admin.set_debug_log_api` directly) on
        # a case-by-case basis. The autouse `_debug_log_buffer` fixture is still
        # armed below — if a test explicitly enables the flag, its output will
        # be captured and attached to failures. Firmware-side fix would need
        # a separate tx buffer or a mutex — out of scope for the MCP harness.

    # If EVERY detected role errored, skip the session — nothing testable.
    # Otherwise yield the partial set. Tests parametrized against a role
    # not in `out` will skip via the `baked_single`/`mesh_pair` presence
    # check with "role not present on the hub".
    if not out:
        details = "\n  ".join(f"{r}: {e}" for r, e in per_role_errors.items())
        pytest.skip(
            "no devices matched the session bake profile:\n  "
            + details
            + "\nRun `pytest tests/test_00_bake.py --force-bake` first."
        )
    return out


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Auto-parametrize `baked_single` over every detected hub role, and
    `mesh_pair` over every ordered (tx, rx) pair.

    This is the "tests are context-aware of the device they're against" layer:
    a test that takes `baked_single` runs once per connected device, so its
    report ID reads `test_owner_survives_reboot[nrf52]` /
    `test_owner_survives_reboot[esp32s3]`. Cross-device tests that take
    `mesh_pair` run for every direction, so A→B and B→A are both asserted.

    Both fall back to a hardcoded default set when hardware isn't present so
    the test still COLLECTS cleanly (it'll just skip via the
    `hub_devices` missing-role check inside the fixture).

    Honors `--hub-profile=<yaml>` for non-default hardware — when set, only
    roles defined in the YAML are parametrized. (So e.g. a yaml with only
    `esp32s3` skips every `[nrf52]` variant at collection time.)
    """
    # Resolve the role → spec map, honoring --hub-profile if passed; otherwise
    # the reference bench from tests/_bench.py.
    profile_path = metafunc.config.getoption("--hub-profile", default=None)
    if profile_path:
        import yaml

        with open(profile_path, encoding="utf-8") as f:
            profile = yaml.safe_load(f) or {}
    else:
        profile = _bench.hub_profile()

    try:
        from meshtastic_mcp import devices as _dev

        found = _dev.list_devices(include_unknown=True)
    except Exception:
        found = []

    # Detect each role by its SPECIFIC board (location, or VID for yaml specs
    # with no location) — so three same-VID nRF52 boards parametrize as three
    # distinct roles rather than one.
    detected: list[str] = []
    for role, spec in profile.items():
        canonical = role.split("_alt", 1)[0]
        if canonical in detected:
            continue
        if _match_role_port(spec, found) is not None:
            detected.append(canonical)

    # Fall back to the full role set when nothing is detected, so the suite
    # still COLLECTS cleanly off-bench (each variant skips at runtime via the
    # hub_devices presence check).
    fallback: list[str] = []
    for role in profile:
        canonical = role.split("_alt", 1)[0]
        if canonical not in fallback:
            fallback.append(canonical)
    roles = detected or fallback

    if "baked_single_role" in metafunc.fixturenames:
        metafunc.parametrize("baked_single_role", roles, ids=roles, scope="function")

    if "mesh_pair_roles" in metafunc.fixturenames:
        pairs = [(a, b) for a in roles for b in roles if a != b]
        ids = [f"{a}->{b}" for a, b in pairs]
        metafunc.parametrize("mesh_pair_roles", pairs, ids=ids, scope="function")


@pytest.fixture
def baked_single(
    baked_mesh: dict[str, Any],
    baked_single_role: str,
    hub_devices: dict[str, str],
) -> dict[str, Any]:
    """Function-scoped: a single verified baked device.

    Auto-parametrized by `pytest_generate_tests` over every detected hub
    role — so any test taking this fixture runs once per connected device
    (e.g. `test_owner_survives_reboot[nrf52]` +
    `test_owner_survives_reboot[esp32s3]`). Tests never hardcode a role
    and never skip a device that happens to be connected.

    Auto-recovery: if the baked device fails a pre-test `device_info` probe
    AND uhubctl is available, power-cycle the port once and retry. Without
    uhubctl, surface the wedge as a clear skip. This catches "device got
    stuck between tests" without masking persistent regressions (a second
    wedge after cycling still skips).
    """
    if baked_single_role not in baked_mesh:
        pytest.skip(f"role {baked_single_role!r} not present on the hub")

    entry = baked_mesh[baked_single_role]
    port = entry.get("port")
    if port:
        try:
            _run_with_timeout(lambda: info.device_info(port=port, timeout_s=3.0), 5.0)
        except Exception:
            # Device didn't respond. Try a power-cycle recovery if uhubctl
            # is installed; otherwise surface a skip that names the root
            # cause clearly.
            from tests import _power

            if not _power.is_uhubctl_available():
                pytest.skip(
                    f"device {baked_single_role!r} unresponsive on {port}; "
                    "install uhubctl (`brew install uhubctl` / `apt install "
                    "uhubctl`) for auto power-cycle recovery"
                )
            try:
                new_port = _power.power_cycle(baked_single_role, delay_s=2)
            except Exception as exc:
                pytest.skip(f"device {baked_single_role!r} wedged and power-cycle failed: {exc}")
            # Mutate both the session-scoped `hub_devices` map AND the
            # baked_mesh entry so downstream fixtures see the recovered port.
            hub_devices[baked_single_role] = new_port
            baked_mesh[baked_single_role]["port"] = new_port
            entry = baked_mesh[baked_single_role]
    return {"role": baked_single_role, **entry}


@pytest.fixture
def power_cycle(
    hub_devices: dict[str, str],
) -> Callable[..., str]:
    """Return a callable `(role, delay_s=2) -> new_port` that hard-resets the
    hub port hosting `role`. Skips the test cleanly when uhubctl isn't
    installed — never want "no uhubctl" to look like a test failure.

    The callable mutates `hub_devices[role]` in place so subsequent fixture
    lookups pick up the post-cycle port (mirrors the pattern in
    provisioning/test_userprefs_survive_factory_reset.py).
    """
    from tests import _power

    if not _power.is_uhubctl_available():
        pytest.skip(
            "uhubctl not installed; this test needs it for power control. "
            "Install via `brew install uhubctl` (macOS) or `apt install "
            "uhubctl` (Debian/Ubuntu)."
        )

    def _cycle(role: str, delay_s: int = 2) -> str:
        new_port = _power.power_cycle(role, delay_s=delay_s)
        hub_devices[role] = new_port
        return new_port

    return _cycle


# role → PlatformIO env, from the single source of truth in tests/_bench.py.
_DEFAULT_ROLE_ENVS = _bench.role_envs()


@pytest.fixture
def role_env() -> Callable[[str], str]:
    """Resolve `role` → PlatformIO env name.

    Falls back to a default map tuned for the lab's default hardware
    (RAK4631 + Heltec V3). Override per-role via env vars like
    `MESHTASTIC_MCP_ENV_NRF52=my-custom-nrf-env`. Used by tests that need to
    reflash a device (provisioning/fleet tiers).
    """

    def _resolve(role: str) -> str:
        override = os.environ.get(f"MESHTASTIC_MCP_ENV_{role.upper()}")
        if override:
            return override
        if role not in _DEFAULT_ROLE_ENVS:
            raise KeyError(
                f"no default env for role {role!r}; set MESHTASTIC_MCP_ENV_{role.upper()}"
            )
        return _DEFAULT_ROLE_ENVS[role]

    return _resolve


@pytest.fixture
def mesh_pair(
    baked_mesh: dict[str, Any],
    mesh_pair_roles: tuple[str, str],
) -> dict[str, Any]:
    """Function-scoped: an ordered (tx, rx) pair of baked devices.

    Auto-parametrized over every directed role pair, so a test that takes
    `mesh_pair` runs for `nrf52->esp32s3` AND `esp32s3->nrf52` and asserts
    communication in both directions independently. Cross-device tests
    (mesh formation, broadcast delivery, direct+ACK) should prefer this over
    `baked_mesh` so both directions are validated.
    """
    tx_role, rx_role = mesh_pair_roles
    for role in (tx_role, rx_role):
        if role not in baked_mesh:
            pytest.skip(f"role {role!r} not present on the hub")
    return {
        "tx_role": tx_role,
        "rx_role": rx_role,
        "tx": {"role": tx_role, **baked_mesh[tx_role]},
        "rx": {"role": rx_role, **baked_mesh[rx_role]},
    }


# ---------- Failure-artifact fixtures -------------------------------------


class _SerialCapture:
    """Active-session wrapper that lazily opens + closes a pio monitor."""

    def __init__(self, port: str, env: str | None = None) -> None:
        self._port = port
        self._env = env
        self._session = None
        self._last_cursor: int | None = None

    def start(self) -> None:
        self._session = serial_session.open_session(port=self._port, env=self._env)

    def snapshot(self, max_lines: int = 500) -> list[str]:
        if self._session is None:
            return []
        out = serial_session.read_session(self._session, max_lines=max_lines, since_cursor=0)
        return out.get("lines", [])

    def stop(self) -> None:
        if self._session is not None:
            try:
                serial_session.close_session(self._session)
            except Exception:
                pass
            self._session = None


@pytest.fixture
def serial_capture(hub_devices: dict[str, str], request: pytest.FixtureRequest) -> Any:
    """Return a `_SerialCapture` factory.

    Usage:
        cap = serial_capture("esp32s3")
        cap.start()
        ... run test ...
        # on failure, serial buffer is attached via pytest_runtest_makereport
    """
    captures: list[_SerialCapture] = []

    def factory(role: str, env: str | None = None) -> _SerialCapture:
        if role not in hub_devices:
            pytest.skip(f"role {role!r} not present on the hub")
        cap = _SerialCapture(port=hub_devices[role], env=env)
        cap.start()
        captures.append(cap)
        request.node._serial_captures = captures  # type: ignore[attr-defined]
        return cap

    yield factory

    for cap in captures:
        cap.stop()


@pytest.fixture
def wait_until() -> Callable[..., Any]:
    """Exponential-backoff polling helper.

    Usage:
        wait_until(lambda: b.node_num in a.iface.nodesByNum, timeout=60)
    """

    def _impl(
        predicate: Callable[[], Any],
        timeout: float = 60.0,
        backoff_start: float = 0.5,
        backoff_max: float = 5.0,
    ) -> Any:
        deadline = time.monotonic() + timeout
        delay = backoff_start
        last: Any = None
        while time.monotonic() < deadline:
            last = predicate()
            if last:
                return last
            time.sleep(delay)
            delay = min(delay * 1.5, backoff_max)
        raise AssertionError(f"predicate did not return truthy within {timeout}s (last={last!r})")

    return _impl


# ---------- Firmware log capture (per-test autouse) -----------------------


@pytest.fixture(scope="session", autouse=True)
def _firmware_log_stream() -> Any:
    """Mirror every `meshtastic.log.line` pubsub event to `tests/fwlog.jsonl`.

    Why this exists: the v1 `_debug_log_buffer` per-test fixture captures
    firmware logs *in memory* for pytest-html failure attachments, but a
    live viewer (``meshtastic-mcp-test-tui``) can't read in-process
    pubsub events from a different process. This fixture adds a
    session-long, durable mirror — one JSON object per line, with
    ``port``, ``ts``, and ``line`` fields — that the TUI tails from a
    worker thread.

    Schema (kept trivially small so the file grows slowly):

        {"ts": 1729100000.123, "port": "/dev/cu.usbmodem1101", "line": "INFO  | ... [SerialConsole] Boot..."}

    The file is truncated at session start (no append across runs — the
    TUI also unlinks it on launch, so double-truncate is deliberate).
    Gitignored via ``mcp-server/.gitignore``.

    Runs alongside ``_debug_log_buffer`` — both subscribe to the same
    pubsub topic; pubsub fans out to every subscriber so there's no
    interference.
    """
    import threading

    from pubsub import pub  # type: ignore[import-untyped]

    out_path = _HERE / "fwlog.jsonl"
    # Truncate at session start. TUI also unlinks on launch; this is the
    # plain-CLI path's turn to start clean.
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("")
    except Exception:
        # Non-fatal: if we can't open the file, the TUI just gets no
        # firmware log stream. Tests still run.
        yield
        return

    lock = threading.Lock()
    fh = out_path.open("a", encoding="utf-8")

    def handler(line: str, interface: Any) -> None:
        # `interface` is the meshtastic SerialInterface; `.devPath`
        # carries the /dev/cu.* we care about. Defensive about missing
        # attribute — the pubsub handler must never raise.
        try:
            port = getattr(interface, "devPath", None) or getattr(interface, "stream", None)
            if port and hasattr(port, "port"):
                port = port.port
            record = {
                "ts": time.time(),
                "port": str(port) if port else None,
                "line": str(line),
            }
            with lock:
                fh.write(json.dumps(record) + "\n")
                fh.flush()
        except Exception:
            # Swallow — firmware log mirroring is best-effort.
            pass

    pub.subscribe(handler, "meshtastic.log.line")
    try:
        yield
    finally:
        try:
            pub.unsubscribe(handler, "meshtastic.log.line")
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _debug_log_buffer(request: pytest.FixtureRequest) -> Any:
    """Per-test capture of `meshtastic.log.line` pubsub events.

    Automatic — every test gets this for free. The pubsub topic fires when
    a connected device has `security.debug_log_api_enabled=True` AND the
    client (us) is talking protobufs over its SerialInterface. `baked_mesh`
    flips the flag on at session start, so every subsequent test that opens
    any SerialInterface (directly via `connect()` or via a
    `ReceiveCollector`) picks up the device's log stream automatically.

    The captured lines are attached to the test's pytest-html failure report
    by `pytest_runtest_makereport`, so mesh/telemetry failures ship with the
    firmware-side log context inline — no separate pio monitor, no
    port-lock conflict.
    """
    import threading as _threading

    from pubsub import pub  # type: ignore[import-untyped]

    lines: list[str] = []
    lock = _threading.Lock()

    def handler(line: str, interface: Any) -> None:
        with lock:
            lines.append(line)

    pub.subscribe(handler, "meshtastic.log.line")
    # Stash a strong ref on the test item so pubsub's weakref doesn't GC
    # the closure before the test ends (same trick ReceiveCollector uses).
    request.node._debug_log_buffer = lines  # type: ignore[attr-defined]
    request.node._debug_log_handler_ref = handler  # type: ignore[attr-defined]
    try:
        yield lines
    finally:
        try:
            pub.unsubscribe(handler, "meshtastic.log.line")
        except Exception:
            pass


# ---------- pytest hooks: report attachments + coverage -------------------


def _run_with_timeout(fn: Callable[[], Any], timeout: float) -> Any:
    """Run `fn()` in a worker thread; raise TimeoutError if it takes > `timeout`s.

    `meshtastic.SerialInterface` construction can hang indefinitely on a
    misconfigured or unresponsive port. pytest-timeout fires from the main
    thread via SIGALRM, which doesn't protect code running inside
    `pytest_runtest_makereport` — that hook runs outside the test's timer. So
    we wrap each device query in a bounded worker.

    Must use a *daemon* thread, NOT a ThreadPoolExecutor: the executor's
    context-manager exit (and its atexit handler) call `shutdown(wait=True)`,
    which JOINS a still-running worker — so a truly-hung `SerialInterface`
    connect makes this function block past `timeout` instead of returning. When
    that block happens inside the makereport hook, pytest-timeout's SIGALRM
    fires mid-hook and crashes the whole session with an INTERNALERROR (losing
    every remaining tier). A daemon thread is abandoned cleanly on timeout — it
    dies with the process and is never joined.
    """
    import threading

    box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:
            box["error"] = exc

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        # The worker is wedged in an uncancellable connect — abandon it (daemon,
        # so it can't block interpreter exit) rather than joining it here.
        raise TimeoutError(f"operation did not complete within {timeout}s")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _attach_ui_captures(item: pytest.Item, report: Any) -> None:
    """Embed per-step UI captures (PNG + OCR) into the pytest-html extras.

    Runs for every UI-tier test on BOTH pass and fail so the HTML report
    always shows the image strip + OCR transcript. Silently no-ops if
    pytest-html isn't installed or the test didn't use `frame_capture`.
    """
    captures = getattr(item, "_ui_captures", None)
    if not captures:
        return
    try:
        from pytest_html import extras as html_extras  # type: ignore[import-untyped]
    except ImportError:
        return

    existing = getattr(report, "extras", None) or []
    extras_list = list(existing)
    for cap in captures:
        png_path = cap.get("png_path")
        label = f"{cap.get('step', '?')}: {cap.get('label', '')}"
        frame = cap.get("frame") or {}
        frame_str = f" — frame {frame.get('idx')} {frame.get('name')!r}" if frame else ""
        if png_path:
            try:
                with open(png_path, "rb") as fh:
                    import base64

                    b64 = base64.b64encode(fh.read()).decode("ascii")
                extras_list.append(html_extras.png(b64, name=f"{label}{frame_str}"))
            except OSError:
                pass
        ocr = (cap.get("ocr_text") or "").strip()
        if ocr:
            extras_list.append(html_extras.text(ocr, name=f"OCR: {label}{frame_str}"))
    report.extras = extras_list  # type: ignore[attr-defined]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Any:
    """On test failure, attach serial capture + device state as report artifacts.

    Hard-bounded by `_run_with_timeout` — if the device is unreachable (stuck
    port, unbaked firmware, dead board), the dump is skipped rather than
    hanging the session.

    For UI-tier tests, also embeds per-step camera captures + OCR on every
    test (pass or fail) so the HTML report shows visual evidence of what
    the device did.
    """
    outcome = yield
    report = outcome.get_result()

    # Attach UI captures on any outcome (pass + fail) — these are the whole
    # point of the UI tier. Do this before the failure-only branch below so
    # passing tests still get their image strip.
    if report.when == "call":
        _attach_ui_captures(item, report)

    if report.when != "call" or report.outcome != "failed":
        return

    extras: list[str] = []

    # Attach firmware log stream captured via the StreamAPI (populated only
    # when the device has security.debug_log_api_enabled=True — baked_mesh
    # flips this on at session start). Cheap and high-signal: last 200 lines
    # of firmware log interleaved with whatever the test was doing.
    log_buffer = getattr(item, "_debug_log_buffer", None)
    if log_buffer:
        extras.append(
            f"--- firmware log stream ({len(log_buffer)} lines, last 200) ---\n"
            + "\n".join(log_buffer[-200:])
        )

    # Attach serial captures (if the test used `serial_capture`)
    caps = getattr(item, "_serial_captures", None)
    if caps:
        for cap in caps:
            try:
                lines = _run_with_timeout(lambda c=cap: c.snapshot(max_lines=2000), 5.0)
            except Exception as exc:
                lines = [f"<serial snapshot failed: {exc!r}>"]
            extras.append(
                f"--- serial capture [{cap._port}] ({len(lines)} lines) ---\n"
                + "\n".join(lines[-200:])
            )

    # Dump device state for any role in hub_devices (if the fixture was used).
    # Each query is bounded to 6s; if the device is wedged, skip the dump for
    # that role rather than hanging the pytest session.
    hub_fixture = item.funcargs.get("hub_devices") if hasattr(item, "funcargs") else None
    if hub_fixture:
        for role, port in hub_fixture.items():
            state: dict[str, Any] = {"role": role, "port": port}
            try:
                state["device_info"] = _run_with_timeout(
                    lambda p=port: info.device_info(port=p, timeout_s=4.0), 6.0
                )
            except Exception as exc:
                state["device_info_error"] = repr(exc)
            try:
                state["config"] = _run_with_timeout(
                    lambda p=port: admin.get_config(section="lora", port=p), 6.0
                )
            except Exception as exc:
                state["config_error"] = repr(exc)
            extras.append(
                f"--- device state [{role}] ---\n{json.dumps(state, indent=2, default=str)}"
            )

    if extras:
        # Attach to pytest-html via `report.sections`; pytest-html renders these
        report.sections.append(("Meshtastic debug", "\n\n".join(extras)))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Emit `tool_coverage.json` at session end."""
    out_path = pathlib.Path(__file__).parent / "tool_coverage.json"
    tool_coverage.write_report(out_path)


# Activate the tool-coverage tracker at import time so imports in fixtures are
# also counted.
tool_coverage.install()
