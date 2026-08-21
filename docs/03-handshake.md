# Handshake

Once [discovery](02-discovery.md) has produced an address, the client opens a
TCP connection to **port 5727** and negotiates which wire dialect the session
will speak. The device leads the exchange.

| | |
|---|---|
| Port | 5727 (`0x165F`) — the same number as UDP discovery |
| Connect timeout | 5 s |
| Socket read timeout | 15 s |
| Line terminator | `\r\n` |

Constants: [`../spec/protocol.toml`](../spec/protocol.toml), `[transport]` and
`[handshake]`.

## Step 1 — the device sends first

Immediately on accept, before the client writes anything, the device sends a
list of the protocol GUIDs it supports: one per line, CRLF-terminated, ending
with a line containing only `.`.

```
{77DB6B28-785E-4641-B840-42F0F06A11FC}\r\n
{369F50E7-750B-459A-BAEE-85ADD3F3798D}\r\n
{2490272E-CD92-4DBA-AE32-E8AF37ED3B0A}\r\n
{774CDB9E-74ED-4740-AF09-AC96B3A69A11}\r\n
.\r\n
```

The `.` is a list terminator, not a field separator. GUIDs are ASCII, including
the braces and hyphens, and are compared literally. The order the device lists
them in carries no meaning; do not assume a position.

A client that reads this list should read line-by-line until the lone `.` rather
than assuming a fixed byte count — the set is firmware-dependent.

## Step 2 — the client selects

The client writes exactly one GUID followed by CRLF, with no other framing:

```
{369F50E7-750B-459A-BAEE-85ADD3F3798D}\r\n
```

## Step 3 — the device accepts or rejects

The reply is one CRLF-terminated line:

| Reply | Meaning |
|---|---|
| `+<GUID>\r\n` | **accepted** — the session stream follows on the same socket |
| `-NO\r\n` | **rejected** |

Match on the first byte: `+` accept, `-` reject. On acceptance the echoed GUID
should equal the requested one; treat a mismatch as a protocol error.

A rejection is not necessarily permanent — the reserved protocol below always
rejects, but any protocol can also be refused because another controller already
holds a session with the device. A Profiler serves **one controller at a time**.

## Step 4 — the session preamble

After acceptance the client writes **8 zero bytes**:

```
00 00 00 00 00 00 00 00
```

This opens the session. From the byte after the preamble, everything in both
directions is framed stream data — for the MIDI3 protocol, that means
[MIDI3 frames](04-midi3-framing.md). Nothing is sent in reply to the preamble
itself; on the MIDI3 stream the device simply begins pushing its realtime status
block within ~50 ms.

Constant: `session_preamble_len = 8`.

## The four protocols

| GUID | Name in the spec | Role | Status |
|---|---|---|---|
| `{369F50E7-750B-459A-BAEE-85ADD3F3798D}` | `midi3_stream` | Bidirectional MIDI3. Pushes the realtime status block and every parameter change; accepts requests, parameter writes, and Control Change, replying on the same socket. | **implemented** |
| `{2490272E-CD92-4DBA-AE32-E8AF37ED3B0A}` | `request_response` | Accepts the handshake. Sends an 11-byte token on open and nothing unsolicited thereafter. Its command grammar is uncharacterized. | not implemented |
| `{774CDB9E-74ED-4740-AF09-AC96B3A69A11}` | `cbor_control` | The device's native control link. Accepts the handshake and streams CBOR items, not MIDI3. | [experimental](06-cbor-channel.md), not implemented |
| `{77DB6B28-785E-4641-B840-42F0F06A11FC}` | `reserved` | Offered in the list but answers `-NO` to every selection attempt. | rejects |

The MIDI3 stream is sufficient for the full control and monitoring surface this
library exposes: reading and writing any NRPN parameter, reading string tags,
switching rigs, toggling effects, and receiving meters. The CBOR channel is a
second encoding of the same event universe plus the device-management operations
MIDI3 cannot express; see [The CBOR channel](06-cbor-channel.md).

Selecting the reserved protocol is only useful as a liveness probe. Selecting
`{2490272E-…}` yields a session that is silent apart from its opening token.

## Failure modes

| Symptom | Cause |
|---|---|
| TCP connect times out | device off, wrong address, or a different subnet — re-run discovery |
| Connect succeeds, no GUID list arrives | another controller already holds the session |
| `-NO` for `{369F50E7-…}` | the session is held elsewhere; retry after disconnecting the other controller |
| Stream stalls after the preamble | none — the MIDI3 stream is always active; a stall means the socket died |

Because the device pushes at ~20 Hz unprompted, silence on an accepted MIDI3
session is itself a reliable liveness signal: no frames for more than ~1 s means
the link is gone. A keep-alive is nonetheless available at the message layer —
the [beacon and sense](05-sysex-nrpn.md#beacon-and-sense) exchange.

## Disconnecting

Close the TCP socket. There is no shutdown message. The device frees the session
immediately and will accept a new controller.

## Sources

The handshake — the device-first GUID list, the `.` terminator, the `+`/`-NO`
responses, the 8-byte session preamble, and the roles of the four GUIDs — was
characterized through observed experimentation. See
[../CREDITS.md](../CREDITS.md).
