# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Vanity node identities: grind a NodeNum / app colour, then adopt it.

On a PKI firmware build a node's identity is not assigned, it is *derived*::

    public_key  = X25519(private_key, 9)
    my_node_num = crc32(public_key)          # NodeDB.cpp::createNewIdentity
    node id     = "!%08x" % my_node_num
    app colour  = the low 24 bits of that number, read straight as RGB

Both steps are one-way, so a chosen id (or colour) means searching the
keypair space. That search is what `mvgrind <https://github.com/miketweaver/mvgrind>`_
does on the GPU; this module drives it, verifies every hit independently on
the CPU, and writes the winning key to a device.

Two halves, deliberately split:

- **grind** (`grind_start`/`grind_poll`/`grind_stop`) needs the ``mvgrind``
  binary, so it is a gated capability.
- **describe/apply** (`describe_key`, `apply_key`) need nothing but the core
  deps — a key ground on some other machine (or on a friend's GPU) can be
  inspected and adopted here.

The X25519 and CRC-32 used for verification are computed here from scratch
(RFC 7748 ladder + `zlib.crc32`), sharing no code with the grinder — so a
broken kernel cannot talk this module into writing a key that does not
actually produce the advertised node id.

**Every result of a grind is private-key material.** Hits land in a 0600 file
under the MCP data dir and are returned inline so they can be applied; treat
both as secrets. See SECURITY.md.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import shutil
import subprocess
import time
import zlib
from pathlib import Path
from typing import Any

from . import config, connection, jobs

# ---------------------------------------------------------------------------
# Curve25519 (RFC 7748 §5) — verification only, one scalarmult per call.
# Deliberately dependency-free: the core must not grow a crypto dep, and an
# independent implementation is the point (see the module docstring).
# ---------------------------------------------------------------------------
_P = 2**255 - 19
_A24 = 121665


class VanityError(RuntimeError):
    """A vanity grind or key-apply could not proceed."""


def clamp(private_key: bytes) -> bytes:
    """Return `private_key` with the X25519 clamp bits forced."""
    b = bytearray(private_key)
    b[0] &= 248
    b[31] &= 127
    b[31] |= 64
    return bytes(b)


def is_clamped(private_key: bytes) -> bool:
    """True when the scalar is already clamped.

    The firmware signs with a clamped copy of the scalar, so an unclamped key
    yields a node whose signatures do not verify against its own public key.
    """
    return private_key[0] & 7 == 0 and private_key[31] & 0xC0 == 0x40


def x25519_public_key(private_key: bytes) -> bytes:
    """Derive the X25519 public key for `private_key` (the base-point mult).

    The scalar is clamped on the way in exactly as RFC 7748 decodeScalar25519
    specifies, so this matches what the firmware's monocypher call produces.
    """
    if len(private_key) != 32:
        raise VanityError(f"private key must be 32 bytes, got {len(private_key)}")
    k = int.from_bytes(clamp(private_key), "little")
    x1 = 9
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in range(254, -1, -1):
        bit = (k >> t) & 1
        swap ^= bit
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = bit
        a = (x2 + z2) % _P
        aa = a * a % _P
        b = (x2 - z2) % _P
        bb = b * b % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = d * a % _P
        cb = c * b % _P
        x3 = pow(da + cb, 2, _P)
        z3 = x1 * pow(da - cb, 2, _P) % _P
        x2 = aa * bb % _P
        z2 = e * ((aa + _A24 * e) % _P) % _P
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


# ---------------------------------------------------------------------------
# Identity derivation — the firmware's own formula, mirrored
# ---------------------------------------------------------------------------
def nodenum_of_public_key(public_key: bytes) -> int:
    """`crc32(public_key)` — the firmware's NodeNum (NodeDB.cpp::createNewIdentity).

    `crc32Buffer()` there is ErriezCRC32, i.e. plain CRC-32/IEEE — `zlib.crc32`.
    """
    if len(public_key) != 32:
        raise VanityError(f"public key must be 32 bytes, got {len(public_key)}")
    return zlib.crc32(public_key) & 0xFFFFFFFF


def node_id(nodenum: int) -> str:
    """The `!8adc143c` form the apps and the firmware print."""
    return f"!{nodenum & 0xFFFFFFFF:08x}"


def node_color(nodenum: int) -> dict[str, Any]:
    """The colour the apps paint this node, from the low 24 bits read as RGB.

    Mirrors `nodeColorsFromNum` (Meshtastic-Android `NodeColors.kt`); the Apple
    client agrees. `foreground` is the black/white the apps pick for legibility
    against that background — worth knowing before committing to a colour.
    """
    r = (nodenum >> 16) & 0xFF
    g = (nodenum >> 8) & 0xFF
    b = nodenum & 0xFF
    brightness = (r * 0.299 + g * 0.587 + b * 0.114) / 255
    return {
        "hex": f"#{r:02x}{g:02x}{b:02x}",
        "rgb": [r, g, b],
        "brightness": round(brightness, 4),
        "foreground": "black" if brightness > 0.5 else "white",
    }


def parse_private_key(text: str) -> bytes:
    """Accept a 64-char hex or base64 private key (mvgrind prints both)."""
    s = text.strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", s):
        return bytes.fromhex(s)
    try:
        raw = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VanityError("private key must be 64 hex chars or base64 of 32 bytes") from exc
    if len(raw) != 32:
        raise VanityError(f"private key must decode to 32 bytes, got {len(raw)}")
    return raw


def describe_key(private_key: str) -> dict[str, Any]:
    """What node id and colour this private key produces. Pure, no device I/O.

    Use it to check a key before `apply_key` burns it into a radio, or to
    inspect a key someone else ground.
    """
    sk = parse_private_key(private_key)
    pk = x25519_public_key(sk)
    num = nodenum_of_public_key(pk)
    return {
        "node_id": node_id(num),
        "nodenum": num,
        "color": node_color(num),
        "public_key_hex": pk.hex(),
        "public_key_b64": base64.b64encode(pk).decode(),
        "clamped": is_clamped(sk),
    }


# ---------------------------------------------------------------------------
# mvgrind — the GPU grinder (capability-gated)
# ---------------------------------------------------------------------------
MVGRIND_ENV = "MESHTASTIC_MCP_MVGRIND"
DEFAULT_TIMEOUT_S = 900.0

# A pattern is hex nibbles, wildcards, and comma-separated alternatives; a
# colour is #rgb/#rrggbb or a CSS name. Both are validated before they reach
# argv so a caller-supplied string can never be read as an mvgrind flag.
_PATTERN_RE = re.compile(r"^!?[0-9a-fA-F*?.]{1,8}(,!?[0-9a-fA-F*?.]{1,8})*$")
_COLOR_RE = re.compile(r"^(#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}|[a-zA-Z]{3,24})$")
_PROGRESS_RE = re.compile(r"^\s*\d+ keys\s")


def mvgrind_bin() -> str | None:
    """Resolve the `mvgrind` binary: `$MESHTASTIC_MCP_MVGRIND` → PATH."""
    override = os.environ.get(MVGRIND_ENV)
    if override:
        p = Path(override).expanduser()
        return str(p) if p.is_file() and os.access(p, os.X_OK) else None
    return shutil.which("mvgrind")


def available() -> bool:
    """True when the grinder is usable (the `mvgrind` capability)."""
    return mvgrind_bin() is not None


def _require_mvgrind() -> str:
    binary = mvgrind_bin()
    if binary is None:
        raise VanityError(
            "mvgrind not found. Build it "
            "(git clone --recursive https://github.com/miketweaver/mvgrind && cd mvgrind && make), "
            f"then put it on PATH or set ${MVGRIND_ENV} to the binary. Run `doctor` for the "
            "platform-specific command."
        )
    return binary


def parse_hits(out_path: Path) -> list[dict[str, Any]]:
    """Parse mvgrind's `--out` file into hits, verifying each one here.

    The file is blank-line-separated `key=value` blocks. Every hit is
    re-derived with this module's own X25519 + CRC-32: `verified` is false
    when the key does not actually produce the advertised id, which is a
    grinder bug, not a near miss — such a hit must not be applied.
    """
    if not out_path.exists():
        return []
    hits: list[dict[str, Any]] = []
    for block in out_path.read_text(encoding="utf-8", errors="replace").split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        raw_sk = fields.get("private_key_hex")
        if not raw_sk:
            continue
        try:
            sk = parse_private_key(raw_sk)
            desc = describe_key(raw_sk)
        except VanityError:
            continue
        claimed_id = fields.get("node_id", "")
        hits.append(
            {
                **desc,
                "private_key_hex": sk.hex(),
                "private_key_b64": base64.b64encode(sk).decode(),
                "verified": claimed_id == desc["node_id"] and desc["clamped"],
                "reported_node_id": claimed_id,
            }
        )
    return hits


def _validate(pattern: str | None, color: str | None, tol: int) -> None:
    if pattern is None and color is None:
        raise VanityError("give a pattern (e.g. 'dc80'), a color, or both.")
    if pattern is not None and not _PATTERN_RE.match(pattern):
        raise VanityError(
            f"invalid pattern {pattern!r}: expected hex nibbles with optional "
            "'*'/'?'/'.' wildcards, or a comma-separated set (e.g. 'dc80,801f')."
        )
    if color is not None and not _COLOR_RE.match(color):
        raise VanityError(
            f"invalid color {color!r}: expected '#rgb', '#rrggbb', or a CSS color name."
        )
    if not 0 <= tol <= 255:
        raise VanityError(f"tol must be 0-255, got {tol}")


def grind_start(
    pattern: str | None = None,
    color: str | None = None,
    tol: int = 0,
    count: int = 1,
    device: int | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Launch a GPU grind in the background and return a `job_id` immediately.

    A grind runs far past the MCP request timeout (a full 8-digit id is ~25 s
    on a discrete GPU but minutes on weaker OpenCL), so it follows the
    `build_start`/`build_poll` pattern. Poll with `grind_poll`.

    `pattern` constrains the node id (`"dc80"` prefix, `"dc80****"` wildcards,
    `"dc80,801f"` a set, a full 8 digits for an exact id); `color` constrains
    the app colour (`"crimson"`, `"#dc143c"`); `tol` widens the colour by ±N
    per channel, which costs nothing and lands a hit far sooner. The two share
    bits — id nibbles 3-8 *are* the colour channels — so mvgrind rejects an
    impossible combination up front rather than grinding forever; that message
    is surfaced verbatim in the job log.

    `timeout_s=0` runs unbounded (stop it with `grind_stop`).
    """
    binary = _require_mvgrind()
    _validate(pattern, color, tol)
    if count < 1:
        raise VanityError("count must be >= 1 (an unbounded grind is what timeout_s=0 is for)")

    label = " ".join(x for x in [pattern, f"--color {color}" if color else None] if x)
    out_dir = jobs.data_dir("grinds")
    argv = [binary]
    if pattern:
        argv.append(pattern)
    if color:
        argv.extend(["--color", color])
        if tol:
            argv.extend(["--tol", str(tol)])
    if device is not None:
        argv.extend(["--device", str(device)])
    argv.extend(["--count", str(count)])

    def _body(state: dict[str, Any], log_path: Path) -> None:
        out_path = out_dir / f"{state['job_id']}.keys"
        _touch_private(out_path)
        _touch_private(log_path)
        with jobs.LOCK:
            state["out_path"] = str(out_path)
        started = time.time()
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"$ {' '.join(argv)} --out {out_path}\n")
            log.flush()
            # argv is validated above and passed as a list — no shell.
            proc = subprocess.Popen(
                [*argv, "--out", str(out_path)],
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            with jobs.LOCK:
                state["pid"] = proc.pid
            try:
                rc = proc.wait(timeout=timeout_s or None)
                status = "done" if rc == 0 else "failed"
                with jobs.LOCK:
                    stop_requested = bool(state.get("stop_requested"))
                if rc != 0 and stop_requested:
                    status = "stopped"
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    rc = proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    rc = proc.wait()
                status = "timeout"
        hits = parse_hits(out_path)
        if not hits:  # don't leave an empty key file behind for a failed grind
            out_path.unlink(missing_ok=True)
        with jobs.LOCK:
            state["status"] = "done" if (status == "timeout" and hits) else status
            state["exit_code"] = rc
            state["finished_at"] = time.time()
            state["duration_s"] = round(time.time() - started, 2)
            state["artifacts"] = [str(out_path)]
            state["hits"] = hits
            if status == "timeout":
                state["timed_out"] = True

    out = jobs.start("grinds", label, _body)
    out["pattern"] = pattern
    out["color"] = color
    out["tol"] = tol
    return out


def _touch_private(path: Path) -> None:
    """Create `path` mode 0600 — it will hold private-key material."""
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    os.close(fd)


def grind_poll(job_id: str, tail_lines: int = 12) -> dict[str, Any]:
    """Status of a background grind, plus every verified hit found so far.

    **The `hits` carry private keys.** Feed one to `apply_key` (or save it);
    do not paste it anywhere public. mvgrind draws progress with carriage
    returns, so `log_tail` is normalised to lines here.
    """
    out = jobs.poll(job_id, tail_lines=1)
    if "job_id" not in out:  # unknown id — jobs.poll returns a bare {"error": ...}
        return out
    state = jobs.state_of(job_id) or {}
    log_path = Path(out["log_path"])
    if log_path.exists():
        text = log_path.read_text(encoding="utf-8", errors="replace").replace("\r", "\n")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        # Progress is redrawn thousands of times; keep only the newest one.
        keep = [ln for ln in lines if not _PROGRESS_RE.match(ln)]
        last_progress = next((ln for ln in reversed(lines) if _PROGRESS_RE.match(ln)), None)
        if last_progress:
            keep.append(last_progress.strip())
        out["log_tail"] = keep[-tail_lines:]
    with jobs.LOCK:
        out_path = state.get("out_path")
        out["hits"] = state.get("hits")
        out["timed_out"] = state.get("timed_out", False)
    # A running grind can already have written a hit (--count > 1); read the
    # file rather than making the caller wait for the whole job to finish.
    if out["hits"] is None:
        out["hits"] = parse_hits(Path(out_path)) if out_path else []
    out["out_path"] = out_path
    out["spec"] = out.pop("label", None)  # what was asked for, as passed to mvgrind
    return out


def grind_stop(job_id: str) -> dict[str, Any]:
    """Stop a running grind. Hits already written are kept and returned."""
    state = jobs.state_of(job_id)
    if state is None:
        return {"error": f"Unknown job_id {job_id!r} (only this session's jobs are tracked)."}
    with jobs.LOCK:
        pid = state.get("pid")
        running = state.get("status") == "running"
    if not running or not pid:
        return {"ok": True, "stopped": False, **grind_poll(job_id)}
    with jobs.LOCK:
        state["stop_requested"] = True
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        pass
    # The worker thread owns the state transition; give it a moment to land.
    for _ in range(20):
        if jobs.poll(job_id, tail_lines=0).get("status") != "running":
            break
        time.sleep(0.25)
    return {"ok": True, "stopped": True, **grind_poll(job_id)}


# ---------------------------------------------------------------------------
# Adopting a ground key — the identity change
# ---------------------------------------------------------------------------
def apply_key(
    private_key: str,
    port: str | None = None,
    confirm: bool = False,
    verify: bool = True,
    verify_timeout_s: float = 75.0,
) -> dict[str, Any]:
    """Write a vanity private key to a device, moving it to the matching NodeNum.

    This **changes the node's identity**. The old NodeNum is removed from its
    own DB, peers keep DMing the old key until they see the new NodeInfo, and
    any admin key or channel binding that named the old node has to be
    re-pointed. It is not a reversible edit unless you kept the old key.

    The write is a `security` config set carrying the new `private_key` with
    `public_key` **cleared** — that is what makes the firmware re-derive the
    public key (`AdminModule.cpp`: it only calls `generateCryptoKeyPair()` when
    the public key is empty) and so recompute `my_node_num`. Echoing back the
    old 32-byte public key silently skips both. The firmware reboots itself
    (~7 s) to commit; with `verify` we reconnect afterwards and confirm the
    node actually landed on the expected number.
    """
    if not confirm:
        raise VanityError(
            "apply_key changes the node's identity (NodeNum, public key, and the "
            "colour every app paints it) and requires confirm=True."
        )
    sk = parse_private_key(private_key)
    if not is_clamped(sk):
        raise VanityError(
            "private key is not clamped. The firmware signs with a clamped copy of "
            "the scalar, so an unclamped key produces a node whose signatures do not "
            "verify. mvgrind only emits clamped keys — re-check where this one came from."
        )
    expected = describe_key(private_key)

    with connection.connect(port=port) as iface:
        node = iface.localNode
        region = node.localConfig.lora.region
        if region == 0:  # RegionCode.UNSET
            raise VanityError(
                "lora.region is UNSET: the firmware refuses key generation until a "
                "region is set (NodeDB.cpp::generateCryptoKeyPair), so this write "
                "would silently leave the identity unchanged. Set the region first."
            )
        before = getattr(iface, "myInfo", None)
        before_num = int(getattr(before, "my_node_num", 0) or 0)
        if before_num == expected["nodenum"]:
            return {
                "ok": True,
                "changed": False,
                "reason": "device already holds this identity",
                **expected,
            }
        sec = node.localConfig.security
        sec.private_key = sk
        sec.public_key = b""  # forces the firmware to re-derive it — see docstring
        node.writeConfig("security")

    result: dict[str, Any] = {
        "ok": True,
        "changed": True,
        "previous_node_id": node_id(before_num) if before_num else None,
        "rebooting": True,
        **expected,
    }
    if not verify:
        result["verified"] = None
        result["note"] = "device reboots in ~7 s; re-read device_info to confirm the new node id."
        return result

    result.update(_verify_identity(port, expected["nodenum"], verify_timeout_s))
    return result


def _verify_identity(port: str | None, expected_num: int, timeout_s: float) -> dict[str, Any]:
    """Reconnect after the self-reboot and read back `my_node_num`.

    Also the empirical PKI check: a build without PKI keygen never moves its
    NodeNum, and shows up here as a mismatch rather than as version archaeology.
    """
    deadline = time.time() + timeout_s
    time.sleep(min(10.0, timeout_s))  # the firmware reboots ~7 s after saveChanges
    last_error = ""
    while time.time() < deadline:
        try:
            with connection.connect(port=port) as iface:
                actual = int(getattr(iface.myInfo, "my_node_num", 0) or 0)
            if actual == expected_num:
                return {"verified": True, "node_id_on_device": node_id(actual)}
            if actual:
                return {
                    "verified": False,
                    "node_id_on_device": node_id(actual),
                    "note": (
                        "the node came back on a different NodeNum. Either this build "
                        "excludes PKI keygen (MESHTASTIC_EXCLUDE_PKI_KEYGEN), or the "
                        "security write did not take."
                    ),
                }
        except Exception as exc:  # port is gone while it reboots — expected
            last_error = str(exc)
        time.sleep(3.0)
    return {
        "verified": None,
        "note": (
            f"could not reconnect within {timeout_s:.0f}s to confirm the new node id"
            + (f" (last error: {last_error})" if last_error else "")
            + ". Re-run device_info once the board is back."
        ),
    }


def data_dir() -> Path:
    """Where ground keys land (0600 files). Exposed for `doctor`/docs."""
    return config.mcp_data_dir() / "grinds"
