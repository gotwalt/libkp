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
synthetic field values. The **numeric and structural** content — message types,
addresses, 14-bit values, meter frames, and the exact byte-level framing — is
preserved unchanged. No captured identity is present in any fixture.

## Format

`manifest.json` lists the fixtures. Each fixture is self-describing JSON:

```json
{
  "name": "...",
  "kind": "discovery" | "midi3_stream",
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

## Harness

Every implementation's conformance suite loads `manifest.json`, then each
fixture, and runs the assertions above for its `kind`. A decode regression in any
language fails that language's build.
