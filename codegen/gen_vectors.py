#!/usr/bin/env python3
"""Author the shared conformance vectors in spec/vectors/.

The vectors are the *behavioral* contract: hex inputs paired with expected
structured or hex outputs that every libkp implementation (Rust, Python, Swift)
must reproduce. This script implements each wire builder/parser once, asserts
the results against known-good reference values inline (so a mistake here fails
loudly rather than silently corrupting the contract), and serializes the vectors
to JSON.

The vectors, not this script, are what implementations load. Re-run only to
regenerate them from a spec change.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VECTORS = ROOT / "spec" / "vectors"

with open(ROOT / "spec" / "protocol.toml", "rb") as fh:
    P = tomllib.load(fh)

MFR = bytes(P["sysex"]["manufacturer_id"])
DEVICE_OMNI = P["sysex"]["device_omni"]
FULL_SCALE = P["sysex"]["full_scale"]
FN = P["sysex"]["functions"]
TAG_CONT = P["midi3"]["tag_continuation"]


def hx(b: bytes | list[int]) -> str:
    return bytes(b).hex()


# --------------------------------------------------------------------------
# Wire builders / parsers (single reference implementation)
# --------------------------------------------------------------------------

def u14(msb: int, lsb: int) -> int:
    return ((msb & 0x7F) << 7) | (lsb & 0x7F)


def u14_split(v: int) -> tuple[int, int]:
    return ((v >> 7) & 0x7F, v & 0x7F)


def sysex(product, device, function, page, number, values) -> bytes:
    return bytes([0xF0, *MFR, product, device, function, 0x00, page, number, *values, 0xF7])


def request_string(product, device, page, number) -> bytes:
    return sysex(product, device, FN["request_string"], page, number, [])


def request_single(product, device, page, number) -> bytes:
    return sysex(product, device, FN["request_single"], page, number, [])


def request_multi(product, device, page, number) -> bytes:
    return sysex(product, device, FN["request_multi"], page, number, [])


def set_single(product, device, page, number, value) -> bytes:
    msb, lsb = u14_split(value)
    return sysex(product, device, FN["single_param"], page, number, [msb, lsb])


def request_rendered_string(product, device, page, number, value) -> bytes:
    msb, lsb = u14_split(value)
    return sysex(product, device, FN["request_rendered_string"], page, number, [msb, lsb])


def beacon(init, tuner, lease_secs, param_set, product) -> bytes:
    b = P["beacon"]
    flags = b["flag_sysex"]
    if init:
        flags |= b["flag_init"]
    if tuner:
        flags |= b["flag_tunemode"]
    return bytes([0xF0, *MFR, product, DEVICE_OMNI, b["function"], 0x00,
                  b["subcommand"], param_set, flags, lease_secs // 2, 0xF7])


def control_change(channel, controller, value) -> bytes:
    return bytes([0xB0 | (channel & 0x0F), controller & 0x7F, value & 0x7F])


def program_change(channel, program) -> bytes:
    return bytes([0xC0 | (channel & 0x0F), program & 0x7F])


def ext_encode(value, n) -> bytes:
    return bytes([(value >> (7 * (n - 1 - i))) & 0x7F for i in range(n)])


def ext_decode(b: bytes) -> int:
    acc = 0
    for x in b:
        acc = (acc << 7) | (x & 0x7F)
    return acc


def multi_values(number, vals: bytes):
    out = []
    for i in range(0, len(vals) - 1, 2):
        out.append([(number + i // 2) & 0xFF, u14(vals[i], vals[i + 1])])
    return out


def parse_header(msg: bytes):
    if len(msg) < 11 or msg[0] != 0xF0 or bytes(msg[1:4]) != MFR:
        return None
    return {
        "product": msg[4], "device": msg[5], "function": msg[6], "instance": msg[7],
        "page": msg[8], "number": msg[9], "values": bytes(msg[10:len(msg) - 1]),
    }


def parse_extended_string(msg: bytes):
    if (len(msg) < 14 or msg[0] != 0xF0 or bytes(msg[1:4]) != MFR
            or msg[6] != FN["ext_string_param"] or msg[-1] != 0xF7):
        return None
    address = ext_decode(msg[8:13])
    text = bytes(msg[13:-1]).split(b"\x00", 1)[0].decode("ascii", "replace")
    return {"address": address, "text": text}


def parse_rendered_string(msg: bytes):
    h = parse_header(msg)
    if h is None or h["function"] != FN["rendered_string_reply"] or len(h["values"]) < 2:
        return None
    v = h["values"]
    value = u14(v[0], v[1])
    text = bytes(v[2:]).split(b"\x00", 1)[0].decode("ascii", "replace")
    return {"page": h["page"], "number": h["number"], "value": value, "text": text}


def frame(msg: bytes) -> bytes:
    out = bytearray()
    n_groups = max((len(msg) + 2) // 3, 1)
    for i in range(0, max(len(msg), 1), 3):
        chunk = msg[i:i + 3]
        is_last = (i // 3) + 1 == n_groups
        tag = (TAG_CONT + len(chunk)) if is_last else TAG_CONT
        group = bytes(chunk) + bytes(3 - len(chunk))
        out.append(tag)
        out.extend(group)
    return bytes(out)


def unframe(stream: bytes):
    """Return (messages, pending) mirroring the streaming Unframer over one push."""
    messages = []
    current = bytearray()
    i = 0
    partial_start = 0
    while i + 4 <= len(stream):
        tag = stream[i]
        payload = stream[i + 1:i + 4]
        if tag == TAG_CONT:
            valid = 3
        elif tag == 0x15:
            valid = 1
        elif tag == 0x16:
            valid = 2
        elif tag == 0x17:
            valid = 3
        else:
            i += 1
            partial_start = i
            current.clear()
            continue
        i += 4
        partial_start = i
        current.extend(payload[:valid])
        if tag != TAG_CONT:
            messages.append(bytes(current))
            current.clear()
    pending = (len(stream) - partial_start) + len(current)
    return [hx(m) for m in messages], pending


# --------------------------------------------------------------------------
# Inline cross-checks against known-good reference values
# --------------------------------------------------------------------------

def _check():
    # Ground-truth reference bytes (hardware/PDF-validated).
    assert list(beacon(True, True, 30, 0x02, 0x02)) == [0xF0, 0x00, 0x20, 0x33, 0x02, 0x7F, 0x7E, 0x00, 0x40, 0x02, 0x23, 0x0F, 0xF7]
    assert list(request_string(0x00, 0x7F, 0x00, 0x01)) == [0xF0, 0x00, 0x20, 0x33, 0x00, 0x7F, 0x43, 0x00, 0x00, 0x01, 0xF7]
    assert list(set_single(0x00, 0x7F, 0x3D, 0x03, 1)) == [0xF0, 0x00, 0x20, 0x33, 0x00, 0x7F, 0x01, 0x00, 0x3D, 0x03, 0x00, 0x01, 0xF7]
    assert set_single(0x00, 0x7F, 0x3D, 0x03, 0)[10:12] == bytes([0x00, 0x00])
    assert list(request_multi(0x02, 0x7F, 0x34, 0x00)) == [0xF0, 0x00, 0x20, 0x33, 0x02, 0x7F, 0x42, 0x00, 0x34, 0x00, 0xF7]
    assert list(request_rendered_string(0x02, 0x7F, 0x3C, 53, 8192)) == [0xF0, 0x00, 0x20, 0x33, 0x02, 0x7F, 0x7C, 0x00, 0x3C, 0x35, 0x40, 0x00, 0xF7]
    assert control_change(0, 50, 1) == bytes([0xB0, 50, 1])
    assert control_change(15, 47, 0) == bytes([0xBF, 47, 0])
    assert program_change(2, 0) == bytes([0xC2, 0])
    assert multi_values(4, bytes([*u14_split(8192), 0, 0, *u14_split(16383)])) == [[4, 8192], [5, 0], [6, 16383]]
    assert multi_values(0, bytes([1, 2, 0x7F])) == [[0, u14(1, 2)]]
    for v in (0, 1, 6925, 8192, 16383):
        assert u14(*u14_split(v)) == v
    for v in (0, 1, 6925, 16383, 0x123456789):
        assert ext_decode(ext_encode(v, 5)) == v
    # extended string round-trip
    m = bytes([0xF0, *MFR, 0x02, 0x00, 0x07, 0x00]) + ext_encode(1, 5) + b"AC30\x00\xf7"
    assert parse_extended_string(m) == {"address": 1, "text": "AC30"}
    # rendered string reply
    rr = sysex(0x02, DEVICE_OMNI, FN["rendered_string_reply"], 0x3C, 53,
               [*u14_split(8192), ord("<"), ord("0"), ord("."), ord("0"), ord(">"), 0])
    assert parse_rendered_string(rr) == {"page": 0x3C, "number": 53, "value": 8192, "text": "<0.0>"}
    # midi3 frame/unframe of a real capture (28 bytes -> one 19-byte SysEx)
    cap = bytes([0x14, 0xf0, 0x00, 0x20, 0x14, 0x33, 0x02, 0x00, 0x14, 0x06, 0x00, 0x00,
                 0x14, 0x00, 0x06, 0x20, 0x14, 0x05, 0x00, 0x00, 0x14, 0x00, 0x00, 0x10,
                 0x15, 0xf7, 0x00, 0x00])
    msgs, pending = unframe(cap)
    assert msgs == [hx([0xF0, 0x00, 0x20, 0x33, 0x02, 0x00, 0x06, 0x00, 0x00, 0x00, 0x06, 0x20, 0x05, 0x00, 0x00, 0x00, 0x00, 0x10, 0xF7])], msgs
    assert pending == 0
    # frame/unframe round-trips
    for m in (bytes([0xF0, *MFR, 0xF7]), bytes([0xF0, *MFR, 0x01, 0xF7])):
        rt, pend = unframe(frame(m))
        assert rt == [hx(m)] and pend == 0

_check()


# --------------------------------------------------------------------------
# Vector construction
# --------------------------------------------------------------------------

def w(name: str, obj: dict):
    (VECTORS).mkdir(parents=True, exist_ok=True)
    (VECTORS / name).write_text(json.dumps(obj, indent=2) + "\n")
    print(f"wrote spec/vectors/{name}")


def build():
    # u14
    w("u14.json", {
        "description": "14-bit value split into (MSB, LSB) 7-bit bytes, and rejoined.",
        "cases": [{"value": v, "msb": u14_split(v)[0], "lsb": u14_split(v)[1]}
                  for v in (0, 1, 130, 6925, 8192, 16383)],
    })

    # discovery
    def poll(mac):
        out = bytearray(b"DSCV")
        for field in (f"MAC#{mac}".encode(), b"POLL:)"):
            out.append(len(field) + 1)
            out.extend(field)
        out.append(0x00)
        return bytes(out)
    assert len(poll("00:00:00:00:00:00")) == 34
    w("discovery.json", {
        "description": "DSCV discovery poll request bytes for a given client MAC.",
        "poll_request": [
            {"mac": "00:00:00:00:00:00", "hex": hx(poll("00:00:00:00:00:00"))},
            {"mac": "D4:4F:80:00:9E:52", "hex": hx(poll("D4:4F:80:00:9E:52"))},
        ],
        "poll_request_len": 34,
    })

    # midi3: single-push streams -> decoded messages (+ pending leftover bytes).
    unframe_streams = [
        # 28-byte capture -> one 19-byte SysEx, no leftover.
        bytes([0x14, 0xf0, 0x00, 0x20, 0x14, 0x33, 0x02, 0x00, 0x14, 0x06, 0x00, 0x00,
               0x14, 0x00, 0x06, 0x20, 0x14, 0x05, 0x00, 0x00, 0x14, 0x00, 0x00, 0x10,
               0x15, 0xf7, 0x00, 0x00]),
        # continuation then a 3-valid final group.
        bytes([0x14, 0xf0, 0x00, 0x20, 0x14, 0x33, 0x02, 0x00, 0x17, 0x01, 0x02, 0xf7]),
        # a trailing partial frame is reported as pending, not decoded.
        bytes([0x14, 0xf0, 0x00, 0x20, 0x17, 0xf7, 0x00, 0x00, 0x14, 0xaa]),
    ]
    unframe_cases = []
    for s in unframe_streams:
        msgs, pending = unframe(s)
        unframe_cases.append({"stream": hx(s), "messages": msgs, "pending": pending})
    frame_cases = []
    for m in (
        [0xF0, *MFR, 0x02, 0x7F, 0x7E, 0x00, 0x40, 0x02, 0x23, 0x0F, 0xF7],
        [0xF0, *MFR, 0xF7],
        [0xF0, *MFR, 0x01, 0xF7],
        list(set_single(0x00, 0x7F, 0x3D, 0x03, 1)),
    ):
        frame_cases.append({"message": hx(m), "framed": hx(frame(bytes(m)))})
    w("midi3.json", {
        "description": "MIDI3 stream framing: unframe a stream into MIDI messages, and frame a message.",
        "unframe": unframe_cases,
        "frame": frame_cases,
        "note": "frame(msg) then unframe() must round-trip to [msg] with pending 0.",
    })

    # nrpn builders + parsers
    w("nrpn.json", {
        "description": "Kemper SysEx/NRPN builders and parsers.",
        "request_string": [
            {"product": 0x00, "device": 0x7F, "page": 0x00, "number": 0x01, "hex": hx(request_string(0x00, 0x7F, 0x00, 0x01))},
            {"product": 0x02, "device": 0x7F, "page": 0x0A, "number": 0x00, "hex": hx(request_string(0x02, 0x7F, 0x0A, 0x00))},
        ],
        "request_single": [
            {"product": 0x00, "device": 0x7F, "page": 0x0A, "number": 0x04, "hex": hx(request_single(0x00, 0x7F, 0x0A, 0x04))},
        ],
        "request_multi": [
            {"product": 0x02, "device": 0x7F, "page": 0x34, "number": 0x00, "hex": hx(request_multi(0x02, 0x7F, 0x34, 0x00))},
        ],
        "set_single": [
            {"product": 0x00, "device": 0x7F, "page": 0x3D, "number": 0x03, "value": 1, "hex": hx(set_single(0x00, 0x7F, 0x3D, 0x03, 1))},
            {"product": 0x00, "device": 0x7F, "page": 0x3D, "number": 0x03, "value": 0, "hex": hx(set_single(0x00, 0x7F, 0x3D, 0x03, 0))},
            {"product": 0x00, "device": 0x7F, "page": 0x0A, "number": 0x04, "value": 8192, "hex": hx(set_single(0x00, 0x7F, 0x0A, 0x04, 8192))},
        ],
        "request_rendered_string": [
            {"product": 0x02, "device": 0x7F, "page": 0x3C, "number": 53, "value": 8192, "hex": hx(request_rendered_string(0x02, 0x7F, 0x3C, 53, 8192))},
        ],
        "beacon": [
            {"init": True, "tuner": True, "lease_secs": 30, "param_set": 0x02, "product": 0x02, "hex": hx(beacon(True, True, 30, 0x02, 0x02))},
            {"init": False, "tuner": False, "lease_secs": 20, "param_set": 0x02, "product": 0x00, "hex": hx(beacon(False, False, 20, 0x02, 0x00))},
        ],
        "control_change": [
            {"channel": 0, "controller": 50, "value": 1, "hex": hx(control_change(0, 50, 1))},
            {"channel": 15, "controller": 47, "value": 0, "hex": hx(control_change(15, 47, 0))},
        ],
        "header_parse": [
            _hdr(sysex(0x00, 0x00, 0x02, 0x7C, 0x4E, [0x00, 0x00])),
            _hdr(request_string(0x00, 0x7F, 0x00, 0x01)),
        ],
        "multi_values": [
            {"number": 4, "values": hx([*u14_split(8192), 0, 0, *u14_split(16383)]), "pairs": multi_values(4, bytes([*u14_split(8192), 0, 0, *u14_split(16383)]))},
            {"number": 0, "values": "010207", "pairs": multi_values(0, bytes([1, 2, 7]))},
        ],
        "ext_decode": [
            {"bytes": hx(ext_encode(1, 5)), "value": 1},
            {"bytes": hx(ext_encode(1280, 5)), "value": 1280},
            {"bytes": hx(ext_encode(0x123456789, 5)), "value": 0x123456789},
        ],
        "parse_extended_string": [
            {"hex": hx(bytes([0xF0, *MFR, 0x02, 0x00, 0x07, 0x00]) + ext_encode(1, 5) + b"AC30\x00\xf7"), "expected": {"address": 1, "text": "AC30"}},
            {"hex": hx(bytes([0xF0, *MFR, 0x02, 0x00, 0x07, 0x00]) + ext_encode(1280, 5) + b"JCM800\x00\xf7"), "expected": {"address": 1280, "text": "JCM800"}},
            {"hex": hx(set_single(0x00, 0x7F, 0x00, 0x01, 1)), "expected": None},
        ],
        "parse_rendered_string": [
            {"hex": hx(sysex(0x02, DEVICE_OMNI, FN["rendered_string_reply"], 0x3C, 53, [*u14_split(8192), 0x3C, 0x30, 0x2E, 0x30, 0x3E, 0])), "expected": {"page": 0x3C, "number": 53, "value": 8192, "text": "<0.0>"}},
            {"hex": hx(set_single(0x00, 0x7F, 0x3C, 53, 8192)), "expected": None},
        ],
    })

    # controls: op -> expected bytes. Each impl maps op names to its Control API.
    controls = [
        ("gain", {"value": 64}, 0, control_change(0, 72, 64)),
        ("gain", {"value": 64}, 15, control_change(15, 72, 64)),
        ("wah_pedal", {"value": 200}, 0, control_change(0, 1, 200)),
        ("delay_mix", {"value": 10}, 0, control_change(0, 68, 10)),
        ("monitor_volume", {"value": 100}, 0, control_change(0, 73, 100)),
        ("tap_tempo", {}, 0, control_change(0, 30, 1)),
        ("tuner_mode", {"on": True}, 0, control_change(0, 31, 1)),
        ("tuner_mode", {"on": False}, 0, control_change(0, 31, 0)),
        ("toggle_all_modules", {}, 0, control_change(0, 16, 1)),
        ("up", {}, 0, control_change(0, 48, 1)),
        ("down", {}, 0, control_change(0, 49, 1)),
        ("bank_preselect", {"value": 3}, 0, control_change(0, 47, 3)),
        ("rotary_fast", {"on": True}, 0, control_change(0, 33, 1)),
        ("delay_infinity", {"on": True}, 0, control_change(0, 34, 1)),
        ("freeze", {"on": True}, 0, control_change(0, 35, 1)),
        ("morph_button", {"on": True}, 0, control_change(0, 80, 1)),
        ("morph_button", {"on": False}, 0, control_change(0, 80, 0)),
        ("load_slot", {"n": 3}, 0, control_change(0, 52, 1)),
        ("load_slot", {"n": 0}, 0, control_change(0, 50, 1)),
        ("load_slot", {"n": 99}, 0, control_change(0, 54, 1)),
        ("effect_button", {"n": 4}, 0, control_change(0, 78, 1)),
        ("effect_button", {"n": 0}, 0, control_change(0, 75, 1)),
        ("slot_enable", {"slot": "REV", "on": True}, 0, control_change(0, 29, 1)),
        ("slot_enable", {"slot": "DLY", "on": False}, 0, control_change(0, 27, 0)),
        ("slot_enable", {"slot": "A", "on": True}, 0, control_change(0, 17, 1)),
        ("slot_enable", {"slot": "MOD", "on": False}, 0, control_change(0, 24, 0)),
        ("program_change", {"program": 5}, 0, program_change(0, 5)),
        ("program_change", {"program": 200}, 0, program_change(0, 200)),
        ("bank_select", {"msb": 0, "lsb": 3}, 0, control_change(0, 0, 0) + control_change(0, 32, 3)),
        ("bank_select", {"msb": 130, "lsb": 129}, 0, control_change(0, 0, 130) + control_change(0, 32, 129)),
    ]
    w("controls.json", {
        "description": "Control op -> raw MIDI bytes. Each impl maps op names to its Control API; "
                       "values are masked to 7 bits, load_slot clamps to 1..=5, effect_button to 1..=4.",
        "cases": [{"op": op, "params": pr, "channel": ch, "hex": hx(b)} for op, pr, ch, b in controls],
    })

    # params / lookups
    with open(ROOT / "spec" / "parameters.toml", "rb") as fh:
        pa = tomllib.load(fh)
    with open(ROOT / "spec" / "effect-types.toml", "rb") as fh:
        et = tomllib.load(fh)
    w("params.json", {
        "description": "Offline name lookups. null means no mapping.",
        "param_name": [
            {"page": 0x09, "number": 0x03, "name": "Noise Gate Intensity"},
            {"page": 0x0A, "number": 4, "name": "Gain"},
            {"page": 0x7F, "number": 0, "name": "Main Output Volume"},
            {"page": 0x7D, "number": 88, "name": "Looper Record/Playback/Overdub"},
            {"page": 0x00, "number": 1, "name": "Rig Name"},
            {"page": 0x32, "number": 0, "name": "Type"},
            {"page": 0x3D, "number": 4, "name": "Mix"},
            {"page": 0x7C, "number": 84, "name": "Meter: Rig Output Level"},
            {"page": 0x76, "number": 5, "name": "User Scale 1 Step"},
            {"page": 0x04, "number": 5, "name": None},
            {"page": 0x7D, "number": 112, "name": None},
        ],
        "effect_type_name": [
            {"value": 0, "name": "empty"},
            {"value": 32, "name": "Kemper Drive"},
            {"value": 193, "name": "Spring Reverb"},
            {"value": 5, "name": None},
        ],
        "page_name": [
            {"page": 0x7C, "name": "Realtime/Meters"},
            {"page": 0x0A, "name": "Amplifier"},
            {"page": 0x99, "name": None},
        ],
        "string_tag_name": [
            {"number": 1, "name": "Rig Name"},
            {"number": 32, "name": "Cabinet Name"},
            {"number": 99, "name": None},
        ],
        "describe": [
            {"page": 0x09, "number": 0x03, "text": "Input Section: Noise Gate Intensity"},
            {"page": 0x7C, "number": 0x4E, "text": "Realtime/Meters: Tuner Strobe Segment (phase-low)"},
            {"page": 0x99, "number": 0x05, "text": "page 0x99 #5 (0x05)"},
        ],
    })

    # state.apply sequences
    w("state.json", {
        "description": "Apply a sequence of decoded MIDI messages to a fresh DeviceState; assert fields.",
        "cases": _state_cases(),
    })


def _hdr(msg: bytes):
    h = parse_header(msg)
    return {"hex": hx(msg), "product": h["product"], "device": h["device"],
            "function": h["function"], "instance": h["instance"], "page": h["page"],
            "number": h["number"], "values": hx(h["values"])}


def _state_cases():
    cases = []
    # 1. Rig name string ($03 page 0 number 1).
    rig = sysex(0x00, 0x00, FN["string_param"], 0x00, 0x01, list(b"AC30"))
    cases.append({"name": "rig name", "messages": [hx(rig)],
                  "expect": {"rig_name": "AC30"}})
    # 2. Effect REV type + on/off.
    typ = set_single(0x00, 0x00, 0x3D, 0x00, 179)   # Easy Reverb
    on = set_single(0x00, 0x00, 0x3D, 0x03, 1)
    cases.append({"name": "effect REV type+on", "messages": [hx(typ), hx(on)],
                  "expect": {"effect": {"slot": "REV", "kind": 179, "on": True, "type_name": "Easy Reverb"}}})
    # 3. Meter block -> status.raw (11 values).
    raw = [100, 200, 300, 8000, 12000, 5000, 9000, 4000, 0, 6000, 0]
    vals = []
    for v in raw:
        vals.extend(u14_split(v))
    meter = sysex(0x00, 0x00, FN["multi_param"], 0x7C, 0x4E, vals)
    cases.append({"name": "meter block", "messages": [hx(meter)],
                  "expect": {"status_raw": raw}})
    # 4. Tempo bpm (value = bpm*64) and rig volume.
    tempo = set_single(0x00, 0x00, 0x04, 0x00, 120 * 64)
    rvol = set_single(0x00, 0x00, 0x04, 0x01, 9000)
    cases.append({"name": "tempo + rig volume", "messages": [hx(tempo), hx(rvol)],
                  "expect": {"tempo_bpm": 120, "rig_volume": 9000}})
    # 5. Amp on/off + gain.
    ampon = set_single(0x00, 0x00, 0x0A, 0x02, 1)
    gain = set_single(0x00, 0x00, 0x0A, 0x04, 6925)
    cases.append({"name": "amp on + gain", "messages": [hx(ampon), hx(gain)],
                  "expect": {"amp_on": True, "amp_gain": 6925}})
    # 6. Morph + tuner note + main volume.
    morph = set_single(0x00, 0x00, 0x00, 0x0B, 4096)
    note = set_single(0x00, 0x00, 0x7D, 0x54, 9)
    mainv = set_single(0x00, 0x00, 0x7F, 0x00, 12000)
    cases.append({"name": "morph + tuner note + main vol",
                  "messages": [hx(morph), hx(note), hx(mainv)],
                  "expect": {"morph": 4096, "tuner_note": 9, "main_volume": 12000}})
    return cases


if __name__ == "__main__":
    build()
    print("all vectors written and cross-checked")
