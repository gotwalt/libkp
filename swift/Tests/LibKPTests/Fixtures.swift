import Foundation
import XCTest

/// Locates the shared spec fixtures relative to this source file:
/// `swift/Tests/LibKPTests/Fixtures.swift` → up four levels is the repo root.
enum Fixtures {
    static let repoRoot: URL = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent() // LibKPTests
        .deletingLastPathComponent() // Tests
        .deletingLastPathComponent() // swift
        .deletingLastPathComponent() // <repo root>

    static var vectorsDirectory: URL { repoRoot.appendingPathComponent("spec/vectors") }
    static var capturesDirectory: URL { repoRoot.appendingPathComponent("spec/captures") }

    /// Every `*.json` file in `spec/vectors`, sorted by name.
    static func vectorFiles() throws -> [URL] {
        try FileManager.default
            .contentsOfDirectory(at: vectorsDirectory, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "json" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
    }

    /// Load a JSON object from a URL.
    static func json(at url: URL) throws -> [String: Any] {
        let data = try Data(contentsOf: url)
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw Failure("\(url.lastPathComponent) is not a JSON object")
        }
        return object
    }

    /// Load a named vector file (e.g. `"u14"`).
    static func vector(_ name: String) throws -> [String: Any] {
        try json(at: vectorsDirectory.appendingPathComponent("\(name).json"))
    }

    /// Load a named capture fixture (e.g. `"rig-load.json"`).
    static func capture(_ file: String) throws -> [String: Any] {
        try json(at: capturesDirectory.appendingPathComponent(file))
    }

    struct Failure: Error, CustomStringConvertible {
        let description: String
        init(_ description: String) { self.description = description }
    }
}

/// Accessors that fail loudly rather than silently skipping a case.
extension Dictionary where Key == String, Value == Any {
    func cases(_ key: String, file: StaticString = #filePath, line: UInt = #line) -> [[String: Any]] {
        guard let list = self[key] as? [[String: Any]] else {
            XCTFail("missing array \"\(key)\"", file: file, line: line)
            return []
        }
        return list
    }

    func string(_ key: String, file: StaticString = #filePath, line: UInt = #line) -> String {
        guard let value = self[key] as? String else {
            XCTFail("missing string \"\(key)\"", file: file, line: line)
            return ""
        }
        return value
    }

    func int(_ key: String, file: StaticString = #filePath, line: UInt = #line) -> Int {
        guard let value = self[key] as? NSNumber else {
            XCTFail("missing number \"\(key)\"", file: file, line: line)
            return 0
        }
        return value.intValue
    }

    func bool(_ key: String, file: StaticString = #filePath, line: UInt = #line) -> Bool {
        guard let value = self[key] as? NSNumber else {
            XCTFail("missing bool \"\(key)\"", file: file, line: line)
            return false
        }
        return value.boolValue
    }

    /// A value that may legitimately be JSON `null`.
    func optionalString(_ key: String) -> String? {
        self[key] as? String
    }

    func u8(_ key: String, file: StaticString = #filePath, line: UInt = #line) -> UInt8 {
        UInt8(truncatingIfNeeded: int(key, file: file, line: line))
    }

    func u16(_ key: String, file: StaticString = #filePath, line: UInt = #line) -> UInt16 {
        UInt16(truncatingIfNeeded: int(key, file: file, line: line))
    }
}
