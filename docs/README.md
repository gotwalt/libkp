# The Kemper Profiler network protocol

This is the reference documentation for the protocol `libkp` implements. It
describes how a controller finds a Profiler on the network, opens a session,
and exchanges the MIDI-derived messages that carry parameters, meters, and
control — and how the library's device model holds the device's two channels
behind one handle.

## Contents

1. [Overview](01-overview.md) — the layers, ports, message universe, and the model's surface
2. [Discovery](02-discovery.md) — UDP `DSCV` TagStream broadcast
3. [Handshake](03-handshake.md) — TCP protocol-GUID negotiation, the greeting wait, and the session preamble
4. [MIDI3 framing](04-midi3-framing.md) — how MIDI is carried over the stream
5. [SysEx / NRPN dialect](05-sysex-nrpn.md) — the parameter message grammar, the string tags, the request lane, the position report, the morph
6. [The CBOR channel](06-cbor-channel.md) — the model's control link: the state dump, the live pushes, the morph position
7. [Realtime status & meters](07-realtime-status.md) — the ~20 Hz status block
8. [Control model](08-control-model.md) — CC vs NRPN, the device model's surface, and the Navigator that loads rigs
9. [Parameter registry](09-parameter-registry.md) — typed parameter descriptors and the state routing table
10. [Versioning & compatibility](10-versioning-and-compatibility.md) — how the three implementations stay in sync
11. [Channels and data paths](11-channels-and-data-paths.md) — the design record: MIDI3 against CBOR, the model that owns both, the fold, the request lane, the Navigator, the ledger, and the measurements behind each

## Sources

Parameter data (addresses, effect types, string tags, CC map) follows the
official [Kemper MIDI Parameter Documentation](https://www.kemper-amps.com/downloads/5/User-Manuals),
cross-checked against [PySwitch](https://github.com/Tunetown/PySwitch). The
transport envelope (discovery, handshake, framing, session, the status block,
and the CBOR channel) and the device's behaviour under load — its request
latency, the shape and timing of its state dump and rig loads, and what it
does not tolerate — were characterized through observed experimentation. See
[../CREDITS.md](../CREDITS.md).
