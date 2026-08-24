# Replay captures — the cross-platform decode harness

These fixtures are **sanitized recordings of real protocol traffic**, captured
through observed experimentation. They validate the full decode pipeline of each
implementation (Rust, Python, Swift) against genuine wire data — real message
sequences, real MIDI3 framing, the real mix of message types — which synthetic
inputs cannot reproduce.

They complement, and do not replace, the synthetic builder/parser vectors in
[`../vectors`](../vectors): the vectors pin individual functions; these fixtures
prove that a real stream decodes correctly end to end.

## Sanitization

All human- and device-identifying **text** — device names, owners, serials, MAC
addresses, profile authors, and timestamps — has been removed. String and
extended-string message payloads carry neutral placeholders (`Test Rig`,
`Test Amp`, `Test Cab`, `Author`, or empty); the discovery reply uses entirely
synthetic field values; the control channel's `[5, addr, bytes]` blobs carry an
empty payload. The **numeric and structural** content — message types,
addresses, 14-bit values, meter frames, CBOR item shapes and order, and the
exact byte-level framing — is preserved unchanged. No captured identity is
present in any fixture.

## Format

`manifest.json` lists the fixtures. Each fixture is self-describing JSON:

```json
{
  "name": "...",
  "kind": "discovery" | "midi3_stream" | "cbor_stream",
  "description": "...",
  "raw": "<lowercase hex of the bytes to feed>",
  "expected": { ... }        // kind-specific, below
}
```

### kind: `discovery`

`raw` is a `DSCV` discovery reply. Parse it as a TagStream and assert:

- `expected.header` == the 4-char header (`"DSCV"`).
- `expected.key_values` == the list of `[key, value]` pairs, where each field is
  a 4-char ASCII key followed by its ASCII value.

### kind: `midi3_stream`

`raw` is a MIDI3 byte stream. Feed it to the unframer in a single push and
assert whichever of these `expected` fields are present:

- `message_count` — number of complete MIDI messages produced.
- `pending` — bytes left buffered (a trailing partial frame); usually `0`.
- `messages` — the exact decoded messages, as lowercase hex (present on small
  fixtures).
- `status_frames` — `[{ "index": i, "raw": [11 ints] }]`: message `i` is a
  realtime status block whose eleven 14-bit values equal `raw`.
- `function_histogram` — `{ "<function code>": count }` over all messages
  (`"none"` for non-Kemper messages).
- `state` — apply every message to a fresh device state, then assert these
  fields (only those present): `rig_name`, `amp_name`, `cab_name`.

### kind: `cbor_stream`

`raw` is a CBOR control-channel byte stream: the device's reply to one
state-dump trigger, with the live items that arrived in the same window. Feed
it to the CBOR decoder in a single push and assert whichever of these
`expected` fields are present:

- `item_count` — number of complete items produced.
- `pending` — bytes left buffered (a trailing partial item); usually `0`.
- `filler_bytes` — inter-item filler bytes the decoder skipped.
- `numeric_count` — number of numeric `(address, value)` pairs the items carry
  (every single, every element of every run, in document order).
- `strings` — the exact `[address, text]` pairs the walk yields, in document
  order. An empty string is not a value and is not listed.
- `blob_count` — number of `[5, addr, bytes]` items. The decoder carries them
  as opaque byte strings; the walk yields nothing for them.
- `live_items` — `{ "<address>": count }`: how many single `[1, addr, value]`
  items name each listed address, a leading `-1` source flag skipped. An
  address listed with `0` must not appear.
- `dump_end_index` — the index of the last item that is a run based at
  `DUMP_END_ADDRESS` (100800), the run that closes the dump; the live items
  that follow it are the window's tail.
- `state` — fold every item into a fresh device state through the control
  path (each numeric via `apply_cbor`, each string via `apply_cbor_text`, in
  document order), then assert these fields (only those present): `rig_name`,
  `amp_name`, `cab_name`, `current_bank`, `current_rig_slot`, `morph`, `bank`
  (five `{ rig_name, amp_name, cab_name }` slots), and `status_raw` (the eleven
  meter values — all zero, because the meter items are stream-only and must not
  land).

## Harness

Every implementation's conformance suite loads `manifest.json`, then each
fixture, and runs the assertions above for its `kind`. A decode regression in any
language fails that language's build.
