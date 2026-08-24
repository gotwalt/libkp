import Foundation

/// Kemper NRPN-over-SysEx message helpers.
///
/// Layout, per the Kemper MIDI Parameter Documentation and PySwitch:
/// `F0 00 20 33 <product> <device> <function> <instance=00> <page> <number> <value…> F7`
///
/// The **bidirectional beacon** (function `$7E`) asks the Profiler to start
/// streaming a selected parameter set and to send a status "sense" message
/// roughly every 500 ms. It must be re-sent inside the time lease to stay alive.
public enum Nrpn {
    // MARK: - 14-bit value helpers

    /// Combine an MSB/LSB pair of 7-bit bytes into a 14-bit value (0–16383).
    public static func u14(_ msb: UInt8, _ lsb: UInt8) -> UInt16 {
        (UInt16(msb & 0x7F) << 7) | UInt16(lsb & 0x7F)
    }

    /// Split a 14-bit value into (MSB, LSB) 7-bit bytes.
    public static func u14Split(_ value: UInt16) -> (msb: UInt8, lsb: UInt8) {
        (UInt8((value >> 7) & 0x7F), UInt8(value & 0x7F))
    }

    // MARK: - Builders

    /// Build a Kemper SysEx message with the standard header:
    /// `F0 00 20 33 <product> <device> <function> <instance=0> <page> <number> <values…> F7`.
    public static func sysex(
        product: UInt8,
        device: UInt8,
        function: UInt8,
        page: UInt8,
        number: UInt8,
        values: [UInt8] = []
    ) -> [UInt8] {
        var msg: [UInt8] = [0xF0]
        msg.append(contentsOf: Generated.manufacturerId)
        msg.append(contentsOf: [product, device, function, 0x00, page, number])
        msg.append(contentsOf: values)
        msg.append(0xF7)
        return msg
    }

    /// Request a string parameter (function `$43`). The device replies with a
    /// `$03` string message. Read-only — it does not change device state.
    public static func requestString(
        product: UInt8, device: UInt8, page: UInt8, number: UInt8
    ) -> [UInt8] {
        sysex(
            product: product, device: device, function: Generated.fnRequestString, page: page,
            number: number)
    }

    /// Request a single numeric parameter (function `$41`). The reply arrives
    /// as `$01`. Read-only — a nonexistent address is silently ignored.
    public static func requestSingle(
        product: UInt8, device: UInt8, page: UInt8, number: UInt8
    ) -> [UInt8] {
        sysex(
            product: product, device: device, function: Generated.fnRequestSingle, page: page,
            number: number)
    }

    /// Request all numeric parameters of a unit (function `$42`). The reply
    /// arrives as a `$02` Multi Parameter Change; decode its value block with
    /// ``multiValues(number:values:)``. The request must address the *first*
    /// controller number of the unit or the device ignores it.
    public static func requestMulti(
        product: UInt8, device: UInt8, page: UInt8, number: UInt8
    ) -> [UInt8] {
        sysex(
            product: product, device: device, function: Generated.fnRequestMulti, page: page,
            number: number)
    }

    /// Set a single numeric parameter (function `$01`, Single Parameter
    /// Change). **Mutating.** `value` is 14-bit; for a switch parameter use
    /// 1 (on) / 0 (off).
    public static func setSingle(
        product: UInt8, device: UInt8, page: UInt8, number: UInt8, value: UInt16
    ) -> [UInt8] {
        let (msb, lsb) = u14Split(value)
        return sysex(
            product: product, device: device, function: Generated.fnSingleParam,
            page: page, number: number, values: [msb, lsb]
        )
    }

    /// Request an extended-address numeric parameter (function `$46`) at a flat
    /// address (`page * 128 + number`, or an extended address at or above
    /// 16384). The device replies with a `$06` Extended Parameter — or a plain
    /// `$01` when the address fits in 14 bits. Read-only.
    ///
    /// Layout mirrors `$06` minus the value:
    /// `F0 00 20 33 <prod> <dev> 46 <inst=00> <5-byte address> F7`. This is how
    /// the device's current bank and rig slot are read on the streaming session
    /// — the addresses are ``Generated/currentBankAddress`` and
    /// ``Generated/currentRigSlotAddress``.
    public static func requestExtendedParam(
        product: UInt8, device: UInt8, address: UInt32
    ) -> [UInt8] {
        var msg: [UInt8] = [0xF0]
        msg.append(contentsOf: Generated.manufacturerId)
        msg.append(contentsOf: [product, device, Generated.fnRequestExtParam, 0x00])
        msg.append(contentsOf: extEncode(UInt64(address), count: 5))
        msg.append(0xF7)
        return msg
    }

    /// Request an extended string parameter (function `$47`) at a flat address
    /// (`page * 128 + number`). The device replies with a `$07` extended string —
    /// or a plain `$03` when the address is below 16384. Read-only.
    ///
    /// Layout mirrors `$07` minus the payload:
    /// `F0 00 20 33 <prod> <dev> 47 <inst=00> <5-byte address> F7`. This is how
    /// the current bank's rig/amp/cabinet names are read on demand — the
    /// addresses come from ``Params/bankPreviewAddress(_:slot:)``.
    public static func requestExtendedString(
        product: UInt8, device: UInt8, address: UInt32
    ) -> [UInt8] {
        var msg: [UInt8] = [0xF0]
        msg.append(contentsOf: Generated.manufacturerId)
        msg.append(contentsOf: [product, device, Generated.fnRequestExtString, 0x00])
        msg.append(contentsOf: extEncode(UInt64(address), count: 5))
        msg.append(0xF7)
        return msg
    }

    /// Request a parameter value rendered to a string (function `$7C`). The
    /// device replies with a `$3C` message carrying the rendered ASCII (e.g.
    /// `<0.0>`). Read-only, but costly in device CPU.
    ///
    /// Layout: `<fn=7C> <flags=00> <page> <number> <valMSB> <valLSB>` — the
    /// flags byte occupies the instance slot of the standard header.
    public static func requestRenderedString(
        product: UInt8, device: UInt8, page: UInt8, number: UInt8, value: UInt16
    ) -> [UInt8] {
        let (msb, lsb) = u14Split(value)
        return sysex(
            product: product, device: device, function: Generated.fnRequestRenderedString,
            page: page, number: number, values: [msb, lsb]
        )
    }

    /// Build the bidirectional beacon SysEx (raw MIDI, `F0…F7`), as documented
    /// by PySwitch.
    ///
    /// - Parameters:
    ///   - init: set on the first beacon of a session.
    ///   - tuner: request tuner data in the stream.
    ///   - leaseSecs: keep-alive lease (encoded in 2-second steps); re-send
    ///     within half of it.
    ///   - paramSet: selected parameter-set id (PySwitch uses `0x02`).
    ///   - product: product type byte to address.
    public static func beacon(
        init isInit: Bool,
        tuner: Bool,
        leaseSecs: UInt8,
        paramSet: UInt8 = Generated.beaconDefaultParamSet,
        product: UInt8
    ) -> [UInt8] {
        var flags: UInt8 = 0
        if isInit { flags |= Generated.beaconFlagInit }
        flags |= Generated.beaconFlagSysex  // always on
        if tuner { flags |= Generated.beaconFlagTunemode }

        var msg: [UInt8] = [0xF0]
        msg.append(contentsOf: Generated.manufacturerId)
        msg.append(contentsOf: [
            product,
            Generated.deviceOmni,
            Generated.beaconFunction,
            0x00,  // instance
            Generated.beaconSubcommand,
            paramSet,
            flags,
            leaseSecs / 2,
        ])
        msg.append(0xF7)
        return msg
    }

    /// A 3-byte MIDI Control Change on `channel` (0–15). Channel, controller
    /// and value are all masked to their wire widths.
    public static func controlChange(channel: UInt8, controller: UInt8, value: UInt8) -> [UInt8] {
        [Generated.controlChangeStatus | (channel & 0x0F), controller & 0x7F, value & 0x7F]
    }

    /// A 2-byte MIDI Program Change on `channel` (0–15).
    public static func programChange(channel: UInt8, program: UInt8) -> [UInt8] {
        [Generated.programChangeStatus | (channel & 0x0F), program & 0x7F]
    }

    // MARK: - Parsers

    /// Decode a `$02` Multi Parameter Change value block: the values apply to
    /// **consecutive addresses** starting at `number`, each a 14-bit MSB/LSB
    /// pair. Returns `(number + i, value)` for each pair; a trailing odd byte is
    /// ignored.
    public static func multiValues(
        number: UInt8, values: [UInt8]
    ) -> [(number: UInt8, value: UInt16)] {
        var out = [(number: UInt8, value: UInt16)]()
        out.reserveCapacity(values.count / 2)
        var i = 0
        var step: UInt8 = 0
        while i + 1 < values.count {
            out.append((number &+ step, u14(values[i], values[i + 1])))
            i += 2
            step &+= 1
        }
        return out
    }

    /// Decode the 5-byte-per-field "extended" encoding: big-endian, 7 data bits
    /// per byte. Works on any slice length; a 5-byte input yields a 35-bit
    /// value. Shared by function `$06` (ext-param) and `$07` (ext-string).
    public static func extDecode(_ bytes: [UInt8]) -> UInt64 {
        bytes.reduce(UInt64(0)) { ($0 << 7) | UInt64($1 & 0x7F) }
    }

    /// Inverse of ``extDecode(_:)``: encode `value` big-endian into `count`
    /// bytes, 7 data bits each.
    public static func extEncode(_ value: UInt64, count: Int) -> [UInt8] {
        (0..<count).map { i in
            UInt8((value >> (7 * (count - 1 - i))) & 0x7F)
        }
    }

    /// Parse a function-`$07` Extended String Parameter message:
    /// `F0 00 20 33 <prod> <dev> 07 <inst> <5-byte address> <ascii…> 00 F7`.
    ///
    /// The address decodes via the 5×7 extended scheme. For a normal page it
    /// equals `page * 128 + number`, so an extended string on page 0 carries the
    /// same string-tag numbers as function `$03`.
    public static func parseExtendedString(_ msg: [UInt8]) -> (address: UInt32, text: String)? {
        // F0 + mfr(3) + prod + dev + fn + inst + 5-byte addr + NUL + F7 = 14 min.
        guard msg.count >= 14,
            msg[0] == 0xF0,
            Array(msg[1..<4]) == Generated.manufacturerId,
            msg[6] == Generated.fnExtStringParam,
            msg[msg.count - 1] == 0xF7
        else { return nil }
        // The scheme can carry 35 bits; an address past 32 is malformed, not
        // an address to wrap onto some other parameter.
        guard let address = UInt32(exactly: extDecode(Array(msg[8..<13]))) else { return nil }
        return (address, Fmt.textUntilNul(msg[13..<(msg.count - 1)]))
    }

    /// Parse a function-`$06` Extended Parameter message:
    /// `F0 00 20 33 <prod> <dev> 06 <inst> <5-byte address> <5-byte value> F7`.
    ///
    /// Both fields use the 5×7 extended scheme, so the value spans 35 bits
    /// rather than the 14 a `$01` carries — hence the 64-bit value. The device sends these unasked when
    /// an extended-address parameter changes, and in reply to
    /// ``requestExtendedParam(product:device:address:)``.
    public static func parseExtendedParam(_ msg: [UInt8]) -> (address: UInt32, value: UInt64)? {
        // F0 + mfr(3) + prod + dev + fn + inst + addr(5) + value(5) + F7 = 19.
        guard msg.count >= 19,
            msg[0] == 0xF0,
            Array(msg[1..<4]) == Generated.manufacturerId,
            msg[6] == Generated.fnExtParam,
            msg[msg.count - 1] == 0xF7
        else { return nil }
        // The scheme can carry 35 bits; an address past 32 is malformed, not
        // an address to wrap onto some other parameter.
        guard let address = UInt32(exactly: extDecode(Array(msg[8..<13]))) else { return nil }
        return (address, extDecode(Array(msg[13..<18])))
    }

    /// Parse a `$3C` Rendered String reply — the response to
    /// ``requestRenderedString(product:device:page:number:value:)``. The reply
    /// mirrors the `$7C` request header, then carries the value pair and the
    /// rendered ASCII: `<fn=3C> <flags> <page> <number> <valMSB> <valLSB> <ascii…> 00`.
    ///
    /// Returns `nil` if it isn't a `$3C` reply or lacks the value pair.
    public static func parseRenderedString(
        _ msg: [UInt8]
    ) -> (page: UInt8, number: UInt8, value: UInt16, text: String)? {
        guard let (header, values) = NrpnHeader.parse(msg),
            header.function == Generated.fnRenderedStringReply,
            values.count >= 2
        else { return nil }
        let value = u14(values[0], values[1])
        return (header.page, header.number, value, Fmt.textUntilNul(values[2...]))
    }
}

/// A parsed Kemper NRPN/SysEx message header.
public struct NrpnHeader: Equatable, Sendable {
    public let product: UInt8
    public let device: UInt8
    public let function: UInt8
    public let instance: UInt8
    public let page: UInt8
    public let number: UInt8

    public init(
        product: UInt8, device: UInt8, function: UInt8, instance: UInt8, page: UInt8, number: UInt8
    ) {
        self.product = product
        self.device = device
        self.function = function
        self.instance = instance
        self.page = page
        self.number = number
    }

    /// Parse the fixed header of a Kemper SysEx, returning it along with the
    /// value bytes (everything between the header and the trailing `0xF7`).
    /// Returns `nil` if the message isn't a Kemper SysEx.
    public static func parse(_ msg: [UInt8]) -> (header: NrpnHeader, values: [UInt8])? {
        guard msg.count >= 11, msg[0] == 0xF0, Array(msg[1..<4]) == Generated.manufacturerId else {
            return nil
        }
        let header = NrpnHeader(
            product: msg[4], device: msg[5], function: msg[6],
            instance: msg[7], page: msg[8], number: msg[9]
        )
        return (header, Array(msg[10..<(msg.count - 1)]))
    }

    /// The human-readable name of this message's function code, if known.
    public var functionName: String? { Params.functionName(function) }
}
