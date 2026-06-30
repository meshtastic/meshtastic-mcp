# Topology & wiring

A closed loop needs **two endpoints**: a radio you drive/observe from the MCP
server (the **TESTER**) and the radio the **phone** connects to (the **DUT**).
The app is the thing under test; the tester is the rest of the mesh.

## Roles

| Role | Connection | Driven by | Notes |
|---|---|---|---|
| TESTER radio | USB-serial | Meshtastic MCP (`mesh_e2e.py`, recorder) | the mesh peer you stimulate from / observe on |
| DUT radio | BLE / USB-serial / **TCP** | the Android app | exclusive — cannot also be the MCP tester |
| Phone (DUT host) | adb (USB or `adb connect`) | Android CLI + adb | runs Meshtastic-Android |

The serial port lock is **exclusive**: one process per port. So a single radio
cannot be both tester and DUT. Options when you only have one radio:

- App connects over **TCP to a Portduino `native-*` node** (`MESHTASTIC_MCP_TCP_HOST`),
  physical radio stays the tester.
- Or: physical radio is the DUT (app over BLE/USB), and a **second** physical radio
  (or native node) is the tester.

Both radios must share **region + primary channel** or no traffic flows between them.
Confirm with `mesh_e2e.py info <port>` on each (`region`, `primary_channel` must match).

## USB switch / multi-node fleet (uhubctl)

A per-port-switched USB hub (e.g. a uhubctl-supported hub) unlocks two things:

1. **More nodes on one host** — plug N radios, enumerate with `mesh_e2e.py devices`,
   assign roles via the firmware harness env vars
   (`MESHTASTIC_MCP_ENV_<ROLE>`, `MESHTASTIC_UHUBCTL_LOCATION_<ROLE>`,
   `MESHTASTIC_UHUBCTL_PORT_<ROLE>` to pin a radio to a hub port when several share a VID).
2. **Per-port power control** — the resilience loop (`loop-resilience.md`) cuts power to
   one radio's port mid-conversation and asserts mesh + app recovery.

Setup:
```bash
brew install uhubctl          # macOS
apt install uhubctl           # Debian/Ubuntu

# Linux: install udev rules so uhubctl works without root (one-time)
sudo curl -fsSL https://raw.githubusercontent.com/mvp/uhubctl/master/udev/rules.d/52-usb.rules \
  -o /etc/udev/rules.d/52-usb.rules
sudo udevadm trigger --attr-match=subsystem=usb
# Your user must be in the dialout group: sudo usermod -a -G dialout $USER
# (log out and back in, or run: newgrp dialout)

uhubctl                      # list hubs + ports; note the location like "1-1.3"
# MCP tools: uhubctl_list / uhubctl_power / uhubctl_cycle (confirm=True gated)
# `meshtastic-mcp doctor` will warn if udev rules are missing
```
Pin roles so power-cycling targets the right radio when VIDs collide:
```bash
export MESHTASTIC_UHUBCTL_LOCATION_NRF52=1-1.3
export MESHTASTIC_UHUBCTL_PORT_NRF52=3
```

## Preflight checklist

```bash
export MESHTASTIC_FIRMWARE_ROOT="$HOME/meshtastic/firmware"
MCP="$MESHTASTIC_FIRMWARE_ROOT/mcp-server/.venv/bin/python"
S="$HOME/.agents/skills/meshtastic-e2e/references/mesh_e2e.py"

$MCP "$S" devices                                   # >=1 tester radio
$MCP "$S" info <TESTER_PORT>                         # region + primary_channel
adb devices                                          # phone (DUT) attached
adb shell pm list packages | grep meshtastic         # app installed
# DUT + TESTER share region/channel? compare the two `info` outputs.
```
