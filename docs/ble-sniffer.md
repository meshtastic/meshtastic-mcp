# BLE sniffer — nRF Sniffer for Bluetooth LE

Off-device capture of the **phone-to-node link**. The other observers in this repo each
watch one plane — `recorder/` taps a node's own serial log, `sdr.py`/`rf_oracle.py` watch
the LoRa RF, `pa_sweep.py` measures the PA. This one watches BLE from outside both ends,
which makes it the independent oracle for the failure class neither end reports honestly:
the app says *"no devices found"* while the node says *"advertising"*. Only a third radio
settles which is lying.

Verified live against an nRF52840 Dongle (`nrfutil-ble-sniffer` 0.16.2, sniffer firmware
protover 3) on 2026-09-20.

## Receive only

**The nRF Sniffer firmware cannot transmit.** Its UART protocol has no transmit command,
so there is no BLE counterpart to `inject_frame`. Injecting a frame into a node's *LoRa*
receive path is `inject.py`; doing the same over BLE would need different firmware on the
dongle (a Zephyr/`hci_uart` controller), not a flag on this module.

The one exception is `scan_follow_rsp`, which makes the sniffer emit SCAN_REQ to solicit
scan responses. It is **off by default**, precisely so a capture cannot perturb the thing
it is observing. See *Reading a capture* for when you need it.

## Device identity

| | |
| --- | --- |
| USB VID | `0x1915` (Nordic Semiconductor) |
| USB PID | `0x522A` — nRF52840 Dongle (PCA10059) running the sniffer **application** |
| Transport | USB CDC, driven by `nrfutil-ble-sniffer sniff` |
| pcap link type | `272` (`LINKTYPE_NORDIC_BLE`) |

The PID allowlist is deliberately narrow. Several Meshtastic nRF52840 boards also ship
Nordic's VID, and matching the VID alone would open a capture on a live node's serial
port — holding it away from the debug session that needs it. `0x521F` (the dongle's
bootloader) is excluded because it cannot capture. Any other board, such as a DK behind a
SEGGER J-Link VCOM, still works by passing `port=` explicitly.

## Prerequisites

The dongle must run the **nRF Sniffer for Bluetooth LE** firmware, and Nordic's
`nrfutil-ble-sniffer` plugin binary must be installed:

```bash
# 1. flash the dongle:
#    https://www.nordicsemi.com/Products/Development-tools/nRF-Sniffer-for-Bluetooth-LE
# 2. install the capture plugin:
nrfutil install ble-sniffer
```

Resolution order for the plugin binary: `$MESHTASTIC_MCP_BLE_SNIFFER` → `$NRFUTIL_HOME/bin`
→ `~/.nrfutil/bin` → `PATH`. `doctor` reports which piece is missing and how to get it.

**Not `config.nrfutil_bin()`.** That resolver also accepts `adafruit-nrfutil` and the
legacy `nordicsemi` pip `nrfutil`, neither of which has a `ble-sniffer` subcommand — on a
machine with both installed it picks the wrong one and every capture fails with a usage
error. This module resolves the plugin binary itself and invokes it directly, without the
`nrfutil` core launcher.

## Capability gating

`ble_sniffer` is active when a dongle is attached. It gates the three capture tools;
`ble_sniff_status` is **core** and always registered, on the same reasoning as
`pa_meter_status` — the tool whose job is to report missing hardware must not be hidden by
that hardware being missing.

## Tools

| Tool | What it does |
| --- | --- |
| `ble_sniff_status` | Dongles attached, plugin resolved, captures already running. Core. |
| `ble_sniff_start` | Start a background capture, returns a `job_id`. |
| `ble_sniff_poll` | Status plus a summary of everything captured so far. |
| `ble_sniff_stop` | Stop a capture; the pcap so far is kept and summarized. |

Captures outrun the 60 s MCP timeout, so they use the `jobs.py` async pattern like
build/flash/grind. `duration_s=0` runs until `ble_sniff_stop`.

```python
ble_sniff_status()                       # ready? which port?
job = ble_sniff_start(duration_s=30)     # passive, all advertisers
ble_sniff_poll(job["job_id"])            # works mid-capture
ble_sniff_stop(job["job_id"])            # early stop; pcap kept
```

Useful arguments:

- `follow=<address>` — lock onto one advertiser and follow it into its connection, so you
  capture the actual GATT traffic of an app session rather than only advertisements.
- `only_advertising`, `rssi_cut_off` — cut noise on a busy band.
- `scan_follow_rsp` — solicit scan responses. **Transmits.** See below.

The pcap path is in every result; open it in Wireshark for the per-packet view the summary
flattens. A terminated capture is still a valid file (pcap has no trailer).

## Reading a capture

Identifying a Meshtastic node is not symmetric across platforms, per
`src/BluetoothCommon.h` and the two Bluetooth backends in firmware:

| Platform | Service UUID | Name |
| --- | --- | --- |
| ESP32 (NimBLE) | in the advertisement | in the advertisement |
| nRF52 (Bluefruit) | in the advertisement | **scan response only** — `Advertising.addName()` is commented out upstream |

So the summary merges every PDU type per advertiser address, and `likely_meshtastic` keys
off `MESH_SERVICE_UUID` (`6ba1b218-15a8-461f-9fa8-5dcae273eafd`) and **never off a name**.
A name is neither necessary nor sufficient: an nRF52 node broadcasts none until something
scans it, and any radio can call itself whatever it likes.

Consequence worth knowing before you go hunting: a passive capture routinely shows a
Meshtastic node with `likely_meshtastic: true` and `names: []`. That is correct and not a
failure. Pass `scan_follow_rsp=True` if you want the name — measured live, the same two
nodes went from `[]` to `["HZR1_17d2"]` and `["Meshtastic_2e32"]`.

CRC-failing packets are counted (`crc_bad`) but excluded from the advertiser rows. A
sniffer captures corrupt frames by design, and a flipped bit in a name or a UUID would
otherwise invent a device that does not exist — the first capture taken here contained
eleven spellings of one device's name, all but one of them corrupt.

`address_type` comes only from PDUs the advertiser itself sent. A SCAN_REQ or CONNECT_IND
is sent by the *scanner*, so the advertiser's type would have to come from its `RxAdd`
bit — and measured on real air, scanners set `RxAdd=0` even when probing a random-static
advertiser, which reported a random node as public on alternate runs. Those PDUs
contribute the address (so connect attempts are counted against the right node) but never
its type.

## Untrusted output

`ble_sniff_poll` and `ble_sniff_stop` return **BLE device names, which are arbitrary text
chosen by whoever owns any radio in range.** This is the same prompt-injection exposure as
`logs_window`/`packets_window`, one step further out: you do not even need to be on the
mesh to put text in front of the agent, just in radio range. All four tools are
`openWorldHint`. Do not process a captured name and call `send_text` in the same agentic
task without human review. See `SECURITY.md`.

## Wire format

Nothing here needs Wireshark or scapy at runtime — `ble_sniffer.py` parses the pcap with
`struct`. The container is a 24-byte libpcap global header (nrfutil writes **big-endian**,
magic `a1b2c3d4`) plus 16-byte record headers. Each record is a
`LINKTYPE_NORDIC_BLE` pseudo-header followed by the BLE link layer:

```
 0      board id
 1-2    u16 LE  length of everything after byte 6
 3      protocol version (3)
 4-5    u16 LE  packet counter
 6      packet id
 7      length of the packet-metadata block that follows (10)
 8      flags — bit 0 is CRC OK, bits 4-6 the PHY
 9      channel index
10      RSSI magnitude (dBm, negated)
11-12   u16 LE  event counter
13-16   u32 LE  timestamp, microseconds
17..    BLE link layer: access address, PDU header, payload, CRC
```

The link layer is located at `7 + meta_len`, not a hardcoded 17, so a metadata block that
grows in future firmware still lands correctly. Only **protover 3** is decoded (nRF Sniffer
4.x, what current firmware emits and what `nrfutil` 0.16 requires); anything else is
counted as unparsed rather than mis-decoded.

Spans are measured from the **pcap record timestamps**, not the sniffer's own microsecond
clock: the latter is 32-bit and wraps every ~71 minutes, which would make a long soak
report a negative duration.

An access address other than `0x8E89BED6` means a data-channel PDU inside an established
connection — a different header layout with no advertiser address — so those are counted
(`data_channel_packets`, evidence a connection is live) but not dissected.

`tests/unit/test_ble_sniffer.py` carries two **verbatim frames from a live capture**,
cross-checked field-for-field against `tshark -V`, as the ground truth for this parser.

## Wireshark

The `nrfutil-ble-sniffer-shim.exe` that Wireshark's extcap directory uses is versioned
separately from the `nrfutil` core, and a mismatched pair panics on
`--extcap-interfaces` (`called Option::unwrap() on a None value`), which hides the dongle
from Wireshark's interface list. That affects live capture *in Wireshark only* — the CLI
path this module uses is unaffected. `nrfutil self-upgrade` plus
`nrfutil ble-sniffer bootstrap` re-pairs them.

Filter note: on `nordic_ble` encapsulation use `nordic_ble.crcok == 1`. The more obvious
`btle.crc.incorrect` is never populated for this link type, so every packet reads as bad.
