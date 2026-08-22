// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "LibKP",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "LibKP", targets: ["LibKP"]),
        .executable(name: "meters", targets: ["meters"]),
        .executable(name: "MetersApp", targets: ["MetersApp"]),
    ],
    targets: [
        .target(name: "LibKP"),
        .executableTarget(name: "meters", dependencies: ["LibKP"]),
        .executableTarget(name: "MetersApp", dependencies: ["LibKP"]),
        .testTarget(name: "LibKPTests", dependencies: ["LibKP"]),
    ]
)
