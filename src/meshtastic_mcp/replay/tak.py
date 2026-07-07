# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Optional TAKPacketV2 wire-format support (the ``[tak]`` extra).

Thin, import-guarded wrapper over the **meshtastic-tak** SDK
(`meshtastic/TAKPacket-SDK`), which owns CoT ↔ TAKPacketV2 conversion and the
zstd *dictionary* compression that shrinks a TAK payload to a LoRa-sized blob
(median ~87 B, max ~184 B). The replay core must import and run without the SDK,
so every SDK import lives behind :func:`available`; when the extra is installed
the sim can emit real wire-compressed TAKPacketV2 payloads instead of the
legacy uncompressed :class:`atak_pb2.TAKPacket`, for high-fidelity exercise of
an app's ATAK plane.

Wire format (from the SDK): a 1-byte dictionary id followed by the zstd body
(or the raw protobuf when incompressible). :func:`compress` / :func:`decompress`
round-trip a ``TAKPacketV2`` through it; a payload produced here decompresses in
any other SDK implementation (Kotlin/Swift/TS/C#) and vice-versa.

Install: ``uv tool install 'meshtastic-mcp[tak]'`` (pulls the SDK + zstandard).
"""

from __future__ import annotations

import functools
from typing import Any


def available() -> bool:
    """True when the meshtastic-tak SDK is importable (the ``[tak]`` extra)."""
    try:
        import meshtastic_tak  # noqa: F401

        return True
    except Exception:
        return False


def _require() -> None:
    if not available():
        raise RuntimeError(
            "TAKPacketV2 wire format requires the [tak] extra: "
            "install 'meshtastic-mcp[tak]' (pulls meshtastic-tak + zstandard)."
        )


@functools.lru_cache(maxsize=1)
def _compressor() -> Any:
    _require()
    from meshtastic_tak import TakCompressor

    return TakCompressor()


# CoT type strings the bridge (SDK CotXmlBuilder) stamps onto the emitted CoT so
# ATAK/iTAK render the right affiliation: a friendly-ground PLI, and a GeoChat.
PLI_COT_TYPE = "a-f-G-U-C"
CHAT_COT_TYPE = "b-t-f"


def build_pli(
    *,
    callsign: str,
    uid: str,
    team: int,
    role: int,
    lat_i: int,
    lon_i: int,
    altitude: int,
    speed: int,
    course: int,
    battery: int,
    device_callsign: str = "",
    cot_type: str = PLI_COT_TYPE,
) -> Any:
    """Build a TAKPacketV2 position/location-information report.

    ``cot_type`` is carried so the bridged CoT event is typed (friendly ground
    by default) rather than emitting an empty ``type=""`` that clients render
    as an unknown affiliation.
    """
    _require()
    from meshtastic_tak import atak_pb2 as v2

    tp = v2.TAKPacketV2()
    tp.callsign = callsign
    tp.uid = uid
    tp.device_callsign = device_callsign or callsign
    tp.team = team
    tp.role = role
    tp.latitude_i = lat_i
    tp.longitude_i = lon_i
    tp.altitude = altitude
    tp.speed = speed
    tp.course = course
    tp.battery = battery
    tp.cot_type_str = cot_type
    return tp


def build_chat(
    *,
    callsign: str,
    uid: str,
    team: int,
    role: int,
    battery: int,
    message: str,
    to: str = "All Chat Rooms",
) -> Any:
    """Build a TAKPacketV2 GeoChat message."""
    _require()
    from meshtastic_tak import atak_pb2 as v2

    tp = v2.TAKPacketV2()
    tp.callsign = callsign
    tp.uid = uid
    tp.device_callsign = callsign
    tp.team = team
    tp.role = role
    tp.battery = battery
    tp.cot_type_str = CHAT_COT_TYPE
    tp.chat.message = message
    tp.chat.to = to
    return tp


def compress(packet: Any) -> bytes:
    """Compress a TAKPacketV2 to its dictionary-zstd wire payload."""
    return bytes(_compressor().compress(packet))


def decompress(wire: bytes) -> Any:
    """Decompress a wire payload back to a TAKPacketV2."""
    return _compressor().decompress(wire)


def parse_cot(cot_xml: str, *, strip_for_mesh: bool = True) -> Any:
    """Parse a CoT XML event (as ATAK/iTAK author it) into a TAKPacketV2.

    The inbound half of the bridge (TAK client → mesh): when ``strip_for_mesh``
    is set, non-essential detail is removed first (mirroring the app's
    CoTDetailStripper) so the result fits the LoRa MTU.
    """
    _require()
    from meshtastic_tak import CotXmlParser, strip_non_essential_for_mesh

    if strip_for_mesh:
        cot_xml = strip_non_essential_for_mesh(cot_xml)
    return CotXmlParser().parse(cot_xml)


def cot_to_wire(cot_xml: str, *, strip_for_mesh: bool = True) -> bytes:
    """Convert an ATAK/iTAK-authored CoT event to a mesh TAKPacketV2 wire payload.

    What an app's TAK server does when a connected client sends a marker/PLI/
    GeoChat: parse → (strip) → compress. The bytes belong on portnum 78
    (``ATAK_PLUGIN_V2``).
    """
    return compress(parse_cot(cot_xml, strip_for_mesh=strip_for_mesh))
