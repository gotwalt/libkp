//! Conformance suite — the shared cross-language contract.
//!
//! Every case comes from the repository's `spec/` directory:
//!
//! - `spec/vectors/*.json` — synthetic builder/parser vectors that pin each
//!   individual function's bytes and return values.
//! - `spec/captures/*.json` — sanitized replay fixtures that prove a real
//!   stream decodes end to end.
//!
//! The same files drive the Python and Swift suites, so a divergence in any
//! implementation fails that implementation's build.

use std::path::{Path, PathBuf};

use serde_json::Value;

use libkp::cbor::{self, Decoder};
use libkp::control::{Control, ModuleSlot};
use libkp::midi3::{Unframer, frame};
use libkp::model::RealtimeStatus;
use libkp::nrpn::{self, NrpnHeader};
use libkp::params;
use libkp::protocol::{TagStream, build_poll_request};
use libkp::state::DeviceState;
use libkp::{PORT, generated};

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

fn spec_dir() -> PathBuf {
    Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/../spec")).to_path_buf()
}

fn load(path: &Path) -> Value {
    let text = std::fs::read_to_string(path)
        .unwrap_or_else(|e| panic!("cannot read {}: {e}", path.display()));
    serde_json::from_str(&text)
        .unwrap_or_else(|e| panic!("cannot parse {} as JSON: {e}", path.display()))
}

fn vector(name: &str) -> Value {
    load(&spec_dir().join("vectors").join(name))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn unhex(s: &str) -> Vec<u8> {
    assert!(s.len() % 2 == 0, "odd-length hex string: {s:?}");
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).expect("valid hex"))
        .collect()
}

fn u8_of(v: &Value, key: &str) -> u8 {
    v[key]
        .as_u64()
        .unwrap_or_else(|| panic!("missing u8 {key}")) as u8
}

fn u16_of(v: &Value, key: &str) -> u16 {
    v[key]
        .as_u64()
        .unwrap_or_else(|| panic!("missing u16 {key}")) as u16
}

fn str_of(v: &Value, key: &str) -> String {
    v[key]
        .as_str()
        .unwrap_or_else(|| panic!("missing string {key}"))
        .to_string()
}

fn cases<'a>(doc: &'a Value, key: &str) -> &'a Vec<Value> {
    doc[key]
        .as_array()
        .unwrap_or_else(|| panic!("vector file has no array at {key:?}"))
}

/// `null` in a vector means "no mapping"; anything else is the expected text.
fn opt_str(v: &Value) -> Option<&str> {
    if v.is_null() {
        None
    } else {
        Some(v.as_str().expect("expected a string or null"))
    }
}

// ---------------------------------------------------------------------------
// spec version
// ---------------------------------------------------------------------------

#[test]
fn spec_version_matches() {
    assert_eq!(generated::SPEC_VERSION, "0.7.0");
    assert_eq!(libkp::SPEC_VERSION, generated::SPEC_VERSION);
    assert_eq!(PORT, 5727);
}

// ---------------------------------------------------------------------------
// u14.json
// ---------------------------------------------------------------------------

#[test]
fn u14_vectors() {
    let doc = vector("u14.json");
    for c in cases(&doc, "cases") {
        let value = u16_of(c, "value");
        let msb = u8_of(c, "msb");
        let lsb = u8_of(c, "lsb");
        assert_eq!(nrpn::u14_split(value), (msb, lsb), "u14_split({value})");
        assert_eq!(nrpn::u14(msb, lsb), value, "u14({msb}, {lsb})");
    }
}

// ---------------------------------------------------------------------------
// discovery.json
// ---------------------------------------------------------------------------

#[test]
fn discovery_vectors() {
    let doc = vector("discovery.json");
    let expect_len = doc["poll_request_len"].as_u64().unwrap() as usize;
    for c in cases(&doc, "poll_request") {
        let mac = str_of(c, "mac");
        let built = build_poll_request(&mac);
        assert_eq!(hex(&built), str_of(c, "hex"), "poll request for {mac}");
        assert_eq!(built.len(), expect_len, "poll request length for {mac}");
    }
}

// ---------------------------------------------------------------------------
// midi3.json
// ---------------------------------------------------------------------------

#[test]
fn midi3_vectors() {
    let doc = vector("midi3.json");

    for c in cases(&doc, "unframe") {
        let stream = unhex(&str_of(c, "stream"));
        let mut uf = Unframer::new();
        let msgs = uf.push(&stream);
        let got: Vec<String> = msgs.iter().map(|m| hex(m)).collect();
        let want: Vec<String> = cases(c, "messages")
            .iter()
            .map(|m| m.as_str().unwrap().to_string())
            .collect();
        assert_eq!(got, want, "unframe {}", str_of(c, "stream"));
        assert_eq!(
            uf.pending(),
            c["pending"].as_u64().unwrap() as usize,
            "pending after {}",
            str_of(c, "stream")
        );
    }

    for c in cases(&doc, "frame") {
        let msg = unhex(&str_of(c, "message"));
        let framed = frame(&msg);
        assert_eq!(
            hex(&framed),
            str_of(c, "framed"),
            "frame {}",
            str_of(c, "message")
        );
        // …and the round trip returns exactly the original message.
        let mut uf = Unframer::new();
        assert_eq!(uf.push(&framed), vec![msg]);
        assert_eq!(uf.pending(), 0);
    }
}

// ---------------------------------------------------------------------------
// nrpn.json
// ---------------------------------------------------------------------------

#[test]
fn nrpn_builder_vectors() {
    let doc = vector("nrpn.json");

    for c in cases(&doc, "request_string") {
        let got = nrpn::request_string(
            u8_of(c, "product"),
            u8_of(c, "device"),
            u8_of(c, "page"),
            u8_of(c, "number"),
        );
        assert_eq!(hex(&got), str_of(c, "hex"), "request_string");
    }

    for c in cases(&doc, "request_single") {
        let got = nrpn::request_single(
            u8_of(c, "product"),
            u8_of(c, "device"),
            u8_of(c, "page"),
            u8_of(c, "number"),
        );
        assert_eq!(hex(&got), str_of(c, "hex"), "request_single");
    }

    for c in cases(&doc, "request_multi") {
        let got = nrpn::request_multi(
            u8_of(c, "product"),
            u8_of(c, "device"),
            u8_of(c, "page"),
            u8_of(c, "number"),
        );
        assert_eq!(hex(&got), str_of(c, "hex"), "request_multi");
    }

    for c in cases(&doc, "set_single") {
        let got = nrpn::set_single(
            u8_of(c, "product"),
            u8_of(c, "device"),
            u8_of(c, "page"),
            u8_of(c, "number"),
            u16_of(c, "value"),
        );
        assert_eq!(hex(&got), str_of(c, "hex"), "set_single");
    }

    for c in cases(&doc, "request_rendered_string") {
        let got = nrpn::request_rendered_string(
            u8_of(c, "product"),
            u8_of(c, "device"),
            u8_of(c, "page"),
            u8_of(c, "number"),
            u16_of(c, "value"),
        );
        assert_eq!(hex(&got), str_of(c, "hex"), "request_rendered_string");
    }

    for c in cases(&doc, "beacon") {
        let got = nrpn::beacon(
            c["init"].as_bool().unwrap(),
            c["tuner"].as_bool().unwrap(),
            u8_of(c, "lease_secs"),
            u8_of(c, "param_set"),
            u8_of(c, "product"),
        );
        assert_eq!(hex(&got), str_of(c, "hex"), "beacon");
    }

    for c in cases(&doc, "control_change") {
        let got = nrpn::control_change(
            u8_of(c, "channel"),
            u8_of(c, "controller"),
            u8_of(c, "value"),
        );
        assert_eq!(hex(&got), str_of(c, "hex"), "control_change");
    }
}

#[test]
fn nrpn_parser_vectors() {
    let doc = vector("nrpn.json");

    for c in cases(&doc, "header_parse") {
        let msg = unhex(&str_of(c, "hex"));
        let (h, vals) = NrpnHeader::parse(&msg).expect("header should parse");
        assert_eq!(h.product, u8_of(c, "product"));
        assert_eq!(h.device, u8_of(c, "device"));
        assert_eq!(h.function, u8_of(c, "function"));
        assert_eq!(h.instance, u8_of(c, "instance"));
        assert_eq!(h.page, u8_of(c, "page"));
        assert_eq!(h.number, u8_of(c, "number"));
        assert_eq!(hex(vals), str_of(c, "values"));
    }

    for c in cases(&doc, "multi_values") {
        let got = nrpn::multi_values(u8_of(c, "number"), &unhex(&str_of(c, "values")));
        let want: Vec<(u8, u16)> = cases(c, "pairs")
            .iter()
            .map(|p| {
                let a = p.as_array().unwrap();
                (a[0].as_u64().unwrap() as u8, a[1].as_u64().unwrap() as u16)
            })
            .collect();
        assert_eq!(got, want, "multi_values");
    }

    for c in cases(&doc, "ext_decode") {
        let got = nrpn::ext_decode(&unhex(&str_of(c, "bytes")));
        assert_eq!(got, c["value"].as_u64().unwrap(), "ext_decode");
    }

    for c in cases(&doc, "request_extended_param") {
        let built = nrpn::request_extended_param(
            c["product"].as_u64().unwrap() as u8,
            c["device"].as_u64().unwrap() as u8,
            c["address"].as_u64().unwrap() as u32,
        );
        assert_eq!(hex(&built), str_of(c, "hex"), "request_extended_param");
    }

    for c in cases(&doc, "parse_extended_param") {
        let got = nrpn::parse_extended_param(&unhex(&str_of(c, "hex")));
        match c["expected"].as_object() {
            None => assert_eq!(got, None, "parse_extended_param should reject"),
            Some(_) => {
                let e = &c["expected"];
                let (addr, value) = got.expect("parse_extended_param should accept");
                assert_eq!(u64::from(addr), e["address"].as_u64().unwrap());
                assert_eq!(value, e["value"].as_u64().unwrap());
            }
        }
    }

    for c in cases(&doc, "parse_extended_string") {
        let got = nrpn::parse_extended_string(&unhex(&str_of(c, "hex")));
        match c["expected"].as_object() {
            None => assert_eq!(got, None, "parse_extended_string should reject"),
            Some(_) => {
                let e = &c["expected"];
                let (addr, text) = got.expect("parse_extended_string should accept");
                assert_eq!(u64::from(addr), e["address"].as_u64().unwrap());
                assert_eq!(text, e["text"].as_str().unwrap());
            }
        }
    }

    for c in cases(&doc, "parse_rendered_string") {
        let got = nrpn::parse_rendered_string(&unhex(&str_of(c, "hex")));
        match c["expected"].as_object() {
            None => assert_eq!(got, None, "parse_rendered_string should reject"),
            Some(_) => {
                let e = &c["expected"];
                let (page, number, value, text) = got.expect("parse_rendered_string should accept");
                assert_eq!(page, u8_of(e, "page"));
                assert_eq!(number, u8_of(e, "number"));
                assert_eq!(value, u16_of(e, "value"));
                assert_eq!(text, e["text"].as_str().unwrap());
            }
        }
    }
}

// ---------------------------------------------------------------------------
// controls.json
// ---------------------------------------------------------------------------

/// Map a vector `op` (+ its params) onto this crate's [`Control`] API.
fn control_for(op: &str, p: &Value) -> Control {
    let value = || p["value"].as_u64().unwrap() as u8;
    let on = || p["on"].as_bool().unwrap();
    let n = || p["n"].as_u64().unwrap() as u8;
    match op {
        "gain" => Control::Gain(value()),
        "wah_pedal" => Control::WahPedal(value()),
        "pitch_pedal" => Control::PitchPedal(value()),
        "volume_pedal" => Control::VolumePedal(value()),
        "panorama" => Control::Panorama(value()),
        "morph_pedal" => Control::MorphPedal(value()),
        "delay_mix" => Control::DelayMix(value()),
        "delay_feedback" => Control::DelayFeedback(value()),
        "reverb_mix" => Control::ReverbMix(value()),
        "reverb_time" => Control::ReverbTime(value()),
        "monitor_volume" => Control::MonitorVolume(value()),
        "tap_tempo" => Control::TapTempo,
        "tuner_mode" => Control::TunerMode(on()),
        "toggle_all_modules" => Control::ToggleAllModules,
        "up" => Control::Up,
        "down" => Control::Down,
        "bank_preselect" => Control::BankPreselect(value()),
        "rotary_fast" => Control::RotaryFast(on()),
        "delay_infinity" => Control::DelayInfinity(on()),
        "freeze" => Control::Freeze(on()),
        "morph_button" => Control::MorphButton(on()),
        "load_slot" => Control::LoadSlot(n()),
        "effect_button" => Control::EffectButton(n()),
        "slot_enable" => Control::SlotEnable {
            slot: ModuleSlot::from_name(p["slot"].as_str().unwrap())
                .expect("vector slot name must be A/B/C/D/X/MOD/DLY/REV"),
            on: on(),
        },
        "program_change" => Control::ProgramChange(p["program"].as_u64().unwrap() as u8),
        "bank_select" => Control::BankSelect {
            msb: p["msb"].as_u64().unwrap() as u8,
            lsb: p["lsb"].as_u64().unwrap() as u8,
        },
        other => panic!("conformance vector uses an unmapped control op {other:?}"),
    }
}

#[test]
fn control_vectors() {
    let doc = vector("controls.json");
    for c in cases(&doc, "cases") {
        let op = str_of(c, "op");
        let channel = u8_of(c, "channel");
        let control = control_for(&op, &c["params"]);
        assert_eq!(
            hex(&control.message(channel)),
            str_of(c, "hex"),
            "control op {op:?} on channel {channel}"
        );
    }
}

// ---------------------------------------------------------------------------
// params.json
// ---------------------------------------------------------------------------

#[test]
fn param_lookup_vectors() {
    let doc = vector("params.json");

    for c in cases(&doc, "param_name") {
        let (page, number) = (u8_of(c, "page"), u8_of(c, "number"));
        assert_eq!(
            params::param_name(page, number),
            opt_str(&c["name"]),
            "param_name({page}, {number})"
        );
    }

    for c in cases(&doc, "effect_type_name") {
        let value = u16_of(c, "value");
        assert_eq!(
            params::effect_type_name(value),
            opt_str(&c["name"]),
            "effect_type_name({value})"
        );
    }

    for c in cases(&doc, "effect_category_name") {
        let value = u16_of(c, "value");
        assert_eq!(
            params::effect_category_name(value),
            opt_str(&c["name"]),
            "effect_category_name({value})"
        );
    }

    for c in cases(&doc, "page_name") {
        let page = u8_of(c, "page");
        assert_eq!(
            params::page_name(page),
            opt_str(&c["name"]),
            "page_name({page})"
        );
    }

    for c in cases(&doc, "string_tag_name") {
        let number = u8_of(c, "number");
        assert_eq!(
            params::string_tag_name(number),
            opt_str(&c["name"]),
            "string_tag_name({number})"
        );
    }

    for c in cases(&doc, "describe") {
        let (page, number) = (u8_of(c, "page"), u8_of(c, "number"));
        assert_eq!(
            params::describe(page, number),
            str_of(c, "text"),
            "describe({page}, {number})"
        );
    }
}

// ---------------------------------------------------------------------------
// state.json
// ---------------------------------------------------------------------------

#[test]
fn state_apply_vectors() {
    let doc = vector("state.json");
    for c in cases(&doc, "cases") {
        let name = str_of(c, "name");
        let mut state = DeviceState::new();
        for m in cases(c, "messages") {
            state.apply(&unhex(m.as_str().unwrap()));
        }
        let expect = &c["expect"];

        if let Some(v) = expect.get("rig_name") {
            assert_eq!(state.rig.name.as_deref(), v.as_str(), "[{name}] rig_name");
        }
        if let Some(e) = expect.get("effect") {
            let slot = e["slot"].as_str().unwrap();
            let fx = state
                .effect(slot)
                .unwrap_or_else(|| panic!("[{name}] no effect slot {slot:?}"));
            if let Some(k) = e.get("kind") {
                assert_eq!(fx.kind, Some(k.as_u64().unwrap() as u16), "[{name}] kind");
            }
            if let Some(on) = e.get("on") {
                assert_eq!(fx.on, Some(on.as_bool().unwrap()), "[{name}] on");
            }
            if let Some(tn) = e.get("type_name") {
                assert_eq!(fx.type_name(), opt_str(tn), "[{name}] type_name");
            }
        }
        if let Some(v) = expect.get("status_raw") {
            let want: Vec<u16> = v
                .as_array()
                .unwrap()
                .iter()
                .map(|x| x.as_u64().unwrap() as u16)
                .collect();
            assert_eq!(state.status.raw.to_vec(), want, "[{name}] status_raw");
        }
        if let Some(v) = expect.get("tempo_bpm") {
            assert_eq!(
                state.rig.tempo_bpm,
                Some(v.as_u64().unwrap() as u16),
                "[{name}] tempo_bpm"
            );
        }
        if let Some(v) = expect.get("rig_volume") {
            assert_eq!(
                state.rig.volume,
                Some(v.as_u64().unwrap() as u16),
                "[{name}] rig_volume"
            );
        }
        if let Some(v) = expect.get("amp_on") {
            assert_eq!(state.amp.on, Some(v.as_bool().unwrap()), "[{name}] amp_on");
        }
        if let Some(v) = expect.get("amp_gain") {
            assert_eq!(
                state.amp.gain,
                Some(v.as_u64().unwrap() as u16),
                "[{name}] amp_gain"
            );
        }
        // A JSON null asserts the morph is still unset — the MIDI3 stream never
        // carries the position, so most messages must leave it alone.
        if let Some(v) = expect.get("morph") {
            assert_eq!(state.morph, v.as_u64().map(|v| v as u16), "[{name}] morph");
        }
        if let Some(v) = expect.get("tuner_note") {
            assert_eq!(
                state.tuner.note,
                Some(v.as_u64().unwrap() as u8),
                "[{name}] tuner_note"
            );
        }
        // A JSON null here asserts the half is still unknown — the device
        // pushes only the index that changed.
        if let Some(v) = expect.get("current_bank") {
            assert_eq!(
                state.current_bank,
                v.as_u64().map(|v| v as u16),
                "[{name}] current_bank"
            );
        }
        // A JSON null here asserts the half is still unknown — the device
        // pushes only the index that changed.
        if let Some(v) = expect.get("current_rig_slot") {
            assert_eq!(
                state.current_rig_slot,
                v.as_u64().map(|v| v as u16),
                "[{name}] current_rig_slot"
            );
        }
        // A JSON null here asserts the half is still unknown — the device
        // pushes only the index that changed.
        if let Some(v) = expect.get("current_rig_index") {
            assert_eq!(
                state.current_rig_index(),
                v.as_u64().map(|v| v as u16),
                "[{name}] current_rig_index"
            );
        }
        if let Some(v) = expect.get("main_volume") {
            assert_eq!(
                state.output.main_volume,
                Some(v.as_u64().unwrap() as u16),
                "[{name}] main_volume"
            );
        }
        if let Some(v) = expect.get("headphone_volume") {
            assert_eq!(
                state.output.headphone_volume,
                Some(v.as_u64().unwrap() as u16),
                "[{name}] headphone_volume"
            );
        }
        if let Some(v) = expect.get("monitor_volume") {
            assert_eq!(
                state.output.monitor_volume,
                Some(v.as_u64().unwrap() as u16),
                "[{name}] monitor_volume"
            );
        }
        if let Some(v) = expect.get("master_volume") {
            assert_eq!(
                state.output.master_volume(),
                Some(v.as_u64().unwrap() as u16),
                "[{name}] master_volume"
            );
        }
        if let Some(entries) = expect.get("bank").and_then(|v| v.as_array()) {
            for e in entries {
                let slot = e["slot"].as_u64().unwrap() as usize;
                let bank_slot = &state.bank.slots[slot];
                if let Some(v) = e.get("rig_name") {
                    assert_eq!(
                        bank_slot.rig_name.as_deref(),
                        v.as_str(),
                        "[{name}] bank rig_name"
                    );
                }
                if let Some(v) = e.get("amp_name") {
                    assert_eq!(
                        bank_slot.amp_name.as_deref(),
                        v.as_str(),
                        "[{name}] bank amp_name"
                    );
                }
                if let Some(v) = e.get("cabinet_name") {
                    assert_eq!(
                        bank_slot.cabinet_name.as_deref(),
                        v.as_str(),
                        "[{name}] bank cabinet_name"
                    );
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// cbor.json
// ---------------------------------------------------------------------------

#[test]
fn cbor_param_write_vectors() {
    let doc = vector("cbor.json");
    for c in cases(&doc, "param_write") {
        let addr = c["addr"].as_u64().unwrap() as u32;
        let value = c["value"].as_i64().unwrap();
        assert_eq!(
            hex(&cbor::to_vec(&cbor::param_write(addr, value))),
            str_of(c, "hex"),
            "param_write({addr}, {value})"
        );
    }
    assert_eq!(
        hex(&cbor::to_vec(&cbor::state_dump_request())),
        str_of(&doc["state_dump_request"], "hex"),
        "state_dump_request"
    );
}

#[test]
fn cbor_extract_snapshot_vectors() {
    let doc = vector("cbor.json");
    for c in cases(&doc, "extract_snapshot") {
        let name = str_of(c, "name");
        let mut decoder = Decoder::new();
        let items = decoder.push(&unhex(&str_of(c, "stream_hex")));
        let snap = cbor::extract_snapshot(&items);
        let expect = &c["expect"];

        let want_bank = expect["current_bank"].as_u64().map(|v| v as u16);
        assert_eq!(snap.current_bank, want_bank, "[{name}] current_bank");
        let want_slot = expect["current_rig_slot"].as_u64().map(|v| v as u16);
        assert_eq!(
            snap.current_rig_slot, want_slot,
            "[{name}] current_rig_slot"
        );
        let want_morph = expect["morph"].as_u64().map(|v| v as u16);
        assert_eq!(snap.morph, want_morph, "[{name}] morph");

        if let Some(strings) = expect.get("strings").and_then(|v| v.as_array()) {
            let got: Vec<(u32, String)> = snap.strings.clone();
            let want: Vec<(u32, String)> = strings
                .iter()
                .map(|s| (s["addr"].as_u64().unwrap() as u32, str_of(s, "text")))
                .collect();
            assert_eq!(got, want, "[{name}] strings");
        }
    }
}

// ---------------------------------------------------------------------------
// spec/captures — replay fixtures
// ---------------------------------------------------------------------------

/// Assert a `kind: "discovery"` fixture: the raw bytes parse as a tag stream
/// with the expected header and key/value fields.
fn check_discovery_fixture(name: &str, fixture: &Value) {
    let raw = unhex(&str_of(fixture, "raw"));
    let ts = TagStream::parse(&raw).unwrap_or_else(|e| panic!("[{name}] tag stream: {e}"));
    let expect = &fixture["expected"];

    if let Some(h) = expect.get("header").and_then(|v| v.as_str()) {
        let got = ts
            .header
            .map(|b| String::from_utf8_lossy(&b).into_owned())
            .unwrap_or_else(|| panic!("[{name}] no header parsed"));
        assert_eq!(got, h, "[{name}] header");
    }

    if let Some(want) = expect.get("key_values").and_then(|v| v.as_array()) {
        let got: Vec<(String, String)> = ts
            .key_values()
            .into_iter()
            .map(|(k, v)| (k, String::from_utf8_lossy(&v).into_owned()))
            .collect();
        let want: Vec<(String, String)> = want
            .iter()
            .map(|p| {
                let a = p.as_array().unwrap();
                (
                    a[0].as_str().unwrap().to_string(),
                    a[1].as_str().unwrap().to_string(),
                )
            })
            .collect();
        assert_eq!(got, want, "[{name}] key_values");
    }
}

/// Assert a `kind: "midi3_stream"` fixture: unframe the whole stream in one
/// push, then check whichever expectations the fixture carries.
fn check_stream_fixture(name: &str, fixture: &Value) {
    let raw = unhex(&str_of(fixture, "raw"));
    let mut uf = Unframer::new();
    let msgs = uf.push(&raw);
    let expect = &fixture["expected"];

    if let Some(n) = expect.get("message_count").and_then(|v| v.as_u64()) {
        assert_eq!(msgs.len() as u64, n, "[{name}] message_count");
    }
    if let Some(n) = expect.get("pending").and_then(|v| v.as_u64()) {
        assert_eq!(uf.pending() as u64, n, "[{name}] pending");
    }
    if let Some(want) = expect.get("messages").and_then(|v| v.as_array()) {
        let got: Vec<String> = msgs.iter().map(|m| hex(m)).collect();
        let want: Vec<String> = want
            .iter()
            .map(|m| m.as_str().unwrap().to_string())
            .collect();
        assert_eq!(got, want, "[{name}] messages");
    }

    if let Some(frames) = expect.get("status_frames").and_then(|v| v.as_array()) {
        for f in frames {
            let idx = f["index"].as_u64().unwrap() as usize;
            let want: Vec<u16> = f["raw"]
                .as_array()
                .unwrap()
                .iter()
                .map(|x| x.as_u64().unwrap() as u16)
                .collect();
            let msg = msgs
                .get(idx)
                .unwrap_or_else(|| panic!("[{name}] no message at index {idx}"));
            let mut state = DeviceState::new();
            state.apply(msg);
            assert_eq!(
                state.status.raw.to_vec(),
                want,
                "[{name}] status frame at index {idx}"
            );
            // The decoded frame is the same one the model's fast lane exposes.
            assert_eq!(
                state.status,
                RealtimeStatus {
                    raw: state.status.raw
                }
            );
        }
    }

    if let Some(hist) = expect.get("function_histogram").and_then(|v| v.as_object()) {
        let mut got: std::collections::BTreeMap<String, u64> = Default::default();
        for m in &msgs {
            let key = match NrpnHeader::parse(m) {
                Some((h, _)) => h.function.to_string(),
                None => "none".to_string(),
            };
            *got.entry(key).or_default() += 1;
        }
        let want: std::collections::BTreeMap<String, u64> = hist
            .iter()
            .map(|(k, v)| (k.clone(), v.as_u64().unwrap()))
            .collect();
        assert_eq!(got, want, "[{name}] function_histogram");
    }

    if let Some(st) = expect.get("state") {
        let mut state = DeviceState::new();
        for m in &msgs {
            state.apply(m);
        }
        if let Some(v) = st.get("rig_name") {
            assert_eq!(state.rig.name.as_deref(), v.as_str(), "[{name}] rig_name");
        }
        if let Some(v) = st.get("amp_name") {
            assert_eq!(state.amp.name.as_deref(), v.as_str(), "[{name}] amp_name");
        }
        if let Some(v) = st.get("cab_name") {
            assert_eq!(
                state.cabinet.name.as_deref(),
                v.as_str(),
                "[{name}] cab_name"
            );
        }
    }
}

#[test]
fn replay_capture_fixtures() {
    let dir = spec_dir().join("captures");
    let manifest = load(&dir.join("manifest.json"));
    let fixtures = manifest["fixtures"]
        .as_array()
        .expect("manifest has a fixtures array");
    assert!(!fixtures.is_empty(), "manifest lists no fixtures");

    for entry in fixtures {
        let file = str_of(entry, "file");
        let kind = str_of(entry, "kind");
        let fixture = load(&dir.join(&file));
        let name = fixture
            .get("name")
            .and_then(|v| v.as_str())
            .unwrap_or(&file)
            .to_string();
        // The fixture is self-describing; the manifest kind must agree with it.
        if let Some(k) = fixture.get("kind").and_then(|v| v.as_str()) {
            assert_eq!(k, kind, "[{name}] manifest/fixture kind disagree");
        }
        match kind.as_str() {
            "discovery" => check_discovery_fixture(&name, &fixture),
            "midi3_stream" => check_stream_fixture(&name, &fixture),
            other => panic!("manifest lists an unknown fixture kind {other:?}"),
        }
    }
}
