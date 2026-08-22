import Foundation
import XCTest

@testable import LibKP

// MARK: - TagStream / discovery poll

final class ProtocolTests: XCTestCase {
    func testPollRequestBytes() {
        let expected: [UInt8] = [
            0x44, 0x53, 0x43, 0x56,  // DSCV
            0x16, 0x4D, 0x41, 0x43, 0x23, 0x30, 0x30, 0x3A, 0x30, 0x30, 0x3A, 0x30,
            0x30, 0x3A, 0x30, 0x30, 0x3A, 0x30, 0x30, 0x3A, 0x30, 0x30,  // "MAC#00:…"
            0x07, 0x50, 0x4F, 0x4C, 0x4C, 0x3A, 0x29,  // "POLL:)"
            0x00,
        ]
        let built = KemperProtocol.buildPollRequest(mac: "00:00:00:00:00:00")
        XCTAssertEqual(built, expected)
        XCTAssertEqual(built.count, 34)
    }

    func testRoundTripParseRequest() throws {
        let packet = KemperProtocol.buildPollRequest()
        let stream = try TagStream.parse(packet)
        XCTAssertEqual(stream.header, Array("DSCV".utf8))
        XCTAssertEqual(stream.fields.count, 2)
        XCTAssertEqual(stream.fields[0], Array("MAC#00:00:00:00:00:00".utf8))
        XCTAssertEqual(stream.fields[1], Array("POLL:)".utf8))
    }

    func testEmptyPayloadIsRejected() {
        XCTAssertThrowsError(try TagStream.parse([])) { error in
            XCTAssertEqual(error as? ParseError, .tooShort(need: 1, got: 0))
        }
    }

    func testFieldOverrunIsRejected() {
        // "DSCV" then a field claiming 16 bytes with only 2 present.
        let packet: [UInt8] = [0x44, 0x53, 0x43, 0x56, 0x10, 0x41, 0x42]
        XCTAssertThrowsError(try TagStream.parse(packet))
    }

    func testKeyValuesSplitsFourCharKeys() throws {
        var packet = Array("DSCV".utf8)
        let field = Array("NAMETest Profiler".utf8)
        packet.append(UInt8(field.count + 1))
        packet.append(contentsOf: field)
        packet.append(0x00)
        let stream = try TagStream.parse(packet)
        XCTAssertEqual(stream.stringValue(forKey: "NAME"), "Test Profiler")
    }

    func testPollDetection() {
        XCTAssertTrue(ReplyCollector.isPoll(KemperProtocol.buildPollRequest()))
        var reply = Array("DSCV".utf8)
        let field = Array("NAMEx".utf8)
        reply.append(UInt8(field.count + 1))
        reply.append(contentsOf: field)
        reply.append(0x00)
        XCTAssertFalse(ReplyCollector.isPoll(reply))
    }
}

// MARK: - Exclusive ownership of the discovery port

final class DiscoveryPortTests: XCTestCase {
    /// A second acquire must fail rather than quietly share the port. Sharing is
    /// the failure this guards against: the kernel gives an arriving reply to
    /// exactly one bound socket, so a co-bound listener steals replies instead of
    /// duplicating them.
    func testThePortIsHeldExclusively() throws {
        let first = try DiscoveryPort(port: 54331)
        defer { first.close() }
        XCTAssertEqual(first.port, 54331)

        XCTAssertThrowsError(try DiscoveryPort(port: 54331)) { error in
            guard case let DiscoverError.portUnavailable(port) = error else {
                return XCTFail("expected portUnavailable, got \(error)")
            }
            XCTAssertEqual(port, 54331)
            XCTAssertTrue("\(error)".contains("exclusive"))
        }
    }

    func testReleasingThePortLetsItBeAcquiredAgain() throws {
        try DiscoveryPort(port: 54332).close()
        try DiscoveryPort(port: 54332).close()  // no leak: the first release freed it
    }

    func testClosingAPortTwiceIsHarmless() throws {
        let port = try DiscoveryPort(port: 54333)
        port.close()
        port.close()
    }

    /// A long-running client re-polls to notice devices coming and going, without
    /// ever releasing the port in between.
    func testAHeldPortCanBePolledRepeatedly() async throws {
        let port = try DiscoveryPort(port: 54334)
        defer { port.close() }
        var options = DiscoveryOptions()
        options.listenFor = 0.1
        options.repeatEvery = 0.05
        options.extraTargets = ["127.0.0.1"]

        // Our own echoed poll must not be mistaken for a device.
        for _ in 0..<2 {
            let replies = try await port.poll(options)
            XCTAssertTrue(replies.isEmpty)
        }
    }

    /// The one-shot helper must not leave the port held behind it.
    func testDiscoverReleasesThePortItAcquired() async throws {
        var options = DiscoveryOptions()
        options.listenFor = 0.1
        options.repeatEvery = 0.05
        _ = try await Discovery.discover(options)
        // Would throw if `discover` leaked the standard port.
        try DiscoveryPort().close()
    }
}

// MARK: - MIDI3 framing

final class Midi3Tests: XCTestCase {
    func testUnframesRealSysEx() {
        let raw: [UInt8] = [
            0x14, 0xF0, 0x00, 0x20, 0x14, 0x33, 0x02, 0x00, 0x14, 0x06, 0x00, 0x00,
            0x14, 0x00, 0x06, 0x20, 0x14, 0x05, 0x00, 0x00, 0x14, 0x00, 0x00, 0x10,
            0x15, 0xF7, 0x00, 0x00,
        ]
        var unframer = Midi3.Unframer()
        let messages = unframer.push(raw)
        XCTAssertEqual(messages.count, 1)
        XCTAssertEqual(
            messages[0],
            [
                0xF0, 0x00, 0x20, 0x33, 0x02, 0x00, 0x06, 0x00, 0x00, 0x00,
                0x06, 0x20, 0x05, 0x00, 0x00, 0x00, 0x00, 0x10, 0xF7,
            ])
        XCTAssertTrue(Midi3.isKemperSysEx(messages[0]))
        XCTAssertEqual(unframer.pending, 0)
    }

    func testFrameUnframeRoundTrip() {
        let messages: [[UInt8]] = [
            [0xF0, 0x00, 0x20, 0x33, 0x02, 0x7F, 0x7E, 0x00, 0x40, 0x02, 0x23, 0x0F, 0xF7],
            [0xF0, 0x00, 0x20, 0x33, 0xF7],
            [0xF0, 0x00, 0x20, 0x33, 0x01, 0xF7],
        ]
        for message in messages {
            let framed = Midi3.frame(message)
            XCTAssertEqual(framed.count % 4, 0)
            var unframer = Midi3.Unframer()
            XCTAssertEqual(unframer.push(framed), [message])
            XCTAssertEqual(unframer.pending, 0)
        }
    }

    func testReassemblesAcrossChunkBoundaries() {
        let raw: [UInt8] = [
            0x14, 0xF0, 0x00, 0x20, 0x14, 0x33, 0x02, 0x00, 0x17, 0x01, 0x02, 0xF7,
        ]
        var unframer = Midi3.Unframer()
        XCTAssertTrue(unframer.push(Array(raw[0..<5])).isEmpty)
        let messages = unframer.push(Array(raw[5...]))
        XCTAssertEqual(messages, [[0xF0, 0x00, 0x20, 0x33, 0x02, 0x00, 0x01, 0x02, 0xF7]])
    }

    func testUnknownTagResyncs() {
        // A stray byte before a valid 1-byte final frame.
        var unframer = Midi3.Unframer()
        let messages = unframer.push([0xAA, 0x15, 0xF7, 0x00, 0x00])
        XCTAssertEqual(messages, [[0xF7]])
        XCTAssertEqual(unframer.pending, 0)
    }
}

// MARK: - NRPN builders and parsers

final class NrpnTests: XCTestCase {
    func testInitBeacon() {
        // init=1, sysex=1, tuner=1 → flags 0x23; lease 30 s → 15 (0x0F).
        let beacon = Nrpn.beacon(
            init: true, tuner: true, leaseSecs: 30, paramSet: 0x02,
            product: Generated.productPlayer)
        XCTAssertEqual(
            beacon,
            [
                0xF0, 0x00, 0x20, 0x33, 0x02, 0x7F, 0x7E, 0x00, 0x40, 0x02, 0x23, 0x0F, 0xF7,
            ])
    }

    func testBuildsRigNameRequest() {
        let message = Nrpn.requestString(
            product: 0x00, device: 0x7F,
            page: Generated.pageStrings, number: Generated.stringRigName
        )
        XCTAssertEqual(message, [0xF0, 0x00, 0x20, 0x33, 0x00, 0x7F, 0x43, 0x00, 0x00, 0x01, 0xF7])
        let parsed = NrpnHeader.parse(message)
        XCTAssertEqual(parsed?.header.function, 0x43)
        XCTAssertEqual(parsed?.header.page, 0x00)
        XCTAssertEqual(parsed?.header.number, 0x01)
        XCTAssertEqual(parsed?.values, [])
        XCTAssertEqual(parsed?.header.functionName, "request-string")
    }

    func testBuildsEffectStateSet() {
        let on = Nrpn.setSingle(product: 0x00, device: 0x7F, page: 0x3D, number: 0x03, value: 1)
        XCTAssertEqual(
            on,
            [
                0xF0, 0x00, 0x20, 0x33, 0x00, 0x7F, 0x01, 0x00, 0x3D, 0x03, 0x00, 0x01, 0xF7,
            ])
        let off = Nrpn.setSingle(product: 0x00, device: 0x7F, page: 0x3D, number: 0x03, value: 0)
        XCTAssertEqual(Array(off[10..<12]), [0x00, 0x00])
    }

    func testU14RoundTrip() {
        for value: UInt16 in [0, 1, 6925, 8192, 16383] {
            let (msb, lsb) = Nrpn.u14Split(value)
            XCTAssertEqual(Nrpn.u14(msb, lsb), value)
        }
    }

    func testControlChangeBytes() {
        XCTAssertEqual(Nrpn.controlChange(channel: 0, controller: 50, value: 1), [0xB0, 50, 1])
        XCTAssertEqual(Nrpn.controlChange(channel: 15, controller: 47, value: 0), [0xBF, 47, 0])
    }

    func testBuildsRequestMulti() {
        let message = Nrpn.requestMulti(product: 0x02, device: 0x7F, page: 0x34, number: 0x00)
        XCTAssertEqual(message, [0xF0, 0x00, 0x20, 0x33, 0x02, 0x7F, 0x42, 0x00, 0x34, 0x00, 0xF7])
    }

    func testBuildsRequestRenderedString() {
        let message = Nrpn.requestRenderedString(
            product: 0x02, device: 0x7F, page: 0x3C, number: 53, value: 8192
        )
        XCTAssertEqual(
            message,
            [
                0xF0, 0x00, 0x20, 0x33, 0x02, 0x7F, 0x7C, 0x00, 0x3C, 53, 0x40, 0x00, 0xF7,
            ])
    }

    func testParsesRenderedStringReply() {
        let (msb, lsb) = Nrpn.u14Split(8192)
        let message = Nrpn.sysex(
            product: Generated.productPlayer, device: Generated.deviceOmni,
            function: Generated.fnRenderedStringReply, page: 0x3C, number: 53,
            values: [msb, lsb] + Array("<0.0>".utf8) + [0]
        )
        let parsed = Nrpn.parseRenderedString(message)
        XCTAssertEqual(parsed?.page, 0x3C)
        XCTAssertEqual(parsed?.number, 53)
        XCTAssertEqual(parsed?.value, 8192)
        XCTAssertEqual(parsed?.text, "<0.0>")

        // A reply without the value pair is rejected.
        let short = Nrpn.sysex(
            product: Generated.productPlayer, device: Generated.deviceOmni,
            function: Generated.fnRenderedStringReply, page: 0x3C, number: 53, values: [0x40]
        )
        XCTAssertNil(Nrpn.parseRenderedString(short))

        // A wrong function is rejected.
        let wrong = Nrpn.setSingle(product: 0x00, device: 0x7F, page: 0x3C, number: 53, value: 8192)
        XCTAssertNil(Nrpn.parseRenderedString(wrong))
    }

    func testMultiValuesDecodesConsecutiveBlock() {
        let (m0, l0) = Nrpn.u14Split(8192)
        let (m2, l2) = Nrpn.u14Split(16383)
        let pairs = Nrpn.multiValues(number: 4, values: [m0, l0, 0x00, 0x00, m2, l2])
        XCTAssertEqual(pairs.map(\.number), [4, 5, 6])
        XCTAssertEqual(pairs.map(\.value), [8192, 0, 16383])
        // A trailing odd byte is ignored.
        let odd = Nrpn.multiValues(number: 0, values: [0x01, 0x02, 0x7F])
        XCTAssertEqual(odd.count, 1)
        XCTAssertEqual(odd[0].value, Nrpn.u14(1, 2))
    }

    func testExtDecodeRoundTrips() {
        for value: UInt64 in [0, 1, 6925, 16383, 0x1_2345_6789] {
            let encoded = Nrpn.extEncode(value, count: 5)
            XCTAssertEqual(Nrpn.extDecode(encoded), value)
            if value <= 16383 {
                let (msb, lsb) = Nrpn.u14Split(UInt16(value))
                XCTAssertEqual(Nrpn.extDecode([msb, lsb]), value)
            }
        }
    }

    func testRequestExtendedString() {
        // Bank Preview page 0x96, rig-name slot 1: flat address 0x96 * 128 = 19200.
        let address = Params.bankPreviewAddress(.rigName, slot: 1)
        XCTAssertEqual(address, 19200)
        let message = Nrpn.requestExtendedString(
            product: Generated.productProfiler, device: Generated.deviceOmni, address: address)
        XCTAssertEqual(
            message,
            [0xF0, 0x00, 0x20, 0x33, 0x00, 0x7F, 0x47, 0x00, 0x00, 0x00, 0x01, 0x16, 0x00, 0xF7])
        XCTAssertEqual(UInt32(Nrpn.extDecode(Array(message[8..<13]))), address)
    }

    func testParseExtendedString() {
        var message: [UInt8] = [0xF0, 0x00, 0x20, 0x33, 0x02, 0x00, 0x07, 0x00]
        message += Nrpn.extEncode(1, count: 5)
        message += Array("AC30".utf8)
        message += [0x00, 0xF7]
        let parsed = Nrpn.parseExtendedString(message)
        XCTAssertEqual(parsed?.address, 1)
        XCTAssertEqual(parsed?.text, "AC30")

        // A normal-page address encodes page * 128 + number.
        var amp: [UInt8] = [0xF0, 0x00, 0x20, 0x33, 0x02, 0x00, 0x07, 0x00]
        amp += Nrpn.extEncode(10 * 128, count: 5)
        amp += Array("JCM800".utf8)
        amp += [0x00, 0xF7]
        let parsedAmp = Nrpn.parseExtendedString(amp)
        let ampAddress = parsedAmp?.address ?? 0
        XCTAssertEqual(ampAddress, 1280)
        XCTAssertEqual(ampAddress / 128, 10)
        XCTAssertEqual(ampAddress % 128, 0)
        XCTAssertEqual(parsedAmp?.text, "JCM800")

        // A wrong function is rejected.
        XCTAssertNil(
            Nrpn.parseExtendedString(
                Nrpn.setSingle(product: 0x00, device: 0x7F, page: 0x00, number: 0x01, value: 1)
            ))
    }

    func testParsesStatusHeader() {
        let message: [UInt8] = [
            0xF0, 0x00, 0x20, 0x33, 0x00, 0x00, 0x02, 0x00, 0x7C, 0x4E, 0x00, 0x00, 0xF7,
        ]
        let parsed = NrpnHeader.parse(message)
        XCTAssertEqual(parsed?.header.function, 0x02)
        XCTAssertEqual(parsed?.header.page, 0x7C)
        XCTAssertEqual(parsed?.header.number, 0x4E)
        XCTAssertEqual(parsed?.values, [0x00, 0x00])
    }

    func testNonKemperMessageHasNoHeader() {
        XCTAssertNil(NrpnHeader.parse([0xB0, 0x20, 0x01]))
        XCTAssertNil(NrpnHeader.parse([]))
    }
}

// MARK: - Control vocabulary

final class ControlTests: XCTestCase {
    func testContinuousControllerBytes() {
        XCTAssertEqual(Control.gain(64).message(), [0xB0, 72, 64])
        XCTAssertEqual(Control.delayMix(10).message(), [0xB0, 68, 10])
        XCTAssertEqual(Control.reverbTime(127).message(), [0xB0, 71, 127])
        XCTAssertEqual(Control.morphPedal(0).message(), [0xB0, 11, 0])
    }

    func testMomentaryActions() {
        XCTAssertEqual(Control.tapTempo.message(), [0xB0, 30, 1])
        XCTAssertEqual(Control.toggleAllModules.message(), [0xB0, 16, 1])
        XCTAssertEqual(Control.up.message(), [0xB0, 48, 1, 0xB0, 48, 0])
        XCTAssertEqual(Control.down.message(), [0xB0, 49, 1, 0xB0, 49, 0])
    }

    func testSwitchVariantsEmitOneOrZero() {
        XCTAssertEqual(Control.tunerMode(true).message(), [0xB0, 31, 1])
        XCTAssertEqual(Control.tunerMode(false).message(), [0xB0, 31, 0])
        XCTAssertEqual(Control.rotaryFast(true).message(), [0xB0, 33, 1])
        XCTAssertEqual(Control.rotaryFast(false).message(), [0xB0, 33, 0])
        XCTAssertEqual(Control.delayInfinity(true).message(), [0xB0, 34, 1])
        XCTAssertEqual(Control.freeze(true).message(), [0xB0, 35, 1])
        XCTAssertEqual(Control.morphButton(true).message(), [0xB0, 80, 1])
        XCTAssertEqual(Control.morphButton(false).message(), [0xB0, 80, 0])
    }

    func testLoadSlotClampsIntoRange() {
        XCTAssertEqual(Control.loadSlot(3).message(), [0xB0, 52, 1, 0xB0, 52, 0])
        XCTAssertEqual(Control.loadSlot(1).message(), [0xB0, 50, 1, 0xB0, 50, 0])
        XCTAssertEqual(Control.loadSlot(5).message(), [0xB0, 54, 1, 0xB0, 54, 0])
        XCTAssertEqual(Control.loadSlot(0).message(), [0xB0, 50, 1, 0xB0, 50, 0])
        XCTAssertEqual(Control.loadSlot(99).message(), [0xB0, 54, 1, 0xB0, 54, 0])
    }

    func testEffectButtonClampsIntoRange() {
        XCTAssertEqual(Control.effectButton(4).message(), [0xB0, 78, 1])
        XCTAssertEqual(Control.effectButton(1).message(), [0xB0, 75, 1])
        XCTAssertEqual(Control.effectButton(0).message(), [0xB0, 75, 1])
        XCTAssertEqual(Control.effectButton(9).message(), [0xB0, 78, 1])
    }

    func testSlotEnableUsesSpilloverCCForDlyRev() {
        XCTAssertEqual(Control.slotEnable(slot: .rev, on: true).message(), [0xB0, 29, 1])
        XCTAssertEqual(Control.slotEnable(slot: .dly, on: false).message(), [0xB0, 27, 0])
        XCTAssertEqual(Control.slotEnable(slot: .a, on: true).message(), [0xB0, 17, 1])
        XCTAssertEqual(Control.slotEnable(slot: .x, on: true).message(), [0xB0, 22, 1])
        XCTAssertEqual(Control.slotEnable(slot: .mod, on: false).message(), [0xB0, 24, 0])
    }

    func testSlotEnableCCTable() {
        let expected: [(ModuleSlot, UInt8)] = [
            (.a, 17), (.b, 18), (.c, 19), (.d, 20), (.x, 22), (.mod, 24), (.dly, 27), (.rev, 29),
        ]
        for (slot, cc) in expected {
            XCTAssertEqual(slotEnableCC(slot), cc, "\(slot.name)")
        }
    }

    func testModuleSlotLookupIsCaseInsensitive() {
        XCTAssertEqual(ModuleSlot(name: "rev"), .rev)
        XCTAssertEqual(ModuleSlot(name: "Dly"), .dly)
        XCTAssertNil(ModuleSlot(name: "nope"))
        XCTAssertEqual(ModuleSlot.a.page, 0x32)
        XCTAssertEqual(ModuleSlot.rev.page, 0x3D)
    }

    func testProgramChangeAndBankSelect() {
        XCTAssertEqual(Control.programChange(5).message(), [0xC0, 5])
        XCTAssertEqual(programChange(channel: 2, program: 0), [0xC2, 0])
        XCTAssertEqual(Control.bankSelect(msb: 0, lsb: 3).message(), [0xB0, 0, 0, 0xB0, 32, 3])
        XCTAssertEqual(Control.bankPreselect(3).message(), [0xB0, 47, 3])
    }

    func testChannelAndValuesAreMasked() {
        XCTAssertEqual(Control.gain(64).message(channel: 15), [0xBF, 72, 64])
        XCTAssertEqual(Control.gain(64).message(channel: 16), [0xB0, 72, 64])
        XCTAssertEqual(Control.programChange(0).message(channel: 15), [0xCF, 0])
        XCTAssertEqual(Control.wahPedal(200).message(), [0xB0, 1, 72])
        XCTAssertEqual(Control.programChange(200).message(), [0xC0, 72])
        XCTAssertEqual(Control.bankSelect(msb: 130, lsb: 129).message(), [0xB0, 0, 2, 0xB0, 32, 1])
    }
}

// MARK: - Name lookups

final class ParamsTests: XCTestCase {
    func testKnownAddresses() {
        XCTAssertEqual(Params.paramName(page: 0x09, number: 0x03), "Noise Gate Intensity")
        XCTAssertEqual(
            Params.describe(page: 0x09, number: 0x03), "Input Section: Noise Gate Intensity")
        XCTAssertEqual(Params.paramName(page: 0x04, number: 0), "Tempo bpm")
        XCTAssertEqual(Params.paramName(page: 0x0A, number: 4), "Gain")
        XCTAssertEqual(Params.paramName(page: 0x7F, number: 0), "Main Output Volume")
        XCTAssertEqual(Params.paramName(page: 0x7D, number: 88), "Looper Record/Playback/Overdub")
        XCTAssertEqual(Params.paramName(page: 0x00, number: 1), "Rig Name")
        XCTAssertNil(Params.paramName(page: 0x04, number: 5))
        XCTAssertNil(Params.paramName(page: 0x7D, number: 112))
    }

    func testEffectModulesShareTheMap() {
        for page: UInt8 in [0x32, 0x33, 0x34, 0x35, 0x38, 0x3A, 0x3C, 0x3D] {
            XCTAssertTrue(Params.isEffectPage(page))
            XCTAssertEqual(Params.paramName(page: page, number: 4), "Mix")
            XCTAssertEqual(Params.paramName(page: page, number: 0), "Type")
        }
        XCTAssertFalse(Params.isEffectPage(0x04))
    }

    func testBankPreviewAddressesAndNames() {
        XCTAssertEqual(Params.pageName(0x96), "Bank Preview")
        XCTAssertEqual(Params.paramName(page: 0x96, number: 0), "Bank Rig Name")
        XCTAssertEqual(Params.paramName(page: 0x96, number: 7), "Bank Amp Name")
        XCTAssertEqual(Params.paramName(page: 0x96, number: 14), "Bank Cabinet Name")
        XCTAssertNil(Params.paramName(page: 0x96, number: 15))
        XCTAssertEqual(Params.bankPreviewAddress(.rigName, slot: 1), 19200)
        XCTAssertEqual(Params.bankPreviewAddress(.cabinetName, slot: 5), 19214)
        // Out-of-range slots clamp into 1...bankSlots.
        XCTAssertEqual(Params.bankPreviewAddress(.ampName, slot: 0), 19205)
        XCTAssertEqual(Params.bankPreviewAddress(.ampName, slot: 9), 19209)
        // The reverse map recovers (field, 0-based slot).
        XCTAssertTrue(Params.bankPreviewSlotField(3).map { $0.0 == .rigName && $0.1 == 3 } ?? false)
        XCTAssertTrue(Params.bankPreviewSlotField(9).map { $0.0 == .ampName && $0.1 == 4 } ?? false)
        XCTAssertTrue(
            Params.bankPreviewSlotField(10).map { $0.0 == .cabinetName && $0.1 == 0 } ?? false)
        XCTAssertNil(Params.bankPreviewSlotField(15))
    }

    func testEffectTypes() {
        XCTAssertEqual(Params.effectTypeName(32), "Kemper Drive")
        XCTAssertEqual(Params.effectTypeName(193), "Spring Reverb")
        XCTAssertNil(Params.effectTypeName(5))
    }

    func testEffectCategories() {
        XCTAssertEqual(Params.effectCategoryName(16), "Wah")
        XCTAssertEqual(Params.effectCategoryName(17), "Shaper")
        // A type with no name still resolves to its block.
        XCTAssertEqual(Params.effectCategoryName(76), "Modulation")
        XCTAssertNil(Params.effectCategoryName(0))
        XCTAssertNil(Params.effectCategoryName(300))
    }

    func testRealtimePageAddresses() {
        XCTAssertEqual(Params.pageName(0x7C), "Realtime/Meters")
        XCTAssertEqual(
            Params.paramName(page: 0x7C, number: 0x4E), "Tuner Strobe Segment (phase-low)")
        XCTAssertEqual(Params.paramName(page: 0x7C, number: 81), "Tuner Strobe Phase")
        XCTAssertEqual(Params.paramName(page: 0x7C, number: 84), "Meter: Rig Output Level")
        XCTAssertEqual(Params.paramName(page: 0x7C, number: 88), "Meter: (unused v10)")
        XCTAssertEqual(Params.paramName(page: 0x7C, number: 0), "Tempo/Beat Pulse")
        XCTAssertEqual(Params.paramName(page: 0x7C, number: 15), "Tuner Deviance")
        XCTAssertEqual(
            Params.describe(page: 0x7C, number: 0x4E),
            "Realtime/Meters: Tuner Strobe Segment (phase-low)"
        )
    }

    func testDualUsePageZero() {
        XCTAssertEqual(Params.paramName(page: 0x05, number: 6), "Fixed Noise Gate On/Off")
        XCTAssertEqual(Params.paramName(page: 0x7D, number: 84), "Tuner Note")
        XCTAssertEqual(Params.paramName(page: 0x7F, number: 126), "Tuner Mode State")
        XCTAssertEqual(Params.page0NumericName(0x0B), "Morph State")
        XCTAssertEqual(Params.describeNumeric(page: 0x00, number: 0x0B), "Page 0: Morph State")
        XCTAssertEqual(Params.describe(page: 0x00, number: 0x0B), "String Tags: Amp Author")
    }

    func testUnknownPageFallsBackToHex() {
        XCTAssertEqual(Params.describe(page: 0x99, number: 5), "page 0x99 #5 (0x05)")
        XCTAssertEqual(Params.describe(page: 0x04, number: 5), "Rig Settings: #5 (0x05)")
    }

    func testEffectSlotTable() {
        XCTAssertEqual(Params.effectSlots.count, 8)
        XCTAssertEqual(Params.effectSlots.first?.name, "A")
        XCTAssertEqual(Params.effectSlots.last?.page, 0x3D)
        XCTAssertEqual(Params.effectSlotPage("rev"), 0x3D)
        XCTAssertEqual(Params.effectSlotIndex(page: 0x3D), 7)
        XCTAssertEqual(Params.effectSlotName(page: 0x38), "X")
        XCTAssertNil(Params.effectSlotPage("nope"))
    }
}

// MARK: - Typed descriptors

final class RegistryTests: XCTestCase {
    func testKnownAddressesResolve() {
        let gain = Registry.descriptor(page: 0x0A, number: 4)
        XCTAssertEqual(gain?.name, "Gain")
        XCTAssertEqual(gain?.kind, .continuous)

        let ampOnOff = Registry.descriptor(page: 0x0A, number: 2)
        XCTAssertEqual(ampOnOff?.name, "On/Off")
        XCTAssertEqual(ampOnOff?.kind, .switch)

        XCTAssertEqual(Registry.descriptor(page: 0x04, number: 1)?.kind, .continuous)
        XCTAssertEqual(Registry.descriptor(page: 0x04, number: 2)?.kind, .switch)
        XCTAssertEqual(Registry.descriptor(page: 0x7F, number: 0)?.name, "Main Output Volume")
    }

    func testEffectSlotParamsOnlyCommonFour() {
        let type = Registry.descriptor(page: 0x3D, number: 0)
        XCTAssertEqual(type?.name, "Type")
        XCTAssertEqual(type?.kind, .enumerated)
        XCTAssertEqual(Registry.descriptor(page: 0x3D, number: 3)?.kind, .switch)
        XCTAssertEqual(Registry.descriptor(page: 0x3D, number: 4)?.name, "Mix")
        XCTAssertEqual(Registry.descriptor(page: 0x3D, number: 6)?.name, "Volume")
        XCTAssertNil(Registry.descriptor(page: 0x3D, number: 7))
        XCTAssertNil(Registry.descriptor(page: 0x7C, number: 84))
    }

    func testFormatValue() throws {
        let onOff = try XCTUnwrap(Registry.descriptor(page: 0x0A, number: 2))
        XCTAssertEqual(Registry.formatValue(onOff, 1), "On")
        XCTAssertEqual(Registry.formatValue(onOff, 0), "Off")

        let type = try XCTUnwrap(Registry.descriptor(page: 0x32, number: 0))
        XCTAssertEqual(Registry.formatValue(type, 32), "Kemper Drive")
        XCTAssertEqual(Registry.formatValue(type, 5), "type 5")

        let continuous = try XCTUnwrap(Registry.descriptor(page: 0x0A, number: 4))
        XCTAssertEqual(Registry.formatValue(continuous, 6925), "42.3%")
        XCTAssertEqual(Registry.formatValue(continuous, 0), "0.0%")
        XCTAssertEqual(Registry.formatValue(continuous, 16383), "100.0%")
    }
}

// MARK: - The state tree and its decode routing

final class StateTests: XCTestCase {
    private func meterBlock(_ values: [UInt16]) -> [UInt8] {
        values.flatMap { value -> [UInt8] in
            let (msb, lsb) = Nrpn.u14Split(value)
            return [msb, lsb]
        }
    }

    private func extString(page: UInt8, number: UInt8, text: String) -> [UInt8] {
        let address = UInt64(page) * 128 + UInt64(number)
        var message: [UInt8] = [
            0xF0, 0x00, 0x20, 0x33, 0x00, 0x00, Generated.fnExtStringParam, 0x00,
        ]
        message += Nrpn.extEncode(address, count: 5)
        message += Array(text.utf8)
        message += [0x00, 0xF7]
        return message
    }

    func testNewStateSeedsEightSlotsInOrder() {
        let state = DeviceState()
        XCTAssertEqual(state.connection, .disconnected)
        XCTAssertEqual(state.effects.count, 8)
        XCTAssertEqual(state.effects[0].slot, "A")
        XCTAssertEqual(state.effects[0].page, 0x32)
        XCTAssertEqual(state.effects[7].slot, "REV")
        XCTAssertEqual(state.effects[7].page, 0x3D)
        XCTAssertTrue(state.effects.allSatisfy { $0.kind == nil && $0.on == nil && $0.mix == nil })
        XCTAssertEqual(state, DeviceState())
    }

    func testEffectLookupIsCaseInsensitive() {
        let state = DeviceState()
        XCTAssertEqual(state.effect("rev")?.slot, "REV")
        XCTAssertEqual(state.effect("A")?.page, 0x32)
        XCTAssertNil(state.effect("nope"))
    }

    func testEffectTypeNameAndEmpty() {
        var effect = DeviceState().effects[7]
        XCTAssertNil(effect.typeName)
        effect.kind = 0
        XCTAssertTrue(effect.isEmpty)
        effect.kind = 179
        XCTAssertFalse(effect.isEmpty)
        XCTAssertEqual(effect.typeName, "Easy Reverb")
    }

    func testExtStringPopulatesBankPreview() {
        var state = DeviceState()
        // Page 0x96: number 0 = slot 1 rig, 5 = slot 1 amp, 12 = slot 3 cabinet.
        let outcome = state.apply(extString(page: 0x96, number: 0, text: "AC30"))
        XCTAssertTrue(outcome.slowChanged)
        XCTAssertEqual(outcome.events, [.bankPreview(number: 0)])
        state.apply(extString(page: 0x96, number: 5, text: "Vox AC30TB"))
        state.apply(extString(page: 0x96, number: 12, text: "2x12"))
        XCTAssertEqual(state.bank.slots[0].rigName, "AC30")
        XCTAssertEqual(state.bank.slots[0].ampName, "Vox AC30TB")
        XCTAssertEqual(state.bank.slots[2].cabinetName, "2x12")
        // An out-of-range bank number is ignored.
        let ignored = state.apply(extString(page: 0x96, number: 15, text: "x"))
        XCTAssertEqual(ignored, ApplyOutcome())
    }

    func testHeadphoneVolumeFeedsMaster() {
        var state = DeviceState()
        XCTAssertNil(state.output.masterVolume)
        // Monitor alone answers for master until headphone is seen.
        state.apply(Nrpn.setSingle(product: 0, device: 0, page: 0x7F, number: 2, value: 5000))
        XCTAssertEqual(state.output.masterVolume, 5000)
        let outcome = state.apply(
            Nrpn.setSingle(product: 0, device: 0, page: 0x7F, number: 1, value: 9000))
        XCTAssertTrue(outcome.slowChanged)
        XCTAssertEqual(state.output.headphoneVolume, 9000)
        // Headphone wins once present.
        XCTAssertEqual(state.output.masterVolume, 9000)
    }

    func testTunerInTuneWindow() {
        var tuner = Tuner()
        XCTAssertNil(tuner.inTune)
        tuner.deviance = 8192
        XCTAssertEqual(tuner.inTune, true)
        tuner.deviance = 8192 + 350
        XCTAssertEqual(tuner.inTune, true)
        tuner.deviance = 8192 + 351
        XCTAssertEqual(tuner.inTune, false)
        tuner.deviance = 0
        XCTAssertEqual(tuner.inTune, false)
    }

    func testRigNameStringUpdatesAndSignalsRigChange() {
        var state = DeviceState()
        let message = Nrpn.sysex(
            product: 0x00, device: 0x7F, function: Generated.fnStringParam,
            page: Generated.pageStrings, number: 1, values: Array("AC30\0".utf8)
        )
        let outcome = state.apply(message)
        XCTAssertEqual(state.rig.name, "AC30")
        XCTAssertTrue(outcome.slowChanged)
        XCTAssertEqual(outcome.events, [.stringTag(number: 1), .rigChanged])

        let author = Nrpn.sysex(
            product: 0x00, device: 0x7F, function: Generated.fnStringParam,
            page: Generated.pageStrings, number: 2, values: Array("Author".utf8)
        )
        let authorOutcome = state.apply(author)
        XCTAssertEqual(state.rig.author, "Author")
        XCTAssertTrue(authorOutcome.slowChanged)
        XCTAssertEqual(authorOutcome.events, [.stringTag(number: 2)])

        state.apply(
            Nrpn.sysex(
                product: 0x00, device: 0x7F, function: Generated.fnStringParam,
                page: Generated.pageStrings, number: 10, values: Array("JCM".utf8)
            ))
        state.apply(
            Nrpn.sysex(
                product: 0x00, device: 0x7F, function: Generated.fnStringParam,
                page: Generated.pageStrings, number: 32, values: Array("412".utf8)
            ))
        XCTAssertEqual(state.amp.name, "JCM")
        XCTAssertEqual(state.cabinet.name, "412")

        // An untracked string tag leaves the snapshot unchanged.
        let untracked = state.apply(
            Nrpn.sysex(
                product: 0x00, device: 0x7F, function: Generated.fnStringParam,
                page: Generated.pageStrings, number: 99, values: Array("x".utf8)
            ))
        XCTAssertFalse(untracked.slowChanged)
        XCTAssertEqual(untracked.events, [.stringTag(number: 99)])
    }

    func testEffectTypeStateAndMixFoldIntoSlot() {
        var state = DeviceState()
        var outcome = state.apply(
            Nrpn.setSingle(
                product: 0x00, device: 0x7F, page: 0x3D, number: Generated.effectParamType,
                value: 179
            ))
        XCTAssertEqual(outcome.events, [.effectChanged(slot: 7)])
        XCTAssertTrue(outcome.slowChanged)
        XCTAssertEqual(state.effects[7].kind, 179)
        XCTAssertEqual(state.effects[7].typeName, "Easy Reverb")

        outcome = state.apply(
            Nrpn.setSingle(
                product: 0x00, device: 0x7F, page: 0x3D, number: Generated.effectParamState,
                value: 1
            ))
        XCTAssertEqual(outcome.events, [.effectChanged(slot: 7)])
        XCTAssertEqual(state.effects[7].on, true)

        outcome = state.apply(
            Nrpn.setSingle(
                product: 0x00, device: 0x7F, page: 0x3D, number: Generated.effectParamMix,
                value: 8192
            ))
        XCTAssertEqual(outcome.events, [.effectChanged(slot: 7)])
        XCTAssertEqual(state.effect("rev")?.mix, 8192)
    }

    func testMeterBlockFillsStatusAndIsFast() {
        var state = DeviceState()
        var values = [UInt16](repeating: 0, count: 11)
        values[3] = 4096
        values[4] = 9000
        values[6] = 12000
        values[9] = 3000
        let message = Nrpn.sysex(
            product: 0x00, device: 0x00, function: Generated.fnMultiParam,
            page: Generated.pageRealtime, number: Generated.meterBlockNumber,
            values: meterBlock(values)
        )
        let outcome = state.apply(message)
        XCTAssertEqual(outcome.events, [.status(state.status)])
        XCTAssertFalse(outcome.slowChanged)
        XCTAssertEqual(state.status.strobePhase, 4096)
        XCTAssertEqual(state.status.stackLevel, 9000)
        XCTAssertEqual(state.status.rigOutLevel, 12000)
        XCTAssertEqual(state.status.loudness, 3000)
        XCTAssertEqual(state.status.raw, values)
        XCTAssertTrue(state.status.strobeActive)
    }

    func testBeatPulseIsFastAndTouchesNothing() {
        var state = DeviceState()
        let before = state
        var outcome = state.apply(
            Nrpn.setSingle(
                product: 0, device: 0, page: Generated.pageRealtime,
                number: Generated.beatPulseNumber, value: 16383
            ))
        XCTAssertEqual(outcome.events, [.beatPulse(on: true)])
        XCTAssertFalse(outcome.slowChanged)
        XCTAssertEqual(state, before)

        outcome = state.apply(
            Nrpn.setSingle(
                product: 0, device: 0, page: Generated.pageRealtime,
                number: Generated.beatPulseNumber, value: 0
            ))
        XCTAssertEqual(outcome.events, [.beatPulse(on: false)])
        XCTAssertFalse(outcome.slowChanged)
    }

    func testTempoAndRigVolume() {
        var state = DeviceState()
        let tempo = state.apply(
            Nrpn.setSingle(
                product: 0, device: 0, page: Generated.pageRigSettings,
                number: Generated.tempoNumber, value: 7680
            ))
        XCTAssertEqual(tempo.events, [.tempoBpm(120)])
        XCTAssertTrue(tempo.slowChanged)
        XCTAssertEqual(state.rig.tempoBpm, 120)

        let volume = state.apply(
            Nrpn.setSingle(
                product: 0, device: 0, page: Generated.pageRigSettings,
                number: Generated.rigVolumeNumber, value: 4096
            ))
        XCTAssertTrue(volume.slowChanged)
        XCTAssertEqual(state.rig.volume, 4096)
    }

    func testAmpAndOutputVolumes() {
        var state = DeviceState()
        XCTAssertTrue(
            state.apply(
                Nrpn.setSingle(
                    product: 0, device: 0, page: Generated.ampPage,
                    number: Generated.ampOnNumber, value: 1
                )
            ).slowChanged)
        XCTAssertEqual(state.amp.on, true)

        let gain = state.apply(
            Nrpn.setSingle(
                product: 0, device: 0, page: Generated.ampPage,
                number: Generated.gainNumber, value: 5000
            ))
        XCTAssertTrue(gain.slowChanged)
        XCTAssertEqual(state.amp.gain, 5000)
        XCTAssertEqual(gain.events, [.paramChanged(page: 0x0A, number: 4, value: 5000)])

        XCTAssertTrue(
            state.apply(
                Nrpn.setSingle(
                    product: 0, device: 0, page: Generated.systemPage,
                    number: Generated.mainVolumeNumber, value: 9000
                )
            ).slowChanged)
        XCTAssertEqual(state.output.mainVolume, 9000)

        XCTAssertTrue(
            state.apply(
                Nrpn.setSingle(
                    product: 0, device: 0, page: Generated.systemPage,
                    number: Generated.monitorVolumeNumber, value: 3000
                )
            ).slowChanged)
        XCTAssertEqual(state.output.monitorVolume, 3000)
    }

    func testUntrackedGenericParamIsNotSlow() {
        var state = DeviceState()
        let outcome = state.apply(
            Nrpn.setSingle(
                product: 0, device: 0, page: 0x09, number: 3, value: 5000
            ))
        XCTAssertFalse(outcome.slowChanged)
        XCTAssertEqual(outcome.events, [.paramChanged(page: 0x09, number: 3, value: 5000)])
    }

    func testRigLoadDumpRoutesMultipleValues() {
        var state = DeviceState()
        let (typeMsb, typeLsb) = Nrpn.u14Split(33)
        let values: [UInt8] = [typeMsb, typeLsb, 0x10, 0x00, 0x20, 0x00, 0x00, 0x01]
        let message = Nrpn.sysex(
            product: 0, device: 0, function: Generated.fnMultiParam,
            page: 0x32, number: 0, values: values
        )
        let outcome = state.apply(message)
        XCTAssertTrue(outcome.slowChanged)
        XCTAssertEqual(
            outcome.events,
            [
                .effectChanged(slot: 0),
                .paramChanged(page: 0x32, number: 1, value: Nrpn.u14(0x10, 0x00)),
                .paramChanged(page: 0x32, number: 2, value: Nrpn.u14(0x20, 0x00)),
                .effectChanged(slot: 0),
            ])
        XCTAssertEqual(state.effects[0].kind, 33)
        XCTAssertEqual(state.effects[0].typeName, "Green Scream")
        XCTAssertEqual(state.effects[0].on, true)
    }

    func testNonKemperMessageIsIgnored() {
        var state = DeviceState()
        XCTAssertEqual(state.apply([0xB0, 0x20, 0x01]), ApplyOutcome())
        XCTAssertEqual(state.apply([]), ApplyOutcome())
    }

    func testExtStringRecoversAmpName() {
        var state = DeviceState()
        let outcome = state.apply(
            extString(page: Generated.pageStrings, number: 10, text: "JCM800"))
        XCTAssertEqual(state.amp.name, "JCM800")
        XCTAssertTrue(outcome.slowChanged)
        XCTAssertEqual(outcome.events, [.stringTag(number: 10)])
    }

    func testExtStringRigNameSignalsRigChange() {
        var state = DeviceState()
        let outcome = state.apply(
            extString(
                page: Generated.pageStrings, number: Generated.stringRigName, text: "AC30"
            ))
        XCTAssertEqual(state.rig.name, "AC30")
        XCTAssertEqual(outcome.events, [.stringTag(number: 1), .rigChanged])
    }

    func testExtStringOffStringPageIsIgnored() {
        var state = DeviceState()
        XCTAssertEqual(state.apply(extString(page: 0x0A, number: 0, text: "nope")), ApplyOutcome())
    }

    func testMorphSetsPosition() {
        var state = DeviceState()
        let outcome = state.apply(
            Nrpn.setSingle(
                product: 0, device: 0, page: Generated.pageMorph,
                number: Generated.morphNumber, value: 8192
            ))
        XCTAssertEqual(outcome.events, [.morphChanged(8192)])
        XCTAssertTrue(outcome.slowChanged)
        XCTAssertEqual(state.morph, 8192)
    }

    func testTunerDevianceIsFastAndNoteIsSlow() {
        var state = DeviceState()
        let deviance = state.apply(
            Nrpn.setSingle(
                product: 0, device: 0, page: Generated.pageRealtime,
                number: Generated.tunerDevianceNumber, value: 8192
            ))
        XCTAssertEqual(deviance.events, [.tunerDeviance(8192)])
        XCTAssertFalse(deviance.slowChanged)
        XCTAssertEqual(state.tuner.inTune, true)

        let note = state.apply(
            Nrpn.setSingle(
                product: 0, device: 0, page: Generated.pageTunerNote,
                number: Generated.tunerNoteNumber, value: 45
            ))
        XCTAssertEqual(note.events, [.tunerNote(45)])
        XCTAssertTrue(note.slowChanged)
        XCTAssertEqual(state.tuner.note, 45)
    }

    func testRenderedStringReplyIsFastEvent() {
        var state = DeviceState()
        let (msb, lsb) = Nrpn.u14Split(8192)
        let message = Nrpn.sysex(
            product: 0x02, device: 0x7F, function: Generated.fnRenderedStringReply,
            page: 0x3C, number: 53, values: [msb, lsb] + Array("<0.0>".utf8) + [0]
        )
        let outcome = state.apply(message)
        XCTAssertFalse(outcome.slowChanged)
        XCTAssertEqual(
            outcome.events,
            [
                .renderedString(page: 0x3C, number: 53, value: 8192, text: "<0.0>")
            ])
    }
}

// MARK: - Session handshake parsing

final class SessionParsingTests: XCTestCase {
    func testParsesCRLFListTerminatedByDot() {
        XCTAssertEqual(
            Session.parseProtocolList(Array("{AAA}\r\n{BBB}\r\n.\r\n".utf8)),
            ["{AAA}", "{BBB}"]
        )
        XCTAssertEqual(Session.parseProtocolList([]), [])
        XCTAssertEqual(Session.parseProtocolList(Array(".\r\n".utf8)), [])
    }

    func testResponseTailIsTheBytesAfterTheAckLine() {
        let response = Array("+{AAA}\r\n".utf8) + [0x14, 0xF0, 0x00, 0x20]
        let outcome = HandshakeOutcome(
            greeting: [], offered: ["{AAA}"], selected: "{AAA}", response: response
        )
        XCTAssertEqual(outcome.responseTail, [0x14, 0xF0, 0x00, 0x20])

        let noTail = HandshakeOutcome(
            greeting: [], offered: [], selected: "", response: Array("+ok".utf8)
        )
        XCTAssertEqual(noTail.responseTail, [])
    }

    func testSessionPreambleIsEightZeroBytes() {
        XCTAssertEqual(Session.sessionPreamble, [UInt8](repeating: 0, count: 8))
    }
}

// MARK: - Formatting helpers

final class FmtTests: XCTestCase {
    func testHexRoundTrip() {
        let bytes: [UInt8] = [0x00, 0x0F, 0xF0, 0xFF]
        XCTAssertEqual(Fmt.hex(bytes), "000ff0ff")
        XCTAssertEqual(Fmt.bytes(fromHex: "000FF0FF"), bytes)
        XCTAssertNil(Fmt.bytes(fromHex: "abc"))
        XCTAssertNil(Fmt.bytes(fromHex: "zz"))
    }

    func testAsciiOrHex() {
        XCTAssertEqual(Fmt.asciiOrHex(Array("AC30".utf8)), "\"AC30\"")
        XCTAssertEqual(Fmt.asciiOrHex([0xF0, 0x00]), "[f0 00]")
    }
}

// MARK: - CBOR channel

final class CborTests: XCTestCase {
    func testEncodesWithMinimalLengthHeads() {
        XCTAssertEqual(
            Fmt.hex(Cbor.encode(Cbor.paramWrite(addr: 15953, value: 0))), "c18301193e5100")
        XCTAssertEqual(
            Fmt.hex(Cbor.encode(Cbor.paramWrite(addr: 102405, value: 19))), "c183011a0001900513")
    }

    func testEncodesTheStateDumpRequest() {
        XCTAssertEqual(Fmt.hex(Cbor.encode(Cbor.stateDumpRequest())), "c183011a0001908001")
    }

    func testRoundTripsThroughTheDecoder() {
        for item in [
            Cbor.paramWrite(addr: 102528, value: 1),
            Cbor.paramWrite(addr: 0, value: 0),
            Cbor.paramWrite(addr: 16383, value: -1),
        ] {
            var decoder = CBORDecoder()
            XCTAssertEqual(decoder.push(Cbor.encode(item)), [item])
            XCTAssertEqual(decoder.pending, 0)
        }
    }

    func testDecoderSkipsInterItemFiller() {
        var bytes: [UInt8] = [Generated.cborFillerByte, Generated.cborFillerByte]
        let item = Cbor.paramWrite(addr: 1412, value: 8629)
        bytes.append(contentsOf: Cbor.encode(item))
        var decoder = CBORDecoder()
        XCTAssertEqual(decoder.push(bytes), [item])
        XCTAssertEqual(decoder.fillerBytes, 2)
    }

    func testExtractsPositionFromAMultiRun() {
        // tag(1)([2, 100700, 0, 1, 2]): 100700, then bank 100701 and slot 100702.
        let run = CBORValue.tag(
            1, .array([.uint(2), .uint(100_700), .uint(0), .uint(1), .uint(2)]))
        let snap = Cbor.extractSnapshot([run])
        XCTAssertEqual(snap.currentBank, 1)
        XCTAssertEqual(snap.currentRigSlot, 2)
        XCTAssertTrue(snap.isComplete)
    }

    func testExtractsPositionFromSingleItems() {
        let snap = Cbor.extractSnapshot([
            Cbor.paramWrite(addr: 100_701, value: 3),
            Cbor.paramWrite(addr: 100_702, value: 4),
        ])
        XCTAssertEqual(snap.currentBank, 3)
        XCTAssertEqual(snap.currentRigSlot, 4)
    }

    func testCollectsStringsAndRedactsSecrets() {
        let name = CBORValue.tag(1, .array([.uint(4), .uint(1), .text("Maz 18 Pushed")]))
        let secret = CBORValue.tag(
            1, .array([.uint(4), .uint(UInt64(Generated.sensitiveAddresses[0])), .text("hunter2")]))
        let snap = Cbor.extractSnapshot([name, secret])
        XCTAssertEqual(snap.string(1), "Maz 18 Pushed")
        XCTAssertEqual(snap.string(Generated.sensitiveAddresses[0]), Generated.redactedPlaceholder)
    }
}
