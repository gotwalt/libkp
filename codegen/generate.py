#!/usr/bin/env python3
"""Generate per-language data modules from the libkp protocol spec.

The files under ``spec/`` are the single source of truth. This script serializes
them into native, data-only modules for each implementation:

    rust/src/generated.rs
    python/src/libkp/_generated.py
    swift/Sources/LibKP/Generated.swift

Those modules contain only constants and lookup tables — no protocol logic. Each
implementation hand-writes the logic (framing, SysEx builders, decode, model)
and is kept honest by the shared conformance vectors in ``spec/vectors/``.

Usage:
    generate.py            # write the generated modules
    generate.py --check    # exit non-zero if any committed module is stale

Requires Python 3.11+ (uses the stdlib ``tomllib``).
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "spec"

BANNER = "GENERATED FILE — DO NOT EDIT. Edit spec/*.toml and run codegen/generate.py."


def load() -> dict:
    data = {}
    for name in ("version", "protocol", "parameters", "effect-types", "controls", "meters"):
        with open(SPEC / f"{name}.toml", "rb") as fh:
            data[name] = tomllib.load(fh)
    return data


def non_effect_params(params: dict) -> list[tuple[int, int, str]]:
    """Flatten non_effect_params plus expanded param_ranges, sorted by (page, number)."""
    rows: list[tuple[int, int, str]] = [
        (e["page"], e["number"], e["name"]) for e in params["non_effect_params"]
    ]
    for r in params.get("param_ranges", []):
        for n in range(r["start"], r["end"] + 1):
            rows.append((r["page"], n, r["name"]))
    rows.sort(key=lambda t: (t[0], t[1]))
    return rows


def cc_const_name(name: str) -> str:
    return "CC_" + name.upper()


# --------------------------------------------------------------------------
# Literal helpers
# --------------------------------------------------------------------------

def esc(s: str) -> str:
    # Escape backslash, double-quote, and control chars. The \r \n \t escapes are
    # spelled identically in Rust, Python, and Swift string literals.
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def q(s: str) -> str:
    return '"' + esc(s) + '"'


# --------------------------------------------------------------------------
# Rust
# --------------------------------------------------------------------------

def emit_rust(d: dict) -> str:
    p = d["protocol"]
    pa = d["parameters"]
    wk = pa["well_known"]
    fns = p["sysex"]["functions"]
    epn = pa["effect_param_numbers"]
    mb = d["meters"]["block"]
    strobe = d["meters"]["strobe"]

    out: list[str] = []
    w = out.append
    w(f"//! {BANNER}")
    w("#![allow(clippy::all)]")
    # Skip the whole generated file: `cargo fmt` must not reformat it, or it
    # would diverge from this generator's output and trip the drift check.
    w("#![cfg_attr(rustfmt, rustfmt::skip)]")
    w("")
    w(f'pub const SPEC_VERSION: &str = {q(d["version"]["spec_version"])};')
    w("")
    w("// Transport")
    w(f'pub const PORT: u16 = {p["transport"]["port"]};')
    w(f'pub const CONNECT_TIMEOUT_SECS: u64 = {p["transport"]["connect_timeout_secs"]};')
    w(f'pub const SOCKET_TIMEOUT_SECS: u64 = {p["transport"]["socket_timeout_secs"]};')
    w(f'pub const CONNECTION_COOLDOWN_MS: u64 = {p["transport"]["connection_cooldown_ms"]};')
    w("")
    w("// Discovery")
    w(f'pub const DISCOVERY_HEADER: &str = {q(p["discovery"]["header"])};')
    w(f'pub const POLL_INTERVAL_MS: u64 = {p["discovery"]["poll_interval_ms"]};')
    w(f'pub const POLL_MAC_PREFIX: &str = {q(p["discovery"]["poll_mac_prefix"])};')
    w(f'pub const POLL_PAYLOAD: &str = {q(p["discovery"]["poll_payload"])};')
    w(f'pub const POLL_PLACEHOLDER_MAC: &str = {q(p["discovery"]["poll_placeholder_mac"])};')
    w("")
    w("// Handshake")
    w(f'pub const HANDSHAKE_TERMINATOR: &str = {q(p["handshake"]["terminator"])};')
    w(f'pub const HANDSHAKE_LIST_END: &str = {q(p["handshake"]["list_end_marker"])};')
    w(f'pub const HANDSHAKE_ACCEPT_PREFIX: &str = {q(p["handshake"]["accept_prefix"])};')
    w(f'pub const HANDSHAKE_REJECT_PREFIX: &str = {q(p["handshake"]["reject_prefix"])};')
    w(f'pub const SESSION_PREAMBLE_LEN: usize = {p["handshake"]["session_preamble_len"]};')
    w("")
    w("// Protocol GUIDs")
    w(f'pub const PROTOCOL_MIDI3_STREAM: &str = {q(p["protocols"]["midi3_stream"])};')
    w(f'pub const PROTOCOL_REQUEST_RESPONSE: &str = {q(p["protocols"]["request_response"])};')
    w(f'pub const PROTOCOL_CBOR_CONTROL: &str = {q(p["protocols"]["cbor_control"])};')
    w(f'pub const PROTOCOL_RESERVED: &str = {q(p["protocols"]["reserved"])};')
    w("")
    w("// CBOR channel")
    cb = p["cbor"]
    w(f'pub const CBOR_ITEM_TAG: u64 = {cb["item_tag"]};')
    w(f'pub const CBOR_SELECTOR_SINGLE: i64 = {cb["selector_single"]};')
    w(f'pub const CBOR_SELECTOR_MULTI: i64 = {cb["selector_multi"]};')
    w(f'pub const CBOR_SELECTOR_STRING: i64 = {cb["selector_string"]};')
    w(f'pub const CBOR_FILLER_BYTE: u8 = {cb["filler_byte"]:#04x};')
    w(f'pub const STATE_DUMP_TRIGGER_ADDRESS: u32 = {cb["state_dump_trigger_address"]};')
    w(f'pub const STATE_DUMP_TRIGGER_VALUE: i64 = {cb["state_dump_trigger_value"]};')
    sens = ", ".join(str(a) for a in cb["sensitive_addresses"])
    w(f'pub const SENSITIVE_ADDRESSES: [u32; {len(cb["sensitive_addresses"])}] = [{sens}];')
    w(f'pub const REDACTED_PLACEHOLDER: &str = {q(cb["redacted_placeholder"])};')
    w("")
    w("// MIDI3 framing tags")
    w(f'pub const MIDI3_TAG_CONTINUATION: u8 = {p["midi3"]["tag_continuation"]:#04x};')
    w(f'pub const MIDI3_TAG_FINAL_1: u8 = {p["midi3"]["tag_final_1"]:#04x};')
    w(f'pub const MIDI3_TAG_FINAL_2: u8 = {p["midi3"]["tag_final_2"]:#04x};')
    w(f'pub const MIDI3_TAG_FINAL_3: u8 = {p["midi3"]["tag_final_3"]:#04x};')
    w("")
    w("// SysEx envelope")
    mid = ", ".join(f"{b:#04x}" for b in p["sysex"]["manufacturer_id"])
    w(f"pub const MANUFACTURER_ID: [u8; 3] = [{mid}];")
    w(f'pub const PRODUCT_PROFILER: u8 = {p["sysex"]["product_profiler"]:#04x};')
    w(f'pub const PRODUCT_PLAYER: u8 = {p["sysex"]["product_player"]:#04x};')
    w(f'pub const DEVICE_OMNI: u8 = {p["sysex"]["device_omni"]:#04x};')
    w(f'pub const FULL_SCALE: u16 = {p["sysex"]["full_scale"]};')
    w("")
    w("// SysEx function codes")
    rust_fn = {
        "single_param": "FN_SINGLE_PARAM", "multi_param": "FN_MULTI_PARAM",
        "string_param": "FN_STRING_PARAM", "blob": "FN_BLOB", "ext_param": "FN_EXT_PARAM",
        "ext_string_param": "FN_EXT_STRING_PARAM", "morphed_multi_param": "FN_MORPHED_MULTI_PARAM",
        "rendered_string_reply": "FN_RENDERED_STRING_REPLY", "request_single": "FN_REQUEST_SINGLE",
        "request_multi": "FN_REQUEST_MULTI", "request_string": "FN_REQUEST_STRING",
        "request_ext_string": "FN_REQUEST_EXT_STRING",
        "request_rendered_string": "FN_REQUEST_RENDERED_STRING", "beacon": "FN_BEACON",
    }
    for k, const in rust_fn.items():
        w(f"pub const {const}: u8 = {fns[k]:#04x};")
    w("")
    w("// Beacon")
    b = p["beacon"]
    w(f'pub const BEACON_FUNCTION: u8 = {b["function"]:#04x};')
    w(f'pub const BEACON_SUBCOMMAND: u8 = {b["subcommand"]:#04x};')
    w(f'pub const BEACON_DEFAULT_PARAM_SET: u8 = {b["default_param_set"]:#04x};')
    w(f'pub const BEACON_FLAG_INIT: u8 = {b["flag_init"]:#04x};')
    w(f'pub const BEACON_FLAG_SYSEX: u8 = {b["flag_sysex"]:#04x};')
    w(f'pub const BEACON_FLAG_TUNEMODE: u8 = {b["flag_tunemode"]:#04x};')
    w("")
    w("// Effect-slot parameter numbers")
    w(f'pub const EFFECT_PARAM_TYPE: u8 = {epn["type"]};')
    w(f'pub const EFFECT_PARAM_STATE: u8 = {epn["state"]};')
    w(f'pub const EFFECT_PARAM_MIX: u8 = {epn["mix"]};')
    w(f'pub const EFFECT_PARAM_VOLUME: u8 = {epn["volume"]};')
    w("")
    w("// Well-known addresses (keyed on by the state / model layer)")
    wk_rust = {
        "page_strings": ("PAGE_STRINGS", "u8"), "string_rig_name": ("STRING_RIG_NAME", "u8"),
        "page_realtime": ("PAGE_REALTIME", "u8"), "meter_block_number": ("METER_BLOCK_NUMBER", "u8"),
        "beat_pulse_number": ("BEAT_PULSE_NUMBER", "u8"),
        "tuner_deviance_number": ("TUNER_DEVIANCE_NUMBER", "u8"),
        "page_rig_settings": ("PAGE_RIG_SETTINGS", "u8"), "tempo_number": ("TEMPO_NUMBER", "u8"),
        "tempo_bpm_scale": ("TEMPO_BPM_SCALE", "u16"), "rig_volume_number": ("RIG_VOLUME_NUMBER", "u8"),
        "amp_page": ("AMP_PAGE", "u8"), "amp_on_number": ("AMP_ON_NUMBER", "u8"),
        "gain_number": ("GAIN_NUMBER", "u8"), "system_page": ("SYSTEM_PAGE", "u8"),
        "main_volume_number": ("MAIN_VOLUME_NUMBER", "u8"),
        "headphone_volume_number": ("HEADPHONE_VOLUME_NUMBER", "u8"),
        "monitor_volume_number": ("MONITOR_VOLUME_NUMBER", "u8"),
        "page_bank_preview": ("PAGE_BANK_PREVIEW", "u8"), "bank_slots": ("BANK_SLOTS", "usize"),
        "bank_rig_name_base": ("BANK_RIG_NAME_BASE", "u8"),
        "bank_amp_name_base": ("BANK_AMP_NAME_BASE", "u8"),
        "bank_cabinet_name_base": ("BANK_CABINET_NAME_BASE", "u8"), "page_morph": ("PAGE_MORPH", "u8"),
        "morph_number": ("MORPH_NUMBER", "u8"), "page_tuner_note": ("PAGE_TUNER_NOTE", "u8"),
        "tuner_note_number": ("TUNER_NOTE_NUMBER", "u8"),
        "tuner_in_tune_center": ("TUNER_IN_TUNE_CENTER", "u16"),
        "tuner_in_tune_window": ("TUNER_IN_TUNE_WINDOW", "u16"), "meter_count": ("METER_COUNT", "usize"),
        "current_bank_address": ("CURRENT_BANK_ADDRESS", "u32"),
        "current_rig_slot_address": ("CURRENT_RIG_SLOT_ADDRESS", "u32"),
    }
    for k, (const, ty) in wk_rust.items():
        val = wk[k]
        lit = f"{val:#04x}" if ty == "u8" and val < 256 else str(val)
        w(f"pub const {const}: {ty} = {lit};")
    w("")
    w("// Meter block")
    w(f'pub const METER_PAGE: u8 = {mb["page"]:#04x};')
    w(f'pub const METER_FIRST_NUMBER: u8 = {mb["first_number"]:#04x};')
    w(f'pub const METER_UPDATE_RATE_HZ: u32 = {mb["update_rate_hz"]};')
    w(f'pub const STROBE_PHASE_INDEX: usize = {strobe["phase_index"]};')
    seg = ", ".join(str(i) for i in strobe["segment_indices"])
    w(f"pub const STROBE_SEGMENT_INDICES: [usize; 3] = [{seg}];")
    w("")

    # Tables
    w("// Tables")
    rows = ",\n".join(f"    ({c:#04x}, {q(n)})" for c, n in
                      sorted((f["code"], f["name"]) for f in pa["functions"]))
    w(f"pub static FUNCTION_NAMES: &[(u8, &str)] = &[\n{rows},\n];")
    w("")
    rows = ",\n".join(f"    ({pg:#04x}, {q(nm)})" for pg, nm in
                      sorted((e["page"], e["name"]) for e in pa["pages"]))
    w(f"pub static PAGE_NAMES: &[(u8, &str)] = &[\n{rows},\n];")
    w("")
    rows = ",\n".join(f"    ({q(s['slot'])}, {s['page']:#04x})" for s in pa["effect_slots"])
    w(f"pub static EFFECT_SLOTS: &[(&str, u8)] = &[\n{rows},\n];")
    w("")
    rows = ",\n".join(f"    ({e['number']}, {q(e['name'])})" for e in
                      sorted(pa["effect_params"], key=lambda e: e["number"]))
    w(f"pub static EFFECT_PARAMS: &[(u8, &str)] = &[\n{rows},\n];")
    w("")
    rows = ",\n".join(f"    ({pg:#04x}, {n}, {q(nm)})" for pg, n, nm in non_effect_params(pa))
    w(f"pub static NON_EFFECT_PARAMS: &[(u8, u8, &str)] = &[\n{rows},\n];")
    w("")
    rows = ",\n".join(f"    ({e['number']}, {q(e['name'])})" for e in
                      sorted(pa["string_tags"], key=lambda e: e["number"]))
    w(f"pub static STRING_TAGS: &[(u8, &str)] = &[\n{rows},\n];")
    w("")
    rows = ",\n".join(f"    ({e['number']:#04x}, {q(e['name'])})" for e in pa["page0_numeric"])
    w(f"pub static PAGE0_NUMERIC: &[(u8, &str)] = &[\n{rows},\n];")
    w("")
    rows = ",\n".join(f"    ({e['value']}, {q(e['name'])})" for e in
                      sorted(d["effect-types"]["effect_types"], key=lambda e: e["value"]))
    w(f"pub static EFFECT_TYPES: &[(u16, &str)] = &[\n{rows},\n];")
    w("")
    w("/// One category block of the effect Type value space: an inclusive range.")
    w("#[derive(Debug, Clone, Copy, PartialEq, Eq)]")
    w("pub struct EffectCategory {")
    w("    pub min: u16,")
    w("    pub max: u16,")
    w("    pub name: &'static str,")
    w("}")
    rows = ",\n".join(
        f"    EffectCategory {{ min: {e['min']}, max: {e['max']}, name: {q(e['name'])} }}"
        for e in d["effect-types"]["effect_categories"])
    w(f"pub static EFFECT_CATEGORIES: &[EffectCategory] = &[\n{rows},\n];")
    w("")
    # CC constants + slot enable
    for c in d["controls"]["cc"]:
        w(f"pub const {cc_const_name(c['name'])}: u8 = {c['cc']};")
    w("")
    rows = ",\n".join(f"    ({q(e['slot'])}, {e['cc']})" for e in d["controls"]["slot_enable_cc"])
    w(f"pub static SLOT_ENABLE_CC: &[(&str, u8)] = &[\n{rows},\n];")
    w("")
    w(f'pub const PROGRAM_CHANGE_STATUS: u8 = {d["controls"]["status"]["program_change"]:#04x};')
    w(f'pub const CONTROL_CHANGE_STATUS: u8 = {d["controls"]["status"]["control_change"]:#04x};')
    w("")
    # Meter fields
    w("/// One realtime status field: (index, number, id, name, render).")
    rows = ",\n".join(
        f"    ({f['index']}, {f['number']}, {q(f['id'])}, {q(f['name'])}, {q(f['render'])})"
        for f in mb["fields"])
    w(f"pub static METER_FIELDS: &[(usize, u8, &str, &str, &str)] = &[\n{rows},\n];")
    w("")
    # Guard the multi-line table literals from `cargo fmt`: rustfmt recurses
    # through `mod` declarations and would otherwise reformat this generated file,
    # making it diverge from the generator output and tripping the drift check.
    guarded: list[str] = []
    for line in out:
        if line.startswith("pub static "):
            guarded.append("#[rustfmt::skip]")
        guarded.append(line)
    return "\n".join(guarded) + "\n"


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------

def emit_python(d: dict) -> str:
    p = d["protocol"]
    pa = d["parameters"]
    wk = pa["well_known"]
    fns = p["sysex"]["functions"]
    epn = pa["effect_param_numbers"]
    mb = d["meters"]["block"]
    strobe = d["meters"]["strobe"]

    out: list[str] = []
    w = out.append
    w(f'"""{BANNER}"""')
    w("")
    w(f'SPEC_VERSION = {q(d["version"]["spec_version"])}')
    w("")
    w(f'PORT = {p["transport"]["port"]}')
    w(f'CONNECT_TIMEOUT_SECS = {p["transport"]["connect_timeout_secs"]}')
    w(f'SOCKET_TIMEOUT_SECS = {p["transport"]["socket_timeout_secs"]}')
    w(f'CONNECTION_COOLDOWN_MS = {p["transport"]["connection_cooldown_ms"]}')
    w("")
    w(f'DISCOVERY_HEADER = {q(p["discovery"]["header"])}')
    w(f'POLL_INTERVAL_MS = {p["discovery"]["poll_interval_ms"]}')
    w(f'POLL_MAC_PREFIX = {q(p["discovery"]["poll_mac_prefix"])}')
    w(f'POLL_PAYLOAD = {q(p["discovery"]["poll_payload"])}')
    w(f'POLL_PLACEHOLDER_MAC = {q(p["discovery"]["poll_placeholder_mac"])}')
    w("")
    w(f'HANDSHAKE_TERMINATOR = {q(p["handshake"]["terminator"])}')
    w(f'HANDSHAKE_LIST_END = {q(p["handshake"]["list_end_marker"])}')
    w(f'HANDSHAKE_ACCEPT_PREFIX = {q(p["handshake"]["accept_prefix"])}')
    w(f'HANDSHAKE_REJECT_PREFIX = {q(p["handshake"]["reject_prefix"])}')
    w(f'SESSION_PREAMBLE_LEN = {p["handshake"]["session_preamble_len"]}')
    w("")
    w(f'PROTOCOL_MIDI3_STREAM = {q(p["protocols"]["midi3_stream"])}')
    w(f'PROTOCOL_REQUEST_RESPONSE = {q(p["protocols"]["request_response"])}')
    w(f'PROTOCOL_CBOR_CONTROL = {q(p["protocols"]["cbor_control"])}')
    w(f'PROTOCOL_RESERVED = {q(p["protocols"]["reserved"])}')
    w("")
    cb = p["cbor"]
    w(f'CBOR_ITEM_TAG = {cb["item_tag"]}')
    w(f'CBOR_SELECTOR_SINGLE = {cb["selector_single"]}')
    w(f'CBOR_SELECTOR_MULTI = {cb["selector_multi"]}')
    w(f'CBOR_SELECTOR_STRING = {cb["selector_string"]}')
    w(f'CBOR_FILLER_BYTE = {cb["filler_byte"]:#04x}')
    w(f'STATE_DUMP_TRIGGER_ADDRESS = {cb["state_dump_trigger_address"]}')
    w(f'STATE_DUMP_TRIGGER_VALUE = {cb["state_dump_trigger_value"]}')
    sens = ", ".join(str(a) for a in cb["sensitive_addresses"])
    w(f'SENSITIVE_ADDRESSES = ({sens})')
    w(f'REDACTED_PLACEHOLDER = {q(cb["redacted_placeholder"])}')
    w("")
    w(f'MIDI3_TAG_CONTINUATION = {p["midi3"]["tag_continuation"]:#04x}')
    w(f'MIDI3_TAG_FINAL_1 = {p["midi3"]["tag_final_1"]:#04x}')
    w(f'MIDI3_TAG_FINAL_2 = {p["midi3"]["tag_final_2"]:#04x}')
    w(f'MIDI3_TAG_FINAL_3 = {p["midi3"]["tag_final_3"]:#04x}')
    w("")
    mid = ", ".join(f"{b:#04x}" for b in p["sysex"]["manufacturer_id"])
    w(f"MANUFACTURER_ID = ({mid})")
    w(f'PRODUCT_PROFILER = {p["sysex"]["product_profiler"]:#04x}')
    w(f'PRODUCT_PLAYER = {p["sysex"]["product_player"]:#04x}')
    w(f'DEVICE_OMNI = {p["sysex"]["device_omni"]:#04x}')
    w(f'FULL_SCALE = {p["sysex"]["full_scale"]}')
    w("")
    py_fn = {
        "single_param": "FN_SINGLE_PARAM", "multi_param": "FN_MULTI_PARAM",
        "string_param": "FN_STRING_PARAM", "blob": "FN_BLOB", "ext_param": "FN_EXT_PARAM",
        "ext_string_param": "FN_EXT_STRING_PARAM", "morphed_multi_param": "FN_MORPHED_MULTI_PARAM",
        "rendered_string_reply": "FN_RENDERED_STRING_REPLY", "request_single": "FN_REQUEST_SINGLE",
        "request_multi": "FN_REQUEST_MULTI", "request_string": "FN_REQUEST_STRING",
        "request_ext_string": "FN_REQUEST_EXT_STRING",
        "request_rendered_string": "FN_REQUEST_RENDERED_STRING", "beacon": "FN_BEACON",
    }
    for k, const in py_fn.items():
        w(f"{const} = {fns[k]:#04x}")
    w("")
    b = p["beacon"]
    w(f'BEACON_FUNCTION = {b["function"]:#04x}')
    w(f'BEACON_SUBCOMMAND = {b["subcommand"]:#04x}')
    w(f'BEACON_DEFAULT_PARAM_SET = {b["default_param_set"]:#04x}')
    w(f'BEACON_FLAG_INIT = {b["flag_init"]:#04x}')
    w(f'BEACON_FLAG_SYSEX = {b["flag_sysex"]:#04x}')
    w(f'BEACON_FLAG_TUNEMODE = {b["flag_tunemode"]:#04x}')
    w("")
    w(f'EFFECT_PARAM_TYPE = {epn["type"]}')
    w(f'EFFECT_PARAM_STATE = {epn["state"]}')
    w(f'EFFECT_PARAM_MIX = {epn["mix"]}')
    w(f'EFFECT_PARAM_VOLUME = {epn["volume"]}')
    w("")
    wk_names = {
        "page_strings": "PAGE_STRINGS", "string_rig_name": "STRING_RIG_NAME",
        "page_realtime": "PAGE_REALTIME", "meter_block_number": "METER_BLOCK_NUMBER",
        "beat_pulse_number": "BEAT_PULSE_NUMBER", "tuner_deviance_number": "TUNER_DEVIANCE_NUMBER",
        "page_rig_settings": "PAGE_RIG_SETTINGS", "tempo_number": "TEMPO_NUMBER",
        "tempo_bpm_scale": "TEMPO_BPM_SCALE", "rig_volume_number": "RIG_VOLUME_NUMBER",
        "amp_page": "AMP_PAGE", "amp_on_number": "AMP_ON_NUMBER", "gain_number": "GAIN_NUMBER",
        "system_page": "SYSTEM_PAGE", "main_volume_number": "MAIN_VOLUME_NUMBER",
        "headphone_volume_number": "HEADPHONE_VOLUME_NUMBER",
        "monitor_volume_number": "MONITOR_VOLUME_NUMBER",
        "page_bank_preview": "PAGE_BANK_PREVIEW", "bank_slots": "BANK_SLOTS",
        "bank_rig_name_base": "BANK_RIG_NAME_BASE", "bank_amp_name_base": "BANK_AMP_NAME_BASE",
        "bank_cabinet_name_base": "BANK_CABINET_NAME_BASE", "page_morph": "PAGE_MORPH",
        "morph_number": "MORPH_NUMBER", "page_tuner_note": "PAGE_TUNER_NOTE",
        "tuner_note_number": "TUNER_NOTE_NUMBER", "tuner_in_tune_center": "TUNER_IN_TUNE_CENTER",
        "tuner_in_tune_window": "TUNER_IN_TUNE_WINDOW", "meter_count": "METER_COUNT",
        "current_bank_address": "CURRENT_BANK_ADDRESS",
        "current_rig_slot_address": "CURRENT_RIG_SLOT_ADDRESS",
    }
    for k, const in wk_names.items():
        val = wk[k]
        lit = f"{val:#04x}" if val < 256 and const not in ("TEMPO_BPM_SCALE", "METER_COUNT", "BANK_SLOTS") else str(val)
        w(f"{const} = {lit}")
    w("")
    w(f'METER_PAGE = {mb["page"]:#04x}')
    w(f'METER_FIRST_NUMBER = {mb["first_number"]:#04x}')
    w(f'METER_UPDATE_RATE_HZ = {mb["update_rate_hz"]}')
    w(f'STROBE_PHASE_INDEX = {strobe["phase_index"]}')
    w(f'STROBE_SEGMENT_INDICES = {tuple(strobe["segment_indices"])}')
    w("")
    # Tables as dicts / lists
    rows = ", ".join(f"{c:#04x}: {q(n)}" for c, n in
                     sorted((f["code"], f["name"]) for f in pa["functions"]))
    w(f"FUNCTION_NAMES = {{{rows}}}")
    w("")
    rows = ", ".join(f"{pg:#04x}: {q(nm)}" for pg, nm in
                     sorted((e["page"], e["name"]) for e in pa["pages"]))
    w(f"PAGE_NAMES = {{{rows}}}")
    w("")
    rows = ", ".join(f"({q(s['slot'])}, {s['page']:#04x})" for s in pa["effect_slots"])
    w(f"EFFECT_SLOTS = [{rows}]")
    w("")
    rows = ", ".join(f"{e['number']}: {q(e['name'])}" for e in
                     sorted(pa["effect_params"], key=lambda e: e["number"]))
    w(f"EFFECT_PARAMS = {{{rows}}}")
    w("")
    rows = ",\n    ".join(f"({pg:#04x}, {n}): {q(nm)}" for pg, n, nm in non_effect_params(pa))
    w(f"NON_EFFECT_PARAMS = {{\n    {rows},\n}}")
    w("")
    rows = ", ".join(f"{e['number']}: {q(e['name'])}" for e in
                     sorted(pa["string_tags"], key=lambda e: e["number"]))
    w(f"STRING_TAGS = {{{rows}}}")
    w("")
    rows = ", ".join(f"{e['number']:#04x}: {q(e['name'])}" for e in pa["page0_numeric"])
    w(f"PAGE0_NUMERIC = {{{rows}}}")
    w("")
    rows = ",\n    ".join(f"{e['value']}: {q(e['name'])}" for e in
                          sorted(d["effect-types"]["effect_types"], key=lambda e: e["value"]))
    w(f"EFFECT_TYPES = {{\n    {rows},\n}}")
    w("")
    rows = ",\n    ".join(f"({e['min']}, {e['max']}, {q(e['name'])})"
                          for e in d["effect-types"]["effect_categories"])
    w(f"# (min, max, name) — inclusive Type value blocks\nEFFECT_CATEGORIES = [\n    {rows},\n]")
    w("")
    for c in d["controls"]["cc"]:
        w(f"{cc_const_name(c['name'])} = {c['cc']}")
    w("")
    rows = ", ".join(f"{q(e['slot'])}: {e['cc']}" for e in d["controls"]["slot_enable_cc"])
    w(f"SLOT_ENABLE_CC = {{{rows}}}")
    w("")
    w(f'PROGRAM_CHANGE_STATUS = {d["controls"]["status"]["program_change"]:#04x}')
    w(f'CONTROL_CHANGE_STATUS = {d["controls"]["status"]["control_change"]:#04x}')
    w("")
    rows = ",\n    ".join(
        f"({f['index']}, {f['number']}, {q(f['id'])}, {q(f['name'])}, {q(f['render'])})"
        for f in mb["fields"])
    w(f"# (index, number, id, name, render)\nMETER_FIELDS = [\n    {rows},\n]")
    w("")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# Swift
# --------------------------------------------------------------------------

def emit_swift(d: dict) -> str:
    p = d["protocol"]
    pa = d["parameters"]
    wk = pa["well_known"]
    fns = p["sysex"]["functions"]
    epn = pa["effect_param_numbers"]
    mb = d["meters"]["block"]
    strobe = d["meters"]["strobe"]

    out: list[str] = []
    w = out.append
    w(f"// {BANNER}")
    # Keep swift-format off this file so it stays byte-identical to the generator
    # output (otherwise the drift check fails).
    w("// swift-format-ignore-file")
    w("import Foundation")
    w("")
    w("public enum Generated {")

    def c(name, ty, val):
        w(f"    public static let {name}: {ty} = {val}")

    c("specVersion", "String", q(d["version"]["spec_version"]))
    c("port", "UInt16", p["transport"]["port"])
    c("connectTimeoutSecs", "UInt64", p["transport"]["connect_timeout_secs"])
    c("socketTimeoutSecs", "UInt64", p["transport"]["socket_timeout_secs"])
    c("connectionCooldownMs", "UInt64", p["transport"]["connection_cooldown_ms"])
    c("discoveryHeader", "String", q(p["discovery"]["header"]))
    c("pollIntervalMs", "UInt64", p["discovery"]["poll_interval_ms"])
    c("pollMacPrefix", "String", q(p["discovery"]["poll_mac_prefix"]))
    c("pollPayload", "String", q(p["discovery"]["poll_payload"]))
    c("pollPlaceholderMac", "String", q(p["discovery"]["poll_placeholder_mac"]))
    c("handshakeTerminator", "String", q(p["handshake"]["terminator"]))
    c("handshakeListEnd", "String", q(p["handshake"]["list_end_marker"]))
    c("handshakeAcceptPrefix", "String", q(p["handshake"]["accept_prefix"]))
    c("handshakeRejectPrefix", "String", q(p["handshake"]["reject_prefix"]))
    c("sessionPreambleLen", "Int", p["handshake"]["session_preamble_len"])
    c("protocolMidi3Stream", "String", q(p["protocols"]["midi3_stream"]))
    c("protocolRequestResponse", "String", q(p["protocols"]["request_response"]))
    c("protocolCborControl", "String", q(p["protocols"]["cbor_control"]))
    c("protocolReserved", "String", q(p["protocols"]["reserved"]))
    cb = p["cbor"]
    c("cborItemTag", "UInt64", cb["item_tag"])
    c("cborSelectorSingle", "Int64", cb["selector_single"])
    c("cborSelectorMulti", "Int64", cb["selector_multi"])
    c("cborSelectorString", "Int64", cb["selector_string"])
    c("cborFillerByte", "UInt8", f'{cb["filler_byte"]:#04x}')
    c("stateDumpTriggerAddress", "UInt32", cb["state_dump_trigger_address"])
    c("stateDumpTriggerValue", "Int64", cb["state_dump_trigger_value"])
    sens = ", ".join(str(a) for a in cb["sensitive_addresses"])
    c("sensitiveAddresses", "[UInt32]", f"[{sens}]")
    c("redactedPlaceholder", "String", q(cb["redacted_placeholder"]))
    c("midi3TagContinuation", "UInt8", f'{p["midi3"]["tag_continuation"]:#04x}')
    c("midi3TagFinal1", "UInt8", f'{p["midi3"]["tag_final_1"]:#04x}')
    c("midi3TagFinal2", "UInt8", f'{p["midi3"]["tag_final_2"]:#04x}')
    c("midi3TagFinal3", "UInt8", f'{p["midi3"]["tag_final_3"]:#04x}')
    mid = ", ".join(f"{b:#04x}" for b in p["sysex"]["manufacturer_id"])
    c("manufacturerId", "[UInt8]", f"[{mid}]")
    c("productProfiler", "UInt8", f'{p["sysex"]["product_profiler"]:#04x}')
    c("productPlayer", "UInt8", f'{p["sysex"]["product_player"]:#04x}')
    c("deviceOmni", "UInt8", f'{p["sysex"]["device_omni"]:#04x}')
    c("fullScale", "UInt16", p["sysex"]["full_scale"])
    sw_fn = {
        "single_param": "fnSingleParam", "multi_param": "fnMultiParam",
        "string_param": "fnStringParam", "blob": "fnBlob", "ext_param": "fnExtParam",
        "ext_string_param": "fnExtStringParam", "morphed_multi_param": "fnMorphedMultiParam",
        "rendered_string_reply": "fnRenderedStringReply", "request_single": "fnRequestSingle",
        "request_multi": "fnRequestMulti", "request_string": "fnRequestString",
        "request_ext_string": "fnRequestExtString",
        "request_rendered_string": "fnRequestRenderedString", "beacon": "fnBeacon",
    }
    for k, const in sw_fn.items():
        c(const, "UInt8", f"{fns[k]:#04x}")
    b = p["beacon"]
    c("beaconFunction", "UInt8", f'{b["function"]:#04x}')
    c("beaconSubcommand", "UInt8", f'{b["subcommand"]:#04x}')
    c("beaconDefaultParamSet", "UInt8", f'{b["default_param_set"]:#04x}')
    c("beaconFlagInit", "UInt8", f'{b["flag_init"]:#04x}')
    c("beaconFlagSysex", "UInt8", f'{b["flag_sysex"]:#04x}')
    c("beaconFlagTunemode", "UInt8", f'{b["flag_tunemode"]:#04x}')
    c("effectParamType", "UInt8", epn["type"])
    c("effectParamState", "UInt8", epn["state"])
    c("effectParamMix", "UInt8", epn["mix"])
    c("effectParamVolume", "UInt8", epn["volume"])
    wk_sw = {
        "page_strings": ("pageStrings", "UInt8"), "string_rig_name": ("stringRigName", "UInt8"),
        "page_realtime": ("pageRealtime", "UInt8"), "meter_block_number": ("meterBlockNumber", "UInt8"),
        "beat_pulse_number": ("beatPulseNumber", "UInt8"),
        "tuner_deviance_number": ("tunerDevianceNumber", "UInt8"),
        "page_rig_settings": ("pageRigSettings", "UInt8"), "tempo_number": ("tempoNumber", "UInt8"),
        "tempo_bpm_scale": ("tempoBpmScale", "UInt16"), "rig_volume_number": ("rigVolumeNumber", "UInt8"),
        "amp_page": ("ampPage", "UInt8"), "amp_on_number": ("ampOnNumber", "UInt8"),
        "gain_number": ("gainNumber", "UInt8"), "system_page": ("systemPage", "UInt8"),
        "main_volume_number": ("mainVolumeNumber", "UInt8"),
        "headphone_volume_number": ("headphoneVolumeNumber", "UInt8"),
        "monitor_volume_number": ("monitorVolumeNumber", "UInt8"),
        "page_bank_preview": ("pageBankPreview", "UInt8"), "bank_slots": ("bankSlots", "Int"),
        "bank_rig_name_base": ("bankRigNameBase", "UInt8"),
        "bank_amp_name_base": ("bankAmpNameBase", "UInt8"),
        "bank_cabinet_name_base": ("bankCabinetNameBase", "UInt8"), "page_morph": ("pageMorph", "UInt8"),
        "morph_number": ("morphNumber", "UInt8"), "page_tuner_note": ("pageTunerNote", "UInt8"),
        "tuner_note_number": ("tunerNoteNumber", "UInt8"),
        "tuner_in_tune_center": ("tunerInTuneCenter", "UInt16"),
        "tuner_in_tune_window": ("tunerInTuneWindow", "UInt16"), "meter_count": ("meterCount", "Int"),
        "current_bank_address": ("currentBankAddress", "UInt32"),
        "current_rig_slot_address": ("currentRigSlotAddress", "UInt32"),
    }
    for k, (const, ty) in wk_sw.items():
        val = wk[k]
        lit = f"{val:#04x}" if ty == "UInt8" else str(val)
        c(const, ty, lit)
    c("meterPage", "UInt8", f'{mb["page"]:#04x}')
    c("meterFirstNumber", "UInt8", f'{mb["first_number"]:#04x}')
    c("meterUpdateRateHz", "Int", mb["update_rate_hz"])
    c("strobePhaseIndex", "Int", strobe["phase_index"])
    seg = ", ".join(str(i) for i in strobe["segment_indices"])
    c("strobeSegmentIndices", "[Int]", f"[{seg}]")
    for cc in d["controls"]["cc"]:
        # camelCase from snake_case
        parts = cc["name"].split("_")
        cname = "cc" + "".join(x.capitalize() for x in parts)
        c(cname, "UInt8", cc["cc"])
    c("programChangeStatus", "UInt8", f'{d["controls"]["status"]["program_change"]:#04x}')
    c("controlChangeStatus", "UInt8", f'{d["controls"]["status"]["control_change"]:#04x}')

    # Tables
    w("")
    rows = ", ".join(f"{cd:#04x}: {q(n)}" for cd, n in
                     sorted((f["code"], f["name"]) for f in pa["functions"]))
    w(f"    public static let functionNames: [UInt8: String] = [{rows}]")
    rows = ", ".join(f"{pg:#04x}: {q(nm)}" for pg, nm in
                     sorted((e["page"], e["name"]) for e in pa["pages"]))
    w(f"    public static let pageNames: [UInt8: String] = [{rows}]")
    rows = ", ".join(f"({q(s['slot'])}, {s['page']:#04x})" for s in pa["effect_slots"])
    w(f"    public static let effectSlots: [(String, UInt8)] = [{rows}]")
    rows = ", ".join(f"{e['number']}: {q(e['name'])}" for e in
                     sorted(pa["effect_params"], key=lambda e: e["number"]))
    w(f"    public static let effectParams: [UInt8: String] = [{rows}]")
    rows = ",\n        ".join(f"NonEffectKey({pg:#04x}, {n}): {q(nm)}"
                              for pg, n, nm in non_effect_params(pa))
    w(f"    public static let nonEffectParams: [NonEffectKey: String] = [\n        {rows},\n    ]")
    rows = ", ".join(f"{e['number']}: {q(e['name'])}" for e in
                     sorted(pa["string_tags"], key=lambda e: e["number"]))
    w(f"    public static let stringTags: [UInt8: String] = [{rows}]")
    rows = ", ".join(f"{e['number']:#04x}: {q(e['name'])}" for e in pa["page0_numeric"])
    w(f"    public static let page0Numeric: [UInt8: String] = [{rows}]")
    rows = ",\n        ".join(f"{e['value']}: {q(e['name'])}" for e in
                              sorted(d["effect-types"]["effect_types"], key=lambda e: e["value"]))
    w(f"    public static let effectTypes: [UInt16: String] = [\n        {rows},\n    ]")
    rows = ",\n        ".join(
        f"EffectCategory(min: {e['min']}, max: {e['max']}, name: {q(e['name'])})"
        for e in d["effect-types"]["effect_categories"])
    w(f"    public static let effectCategories: [EffectCategory] = [\n        {rows},\n    ]")
    rows = ", ".join(f"{q(e['slot'])}: {e['cc']}" for e in d["controls"]["slot_enable_cc"])
    w(f"    public static let slotEnableCc: [String: UInt8] = [{rows}]")
    rows = ",\n        ".join(
        f"MeterField(index: {f['index']}, number: {f['number']}, id: {q(f['id'])}, "
        f"name: {q(f['name'])}, render: {q(f['render'])})" for f in mb["fields"])
    w(f"    public static let meterFields: [MeterField] = [\n        {rows},\n    ]")
    w("}")
    w("")
    # Support types for Swift keys.
    w("public struct NonEffectKey: Hashable {")
    w("    public let page: UInt8")
    w("    public let number: UInt8")
    w("    public init(_ page: UInt8, _ number: UInt8) { self.page = page; self.number = number }")
    w("}")
    w("")
    w("public struct MeterField {")
    w("    public let index: Int")
    w("    public let number: UInt8")
    w("    public let id: String")
    w("    public let name: String")
    w("    public let render: String")
    w("}")
    w("")
    w("public struct EffectCategory {")
    w("    public let min: UInt16")
    w("    public let max: UInt16")
    w("    public let name: String")
    w("}")
    w("")
    return "\n".join(out) + "\n"


TARGETS = [
    ("rust/src/generated.rs", emit_rust),
    ("python/src/libkp/_generated.py", emit_python),
    ("swift/Sources/LibKP/Generated.swift", emit_swift),
]


def main() -> int:
    d = load()
    check = "--check" in sys.argv[1:]
    stale = []
    for rel, emit in TARGETS:
        path = ROOT / rel
        content = emit(d)
        if check:
            existing = path.read_text() if path.exists() else None
            if existing != content:
                stale.append(rel)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            print(f"wrote {rel}")
    if check:
        if stale:
            print("STALE generated files (run codegen/generate.py):", file=sys.stderr)
            for s in stale:
                print(f"  {s}", file=sys.stderr)
            return 1
        print("generated files are up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
