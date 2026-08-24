import Foundation
import XCTest

@testable import LibKP

/// The shared cross-language conformance suite: every file in `spec/vectors`
/// is loaded and asserted against this implementation.
final class ConformanceTests: XCTestCase {
    // MARK: - Suite bookkeeping

    func testSpecVersionMatches() {
        XCTAssertEqual(Generated.specVersion, "0.7.0")
    }

    /// Every vector file must be covered by a test in this class, so a new file
    /// cannot land unnoticed.
    func testEveryVectorFileIsCovered() throws {
        let covered: Set<String> = [
            "u14.json", "discovery.json", "midi3.json", "nrpn.json",
            "controls.json", "params.json", "state.json", "cbor.json",
        ]
        let present = Set(try Fixtures.vectorFiles().map(\.lastPathComponent))
        XCTAssertEqual(present, covered, "spec/vectors changed; update the conformance suite")
    }

    // MARK: - u14.json

    func testU14Vectors() throws {
        let vector = try Fixtures.vector("u14")
        let cases = vector.cases("cases")
        XCTAssertFalse(cases.isEmpty)
        for entry in cases {
            let value = entry.u16("value")
            let msb = entry.u8("msb")
            let lsb = entry.u8("lsb")
            XCTAssertEqual(Nrpn.u14(msb, lsb), value, "u14(\(msb), \(lsb))")
            let split = Nrpn.u14Split(value)
            XCTAssertEqual(split.msb, msb, "u14Split(\(value)).msb")
            XCTAssertEqual(split.lsb, lsb, "u14Split(\(value)).lsb")
        }
    }

    // MARK: - discovery.json

    func testDiscoveryVectors() throws {
        let vector = try Fixtures.vector("discovery")
        let expectedLength = vector.int("poll_request_len")
        let cases = vector.cases("poll_request")
        XCTAssertFalse(cases.isEmpty)
        for entry in cases {
            let mac = entry.string("mac")
            let packet = KemperProtocol.buildPollRequest(mac: mac)
            XCTAssertEqual(Fmt.hex(packet), entry.string("hex"), "poll request for \(mac)")
            XCTAssertEqual(packet.count, expectedLength)

            // A built packet must round-trip through the TagStream parser.
            let stream = try TagStream.parse(packet)
            XCTAssertEqual(stream.header, Array("DSCV".utf8))
            XCTAssertEqual(stream.fields.count, 2)
            XCTAssertEqual(String(decoding: stream.fields[0], as: UTF8.self), "MAC#\(mac)")
            XCTAssertEqual(String(decoding: stream.fields[1], as: UTF8.self), "POLL:)")
        }
    }

    // MARK: - midi3.json

    func testMidi3Vectors() throws {
        let vector = try Fixtures.vector("midi3")

        for entry in vector.cases("unframe") {
            let stream = try hex(entry.string("stream"))
            var unframer = Midi3.Unframer()
            let messages = unframer.push(stream)
            let expected = (entry["messages"] as? [String]) ?? []
            XCTAssertEqual(
                messages.map { Fmt.hex($0) }, expected, "unframe \(entry.string("stream"))")
            XCTAssertEqual(unframer.pending, entry.int("pending"), "pending")
        }

        for entry in vector.cases("frame") {
            let message = try hex(entry.string("message"))
            let framed = Midi3.frame(message)
            XCTAssertEqual(
                Fmt.hex(framed), entry.string("framed"), "frame \(entry.string("message"))")
            XCTAssertEqual(framed.count % 4, 0)

            // Round-trip: frame then unframe yields exactly the input message.
            var unframer = Midi3.Unframer()
            XCTAssertEqual(unframer.push(framed), [message])
            XCTAssertEqual(unframer.pending, 0)
        }
    }

    // MARK: - nrpn.json

    func testNrpnVectors() throws {
        let vector = try Fixtures.vector("nrpn")

        for entry in vector.cases("request_string") {
            let built = Nrpn.requestString(
                product: entry.u8("product"), device: entry.u8("device"),
                page: entry.u8("page"), number: entry.u8("number")
            )
            XCTAssertEqual(Fmt.hex(built), entry.string("hex"), "request_string")
        }

        for entry in vector.cases("request_single") {
            let built = Nrpn.requestSingle(
                product: entry.u8("product"), device: entry.u8("device"),
                page: entry.u8("page"), number: entry.u8("number")
            )
            XCTAssertEqual(Fmt.hex(built), entry.string("hex"), "request_single")
        }

        for entry in vector.cases("request_multi") {
            let built = Nrpn.requestMulti(
                product: entry.u8("product"), device: entry.u8("device"),
                page: entry.u8("page"), number: entry.u8("number")
            )
            XCTAssertEqual(Fmt.hex(built), entry.string("hex"), "request_multi")
        }

        for entry in vector.cases("set_single") {
            let built = Nrpn.setSingle(
                product: entry.u8("product"), device: entry.u8("device"),
                page: entry.u8("page"), number: entry.u8("number"), value: entry.u16("value")
            )
            XCTAssertEqual(Fmt.hex(built), entry.string("hex"), "set_single")
        }

        for entry in vector.cases("request_rendered_string") {
            let built = Nrpn.requestRenderedString(
                product: entry.u8("product"), device: entry.u8("device"),
                page: entry.u8("page"), number: entry.u8("number"), value: entry.u16("value")
            )
            XCTAssertEqual(Fmt.hex(built), entry.string("hex"), "request_rendered_string")
        }

        for entry in vector.cases("beacon") {
            let built = Nrpn.beacon(
                init: entry.bool("init"), tuner: entry.bool("tuner"),
                leaseSecs: entry.u8("lease_secs"), paramSet: entry.u8("param_set"),
                product: entry.u8("product")
            )
            XCTAssertEqual(Fmt.hex(built), entry.string("hex"), "beacon")
        }

        for entry in vector.cases("control_change") {
            let built = Nrpn.controlChange(
                channel: entry.u8("channel"), controller: entry.u8("controller"),
                value: entry.u8("value")
            )
            XCTAssertEqual(Fmt.hex(built), entry.string("hex"), "control_change")
        }

        for entry in vector.cases("header_parse") {
            let message = try hex(entry.string("hex"))
            let parsed = NrpnHeader.parse(message)
            XCTAssertNotNil(parsed, "header_parse \(entry.string("hex"))")
            guard let (header, values) = parsed else { continue }
            XCTAssertEqual(header.product, entry.u8("product"))
            XCTAssertEqual(header.device, entry.u8("device"))
            XCTAssertEqual(header.function, entry.u8("function"))
            XCTAssertEqual(header.instance, entry.u8("instance"))
            XCTAssertEqual(header.page, entry.u8("page"))
            XCTAssertEqual(header.number, entry.u8("number"))
            XCTAssertEqual(Fmt.hex(values), entry.string("values"))
        }

        for entry in vector.cases("multi_values") {
            let values = try hex(entry.string("values"))
            let pairs = Nrpn.multiValues(number: entry.u8("number"), values: values)
            let expected = (entry["pairs"] as? [[NSNumber]]) ?? []
            XCTAssertEqual(pairs.count, expected.count, "multi_values count")
            for (actual, want) in zip(pairs, expected) {
                XCTAssertEqual(Int(actual.number), want[0].intValue)
                XCTAssertEqual(Int(actual.value), want[1].intValue)
            }
        }

        for entry in vector.cases("ext_decode") {
            let bytes = try hex(entry.string("bytes"))
            let expected = (entry["value"] as? NSNumber)?.uint64Value ?? 0
            XCTAssertEqual(Nrpn.extDecode(bytes), expected, "ext_decode \(entry.string("bytes"))")
            // The inverse must reproduce the input bytes.
            XCTAssertEqual(Nrpn.extEncode(expected, count: bytes.count), bytes)
        }

        for entry in vector.cases("request_extended_param") {
            let built = Nrpn.requestExtendedParam(
                product: UInt8(entry.int("product")), device: UInt8(entry.int("device")),
                address: UInt32(entry.int("address")))
            XCTAssertEqual(Fmt.hex(built), entry.string("hex"), "request_extended_param")
        }

        for entry in vector.cases("parse_extended_param") {
            let message = try hex(entry.string("hex"))
            let parsed = Nrpn.parseExtendedParam(message)
            guard let expected = entry["expected"] as? [String: Any] else {
                XCTAssertNil(parsed, "parse_extended_param should reject \(entry.string("hex"))")
                continue
            }
            XCTAssertEqual(parsed?.address, UInt32(expected.int("address")))
            XCTAssertEqual(parsed?.value, UInt64(expected.int("value")))
        }

        for entry in vector.cases("parse_extended_string") {
            let message = try hex(entry.string("hex"))
            let parsed = Nrpn.parseExtendedString(message)
            guard let expected = entry["expected"] as? [String: Any] else {
                XCTAssertNil(parsed, "parse_extended_string should reject \(entry.string("hex"))")
                continue
            }
            XCTAssertEqual(parsed?.address, UInt32(expected.int("address")))
            XCTAssertEqual(parsed?.text, expected.string("text"))
        }

        for entry in vector.cases("parse_rendered_string") {
            let message = try hex(entry.string("hex"))
            let parsed = Nrpn.parseRenderedString(message)
            guard let expected = entry["expected"] as? [String: Any] else {
                XCTAssertNil(parsed, "parse_rendered_string should reject \(entry.string("hex"))")
                continue
            }
            XCTAssertEqual(parsed?.page, expected.u8("page"))
            XCTAssertEqual(parsed?.number, expected.u8("number"))
            XCTAssertEqual(parsed?.value, expected.u16("value"))
            XCTAssertEqual(parsed?.text, expected.string("text"))
        }
    }

    // MARK: - controls.json

    func testControlVectors() throws {
        let vector = try Fixtures.vector("controls")
        let cases = vector.cases("cases")
        XCTAssertFalse(cases.isEmpty)
        for entry in cases {
            let op = entry.string("op")
            let params = (entry["params"] as? [String: Any]) ?? [:]
            let channel = entry.u8("channel")
            guard let control = ConformanceTests.control(op: op, params: params) else {
                XCTFail("no Control mapping for op \"\(op)\"")
                continue
            }
            XCTAssertEqual(
                Fmt.hex(control.message(channel: channel)),
                entry.string("hex"),
                "\(op) on channel \(channel)"
            )
        }
    }

    /// Map a vector `op` + `params` onto this implementation's `Control` API.
    static func control(op: String, params: [String: Any]) -> Control? {
        func value() -> UInt8 { params.u8("value") }
        func on() -> Bool { params.bool("on") }
        func n() -> UInt8 { UInt8(clamping: params.int("n")) }

        switch op {
        case "wah_pedal": return .wahPedal(value())
        case "pitch_pedal": return .pitchPedal(value())
        case "volume_pedal": return .volumePedal(value())
        case "panorama": return .panorama(value())
        case "morph_pedal": return .morphPedal(value())
        case "gain": return .gain(value())
        case "delay_mix": return .delayMix(value())
        case "delay_feedback": return .delayFeedback(value())
        case "reverb_mix": return .reverbMix(value())
        case "reverb_time": return .reverbTime(value())
        case "monitor_volume": return .monitorVolume(value())
        case "toggle_all_modules": return .toggleAllModules
        case "rotary_fast": return .rotaryFast(on())
        case "delay_infinity": return .delayInfinity(on())
        case "freeze": return .freeze(on())
        case "tap_tempo": return .tapTempo
        case "tuner_mode": return .tunerMode(on())
        case "bank_preselect": return .bankPreselect(value())
        case "up": return .up
        case "down": return .down
        case "load_slot": return .loadSlot(n())
        case "effect_button": return .effectButton(n())
        case "morph_button": return .morphButton(on())
        case "program_change": return .programChange(params.u8("program"))
        case "bank_select": return .bankSelect(msb: params.u8("msb"), lsb: params.u8("lsb"))
        case "slot_enable":
            guard let slot = ModuleSlot(name: params.string("slot")) else { return nil }
            return .slotEnable(slot: slot, on: on())
        default: return nil
        }
    }

    // MARK: - params.json

    func testParamVectors() throws {
        let vector = try Fixtures.vector("params")

        for entry in vector.cases("param_name") {
            let actual = Params.paramName(page: entry.u8("page"), number: entry.u8("number"))
            XCTAssertEqual(
                actual, entry.optionalString("name"),
                "param_name(\(entry.int("page")), \(entry.int("number")))"
            )
        }

        for entry in vector.cases("effect_type_name") {
            XCTAssertEqual(
                Params.effectTypeName(entry.u16("value")), entry.optionalString("name"),
                "effect_type_name(\(entry.int("value")))"
            )
        }

        for entry in vector.cases("effect_category_name") {
            XCTAssertEqual(
                Params.effectCategoryName(entry.u16("value")), entry.optionalString("name"),
                "effect_category_name(\(entry.int("value")))"
            )
        }

        for entry in vector.cases("page_name") {
            XCTAssertEqual(
                Params.pageName(entry.u8("page")), entry.optionalString("name"),
                "page_name(\(entry.int("page")))"
            )
        }

        for entry in vector.cases("string_tag_name") {
            XCTAssertEqual(
                Params.stringTagName(entry.u8("number")), entry.optionalString("name"),
                "string_tag_name(\(entry.int("number")))"
            )
        }

        for entry in vector.cases("describe") {
            XCTAssertEqual(
                Params.describe(page: entry.u8("page"), number: entry.u8("number")),
                entry.string("text"),
                "describe(\(entry.int("page")), \(entry.int("number")))"
            )
        }
    }

    // MARK: - state.json

    /// A case carries either `"messages"` (unframed MIDI3 hex, each through
    /// `apply`) or `"steps"` (each naming the entry point it drives). The steps
    /// form also pins the ordered event names every step raised and how many
    /// steps flagged the snapshot, so the fold's reporting is held to the
    /// vectors and not only its tree.
    func testStateVectors() throws {
        let vector = try Fixtures.vector("state")
        let cases = vector.cases("cases")
        XCTAssertFalse(cases.isEmpty)
        for entry in cases {
            let name = entry.string("name")
            var state = DeviceState()
            var outcomes: [ApplyOutcome] = []
            for messageHex in (entry["messages"] as? [String]) ?? [] {
                outcomes.append(state.apply(try hex(messageHex)))
            }
            for step in (entry["steps"] as? [[String: Any]]) ?? [] {
                outcomes.append(try run(step, on: &state, caseName: name))
            }
            guard let expect = entry["expect"] as? [String: Any] else {
                XCTFail("case \"\(name)\" has no expect block")
                continue
            }
            assertState(state, matches: expect, caseName: name)
            if let events = expect["events"] as? [String] {
                XCTAssertEqual(
                    outcomes.flatMap(\.events).map(eventName), events, "\(name): events")
            }
            if let slowSteps = expect["slow_steps"] as? NSNumber {
                XCTAssertEqual(
                    outcomes.filter(\.slowChanged).count, slowSteps.intValue,
                    "\(name): slow_steps")
            }
        }
    }

    /// Drive one `"steps"` entry — exactly one key naming the entry point —
    /// against `state`, returning what it reported.
    private func run(
        _ step: [String: Any], on state: inout DeviceState, caseName: String
    ) throws -> ApplyOutcome {
        guard step.count == 1, let (kind, payload) = step.first else {
            throw Fixtures.Failure("\(caseName): a step must have exactly one key")
        }
        func pair() throws -> (UInt32, Any) {
            guard let list = payload as? [Any], list.count == 2, let addr = list[0] as? NSNumber
            else { throw Fixtures.Failure("\(caseName): bad \(kind) step") }
            return (addr.uint32Value, list[1])
        }
        func number(_ value: Any) throws -> UInt64 {
            guard let n = value as? NSNumber, let u = UInt64(exactly: n.int64Value) else {
                throw Fixtures.Failure("\(caseName): \(kind) value is not a whole number")
            }
            return u
        }
        func text(_ value: Any) throws -> String {
            guard let t = value as? String else {
                throw Fixtures.Failure("\(caseName): \(kind) value is not a string")
            }
            return t
        }
        switch kind {
        case "midi3":
            return state.apply(try hex(try text(payload)))
        case "cbor":
            let (address, value) = try pair()
            return state.applyCbor(address: address, value: Int64(try number(value)))
        case "cbor_text":
            let (address, value) = try pair()
            return state.applyCborText(address: address, text: try text(value))
        case "cbor_dump":
            let (address, value) = try pair()
            return state.applyUpdate(
                Update(
                    source: .control, phase: .dump, address: address,
                    decoded: .num(try number(value))))
        case "cbor_dump_text":
            let (address, value) = try pair()
            return state.applyUpdate(
                Update(
                    source: .control, phase: .dump, address: address,
                    decoded: .text(try text(value))))
        case "dump_begin":
            state.beginDump()
            return ApplyOutcome()
        case "dump_end":
            state.endDump()
            return ApplyOutcome()
        default:
            throw Fixtures.Failure("\(caseName): unknown step kind \"\(kind)\"")
        }
    }

    /// The vectors' snake_case name for an event, as `spec/state.toml` spells
    /// the `event` column.
    private func eventName(_ event: DeviceEvent) -> String {
        switch event {
        case .rigChanged: return "rig_changed"
        case .stringTag: return "string_tag"
        case .bankPreview: return "bank_preview"
        case .effectChanged: return "effect_changed"
        case .paramChanged: return "param_changed"
        case .status: return "status"
        case .beatPulse: return "beat_pulse"
        case .tempoBpm: return "tempo_bpm"
        case .morphChanged: return "morph_changed"
        case .morphButton: return "morph_button"
        case .tunerDeviance: return "tuner_deviance"
        case .tunerNote: return "tuner_note"
        case .renderedString: return "rendered_string"
        case .currentPosition: return "current_position"
        case .connected: return "connected"
        case .disconnected: return "disconnected"
        // The model raises these from what its sockets do; no vector step
        // drives them, so they have no wire name to check.
        case .connectionChanged: return "connection_changed"
        case .channelChanged: return "channel_changed"
        case .syncCompleted: return "sync_completed"
        case .requestTimedOut: return "request_timed_out"
        }
    }

    private func assertState(_ state: DeviceState, matches expect: [String: Any], caseName: String)
    {
        if let rigName = expect["rig_name"] as? String {
            XCTAssertEqual(state.rig.name, rigName, "\(caseName): rig_name")
        }
        if let author = expect["rig_author"] as? String {
            XCTAssertEqual(state.rig.author, author, "\(caseName): rig_author")
        }
        if let ampName = expect["amp_name"] as? String {
            XCTAssertEqual(state.amp.name, ampName, "\(caseName): amp_name")
        }
        if let cabName = expect["cab_name"] as? String {
            XCTAssertEqual(state.cabinet.name, cabName, "\(caseName): cab_name")
        }
        if let tempo = expect["tempo_bpm"] as? NSNumber {
            XCTAssertEqual(state.rig.tempoBpm, tempo.uint16Value, "\(caseName): tempo_bpm")
        }
        if let volume = expect["rig_volume"] as? NSNumber {
            XCTAssertEqual(state.rig.volume, volume.uint16Value, "\(caseName): rig_volume")
        }
        if let ampOn = expect["amp_on"] as? NSNumber {
            XCTAssertEqual(state.amp.on, ampOn.boolValue, "\(caseName): amp_on")
        }
        if let gain = expect["amp_gain"] as? NSNumber {
            XCTAssertEqual(state.amp.gain, gain.uint16Value, "\(caseName): amp_gain")
        }
        // A JSON null asserts the morph is still unset — the MIDI3 stream never
        // carries the position, so most messages must leave it alone.
        if let morph = expect["morph"] {
            XCTAssertEqual(state.morph, (morph as? NSNumber)?.uint16Value, "\(caseName): morph")
        }
        if let note = expect["tuner_note"] as? NSNumber {
            XCTAssertEqual(state.tuner.note, note.uint8Value, "\(caseName): tuner_note")
        }
        if let deviance = expect["tuner_deviance"] as? NSNumber {
            XCTAssertEqual(
                state.tuner.deviance, deviance.uint16Value, "\(caseName): tuner_deviance")
        }
        // A JSON null here asserts the half is still unknown — the device pushes
        // only the index that changed.
        if let bank = expect["current_bank"] {
            XCTAssertEqual(
                state.currentBank, (bank as? NSNumber)?.uint16Value, "\(caseName): current_bank")
        }
        if let slot = expect["current_rig_slot"] {
            XCTAssertEqual(
                state.currentRigSlot, (slot as? NSNumber)?.uint16Value,
                "\(caseName): current_rig_slot")
        }
        if let index = expect["current_rig_index"] {
            XCTAssertEqual(
                state.currentRigIndex, (index as? NSNumber)?.uint16Value,
                "\(caseName): current_rig_index")
        }
        if let mainVolume = expect["main_volume"] as? NSNumber {
            XCTAssertEqual(
                state.output.mainVolume, mainVolume.uint16Value, "\(caseName): main_volume")
        }
        if let monitorVolume = expect["monitor_volume"] as? NSNumber {
            XCTAssertEqual(
                state.output.monitorVolume, monitorVolume.uint16Value, "\(caseName): monitor_volume"
            )
        }
        if let headphoneVolume = expect["headphone_volume"] as? NSNumber {
            XCTAssertEqual(
                state.output.headphoneVolume, headphoneVolume.uint16Value,
                "\(caseName): headphone_volume")
        }
        if let masterVolume = expect["master_volume"] as? NSNumber {
            XCTAssertEqual(
                state.output.masterVolume, masterVolume.uint16Value, "\(caseName): master_volume")
        }
        if let bank = expect["bank"] as? [[String: Any]] {
            for entry in bank {
                let slot = state.bank.slots[entry.int("slot")]
                if let rigName = entry["rig_name"] as? String {
                    XCTAssertEqual(slot.rigName, rigName, "\(caseName): bank rig_name")
                }
                if let ampName = entry["amp_name"] as? String {
                    XCTAssertEqual(slot.ampName, ampName, "\(caseName): bank amp_name")
                }
                if let cabName = entry["cabinet_name"] as? String {
                    XCTAssertEqual(slot.cabinetName, cabName, "\(caseName): bank cabinet_name")
                }
            }
        }
        if let raw = expect["status_raw"] as? [NSNumber] {
            XCTAssertEqual(state.status.raw, raw.map(\.uint16Value), "\(caseName): status_raw")
        }
        if let effect = expect["effect"] as? [String: Any] {
            let slot = effect.string("slot")
            guard let actual = state.effect(slot) else {
                XCTFail("\(caseName): no effect slot \"\(slot)\"")
                return
            }
            if let kind = effect["kind"] as? NSNumber {
                XCTAssertEqual(actual.kind, kind.uint16Value, "\(caseName): effect kind")
            }
            if let on = effect["on"] as? NSNumber {
                XCTAssertEqual(actual.on, on.boolValue, "\(caseName): effect on")
            }
            if let mix = effect["mix"] as? NSNumber {
                XCTAssertEqual(actual.mix, mix.uint16Value, "\(caseName): effect mix")
            }
            if let typeName = effect["type_name"] as? String {
                XCTAssertEqual(actual.typeName, typeName, "\(caseName): effect type_name")
            }
        }
    }

    // MARK: - cbor.json

    func testCborVectors() throws {
        let vector = try Fixtures.vector("cbor")

        for entry in vector.cases("param_write") {
            let addr = UInt32(entry.int("addr"))
            let value = Int64(entry.int("value"))
            XCTAssertEqual(
                Fmt.hex(Cbor.encode(Cbor.paramWrite(addr: addr, value: value))),
                entry.string("hex"), "param_write(\(addr), \(value))")
        }
        if let dump = vector["state_dump_request"] as? [String: Any] {
            XCTAssertEqual(
                Fmt.hex(Cbor.encode(Cbor.stateDumpRequest())), dump.string("hex"),
                "state_dump_request")
        } else {
            XCTFail("cbor.json has no state_dump_request")
        }

        for entry in vector.cases("extract_snapshot") {
            let name = entry.string("name")
            var decoder = CBORDecoder()
            let items = decoder.push(try hex(entry.string("stream_hex")))
            let snap = Cbor.extractSnapshot(items)
            guard let expect = entry["expect"] as? [String: Any] else {
                XCTFail("case \"\(name)\" has no expect block")
                continue
            }
            if let bank = expect["current_bank"] as? NSNumber {
                XCTAssertEqual(snap.currentBank, bank.uint16Value, "\(name): current_bank")
            } else {
                XCTAssertNil(snap.currentBank, "\(name): current_bank")
            }
            if let slot = expect["current_rig_slot"] as? NSNumber {
                XCTAssertEqual(snap.currentRigSlot, slot.uint16Value, "\(name): current_rig_slot")
            } else {
                XCTAssertNil(snap.currentRigSlot, "\(name): current_rig_slot")
            }
            XCTAssertEqual(
                snap.morph, (expect["morph"] as? NSNumber)?.uint16Value, "\(name): morph")
            if let strings = expect["strings"] as? [[String: Any]] {
                XCTAssertEqual(snap.strings.count, strings.count, "\(name): strings count")
                for (i, expected) in strings.enumerated() where i < snap.strings.count {
                    XCTAssertEqual(
                        snap.strings[i].address, UInt32(expected.int("addr")),
                        "\(name): strings[\(i)].addr")
                    XCTAssertEqual(
                        snap.strings[i].text, expected.string("text"), "\(name): strings[\(i)].text"
                    )
                }
            }
        }
    }

    // MARK: - Helpers

    private func hex(_ string: String) throws -> [UInt8] {
        guard let bytes = Fmt.bytes(fromHex: string) else {
            throw Fixtures.Failure("bad hex string \"\(string)\"")
        }
        return bytes
    }
}
