# Discovery

Profilers are found by UDP broadcast on port **5727**. The client sends a fixed
34-byte poll; every Profiler on the LAN answers with a description of itself.
Both the poll and the reply use the same small serialization, **TagStream**.

## Owning the port

A client must take UDP 5727 **exclusively**, and hold it for as long as the
session is active.

This is not a stylistic preference. The device answers a poll only on port 5727 —
it ignores the poll's source port, so listening anywhere else hears nothing — and
when several sockets are bound to one UDP port the kernel delivers each arriving
datagram to *exactly one* of them. A second listener therefore does not observe a
copy of the reply; it takes it. Two programs sharing the port turn discovery into
a coin flip, and the one that loses reports "no device found" while the network
and the Profiler are both perfectly healthy.

So bind without `SO_REUSEADDR` and without `SO_REUSEPORT`:

- **If the port is free**, the exclusive bind succeeds and no other process can
  take it while the socket is open — replies cannot be stolen mid-session.
- **If the port is already held**, the bind fails with `EADDRINUSE`, and that is
  the correct moment to stop and say so. Report it as a port conflict naming the
  likely holder; do not fall back to a shared bind, which merely converts a clear
  start-up error into an intermittent one.

The usual holder on a desktop machine is other Kemper software: Rig Manager binds
UDP 5727 for its entire run, so it has to be quit before a client can discover.

Implementations expose this as an owned handle — `DiscoveryPort` in all three —
acquired before the session and released when it ends. `poll` may be called on it
as often as needed, which is what a long-running client wants: re-poll to notice
Profilers appearing and disappearing without ever letting go of the port in
between. The one-shot `discover` / `find_first` helpers acquire, poll once and
release, and suit a CLI rather than a session.

## TagStream

TagStream is a length-prefixed field list with an optional 4-byte ASCII header.

```
[4-byte ASCII header]  [len][content]  [len][content]  …  0x00
```

- The **header** is 4 raw ASCII bytes with no length prefix. For discovery it is
  always `"DSCV"` (`44 53 43 56`).
- Each **field** is a single length byte followed by its content. The length byte
  is **inclusive of itself**: a field whose content is *n* bytes is written as
  `[n+1][content]`. A field is therefore at most 254 content bytes.
- A **`0x00`** byte where a length would be read terminates the stream.

In a reply, each field's content is itself a **4-character ASCII key** followed
by its value; the value runs to the end of the field, so it may contain any byte
except that the field length must fit in a byte. Keys are fixed-width, so
`content[0..4]` is the key and `content[4..]` is the value.

## The poll request

The client broadcasts this to `255.255.255.255:5727` (and to each interface's
directed broadcast address) every **500 ms** until a device answers, and
continues polling to notice devices appearing and disappearing.

```
44 53 43 56                                             "DSCV"
16                                                      len = 22 (inclusive)
4D 41 43 23 30 30 3A 30 30 3A 30 30 3A                  "MAC#00:00:00:00:00:00"
30 30 3A 30 30 3A 30 30
07                                                      len = 7 (inclusive)
50 4F 4C 4C 3A 29                                       "POLL:)"
00                                                      terminator
```

The payload is **exactly 34 bytes**: 4 (header) + 1 + 21 + 1 + 6 + 1. The MAC
field carries the client's own MAC address as `MAC#` followed by the
colon-separated uppercase hex form. Its length is fixed, so the packet length
never varies. An all-zero placeholder MAC — `00:00:00:00:00:00` — is accepted
and devices reply to it normally.

Conformance vectors: [`../spec/vectors/discovery.json`](../spec/vectors/discovery.json).

```
mac = "00:00:00:00:00:00"
44534356164d41432330303a30303a30303a30303a30303a303007504f4c4c3a2900

mac = "DE:AD:BE:EF:CA:FE"
44534356164d41432344453a41443a42453a45463a43413a464507504f4c4c3a2900
```

Constants: `header = "DSCV"`, `poll_mac_prefix = "MAC#"`,
`poll_payload = "POLL:)"`, `poll_interval_ms = 500`, in
[`../spec/protocol.toml`](../spec/protocol.toml) `[discovery]`.

## The reply

Each Profiler answers with a `DSCV` TagStream of key/value fields. The reply
arrives on the client's UDP 5727 socket but is sent **from an ephemeral source
port**, and a device answers every poll from a new one — deduplicate discovered
devices by source IP address, never by port.

A reply, decoded (field values here are synthetic — they match the
[`discovery-reply`](../spec/captures/discovery-reply.json) capture fixture):

```
"DSCV"
  NAME  "Test Profiler"
  SWVS  "00000"
  VSTR  "Release: 0.0.0.00000"
  MAC#  "AABBCCDDEEFF"
  OWNR  "Example Owner"
  APPR  "PROFILINGAMP"
  TYPE  "4"
  SER#  "EXAMPLE0000000"
  MGR#  "{00000000-0000-0000-0000-000000000001}"
  DEF#  "{00000000-0000-0000-0000-000000000002}"
  MGR2  "{00000000-0000-0000-0000-000000000003}"
  FSET  "000000"
  LICN  "LVL II & III"
  SYNL  "1"
0x00
```

### Reply fields

| Key | Meaning | Example |
|---|---|---|
| `NAME` | device display name | `Test Profiler` |
| `SWVS` | software build number | `00000` |
| `VSTR` | full version string | `Release: 0.0.0.00000` |
| `MAC#` | device MAC address, no separators | `AABBCCDDEEFF` |
| `OWNR` | owner name as configured on the device | `Example Owner` |
| `APPR` | appliance / product class | `PROFILINGAMP` |
| `TYPE` | product / model type id | `4` |
| `SER#` | serial number | `EXAMPLE0000000` |
| `MGR#` | manager / session GUID | `{00000000-…-000000000001}` |
| `DEF#` | device-definition (DDF) GUID | `{00000000-…-000000000002}` |
| `MGR2` | secondary manager GUID | `{00000000-…-000000000003}` |
| `FSET` | feature-set token | `000000` |
| `LICN` | license level | `LVL II & III` |
| `SYNL` | sync-layer available flag | `1` |

Note that `MAC#` appears in both directions with **different formatting**: the
poll writes the client MAC with colons, the reply writes the device MAC without.

Values are ASCII text even when numeric (`TYPE`, `SWVS`, `SYNL`). Treat unknown
keys as forward compatibility, not as errors — see
[Versioning & compatibility](10-versioning-and-compatibility.md).

## Parsing rules

- Verify the 4-byte header before reading fields; ignore packets that do not
  start with `DSCV`.
- Stop at the first `0x00` length byte, and also stop if a length byte would run
  past the end of the datagram — a truncated field is a malformed packet, not a
  short field.
- A field shorter than 5 bytes total (`len` ≤ 4) cannot hold a 4-character key
  and should be skipped.
- The reply is a single datagram; there is no continuation or reassembly.

A sanitized recording of a real reply is replayed by every implementation's test
suite as the `discovery` fixture in [`../spec/captures`](../spec/captures), which
asserts the header and the full key/value list.

## After discovery

Take the device's source IP and open a TCP connection to port 5727 —
[Handshake](03-handshake.md).

## Sources

Discovery — the `DSCV` TagStream encoding, the poll payload, the port, the poll
interval, and the reply key set — was characterized through observed
experimentation. See [../CREDITS.md](../CREDITS.md).
