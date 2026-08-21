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

    // MARK: - Helpers

    private func bytes(_ hex: String, file: String) throws -> [UInt8] {
        guard let decoded = Fmt.bytes(fromHex: hex) else {
            throw Fixtures.Failure("\(file): raw is not valid hex")
        }
        return decoded
    }
}
