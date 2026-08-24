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

with open(ROOT / "spec" / "parameters.toml", "rb") as fh:
    PARAMS = tomllib.load(fh)

MFR = bytes(P["sysex"]["manufacturer_id"])
DEVICE_OMNI = P["sysex"]["device_omni"]
FULL_SCALE = P["sysex"]["full_scale"]
FN = P["sysex"]["functions"]
TAG_CONT = P["midi3"]["tag_continuation"]
BANK_SLOTS = PARAMS["well_known"]["bank_slots"]
MORPH_PAGE = PARAMS["well_known"]["page_morph"]
MORPH_NUMBER = PARAMS["well_known"]["morph_number"]
MORPH_BUTTON_NUMBER = PARAMS["well_known"]["morph_button_number"]
CURRENT_BANK_ADDRESS = PARAMS["well_known"]["current_bank_address"]
CURRENT_RIG_SLOT_ADDRESS = PARAMS["well_known"]["current_rig_slot_address"]
MORPH_ADDRESS = PARAMS["well_known"]["morph_address"]
CBOR = P["cbor"]


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


def request_extended_param(product, device, address) -> bytes:
    return bytes([0xF0, *MFR, product, device, FN["request_ext_param"], 0x00,
                  *ext_encode(address, 5), 0xF7])


def ext_param(product, device, address, value) -> bytes:
    return bytes([0xF0, *MFR, product, device, FN["ext_param"], 0x00,
                  *ext_encode(address, 5), *ext_encode(value, 5), 0xF7])


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


def parse_extended_param(msg: bytes):
    if (len(msg) < 19 or msg[0] != 0xF0 or bytes(msg[1:4]) != MFR
            or msg[6] != FN["ext_param"] or msg[-1] != 0xF7):
        return None
    return {"address": ext_decode(msg[8:13]), "value": ext_decode(msg[13:18])}


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
# CBOR (RFC 8949): just enough of an encoder for the control channel's items —
# unsigned integers, text strings, arrays and tags, each with the shortest head
# that holds its argument, which is the only encoding the device accepts.
# --------------------------------------------------------------------------

def cbor_head(major: int, arg: int) -> bytes:
    if arg < 24:
        return bytes([major << 5 | arg])
    for info, width in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if arg < 1 << (8 * width):
            return bytes([major << 5 | info]) + arg.to_bytes(width, "big")
    raise ValueError(f"CBOR argument out of range: {arg}")


def cbor(v) -> bytes:
    """Encode an int, str, list, or ``("tag", n, value)`` tuple."""
    if isinstance(v, int):
        return cbor_head(0, v) if v >= 0 else cbor_head(1, -1 - v)
    if isinstance(v, str):
        raw = v.encode("utf-8")
        return cbor_head(3, len(raw)) + raw
    if isinstance(v, list):
        return cbor_head(4, len(v)) + b"".join(cbor(x) for x in v)
    if isinstance(v, tuple) and v[0] == "tag":
        return cbor_head(6, v[1]) + cbor(v[2])
    raise TypeError(f"cannot encode {v!r}")


def cbor_item(*payload) -> bytes:
    """One control-channel item: ``tag(item_tag)([selector, ...])``."""
    return cbor(("tag", CBOR["item_tag"], list(payload)))


def cbor_param_write(addr: int, value: int) -> bytes:
    return cbor_item(CBOR["selector_single"], addr, value)


def cbor_multi(base: int, *values: int) -> bytes:
    return cbor_item(CBOR["selector_multi"], base, *values)


def cbor_string(addr: int, text: str) -> bytes:
    return cbor_item(CBOR["selector_string"], addr, text)


def cbor_filler(n: int) -> bytes:
    return bytes([CBOR["filler_byte"]]) * n


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
    # extended param: ground truth from a captured position reply — current bank
    # (address 100701) reading 1.
    assert list(ext_param(0x02, 0x00, CURRENT_BANK_ADDRESS, 1)) == [
        0xF0, 0x00, 0x20, 0x33, 0x02, 0x00, 0x06, 0x00,
        0x00, 0x00, 0x06, 0x12, 0x5D, 0x00, 0x00, 0x00, 0x00, 0x01, 0xF7]
    assert list(request_extended_param(0x00, 0x7F, CURRENT_RIG_SLOT_ADDRESS)) == [
        0xF0, 0x00, 0x20, 0x33, 0x00, 0x7F, 0x46, 0x00,
        0x00, 0x00, 0x06, 0x12, 0x5E, 0xF7]
    for addr, value in ((CURRENT_BANK_ADDRESS, 24), (102405, 0x123456789)):
        assert parse_extended_param(ext_param(0x02, 0x00, addr, value)) == {
            "address": addr, "value": value}
    assert parse_extended_param(set_single(0x00, 0x7F, 0x00, 0x01, 1)) is None
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
    # CBOR heads at each width boundary, and the state-dump trigger as captured
    # on the wire: tag(1) [1, 102528, 1] with a 4-byte address argument.
    assert hx(cbor(23)) == "17" and hx(cbor(24)) == "1818" and hx(cbor(255)) == "18ff"
    assert hx(cbor(256)) == "190100" and hx(cbor(65536)) == "1a00010000"
    assert hx(cbor(-1)) == "20" and hx(cbor("AC30")) == "6441433330"
    assert hx(cbor_param_write(CBOR["state_dump_trigger_address"],
                               CBOR["state_dump_trigger_value"])) == "c183011a0001908001"
    assert hx(cbor_param_write(15953, 0)) == "c18301193e5100"

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
        "request_extended_param": [
            {"product": 0x00, "device": 0x7F, "address": CURRENT_BANK_ADDRESS, "hex": hx(request_extended_param(0x00, 0x7F, CURRENT_BANK_ADDRESS))},
            {"product": 0x00, "device": 0x7F, "address": CURRENT_RIG_SLOT_ADDRESS, "hex": hx(request_extended_param(0x00, 0x7F, CURRENT_RIG_SLOT_ADDRESS))},
        ],
        "parse_extended_param": [
            {"hex": hx(ext_param(0x02, 0x00, CURRENT_BANK_ADDRESS, 24)), "expected": {"address": CURRENT_BANK_ADDRESS, "value": 24}},
            {"hex": hx(ext_param(0x02, 0x00, CURRENT_RIG_SLOT_ADDRESS, 3)), "expected": {"address": CURRENT_RIG_SLOT_ADDRESS, "value": 3}},
            {"hex": hx(ext_param(0x02, 0x00, 102405, 0x123456789)), "expected": {"address": 102405, "value": 0x123456789}},
            # The scheme can carry 35 bits; an address past 32 is malformed,
            # not an address to wrap onto some other parameter.
            {"hex": hx(ext_param(0x02, 0x00, 0x1_0000_0000, 7)), "expected": None},
            {"hex": hx(set_single(0x00, 0x7F, 0x00, 0x01, 1)), "expected": None},
        ],
        "parse_extended_string": [
            {"hex": hx(bytes([0xF0, *MFR, 0x02, 0x00, 0x07, 0x00]) + ext_encode(1, 5) + b"AC30\x00\xf7"), "expected": {"address": 1, "text": "AC30"}},
            {"hex": hx(bytes([0xF0, *MFR, 0x02, 0x00, 0x07, 0x00]) + ext_encode(1280, 5) + b"JCM800\x00\xf7"), "expected": {"address": 1280, "text": "JCM800"}},
            # As for the $06: 35 encodable bits, 32 addressable ones.
            {"hex": hx(bytes([0xF0, *MFR, 0x02, 0x00, 0x07, 0x00]) + ext_encode(0x1_0000_0000, 5) + b"X\x00\xf7"), "expected": None},
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
        ("up", {}, 0, control_change(0, 48, 1) + control_change(0, 48, 0)),
        ("down", {}, 0, control_change(0, 49, 1) + control_change(0, 49, 0)),
        ("bank_preselect", {"value": 3}, 0, control_change(0, 47, 3)),
        ("rotary_fast", {"on": True}, 0, control_change(0, 33, 1)),
        ("delay_infinity", {"on": True}, 0, control_change(0, 34, 1)),
        ("freeze", {"on": True}, 0, control_change(0, 35, 1)),
        ("morph_button", {"on": True}, 0, control_change(0, 80, 1)),
        ("morph_button", {"on": False}, 0, control_change(0, 80, 0)),
        ("load_slot", {"n": 3}, 0, control_change(0, 52, 1) + control_change(0, 52, 0)),
        ("load_slot", {"n": 0}, 0, control_change(0, 50, 1) + control_change(0, 50, 0)),
        ("load_slot", {"n": 99}, 0, control_change(0, 54, 1) + control_change(0, 54, 0)),
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
                       "values are masked to 7 bits, load_slot clamps to 1..=5, effect_button to 1..=4. "
                       "Momentary controls (up, down, load_slot) emit a press (value 1) followed by a release (value 0) as one 6-byte message.",
        "cases": [{"op": op, "params": pr, "channel": ch, "hex": hx(b)} for op, pr, ch, b in controls],
    })

    # params / lookups
    with open(ROOT / "spec" / "parameters.toml", "rb") as fh:
        pa = tomllib.load(fh)
    with open(ROOT / "spec" / "effect-types.toml", "rb") as fh:
        et = tomllib.load(fh)

    def category_of(v: int) -> str | None:
        for c in et["effect_categories"]:
            if c["min"] <= v <= c["max"]:
                return c["name"]
        return None

    # Ground-truth category expectations, cross-checked against the spec table:
    # block interiors, both sides of a block boundary, an unnamed Type value,
    # the empty type, and values past the last block.
    effect_category_cases = [
        {"value": 0, "name": None},
        {"value": 1, "name": "Wah"},
        {"value": 16, "name": "Wah"},
        {"value": 17, "name": "Shaper"},
        {"value": 32, "name": "Distortion"},
        {"value": 49, "name": "Dynamics"},
        {"value": 76, "name": "Modulation"},
        {"value": 89, "name": "Phaser & Flanger"},
        {"value": 112, "name": "Booster"},
        {"value": 121, "name": "Effect Loop"},
        {"value": 129, "name": "Pitch"},
        {"value": 161, "name": "Delay"},
        {"value": 179, "name": "Reverb"},
        {"value": 207, "name": "Reverb"},
        {"value": 208, "name": None},
        {"value": 300, "name": None},
    ]
    for c in effect_category_cases:
        assert category_of(c["value"]) == c["name"], c

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
        "effect_category_name": effect_category_cases,
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

    # state.apply sequences: the older cases feed unframed MIDI3 messages
    # through `apply`; the transport-tagged ones drive the fold through every
    # entry point and pin which events each step raises.
    w("state.json", {
        "description": "Apply a sequence of updates to a fresh DeviceState; assert fields. "
                       "A case carries either \"messages\" (unframed MIDI3 hex, each through "
                       "apply) or \"steps\" (midi3 / cbor / cbor_text / cbor_dump / "
                       "cbor_dump_text / dump_begin / dump_end, each naming the entry point it "
                       "drives); a \"steps\" case also pins expect.events, the ordered event "
                       "names raised across all steps, and expect.slow_steps, how many steps "
                       "returned an outcome with the snapshot flag set. A \"steps\" case may "
                       "also pin expect.positions: every flat rig index the steps reported for "
                       "the Navigator, in order. A position row reports the tree's index once "
                       "both halves are known and it fits sixteen bits -- when it stores, and "
                       "equally when dedupe swallows an unchanged report, because a reload of "
                       "the loaded rig is confirmed by a report that changes nothing.",
        "cases": _state_cases() + _state_step_cases(),
    })

    # cbor: the one write this library sends, and reading a dump's position,
    # strings and morph back out of a decoded item stream.
    w("cbor.json", {
        "description": "The native CBOR control channel: the state-dump write, and reading the "
                       "current bank/rig position and morph out of a decoded dump.",
        "param_write": [
            {"addr": addr, "value": value, "hex": hx(cbor_param_write(addr, value))}
            for addr, value in ((15953, 0), (102405, 19),
                                (CBOR["state_dump_trigger_address"],
                                 CBOR["state_dump_trigger_value"]))
        ],
        "state_dump_request": {
            "hex": hx(cbor_param_write(CBOR["state_dump_trigger_address"],
                                       CBOR["state_dump_trigger_value"])),
        },
        "extract_snapshot": _cbor_snapshot_cases(),
    })

    # navigation: the pure rig-load serializer, driven one input at a time.
    w("navigation.json", {
        "description": "The Navigator's pure state machine {aim, sent, in_flight, awaiting}, "
                       "started fresh (all None/false) and driven by ordered steps: "
                       "navigate:n (aim = n; pump unless in flight), settle:true (the "
                       "RIG_LOAD_SETTLE_MS timer fired: in_flight = false; if the aim is the "
                       "sent index and still unconfirmed, start_window and awaiting = true; "
                       "then pump), window:true (the PENDING_WINDOW_MS timer fired: while "
                       "awaiting, drop the aim and forget the sent index), position:n (a "
                       "position report from either wire: only a report equal to the aim "
                       "retires it, clearing aim, sent and awaiting). pump sends the aim when "
                       "nothing is in flight and the aim is not the index already sent: "
                       "sent = aim, in_flight = true, awaiting = false -> send:n, start_settle. "
                       "expect.sent is every send's index in order (the wire log), "
                       "expect.actions the exact ordered actions across all steps "
                       "(send:n, start_settle, start_window, settled:n, dropped:n), and "
                       "aim / in_flight / awaiting the final state. A stale timer is "
                       "harmless: settle with nothing aimed and window while not awaiting do "
                       "nothing, so the model never needs to cancel one.",
        "cases": _navigation_cases(),
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
    morph = set_single(0x00, 0x00, MORPH_PAGE, MORPH_NUMBER, 4096)
    note = set_single(0x00, 0x00, 0x7D, 0x54, 9)
    mainv = set_single(0x00, 0x00, 0x7F, 0x00, 12000)
    cases.append({"name": "morph + tuner note + main vol",
                  "messages": [hx(morph), hx(note), hx(mainv)],
                  "expect": {"morph": 4096, "tuner_note": 9, "main_volume": 12000}})
    # 6b. The morph button is momentary: it says a morph happened, and moves
    #     nothing in the tree. Only the position (0x77) is stored.
    cases.append({"name": "morph button leaves the position alone",
                  "messages": [hx(set_single(0x00, 0x00, MORPH_PAGE, MORPH_BUTTON_NUMBER, 1))],
                  "expect": {"morph": None}})
    # 6c. Regression: 0x0B is *not* the morph. It is a real address that answers
    #     a request with a constant 0 whether the rig is morphed or at base, and
    #     is never pushed — so a value landing there must move nothing, or the
    #     mistake it caused goes unnoticed again.
    cases.append({"name": "0x0B is not the morph",
                  "messages": [hx(set_single(0x00, 0x00, MORPH_PAGE, 0x0B, 16383))],
                  "expect": {"morph": None}})
    # 7. The device's position: a `$06` Extended Parameter per index, both
    #    0-based. Bank 24 slot 3 is flat index 123 — the last rig of a 25-bank
    #    device.
    cases.append({"name": "position from extended params",
                  "messages": [hx(ext_param(0x02, 0x00, CURRENT_BANK_ADDRESS, 24)),
                               hx(ext_param(0x02, 0x00, CURRENT_RIG_SLOT_ADDRESS, 3))],
                  "expect": {"current_bank": 24, "current_rig_slot": 3,
                             "current_rig_index": 123}})
    # 8. The two arrive independently — the device pushes only the one that
    #    changed — so a bank alone must land, leaving the slot unknown.
    cases.append({"name": "position, bank alone",
                  "messages": [hx(ext_param(0x02, 0x00, CURRENT_BANK_ADDRESS, 40))],
                  "expect": {"current_bank": 40, "current_rig_slot": None,
                             "current_rig_index": None}})
    # 9. A later push of one half must not disturb the other: slot 0 of bank 40
    #    is flat index 200.
    cases.append({"name": "position, halves pushed apart",
                  "messages": [hx(ext_param(0x02, 0x00, CURRENT_RIG_SLOT_ADDRESS, 2)),
                               hx(ext_param(0x02, 0x00, CURRENT_BANK_ADDRESS, 40)),
                               hx(ext_param(0x02, 0x00, CURRENT_RIG_SLOT_ADDRESS, 0))],
                  "expect": {"current_bank": 40, "current_rig_slot": 0,
                             "current_rig_index": 200}})
    # 10. An extended address the state tree does not track changes nothing —
    #     102405 is the free-running counter the device pushes every second.
    cases.append({"name": "position ignores an untracked extended address",
                  "messages": [hx(ext_param(0x02, 0x00, CURRENT_BANK_ADDRESS, 24)),
                               hx(ext_param(0x02, 0x00, 102405, 31))],
                  "expect": {"current_bank": 24, "current_rig_slot": None,
                             "current_rig_index": None}})
    return cases


# The transport-tagged cases below drive `DeviceState.apply_update` through
# every entry point — `apply` for a MIDI3 message, `apply_cbor` /
# `apply_cbor_text` for a live CBOR item, and a dump-phase update for an item
# of the CBOR state dump — against one fresh state, and pin the ordered event
# names and the number of snapshot-flagged steps alongside the tree. Each
# expectation is derived from the fold contract's rules and the routing
# table's rows (spec/state.toml), never from an implementation; the name of
# each case says which rule it pins so a failure reads as a contract
# violation, not a mystery.

# Every event name the fold may raise; a typo here would pin nothing, so the
# case builder refuses names outside this set.
_EVENT_NAMES = frozenset({
    "string_tag", "rig_changed", "bank_preview", "effect_changed", "param_changed",
    "status", "beat_pulse", "tempo_bpm", "morph_changed", "morph_button",
    "tuner_deviance", "tuner_note", "rendered_string", "current_position",
})
_STEP_KINDS = frozenset({"midi3", "cbor", "cbor_text", "cbor_dump", "cbor_dump_text",
                         "dump_begin", "dump_end"})

WK = PARAMS["well_known"]


def flat(page: int, number: int) -> int:
    """The flat address the routing table keys on: `page * 128 + number`."""
    return page * 128 + number


def step_midi3(msg: bytes) -> dict:
    return {"midi3": hx(msg)}


def step_cbor(address: int, value: int) -> dict:
    return {"cbor": [address, value]}


def step_cbor_text(address: int, text: str) -> dict:
    return {"cbor_text": [address, text]}


def step_cbor_dump(address: int, value: int) -> dict:
    return {"cbor_dump": [address, value]}


def step_cbor_dump_text(address: int, text: str) -> dict:
    return {"cbor_dump_text": [address, text]}


STEP_DUMP_BEGIN = {"dump_begin": True}
STEP_DUMP_END = {"dump_end": True}


def _steps_case(name: str, steps: list[dict], events: list[str], slow_steps: int,
                positions: list[int] | None = None, **tree) -> dict:
    """One transport-tagged case; the tree keys are whatever the loaders assert."""
    for s in steps:
        assert len(s) == 1 and next(iter(s)) in _STEP_KINDS, s
    unknown = set(events) - _EVENT_NAMES
    assert not unknown, f"{name}: unknown event names {sorted(unknown)}"
    assert 0 <= slow_steps <= len(steps), name
    expect = {**tree, "events": events, "slow_steps": slow_steps}
    if positions is not None:
        assert all(isinstance(i, int) and 0 <= i <= 0xFFFF for i in positions), name
        expect["positions"] = positions
    return {"name": name, "steps": steps, "expect": expect}


def _state_step_cases():
    # Addresses, every one derived from the well-known keys the table's rows
    # reference, so this file never spells an address the spec does not.
    rig_name_addr = flat(WK["page_strings"], WK["string_rig_name"])
    morph_button_addr = flat(WK["page_morph"], WK["morph_button_number"])
    tempo_addr = flat(WK["page_rig_settings"], WK["tempo_number"])
    rig_volume_addr = flat(WK["page_rig_settings"], WK["rig_volume_number"])
    amp_on_addr = flat(WK["amp_page"], WK["amp_on_number"])
    beat_pulse_addr = flat(WK["page_realtime"], WK["beat_pulse_number"])
    meter_base_addr = flat(WK["page_realtime"], WK["meter_block_number"])
    tuner_note_addr = flat(WK["page_tuner_note"], WK["tuner_note_number"])
    headphone_addr = flat(WK["system_page"], WK["headphone_volume_number"])
    bank_rig_name_addr = flat(WK["page_bank_preview"], WK["bank_rig_name_base"])
    bpm_scale = WK["tempo_bpm_scale"]
    assert MORPH_ADDRESS == flat(MORPH_PAGE, MORPH_NUMBER)
    # The REV slot's page, as the "effect REV type+on" case above spells it.
    rev_page = 0x3D
    rev_type_addr = flat(rev_page, PARAMS["effect_param_numbers"]["type"])
    rev_on_addr = flat(rev_page, PARAMS["effect_param_numbers"]["state"])
    assert (rev_type_addr, rev_on_addr) == (7808, 7811)
    secret = CBOR["sensitive_addresses"][0]
    fresh_status = [0] * WK["meter_count"]

    def bank(value: int) -> dict:
        return step_midi3(ext_param(0x02, 0x00, CURRENT_BANK_ADDRESS, value))

    def rig_slot(value: int) -> dict:
        return step_midi3(ext_param(0x02, 0x00, CURRENT_RIG_SLOT_ADDRESS, value))

    def meter_block(raw: list[int]) -> dict:
        vals = []
        for v in raw:
            vals.extend(u14_split(v))
        return step_midi3(sysex(0x00, 0x00, FN["multi_param"], WK["page_realtime"],
                                WK["meter_block_number"], vals))

    def string_tag(page: int, number: int, text: str) -> dict:
        return step_midi3(sysex(0x00, 0x00, FN["string_param"], page, number, list(text.encode())))

    def single(page: int, number: int, value: int) -> dict:
        return step_midi3(set_single(0x00, 0x00, page, number, value))

    raw = [100, 200, 300, 8000, 12000, 5000, 9000, 4000, 0, 6000, 0]
    slot_names = ["Slot A", "Slot B", "Slot C", "Slot D", "Slot E"]

    return [
        # --- Rule 7 across wires: both channels name the same address, so a
        #     value already stored is a no-op whichever wire repeats it.
        _steps_case("rule 7: the same position on both wires raises one current_position",
                    [bank(2), step_cbor(CURRENT_BANK_ADDRESS, 2)],
                    ["current_position"], 1,
                    current_bank=2, current_rig_slot=None),
        _steps_case("rule 3: a stream morph position is accepted, and rule 7 drops the equal CBOR copy",
                    [single(MORPH_PAGE, MORPH_NUMBER, 8000), step_cbor(MORPH_ADDRESS, 8000)],
                    ["morph_changed"], 1,
                    morph=8000),
        _steps_case("rule 7: the same rig name on both wires raises one string_tag and one rig_changed",
                    [string_tag(WK["page_strings"], WK["string_rig_name"], "AC30"),
                     step_cbor_text(rig_name_addr, "AC30")],
                    ["string_tag", "rig_changed"], 1,
                    rig_name="AC30"),
        _steps_case("rule 7: a repeated control numeric is a no-op",
                    [step_cbor(rig_volume_addr, 100), step_cbor(rig_volume_addr, 100)],
                    ["param_changed"], 1,
                    rig_volume=100),
        # Dedupe compares what the row would store, not the wire value: 1 and
        # 5 are both "on" for a bool row.
        _steps_case("rule 7: dedupe compares the decoded value, not the wire value",
                    [step_cbor(amp_on_addr, 1), step_cbor(amp_on_addr, 5)],
                    ["param_changed"], 1,
                    amp_on=True),
        _steps_case("rule 7: the momentary morph button is never deduped",
                    [single(MORPH_PAGE, MORPH_BUTTON_NUMBER, 1),
                     single(MORPH_PAGE, MORPH_BUTTON_NUMBER, 1)],
                    ["morph_button", "morph_button"], 0,
                    morph=None),
        _steps_case("rule 7: the beat pulse is never deduped",
                    [single(WK["page_realtime"], WK["beat_pulse_number"], 1),
                     single(WK["page_realtime"], WK["beat_pulse_number"], 1)],
                    ["beat_pulse", "beat_pulse"], 0),
        _steps_case("rule 7: the meter frame is never deduped",
                    [meter_block(raw), meter_block(raw)],
                    ["status", "status"], 0,
                    status_raw=raw),
        # Dedupe silences the event, not the Navigator: re-loading the rig
        # already loaded pushes an unchanged position, and that push is the
        # confirmation the aim is waiting for.
        _steps_case("rule 7: a deduped position still reports the rig index",
                    [bank(1), rig_slot(3), bank(1), rig_slot(3)],
                    ["current_position", "current_position"], 2,
                    positions=[8, 8, 8],
                    current_bank=1, current_rig_slot=3, current_rig_index=8),
        # A `$02` at the meter base IS the frame, whatever its length: a
        # truncated read zero-fills the tail, an extra value is ignored --
        # never a run of bogus generic params at meter rate.
        _steps_case("rule 1: a short meter frame zero-fills its tail",
                    [meter_block(raw[:-1])],
                    ["status"], 0,
                    status_raw=raw[:-1] + [0]),
        _steps_case("rule 1: an overlong meter frame keeps its span",
                    [meter_block(raw + [7])],
                    ["status"], 0,
                    status_raw=raw),
        _steps_case("rule 7: the tuner deviance is deduped on the fast lane",
                    [single(WK["page_realtime"], WK["tuner_deviance_number"], 8192),
                     single(WK["page_realtime"], WK["tuner_deviance_number"], 8192)],
                    ["tuner_deviance"], 0),
        # --- Rule 8: the control wire lands in the same rows as the stream,
        #     raising each row's declared event.
        _steps_case("rule 8: CBOR numerics land in tempo and rig volume with the rows' events",
                    [step_cbor(tempo_addr, 84 * bpm_scale), step_cbor(rig_volume_addr, 8106)],
                    ["tempo_bpm", "param_changed"], 2,
                    tempo_bpm=84, rig_volume=8106),
        _steps_case("rule 8: CBOR text lands in rig_name and raises string_tag then rig_changed",
                    [step_cbor_text(rig_name_addr, "AC30")],
                    ["string_tag", "rig_changed"], 1,
                    rig_name="AC30"),
        _steps_case("rule 8: effect rows land from the control wire",
                    [step_cbor(rev_type_addr, 179), step_cbor(rev_on_addr, 1)],
                    ["effect_changed", "effect_changed"], 2,
                    effect={"slot": "REV", "kind": 179, "on": True, "type_name": "Easy Reverb"}),
        _steps_case("rule 8: the control wire reaches the output volumes",
                    [step_cbor(headphone_addr, 1000)],
                    ["param_changed"], 1,
                    headphone_volume=1000),
        _steps_case("rule 8: dump text fills the bank preview one slot at a time",
                    [step_cbor_dump_text(bank_rig_name_addr + i, n) for i, n in enumerate(slot_names)],
                    ["bank_preview"] * BANK_SLOTS, BANK_SLOTS,
                    bank=[{"slot": i, "rig_name": n} for i, n in enumerate(slot_names)]),
        _steps_case("rule 8: the stream reaches the bank preview too",
                    [string_tag(WK["page_bank_preview"], WK["bank_amp_name_base"], "Twin")],
                    ["bank_preview"], 1,
                    bank=[{"slot": 0, "amp_name": "Twin"}]),
        # --- Rule 2: no row, so the stream's generic fallback or nothing.
        _steps_case("rule 2: an untracked control address is silent",
                    [step_cbor(secret, 1)],
                    [], 0),
        _steps_case("rule 2: an untracked stream numeric still raises a fast param_changed",
                    [single(WK["page_rig_settings"], 0x10, 5)],
                    ["param_changed"], 0),
        _steps_case("rule 2: an untracked stream text is silent",
                    [string_tag(WK["page_strings"], 0x05, "x")],
                    [], 0,
                    rig_name=None),
        _steps_case("rule 2: an untracked extended address on the stream is silent",
                    [step_midi3(ext_param(0x02, 0x00, 102405, 31))],
                    [], 0,
                    current_bank=None),
        # --- Rule 3: a stream-only row ignores the control channel's copy.
        _steps_case("rule 3: the control copy of the meter block is dropped",
                    [step_cbor(meter_base_addr + 3, 1234)],
                    [], 0,
                    status_raw=fresh_status),
        _steps_case("rule 3: the control copy of the beat pulse is dropped",
                    [step_cbor(beat_pulse_addr, 16383)],
                    [], 0,
                    status_raw=fresh_status),
        _steps_case("rule 3: the control copy of the tuner note is dropped",
                    [step_cbor(tuner_note_addr, 9)],
                    [], 0),
        _steps_case("rule 3: the control copy of the morph button is dropped",
                    [step_cbor(morph_button_addr, 1)],
                    [], 0,
                    morph=None),
        # --- Rule 4: page 0 is dual-use, so a row accepts one face only. A
        #     mismatch is "no route", which on the stream means the generic
        #     fallback and on the control channel means silence.
        _steps_case("rule 4: text at a numeric row is untracked",
                    [step_cbor(rig_volume_addr, 100), step_cbor_text(rig_volume_addr, "x")],
                    ["param_changed"], 1,
                    rig_volume=100),
        _steps_case("rule 4: a control numeric at a text row is untracked",
                    [step_cbor_text(rig_name_addr, "AC30"), step_cbor(rig_name_addr, 5)],
                    ["string_tag", "rig_changed"], 1,
                    rig_name="AC30"),
        _steps_case("rule 4: a stream numeric at a text row falls through to param_changed",
                    [single(WK["page_strings"], WK["string_rig_name"], 5)],
                    ["param_changed"], 0,
                    rig_name=None),
        # --- Rule 5: the row's kind decides the range and the decode.
        _steps_case("rule 5: a u14 row drops a value past 16383",
                    [step_cbor(rig_volume_addr, 100), step_cbor(rig_volume_addr, 70000)],
                    ["param_changed"], 1,
                    rig_volume=100),
        _steps_case("rule 5: a u16 row drops a value past 65535",
                    [step_cbor(CURRENT_BANK_ADDRESS, 70000)],
                    [], 0,
                    current_bank=None, current_rig_index=None),
        _steps_case("rule 5: a u16 row keeps a value past 14 bits",
                    [step_cbor(CURRENT_BANK_ADDRESS, 40000)],
                    ["current_position"], 1,
                    current_bank=40000),
        _steps_case("rule 5: the stream's extended value is range-checked by the same row",
                    [bank(70000)],
                    [], 0,
                    current_bank=None),
        # The flat index is bounded like everything else: a pair of halves
        # whose product leaves sixteen bits yields no index and reports no
        # position, rather than trapping or wrapping into a plausible one.
        _steps_case("rule 5: a position past sixteen bits yields no index",
                    [bank(13107), rig_slot(3)],
                    ["current_position", "current_position"], 2,
                    positions=[],
                    current_bank=13107, current_rig_slot=3, current_rig_index=None),
        _steps_case("rule 5: the largest sixteen-bit index still reports",
                    [bank(13106), rig_slot(4)],
                    ["current_position", "current_position"], 2,
                    positions=[65534],
                    current_rig_index=65534),
        _steps_case("rule 5: a u7 row keeps the low seven bits",
                    [single(WK["page_tuner_note"], WK["tuner_note_number"], 0x80 | 9)],
                    ["tuner_note"], 1,
                    tuner_note=9),
        # --- Rule 6: between begin_dump and end_dump a live value beats the
        #     dump's copy of the same address, whichever order they arrive in.
        _steps_case("rule 6: a live position during the dump beats the dump's stale copy",
                    [STEP_DUMP_BEGIN, bank(3), step_cbor_dump(CURRENT_BANK_ADDRESS, 2), STEP_DUMP_END],
                    ["current_position"], 1,
                    current_bank=3),
        _steps_case("rule 6: a dump position dropped by a live push reports nothing",
                    [STEP_DUMP_BEGIN, bank(3), rig_slot(0),
                     step_cbor_dump(CURRENT_BANK_ADDRESS, 2), STEP_DUMP_END],
                    ["current_position", "current_position"], 2,
                    positions=[15],
                    current_bank=3, current_rig_slot=0, current_rig_index=15),
        _steps_case("rule 6: after the dump ends a live position lands over the dump's",
                    [STEP_DUMP_BEGIN, step_cbor_dump(CURRENT_BANK_ADDRESS, 2), STEP_DUMP_END, bank(3)],
                    ["current_position", "current_position"], 2,
                    current_bank=3),
        _steps_case("rule 6: a live string during the dump beats the dump's stale text",
                    [STEP_DUMP_BEGIN,
                     string_tag(WK["page_strings"], WK["string_rig_name"], "AC30"),
                     step_cbor_dump_text(rig_name_addr, "Old"),
                     STEP_DUMP_END],
                    ["string_tag", "rig_changed"], 1,
                    rig_name="AC30"),
        # The touch is recorded before the dedupe: a live repeat of the stored
        # value changes nothing, but still says "this address is current".
        _steps_case("rule 6: a live update marks its address even when deduped",
                    [bank(3), STEP_DUMP_BEGIN, bank(3), step_cbor_dump(CURRENT_BANK_ADDRESS, 2), STEP_DUMP_END],
                    ["current_position"], 1,
                    current_bank=3),
        # Rules 3 and 5 come first, so a live value they drop never reaches
        # the touch: the dump's copy is still the best information.
        _steps_case("rule 6: a live update dropped by range does not mark its address",
                    [STEP_DUMP_BEGIN, step_cbor(CURRENT_BANK_ADDRESS, 70000),
                     step_cbor_dump(CURRENT_BANK_ADDRESS, 2), STEP_DUMP_END],
                    ["current_position"], 1,
                    current_bank=2),
        _steps_case("rule 6: the dump guard is per address",
                    [STEP_DUMP_BEGIN, bank(3), step_cbor_dump(CURRENT_RIG_SLOT_ADDRESS, 1), STEP_DUMP_END],
                    ["current_position", "current_position"], 2,
                    current_bank=3, current_rig_slot=1, current_rig_index=16),
        _steps_case("rule 6: end_dump clears the touched set",
                    [STEP_DUMP_BEGIN, bank(3), STEP_DUMP_END,
                     STEP_DUMP_BEGIN, step_cbor_dump(CURRENT_BANK_ADDRESS, 2), STEP_DUMP_END],
                    ["current_position", "current_position"], 2,
                    current_bank=2),
        _steps_case("rule 6: a dump item outside a dump folds like a live one",
                    [step_cbor_dump(rig_volume_addr, 100)],
                    ["param_changed"], 1,
                    rig_volume=100),
        # --- Rule 1: a $02 block is one unit at the meter base and a run of
        #     singles anywhere else.
        _steps_case("rule 1: a stream block at the meter base is one status frame",
                    [meter_block(raw)],
                    ["status"], 0,
                    status_raw=raw),
        _steps_case("rule 1: a stream block off a Multi base folds element by element",
                    [step_midi3(sysex(0x00, 0x00, FN["multi_param"], WK["page_rig_settings"],
                                      WK["tempo_number"],
                                      [*u14_split(84 * bpm_scale), *u14_split(9000)]))],
                    ["tempo_bpm", "param_changed"], 1,
                    tempo_bpm=84, rig_volume=9000),
        # --- Outside the table: the two decodes `apply` keeps for itself.
        _steps_case("outside the table: a rendered-string reply is a fast event with no state",
                    [step_midi3(sysex(0x02, DEVICE_OMNI, FN["rendered_string_reply"], 0x3C, 53,
                                      [*u14_split(8192), *b"<0.0>", 0]))],
                    ["rendered_string"], 0),
        _steps_case("outside the table: a non-Kemper message is ignored",
                    [step_midi3(control_change(0, 50, 1))],
                    [], 0),
    ]


def _cbor_snapshot_cases():
    def case(name, stream, bank, slot, morph, strings):
        return {"name": name, "stream_hex": hx(stream),
                "expect": {"current_bank": bank, "current_rig_slot": slot, "morph": morph,
                           "strings": [{"addr": a, "text": t} for a, t in strings]}}
    secret = CBOR["sensitive_addresses"][0]
    return [
        # A multi-run is consecutive addresses from its base, so the position
        # can sit inside one; the filler bytes before it must be skipped.
        case("position inside one consecutive multi-run, after inter-item filler",
             cbor_filler(2) + cbor_multi(CURRENT_BANK_ADDRESS - 1, 0, 1, 2),
             1, 2, None, []),
        # Singles land too, strings are kept, and a sensitive address is
        # replaced by the placeholder rather than surfaced.
        case("position from single items, plus a rig name and a redacted secret",
             cbor_param_write(CURRENT_BANK_ADDRESS, 3) + cbor_param_write(CURRENT_RIG_SLOT_ADDRESS, 4)
             + cbor_string(1, "Maz 18 Pushed") + cbor_string(secret, "secret"),
             3, 4, None, [(1, "Maz 18 Pushed"), (secret, CBOR["redacted_placeholder"])]),
        case("an empty stream yields no position", b"", None, None, None, []),
        # An empty string is a value like any other: it is how a cleared tag
        # reaches a reader, which could otherwise never unlearn the old text.
        case("an empty string clears a tag rather than vanishing",
             cbor_string(1, "Maz 18") + cbor_string(1, ""),
             None, None, None, [(1, "Maz 18"), (1, "")]),
        case("the dump carries the morph position alongside the indices",
             cbor_param_write(CURRENT_BANK_ADDRESS, 1) + cbor_param_write(CURRENT_RIG_SLOT_ADDRESS, 2)
             + cbor_param_write(MORPH_ADDRESS, 16383),
             1, 2, 16383, []),
    ]


# --------------------------------------------------------------------------
# Navigation: the rig-load serializer as a pure state machine. Overlapping rig
# loads wedge the device on a delayed fuse, so the model funnels every load
# through this machine; the vectors pin its every transition so the three
# ports cannot drift by one action.
#
# The rules, from the contract:
#   navigate(t):      aim = t; if not in_flight: pump()
#   settle_elapsed(): in_flight = false; if aim is the sent index (the settled
#                     move's own target, still unconfirmed): start_window,
#                     awaiting = true; then pump()
#   window_elapsed(): if awaiting and aim is some a: aim = sent = None,
#                     awaiting = false -> dropped(a)
#   position(i):      if aim == i: aim = sent = None, awaiting = false
#                     -> settled(i); otherwise nothing
#   pump():           if not in_flight and aim is some a and sent != a:
#                     sent = a, in_flight = true, awaiting = false
#                     -> send(a), start_settle
#
# Two readings the contract leaves open are fixed here, and the cases below
# pin them. First, settle starts the window only when the aim *is* the index
# that was sent: an aim that moved on during the flight has no settled move to
# wait on, it is simply pumped, so a burst never arms a window it immediately
# abandons. Second, navigate clears `awaiting` only through the pump: a
# re-navigate to the very index already sent, while its window is running,
# leaves the window running — otherwise a re-tapped aim the device can never
# confirm would stick forever, since the machine has no action to cancel a
# timer, only to ignore a stale one.
#
# Every expectation below was derived by hand from those rules; the reference
# replay in `_nav_replay` is a cross-check that fails loudly on a slip, never
# the source of a value.

_NAV_STEP_KINDS = frozenset({"navigate", "settle", "window", "position"})


def nav(index: int) -> dict:
    return {"navigate": index}


def pos(index: int) -> dict:
    return {"position": index}


NAV_SETTLE = {"settle": True}
NAV_WINDOW = {"window": True}


def _nav_replay(steps: list[dict]) -> tuple[list[int], list[str], int | None, bool, bool]:
    """Replay the rules above over a fresh machine; return what a case pins."""
    aim = sent = None
    in_flight = awaiting = False
    sends: list[int] = []
    actions: list[str] = []

    def pump():
        nonlocal sent, in_flight, awaiting
        if not in_flight and aim is not None and sent != aim:
            sent, in_flight, awaiting = aim, True, False
            sends.append(aim)
            actions.extend([f"send:{aim}", "start_settle"])

    for step in steps:
        kind, value = next(iter(step.items()))
        if kind == "navigate":
            aim = value
            if not in_flight:
                pump()
        elif kind == "settle":
            in_flight = False
            if aim is not None and sent == aim:
                awaiting = True
                actions.append("start_window")
            pump()
        elif kind == "window":
            if awaiting and aim is not None:
                actions.append(f"dropped:{aim}")
                aim = sent = None
                awaiting = False
        elif kind == "position":
            if aim == value:
                actions.append(f"settled:{value}")
                aim = sent = None
                awaiting = False
    return sends, actions, aim, in_flight, awaiting


def _nav_case(name: str, steps: list[dict], sent: list[int], actions: list[str],
              aim: int | None, in_flight: bool, awaiting: bool) -> dict:
    """One navigation case: the hand-derived expectation, cross-checked by replay."""
    for s in steps:
        assert len(s) == 1 and next(iter(s)) in _NAV_STEP_KINDS, s
    for a in actions:
        head, _, arg = a.partition(":")
        assert head in ("send", "start_settle", "start_window", "settled", "dropped"), a
        assert (arg == "") == (head in ("start_settle", "start_window")), a
    assert sent == [int(a[5:]) for a in actions if a.startswith("send:")], name
    expected = (sent, actions, aim, in_flight, awaiting)
    replayed = _nav_replay(steps)
    assert replayed == expected, f"{name}: hand derivation {expected} != replay {replayed}"
    return {"name": name, "steps": steps,
            "expect": {"sent": sent, "actions": actions, "aim": aim,
                       "in_flight": in_flight, "awaiting": awaiting}}


def _navigation_cases():
    return [
        # --- A burst of taps costs two loads however long it is: the first
        #     tap sends, the aim moves freely while that move is in flight,
        #     and the settle pumps the final aim once.
        _nav_case("a tap burst sends the first tap and moves the aim",
                  [nav(14), nav(15), nav(16)],
                  [14], ["send:14", "start_settle"], 16, True, False),
        _nav_case("a tap burst costs two loads: the settle sends the final aim",
                  [nav(14), nav(15), nav(16), NAV_SETTLE],
                  [14, 16], ["send:14", "start_settle", "send:16", "start_settle"],
                  16, True, False),
        _nav_case("the burst lands: the position retires the final aim and the last settle sends nothing",
                  [nav(14), nav(16), NAV_SETTLE, pos(16), NAV_SETTLE],
                  [14, 16], ["send:14", "start_settle", "send:16", "start_settle", "settled:16"],
                  None, False, False),
        _nav_case("navigate while in flight only moves the aim",
                  [nav(14), nav(15)],
                  [14], ["send:14", "start_settle"], 15, True, False),
        # --- An index already sent is never re-sent, whether the aim comes
        #     back to it in flight or is tapped again during its window.
        _nav_case("re-navigating to the sent index while in flight sends nothing more on settle",
                  [nav(14), nav(14), NAV_SETTLE],
                  [14], ["send:14", "start_settle", "start_window"], 14, False, True),
        _nav_case("an aim that returns to the sent index in flight is not re-sent",
                  [nav(14), nav(15), nav(14), NAV_SETTLE],
                  [14], ["send:14", "start_settle", "start_window"], 14, False, True),
        _nav_case("re-navigating to the sent index during the window does not re-send and leaves the window running",
                  [nav(14), NAV_SETTLE, nav(14)],
                  [14], ["send:14", "start_settle", "start_window"], 14, False, True),
        _nav_case("a re-tapped aim the device never confirms is still dropped",
                  [nav(14), NAV_SETTLE, nav(14), NAV_WINDOW],
                  [14], ["send:14", "start_settle", "start_window", "dropped:14"],
                  None, False, False),
        # --- Settle with a still-unconfirmed aim starts the window; the aim
        #     past the end of the device's rigs is the case that needs it,
        #     because the device stays put and reports where it is.
        _nav_case("settle with a still-unconfirmed aim starts the window",
                  [nav(14), NAV_SETTLE],
                  [14], ["send:14", "start_settle", "start_window"], 14, False, True),
        _nav_case("aim past the end: a position that is not the aim keeps it during the window",
                  [nav(999), NAV_SETTLE, pos(13)],
                  [999], ["send:999", "start_settle", "start_window"], 999, False, True),
        _nav_case("aim past the end expires after the window",
                  [nav(999), NAV_SETTLE, pos(13), NAV_WINDOW],
                  [999], ["send:999", "start_settle", "start_window", "dropped:999"],
                  None, False, False),
        _nav_case("a drop forgets the sent index, so the same index is sent again",
                  [nav(999), NAV_SETTLE, NAV_WINDOW, nav(999)],
                  [999, 999],
                  ["send:999", "start_settle", "start_window", "dropped:999", "send:999", "start_settle"],
                  999, True, False),
        _nav_case("a new aim after a drop is sent at once",
                  [nav(999), NAV_SETTLE, NAV_WINDOW, nav(14)],
                  [999, 14],
                  ["send:999", "start_settle", "start_window", "dropped:999", "send:14", "start_settle"],
                  14, True, False),
        # --- A matching position retires the aim; a mismatch is ignored.
        _nav_case("a matching position retires the aim while the move is still in flight",
                  [nav(14), pos(14)],
                  [14], ["send:14", "start_settle", "settled:14"], None, True, False),
        _nav_case("a matching position retires the aim and a later settle sends nothing",
                  [nav(14), pos(14), NAV_SETTLE],
                  [14], ["send:14", "start_settle", "settled:14"], None, False, False),
        _nav_case("a matching position during the window retires the aim",
                  [nav(14), NAV_SETTLE, pos(14)],
                  [14], ["send:14", "start_settle", "start_window", "settled:14"],
                  None, False, False),
        _nav_case("the window after a confirmed aim drops nothing",
                  [nav(14), NAV_SETTLE, pos(14), NAV_WINDOW],
                  [14], ["send:14", "start_settle", "start_window", "settled:14"],
                  None, False, False),
        _nav_case("a mismatched position keeps the aim",
                  [nav(14), pos(13)],
                  [14], ["send:14", "start_settle"], 14, True, False),
        _nav_case("the sent move landing does not confirm an aim that has moved on",
                  [nav(14), nav(15), pos(14)],
                  [14], ["send:14", "start_settle"], 15, True, False),
        _nav_case("the sent move landing does not confirm an aim that has moved on, and the settle sends it",
                  [nav(14), nav(15), pos(14), NAV_SETTLE],
                  [14, 15], ["send:14", "start_settle", "send:15", "start_settle"],
                  15, True, False),
        _nav_case("an early confirmation does not shorten the settle: the next aim waits for it",
                  [nav(14), pos(14), nav(15)],
                  [14], ["send:14", "start_settle", "settled:14"], 15, True, False),
        _nav_case("an early confirmation does not shorten the settle: the settle then sends the next aim",
                  [nav(14), pos(14), nav(15), NAV_SETTLE],
                  [14, 15], ["send:14", "start_settle", "settled:14", "send:15", "start_settle"],
                  15, True, False),
        _nav_case("index 0 is a real aim",
                  [nav(0), pos(0)],
                  [0], ["send:0", "start_settle", "settled:0"], None, True, False),
        # --- A navigate while awaiting cancels the window and pumps at once:
        #     nothing is in flight, so the new index goes straight out.
        _nav_case("navigate while awaiting cancels the window and pumps at once",
                  [nav(14), NAV_SETTLE, nav(15)],
                  [14, 15], ["send:14", "start_settle", "start_window", "send:15", "start_settle"],
                  15, True, False),
        _nav_case("the cancelled window's timer firing late drops nothing",
                  [nav(14), NAV_SETTLE, nav(15), NAV_WINDOW],
                  [14, 15], ["send:14", "start_settle", "start_window", "send:15", "start_settle"],
                  15, True, False),
        _nav_case("a second navigate during the window opens its own window on settle, and the position closes it",
                  [nav(14), NAV_SETTLE, nav(15), NAV_SETTLE, pos(15)],
                  [14, 15],
                  ["send:14", "start_settle", "start_window", "send:15", "start_settle", "start_window", "settled:15"],
                  None, False, False),
        # --- Nothing pending: a position report with no aim, or a stale
        #     timer, changes nothing.
        _nav_case("a position with no aim is ignored",
                  [pos(3)],
                  [], [], None, False, False),
        _nav_case("stale timers with nothing pending do nothing",
                  [NAV_SETTLE, NAV_WINDOW],
                  [], [], None, False, False),
    ]


if __name__ == "__main__":
    build()
    print("all vectors written and cross-checked")
