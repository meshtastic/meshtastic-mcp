# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""nRF Sniffer pcap dissection, resolution, and capture-argv construction.

The two `FRAME_*` blobs are **verbatim bytes from a live capture**, cross-checked
field-for-field against `tshark -V` on the same file before being pasted here:
one nRF52-class advertiser broadcasting a name, one Meshtastic ESP32 node
broadcasting the mesh service UUID. They are the ground truth that keeps the
hand-rolled `struct` parser honest — everything else in this file is built from
their metadata template.

No test here starts a real capture: `capture_start` spawns a subprocess that
holds a serial port, so the covered surface is the pure parts (`_build_argv`,
the parser, resolution) plus the guards, with `jobs` stubbed.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from meshtastic_mcp import ble_sniffer

# ADV_IND from 68:12:68:10:d1:57, complete local name "MST_VNSE3_d157", CRC OK,
# channel 37, RSSI -86 dBm. Name in the advertisement, no service UUID.
FRAME_NAMED = bytes.fromhex(
    "1b2c0003ed03020a0125560000c1f4420b"  # sniffer pseudo-header (protover 3)
    "d6be898e"  # advertising access address
    "0019"  # PDU header: ADV_IND, public address, 25-byte payload
    "57d110681268"  # AdvA, little-endian
    "020106"  # AD: flags
    "0f094d53545f564e5345335f64313537"  # AD: complete local name
    "9a7e79"  # CRC
)

# ADV_IND from c8:54:dd:59:2e:32 (Espressif OUI — a Meshtastic ESP32 node),
# complete list of 128-bit service UUIDs = the mesh service, CRC OK, channel 38,
# RSSI -83 dBm. Service UUID in the advertisement, no name: the NimBLE/Bluefruit
# split this module exists to reconcile.
FRAME_MESH_UUID = bytes.fromhex(
    "1b2e00033400020a01265300006a0e8d0a"  # sniffer pseudo-header (protover 3)
    "d6be898e"  # advertising access address
    "401b"  # PDU header: ADV_IND, random address, 27-byte payload
    "322e59dd54c8"  # AdvA, little-endian
    "020106"  # AD: flags
    "1107fdea73e2ca5da89f1f46a81518b2a16b"  # AD: complete 128-bit service UUIDs
    "82a5ea"  # CRC
)

MESH_ADDRESS = "c8:54:dd:59:2e:32"
NAMED_ADDRESS = "68:12:68:10:d1:57"


def _record(payload: bytes, *, ts_sec: int = 1789916557, ts_usec: int = 0) -> bytes:
    """A big-endian pcap record header + payload, matching what nrfutil writes."""
    return struct.pack(">IIII", ts_sec, ts_usec, len(payload), len(payload)) + payload


def _pcap(*payloads: bytes, linktype: int = ble_sniffer.LINKTYPE_NORDIC_BLE) -> bytes:
    header = struct.pack(">IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, linktype)
    return header + b"".join(_record(p, ts_usec=i * 1000) for i, p in enumerate(payloads))


def _write(tmp_path: Path, *payloads: bytes, **kw) -> Path:
    path = tmp_path / "capture.pcap"
    path.write_bytes(_pcap(*payloads, **kw))
    return path


def _nordic(ble: bytes, *, crc_ok: bool = True, channel: int = 37, rssi: int = 80) -> bytes:
    """Wrap a BLE link-layer frame in a protover-3 nRF Sniffer pseudo-header."""
    meta = bytes(
        [
            0x1B,  # board id
            0,
            0,  # payload length (recomputed below)
            3,  # protocol version
            0,
            0,  # packet counter
            0x02,  # packet id: EVENT_PACKET_ADVERTISING
            10,  # metadata block length
            0x01 if crc_ok else 0x00,  # flags: bit 0 = CRC OK
            channel,
            rssi,  # RSSI magnitude
            0,
            0,  # event counter
            0,
            0,
            0,
            0,  # timestamp
        ]
    )
    body = meta + ble
    return body[:1] + struct.pack("<H", len(body) - 7) + body[3:]


def _adv(pdu_type: int, address: bytes, ad: bytes = b"", *, tx_random: bool = False) -> bytes:
    payload = address + ad
    header = pdu_type | (0x40 if tx_random else 0x00)
    return (
        struct.pack("<I", ble_sniffer.ADV_ACCESS_ADDRESS)
        + bytes([header, len(payload)])
        + payload
        + b"\x00\x00\x00"  # CRC (not checked here; the sniffer flag is authoritative)
    )


def _name_ad(name: str) -> bytes:
    raw = name.encode()
    return bytes([len(raw) + 1, 0x09]) + raw


# ---------------------------------------------------------------------------
# Ground truth: the real frames
# ---------------------------------------------------------------------------


def test_real_named_advertisement_parses_to_tshark_values(tmp_path):
    (pkt,) = list(ble_sniffer.iter_packets(_write(tmp_path, FRAME_NAMED)))
    assert pkt.crc_ok is True
    assert pkt.channel == 37
    assert pkt.rssi_dbm == -86
    assert pkt.protover == 3
    assert pkt.is_advertising
    assert pkt.pdu_name == "ADV_IND"
    assert pkt.address == NAMED_ADDRESS
    assert pkt.address_random is False
    assert pkt.name == "MST_VNSE3_d157"
    assert pkt.service_uuids == ()


def test_real_mesh_advertisement_yields_the_service_uuid(tmp_path):
    (pkt,) = list(ble_sniffer.iter_packets(_write(tmp_path, FRAME_MESH_UUID)))
    assert pkt.channel == 38
    assert pkt.rssi_dbm == -83
    assert pkt.address == MESH_ADDRESS
    assert pkt.name is None, "this node advertises no name — only the service UUID"
    assert pkt.service_uuids == (ble_sniffer.MESH_SERVICE_UUID,)


def test_mesh_service_uuid_matches_the_meshtastic_library():
    """The literal here is a copy of the firmware/library constant. If upstream
    ever changes it, `likely_meshtastic` silently stops matching — catch it here."""
    from meshtastic.ble_interface import SERVICE_UUID

    assert ble_sniffer.MESH_SERVICE_UUID == SERVICE_UUID


def test_summary_flags_the_mesh_node_and_not_the_other(tmp_path):
    pcap = _write(tmp_path, FRAME_NAMED, FRAME_MESH_UUID)
    summary = ble_sniffer.summarize(pcap)
    assert summary["packets"] == 2
    assert summary["crc_ok"] == 2
    assert summary["crc_bad"] == 0
    assert summary["advertiser_count"] == 2
    assert summary["meshtastic_advertisers"] == [MESH_ADDRESS]
    rows = {r["address"]: r for r in summary["advertisers"]}
    assert rows[MESH_ADDRESS]["likely_meshtastic"] is True
    assert rows[NAMED_ADDRESS]["likely_meshtastic"] is False, (
        "a name must never be enough to call something a Meshtastic node"
    )
    # Strongest signal first: -83 beats -86.
    assert summary["advertisers"][0]["address"] == MESH_ADDRESS


# ---------------------------------------------------------------------------
# The merge that makes an nRF52 node readable
# ---------------------------------------------------------------------------


def test_name_from_scan_response_merges_with_uuid_from_advertisement(tmp_path):
    """An nRF52 Meshtastic node puts the service UUID in ADV_IND and the name in
    the SCAN_RSP alone (`Advertising.addName()` is commented out upstream), so the
    two must be stitched together per address or the node reads as nameless."""
    address = bytes.fromhex("57d110681268")
    scan_rsp = _nordic(_adv(0x04, address, _name_ad("Meshtastic_d157")), channel=37, rssi=84)
    pcap = _write(tmp_path, FRAME_NAMED, scan_rsp)
    (row,) = ble_sniffer.summarize(pcap)["advertisers"]
    assert row["address"] == NAMED_ADDRESS
    assert set(row["names"]) == {"MST_VNSE3_d157", "Meshtastic_d157"}
    assert row["pdu_types"] == ["ADV_IND", "SCAN_RSP"]
    assert row["packets"] == 2


def test_scan_req_contributes_the_address_but_never_its_type(tmp_path):
    """A SCAN_REQ is sent by the *scanner*, so its TxAdd describes the scanner and
    only its RxAdd claims anything about the advertiser. Measured on real air,
    scanners set RxAdd=0 even when probing a random-static advertiser, which
    reported a random node as public. So a SCAN_REQ attributes the packet to its
    target (for the connect/probe counts) but must not set the address type —
    only PDUs the advertiser itself sent may do that."""
    scanner = bytes.fromhex("aabbccddeeff")
    advertiser = bytes.fromhex("322e59dd54c8")
    # TxAdd=1 (scanner random), RxAdd=0 — the misreport seen live.
    scan_req = _nordic(
        struct.pack("<I", ble_sniffer.ADV_ACCESS_ADDRESS)
        + bytes([0x03 | 0x40, 12])
        + scanner
        + advertiser
        + bytes(3)  # CRC
    )
    (pkt,) = list(ble_sniffer.iter_packets(_write(tmp_path, scan_req)))
    assert pkt.address == MESH_ADDRESS, "attributed to the node being probed"
    assert pkt.address_random is None, "a scanner's RxAdd is not evidence"

    # Merged with the node's own ADV_IND (header 0x40 = random), the advertiser's
    # own claim is what survives.
    (row,) = ble_sniffer.summarize(_write(tmp_path, scan_req, FRAME_MESH_UUID))["advertisers"]
    assert row["address_type"] == "random"
    assert "SCAN_REQ" in row["pdu_types"]


def test_connect_request_is_attributed_to_the_advertiser(tmp_path):
    """CONNECT_IND payload is InitA then AdvA — the interesting party is the node
    being connected to, not the phone doing it."""
    initiator = bytes.fromhex("aabbccddeeff")
    advertiser = bytes.fromhex("322e59dd54c8")
    connect = _nordic(_adv(0x05, initiator + advertiser))
    pcap = _write(tmp_path, FRAME_MESH_UUID, connect)
    (row,) = ble_sniffer.summarize(pcap)["advertisers"]
    assert row["address"] == MESH_ADDRESS
    assert row["connect_requests"] == 1
    assert "CONNECT_IND" in row["pdu_types"]


# ---------------------------------------------------------------------------
# Robustness: a sniffer sees corrupt and unparseable traffic by design
# ---------------------------------------------------------------------------


def test_crc_failures_are_counted_but_never_become_advertisers(tmp_path):
    """A flipped bit in a name would otherwise invent a device that does not exist."""
    corrupt = _nordic(_adv(0x00, bytes.fromhex("010203040506"), _name_ad("gh0st")), crc_ok=False)
    summary = ble_sniffer.summarize(_write(tmp_path, FRAME_MESH_UUID, corrupt))
    assert summary["packets"] == 2
    assert summary["crc_ok"] == 1
    assert summary["crc_bad"] == 1
    assert summary["advertiser_count"] == 1
    assert summary["advertisers"][0]["address"] == MESH_ADDRESS


def test_data_channel_pdu_is_counted_not_dissected(tmp_path):
    """A non-advertising access address means an established connection: different
    header layout, no AdvA to read."""
    data_pdu = _nordic(struct.pack("<I", 0x12345678) + bytes([0x02, 0x04]) + b"\xde\xad\xbe\xef")
    summary = ble_sniffer.summarize(_write(tmp_path, data_pdu))
    assert summary["packets"] == 1
    assert summary["data_channel_packets"] == 1
    assert summary["advertisers"] == []


def test_malformed_ad_length_does_not_raise(tmp_path):
    """An AD length byte claiming more bytes than exist must stop the walk, not
    explode a capture poll."""
    lying = bytes([0x40, 0x09]) + b"short"
    pkt = _nordic(_adv(0x00, bytes.fromhex("010203040506"), lying))
    summary = ble_sniffer.summarize(_write(tmp_path, pkt))
    assert summary["advertiser_count"] == 1
    assert summary["advertisers"][0]["names"] == []


def test_non_utf8_name_is_replaced_not_raised(tmp_path):
    raw = b"\xff\xfe\x41"
    ad = bytes([len(raw) + 1, 0x09]) + raw
    pkt = _nordic(_adv(0x00, bytes.fromhex("010203040506"), ad))
    (row,) = ble_sniffer.summarize(_write(tmp_path, pkt))["advertisers"]
    assert row["names"] and "A" in row["names"][0]


def test_torn_final_record_ends_iteration(tmp_path):
    """`capture_poll` parses a file nrfutil is still appending to."""
    path = tmp_path / "c.pcap"
    path.write_bytes(_pcap(FRAME_MESH_UUID) + _record(FRAME_NAMED)[:20])
    assert len(list(ble_sniffer.iter_packets(path))) == 1


def test_unknown_protover_is_skipped_not_misparsed(tmp_path):
    legacy = bytearray(_nordic(_adv(0x00, bytes.fromhex("010203040506"))))
    legacy[3] = 2  # protover 2 puts flags/channel/RSSI elsewhere
    summary = ble_sniffer.summarize(_write(tmp_path, bytes(legacy)))
    assert summary["packets"] == 0, "a layout we cannot read must be dropped, not guessed at"


def test_wrong_link_type_is_rejected(tmp_path):
    path = tmp_path / "wrong.pcap"
    path.write_bytes(_pcap(FRAME_NAMED, linktype=1))  # LINKTYPE_ETHERNET
    with pytest.raises(ble_sniffer.BleSnifferError, match="link type"):
        list(ble_sniffer.iter_packets(path))


def test_missing_pcap_summarizes_empty(tmp_path):
    summary = ble_sniffer.summarize(tmp_path / "never-created.pcap")
    assert summary["packets"] == 0
    assert summary["advertisers"] == []


def test_advertisers_are_truncated_with_a_flag(tmp_path):
    frames = [_nordic(_adv(0x00, bytes([1, 2, 3, 4, 5, i])), rssi=60 + i) for i in range(10)]
    summary = ble_sniffer.summarize(_write(tmp_path, *frames), max_advertisers=3)
    assert summary["advertiser_count"] == 10
    assert len(summary["advertisers"]) == 3
    assert summary["advertisers_truncated"] is True


# ---------------------------------------------------------------------------
# Resolution and capture arguments
# ---------------------------------------------------------------------------


def test_env_override_resolves_the_plugin_binary(tmp_path, monkeypatch):
    fake = tmp_path / "nrfutil-ble-sniffer"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv(ble_sniffer.SNIFFER_BIN_ENV, str(fake))
    assert ble_sniffer.sniffer_bin() == fake


def test_env_override_pointing_at_nothing_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv(ble_sniffer.SNIFFER_BIN_ENV, str(tmp_path / "absent"))
    with pytest.raises(ble_sniffer.BleSnifferError, match="not an executable file"):
        ble_sniffer.sniffer_bin()


def test_nrfutil_home_is_searched_for_the_plugin(tmp_path, monkeypatch):
    monkeypatch.delenv(ble_sniffer.SNIFFER_BIN_ENV, raising=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    plugin = bin_dir / "nrfutil-ble-sniffer"
    plugin.write_text("#!/bin/sh\n")
    monkeypatch.setenv(ble_sniffer.NRFUTIL_HOME_ENV, str(tmp_path))
    assert ble_sniffer.sniffer_bin() == plugin


def test_resolve_port_without_hardware_explains_itself(monkeypatch):
    monkeypatch.setattr(ble_sniffer, "list_sniffers", lambda: [])
    with pytest.raises(ble_sniffer.BleSnifferError, match="No nRF Sniffer dongle"):
        ble_sniffer.resolve_port(None)


def test_resolve_port_trusts_an_explicit_port(monkeypatch):
    """A DK behind a SEGGER J-Link VCOM is outside SNIFFER_PIDS but still works."""
    monkeypatch.setattr(ble_sniffer, "list_sniffers", lambda: [])
    assert ble_sniffer.resolve_port("/dev/ttyACM9") == "/dev/ttyACM9"


def test_two_dongles_require_an_explicit_choice(monkeypatch):
    monkeypatch.setattr(ble_sniffer, "list_sniffers", lambda: [{"port": "COM1"}, {"port": "COM2"}])
    with pytest.raises(ble_sniffer.BleSnifferError, match="pass `port=`"):
        ble_sniffer.resolve_port(None)


def test_meshtastic_nodes_are_not_mistaken_for_sniffers():
    """Several Meshtastic nRF52840 boards also ship Nordic's VID. Matching the VID
    alone would open a capture on a live node's port and hold it away from the
    debug session — so the PID allowlist is the guard."""
    assert 0x522A in ble_sniffer.SNIFFER_PIDS
    assert 0x521F not in ble_sniffer.SNIFFER_PIDS, "0x521F is the dongle bootloader"
    assert len(ble_sniffer.SNIFFER_PIDS) == 1


def test_capture_argv_is_passive_by_default():
    argv = ble_sniffer._build_argv(
        Path("/opt/nrfutil-ble-sniffer"),
        "COM27",
        Path("/tmp/out.pcap"),
        follow=None,
        only_advertising=False,
        scan_follow_rsp=False,
        rssi_cut_off=None,
    )
    # str(Path(...)) normalizes separators per platform — compare the name, not the path.
    assert Path(argv[0]).name == "nrfutil-ble-sniffer"
    assert argv[1] == "sniff"
    assert "--port" in argv and "COM27" in argv
    assert "--output-pcap-file" in argv
    assert "--scan-follow-rsp" not in argv, (
        "scan-follow-rsp makes the sniffer transmit SCAN_REQ; it must be opt-in"
    )


def test_capture_argv_passes_through_the_filters():
    argv = ble_sniffer._build_argv(
        Path("sniffer"),
        "COM27",
        Path("out.pcap"),
        follow="c8:54:dd:59:2e:32",
        only_advertising=True,
        scan_follow_rsp=True,
        rssi_cut_off=-90,
    )
    assert "--follow" in argv and "c8:54:dd:59:2e:32" in argv
    assert "--only-advertising" in argv
    assert "--scan-follow-rsp" in argv
    assert argv[argv.index("--rssi-cut-off") + 1] == "-90"


def test_capture_start_refuses_a_second_capture_on_one_dongle(monkeypatch, tmp_path):
    """The per-port lock would also catch this, but only inside the worker thread —
    after the caller was already told the job started."""
    fake = tmp_path / "nrfutil-ble-sniffer"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv(ble_sniffer.SNIFFER_BIN_ENV, str(fake))
    monkeypatch.setattr(ble_sniffer, "list_sniffers", lambda: [{"port": "COM27"}])
    monkeypatch.setattr(
        ble_sniffer.jobs,
        "running_of_kind",
        lambda kind: [{"job_id": "abc123", "sniffer_port": "COM27"}],
    )
    with pytest.raises(ble_sniffer.BleSnifferError, match="already running"):
        ble_sniffer.capture_start()


def test_capture_start_rejects_a_negative_duration(monkeypatch, tmp_path):
    fake = tmp_path / "nrfutil-ble-sniffer"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv(ble_sniffer.SNIFFER_BIN_ENV, str(fake))
    monkeypatch.setattr(ble_sniffer, "list_sniffers", lambda: [{"port": "COM27"}])
    with pytest.raises(ble_sniffer.BleSnifferError, match="duration_s"):
        ble_sniffer.capture_start(duration_s=-1)


def test_poll_and_stop_report_an_unknown_job_id():
    for result in (
        ble_sniffer.capture_poll("deadbeef"),
        ble_sniffer.capture_stop("deadbeef"),
    ):
        assert "Unknown job_id" in result["error"]


def test_status_reports_missing_hardware_without_raising(monkeypatch):
    monkeypatch.setattr(ble_sniffer, "list_sniffers", lambda: [])
    monkeypatch.setattr(ble_sniffer, "sniffer_bin_or_none", lambda: None)
    result = ble_sniffer.status()
    assert result["ready"] is False
    assert result["transmit_supported"] is False, "the sniffer firmware cannot transmit"
    assert len(result["missing"]) == 2
    assert result["hint"]


def test_list_sniffers_survives_a_broken_usb_stack(monkeypatch):
    """Capability detection runs on every server start and must never crash it."""

    def boom():
        raise OSError("usb enumeration failed")

    monkeypatch.setattr(ble_sniffer.list_ports, "comports", boom)
    assert ble_sniffer.list_sniffers() == []
    assert ble_sniffer.available() is False
