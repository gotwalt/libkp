# Handshake

Once [discovery](02-discovery.md) has produced an address, the client opens a
TCP connection to **port 5727** and negotiates which wire dialect the session
will speak. The device leads the exchange.

| | |
|---|---|
| Port | 5727 (`0x165F`) — the same number as UDP discovery |
| Connect timeout | 5 s |
| Greeting wait | 2 s for the first byte (`handshake_timeout_ms`) |
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

The reserved protocol below always rejects. The other three accept whether or
not another session is open: the device serves several sessions at once, and a
`DeviceModel` holds two of them by design ([Channels and data paths](11-channels-and-data-paths.md)).
What it will not do is greet a socket opened too soon after another one opened
or closed — see [the greeting wait](#the-greeting-wait-and-the-read-idle) and
[Failure modes](#failure-modes).

## The greeting wait, and the read idle

Two different silences bound a handshake, and `libkp` times them separately:

| Wait | Bounds | Constant |
|---|---|---|
| **Greeting wait** | how long the device may take to send the *first byte* of its greeting after the socket opens — and, again, the first byte of its reply to the selection | `handshake_timeout_ms` = 2000 |
| **Read idle** | the gap between chunks once the device is talking; the same short gap paces every read of the live stream | tens of milliseconds, chosen by the caller |

They have to be separate. Established by observed experimentation, a freshly
booted device greets within a few milliseconds, but one that has served a few
sessions has taken close to 800 ms to send its first byte — far longer than
the idle gap that ends a line once it has begun — and the reply to the
selection can lag the same way. A connect bounded by the idle alone fails on a
healthy device; one that waited the full 2 s between every chunk would make
every read of the stream sluggish. So `Session.handshake` gives each reply
`handshake_timeout_ms` to *begin* and the idle to *finish*, and only a device
that says nothing at all for the whole timeout fails the connect:
`SessionError::Timeout` with phase `"greeting"`, or `"protocol selection"` for
a device that read the selection and never answered it. A greeting that offers
nothing to select is the same failure. The control link reads its greeting on
the same budget.

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
| `{2490272E-CD92-4DBA-AE32-E8AF37ED3B0A}` | `request_response` | Accepts the handshake. Sends an 11-byte token on open and nothing unsolicited thereafter. Its command grammar is uncharacterized. | not opened |
| `{774CDB9E-74ED-4740-AF09-AC96B3A69A11}` | `cbor_control` | The device's native control link. Accepts the handshake and streams CBOR items, not MIDI3. | **implemented** as the model's [control link](06-cbor-channel.md): the state dump and the live pushes are read; one item is ever written |
| `{77DB6B28-785E-4641-B840-42F0F06A11FC}` | `reserved` | Offered in the list but answers `-NO` to every selection attempt. | rejects |

The MIDI3 stream carries the control and monitoring surface this library
exposes: reading and writing any NRPN parameter, reading string tags, loading
rigs, toggling effects, and receiving meters. The CBOR channel is a second
encoding of the same event universe plus the one value the stream never carries
— the morph position — and the device-management operations MIDI3 cannot
express; a `DeviceModel` opens both, the stream first. See
[The CBOR channel](06-cbor-channel.md).

Selecting the reserved protocol is only useful as a liveness probe. Selecting
`{2490272E-…}` yields a session that is silent apart from its opening token.

## Failure modes

| Symptom | Cause |
|---|---|
| TCP connect times out | device off, wrong address, or a different subnet — re-run discovery |
| Connect succeeds, the greeting takes most of a second | normal for a device that has served a few sessions; wait `handshake_timeout_ms` for the first byte |
| Connect succeeds, no greeting within the timeout | the socket was opened too soon after another one opened or closed to the same device (inside `connection_cooldown_ms`), or the device has stopped accepting sessions altogether — the state connection churn or overlapping rig loads leave it in, cleared only by a power cycle |
| Connect refused | the device has stopped listening; the same wedged state |
| `-NO` for `{774CDB9E-…}` when it was offered | not observed; the model reports the link `Unavailable` and carries on |
| Stream stalls after the preamble | none — the MIDI3 stream is always active; a stall means the socket died |

Because the device pushes at ~20 Hz unprompted, silence on an accepted MIDI3
session is itself a reliable liveness signal: no frames for more than ~1 s means
the link is gone. A keep-alive is nonetheless available at the message layer —
the [beacon and sense](05-sysex-nrpn.md#beacon-and-sense) exchange.

## Disconnecting

Close the TCP socket. There is no shutdown message. The device frees the session
at once — but it does not greet a new socket that follows too closely. Every
`Session` in `libkp` records its open and its close in a process-wide ledger
keyed by `(address, port)`, and the next open to the same peer waits until
`connection_cooldown_ms` (1 s) has passed since the later of the two, so no
caller has to space its own connections. A device is spared churn by
construction; it is not made tolerant of it.

## Sources

The handshake — the device-first GUID list, the `.` terminator, the `+`/`-NO`
responses, the 8-byte session preamble, the roles of the four GUIDs, the slow
greeting and the cooldown a device needs between sessions — was characterized
through observed experimentation. See [../CREDITS.md](../CREDITS.md).
