import Foundation

/// MIDI3 stream framing — the wrapper that carries MIDI over TCP on the
/// streaming protocol.
///
/// Established by observed experimentation. The stream is a sequence of 4-byte
/// frames, `[tag][b0][b1][b2]`:
///
/// - `0x14` — continuation group: all 3 bytes are valid, more groups follow.
/// - `0x15` — final group, 1 valid byte  (b0), message ends.
/// - `0x16` — final group, 2 valid bytes (b0, b1), message ends.
/// - `0x17` — final group, 3 valid bytes (b0, b1, b2), message ends.
///
/// Concatenating the valid bytes of the groups making up a message yields a raw
/// MIDI message (typically a Kemper SysEx `F0 00 20 33 … F7`).
public enum Midi3 {
    /// Continuation frame tag: 3 valid bytes, message continues.
    public static let tagContinuation: UInt8 = Generated.midi3TagContinuation

    /// A streaming de-framer. Feed it raw bytes; it yields complete MIDI
    /// messages.
    public struct Unframer: Sendable {
        /// Leftover bytes that don't yet complete a 4-byte frame.
        private var partial: [UInt8] = []
        /// Bytes accumulated for the in-progress MIDI message.
        private var current: [UInt8] = []

        public init() {}

        /// Bytes buffered but not yet yielded as a complete message.
        public var pending: Int { partial.count + current.count }

        /// Feed raw stream bytes; return any MIDI messages completed by this
        /// input.
        ///
        /// A frame whose tag is not `0x14...0x17` is treated as a desync: the
        /// in-progress message is discarded and one byte skipped to resync.
        public mutating func push(_ data: [UInt8]) -> [[UInt8]] {
            partial.append(contentsOf: data)
            var out = [[UInt8]]()
            var cursor = 0

            while partial.count - cursor >= 4 {
                let tag = partial[cursor]
                let valid: Int
                switch tag {
                case Generated.midi3TagContinuation: valid = 3
                case Generated.midi3TagFinal1: valid = 1
                case Generated.midi3TagFinal2: valid = 2
                case Generated.midi3TagFinal3: valid = 3
                default:
                    // Unknown tag — resync by dropping one byte.
                    cursor += 1
                    current.removeAll(keepingCapacity: true)
                    continue
                }
                current.append(contentsOf: partial[(cursor + 1)...(cursor + valid)])
                cursor += 4
                if tag != Generated.midi3TagContinuation {
                    out.append(current)
                    current.removeAll(keepingCapacity: true)
                }
            }
            if cursor > 0 { partial.removeFirst(cursor) }
            return out
        }
    }

    /// Frame a raw MIDI message into MIDI3 wire format (inverse of `Unframer`).
    ///
    /// Splits into 3-byte groups: full non-final groups get `0x14`; the final
    /// group gets `0x15`/`0x16`/`0x17` for 1/2/3 valid bytes (padded to 3).
    public static func frame(_ msg: [UInt8]) -> [UInt8] {
        var out = [UInt8]()
        out.reserveCapacity(((msg.count + 2) / 3) * 4)
        let groupCount = max((msg.count + 2) / 3, 1)
        var index = 0
        var start = 0
        while start < msg.count {
            let end = min(start + 3, msg.count)
            let chunk = Array(msg[start..<end])
            let isLast = index + 1 == groupCount
            let tag = isLast ? Generated.midi3TagContinuation + UInt8(chunk.count) : tagContinuation
            out.append(tag)
            out.append(contentsOf: chunk)
            out.append(contentsOf: [UInt8](repeating: 0, count: 3 - chunk.count))
            start = end
            index += 1
        }
        return out
    }

    /// True if `msg` is a Kemper SysEx (`F0 00 20 33 … F7`).
    public static func isKemperSysEx(_ msg: [UInt8]) -> Bool {
        msg.count >= 5
            && msg[0] == 0xF0
            && Array(msg[1..<4]) == Generated.manufacturerId
            && msg[msg.count - 1] == 0xF7
    }
}
