import Foundation

/// Small formatting helpers shared by the library and its examples.
public enum Fmt {
    /// Lowercase, unseparated hex — the canonical form the shared conformance
    /// vectors compare against.
    public static func hex(_ bytes: [UInt8]) -> String {
        var out = String()
        out.reserveCapacity(bytes.count * 2)
        for b in bytes { out += String(format: "%02x", b) }
        return out
    }

    /// Parse a lowercase/uppercase hex string into bytes. Returns `nil` if the
    /// string has an odd length or a non-hex character.
    public static func bytes(fromHex string: String) -> [UInt8]? {
        let chars = Array(string.utf8)
        guard chars.count % 2 == 0 else { return nil }
        var out = [UInt8]()
        out.reserveCapacity(chars.count / 2)
        var i = 0
        while i < chars.count {
            guard let hi = nibble(chars[i]), let lo = nibble(chars[i + 1]) else { return nil }
            out.append(hi << 4 | lo)
            i += 2
        }
        return out
    }

    private static func nibble(_ c: UInt8) -> UInt8? {
        switch c {
        case 0x30...0x39: return c - 0x30
        case 0x61...0x66: return c - 0x61 + 10
        case 0x41...0x46: return c - 0x41 + 10
        default: return nil
        }
    }

    /// One-line hex, space separated, for compact logging.
    public static func hexInline(_ bytes: [UInt8]) -> String {
        bytes.map { String(format: "%02x", $0) }.joined(separator: " ")
    }

    /// Render bytes as a quoted ASCII string when fully printable, else as hex.
    public static func asciiOrHex(_ bytes: [UInt8]) -> String {
        let printable = !bytes.isEmpty && bytes.allSatisfy { isGraphic($0) || $0 == 0x20 }
        if printable {
            return "\"\(String(decoding: bytes, as: UTF8.self))\""
        }
        return "[\(hexInline(bytes))]"
    }

    /// Classic offset / hex / ASCII hexdump, each line prefixed with `indent`.
    public static func hexdump(_ bytes: [UInt8], indent: String = "") -> String {
        var out = String()
        var offset = 0
        while offset < bytes.count {
            let chunk = Array(bytes[offset..<min(offset + 16, bytes.count)])
            var hexPart = ""
            var asciiPart = ""
            for (j, b) in chunk.enumerated() {
                if j == 8 { hexPart += " " }
                hexPart += String(format: "%02x ", b)
                asciiPart.append(isGraphic(b) || b == 0x20 ? Character(UnicodeScalar(b)) : ".")
            }
            out += indent + String(format: "%04x  ", offset)
            out += hexPart.padding(toLength: 49, withPad: " ", startingAt: 0)
            out += " " + asciiPart + "\n"
            offset += 16
        }
        return out
    }

    /// ASCII graphic range (`!`..`~`).
    static func isGraphic(_ b: UInt8) -> Bool { b > 0x20 && b < 0x7F }

    /// Decode bytes up to (but not including) the first NUL as Latin-1 text.
    /// The device sends single-byte characters; this never fails.
    static func textUntilNul(_ bytes: ArraySlice<UInt8>) -> String {
        var scalars = String.UnicodeScalarView()
        for b in bytes {
            if b == 0 { break }
            scalars.append(UnicodeScalar(b))
        }
        return String(scalars)
    }
}
