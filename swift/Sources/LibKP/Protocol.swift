import Foundation

/// The discovery ("DSCV") wire encoding — the *TagStream*.
///
/// A payload is an optional 4-byte ASCII header followed by a series of
/// length-prefixed fields. Each field is `[len: UInt8][content: len-1 bytes]`,
/// where **`len` is inclusive of the length byte itself** (so an empty field is
/// `0x00`, and the content length is `len - 1`). A `0x00` byte terminates the
/// stream.
///
/// The 34-byte poll request is:
/// ```text
/// "DSCV"  0x16 "MAC#00:00:00:00:00:00"  0x07 "POLL:)"  0x00
/// ```
///
/// The encoding and the poll layout come from observed experimentation with the
/// device on the LAN.
public enum KemperProtocol {
    /// The single UDP/TCP port used for both discovery and sessions (5727).
    public static let port: UInt16 = Generated.port

    /// The 4-byte header that opens a discovery poll request.
    public static let discoveryHeader: [UInt8] = Array(Generated.discoveryHeader.utf8)

    /// Build the discovery poll request packet.
    ///
    /// `mac` is the client's own MAC address string. The device tracks it but
    /// replies regardless, so the all-zero placeholder
    /// `"00:00:00:00:00:00"` works fine for probing.
    public static func buildPollRequest(mac: String = Generated.pollPlaceholderMac) -> [UInt8] {
        var out = [UInt8]()
        out.reserveCapacity(40)
        out.append(contentsOf: discoveryHeader)
        pushField(&out, Array((Generated.pollMacPrefix + mac).utf8))
        pushField(&out, Array(Generated.pollPayload.utf8))
        out.append(0x00)  // stream terminator
        return out
    }

    /// Append one length-prefixed field. The `len` byte is inclusive of itself.
    private static func pushField(_ out: inout [UInt8], _ content: [UInt8]) {
        precondition(content.count < Int(UInt8.max), "TagStream field too long")
        out.append(UInt8(content.count + 1))
        out.append(contentsOf: content)
    }
}

/// A decoded TagStream payload.
public struct TagStream: Equatable, Sendable {
    /// Leading 4-byte ASCII header, if the payload looked like it had one.
    public let header: [UInt8]?
    /// Length-prefixed fields, content only (length byte stripped).
    public let fields: [[UInt8]]

    public init(header: [UInt8]?, fields: [[UInt8]]) {
        self.header = header
        self.fields = fields
    }

    /// Best-effort parse of a received payload.
    ///
    /// If the first four bytes are printable ASCII and the fifth byte is a
    /// plausible field length, they are taken as a header; otherwise fields are
    /// read from offset 0.
    public static func parse(_ buf: [UInt8]) throws -> TagStream {
        guard !buf.isEmpty else { throw ParseError.tooShort(need: 1, got: 0) }

        let (header, start) = detectHeader(buf)
        var fields = [[UInt8]]()
        var off = start

        while off < buf.count {
            let len = Int(buf[off])
            if len == 0 { break }  // terminator / empty field
            let contentStart = off + 1
            let end = contentStart + (len - 1)
            guard end <= buf.count else {
                throw ParseError.fieldOverrun(offset: off, len: len, remaining: buf.count - off - 1)
            }
            fields.append(Array(buf[contentStart..<end]))
            off = end
        }
        return TagStream(header: header, fields: fields)
    }

    /// Split each field into its 4-char ASCII key and value bytes.
    ///
    /// Discovery-reply fields are `[4-char key][value]` (e.g. `NAME`, `SER#`).
    /// Fields whose first four bytes are not printable are skipped.
    public func keyValues() -> [(key: String, value: [UInt8])] {
        fields.compactMap { field in
            guard field.count >= 4, field[0..<4].allSatisfy({ Fmt.isGraphic($0) }) else {
                return nil
            }
            let key = String(decoding: field[0..<4], as: UTF8.self)
            return (key, Array(field[4...]))
        }
    }

    /// The value of the first field whose 4-char key equals `key`, as text.
    public func stringValue(forKey key: String) -> String? {
        keyValues().first { $0.key == key }.map { String(decoding: $0.value, as: UTF8.self) }
    }

    /// Decide whether `buf` opens with a 4-byte ASCII header.
    private static func detectHeader(_ buf: [UInt8]) -> ([UInt8]?, Int) {
        guard buf.count >= 5, buf[0..<4].allSatisfy({ Fmt.isGraphic($0) }) else { return (nil, 0) }
        // The fifth byte should be a plausible inclusive field length.
        let len = Int(buf[4])
        guard len == 0 || 4 + len <= buf.count else { return (nil, 0) }
        return (Array(buf[0..<4]), 4)
    }
}
