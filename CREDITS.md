# Credits & sources

`libkp` stands on three sources of knowledge.

## Kemper MIDI Parameter Documentation

The parameter model carried inside the protocol — the page/number address space,
SysEx function codes, effect-type values, string tags, and the Control Change
map — follows Kemper's official MIDI parameter documentation:

- <https://www.kemper-amps.com/downloads/5/User-Manuals>

All parameter names, effect-type names, and CC assignments in
[`spec/`](spec/) are transcribed from that reference.

## PySwitch

[PySwitch](https://github.com/Tunetown/PySwitch) is an open-source
CircuitPython MIDI controller for the Kemper. Its Kemper client was invaluable
for cross-checking NRPN addresses against real hardware and for details beyond
the official documentation, including:

- The Fixed-FX parameter page and several tuner / tempo / meter addresses.
- The bidirectional beacon and "sense" keep-alive semantics.
- The numeric morph-state parameter and the rig/bank selection scheme.

PySwitch is credited wherever those addresses appear in the spec.

## Observed experimentation

The protocol's transport envelope is not covered by any published
documentation; it was characterized empirically by observing network traffic and
device behavior. This includes:

- UDP discovery (the `DSCV` TagStream poll and reply format).
- The TCP handshake (the protocol-GUID negotiation and session preamble).
- MIDI3 stream framing.
- The realtime status / meter block field identities.
- The native CBOR control channel: its wire shape, the state-dump trigger, and
  the current bank/rig addresses it returns (documented in
  [docs/06](docs/06-cbor-channel.md); `libkp` implements the codec and the
  state-dump snapshot, not the wider management grammar).

These findings are labeled "observed experimentation" throughout the docs.
