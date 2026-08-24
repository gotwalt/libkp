import Foundation
import XCTest

@testable import LibKP

/// The replay-capture harness: sanitized recordings of real protocol traffic,
/// gathered by observed experimentation, driven end to end through the decode
/// pipeline. These complement the synthetic vectors — the vectors pin individual
/// functions, these prove a whole stream decodes correctly.
final class CaptureTests: XCTestCase {
    /// Load `manifest.json` and run every fixture it lists.
    func testAllCaptureFixtures() throws {
        let manifest = try Fixtures.capture("manifest.json")
        let fixtures = manifest.cases("fixtures")
        XCTAssertFalse(fixtures.isEmpty, "the capture manifest lists no fixtures")

        var checks = 0
        for entry in fixtures {
            let file = entry.string("file")
            let kind = entry.string("kind")
            let fixture = try Fixtures.capture(file)
            XCTAssertEqual(fixture.string("kind"), kind, "\(file): manifest/fixture kind disagree")

            switch kind {
            case "discovery":
                checks += try assertDiscovery(fixture, file: file)
            case "midi3_stream":
                checks += try assertMidi3Stream(fixture, file: file)
            case "cbor_stream":
                checks += try assertCborStream(fixture, file: file)
            default:
                XCTFail("\(file): unknown fixture kind \"\(kind)\"")
            }
        }
        // Guard against a fixture silently contributing no assertions.
        XCTAssertGreaterThanOrEqual(checks, fixtures.count, "some fixture asserted nothing")
    }

    // MARK: - kind: discovery

    @discardableResult
    private func assertDiscovery(_ fixture: [String: Any], file: String) throws -> Int {
        let raw = try bytes(fixture.string("raw"), file: file)
        let stream = try TagStream.parse(raw)
        guard let expected = fixture["expected"] as? [String: Any] else {
            XCTFail("\(file): no expected block")
            return 0
        }

        let header = stream.header.map { String(decoding: $0, as: UTF8.self) }
        XCTAssertEqual(header, expected.string("header"), "\(file): header")

        let actual = stream.keyValues().map { [$0.key, String(decoding: $0.value, as: UTF8.self)] }
        let want = (expected["key_values"] as? [[String]]) ?? []
        XCTAssertFalse(want.isEmpty, "\(file): key_values is empty")
        XCTAssertEqual(actual, want, "\(file): key_values")
        return 2
    }

    // MARK: - kind: midi3_stream

    @discardableResult
    private func assertMidi3Stream(_ fixture: [String: Any], file: String) throws -> Int {
        let raw = try bytes(fixture.string("raw"), file: file)
        var unframer = Midi3.Unframer()
        let messages = unframer.push(raw)
        guard let expected = fixture["expected"] as? [String: Any] else {
            XCTFail("\(file): no expected block")
            return 0
        }
        var checks = 0

        if let count = expected["message_count"] as? NSNumber {
            XCTAssertEqual(messages.count, count.intValue, "\(file): message_count")
            checks += 1
        }
        if let pending = expected["pending"] as? NSNumber {
            XCTAssertEqual(unframer.pending, pending.intValue, "\(file): pending")
            checks += 1
        }
        if let wanted = expected["messages"] as? [String] {
            XCTAssertEqual(messages.map { Fmt.hex($0) }, wanted, "\(file): messages")
            checks += 1
        }
        if let frames = expected["status_frames"] as? [[String: Any]] {
            XCTAssertFalse(frames.isEmpty, "\(file): status_frames is empty")
            assertStatusFrames(frames, messages: messages, file: file)
            checks += frames.count
        }
        if let histogram = expected["function_histogram"] as? [String: NSNumber] {
            assertFunctionHistogram(histogram, messages: messages, file: file)
            checks += 1
        }
        if let state = expected["state"] as? [String: Any] {
            assertDecodedState(state, messages: messages, file: file)
            checks += 1
        }
        return checks
    }

    /// Each entry names a message index whose realtime status block must decode
    /// to the eleven listed 14-bit values.
    private func assertStatusFrames(
        _ frames: [[String: Any]], messages: [[UInt8]], file: String
    ) {
        for frame in frames {
            let index = frame.int("index")
            guard index < messages.count else {
                XCTFail("\(file): status frame index \(index) beyond \(messages.count) messages")
                continue
            }
            var state = DeviceState()
            let outcome = state.apply(messages[index])
            let want = ((frame["raw"] as? [NSNumber]) ?? []).map(\.uint16Value)
            XCTAssertEqual(state.status.raw, want, "\(file): status frame \(index)")
            XCTAssertEqual(
                outcome.events, [.status(state.status)],
                "\(file): message \(index) should decode as a status frame"
            )
            XCTAssertFalse(outcome.slowChanged, "\(file): a status frame is FAST-lane only")
        }
    }

    /// The per-function message tally over the whole stream. Non-Kemper messages
    /// count under `"none"`.
    private func assertFunctionHistogram(
        _ expected: [String: NSNumber], messages: [[UInt8]], file: String
    ) {
        var actual: [String: Int] = [:]
        for message in messages {
            let key = NrpnHeader.parse(message).map { String($0.header.function) } ?? "none"
            actual[key, default: 0] += 1
        }
        XCTAssertEqual(actual, expected.mapValues(\.intValue), "\(file): function_histogram")
    }

    /// Apply every message to a fresh state and assert the named fields.
    private func assertDecodedState(_ expected: [String: Any], messages: [[UInt8]], file: String) {
        var state = DeviceState()
        for message in messages { state.apply(message) }
        if let rigName = expected["rig_name"] as? String {
            XCTAssertEqual(state.rig.name, rigName, "\(file): rig_name")
        }
        if let ampName = expected["amp_name"] as? String {
            XCTAssertEqual(state.amp.name, ampName, "\(file): amp_name")
        }
        if let cabName = expected["cab_name"] as? String {
            XCTAssertEqual(state.cabinet.name, cabName, "\(file): cab_name")
        }
    }

    // MARK: - kind: cbor_stream

    /// The selector of an opaque `[5, addr, bytes]` blob, which the walk ignores.
    private static let blobSelector: Int64 = 5

    @discardableResult
    private func assertCborStream(_ fixture: [String: Any], file: String) throws -> Int {
        let raw = try bytes(fixture.string("raw"), file: file)
        var decoder = CBORDecoder()
        let items = decoder.push(raw)
        let heads = items.map(itemHead)
        guard let expected = fixture["expected"] as? [String: Any] else {
            XCTFail("\(file): no expected block")
            return 0
        }
        var checks = 0

        if let count = expected["item_count"] as? NSNumber {
            XCTAssertEqual(items.count, count.intValue, "\(file): item_count")
            checks += 1
        }
        if let pending = expected["pending"] as? NSNumber {
            XCTAssertEqual(decoder.pending, pending.intValue, "\(file): pending")
            checks += 1
        }
        if let filler = expected["filler_bytes"] as? NSNumber {
            XCTAssertEqual(decoder.fillerBytes, filler.intValue, "\(file): filler_bytes")
            checks += 1
        }
        if let count = expected["numeric_count"] as? NSNumber {
            XCTAssertEqual(
                Cbor.numericValues(items).count, count.intValue, "\(file): numeric_count")
            checks += 1
        }
        if let strings = expected["strings"] as? [[Any]] {
            let want = strings.map {
                SnapshotString(address: ($0[0] as! NSNumber).uint32Value, text: $0[1] as! String)
            }
            XCTAssertEqual(Cbor.extractSnapshot(items).strings, want, "\(file): strings")
            checks += 1
        }
        if let count = expected["blob_count"] as? NSNumber {
            let blobs = zip(items, heads).filter { $0.1?.selector == Self.blobSelector }.map(\.0)
            XCTAssertEqual(blobs.count, count.intValue, "\(file): blob_count")
            // A blob is opaque to the walk: it yields nothing.
            for blob in blobs {
                XCTAssertNil(Cbor.controlItem(blob), "\(file): a blob yielded values")
            }
            checks += 1
        }
        if let live = expected["live_items"] as? [String: NSNumber] {
            for (address, count) in live {
                let want = (selector: Generated.cborSelectorSingle, address: UInt32(address)!)
                let got = heads.filter { $0.map { $0 == want } ?? false }.count
                XCTAssertEqual(got, count.intValue, "\(file): live items at \(address)")
            }
            checks += 1
        }
        if let index = expected["dump_end_index"] as? NSNumber {
            let end = (selector: Generated.cborSelectorMulti, address: Generated.dumpEndAddress)
            let got = heads.lastIndex { $0.map { $0 == end } ?? false }
            XCTAssertEqual(got, index.intValue, "\(file): dump_end_index")
            checks += 1
        }
        if let state = expected["state"] as? [String: Any] {
            assertCborState(state, items: items, file: file)
            checks += 1
        }
        return checks
    }

    /// The `(selector, address)` an item names, a leading source flag skipped;
    /// `nil` for anything that is not one of the channel's array shapes.
    private func itemHead(_ item: CBORValue) -> (selector: Int64, address: UInt32)? {
        guard let fields = item.asArray else { return nil }
        let rest: ArraySlice<CBORValue>
        if let first = fields.first?.asInt, first < 0 {
            rest = fields.dropFirst()
        } else {
            rest = fields[...]
        }
        guard let selector = rest.first?.asInt, rest.count > 1,
            let raw = rest[rest.startIndex + 1].asInt, let address = UInt32(exactly: raw)
        else { return nil }
        return (selector, address)
    }

    /// Fold every item into a fresh state through the control path, in document
    /// order, and assert the named fields.
    private func assertCborState(_ expected: [String: Any], items: [CBORValue], file: String) {
        var state = DeviceState()
        for entry in items.compactMap(Cbor.controlItem).flatMap(\.entries) {
            switch entry {
            case let .num(address, value):
                state.applyCbor(address: address, value: value)
            case let .text(address, text):
                state.applyCborText(address: address, text: text)
            }
        }
        if let rigName = expected["rig_name"] as? String {
            XCTAssertEqual(state.rig.name, rigName, "\(file): rig_name")
        }
        if let ampName = expected["amp_name"] as? String {
            XCTAssertEqual(state.amp.name, ampName, "\(file): amp_name")
        }
        if let cabName = expected["cab_name"] as? String {
            XCTAssertEqual(state.cabinet.name, cabName, "\(file): cab_name")
        }
        if let bank = expected["current_bank"] as? NSNumber {
            XCTAssertEqual(state.currentBank, bank.uint16Value, "\(file): current_bank")
        }
        if let slot = expected["current_rig_slot"] as? NSNumber {
            XCTAssertEqual(state.currentRigSlot, slot.uint16Value, "\(file): current_rig_slot")
        }
        if let morph = expected["morph"] as? NSNumber {
            XCTAssertEqual(state.morph, morph.uint16Value, "\(file): morph")
        }
        if let slots = expected["bank"] as? [[String: Any]] {
            XCTAssertEqual(slots.count, Generated.bankSlots, "\(file): bank slots")
            for (i, slot) in slots.enumerated() {
                let got = state.bank.slots[i]
                XCTAssertEqual(
                    got.rigName, slot.optionalString("rig_name"), "\(file): bank slot \(i) rig_name"
                )
                XCTAssertEqual(
                    got.ampName, slot.optionalString("amp_name"), "\(file): bank slot \(i) amp_name"
                )
                XCTAssertEqual(
                    got.cabinetName, slot.optionalString("cab_name"),
                    "\(file): bank slot \(i) cab_name")
            }
        }
        if let raw = expected["status_raw"] as? [NSNumber] {
            XCTAssertEqual(state.status.raw, raw.map(\.uint16Value), "\(file): status_raw")
        }
    }

    // MARK: - Helpers

    private func bytes(_ hex: String, file: String) throws -> [UInt8] {
        guard let decoded = Fmt.bytes(fromHex: hex) else {
            throw Fixtures.Failure("\(file): raw is not valid hex")
        }
        return decoded
    }
}
