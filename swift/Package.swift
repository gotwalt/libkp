// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "LibKP",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "LibKP", targets: ["LibKP"]),
        .executable(name: "meters", targets: ["meters"]),
    ],
    targets: [
        .target(name: "LibKP"),
        .executableTarget(name: "meters", dependencies: ["LibKP"]),
        .testTarget(name: "LibKPTests", dependencies: ["LibKP"]),
    ]
)
