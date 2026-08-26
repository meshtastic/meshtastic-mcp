# Vanity identities — chosen NodeNums and app colours

A Meshtastic node on a PKI firmware build does not get told its number. It
derives it:

```
public_key  = X25519(private_key, 9)
my_node_num = crc32(public_key)                # NodeDB.cpp::createNewIdentity
node id     = "!%08x" % my_node_num
app colour  = the low 24 bits of that number, read straight as RGB
```

Both steps are one-way, so a *chosen* id — or a chosen colour, which is the same
thing over fewer bits — means searching the keypair space until one lands.
[mvgrind](https://github.com/miketweaver/mvgrind) does that search on the GPU.
This server drives it, checks every hit with its own arithmetic, and writes the
winning key to a radio.

The colour is not a Meshtastic-specific invention layered on top: every client
paints a node with those 24 bits. `nodeColorsFromNum` in Meshtastic-Android's
`NodeColors.kt` and `Color.swift` in Meshtastic-Apple agree, down to the
black-or-white foreground each picks for legibility. So `!8adc143c` is crimson
in both apps, and picking a colour is just a pattern over the id.

## Tools

| Tool | Needs | What it does |
|---|---|---|
| `vanity_preview` | — | What node id + colour a private key produces. No device, no GPU. |
| `vanity_grind_start` | `mvgrind` | Launch a grind in the background, get a `job_id`. |
| `vanity_grind_poll` | `mvgrind` | Status, progress, and any verified hits so far. |
| `vanity_grind_stop` | `mvgrind` | Stop a grind; keep what it already found. |
| `vanity_apply` | — | Write a key to a device, moving it to the matching NodeNum. |

Grinding is capability-gated on the binary; preview and apply are **core** — a
key ground on a friend's GPU is still inspectable and applicable here.

## A worked run

```python
vanity_grind_start(color="crimson", tol=6)     # -> {"job_id": "ea27…"}
vanity_grind_poll("ea27…")                     # -> hits[0].node_id "!19d70f3f", verified true
vanity_apply(private_key=hits[0]["private_key_hex"], port="/dev/ttyUSB0", confirm=True)
# -> {"changed": true, "node_id": "!19d70f3f", "verified": true}
```

`pattern` constrains the id, `color` the colour, and they compose:

| ask | what it means |
|---|---|
| `pattern="dc80"` | id starts `!dc80` |
| `pattern="dc801051"` | that exact id |
| `pattern="dc80****"` | the same as the prefix, spelled out |
| `pattern="dc80,801f,d0f0"` | a set — any of them wins, at no extra cost |
| `color="crimson"` / `color="#dc143c"` | a node the apps paint crimson |
| `color="teal", tol=6` | near enough to teal, ~2000x fewer keys |

`tol` is free to check and lands a hit far sooner: exact `crimson` averages
~17 M keys, `crimson` ±6 averages ~8 K. On an Apple M4 (~92 M keys/s via Apple's
OpenCL) a full 8-digit id averages about 48 s; a tolerant colour is instant.

The two constraints **share bits** — id nibbles 3-8 *are* the colour channels —
so `pattern="dc80"` already pins red to `0x80`. An impossible pair is rejected
before any grinding, and the reason lands verbatim in the job log:

```
the id pattern and that color disagree on the red channel:
the pattern needs (byte & 0xff) == 0xef, the color needs 0x00-0x08
```

## Every hit is re-derived here

`parse_hits` recomputes the public key and the CRC-32 with this repo's own
X25519 ladder (RFC 7748, `vanity.py`) and `zlib.crc32` — code that shares
nothing with the grinder's OpenCL kernels. A hit whose key does not actually
produce the id it claims comes back `verified: false`, and must not be applied.
That is a grinder bug, not a near miss.

The same arithmetic backs `vanity_preview`, so a key from anywhere can be
checked before it touches a radio.

## Applying a key: what actually happens

`vanity_apply` sends a `security` config set carrying the new `private_key`
with **`public_key` cleared**. That clearing is the whole trick. In
`AdminModule.cpp`:

```cpp
if (config.security.private_key.size != 32) {
    nodeDB->generateCryptoKeyPair();
} else if (config.security.public_key.size == 0) {
    nodeDB->generateCryptoKeyPair(config.security.private_key.bytes);
}
```

Send a new private key *and* echo back the old 32-byte public key and **neither
branch fires**: the node keeps the old public key, the old NodeNum, and a DH key
that no longer matches. The write appears to succeed and changes nothing.

With the public key empty the firmware re-derives it, `createNewIdentity()`
recomputes `my_node_num`, drops the old identity from the node DB, and
`saveChanges(…, requiresReboot=true)` reboots the board ~7 s later. `vanity_apply`
reconnects afterwards and reads `my_node_num` back — which doubles as the
empirical PKI check: a build compiled with `MESHTASTIC_EXCLUDE_PKI_KEYGEN` never
moves, and shows up as a mismatch rather than as firmware-version archaeology.

Two preconditions the tool enforces rather than discovering the hard way:

- **`lora.region` must be set.** `generateCryptoKeyPair` refuses to derive keys
  while the region is `UNSET`, so the write would be a silent no-op.
- **The key must be clamped.** The firmware signs with a clamped copy of the
  scalar, so an unclamped key yields a node whose signatures do not verify
  against its own public key. mvgrind only emits clamped keys.

### This is an identity change, not a setting

The old NodeNum is *removed* from the node's own DB. Peers keep DMing the old
public key until they see the new NodeInfo. Anything that named the old node —
an `admin_key` entry on another radio, a channel binding, a DM history — has to
be re-pointed. Keep the old private key if you want a way back. Hence
`confirm=True`, `destructiveHint`, and the up-front `previous_node_id` in the
result.

A 32-bit id is also not an identity: anyone can grind a different key with the
same id. It is a cosmetic label; security comes from the signature.

## Installing mvgrind

```sh
git clone --recursive https://github.com/miketweaver/mvgrind
cd mvgrind && make && make test
```

Then put `mvgrind` on `PATH`, or point `$MESHTASTIC_MCP_MVGRIND` at the binary.
`doctor` prints the command for this platform and reports where it resolved.

**macOS needs a one-line patch as of 2026-08-26.** Upstream probes for
`getrandom(2)` with `__has_include(<sys/random.h>)`; macOS ships that header but
declares only `getentropy`, so a stock `make` fails with *"call to undeclared
function 'getrandom'"*. The `/dev/urandom` fallback in `fill_random()` is
already correct — only the probe is wrong:

```c
#if __has_include(<sys/random.h>)
#include <sys/random.h>
#if !defined(__APPLE__)          /* macOS has the header but not getrandom(2) */
#define MV_HAVE_GETRANDOM 1
#endif
#endif
```

With that, it builds and `--selftest` passes against Apple's OpenCL.

## Handling keys

Everything a grind produces is **private-key material**:

- Hits land in `<MCP data dir>/grinds/<job_id>.keys`, mode `0600`.
- The job log holds the same keys — mvgrind prints hits to stdout — also `0600`.
- `vanity_grind_poll` returns keys inline, because that is what `vanity_apply`
  consumes. They pass through the model's context; treat the transcript
  accordingly.
- Never run `mvgrind` by hand in a repo checkout without `-o`: it appends to
  `found.txt` in the current directory, which is the sort of file that gets
  committed by accident.
